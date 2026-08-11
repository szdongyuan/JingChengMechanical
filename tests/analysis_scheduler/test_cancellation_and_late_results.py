"""Exact process-local cancellation and drain-complete handoff tests."""

from __future__ import annotations

import sys
import threading
from threading import Condition, Event, Lock, Thread, current_thread

import pytest

from app.service.analysis.scheduler import (
    MAX_CANCEL_REASON_UTF8_BYTES,
    AnalysisCancelOrigin,
    AnalysisErrorCategory,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStatus,
    CancelStatus,
    SchedulerCounterKey,
    SchedulerDiagnosticEvent,
    SchedulerDiagnosticEventKind,
    ShutdownStatus,
    SubmitStatus,
)

from .fakes import (
    AudioResidencyProbe,
    CancellationGateAudioLoader,
    CloneForbiddenAudioLoader,
    GatedResultExecutor,
    ManualClock,
    OutcomeCollector,
    PassThroughStartGate,
    RecordingExecutor,
    make_job,
)


def make_scheduler(
    *,
    loader: object,
    executor: object,
    sink: object,
    clock: object | None = None,
    diagnostic_sink: object | None = None,
    pending_capacity: int = 64,
    terminal_receipt_capacity: int = 256,
) -> AnalysisScheduler:
    return AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=1,
            pending_capacity=pending_capacity,
            terminal_receipt_capacity=terminal_receipt_capacity,
        ),
        audio_loader=loader,
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=sink,
        clock=clock or ManualClock(1.0),
        diagnostic_sink=diagnostic_sink,
    )


def accepted(scheduler: AnalysisScheduler, job: object) -> None:
    assert scheduler.submit(job).status is SubmitStatus.ACCEPTED


def counter(scheduler: AnalysisScheduler, key: SchedulerCounterKey) -> int:
    return dict(scheduler.diagnostics().counters)[key.value]


class ControlWaitProbe:
    """Observe exact control-thread wait entries without sleep polling."""

    def __init__(self, monkeypatch) -> None:
        self._condition = Condition()
        self._count = 0
        original_wait = threading.Condition.wait

        def observed_wait(condition, timeout=None):
            if "analysis-scheduler-control-" in current_thread().name:
                with self._condition:
                    self._count += 1
                    self._condition.notify_all()
            return original_wait(condition, timeout)

        monkeypatch.setattr(threading.Condition, "wait", observed_wait)

    def wait_for_count(self, count: int) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: self._count >= count,
                timeout=2.0,
            ), f"control thread did not enter wait {count}"


def control_frame_retains_record(
    scheduler: AnalysisScheduler,
    expected_record: object,
) -> bool:
    frame = sys._current_frames()[scheduler._control_thread.ident]
    while frame is not None and frame.f_code.co_name != "_control_loop":
        frame = frame.f_back
    assert frame is not None

    def contains(value: object) -> bool:
        if value is expected_record:
            return True
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(item is expected_record for item in value)
        if isinstance(value, dict):
            return any(item is expected_record for item in value.values())
        return False

    return any(
        contains(value)
        for name, value in frame.f_locals.items()
        if name != "self"
    )


def test_pending_cancel_drops_control_frame_job_graph_at_idle(monkeypatch):
    waits = ControlWaitProbe(monkeypatch)
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=sink,
    )
    blocker = make_job(1, run_id="shared-run")
    target = make_job(2, run_id="shared-run")
    try:
        waits.wait_for_count(1)
        accepted(scheduler, blocker)
        executor.wait_for_calls(1)
        waits.wait_for_count(2)
        accepted(scheduler, target)
        waits.wait_for_count(3)
        with scheduler._condition:
            target_record = scheduler._active[target.analysis_attempt_id]

        receipt = scheduler.cancel(
            target.run_id,
            target.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "stop",
        )
        assert receipt.status is CancelStatus.CANCELED_PENDING
        sink.wait_for_attempt(target.analysis_attempt_id)
        waits.wait_for_count(4)

        assert control_frame_retains_record(scheduler, target_record) is False
        retained = scheduler.diagnostics().terminal_receipts
        assert retained[-1].analysis_attempt_id == target.analysis_attempt_id
        assert not hasattr(retained[-1], "job")
    finally:
        executor.release()
        sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_running_completion_drops_control_frame_job_graph_at_idle(monkeypatch):
    waits = ControlWaitProbe(monkeypatch)
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=sink,
    )
    job = make_job(1)
    try:
        waits.wait_for_count(1)
        accepted(scheduler, job)
        executor.wait_for_calls(1)
        waits.wait_for_count(2)
        with scheduler._condition:
            completed_record = scheduler._active[job.analysis_attempt_id]

        executor.release()
        sink.wait_for_count(1)
        with scheduler._condition:
            scheduler._wake_locked()
        waits.wait_for_count(3)

        assert control_frame_retains_record(scheduler, completed_record) is False
        diagnostics = scheduler.diagnostics()
        assert diagnostics.running == 0
        assert diagnostics.pending == 0
        assert diagnostics.terminal_receipts[-1].analysis_attempt_id == (
            job.analysis_attempt_id
        )
    finally:
        executor.release()
        if not sink.outcomes:
            sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_pending_cancel_is_exact_releases_capacity_and_hands_off_once():
    residency = AudioResidencyProbe()
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=sink,
        pending_capacity=1,
    )
    running = make_job(1)
    target = make_job(2, run_id="target-run")
    replacement = make_job(3)
    accepted_count = 0
    try:
        accepted(scheduler, running)
        accepted_count += 1
        executor.wait_for_calls(1)
        accepted(scheduler, target)
        accepted_count += 1
        assert scheduler.submit(replacement).status is SubmitStatus.QUEUE_FULL

        mismatch = scheduler.cancel(
            "wrong-run",
            target.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "do not cancel",
        )
        assert mismatch.status is CancelStatus.RUN_ID_MISMATCH
        assert scheduler.diagnostics().pending == 1

        receipt = scheduler.cancel(
            target.run_id,
            target.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "operator canceled",
        )
        assert receipt.status is CancelStatus.CANCELED_PENDING
        assert receipt.drain_pending is False
        canceled = sink.wait_for_attempt(target.analysis_attempt_id)
        assert [item.analysis_attempt_id for item in canceled] == [
            target.analysis_attempt_id
        ]
        assert canceled[0].status is AnalysisStatus.CANCELED
        assert canceled[0].result_payload is None
        assert canceled[0].error.category is AnalysisErrorCategory.ANALYSIS_CANCELED
        assert canceled[0].error.stage is AnalysisStage.CANCEL
        accepted(scheduler, replacement)
        accepted_count += 1
        assert scheduler.cancel(
            target.run_id,
            target.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "again",
        ).status is CancelStatus.ALREADY_TERMINAL
        assert scheduler.cancel(
            "missing-run",
            "missing-attempt",
            AnalysisCancelOrigin.USER_STOP,
            "",
        ).status is CancelStatus.NOT_FOUND
    finally:
        executor.release()
        sink.wait_for_count(accepted_count)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


@pytest.mark.parametrize(
    "bad_call, expected_exception",
    (
        (
            lambda scheduler: scheduler.cancel(
                "", "attempt", AnalysisCancelOrigin.USER_STOP, "reason"
            ),
            ValueError,
        ),
        (
            lambda scheduler: scheduler.cancel(
                "run", "", AnalysisCancelOrigin.USER_STOP, "reason"
            ),
            ValueError,
        ),
        (
            lambda scheduler: scheduler.cancel(
                1, "attempt", AnalysisCancelOrigin.USER_STOP, "reason"
            ),
            ValueError,
        ),
        (
            lambda scheduler: scheduler.cancel(
                "run", 1, AnalysisCancelOrigin.USER_STOP, "reason"
            ),
            ValueError,
        ),
        (
            lambda scheduler: scheduler.cancel(
                "run", "attempt", object(), "reason"
            ),
            TypeError,
        ),
        (
            lambda scheduler: scheduler.cancel(
                "run", "attempt", AnalysisCancelOrigin.USER_STOP, object()
            ),
            TypeError,
        ),
    ),
)
def test_cancel_validates_exact_inputs_without_mutation(
    bad_call,
    expected_exception,
):
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=sink,
    )
    job = make_job(1)
    try:
        accepted(scheduler, job)
        executor.wait_for_calls(1)
        with pytest.raises(expected_exception):
            bad_call(scheduler)
        assert scheduler.diagnostics().running == 1
        assert sink.outcomes == []
    finally:
        executor.release()
        sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_retained_receipts_bound_duplicate_ids_and_evict_fifo():
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        sink=sink,
        terminal_receipt_capacity=2,
    )
    jobs = [make_job(number) for number in range(1, 4)]
    try:
        for count, job in enumerate(jobs, 1):
            accepted(scheduler, job)
            sink.wait_for_count(count)

        receipts = scheduler.diagnostics().terminal_receipts
        assert tuple(item.analysis_attempt_id for item in receipts) == (
            jobs[1].analysis_attempt_id,
            jobs[2].analysis_attempt_id,
        )
        assert scheduler.cancel(
            jobs[0].run_id,
            jobs[0].analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "",
        ).status is CancelStatus.NOT_FOUND
        assert scheduler.cancel(
            jobs[1].run_id,
            jobs[1].analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "",
        ).status is CancelStatus.ALREADY_TERMINAL
        assert scheduler.cancel(
            "wrong-run",
            jobs[2].analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "",
        ).status is CancelStatus.RUN_ID_MISMATCH
        assert scheduler.submit(jobs[2]).status is (
            SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID
        )
        accepted(scheduler, jobs[0])
        sink.wait_for_count(4)
    finally:
        if scheduler.diagnostics().pending or scheduler.diagnostics().running:
            sink.wait_for_count(4)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_multibyte_reason_is_bounded_and_first_running_reason_is_retained():
    events: list[SchedulerDiagnosticEvent] = []
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=sink,
        diagnostic_sink=events.append,
    )
    job = make_job(1)
    original = "界" * 400
    try:
        accepted(scheduler, job)
        executor.wait_for_calls(1)
        first = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            original,
        )
        second = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.RESULT_EVICTION,
            "replacement",
        )
        assert first.status is second.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        token = executor.calls[0][2]
        assert token.is_cancelled is True
        assert token.reason != original
        assert "replacement" not in token.reason
        assert len(token.reason.encode("utf-8")) <= MAX_CANCEL_REASON_UTF8_BYTES
        assert original.startswith(token.reason)
        requested = [
            event
            for event in events
            if event.event_kind is SchedulerDiagnosticEventKind.CANCEL_REQUESTED
        ]
        assert len(requested) == 1
        assert requested[0].summary == token.reason
    finally:
        executor.release()
        outcome = sink.wait_for_count(1)[0]
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert outcome.status is AnalysisStatus.CANCELED
    assert outcome.result_payload is None
    assert outcome.error.summary == token.reason


def test_loader_blocked_cancel_retains_owner_until_external_return_and_close():
    residency = AudioResidencyProbe()
    loader = CancellationGateAudioLoader(residency)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=loader,
        executor=RecordingExecutor(),
        sink=sink,
    )
    job = make_job(1, toolchain_id="toolchain-a")
    try:
        accepted(scheduler, job)
        assert loader.entered.wait(2.0)
        token = loader.calls[0][1]
        receipt = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "stop",
        )
        assert receipt.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        assert token.is_cancelled is True
        diagnostics = scheduler.diagnostics()
        assert diagnostics.cancel_requested == 1
        assert diagnostics.full_audio_residency_count == 0
        assert sink.outcomes == []
        loader.release()
        outcome = sink.wait_for_count(1)[0]
    finally:
        loader.release()
        if not sink.outcomes:
            sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert outcome.status is AnalysisStatus.CANCELED
    assert loader.handles[0].close_count == 1
    assert residency.current == 0
    assert scheduler.diagnostics().running == 0


def test_post_loader_cancel_waits_for_handle_close_and_lane_release(monkeypatch):
    gate_lock = Lock()
    entered = Event()
    release = Event()
    worker_reads = 0
    original_read = AnalysisScheduler._read_clock_in_background

    def gated_worker_read(self):
        nonlocal worker_reads
        if "analysis-scheduler-worker-" in current_thread().name:
            with gate_lock:
                worker_reads += 1
                should_gate = worker_reads == 2
            if should_gate:
                entered.set()
                assert release.wait(2.0), "post-loader gate was not released"
        return original_read(self)

    monkeypatch.setattr(
        AnalysisScheduler,
        "_read_clock_in_background",
        gated_worker_read,
    )
    residency = AudioResidencyProbe()
    loader = CloneForbiddenAudioLoader(residency)
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=loader,
        executor=RecordingExecutor(),
        sink=sink,
    )
    job = make_job(1, toolchain_id="toolchain-a")
    try:
        accepted(scheduler, job)
        assert entered.wait(2.0)
        assert residency.current == 1
        receipt = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "stop",
        )
        assert receipt.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        assert loader.handles[0].closed.is_set() is False
        assert scheduler.diagnostics().cancel_requested == 1
        assert sink.outcomes == []
        release.set()
        outcome = sink.wait_for_count(1)[0]
    finally:
        release.set()
        if not sink.outcomes:
            sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert outcome.status is AnalysisStatus.CANCELED
    assert loader.handles[0].close_count == 1
    assert residency.current == 0


@pytest.mark.parametrize(
    "late_value",
    (
        AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=("late-success",),
        ),
        AnalysisExecutionResult(
            status=AnalysisExecutionStatus.NO_RESULT,
            result_payload=None,
            summary="late no result",
        ),
        RuntimeError("late failure"),
    ),
)
def test_executor_blocked_cancel_suppresses_every_late_terminal(late_value):
    residency = AudioResidencyProbe()
    loader = CloneForbiddenAudioLoader(residency)
    executor = GatedResultExecutor(late_value)
    sink = OutcomeCollector()
    scheduler = make_scheduler(loader=loader, executor=executor, sink=sink)
    job = make_job(1, toolchain_id="toolchain-a")
    try:
        accepted(scheduler, job)
        executor.wait_for_calls(1)
        token = executor.calls[0][2]
        receipt = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "stop",
        )
        assert receipt.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        assert token.is_cancelled is True
        assert residency.current == 1
        diagnostics = scheduler.diagnostics()
        assert diagnostics.cancel_requested == 1
        assert diagnostics.toolchain_lanes[0].running == 1
        assert sink.outcomes == []
        executor.release()
        outcome = sink.wait_for_count(1)[0]
    finally:
        executor.release()
        if not sink.outcomes:
            sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert outcome.status is AnalysisStatus.CANCELED
    assert outcome.result_payload is None
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_CANCELED
    assert loader.handles[0].close_count == 1
    assert residency.current == 0
    assert counter(scheduler, SchedulerCounterKey.LATE_RESULTS_SUPPRESSED) == 1
    assert counter(scheduler, SchedulerCounterKey.CANCELED) == 1
    assert counter(scheduler, SchedulerCounterKey.SUCCEEDED) == 0
    assert counter(scheduler, SchedulerCounterKey.NO_RESULT) == 0
    assert counter(scheduler, SchedulerCounterKey.FAILED) == 0


def test_completion_between_cancel_lookup_and_revalidation_wins_once(monkeypatch):
    cancel_read_entered = Event()
    release_cancel_read = Event()
    original_read_clock = AnalysisScheduler._read_clock

    def gated_cancel_read(self):
        if current_thread().name == "cancel-race":
            cancel_read_entered.set()
            assert release_cancel_read.wait(2.0), "cancel read gate was not released"
        return original_read_clock(self)

    monkeypatch.setattr(AnalysisScheduler, "_read_clock", gated_cancel_read)
    executor = GatedResultExecutor(
        AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=("winner",),
        )
    )
    sink = OutcomeCollector()
    residency = AudioResidencyProbe()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=sink,
    )
    job = make_job(1)
    receipts: list[object] = []
    cancel_errors: list[BaseException] = []
    cancel_done = Event()

    def cancel_after_lookup() -> None:
        try:
            receipts.append(
                scheduler.cancel(
                    job.run_id,
                    job.analysis_attempt_id,
                    AnalysisCancelOrigin.USER_STOP,
                    "late",
                )
            )
        except BaseException as exc:
            cancel_errors.append(exc)
        finally:
            cancel_done.set()

    cancel_thread = Thread(target=cancel_after_lookup, name="cancel-race")
    try:
        accepted(scheduler, job)
        executor.wait_for_calls(1)
        cancel_thread.start()
        assert cancel_read_entered.wait(2.0)
        executor.release()
        outcome = sink.wait_for_count(1)[0]
        assert outcome.status is AnalysisStatus.SUCCEEDED
        assert residency.current == 0
        release_cancel_read.set()
        assert cancel_done.wait(2.0)
        cancel_thread.join(2.0)

        assert cancel_errors == []
        assert receipts[0].status is CancelStatus.ALREADY_TERMINAL
        assert len(sink.outcomes) == 1
        diagnostics = scheduler.diagnostics()
        assert diagnostics.running == 0
        assert diagnostics.cancel_requested == 0
    finally:
        release_cancel_read.set()
        executor.release()
        if cancel_thread.is_alive():
            cancel_thread.join(2.0)
        if not sink.outcomes:
            sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_cancel_first_barrier_suppresses_completion_and_releases_owners_once():
    executor = GatedResultExecutor(
        AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=("winner",),
        )
    )
    sink = OutcomeCollector()
    residency = AudioResidencyProbe()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=executor,
        sink=sink,
    )
    job = make_job(1)
    try:
        accepted(scheduler, job)
        executor.wait_for_calls(1)
        receipt = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "stop",
        )
        assert receipt.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        assert sink.outcomes == []
        executor.release()
        outcome = sink.wait_for_count(1)[0]
        assert outcome.status is AnalysisStatus.CANCELED
        assert len(sink.outcomes) == 1
        assert residency.current == 0
        diagnostics = scheduler.diagnostics()
        assert diagnostics.running == 0
        assert diagnostics.cancel_requested == 0
    finally:
        executor.release()
        if not sink.outcomes:
            sink.wait_for_count(1)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_sink_exceptions_do_not_escape_retry_or_change_pending_terminal_state():
    outcome_calls: list[object] = []
    diagnostic_calls: list[SchedulerDiagnosticEvent] = []
    second_outcome = Event()

    def outcome_sink(outcome: object) -> None:
        outcome_calls.append(outcome)
        if len(outcome_calls) >= 2:
            second_outcome.set()
        raise RuntimeError("planned outcome sink failure")

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        diagnostic_calls.append(event)
        raise RuntimeError("planned diagnostic sink failure")

    executor = RecordingExecutor(gated=True)
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=executor,
        sink=outcome_sink,
        diagnostic_sink=diagnostic_sink,
    )
    first = make_job(1)
    target = make_job(2)
    try:
        accepted(scheduler, first)
        executor.wait_for_calls(1)
        accepted(scheduler, target)
        receipt = scheduler.cancel(
            target.run_id,
            target.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "stop",
        )
        assert receipt.status is CancelStatus.CANCELED_PENDING
        assert len(outcome_calls) == 1
        assert outcome_calls[0].status is AnalysisStatus.CANCELED
        assert scheduler.cancel(
            target.run_id,
            target.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "again",
        ).status is CancelStatus.ALREADY_TERMINAL
        assert counter(scheduler, SchedulerCounterKey.OUTCOME_SINK_FAILURE) == 1
        assert counter(scheduler, SchedulerCounterKey.CANCELED) == 1
        terminal_events = [
            event
            for event in diagnostic_calls
            if event.event_kind is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL
        ]
        assert len(terminal_events) == 1
    finally:
        executor.release()
        assert second_outcome.wait(2.0)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED
