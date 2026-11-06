"""Complete-input ownership and execution normalization tests."""

from __future__ import annotations

import builtins
import os
from dataclasses import replace
from pathlib import Path
from threading import Condition, Event

import pytest

from app.service.analysis.scheduler import (
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionError,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStatus,
    AudioLoadError,
    FrozenResultObject,
    SchedulerCounterKey,
    SchedulerDiagnosticEvent,
    ShutdownStatus,
    SubmitStatus,
)

from .fakes import (
    AudioResidencyProbe,
    CallbackExecutor,
    CloneForbiddenAudioLoader,
    FullAudioPayloadProbe,
    FullInputSamplesProbe,
    FakeLoadedAudio,
    GatedAudioLoader,
    ManualClock,
    OutcomeCollector,
    PassThroughStartGate,
    RaisingAudioLoader,
    RaisingExecutor,
    RecordingExecutor,
    WorkerReadGateClock,
    make_job,
)


def make_scheduler(
    *,
    loader: object,
    executor: object,
    sink: object,
    worker_count: int = 1,
    pending_capacity: int = 64,
    clock: ManualClock | None = None,
    diagnostic_sink: object | None = None,
) -> AnalysisScheduler:
    return AnalysisScheduler(
        config=AnalysisSchedulerConfig(
            worker_count=worker_count,
            pending_capacity=pending_capacity,
        ),
        audio_loader=loader,
        analysis_executor=executor,
        start_gate=PassThroughStartGate(),
        outcome_sink=sink,
        clock=clock or ManualClock(1.0),
        diagnostic_sink=diagnostic_sink,
    )


def accepted(scheduler: AnalysisScheduler, *jobs: object) -> None:
    for job in jobs:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED


def counter(scheduler: AnalysisScheduler, key: SchedulerCounterKey) -> int:
    return dict(scheduler.diagnostics().counters)[key.value]


def test_pending_jobs_load_only_after_dispatch_and_share_one_exact_token():
    loader = GatedAudioLoader()
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(loader=loader, executor=executor, sink=sink)
    first = make_job(1)
    second = make_job(2)
    try:
        accepted(scheduler, first)
        assert loader.entered.wait(2.0)
        accepted(scheduler, second)
        assert loader.calls == []

        loader.release()
        loader.wait_for_calls(1)
        executor.wait_for_calls(1)
        assert loader.calls[0][0] is first.audio
        assert executor.calls[0][0] is first
        assert loader.calls[0][1] is executor.calls[0][2]
        assert len(loader.calls) == 1

        executor.release()
        sink.wait_for_count(2)
    finally:
        loader.release()
        executor.release()
        if len(sink.outcomes) < 2:
            sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_submit_does_no_path_io_while_worker_is_occupied(monkeypatch):
    loader = CloneForbiddenAudioLoader(AudioResidencyProbe())
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    scheduler = make_scheduler(loader=loader, executor=executor, sink=sink)
    try:
        accepted(scheduler, make_job(1))
        executor.wait_for_calls(1)

        def forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("submit performed path I/O")

        monkeypatch.setattr(builtins, "open", forbidden)
        monkeypatch.setattr(os, "stat", forbidden)
        monkeypatch.setattr(Path, "open", forbidden)
        accepted(scheduler, make_job(2))
    finally:
        executor.release()
        sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_scheduler_tracks_one_handle_and_closes_before_outcome():
    residency = AudioResidencyProbe()
    loader = CloneForbiddenAudioLoader(residency)
    executor = RecordingExecutor()
    sink_observations: list[tuple[int, bool]] = []
    collected = OutcomeCollector()

    def sink(outcome: object) -> None:
        sink_observations.append(
            (residency.current, loader.handles[0].closed.is_set())
        )
        collected(outcome)

    scheduler = make_scheduler(loader=loader, executor=executor, sink=sink)
    try:
        accepted(scheduler, make_job(1))
        collected.wait_for_count(1)
        diagnostics = scheduler.diagnostics()
    finally:
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert residency.current == 0
    assert residency.peak == 1
    assert loader.handles[0].close_count == 1
    assert sink_observations == [(0, True)]
    assert diagnostics.full_audio_residency_count == 0
    assert diagnostics.peak_full_audio_residency_count == 1


def make_error(
    category: AnalysisErrorCategory,
    stage: AnalysisStage,
    summary: str = "planned failure",
) -> AnalysisError:
    return AnalysisError(category=category, stage=stage, summary=summary)


def bypass_execution_result(
    *,
    status: object,
    payload: object,
    summary: object,
) -> AnalysisExecutionResult:
    result = object.__new__(AnalysisExecutionResult)
    object.__setattr__(result, "status", status)
    object.__setattr__(result, "result_payload", payload)
    object.__setattr__(result, "summary", summary)
    return result


def run_one(
    *,
    loader: object,
    executor: object,
    clock: ManualClock | None = None,
    job: object | None = None,
    diagnostic_sink: object | None = None,
) -> tuple[AnalysisScheduler, object]:
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=loader,
        executor=executor,
        sink=sink,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    accepted(scheduler, job or make_job(1))
    outcome = sink.wait_for_count(1)[0]
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED
    return scheduler, outcome


@pytest.mark.parametrize(
    "category",
    (
        AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
        AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
        AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
        AnalysisErrorCategory.AUDIO_LOAD_FAILED,
    ),
)
def test_known_audio_load_errors_keep_exact_category(category):
    error = make_error(category, AnalysisStage.AUDIO_LOAD)
    scheduler, outcome = run_one(
        loader=RaisingAudioLoader(AudioLoadError(error)),
        executor=RecordingExecutor(),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.error is error
    assert outcome.result_payload is None
    assert outcome.execution_ms is None
    assert counter(scheduler, SchedulerCounterKey.AUDIO_LOAD_FAILURE) == 1


def test_unknown_audio_load_error_is_normalized_without_exception_leakage():
    exception = RuntimeError("private loader detail")
    _, outcome = run_one(
        loader=RaisingAudioLoader(exception),
        executor=RecordingExecutor(),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.error.category is AnalysisErrorCategory.AUDIO_LOAD_FAILED
    assert outcome.error.stage is AnalysisStage.AUDIO_LOAD
    assert "private loader detail" not in repr(outcome)
    assert outcome.result_payload is None


@pytest.mark.parametrize(
    ("result", "status", "category", "summary"),
    (
        (
            AnalysisExecutionResult(
                status=AnalysisExecutionStatus.SUCCEEDED,
                result_payload=(1, 2.5, "waveform-summary"),
            ),
            AnalysisStatus.SUCCEEDED,
            None,
            None,
        ),
        (
            AnalysisExecutionResult(
                status=AnalysisExecutionStatus.NO_RESULT,
                result_payload=None,
            ),
            AnalysisStatus.NO_RESULT,
            AnalysisErrorCategory.ANALYSIS_NO_RESULT,
            "analysis produced no result",
        ),
        (
            AnalysisExecutionResult(
                status=AnalysisExecutionStatus.NO_RESULT,
                result_payload=None,
                summary="no stable decision",
            ),
            AnalysisStatus.NO_RESULT,
            AnalysisErrorCategory.ANALYSIS_NO_RESULT,
            "no stable decision",
        ),
    ),
)
def test_execution_results_normalize_success_and_no_result(
    result,
    status,
    category,
    summary,
):
    residency = AudioResidencyProbe()
    scheduler, outcome = run_one(
        loader=CloneForbiddenAudioLoader(residency),
        executor=CallbackExecutor(lambda job, audio, token: result),
    )

    assert outcome.status is status
    assert outcome.result_payload == result.result_payload
    assert (None if outcome.error is None else outcome.error.category) is category
    assert (None if outcome.error is None else outcome.error.summary) == summary
    expected_counter = (
        SchedulerCounterKey.SUCCEEDED
        if status is AnalysisStatus.SUCCEEDED
        else SchedulerCounterKey.NO_RESULT
    )
    assert counter(scheduler, expected_counter) == 1
    assert residency.current == 0


@pytest.mark.parametrize(
    "result",
    (
        bypass_execution_result(
            status=AnalysisExecutionStatus.SUCCEEDED,
            payload=None,
            summary=None,
        ),
        bypass_execution_result(
            status=AnalysisExecutionStatus.SUCCEEDED,
            payload=("ok",),
            summary="contradictory",
        ),
        bypass_execution_result(
            status=AnalysisExecutionStatus.NO_RESULT,
            payload=("leak",),
            summary=None,
        ),
        bypass_execution_result(
            status="succeeded",
            payload=("leak",),
            summary=None,
        ),
    ),
)
def test_contradictory_execution_result_is_failed_without_payload(result):
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=CallbackExecutor(lambda job, audio, token: result),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.result_payload is None
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    assert outcome.error.stage is AnalysisStage.EXECUTE


@pytest.mark.parametrize("payload_kind", ("cycle", "excessive_depth"))
def test_invalid_recursive_payload_is_execution_failure(payload_kind):
    if payload_kind == "cycle":
        payload = object.__new__(FrozenResultObject)
        object.__setattr__(payload, "items", (("cycle", payload),))
    else:
        payload = "leaf"
        for _ in range(33):
            payload = (payload,)
    result = bypass_execution_result(
        status=AnalysisExecutionStatus.SUCCEEDED,
        payload=payload,
        summary=None,
    )

    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=CallbackExecutor(lambda job, audio, token: result),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.result_payload is None
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED


class QtObjectProbe:
    pass


class OrmObjectProbe:
    pass


class HandleProbe:
    def close(self) -> None:
        pass


def callback_probe() -> None:
    pass


@pytest.mark.parametrize(
    "forbidden_payload",
    (
        [],
        b"audio",
        bytearray(b"audio"),
        memoryview(b"audio"),
        FullAudioPayloadProbe(),
        FullInputSamplesProbe(),
        FakeLoadedAudio(make_job(90).audio),
        QtObjectProbe(),
        OrmObjectProbe(),
        HandleProbe(),
        callback_probe,
    ),
)
def test_forbidden_result_runtime_values_are_failed_without_leakage(
    forbidden_payload,
):
    result = bypass_execution_result(
        status=AnalysisExecutionStatus.SUCCEEDED,
        payload=forbidden_payload,
        summary=None,
    )
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=CallbackExecutor(lambda job, audio, token: result),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.result_payload is None
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    assert forbidden_payload is not outcome.result_payload


def test_structurally_valid_numeric_tuple_and_string_has_no_audio_heuristic():
    payload = tuple(range(1024)) + ("base64-looking-audio-AAAA",)
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=CallbackExecutor(
            lambda job, audio, token: AnalysisExecutionResult(
                status=AnalysisExecutionStatus.SUCCEEDED,
                result_payload=payload,
            )
        ),
    )

    assert outcome.status is AnalysisStatus.SUCCEEDED
    assert outcome.result_payload is payload


@pytest.mark.parametrize(
    "error",
    (
        make_error(
            AnalysisErrorCategory.ANALYSIS_TIMEOUT,
            AnalysisStage.EXECUTE,
            "executor deadline expired",
        ),
        make_error(
            AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            AnalysisStage.EXECUTE,
            "executor rejected input",
        ),
    ),
)
def test_known_execution_errors_keep_exact_error_and_close_audio(error):
    residency = AudioResidencyProbe()
    scheduler, outcome = run_one(
        loader=CloneForbiddenAudioLoader(residency),
        executor=RaisingExecutor(AnalysisExecutionError(error)),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.error is error
    assert residency.current == 0
    expected = (
        SchedulerCounterKey.ANALYSIS_TIMEOUT
        if error.category is AnalysisErrorCategory.ANALYSIS_TIMEOUT
        else SchedulerCounterKey.EXECUTOR_FAILURE
    )
    assert counter(scheduler, expected) == 1


def test_cooperative_error_without_cancel_request_is_execution_failure():
    error = make_error(
        AnalysisErrorCategory.ANALYSIS_CANCELED,
        AnalysisStage.CANCEL,
        "unrequested cancellation",
    )
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RaisingExecutor(AnalysisExecutionError(error)),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    assert outcome.error.stage is AnalysisStage.EXECUTE


def test_unknown_executor_error_is_normalized_and_audio_is_closed():
    residency = AudioResidencyProbe()
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(residency),
        executor=RaisingExecutor(RuntimeError("private executor detail")),
    )

    assert outcome.status is AnalysisStatus.FAILED
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED
    assert outcome.error.stage is AnalysisStage.EXECUTE
    assert outcome.result_payload is None
    assert residency.current == 0


def test_execution_timing_starts_after_load_and_ai_timeout_is_unchanged():
    clock = ManualClock(10.0)
    residency = AudioResidencyProbe()

    def after_slow_load(reference: object, token: object) -> None:
        clock.set(40.0)

    observed: list[tuple[float, float]] = []

    def execute(job, audio, token):
        observed.append(
            (
                job.execution_timeout_seconds,
                job.snapshot.ai_toolchain.timeout_seconds,
            )
        )
        clock.set(40.25)
        raise AnalysisExecutionError(
            make_error(
                AnalysisErrorCategory.ANALYSIS_TIMEOUT,
                AnalysisStage.EXECUTE,
                "executor deadline expired",
            )
        )

    job = replace(
        make_job(1, toolchain_id="toolchain-a"),
        execution_timeout_seconds=2.5,
    )
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(
            residency,
            callback=after_slow_load,
        ),
        executor=CallbackExecutor(execute),
        clock=clock,
        job=job,
    )

    assert observed == [(2.5, 1.0)]
    assert outcome.error.category is AnalysisErrorCategory.ANALYSIS_TIMEOUT
    assert outcome.execution_ms == 250
    assert outcome.queue_wait_ms == 0


def test_load_timing_excludes_dispatch_and_control_lock_delay(monkeypatch):
    clock = WorkerReadGateClock(
        gate_on_worker_read=2,
        value=1.0,
    )
    residency = AudioResidencyProbe()
    worker_dispatched = Event()
    release_worker = Event()
    loader_entered = Event()
    release_loader = Event()
    events: list[SchedulerDiagnosticEvent] = []
    terminal_emitted = Event()
    original = AnalysisScheduler._execute_successful_attempt

    def dispatch_gate(self, worker_id, command):
        worker_dispatched.set()
        assert release_worker.wait(2.0), "dispatch gate was not released"
        return original(self, worker_id, command)

    def on_load(reference: object, token: object) -> None:
        loader_entered.set()
        assert release_loader.wait(2.0), "loader gate was not released"
        clock.set(31.0)

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        events.append(event)
        terminal_emitted.set()

    monkeypatch.setattr(
        AnalysisScheduler,
        "_execute_successful_attempt",
        dispatch_gate,
    )
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(
            residency,
            callback=on_load,
        ),
        executor=RecordingExecutor(),
        sink=sink,
        clock=clock,
        diagnostic_sink=diagnostic_sink,
    )
    try:
        accepted(scheduler, make_job(1))
        assert worker_dispatched.wait(2.0)
        clock.set(10.0)
        scheduler._condition.acquire()
        try:
            release_worker.set()
            clock.set(30.0)
        finally:
            scheduler._condition.release()
        assert loader_entered.wait(2.0)
        scheduler._condition.acquire()
        try:
            release_loader.set()
            assert clock.entered.wait(2.0)
            clock.release()
            clock.set(60.0)
        finally:
            scheduler._condition.release()
        sink.wait_for_count(1)
        assert terminal_emitted.wait(2.0)
    finally:
        release_worker.set()
        release_loader.set()
        clock.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    terminal = events[-1]
    assert terminal.load_ms == 1000
    assert residency.current == 0


def test_execution_timing_starts_after_control_bookkeeping():
    clock = WorkerReadGateClock(
        gate_on_worker_read=3,
        value=1.0,
    )
    residency = AudioResidencyProbe()
    executor_entered = Event()
    delayed_by_scheduler_lock = Event()

    def execute(job, audio, token):
        executor_entered.set()
        clock.set(
            81.0 if delayed_by_scheduler_lock.is_set() else 2.0
        )
        return AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=("ok",),
        )

    sink = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=CallbackExecutor(execute),
        sink=sink,
        clock=clock,
    )
    try:
        accepted(scheduler, make_job(1))
        assert clock.entered.wait(2.0)
        scheduler._condition.acquire()
        entered_while_control_lock_blocked = False
        try:
            clock.release()
            entered_while_control_lock_blocked = executor_entered.wait(0.5)
            if not entered_while_control_lock_blocked:
                delayed_by_scheduler_lock.set()
                clock.set(80.0)
        finally:
            scheduler._condition.release()
        outcome = sink.wait_for_count(1)[0]
    finally:
        clock.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert entered_while_control_lock_blocked
    assert outcome.execution_ms == 1000
    assert residency.current == 0


@pytest.mark.parametrize("worker_count", range(1, 7))
def test_pending_pressure_never_exceeds_configured_audio_residency(worker_count):
    residency = AudioResidencyProbe()
    loader = CloneForbiddenAudioLoader(residency)
    executor = RecordingExecutor(gated=True)
    sink = OutcomeCollector()
    job_count = worker_count * 4
    scheduler = make_scheduler(
        loader=loader,
        executor=executor,
        sink=sink,
        worker_count=worker_count,
        pending_capacity=job_count,
    )
    try:
        accepted(
            scheduler,
            *(make_job(number) for number in range(1, job_count + 1)),
        )
        executor.wait_for_calls(worker_count)
        residency.wait_for_peak(worker_count)
        diagnostics = scheduler.diagnostics()
        assert len(loader.calls) == worker_count
        assert diagnostics.full_audio_residency_count == worker_count
        assert diagnostics.peak_full_audio_residency_count == worker_count
    finally:
        executor.release()
        sink.wait_for_count(job_count)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert residency.current == 0
    assert residency.created == job_count
    assert residency.closed == job_count
    assert residency.peak <= worker_count
    assert all(handle.close_count == 1 for handle in loader.handles)


class RecordingFaultingSink:
    def __init__(self, *, fail: bool = False, block: bool = False) -> None:
        self._condition = Condition()
        self._release = Event()
        if not block:
            self._release.set()
        self.fail = fail
        self.outcomes: list[object] = []

    def __call__(self, outcome: object) -> None:
        with self._condition:
            self.outcomes.append(outcome)
            self._condition.notify_all()
        assert self._release.wait(2.0), "sink gate was not released"
        if self.fail:
            raise RuntimeError("planned sink fault")

    def wait_for_count(self, count: int) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.outcomes) >= count,
                timeout=2.0,
            )

    def release(self) -> None:
        self._release.set()


@pytest.mark.parametrize("worker_count", range(1, 7))
@pytest.mark.parametrize(
    "scenario",
    (
        "success",
        "no_result",
        "known_load_failure",
        "known_execution_failure",
        "unexpected_execution_failure",
        "timeout",
        "sink_failure",
    ),
)
def test_audio_residency_bound_holds_for_every_terminal_path(
    worker_count,
    scenario,
):
    residency = AudioResidencyProbe()
    if scenario == "known_load_failure":
        loader = RaisingAudioLoader(
            AudioLoadError(
                make_error(
                    AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                    AnalysisStage.AUDIO_LOAD,
                )
            )
        )
    else:
        loader = CloneForbiddenAudioLoader(residency)

    if scenario == "no_result":
        executor = CallbackExecutor(
            lambda job, audio, token: AnalysisExecutionResult(
                status=AnalysisExecutionStatus.NO_RESULT,
                result_payload=None,
            )
        )
    elif scenario == "known_execution_failure":
        executor = RaisingExecutor(
            AnalysisExecutionError(
                make_error(
                    AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
                    AnalysisStage.EXECUTE,
                )
            )
        )
    elif scenario == "unexpected_execution_failure":
        executor = RaisingExecutor(RuntimeError("unexpected"))
    elif scenario == "timeout":
        executor = RaisingExecutor(
            AnalysisExecutionError(
                make_error(
                    AnalysisErrorCategory.ANALYSIS_TIMEOUT,
                    AnalysisStage.EXECUTE,
                )
            )
        )
    else:
        executor = RecordingExecutor()

    sink = RecordingFaultingSink(fail=scenario == "sink_failure")
    job_count = worker_count * 3
    scheduler = make_scheduler(
        loader=loader,
        executor=executor,
        sink=sink,
        worker_count=worker_count,
        pending_capacity=job_count,
    )
    accepted(
        scheduler,
        *(make_job(number) for number in range(1, job_count + 1)),
    )
    sink.wait_for_count(job_count)
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    diagnostics = scheduler.diagnostics()
    assert residency.current == 0
    assert residency.peak <= worker_count
    assert diagnostics.full_audio_residency_count == 0
    assert diagnostics.peak_full_audio_residency_count <= worker_count
    if isinstance(loader, CloneForbiddenAudioLoader):
        assert all(handle.close_count == 1 for handle in loader.handles)


def test_outcome_sink_can_read_diagnostics_and_fault_is_not_retried():
    residency = AudioResidencyProbe()
    holder: dict[str, AnalysisScheduler] = {}
    calls: list[object] = []

    def sink(outcome: object) -> None:
        calls.append(holder["scheduler"].diagnostics())
        raise RuntimeError("planned outcome fault")

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=RecordingExecutor(),
        sink=sink,
    )
    holder["scheduler"] = scheduler
    accepted(scheduler, make_job(1))

    deadline = Event()
    for _ in range(100):
        if counter(scheduler, SchedulerCounterKey.OUTCOME_SINK_FAILURE) == 1:
            deadline.set()
            break
        Event().wait(0.01)
    assert deadline.is_set()
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED
    assert len(calls) == 1
    assert residency.current == 0
    assert counter(scheduler, SchedulerCounterKey.SUCCEEDED) == 1


def test_diagnostic_sink_runs_outside_lock_and_faults_without_recursion():
    residency = AudioResidencyProbe()
    holder: dict[str, AnalysisScheduler] = {}
    events: list[SchedulerDiagnosticEvent] = []

    def diagnostic_sink(event: SchedulerDiagnosticEvent) -> None:
        holder["scheduler"].diagnostics()
        events.append(event)
        raise RuntimeError("planned diagnostic fault")

    collected = OutcomeCollector()
    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency),
        executor=RecordingExecutor(),
        sink=collected,
        diagnostic_sink=diagnostic_sink,
    )
    holder["scheduler"] = scheduler
    accepted(scheduler, make_job(1))
    outcome = collected.wait_for_count(1)[0]
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED

    assert outcome.status is AnalysisStatus.SUCCEEDED
    assert len(events) == 1
    assert counter(scheduler, SchedulerCounterKey.DIAGNOSTIC_SINK_FAILURE) == 1


def test_blocked_sink_has_no_audio_residency_or_active_lane():
    residency = AudioResidencyProbe()
    loader = CloneForbiddenAudioLoader(residency)
    sink = RecordingFaultingSink(block=True)
    scheduler = make_scheduler(
        loader=loader,
        executor=RecordingExecutor(),
        sink=sink,
    )
    try:
        accepted(scheduler, make_job(1))
        sink.wait_for_count(1)
        diagnostics = scheduler.diagnostics()
        assert residency.current == 0
        assert loader.handles[0].closed.is_set()
        assert diagnostics.full_audio_residency_count == 0
        assert diagnostics.running == 0
    finally:
        sink.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_loader_executor_and_outcome_ports_run_without_scheduler_lock():
    residency = AudioResidencyProbe()
    holder: dict[str, AnalysisScheduler] = {}
    observations: list[str] = []
    collected = OutcomeCollector()

    def on_load(reference: object, token: object) -> None:
        holder["scheduler"].diagnostics()
        observations.append("load")

    def execute(job, audio, token):
        diagnostics = holder["scheduler"].diagnostics()
        assert diagnostics.attempts[0].execution_ms is not None
        observations.append("execute")
        return AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=("ok",),
        )

    def sink(outcome: object) -> None:
        holder["scheduler"].diagnostics()
        observations.append("outcome")
        collected(outcome)

    scheduler = make_scheduler(
        loader=CloneForbiddenAudioLoader(residency, callback=on_load),
        executor=CallbackExecutor(execute),
        sink=sink,
    )
    holder["scheduler"] = scheduler
    accepted(scheduler, make_job(1))
    collected.wait_for_count(1)
    assert scheduler.shutdown().status is ShutdownStatus.CLOSED
    assert observations == ["load", "execute", "outcome"]


def test_outcome_contains_only_public_identity_not_path_or_audio():
    job = make_job(1)
    _, outcome = run_one(
        loader=CloneForbiddenAudioLoader(AudioResidencyProbe()),
        executor=RecordingExecutor(),
        job=job,
    )

    assert outcome.run_id == job.run_id
    assert outcome.analysis_attempt_id == job.analysis_attempt_id
    assert outcome.audio_reference_id == job.audio.reference_id
    assert not hasattr(outcome, "absolute_path")
    assert not hasattr(outcome, "audio")
    assert job.audio.absolute_path not in repr(outcome)
def test_frozen_ai_failure_does_not_change_other_one_shot_outcomes():
    from app.service.analysis.production_executor import (
        frozen_ai_snapshot_identity,
    )
    from app.service.analysis.scheduler import AiToolchainSnapshot

    toolchain = AiToolchainSnapshot(
        toolchain_id="unchanged",
        runtime_lane_key="ai-toolchain:unchanged",
        execution_revision="r1",
        execution_mode="resident",
        entrypoint="main.py",
        package_dir="C:/package",
        params_json="{}",
        timeout_seconds=1.0,
    )
    identity = frozen_ai_snapshot_identity(toolchain)
    assert identity.startswith("sha256:")
    assert len(identity) == 71
    assert {
        AnalysisExecutionStatus.SUCCEEDED.value,
        AnalysisExecutionStatus.NO_RESULT.value,
    } == {"succeeded", "no_result"}
