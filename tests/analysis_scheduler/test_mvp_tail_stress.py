"""Deterministic bounded stress evidence for the scheduler MVP tail."""

from __future__ import annotations

from collections import Counter
from threading import Condition, Event, Lock, Thread, current_thread

import pytest

from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AnalysisErrorCategory,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisKind,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStatus,
    CancelStatus,
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
    ManualClock,
    OutcomeCollector,
    PassThroughStartGate,
    PerAttemptGatedExecutor,
    RecordingExecutor,
    ThreadRecordingClock,
    WorkerDrainCompletionFaultClock,
    make_job,
)


def _successful_result() -> AnalysisExecutionResult:
    return AnalysisExecutionResult(
        status=AnalysisExecutionStatus.SUCCEEDED,
        result_payload=("late", "payload"),
    )


def _counter(scheduler: AnalysisScheduler, key: SchedulerCounterKey) -> int:
    return dict(scheduler.diagnostics().counters)[key.value]


def _join_scheduler_threads(scheduler: AnalysisScheduler) -> None:
    scheduler._control_thread.join(2.0)
    for worker in scheduler._workers:
        worker.join(2.0)
    assert not scheduler._control_thread.is_alive()
    assert not any(worker.is_alive() for worker in scheduler._workers)


def _wait_for_no_active(scheduler: AnalysisScheduler) -> None:
    with scheduler._condition:
        assert scheduler._condition.wait_for(
            lambda: not scheduler._active,
            timeout=2.0,
        ), "scheduler did not release all active attempts"


def _cleanup_scheduler(
    scheduler: AnalysisScheduler,
    *,
    release: callable,
    threads: tuple[Thread, ...] = (),
) -> None:
    cleanup: Thread | None = None
    with scheduler._condition:
        running = scheduler._lifecycle is SchedulerLifecycle.RUNNING
    closing_thread_alive = any(thread.is_alive() for thread in threads)
    if running and not closing_thread_alive:
        cleanup_reports: list[ShutdownReport] = []
        cleanup_errors: list[BaseException] = []
        cleanup = Thread(
            target=lambda: _record_shutdown(
                scheduler,
                cleanup_reports,
                cleanup_errors,
            ),
            name="stress-cleanup-owner",
        )
        cleanup.start()
    with scheduler._condition:
        assert scheduler._condition.wait_for(
            lambda: scheduler._lifecycle is not SchedulerLifecycle.RUNNING,
            timeout=2.0,
        )
    release()
    for thread in threads:
        if thread.ident is not None:
            thread.join(2.0)
    if cleanup is not None:
        cleanup.join(2.0)
        assert not cleanup.is_alive()
        assert cleanup_errors == []
    release()
    _wait_for_no_active(scheduler)
    _join_scheduler_threads(scheduler)


class _FaultingOutcomeCollector:
    """Record every handoff and raise one ordinary sink exception."""

    def __init__(self) -> None:
        self._condition = Condition()
        self.outcomes = []
        self.call_count = 0

    def __call__(self, outcome) -> None:
        with self._condition:
            self.call_count += 1
            self.outcomes.append(outcome)
            should_raise = self.call_count == 1
            self._condition.notify_all()
        if should_raise:
            raise RuntimeError("planned outcome sink failure")

    def wait_for_count(self, count: int) -> tuple:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.outcomes) >= count,
                timeout=2.0,
            ), f"outcome sink did not receive {count} outcomes"
            return tuple(self.outcomes)


class _FaultingDiagnosticCollector:
    """Record every event and raise one ordinary sink exception."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.events: list[SchedulerDiagnosticEvent] = []
        self.call_count = 0

    def __call__(self, event: SchedulerDiagnosticEvent) -> None:
        with self._lock:
            self.call_count += 1
            self.events.append(event)
            should_raise = self.call_count == 1
        if should_raise:
            raise RuntimeError("planned diagnostic sink failure")

    def snapshot(self) -> tuple[SchedulerDiagnosticEvent, ...]:
        with self._lock:
            return tuple(self.events)


class _FinalFreezeGateCondition:
    """Expose whether finalization locks before its sole completion read."""

    def __init__(
        self,
        condition: Condition,
        *,
        owner_thread_name: str,
        owner_step: Event,
        owner_step_mode: list[str],
        allow_owner_step: Event,
    ) -> None:
        self._condition = condition
        self._owner_thread_name = owner_thread_name
        self._owner_step = owner_step
        self._owner_step_mode = owner_step_mode
        self._allow_owner_step = allow_owner_step
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def __enter__(self):
        if (
            self._armed
            and not self._owner_step.is_set()
            and current_thread().name == self._owner_thread_name
        ):
            self._armed = False
            self._owner_step_mode.append("final-condition")
            self._owner_step.set()
            assert self._allow_owner_step.wait(
                2.0
            ), "final condition gate was not released"
        return self._condition.__enter__()

    def __exit__(self, *args):
        return self._condition.__exit__(*args)

    def __getattr__(self, name: str):
        return getattr(self._condition, name)


class _StaleOwnerCompletionClock(ManualClock):
    """Return one stale owner completion value while workers see current time."""

    def __init__(self, *, owner_thread_name: str) -> None:
        super().__init__(10.0)
        self._owner_thread_name = owner_thread_name
        self._owner_calls = 0

    def monotonic(self) -> object:
        with self._lock:
            self.call_count += 1
            if current_thread().name == self._owner_thread_name:
                self._owner_calls += 1
                if self._owner_calls == 2:
                    return 20.0
            return self._value


class _BlockingWorkerClock:
    """Hold exactly one armed worker call inside external monotonic()."""

    def __init__(self, value: float) -> None:
        self._value = value
        self._lock = Lock()
        self._armed = False
        self.worker_call_entered = Event()
        self.allow_worker_call = Event()
        self.blocked_call_count = 0

    def arm_worker_call(self) -> None:
        with self._lock:
            assert not self._armed
            self._armed = True

    def monotonic(self) -> float:
        with self._lock:
            should_block = (
                self._armed
                and "analysis-scheduler-worker-" in current_thread().name
            )
            if should_block:
                self._armed = False
                self.blocked_call_count += 1
        if should_block:
            self.worker_call_entered.set()
            assert self.allow_worker_call.wait(
                2.0
            ), "blocked worker clock call was not released"
        return self._value


@pytest.mark.parametrize("worker_count", [1, 6])
@pytest.mark.parametrize("cycle", range(2))
def test_fixed_mixed_cycles_stay_bounded_across_cancel_complete_and_shutdown(
    worker_count,
    cycle,
):
    base = worker_count * 1_000 + cycle * 100
    clock = ManualClock(0.0)
    residency = AudioResidencyProbe()
    executor = PerAttemptGatedExecutor()
    outcomes = _FaultingOutcomeCollector()
    diagnostics_sink = _FaultingDiagnosticCollector()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            pending_capacity=3,
            terminal_receipt_capacity=worker_count + 8,
            shutdown_timeout_seconds=0.5,
        ),
        audio_loader=CloneForbiddenAudioLoader(residency),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=clock,
        diagnostic_sink=diagnostics_sink,
    )

    initial = [
        make_job(
            base + index,
            run_id=("contended-run" if index == 1 else f"run-{base + index}"),
            kind=tuple(AnalysisKind)[(index - 1) % len(AnalysisKind)],
            toolchain_id=(
                "contended-toolchain"
                if index == 1
                else f"toolchain-{base + index}"
            ),
        )
        for index in range(1, worker_count + 1)
    ]
    timed_out = make_job(
        base + worker_count + 1,
        kind=AnalysisKind.REANALYSIS,
        queue_timeout_seconds=0.5,
        toolchain_id="timeout-toolchain",
    )
    pending_cancel = make_job(
        base + worker_count + 2,
        run_id="contended-run",
        kind=AnalysisKind.IMPORTED,
        toolchain_id="contended-toolchain",
    )
    completion_candidate = make_job(
        base + worker_count + 3,
        kind=AnalysisKind.AUTOMATIC,
        toolchain_id="completion-toolchain",
    )
    rejected = make_job(base + worker_count + 4)
    replacement = make_job(
        base + worker_count + 5,
        kind=AnalysisKind.IMPORTED,
        toolchain_id="shutdown-toolchain",
    )
    executor.hold(
        *(
            job.analysis_attempt_id
            for job in (
                *initial,
                timed_out,
                pending_cancel,
                completion_candidate,
                replacement,
            )
        )
    )
    accepted_jobs = [*initial, timed_out, pending_cancel, completion_candidate]
    owner_reports: list[ShutdownReport] = []
    owner_errors: list[BaseException] = []
    owner = Thread(
        target=lambda: _record_shutdown(
            scheduler,
            owner_reports,
            owner_errors,
        ),
        name=f"mixed-cycle-owner-{worker_count}-{cycle}",
    )

    try:
        for index, job in enumerate(initial, start=1):
            assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
            executor.wait_for_calls(index)

        for job in (timed_out, pending_cancel, completion_candidate):
            assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        full = scheduler.submit(rejected)
        assert full.status is SubmitStatus.QUEUE_FULL

        bounded = scheduler.diagnostics()
        assert bounded.running == worker_count
        assert bounded.pending == 3
        assert bounded.running + bounded.cancel_requested <= worker_count
        assert bounded.pending <= bounded.pending_capacity
        assert bounded.full_audio_residency_count <= worker_count
        assert residency.current <= worker_count
        assert bounded.blocked_on_run_lane >= 1
        assert bounded.blocked_on_toolchain_lane >= 1

        pending_receipt = scheduler.cancel(
            pending_cancel.run_id,
            pending_cancel.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "pending cycle cancel",
        )
        assert pending_receipt.status is CancelStatus.CANCELED_PENDING

        clock.set(1.0)
        assert scheduler.submit(replacement).status is SubmitStatus.ACCEPTED
        accepted_jobs.append(replacement)
        timed_out_outcome = next(
            outcome
            for outcome in outcomes.wait_for_count(2)
            if outcome.analysis_attempt_id == timed_out.analysis_attempt_id
        )
        assert timed_out_outcome.status is AnalysisStatus.FAILED
        assert timed_out_outcome.started_monotonic is None
        assert (
            timed_out_outcome.error.category
            is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT
        )

        cancel_first = initial[0]
        running_receipt = scheduler.cancel(
            cancel_first.run_id,
            cancel_first.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "cancel wins",
        )
        assert running_receipt.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        executor.release(cancel_first.analysis_attempt_id)
        canceled_outcome = next(
            outcome
            for outcome in outcomes.wait_for_count(3)
            if outcome.analysis_attempt_id == cancel_first.analysis_attempt_id
        )
        assert canceled_outcome.status is AnalysisStatus.CANCELED
        assert canceled_outcome.result_payload is None

        if worker_count == 1:
            completion_first = completion_candidate
            executor.wait_for_attempt(completion_first.analysis_attempt_id)
        else:
            completion_first = initial[1]
        executor.release(completion_first.analysis_attempt_id)
        completed_outcome = next(
            outcome
            for outcome in outcomes.wait_for_count(4)
            if outcome.analysis_attempt_id == completion_first.analysis_attempt_id
        )
        assert completed_outcome.status is AnalysisStatus.SUCCEEDED
        assert (
            scheduler.cancel(
                completion_first.run_id,
                completion_first.analysis_attempt_id,
                AnalysisCancelOrigin.USER_STOP,
                "completion already won",
            ).status
            is CancelStatus.ALREADY_TERMINAL
        )

        if worker_count == 1:
            executor.wait_for_attempt(replacement.analysis_attempt_id)
        active_before_shutdown = {
            attempt.analysis_attempt_id
            for attempt in scheduler.diagnostics().attempts
            if attempt.state.value in {"running", "cancel_requested"}
        }
        assert active_before_shutdown
        if worker_count == 6:
            assert len(active_before_shutdown) >= 2
        for attempt_id in sorted(active_before_shutdown):
            executor.wait_for_attempt(attempt_id)
        with executor._condition:
            calls_by_attempt = {
                call[0].analysis_attempt_id: call[2]
                for call in executor.calls
            }
            recorded_active_ids = set(calls_by_attempt)
            assert active_before_shutdown <= recorded_active_ids
            active_tokens = tuple(
                calls_by_attempt[attempt_id]
                for attempt_id in sorted(active_before_shutdown)
            )
            assert len(active_tokens) == len(active_before_shutdown)
            assert all(token is not None for token in active_tokens)
        owner.start()
        for token in active_tokens:
            assert token.wait(2.0)
        executor.release_all()
        owner.join(2.0)

        assert not owner.is_alive()
        assert owner_errors == []
        assert len(owner_reports) == 1
        final = owner_reports[0]
        assert final.status is ShutdownStatus.CLOSED
        assert scheduler.shutdown() is final
        handed_off = outcomes.wait_for_count(len(accepted_jobs))
        assert len(handed_off) == len(accepted_jobs)
        outcome_counts = Counter(
            outcome.analysis_attempt_id for outcome in handed_off
        )
        assert outcome_counts == Counter(
            job.analysis_attempt_id for job in accepted_jobs
        )

        events = diagnostics_sink.snapshot()
        for job in accepted_jobs:
            terminal_count = sum(
                event.event_kind
                is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
                and event.analysis_attempt_id == job.analysis_attempt_id
                for event in events
            )
            cancel_count = sum(
                event.event_kind
                is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
                and event.analysis_attempt_id == job.analysis_attempt_id
                for event in events
            )
            assert terminal_count <= 1
            assert cancel_count <= 1
        for attempt_id in active_before_shutdown:
            assert sum(
                event.event_kind
                is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
                and event.analysis_attempt_id == attempt_id
                for event in events
            ) == 1

        final_diagnostics = scheduler.diagnostics()
        assert final_diagnostics.lifecycle is SchedulerLifecycle.CLOSED
        assert final_diagnostics.attempts == ()
        assert final_diagnostics.pending == 0
        assert final_diagnostics.running == 0
        assert final_diagnostics.cancel_requested == 0
        assert final_diagnostics.full_audio_residency_count == 0
        assert final_diagnostics.toolchain_lanes == ()
        assert final_diagnostics.peak_full_audio_residency_count <= worker_count
        assert final_diagnostics.peak_running_workers <= worker_count
        assert executor.peak_active <= worker_count
        assert executor.run_overlap_attempt_ids == []
        assert executor.toolchain_busy_attempt_ids == []
        assert residency.current == 0
        assert residency.created == residency.closed
        assert all(
            handle.close_count == 1
            for handle in scheduler._audio_loader.handles
        )
        with scheduler._condition:
            assert scheduler._run_lane_owners == {}
            assert scheduler._toolchain_lane_owners == {}
            assert len(scheduler._free_worker_ids) == worker_count
        assert _counter(
            scheduler,
            SchedulerCounterKey.OUTCOME_SINK_FAILURE,
        ) == 1
        assert _counter(
            scheduler,
            SchedulerCounterKey.DIAGNOSTIC_SINK_FAILURE,
        ) == 1
        assert {
            job.kind for job in accepted_jobs
        } == set(AnalysisKind)
        _join_scheduler_threads(scheduler)
    finally:
        _cleanup_scheduler(
            scheduler,
            release=executor.release_all,
            threads=(owner,),
        )


def _record_shutdown(
    scheduler: AnalysisScheduler,
    reports: list[ShutdownReport],
    errors: list[BaseException],
) -> None:
    try:
        reports.append(scheduler.shutdown())
    except BaseException as exc:
        errors.append(exc)


def test_callback_reentry_does_not_wait_for_external_worker_clock_call():
    clock = _BlockingWorkerClock(5.0)
    executor = PerAttemptGatedExecutor()
    callback_entered = Event()
    allow_reentry = Event()
    nested_returned = Event()
    nested_reports: list[ShutdownReport] = []
    owner_reports: list[ShutdownReport] = []
    errors: list[BaseException] = []
    holder: dict[str, AnalysisScheduler] = {}
    callback_job = make_job(
        19_001,
        run_id="clock-callback-run",
        toolchain_id="clock-callback-toolchain",
    )
    blocked_clock_job = make_job(
        19_002,
        run_id="clock-blocked-run",
        toolchain_id="clock-blocked-toolchain",
    )
    executor.hold(
        callback_job.analysis_attempt_id,
        blocked_clock_job.analysis_attempt_id,
    )

    def outcome_sink(outcome) -> None:
        if outcome.analysis_attempt_id != callback_job.analysis_attempt_id:
            return
        callback_entered.set()
        assert allow_reentry.wait(2.0), "callback reentry was not released"
        nested_reports.append(holder["scheduler"].shutdown())
        nested_returned.set()

    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=2,
            shutdown_timeout_seconds=1.0,
        ),
        audio_loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcome_sink,
        clock=clock,
    )
    holder["scheduler"] = scheduler
    owner = Thread(
        target=lambda: _record_shutdown(scheduler, owner_reports, errors),
        name="clock-contention-owner",
    )

    try:
        assert scheduler.submit(callback_job).status is SubmitStatus.ACCEPTED
        assert (
            scheduler.submit(blocked_clock_job).status
            is SubmitStatus.ACCEPTED
        )
        executor.wait_for_calls(2)

        owner.start()
        for _, _, token in executor.calls:
            assert token.wait(2.0), "shutdown did not request cancellation"

        executor.release(callback_job.analysis_attempt_id)
        assert callback_entered.wait(2.0), "worker callback was not entered"

        clock.arm_worker_call()
        executor.release(blocked_clock_job.analysis_attempt_id)
        assert clock.worker_call_entered.wait(
            2.0
        ), "worker did not enter external monotonic()"

        allow_reentry.set()
        assert nested_returned.wait(
            0.5
        ), "callback shutdown waited for the external clock call"
        assert not clock.allow_worker_call.is_set()
        assert len(nested_reports) == 1
        assert nested_reports[0].status is ShutdownStatus.IN_PROGRESS
        assert clock.blocked_call_count == 1

        clock.allow_worker_call.set()
        owner.join(2.0)
        assert not owner.is_alive()
        assert errors == []
        assert len(owner_reports) == 1
        assert owner_reports[0].status is ShutdownStatus.CLOSED
    finally:
        allow_reentry.set()
        executor.release_all()
        clock.allow_worker_call.set()
        owner.join(2.0)
        if scheduler._shutdown_report is None:
            _wait_for_no_active(scheduler)
            scheduler.shutdown()
        _join_scheduler_threads(scheduler)


@pytest.mark.parametrize("worker_count", [1, 6])
@pytest.mark.parametrize("callback_thread", ["outer-owner", "worker"])
def test_callback_reentry_is_transient_and_external_callers_share_final(
    monkeypatch,
    worker_count,
    callback_thread,
):
    clock = ThreadRecordingClock(5.0)
    executor = GatedResultExecutor(result_or_exception=_successful_result())
    outcomes = OutcomeCollector()
    events: list[SchedulerDiagnosticEvent] = []
    events_lock = Lock()
    callback_entered = Event()
    nested_returned = Event()
    allow_callback_return = Event()
    owner_returned = Event()
    waiter_returned = Event()
    waiter_waiting = Event()
    nested_reports: list[ShutdownReport] = []
    owner_reports: list[ShutdownReport] = []
    waiter_reports: list[ShutdownReport] = []
    errors: list[BaseException] = []
    report_absent_during_callback: list[bool] = []
    holder: dict[str, AnalysisScheduler] = {}
    running = make_job(
        20_000 + worker_count * 10,
        run_id="callback-run",
        toolchain_id="callback-toolchain",
    )
    pending = make_job(
        20_001 + worker_count * 10,
        run_id="callback-run",
        toolchain_id="callback-toolchain",
    )
    target_attempt_id = (
        pending.analysis_attempt_id
        if callback_thread == "outer-owner"
        else running.analysis_attempt_id
    )

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        with events_lock:
            events.append(event)
        if (
            event.event_kind
            is not SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
            or event.analysis_attempt_id != target_attempt_id
        ):
            return
        callback_entered.set()
        scheduler = holder["scheduler"]
        nested_reports.append(scheduler.shutdown())
        report_absent_during_callback.append(
            scheduler._shutdown_report is None
        )
        nested_returned.set()
        assert allow_callback_return.wait(2.0)

    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            pending_capacity=2,
            shutdown_timeout_seconds=0.5,
        ),
        audio_loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    holder["scheduler"] = scheduler
    original_wait_for = scheduler._condition.wait_for

    def observed_wait_for(predicate, timeout=None):
        if current_thread().name.startswith("callback-waiter-"):
            waiter_waiting.set()
        return original_wait_for(predicate, timeout)

    monkeypatch.setattr(
        scheduler._condition,
        "wait_for",
        observed_wait_for,
    )

    def close_owner() -> None:
        _record_shutdown(scheduler, owner_reports, errors)
        owner_returned.set()

    def close_waiter() -> None:
        _record_shutdown(scheduler, waiter_reports, errors)
        waiter_returned.set()

    owner = Thread(
        target=close_owner,
        name=f"callback-owner-{worker_count}-{callback_thread}",
    )
    waiter = Thread(
        target=close_waiter,
        name=f"callback-waiter-{worker_count}-{callback_thread}",
    )
    try:
        assert scheduler.submit(running).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        if callback_thread == "outer-owner":
            assert scheduler.submit(pending).status is SubmitStatus.ACCEPTED
        owner.start()
        assert executor.calls[0][2].wait(2.0)
        if callback_thread == "worker":
            executor.release()
        assert callback_entered.wait(2.0)
        assert nested_returned.wait(2.0)

        waiter.start()
        assert waiter_waiting.wait(2.0)
        assert not waiter_returned.is_set()
        assert not owner_returned.is_set()
        if callback_thread == "outer-owner":
            executor.release()
        allow_callback_return.set()
        owner.join(2.0)
        waiter.join(2.0)

        assert errors == []
        assert not owner.is_alive()
        assert not waiter.is_alive()
        assert len(nested_reports) == 1
        assert len(owner_reports) == len(waiter_reports) == 1
        transient = nested_reports[0]
        final = owner_reports[0]
        assert transient.status is ShutdownStatus.IN_PROGRESS
        assert transient is not final
        assert report_absent_during_callback == [True]
        assert transient.started_monotonic == 5.0
        assert transient.finished_monotonic >= transient.started_monotonic
        assert len(transient.remaining_attempt_ids) <= worker_count
        assert len(transient.remaining_worker_ids) <= worker_count
        assert all(
            1 <= worker_id <= worker_count
            for worker_id in transient.remaining_worker_ids
        )
        assert final.status is ShutdownStatus.CLOSED
        assert final.remaining_attempt_ids == ()
        assert final.remaining_worker_ids == ()
        assert final.remaining_run_lanes == ()
        assert final.remaining_toolchain_lanes == ()
        assert final.control_thread_alive is False
        assert waiter_reports[0] is final
        assert scheduler.shutdown() is final
        expected_outcomes = 2 if callback_thread == "outer-owner" else 1
        assert len(outcomes.wait_for_count(expected_outcomes)) == expected_outcomes

        with events_lock:
            event_snapshot = tuple(events)
        expected_attempts = {running.analysis_attempt_id}
        if callback_thread == "outer-owner":
            expected_attempts.add(pending.analysis_attempt_id)
        for attempt_id in expected_attempts:
            assert sum(
                event.event_kind
                is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
                and event.analysis_attempt_id == attempt_id
                for event in event_snapshot
            ) == 1
            assert sum(
                event.event_kind
                is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
                and event.analysis_attempt_id == attempt_id
                for event in event_snapshot
            ) <= 1
        shutdown_clock_callers = [
            name
            for name in clock.calling_thread_names
            if name in {owner.name, waiter.name}
        ]
        assert shutdown_clock_callers == [owner.name, owner.name]
        _join_scheduler_threads(scheduler)
    finally:
        allow_callback_return.set()
        executor.release()
        owner.join(2.0)
        waiter.join(2.0)
        if scheduler._shutdown_report is None:
            _wait_for_no_active(scheduler)
            scheduler.shutdown()
        _join_scheduler_threads(scheduler)


def test_control_queue_timeout_callback_reentry_is_transient_and_bounded(
    monkeypatch,
):
    clock = ThreadRecordingClock(0.0)
    executor = GatedResultExecutor(result_or_exception=_successful_result())
    residency = AudioResidencyProbe()
    outcome_condition = Condition()
    outcomes = []
    events: list[SchedulerDiagnosticEvent] = []
    events_lock = Lock()
    callback_entered = Event()
    allow_reentry = Event()
    nested_returned = Event()
    owner_returned = Event()
    waiter_waiting = Event()
    waiter_returned = Event()
    nested_reports: list[ShutdownReport] = []
    owner_reports: list[ShutdownReport] = []
    waiter_reports: list[ShutdownReport] = []
    report_absent_during_callback: list[bool] = []
    callback_clock_deltas: list[int] = []
    callback_join_callers: list[tuple[str, ...]] = []
    errors: list[BaseException] = []
    join_callers: list[str] = []
    join_callers_lock = Lock()
    holder: dict[str, AnalysisScheduler] = {}
    running = make_job(
        25_000,
        run_id="control-running-run",
        toolchain_id="control-running-toolchain",
    )
    timed_out = make_job(
        25_001,
        queue_timeout_seconds=1.0,
        toolchain_id="control-timeout-toolchain",
    )
    wake = make_job(
        25_002,
        toolchain_id="control-wake-toolchain",
    )

    def outcome_sink(outcome) -> None:
        with outcome_condition:
            outcomes.append(outcome)
            outcome_condition.notify_all()
        if outcome.analysis_attempt_id != timed_out.analysis_attempt_id:
            return
        callback_entered.set()
        assert allow_reentry.wait(2.0)
        scheduler = holder["scheduler"]
        calls_before = clock.call_count
        nested_reports.append(scheduler.shutdown())
        callback_clock_deltas.append(clock.call_count - calls_before)
        report_absent_during_callback.append(
            scheduler._shutdown_report is None
        )
        with join_callers_lock:
            callback_join_callers.append(tuple(join_callers))
        nested_returned.set()

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        with events_lock:
            events.append(event)

    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=1,
            pending_capacity=2,
            shutdown_timeout_seconds=0.5,
        ),
        audio_loader=CloneForbiddenAudioLoader(residency),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcome_sink,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    holder["scheduler"] = scheduler
    control_name = scheduler._control_thread.name

    for scheduler_thread in (scheduler._control_thread, *scheduler._workers):
        original_join = scheduler_thread.join

        def observed_join(timeout=None, join=original_join):
            with join_callers_lock:
                join_callers.append(current_thread().name)
            join(timeout)

        monkeypatch.setattr(scheduler_thread, "join", observed_join)

    original_wait_for = scheduler._condition.wait_for

    def observed_wait_for(predicate, timeout=None):
        if current_thread().name == "control-callback-waiter":
            waiter_waiting.set()
        return original_wait_for(predicate, timeout)

    monkeypatch.setattr(
        scheduler._condition,
        "wait_for",
        observed_wait_for,
    )

    def close_owner() -> None:
        _record_shutdown(scheduler, owner_reports, errors)
        owner_returned.set()

    def close_waiter() -> None:
        _record_shutdown(scheduler, waiter_reports, errors)
        waiter_returned.set()

    owner = Thread(target=close_owner, name="control-callback-owner")
    waiter = Thread(target=close_waiter, name="control-callback-waiter")
    try:
        assert scheduler.submit(running).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        assert scheduler.submit(timed_out).status is SubmitStatus.ACCEPTED
        clock.set(1.0)
        assert scheduler.submit(wake).status is SubmitStatus.ACCEPTED
        assert callback_entered.wait(2.0)

        owner.start()
        assert executor.calls[0][2].wait(2.0)
        waiter.start()
        assert waiter_waiting.wait(2.0)
        assert not owner_returned.is_set()
        assert not waiter_returned.is_set()

        allow_reentry.set()
        assert nested_returned.wait(2.0)
        assert not owner_returned.is_set()
        assert not waiter_returned.is_set()
        executor.release()
        owner.join(2.0)
        waiter.join(2.0)

        assert errors == []
        assert not owner.is_alive()
        assert not waiter.is_alive()
        assert len(nested_reports) == 1
        assert len(owner_reports) == len(waiter_reports) == 1
        transient = nested_reports[0]
        final = owner_reports[0]
        assert transient.status is ShutdownStatus.IN_PROGRESS
        assert transient is not final
        assert report_absent_during_callback == [True]
        assert callback_clock_deltas == [0]
        assert all(
            caller != control_name
            for caller in callback_join_callers[0]
        )
        assert transient.started_monotonic == 1.0
        assert transient.finished_monotonic == 1.0
        assert transient.canceled_pending == 1
        assert transient.cancel_requested_running == 1
        assert transient.remaining_attempt_ids == (
            running.analysis_attempt_id,
        )
        assert transient.remaining_worker_ids == (1,)
        assert transient.remaining_run_lanes == (running.run_id,)
        assert transient.remaining_toolchain_lanes == (
            canonical_toolchain_lane_key("control-running-toolchain"),
        )
        assert transient.control_thread_alive is True

        assert final.status is ShutdownStatus.CLOSED
        assert waiter_reports[0] is final
        assert scheduler.shutdown() is final
        with outcome_condition:
            assert outcome_condition.wait_for(
                lambda: len(outcomes) == 3,
                timeout=2.0,
            )
            outcome_snapshot = tuple(outcomes)
        assert Counter(
            outcome.analysis_attempt_id for outcome in outcome_snapshot
        ) == Counter(
            (
                running.analysis_attempt_id,
                timed_out.analysis_attempt_id,
                wake.analysis_attempt_id,
            )
        )
        assert sum(
            outcome.analysis_attempt_id == timed_out.analysis_attempt_id
            and outcome.status is AnalysisStatus.FAILED
            and outcome.error.category
            is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT
            for outcome in outcome_snapshot
        ) == 1

        with events_lock:
            event_snapshot = tuple(events)
        for attempt_id in (
            running.analysis_attempt_id,
            timed_out.analysis_attempt_id,
            wake.analysis_attempt_id,
        ):
            assert sum(
                event.analysis_attempt_id == attempt_id
                and event.event_kind
                is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
                for event in event_snapshot
            ) <= 1
            assert sum(
                event.analysis_attempt_id == attempt_id
                and event.event_kind
                is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
                for event in event_snapshot
            ) <= 1
        assert _counter(scheduler, SchedulerCounterKey.QUEUE_TIMEOUT) == 1
        assert scheduler.diagnostics().attempts == ()
        assert residency.current == 0
        _join_scheduler_threads(scheduler)
    finally:
        allow_reentry.set()
        _cleanup_scheduler(
            scheduler,
            release=executor.release,
            threads=(owner, waiter),
        )


@pytest.mark.parametrize("worker_count", [1, 6])
@pytest.mark.parametrize(
    ("mode", "hold_worker", "hold_control"),
    [
        ("worker-only", True, False),
        ("control-only", False, True),
        ("combined", True, True),
    ],
)
def test_nonclean_thread_snapshots_use_actual_ids_and_frozen_control_bit(
    monkeypatch,
    worker_count,
    mode,
    hold_worker,
    hold_control,
):
    del mode
    target_worker_id = worker_count
    release_worker = Event()
    release_control = Event()
    original_worker_loop = AnalysisScheduler._worker_loop
    original_control_loop = AnalysisScheduler._control_loop

    def observed_worker_loop(self, worker_id):
        original_worker_loop(self, worker_id)
        if hold_worker and worker_id == target_worker_id:
            assert release_worker.wait(2.0)

    def observed_control_loop(self):
        original_control_loop(self)
        if hold_control:
            assert release_control.wait(2.0)

    monkeypatch.setattr(AnalysisScheduler, "_worker_loop", observed_worker_loop)
    monkeypatch.setattr(AnalysisScheduler, "_control_loop", observed_control_loop)
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            shutdown_timeout_seconds=0.1,
        ),
        audio_loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        analysis_executor=RecordingExecutor(),
        start_gate=PassThroughStartGate(),
        outcome_sink=OutcomeCollector(),
        clock=ThreadRecordingClock(7.0),
    )
    original_control_join = scheduler._control_thread.join
    synchronized_exit_snapshot = False

    def deterministic_control_join(timeout=None):
        nonlocal synchronized_exit_snapshot
        if synchronized_exit_snapshot:
            original_control_join(timeout)
            return
        if hold_control:
            assert scheduler._control_thread.is_alive()
        else:
            original_control_join(2.0)
            assert not scheduler._control_thread.is_alive()
        for worker_id, worker in enumerate(scheduler._workers, start=1):
            if not (hold_worker and worker_id == target_worker_id):
                worker.join(2.0)
                assert not worker.is_alive()
        if hold_worker:
            assert scheduler._workers[target_worker_id - 1].is_alive()
        if hold_control:
            assert scheduler._control_thread.is_alive()
            original_control_join(timeout)
        synchronized_exit_snapshot = True

    monkeypatch.setattr(
        scheduler._control_thread,
        "join",
        deterministic_control_join,
    )
    try:
        report = scheduler.shutdown(5e-324)
        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == (
            (target_worker_id,) if hold_worker else ()
        )
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is hold_control
        assert scheduler.shutdown(0.5) is report

        release_worker.set()
        release_control.set()
        _join_scheduler_threads(scheduler)
        final_diagnostics = scheduler.diagnostics()
        assert final_diagnostics.attempts == ()
        assert final_diagnostics.full_audio_residency_count == 0
        assert scheduler.shutdown() is report
        assert report.remaining_worker_ids == (
            (target_worker_id,) if hold_worker else ()
        )
        assert report.control_thread_alive is hold_control
    finally:
        release_worker.set()
        release_control.set()
        _join_scheduler_threads(scheduler)


@pytest.mark.parametrize("worker_count", [1, 6])
def test_nonclean_active_release_has_no_report_rewrite_or_payload_leak(
    monkeypatch,
    worker_count,
):
    residency = AudioResidencyProbe()
    executor = GatedResultExecutor(result_or_exception=_successful_result())
    outcomes = OutcomeCollector()
    events: list[SchedulerDiagnosticEvent] = []
    nested_reports: list[ShutdownReport] = []
    holder: dict[str, AnalysisScheduler] = {}

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        events.append(event)
        if (
            event.event_kind
            is SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
        ):
            nested_reports.append(holder["scheduler"].shutdown())

    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            shutdown_timeout_seconds=0.02,
        ),
        audio_loader=CloneForbiddenAudioLoader(residency),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=ThreadRecordingClock(9.0),
        diagnostic_sink=diagnostic_sink,
    )
    holder["scheduler"] = scheduler
    job = make_job(
        30_000 + worker_count,
        run_id=f"nonclean-run-{worker_count}",
        toolchain_id=f"nonclean-toolchain-{worker_count}",
    )
    try:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        before = scheduler.diagnostics()
        attempt = before.attempts[0]
        assert attempt.worker_id is not None

        original_control_join = scheduler._control_thread.join

        def join_control_to_exit(timeout=None):
            original_control_join(2.0)
            assert not scheduler._control_thread.is_alive()

        monkeypatch.setattr(
            scheduler._control_thread,
            "join",
            join_control_to_exit,
        )
        for worker_id, worker in enumerate(scheduler._workers, start=1):
            if worker_id == attempt.worker_id:
                continue
            original_join = worker.join

            def join_idle_to_exit(timeout=None, join=original_join):
                join(2.0)

            monkeypatch.setattr(worker, "join", join_idle_to_exit)

        report = scheduler.shutdown()
        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert report.remaining_attempt_ids == (job.analysis_attempt_id,)
        assert report.remaining_worker_ids == (attempt.worker_id,)
        assert report.remaining_run_lanes == (job.run_id,)
        assert report.remaining_toolchain_lanes == (
            canonical_toolchain_lane_key(
                f"nonclean-toolchain-{worker_count}"
            ),
        )
        assert report.control_thread_alive is False
        assert nested_reports == [report]
        assert scheduler.shutdown() is report

        executor.release()
        handed_off = outcomes.wait_for_count(1)
        assert len(handed_off) == 1
        assert handed_off[0].status is AnalysisStatus.CANCELED
        assert handed_off[0].result_payload is None
        _wait_for_no_active(scheduler)
        _join_scheduler_threads(scheduler)

        late = scheduler.diagnostics()
        assert late.attempts == ()
        assert late.pending == 0
        assert late.running == 0
        assert late.cancel_requested == 0
        assert late.full_audio_residency_count == 0
        assert residency.current == 0
        assert residency.created == residency.closed == 1
        assert all(handle.close_count == 1 for handle in scheduler._audio_loader.handles)
        assert _counter(
            scheduler,
            SchedulerCounterKey.LATE_RESULTS_SUPPRESSED,
        ) == 1
        assert scheduler.shutdown(0.5) is report
        assert report.status is ShutdownStatus.SHUTDOWN_INCOMPLETE
        assert report.remaining_attempt_ids == (job.analysis_attempt_id,)
        assert sum(
            event.event_kind
            is SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED
            for event in events
        ) == 1
    finally:
        executor.release()
        _wait_for_no_active(scheduler)
        if scheduler._shutdown_report is None:
            scheduler.shutdown()
        _join_scheduler_threads(scheduler)


def test_worker_callback_reentry_does_not_wait_for_owner_completion_clock():
    owner_name = "blocked-owner-completion-clock"
    clock = GatedShutdownCompletionClock(
        owner_thread_name=owner_name,
        started_value=10.0,
        completion_value_or_exception=20.0,
    )
    executor = GatedResultExecutor(result_or_exception=_successful_result())
    callback_entered = Event()
    allow_reentry = Event()
    nested_returned = Event()
    nested_reports: list[ShutdownReport] = []
    owner_reports: list[ShutdownReport] = []
    outcomes = []
    errors: list[BaseException] = []
    holder: dict[str, AnalysisScheduler] = {}

    def outcome_sink(outcome) -> None:
        outcomes.append(outcome)
        callback_entered.set()
        assert allow_reentry.wait(2.0), "callback reentry was not released"
        nested_reports.append(holder["scheduler"].shutdown())
        nested_returned.set()

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        if (
            event.event_kind
            is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
        ):
            clock.set(30.0)
            executor.release()

    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=1,
            shutdown_timeout_seconds=0.01,
        ),
        audio_loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcome_sink,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    holder["scheduler"] = scheduler
    job = make_job(19_003)
    owner = Thread(
        target=lambda: _record_shutdown(
            scheduler,
            owner_reports,
            errors,
        ),
        name=owner_name,
    )

    try:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        owner.start()
        assert callback_entered.wait(2.0), "worker callback was not entered"
        assert clock.completion_entered.wait(
            2.0
        ), "owner did not enter its completion clock read"

        allow_reentry.set()
        assert nested_returned.wait(
            0.5
        ), "callback shutdown waited for the owner completion clock"
        assert len(nested_reports) == 1
        transient = nested_reports[0]
        assert transient.status is ShutdownStatus.IN_PROGRESS
        assert transient.started_monotonic == 10.0
        assert transient.finished_monotonic == 30.0
        assert transient.remaining_attempt_ids == ()
        assert transient.remaining_worker_ids == (1,)
        assert transient.remaining_run_lanes == ()
        assert transient.remaining_toolchain_lanes == ()

        scheduler._workers[0].join(2.0)
        assert not scheduler._workers[0].is_alive()
        clock.release()
        owner.join(2.0)

        assert not owner.is_alive()
        assert errors == []
        assert len(owner_reports) == 1
        report = owner_reports[0]
        assert report.status is ShutdownStatus.CLOSED
        assert report.started_monotonic == 10.0
        assert report.finished_monotonic == 30.0
        assert report.remaining_attempt_ids == ()
        assert report.remaining_worker_ids == ()
        assert report.remaining_run_lanes == ()
        assert report.remaining_toolchain_lanes == ()
        assert report.control_thread_alive is False
        assert len(outcomes) == 1
        assert outcomes[0].analysis_attempt_id == job.analysis_attempt_id
        assert outcomes[0].status is AnalysisStatus.CANCELED
        assert outcomes[0].result_payload is None
        with scheduler._condition:
            receipts = tuple(scheduler._terminal_receipts)
        assert len(receipts) == 1
        assert receipts[0].analysis_attempt_id == job.analysis_attempt_id
        assert receipts[0].terminal_status is AnalysisStatus.CANCELED
        assert receipts[0].finished_monotonic == 30.0
        assert scheduler.shutdown() is report
        assert clock._owner_calls == 2
        _join_scheduler_threads(scheduler)
    finally:
        allow_reentry.set()
        executor.release()
        clock.release()
        owner.join(2.0)
        if scheduler._shutdown_report is None:
            _wait_for_no_active(scheduler)
            scheduler.shutdown()
        _join_scheduler_threads(scheduler)


@pytest.mark.parametrize("worker_count", [1, 6])
@pytest.mark.parametrize(
    "completion_fault",
    [
        pytest.param(
            RuntimeError("planned completion clock failure"),
            id="exception",
        ),
        pytest.param(20.0, id="regression"),
    ],
)
def test_later_worker_drain_time_survives_one_completion_clock_fault(
    worker_count,
    completion_fault,
):
    owner_name = f"clock-fault-owner-{worker_count}"
    clock = WorkerDrainCompletionFaultClock(
        owner_thread_name=owner_name,
        completion_value_or_exception=completion_fault,
    )
    executor = GatedResultExecutor(result_or_exception=_successful_result())
    outcomes = OutcomeCollector()

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        if (
            event.event_kind
            is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
        ):
            clock.enable_worker_terminal_value()
            executor.release()

    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            shutdown_timeout_seconds=0.5,
        ),
        audio_loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    job = make_job(40_000 + worker_count)
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []
    owner = Thread(
        target=lambda: _record_shutdown(scheduler, reports, errors),
        name=owner_name,
    )
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
        _join_scheduler_threads(scheduler)
    finally:
        executor.release()
        owner.join(2.0)
        if scheduler._shutdown_report is None:
            _wait_for_no_active(scheduler)
            scheduler.shutdown()
        _join_scheduler_threads(scheduler)


def test_final_report_clock_cannot_precede_worker_included_at_freeze():
    owner_name = "clock-freeze-owner"
    clock = _StaleOwnerCompletionClock(owner_thread_name=owner_name)
    executor = GatedResultExecutor(result_or_exception=_successful_result())
    outcomes = OutcomeCollector()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=1,
            shutdown_timeout_seconds=0.01,
        ),
        audio_loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=outcomes,
        clock=clock,
    )
    job = make_job(50_001)
    reports: list[ShutdownReport] = []
    errors: list[BaseException] = []
    owner = Thread(
        target=lambda: _record_shutdown(scheduler, reports, errors),
        name=owner_name,
    )

    join_returned = Event()
    allow_after_join = Event()
    worker_at_terminal_clock = Event()
    allow_worker_terminal_clock = Event()
    owner_step = Event()
    allow_owner_step = Event()
    owner_step_mode: list[str] = []
    original_worker_join = scheduler._workers[0].join
    original_read_clock = scheduler._read_clock
    original_background_read_clock = scheduler._read_clock_in_background
    owner_clock_reads = 0
    worker_clock_reads = 0

    def gated_worker_join(timeout=None):
        original_worker_join(timeout)
        join_returned.set()
        assert allow_after_join.wait(2.0), "post-join gate was not released"

    def gated_read_clock():
        nonlocal owner_clock_reads
        if current_thread().name == owner_name:
            owner_clock_reads += 1
        value = original_read_clock()
        if current_thread().name == owner_name:
            if owner_clock_reads == 2:
                owner_step_mode.append("completion-read")
                owner_step.set()
                assert allow_owner_step.wait(
                    2.0
                ), "completion-read gate was not released"
        return value

    def gated_background_read_clock():
        nonlocal worker_clock_reads
        if "analysis-scheduler-worker-" in current_thread().name:
            worker_clock_reads += 1
            if worker_clock_reads == 4:
                worker_at_terminal_clock.set()
                assert allow_worker_terminal_clock.wait(
                    2.0
                ), "worker terminal clock gate was not released"
        return original_background_read_clock()

    gated_condition = _FinalFreezeGateCondition(
        scheduler._condition,
        owner_thread_name=owner_name,
        owner_step=owner_step,
        owner_step_mode=owner_step_mode,
        allow_owner_step=allow_owner_step,
    )
    scheduler._condition = gated_condition
    scheduler._workers[0].join = gated_worker_join
    scheduler._read_clock = gated_read_clock
    scheduler._read_clock_in_background = gated_background_read_clock

    try:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        executor.wait_for_calls(1)
        clock.set(20.0)
        owner.start()
        assert join_returned.wait(2.0), "shutdown owner did not finish its join"

        clock.set(30.0)
        executor.release()
        assert worker_at_terminal_clock.wait(
            2.0
        ), "worker did not reach its terminal clock"
        gated_condition.arm()
        allow_after_join.set()
        assert owner_step.wait(2.0), "owner did not reach its final step"

        allow_worker_terminal_clock.set()
        outcome = outcomes.wait_for_count(1)[0]
        original_worker_join(2.0)
        assert not scheduler._workers[0].is_alive()
        with scheduler._condition:
            receipts = tuple(scheduler._terminal_receipts)
        assert len(receipts) == 1
        receipt = receipts[0]

        allow_owner_step.set()
        owner.join(2.0)
        assert not owner.is_alive()
        assert errors == []
        assert len(reports) == 1
        report = reports[0]
        assert report.status is ShutdownStatus.CLOSED
        assert outcome.analysis_attempt_id == job.analysis_attempt_id
        assert receipt.analysis_attempt_id == job.analysis_attempt_id
        assert outcome.finished_monotonic == 30.0
        assert receipt.finished_monotonic == 30.0
        assert report.finished_monotonic >= outcome.finished_monotonic
        assert report.finished_monotonic >= receipt.finished_monotonic
        assert owner_clock_reads == 2
        assert owner_step_mode in (
            ["completion-read"],
            ["final-condition"],
        )
        _join_scheduler_threads(scheduler)
    finally:
        executor.release()
        allow_after_join.set()
        allow_worker_terminal_clock.set()
        allow_owner_step.set()
        owner.join(2.0)
        if scheduler._shutdown_report is None:
            _wait_for_no_active(scheduler)
            scheduler.shutdown()
        _join_scheduler_threads(scheduler)
