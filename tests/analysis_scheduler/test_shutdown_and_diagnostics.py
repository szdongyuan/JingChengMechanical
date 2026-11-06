"""Bounded shutdown and minimal process-local diagnostic tests."""

from __future__ import annotations

import dataclasses
from threading import Barrier, Condition, Event, Thread, current_thread

import pytest

from app.service.analysis.scheduler import (
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStatus,
    SchedulerCounterKey,
    SchedulerDiagnosticEvent,
    SchedulerDiagnosticEventKind,
    SchedulerLifecycle,
    ShutdownReport,
    ShutdownStatus,
    SubmitStatus,
    canonical_toolchain_lane_key,
)

from .fakes import (
    AudioResidencyProbe,
    CloneForbiddenAudioLoader,
    GatedShutdownCompletionClock,
    GatedResultExecutor,
    OutcomeCollector,
    PassThroughStartGate,
    RecordingExecutor,
    ThreadRecordingClock,
    WorkerDrainCompletionFaultClock,
    make_job,
)

_CREATED_SCHEDULERS: list[AnalysisScheduler] = []


def make_scheduler(
    *,
    loader: object,
    executor: object,
    sink: object,
    clock: object,
    diagnostic_sink: object | None = None,
    worker_count: int = 1,
    shutdown_timeout_seconds: float = 0.05,
) -> AnalysisScheduler:
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        ),
        audio_loader=loader,
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=sink,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    _CREATED_SCHEDULERS.append(scheduler)
    return scheduler


def counter(scheduler: AnalysisScheduler, key: SchedulerCounterKey) -> int:
    return dict(scheduler.diagnostics().counters)[key.value]


def successful_result() -> AnalysisExecutionResult:
    return AnalysisExecutionResult(
        status=AnalysisExecutionStatus.SUCCEEDED,
        result_payload=("late", "payload"),
    )


def join_scheduler_threads(scheduler: AnalysisScheduler) -> None:
    scheduler._control_thread.join(2.0)
    for worker in scheduler._workers:
        worker.join(2.0)
    assert not scheduler._control_thread.is_alive()
    assert not any(worker.is_alive() for worker in scheduler._workers)


@pytest.fixture(autouse=True)
def close_created_schedulers():
    yield
    while _CREATED_SCHEDULERS:
        scheduler = _CREATED_SCHEDULERS.pop()
        for port in (scheduler._audio_loader, scheduler._analysis_executor):
            release = getattr(port, "release", None)
            if callable(release):
                release()
        with scheduler._condition:
            assert scheduler._condition.wait_for(
                lambda: not scheduler._active,
                timeout=2.0,
            )
        scheduler.shutdown()
        join_scheduler_threads(scheduler)


@pytest.mark.parametrize(
    "invalid",
    [0.0, -1.0, float("nan"), float("inf"), 1, True, "0.1"],
)
def test_timeout_override_is_validated_before_state_or_frozen_report_lookup(
    invalid,
):
    clock = ThreadRecordingClock(1.0)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=OutcomeCollector(),
        clock=clock,
    )

    before = scheduler.diagnostics()
    with pytest.raises(ValueError):
        scheduler.shutdown(invalid)
    after = scheduler.diagnostics()
    assert after.lifecycle is before.lifecycle is SchedulerLifecycle.RUNNING
    assert after.counters == before.counters

    report = scheduler.shutdown()
    calls_after_report = clock.call_count
    with pytest.raises(ValueError):
        scheduler.shutdown(invalid)
    assert clock.call_count == calls_after_report
    assert scheduler.shutdown() is report


@pytest.mark.parametrize(
    "completion_fault",
    [
        object(),
        RuntimeError("planned completion clock failure"),
        7.0,
    ],
    ids=["wrong-type", "exception", "regression"],
)
def test_completion_clock_fault_still_freezes_report_and_releases_waiters(
    completion_fault,
):
    owner_name = "completion-fault-owner"
    waiter_name = "completion-fault-waiter"
    clock = GatedShutdownCompletionClock(
        owner_thread_name=owner_name,
        started_value=8.0,
        completion_value_or_exception=completion_fault,
    )
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=OutcomeCollector(),
        clock=clock,
        shutdown_timeout_seconds=0.2,
    )
    waiter_entered = Event()
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        if current_thread().name == waiter_name:
            waiter_entered.set()
        try:
            reports.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=close, name=owner_name)
    waiter = Thread(target=close, name=waiter_name)
    try:
        owner.start()
        assert clock.completion_entered.wait(2.0)
        waiter.start()
        assert waiter_entered.wait(2.0)
        assert waiter.is_alive()
        assert reports == []
        assert errors == []
        clock.release()
        owner.join(2.0)

        # Keep the pre-fix RED path recoverable when the owner escapes before
        # freezing a report and otherwise leaves its waiter blocked.
        if errors:
            with scheduler._condition:
                scheduler._shutdown_report = object()
                scheduler._shutdown_in_progress = False
                scheduler._condition.notify_all()
        waiter.join(2.0)

        assert errors == []
        assert len(reports) == 2
        assert reports[0] is reports[1]
        report = reports[0]
        assert report.status is ShutdownStatus.CLOSED
        assert report.started_monotonic == 8.0
        assert report.finished_monotonic == 8.0
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == ()
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is False
        assert scheduler._shutdown_report is report
        assert scheduler._shutdown_in_progress is False
        assert scheduler.diagnostics().lifecycle is SchedulerLifecycle.CLOSED
        calls_after_report = clock.call_count
        assert scheduler.shutdown(0.01) is report
        assert clock.call_count == calls_after_report
    finally:
        clock.release()
        owner.join(2.0)
        waiter.join(2.0)


@pytest.mark.parametrize(
    "completion_fault",
    [
        RuntimeError("planned completion clock failure"),
        20.0,
    ],
    ids=["exception", "regression"],
)
def test_completion_clock_fault_preserves_later_worker_drain_time(
    completion_fault,
):
    owner_name = "worker-drain-fault-owner"
    clock = WorkerDrainCompletionFaultClock(
        owner_thread_name=owner_name,
        completion_value_or_exception=completion_fault,
    )
    executor = GatedResultExecutor(result_or_exception=successful_result())
    outcomes = OutcomeCollector()

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        if (
            event.event_kind
            is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
        ):
            clock.enable_worker_terminal_value()
            executor.release()

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=outcomes,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
        shutdown_timeout_seconds=0.2,
    )
    job = make_job(82)
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        try:
            reports.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=close, name=owner_name)
    try:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        owner.start()
        owner.join(2.0)

        assert not owner.is_alive()
        assert errors == []
        assert len(reports) == 1
        report = reports[0]
        assert report.status is ShutdownStatus.CLOSED
        assert report.started_monotonic == 20.0
        assert report.finished_monotonic == 30.0
        assert report.control_thread_alive is False
        assert clock.worker_terminal_observed.is_set()
        assert clock.completion_call_count == 1
        assert outcomes.wait_for_count(1)[0].status is AnalysisStatus.CANCELED
        calls_after_report = clock.call_count
        assert scheduler.shutdown() is report
        assert clock.call_count == calls_after_report
    finally:
        executor.release()
        owner.join(2.0)


@pytest.mark.parametrize("callback_kind", ["outcome", "diagnostic"])
def test_worker_callback_shutdown_returns_transient_while_owner_finalizes(
    callback_kind,
    monkeypatch,
):
    callback_entered = Event()
    callback_done = Event()
    callback_waiting = Event()
    waiter_waiting = Event()
    callback_progress = Condition()
    executor = GatedResultExecutor(result_or_exception=successful_result())
    clock = ThreadRecordingClock(8.5)
    nested_reports: list[ShutdownReport] = []
    owner_reports: list[ShutdownReport] = []
    waiter_reports: list[ShutdownReport] = []
    snapshots: list[tuple[tuple[int, ...], bool, int, object]] = []
    errors: list[BaseException] = []
    holder: dict[str, AnalysisScheduler] = {}

    def callback_shutdown() -> None:
        callback_entered.set()
        try:
            scheduler = holder["scheduler"]
            worker_ids = tuple(
                worker_id
                for worker_id, worker in enumerate(
                    scheduler._workers,
                    start=1,
                )
                if worker.is_alive()
            )
            control_alive = scheduler._control_thread.is_alive()
            calls_before = clock.call_count
            nested_reports.append(scheduler.shutdown())
            snapshots.append(
                (
                    worker_ids,
                    control_alive,
                    clock.call_count - calls_before,
                    scheduler._shutdown_report,
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            callback_done.set()
            with callback_progress:
                callback_progress.notify_all()

    def outcome_sink(_outcome) -> None:
        if callback_kind == "outcome":
            callback_shutdown()

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        if (
            callback_kind == "diagnostic"
            and event.event_kind
            is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
        ):
            callback_shutdown()

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=outcome_sink,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
        shutdown_timeout_seconds=0.5,
    )
    holder["scheduler"] = scheduler
    original_wait_for = scheduler._condition.wait_for

    def observed_wait_for(predicate, timeout=None):
        if current_thread().name == "callback-external-waiter":
            waiter_waiting.set()
        elif (
            "analysis-scheduler-worker-" in current_thread().name
            and callback_entered.is_set()
            and not callback_done.is_set()
        ):
            callback_waiting.set()
            with callback_progress:
                callback_progress.notify_all()
        return original_wait_for(predicate, timeout)

    monkeypatch.setattr(
        scheduler._condition,
        "wait_for",
        observed_wait_for,
    )

    def wait_for_shutdown(target: list[ShutdownReport]) -> None:
        try:
            target.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    waiter = Thread(
        target=wait_for_shutdown,
        args=(waiter_reports,),
        name="callback-external-waiter",
    )
    owner = Thread(
        target=wait_for_shutdown,
        args=(owner_reports,),
        name="callback-outer-owner",
    )
    try:
        assert scheduler.submit(make_job(70)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        owner.start()
        assert executor.calls[0][2].wait(2.0)
        waiter.start()
        assert waiter_waiting.wait(2.0)
        executor.release()
        assert callback_entered.wait(2.0)

        # Keep the superseded wait-on-owner RED path bounded and reclaimable.
        with callback_progress:
            assert callback_progress.wait_for(
                lambda: callback_done.is_set() or callback_waiting.is_set(),
                timeout=2.0,
            )
        if callback_waiting.is_set():
            with scheduler._condition:
                scheduler._shutdown_report = ShutdownReport(
                    status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
                    started_monotonic=8.5,
                    finished_monotonic=8.5,
                    canceled_pending=0,
                    cancel_requested_running=1,
                    remaining_attempt_ids=(),
                    remaining_worker_ids=(1,),
                    remaining_run_lanes=(),
                    remaining_toolchain_lanes=(),
                    control_thread_alive=False,
                )
                scheduler._shutdown_in_progress = False
                scheduler._condition.notify_all()
        owner.join(2.0)
        waiter.join(2.0)

        assert errors == []
        assert len(nested_reports) == 1
        assert len(owner_reports) == len(waiter_reports) == 1
        transient = nested_reports[0]
        final = owner_reports[0]
        assert waiter_reports[0] is final
        assert transient is not final
        assert transient.status is ShutdownStatus.IN_PROGRESS
        assert transient.started_monotonic == 8.5
        assert transient.finished_monotonic >= transient.started_monotonic
        assert transient.canceled_pending == 0
        assert transient.cancel_requested_running == 1
        assert transient.remaining_attempt_ids == ()
        assert transient.remaining_worker_ids == snapshots[0][0] == (1,)
        assert transient.remaining_run_lanes == ()
        assert transient.remaining_toolchain_lanes == ()
        assert transient.control_thread_alive is snapshots[0][1]
        assert snapshots[0][2] == 0
        assert snapshots[0][3] is None
        assert final.status is ShutdownStatus.CLOSED
        assert scheduler._shutdown_in_progress is False
        assert scheduler.shutdown(0.5) is final
    finally:
        executor.release()
        owner.join(2.0)
        waiter.join(2.0)


def test_shutdown_freezes_one_worker_liveness_snapshot_for_all_callers(
    monkeypatch,
):
    owner_name = "liveness-owner"
    waiter_name = "liveness-waiter"
    clock = GatedShutdownCompletionClock(
        owner_thread_name=owner_name,
        started_value=9.0,
        completion_value_or_exception=9.0,
    )
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=OutcomeCollector(),
        clock=clock,
    )
    worker = scheduler._workers[0]
    waiter_entered = Event()
    liveness_reads = 0
    original_is_alive = worker.is_alive

    def changing_is_alive():
        nonlocal liveness_reads
        liveness_reads += 1
        if liveness_reads == 1:
            return True
        return False

    monkeypatch.setattr(worker, "is_alive", changing_is_alive)
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        if current_thread().name == waiter_name:
            waiter_entered.set()
        try:
            reports.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=close, name=owner_name)
    waiter = Thread(target=close, name=waiter_name)
    try:
        owner.start()
        assert clock.completion_entered.wait(2.0)
        waiter.start()
        assert waiter_entered.wait(2.0)
        assert waiter.is_alive()
        assert reports == []
        assert errors == []
        clock.release()
        owner.join(2.0)

        # Keep the double-read RED path from leaking its blocked waiter.
        if errors:
            with scheduler._condition:
                scheduler._shutdown_report = object()
                scheduler._shutdown_in_progress = False
                scheduler._condition.notify_all()
        waiter.join(2.0)

        assert errors == []
        assert len(reports) == 2
        assert reports[0] is reports[1]
        report = reports[0]
        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == (1,)
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is False
        assert liveness_reads == 1
        assert scheduler._shutdown_in_progress is False
        assert scheduler.shutdown(0.5) is report
    finally:
        clock.release()
        monkeypatch.setattr(worker, "is_alive", original_is_alive)
        owner.join(2.0)
        waiter.join(2.0)


def test_clean_shutdown_joins_threads_freezes_report_and_closes_admission():
    events: list[SchedulerDiagnosticEvent] = []
    clock = ThreadRecordingClock(2.0)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=OutcomeCollector(),
        clock=clock,
        diagnostic_sink=events.append,
    )

    report = scheduler.shutdown(0.2)

    assert report.status is ShutdownStatus.CLOSED
    assert report.canceled_pending == 0
    assert report.cancel_requested_running == 0
    assert report.remaining_attempt_ids == ()
    assert report.remaining_worker_ids == ()
    assert report.remaining_run_lanes == ()
    assert report.remaining_toolchain_lanes == ()
    assert report.control_thread_alive is False
    assert not scheduler._control_thread.is_alive()
    assert not any(worker.is_alive() for worker in scheduler._workers)
    diagnostics = scheduler.diagnostics()
    assert diagnostics.lifecycle is SchedulerLifecycle.CLOSED
    assert diagnostics.attempts == ()
    assert diagnostics.full_audio_residency_count == 0
    assert scheduler.submit(make_job(90)).status is SubmitStatus.NOT_ACCEPTING
    event_count = len(events)
    clock_calls = clock.call_count
    assert scheduler.shutdown(0.01) is report
    assert len(events) == event_count
    assert clock.call_count == clock_calls
    assert not any(
        event.event_kind is SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
        for event in events
    )


def test_running_drain_within_deadline_returns_clean_closed_report():
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=successful_result())
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=sink,
        clock=ThreadRecordingClock(3.0),
        shutdown_timeout_seconds=0.5,
    )
    job = make_job(1, toolchain_id="clean-lane")
    assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
    executor.wait_for_calls(1)
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        try:
            reports.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=close, name="clean-shutdown")
    thread.start()
    token = executor.calls[0][2]
    assert token.wait(1.0)
    executor.release()
    thread.join(2.0)

    assert errors == []
    assert reports[0].status is ShutdownStatus.CLOSED
    assert reports[0].cancel_requested_running == 1
    assert reports[0].remaining_attempt_ids == ()
    assert reports[0].control_thread_alive is False
    outcomes = sink.wait_for_count(1)
    assert outcomes[0].status is AnalysisStatus.CANCELED
    assert outcomes[0].result_payload is None
    diagnostics = scheduler.diagnostics()
    assert diagnostics.lifecycle is SchedulerLifecycle.CLOSED
    assert diagnostics.attempts == ()
    assert diagnostics.full_audio_residency_count == 0
    assert residency.current == 0
    assert not scheduler._control_thread.is_alive()
    assert not any(worker.is_alive() for worker in scheduler._workers)


def test_incomplete_report_is_exact_and_late_drain_does_not_rewrite_it():
    events: list[SchedulerDiagnosticEvent] = []
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=successful_result())
    sink = OutcomeCollector()
    clock = ThreadRecordingClock(4.0)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=sink,
        clock=clock,
        diagnostic_sink=events.append,
        shutdown_timeout_seconds=0.02,
    )
    running = make_job(1, run_id="z-run", toolchain_id="z-toolchain")
    pending = make_job(2, run_id="z-run", toolchain_id="z-toolchain")
    assert scheduler.submit(running).status is SubmitStatus.ACCEPTED
    executor.wait_for_calls(1)
    assert scheduler.submit(pending).status is SubmitStatus.ACCEPTED

    report = scheduler.shutdown()

    assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
    assert report.canceled_pending == 1
    assert report.cancel_requested_running == 1
    assert report.remaining_attempt_ids == (running.analysis_attempt_id,)
    assert report.remaining_worker_ids == (1,)
    assert report.remaining_run_lanes == (running.run_id,)
    assert report.remaining_toolchain_lanes == (
        canonical_toolchain_lane_key("z-toolchain"),
    )
    assert report.control_thread_alive is False
    diagnostics = scheduler.diagnostics()
    assert diagnostics.lifecycle is SchedulerLifecycle.SHUTDOWN_INCOMPLETE
    assert tuple(item.analysis_attempt_id for item in diagnostics.attempts) == (
        running.analysis_attempt_id,
    )
    assert diagnostics.cancel_requested == 1
    assert diagnostics.full_audio_residency_count == 1
    assert counter(scheduler, SchedulerCounterKey.SHUTDOWN_INCOMPLETE) == 1
    assert len(sink.outcomes) == 1
    assert sink.outcomes[0].analysis_attempt_id == pending.analysis_attempt_id
    assert sink.outcomes[0].status is AnalysisStatus.CANCELED

    frozen_fields = dataclasses.astuple(report)
    executor.release()
    outcomes = sink.wait_for_count(2)
    join_scheduler_threads(scheduler)
    assert outcomes[1].analysis_attempt_id == running.analysis_attempt_id
    assert outcomes[1].status is AnalysisStatus.CANCELED
    assert outcomes[1].result_payload is None
    assert residency.current == 0
    late = scheduler.diagnostics()
    assert late.lifecycle is SchedulerLifecycle.SHUTDOWN_INCOMPLETE
    assert late.attempts == ()
    assert late.full_audio_residency_count == 0
    assert counter(scheduler, SchedulerCounterKey.LATE_RESULTS_SUPPRESSED) == 1
    assert scheduler.shutdown(0.5) is report
    assert dataclasses.astuple(report) == frozen_fields
    assert sum(
        event.event_kind is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
        for event in events
    ) == 1
    assert sum(
        event.event_kind is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
        and event.analysis_attempt_id == running.analysis_attempt_id
        for event in events
    ) == 1


def test_post_freeze_incomplete_callback_reentry_reads_frozen_final_report():
    executor = GatedResultExecutor(result_or_exception=successful_result())
    nested_reports: list[ShutdownReport] = []
    cached_during_callback: list[object] = []
    holder: dict[str, AnalysisScheduler] = {}

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        if (
            event.event_kind
            is SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
            and event.lifecycle_after
            is SchedulerLifecycle.SHUTDOWN_INCOMPLETE
        ):
            scheduler = holder["scheduler"]
            cached_during_callback.append(scheduler._shutdown_report)
            nested_reports.append(scheduler.shutdown())

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=OutcomeCollector(),
        clock=ThreadRecordingClock(4.5),
        diagnostic_sink=diagnostic_sink,
        shutdown_timeout_seconds=0.02,
    )
    holder["scheduler"] = scheduler
    try:
        assert scheduler.submit(make_job(79)).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)

        report = scheduler.shutdown()

        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert nested_reports == [report]
        assert nested_reports[0] is report
        assert cached_during_callback == [report]
    finally:
        executor.release()


def test_concurrent_shutdown_callers_share_owner_deadline_and_frozen_report():
    events: list[SchedulerDiagnosticEvent] = []
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=successful_result())
    sink = OutcomeCollector()
    clock = ThreadRecordingClock(5.0)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=sink,
        clock=clock,
        diagnostic_sink=events.append,
        shutdown_timeout_seconds=0.05,
    )
    job = make_job(1)
    assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
    executor.wait_for_calls(1)
    barrier = Barrier(3)
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        barrier.wait()
        try:
            reports.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    callers = [
        Thread(target=close, name=name)
        for name in ("shutdown-a", "shutdown-b")
    ]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(2.0)

    assert errors == []
    assert len(reports) == 2
    assert reports[0] is reports[1]
    assert reports[0].status is ShutdownStatus.SHUTDOWN_INCOMPLETE
    assert reports[0].control_thread_alive is False
    shutdown_clock_callers = [
        name
        for name in clock.calling_thread_names
        if name in {"shutdown-a", "shutdown-b"}
    ]
    assert len(set(shutdown_clock_callers)) == 1
    assert len(shutdown_clock_callers) == 2
    assert sum(
        event.event_kind is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
        for event in events
    ) == 1
    assert sum(
        event.event_kind is SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
        and event.lifecycle_after is SchedulerLifecycle.SHUTDOWN_INCOMPLETE
        for event in events
    ) == 1
    assert executor.calls[0][2].is_cancelled

    executor.release()
    sink.wait_for_count(1)
    join_scheduler_threads(scheduler)
    assert len(sink.outcomes) == 1
    assert sink.outcomes[0].status is AnalysisStatus.CANCELED
    assert scheduler.shutdown() is reports[0]


def test_outer_owner_callback_reentry_returns_atomic_transient_snapshot(
    monkeypatch,
):
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=successful_result())
    outcomes = OutcomeCollector()
    events: list[SchedulerDiagnosticEvent] = []
    callback_entered = Event()
    allow_reentry = Event()
    waiter_waiting = Event()
    reentrant_reports: list[ShutdownReport] = []
    owner_reports: list[ShutdownReport] = []
    waiter_reports: list[ShutdownReport] = []
    snapshots: list[
        tuple[
            tuple[str, ...],
            tuple[int, ...],
            tuple[str, ...],
            tuple[str, ...],
            bool,
            int,
            object,
        ]
    ] = []
    errors: list[BaseException] = []
    holder: dict[str, AnalysisScheduler] = {}
    running = make_job(80, run_id="reentrant-run")
    pending = make_job(81, run_id="reentrant-run")

    def is_reentry_event(event: SchedulerDiagnosticEvent) -> bool:
        return (
            event.event_kind
            is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
            and event.analysis_attempt_id == pending.analysis_attempt_id
        )

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        events.append(event)
        if not is_reentry_event(event):
            return
        callback_entered.set()
        assert allow_reentry.wait(2.0), "callback reentry was not released"
        scheduler = holder["scheduler"]
        with scheduler._condition:
            expected_attempts = tuple(sorted(scheduler._active))
            expected_workers = tuple(
                worker_id
                for worker_id, worker in enumerate(
                    scheduler._workers,
                    start=1,
                )
                if worker.is_alive()
            )
            expected_run_lanes = tuple(sorted(scheduler._run_lane_owners))
            expected_toolchain_lanes = tuple(
                sorted(scheduler._toolchain_lane_owners)
            )
            expected_control_alive = scheduler._control_thread.is_alive()
        calls_before = scheduler._clock.call_count
        reentrant_reports.append(scheduler.shutdown())
        snapshots.append(
            (
                expected_attempts,
                expected_workers,
                expected_run_lanes,
                expected_toolchain_lanes,
                expected_control_alive,
                scheduler._clock.call_count - calls_before,
                scheduler._shutdown_report,
            )
        )
        executor.release()

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=outcomes,
        clock=ThreadRecordingClock(10.0),
        diagnostic_sink=diagnostic_sink,
        shutdown_timeout_seconds=0.5,
    )
    holder["scheduler"] = scheduler
    original_wait_for = scheduler._condition.wait_for

    def observed_wait_for(predicate, timeout=None):
        if current_thread().name == "reentrant-external-waiter":
            waiter_waiting.set()
        return original_wait_for(predicate, timeout)

    monkeypatch.setattr(
        scheduler._condition,
        "wait_for",
        observed_wait_for,
    )
    def close(target: list[ShutdownReport]) -> None:
        try:
            target.append(scheduler.shutdown())
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(
        target=close,
        args=(owner_reports,),
        name="reentrant-shutdown-owner",
    )
    waiter = Thread(
        target=close,
        args=(waiter_reports,),
        name="reentrant-external-waiter",
    )
    try:
        assert scheduler.submit(running).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert scheduler.submit(pending).status is SubmitStatus.ACCEPTED
        owner.start()
        assert callback_entered.wait(2.0)
        waiter.start()
        assert waiter_waiting.wait(2.0)
        allow_reentry.set()
        owner.join(2.0)
        waiter.join(2.0)

        assert errors == []
        assert len(reentrant_reports) == 1
        assert len(owner_reports) == len(waiter_reports) == 1
        transient = reentrant_reports[0]
        final = owner_reports[0]
        assert waiter_reports[0] is final
        assert transient is not final
        assert type(transient) is ShutdownReport
        assert transient.status is ShutdownStatus.IN_PROGRESS
        assert transient.started_monotonic == 10.0
        assert transient.finished_monotonic == 10.0
        assert transient.canceled_pending == 1
        assert transient.cancel_requested_running == 1
        assert transient.remaining_attempt_ids == snapshots[0][0] == (
            running.analysis_attempt_id,
        )
        assert transient.remaining_worker_ids == snapshots[0][1] == (1,)
        assert transient.remaining_run_lanes == snapshots[0][2] == (
            running.run_id,
        )
        assert transient.remaining_toolchain_lanes == snapshots[0][3]
        assert transient.control_thread_alive is snapshots[0][4]
        assert snapshots[0][5] == 0
        assert snapshots[0][6] is None
        assert final.status is ShutdownStatus.CLOSED
        assert final.remaining_attempt_ids == ()
        assert final.remaining_worker_ids == ()
        assert final.remaining_run_lanes == ()
        assert final.remaining_toolchain_lanes == ()
        assert final.control_thread_alive is False
        assert sum(is_reentry_event(event) for event in events) == 1
        assert scheduler._shutdown_in_progress is False
        assert scheduler.shutdown(0.5) is final

        drained = outcomes.wait_for_count(2)
        join_scheduler_threads(scheduler)
        assert len(drained) == 2
        assert {
            outcome.analysis_attempt_id for outcome in drained
        } == {
            running.analysis_attempt_id,
            pending.analysis_attempt_id,
        }
        assert all(
            outcome.status is AnalysisStatus.CANCELED
            for outcome in drained
        )
        assert scheduler.shutdown() is final
    finally:
        allow_reentry.set()
        executor.release()
        owner.join(2.0)
        waiter.join(2.0)


def test_multi_attempt_shutdown_callbacks_are_at_most_once_without_global_order():
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=successful_result())
    outcomes = OutcomeCollector()
    events: list[SchedulerDiagnosticEvent] = []
    first = make_job(
        83,
        run_id="multi-run-a",
        toolchain_id="multi-tool-a",
    )
    second = make_job(
        84,
        run_id="multi-run-b",
        toolchain_id="multi-tool-b",
    )

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=outcomes,
        clock=ThreadRecordingClock(10.0),
        diagnostic_sink=events.append,
        worker_count=2,
        shutdown_timeout_seconds=0.2,
    )
    try:
        assert scheduler.submit(first).status is SubmitStatus.ACCEPTED
        assert scheduler.submit(second).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(2)
        owner_reports: list[ShutdownReport] = []
        owner = Thread(
            target=lambda: owner_reports.append(scheduler.shutdown()),
            name="multi-attempt-owner",
        )
        owner.start()
        assert all(call[2].wait(2.0) for call in executor.calls)
        executor.release()
        owner.join(2.0)

        assert len(owner_reports) == 1
        report = owner_reports[0]
        assert report.status is ShutdownStatus.CLOSED
        assert report.canceled_pending == 0
        assert report.cancel_requested_running == 2
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == ()
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is False
        handed_off = outcomes.wait_for_count(2)
        expected_attempt_ids = {
            first.analysis_attempt_id,
            second.analysis_attempt_id,
        }
        assert {outcome.analysis_attempt_id for outcome in handed_off} == (
            expected_attempt_ids
        )
        for attempt_id in expected_attempt_ids:
            attempt_events = [
                event.event_kind
                for event in events
                if event.analysis_attempt_id == attempt_id
                and event.event_kind
                in {
                    SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
                    SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
                }
            ]
            assert attempt_events.count(
                SchedulerDiagnosticEventKind.CANCEL_REQUESTED
            ) == 1
            assert attempt_events.count(
                SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
            ) == 1
            assert sum(
                outcome.analysis_attempt_id == attempt_id
                and outcome.status is AnalysisStatus.CANCELED
                for outcome in handed_off
            ) == 1
        diagnostics = scheduler.diagnostics()
        assert diagnostics.attempts == ()
        assert diagnostics.pending == 0
        assert diagnostics.running == 0
        assert diagnostics.cancel_requested == 0
        assert diagnostics.full_audio_residency_count == 0
        assert residency.current == 0
        assert scheduler.shutdown() is report
    finally:
        executor.release()


@pytest.mark.parametrize(
    ("hold_worker", "expected_worker_ids"),
    [(False, ()), (True, (1,))],
)
def test_minimum_timeout_freezes_actual_control_liveness_for_all_callers(
    monkeypatch,
    hold_worker,
    expected_worker_ids,
):
    control_held = Event()
    release_control = Event()
    worker_finished = Event()
    release_worker = Event()
    original_control_loop = AnalysisScheduler._control_loop
    original_worker_loop = AnalysisScheduler._worker_loop

    def held_control_loop(self):
        original_control_loop(self)
        control_held.set()
        assert release_control.wait(2.0), "held control thread was not released"

    def observed_worker_loop(self, worker_id):
        original_worker_loop(self, worker_id)
        worker_finished.set()
        if hold_worker:
            assert release_worker.wait(2.0), "held worker was not released"

    monkeypatch.setattr(
        AnalysisScheduler,
        "_control_loop",
        held_control_loop,
    )
    monkeypatch.setattr(
        AnalysisScheduler,
        "_worker_loop",
        observed_worker_loop,
    )
    clock = ThreadRecordingClock(6.5)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=OutcomeCollector(),
        clock=clock,
    )
    original_control_join = scheduler._control_thread.join
    owner_join_entered = Event()
    release_owner_join = Event()

    def deterministic_control_join(timeout=None):
        assert control_held.wait(2.0), "control thread did not reach its gate"
        assert worker_finished.wait(2.0), "worker did not finish its loop"
        owner_join_entered.set()
        assert release_owner_join.wait(2.0), "owner join was not released"
        original_control_join(timeout)

    monkeypatch.setattr(
        scheduler._control_thread,
        "join",
        deterministic_control_join,
    )
    waiter_waiting = Event()
    original_wait_for = scheduler._condition.wait_for

    def observed_wait_for(predicate, timeout=None):
        if current_thread().name == "control-boundary-waiter":
            waiter_waiting.set()
        return original_wait_for(predicate, timeout)

    monkeypatch.setattr(
        scheduler._condition,
        "wait_for",
        observed_wait_for,
    )
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        try:
            reports.append(scheduler.shutdown(5e-324))
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=close, name="control-boundary-owner")
    waiter = Thread(target=close, name="control-boundary-waiter")
    try:
        owner.start()
        assert owner_join_entered.wait(2.0)
        waiter.start()
        assert waiter_waiting.wait(2.0)
        release_owner_join.set()
        owner.join(2.0)

        # The pre-contract RED path can fail before freezing a report.
        # Wake its waiter so the regression never leaks a test thread.
        if errors:
            with scheduler._condition:
                scheduler._shutdown_report = object()
                scheduler._shutdown_in_progress = False
                scheduler._condition.notify_all()
        waiter.join(2.0)

        assert errors == []
        assert len(reports) == 2
        assert reports[0] is reports[1]
        report = reports[0]
        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == expected_worker_ids
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is True
        assert scheduler._shutdown_in_progress is False
        shutdown_clock_callers = [
            name
            for name in clock.calling_thread_names
            if name in {
                "control-boundary-owner",
                "control-boundary-waiter",
            }
        ]
        assert shutdown_clock_callers == [
            "control-boundary-owner",
            "control-boundary-owner",
        ]
        clock_calls = clock.call_count
        assert scheduler.shutdown(5e-324) is report
        assert clock.call_count == clock_calls

        release_control.set()
        release_worker.set()
        join_scheduler_threads(scheduler)
        assert scheduler.shutdown(0.5) is report
        assert report.control_thread_alive is True
        assert report.remaining_worker_ids == expected_worker_ids
    finally:
        release_owner_join.set()
        release_control.set()
        release_worker.set()
        owner.join(2.0)
        waiter.join(2.0)


def test_shortest_timeout_freezes_actionable_report_for_concurrent_callers(
    monkeypatch,
):
    worker_held = Event()
    release_worker = Event()
    original_worker_loop = AnalysisScheduler._worker_loop

    def held_worker_loop(self, worker_id):
        original_worker_loop(self, worker_id)
        worker_held.set()
        assert release_worker.wait(2.0), "held worker was not released"

    monkeypatch.setattr(
        AnalysisScheduler,
        "_worker_loop",
        held_worker_loop,
    )
    clock = ThreadRecordingClock(7.0)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=OutcomeCollector(),
        clock=clock,
    )
    worker = scheduler._workers[0]
    join_entered = Event()
    release_owner_join = Event()
    original_worker_join = worker.join

    def gated_worker_join(timeout=None):
        scheduler._control_thread.join(2.0)
        assert not scheduler._control_thread.is_alive()
        assert worker_held.wait(2.0), "worker did not reach its exit gate"
        join_entered.set()
        assert release_owner_join.wait(2.0), "owner join was not released"
        original_worker_join(timeout)

    monkeypatch.setattr(worker, "join", gated_worker_join)
    waiter_waiting = Event()
    original_wait_for = scheduler._condition.wait_for

    def observed_wait_for(predicate, timeout=None):
        if current_thread().name == "boundary-waiter":
            waiter_waiting.set()
        return original_wait_for(predicate, timeout)

    monkeypatch.setattr(
        scheduler._condition,
        "wait_for",
        observed_wait_for,
    )
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []

    def close() -> None:
        try:
            reports.append(scheduler.shutdown(5e-324))
        except BaseException as exc:
            errors.append(exc)

    owner = Thread(target=close, name="boundary-owner")
    waiter = Thread(target=close, name="boundary-waiter")
    try:
        owner.start()
        assert join_entered.wait(2.0)
        waiter.start()
        assert waiter_waiting.wait(2.0)
        release_owner_join.set()
        owner.join(2.0)

        # Keep the regression harness recoverable on the pre-fix RED path,
        # where report construction raises and otherwise strands the waiter.
        if errors:
            with scheduler._condition:
                scheduler._shutdown_report = ShutdownReport(
                    status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
                    started_monotonic=7.0,
                    finished_monotonic=7.0,
                    canceled_pending=0,
                    cancel_requested_running=0,
                    remaining_attempt_ids=(),
                    remaining_worker_ids=(1,),
                    remaining_run_lanes=(),
                    remaining_toolchain_lanes=(),
                    control_thread_alive=False,
                )
                scheduler._lifecycle = (
                    SchedulerLifecycle.SHUTDOWN_INCOMPLETE
                )
                scheduler._shutdown_in_progress = False
                scheduler._counters[
                    SchedulerCounterKey.SHUTDOWN_INCOMPLETE
                ] = 1
                scheduler._condition.notify_all()
        waiter.join(2.0)

        assert errors == []
        assert len(reports) == 2
        assert reports[0] is reports[1]
        report = reports[0]
        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == (1,)
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is False
        shutdown_clock_callers = [
            name
            for name in clock.calling_thread_names
            if name in {"boundary-owner", "boundary-waiter"}
        ]
        assert shutdown_clock_callers == [
            "boundary-owner",
            "boundary-owner",
        ]
        clock_calls = clock.call_count
        assert scheduler.shutdown(5e-324) is report
        assert clock.call_count == clock_calls

        release_worker.set()
        join_scheduler_threads(scheduler)
        assert scheduler.shutdown(0.5) is report
        assert report.remaining_worker_ids == (1,)
        assert report.control_thread_alive is False
    finally:
        release_owner_join.set()
        release_worker.set()
        owner.join(2.0)
        waiter.join(2.0)


def test_shutdown_uses_only_existing_bounded_sink_diagnostic_facts():
    events: list[SchedulerDiagnosticEvent] = []
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=successful_result())

    def outcome_sink(_outcome) -> None:
        raise RuntimeError("planned outcome sink failure")

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        events.append(event)
        raise RuntimeError("planned diagnostic sink failure")

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=outcome_sink,
        clock=ThreadRecordingClock(6.0),
        diagnostic_sink=diagnostic_sink,
        shutdown_timeout_seconds=0.02,
    )
    running = make_job(1, run_id="shared")
    pending = make_job(2, run_id="shared")
    assert scheduler.submit(running).status is SubmitStatus.ACCEPTED
    executor.wait_for_calls(1)
    assert scheduler.submit(pending).status is SubmitStatus.ACCEPTED

    report = scheduler.shutdown()

    assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
    assert counter(scheduler, SchedulerCounterKey.OUTCOME_SINK_FAILURE) == 1
    assert counter(scheduler, SchedulerCounterKey.DIAGNOSTIC_SINK_FAILURE) == len(
        events
    )
    assert sum(
        event.event_kind is SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED
        for event in events
    ) == 1
    assert sum(
        event.event_kind is SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
        and event.lifecycle_after is SchedulerLifecycle.SHUTDOWN_INCOMPLETE
        for event in events
    ) == 1
    assert tuple(key.value for key in SchedulerCounterKey) == tuple(
        key for key, _value in scheduler.diagnostics().counters
    )
    assert tuple(kind for kind in SchedulerDiagnosticEventKind) == (
        SchedulerDiagnosticEventKind.SUBMIT_ACCEPTED,
        SchedulerDiagnosticEventKind.SUBMIT_REJECTED,
        SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED,
        SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
        SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
        SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
        SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
    )

    executor.release()
    join_scheduler_threads(scheduler)
    assert counter(scheduler, SchedulerCounterKey.OUTCOME_SINK_FAILURE) == 2
    assert scheduler.shutdown() is report
