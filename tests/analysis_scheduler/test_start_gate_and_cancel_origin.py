"""Durable start-gate and exact cancellation-origin scheduler seams."""

from __future__ import annotations

import dataclasses
from threading import Thread

import pytest

from app.service.analysis.scheduler import (
    MAX_ERROR_SUMMARY_UTF8_BYTES,
    AnalysisCancelOrigin,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisKind,
    AnalysisOutcome,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStartGateCode,
    AnalysisStartGateError,
    AnalysisStartGateFailure,
    AnalysisStatus,
    AttemptState,
    CancelStatus,
    FrozenResultObject,
    SchedulerCounterKey,
    SchedulerDiagnosticEvent,
    ShutdownStatus,
    SubmitStatus,
)

from .fakes import (
    GatedStartGate,
    ManualClock,
    OutcomeCollector,
    PassThroughStartGate,
    RecordingAudioLoader,
    RecordingExecutor,
    ScriptedStartGate,
    make_job,
)


def make_outcome(**changes: object) -> AnalysisOutcome:
    values: dict[str, object] = {
        "run_id": "run-1",
        "analysis_attempt_id": "attempt-1",
        "kind": AnalysisKind.AUTOMATIC,
        "status": AnalysisStatus.SUCCEEDED,
        "audio_reference_id": "audio-1",
        "result_payload": FrozenResultObject(()),
        "error": None,
        "submitted_monotonic": 1.0,
        "started_monotonic": 2.0,
        "finished_monotonic": 3.0,
        "queue_wait_ms": 1000,
        "execution_ms": 1000,
        "cancel_origin": None,
    }
    values.update(changes)
    return AnalysisOutcome(**values)


def canceled_outcome(origin: object) -> AnalysisOutcome:
    return make_outcome(
        status=AnalysisStatus.CANCELED,
        result_payload=None,
        error=AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_CANCELED,
            stage=AnalysisStage.CANCEL,
            summary="canceled",
        ),
        cancel_origin=origin,
    )


def test_start_gate_failure_is_frozen_slotted_exact_and_summary_bounded():
    failure = AnalysisStartGateFailure(
        AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
        "durable start failed",
    )

    assert dataclasses.is_dataclass(failure)
    assert failure.code is AnalysisStartGateCode.ATTEMPT_START_DB_FAILED
    assert failure.summary == "durable start failed"
    assert "__slots__" in AnalysisStartGateFailure.__dict__
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.summary = "changed"

    invalid = [
        (AnalysisStartGateCode.ATTEMPT_START_DB_FAILED.value, "summary"),
        (AnalysisStartGateCode.ATTEMPT_START_DB_FAILED, 1),
        (AnalysisStartGateCode.ATTEMPT_START_DB_FAILED, ""),
        (
            AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
            "界" * (MAX_ERROR_SUMMARY_UTF8_BYTES // 3 + 1),
        ),
    ]
    for code, summary in invalid:
        with pytest.raises((TypeError, ValueError)):
            AnalysisStartGateFailure(code, summary)


def test_start_gate_error_accepts_only_exact_failure_positionally():
    failure = AnalysisStartGateFailure(
        AnalysisStartGateCode.ATTEMPT_START_NOT_QUEUED,
        "attempt is no longer queued",
    )
    error = AnalysisStartGateError(failure)

    assert error.failure is failure
    assert str(error) == failure.summary
    with pytest.raises(TypeError):
        AnalysisStartGateError(failure=failure)
    for invalid in (None, object(), (failure.code, failure.summary)):
        with pytest.raises(TypeError):
            AnalysisStartGateError(invalid)


def test_start_gate_error_pair_is_closed_and_pre_executor():
    error = AnalysisError(
        category=AnalysisErrorCategory.ATTEMPT_START_FAILED,
        stage=AnalysisStage.START_GATE,
        summary="durable start failed",
        details=(("code", AnalysisStartGateCode.ATTEMPT_START_DB_FAILED.value),),
    )
    outcome = make_outcome(
        status=AnalysisStatus.FAILED,
        result_payload=None,
        error=error,
        execution_ms=None,
    )
    assert outcome.error is error

    for stage in AnalysisStage:
        if stage is AnalysisStage.START_GATE:
            continue
        with pytest.raises(ValueError):
            AnalysisError(
                category=AnalysisErrorCategory.ATTEMPT_START_FAILED,
                stage=stage,
                summary="illegal pair",
            )
    with pytest.raises(ValueError):
        make_outcome(
            status=AnalysisStatus.FAILED,
            result_payload=None,
            error=error,
            execution_ms=0,
        )


def test_cancel_origin_is_required_exactly_for_canceled_outcomes():
    for origin in AnalysisCancelOrigin:
        assert canceled_outcome(origin).cancel_origin is origin

    for invalid in (None, AnalysisCancelOrigin.USER_STOP.value, object()):
        with pytest.raises((TypeError, ValueError)):
            canceled_outcome(invalid)

    for status, result_payload, error, execution_ms in (
        (AnalysisStatus.SUCCEEDED, FrozenResultObject(()), None, 1000),
        (
            AnalysisStatus.NO_RESULT,
            None,
            AnalysisError(
                AnalysisErrorCategory.ANALYSIS_NO_RESULT,
                AnalysisStage.EXECUTE,
                "no result",
            ),
            1000,
        ),
        (
            AnalysisStatus.FAILED,
            None,
            AnalysisError(
                AnalysisErrorCategory.ATTEMPT_START_FAILED,
                AnalysisStage.START_GATE,
                "start failed",
            ),
            None,
        ),
    ):
        with pytest.raises(ValueError):
            make_outcome(
                status=status,
                result_payload=result_payload,
                error=error,
                execution_ms=execution_ms,
                cancel_origin=AnalysisCancelOrigin.USER_STOP,
            )


def scheduler_dependencies() -> dict[str, object]:
    return {
        "config": AnalysisSchedulerConfig(worker_count=1),
        "audio_loader": RecordingAudioLoader(),
        "analysis_executor": RecordingExecutor(),
        "outcome_sink": OutcomeCollector(),
        "clock": ManualClock(1.0),
    }


def make_scheduler(
    *,
    start_gate: object,
    loader: object,
    executor: object,
    sink: object,
    diagnostic_sink: object | None = None,
) -> AnalysisScheduler:
    return AnalysisScheduler(
        config=AnalysisSchedulerConfig(worker_count=1),
        audio_loader=loader,
        analysis_executor=executor,
        start_gate=start_gate,
        outcome_sink=sink,
        clock=ManualClock(1.0),
        diagnostic_sink=diagnostic_sink,
    )


def test_constructor_requires_a_callable_start_gate_member():
    with pytest.raises(TypeError):
        AnalysisScheduler(**scheduler_dependencies())

    for invalid in (None, object(), lambda: None):
        with pytest.raises(TypeError):
            AnalysisScheduler(start_gate=invalid, **scheduler_dependencies())


def test_gate_runs_once_before_loader_and_success_preserves_execution():
    gate = GatedStartGate()
    loader = RecordingAudioLoader()
    executor = RecordingExecutor()
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        start_gate=gate,
        loader=loader,
        executor=executor,
        sink=sink,
    )
    job = make_job(1)
    try:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        assert gate.entered.wait(2.0)
        assert gate.calls == [job]
        assert loader.calls == []
        assert executor.calls == []

        gate.release()
        outcome = sink.wait_for_count(1)[0]
        assert loader.calls[0][0] is job.audio
        assert executor.calls[0][0] is job
        assert outcome.status is AnalysisStatus.SUCCEEDED
        assert outcome.cancel_origin is None
    finally:
        gate.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


@pytest.mark.parametrize(
    ("raised", "expected_code", "expected_summary"),
    [
        (
            AnalysisStartGateError(
                AnalysisStartGateFailure(
                    AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
                    "durable start rolled back",
                )
            ),
            AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
            "durable start rolled back",
        ),
        (
            RuntimeError("private database failure"),
            AnalysisStartGateCode.ATTEMPT_START_GATE_EXCEPTION,
            "analysis start gate failed",
        ),
    ],
)
def test_gate_failure_is_terminal_pre_executor_and_releases_lanes(
    raised: BaseException,
    expected_code: AnalysisStartGateCode,
    expected_summary: str,
):
    gate = ScriptedStartGate(raised, None)
    loader = RecordingAudioLoader()
    executor = RecordingExecutor()
    sink = OutcomeCollector()
    events: list[SchedulerDiagnosticEvent] = []
    scheduler = make_scheduler(
        start_gate=gate,
        loader=loader,
        executor=executor,
        sink=sink,
        diagnostic_sink=events.append,
    )
    failed = make_job(1, run_id="shared-run", toolchain_id="shared-toolchain")
    successor = make_job(
        2,
        run_id="shared-run",
        toolchain_id="shared-toolchain",
    )
    try:
        assert scheduler.submit(failed).status is SubmitStatus.ACCEPTED
        failed_outcome = sink.wait_for_count(1)[0]

        assert failed_outcome.status is AnalysisStatus.FAILED
        assert failed_outcome.result_payload is None
        assert failed_outcome.error.category is (
            AnalysisErrorCategory.ATTEMPT_START_FAILED
        )
        assert failed_outcome.error.stage is AnalysisStage.START_GATE
        assert failed_outcome.error.summary == expected_summary
        assert failed_outcome.error.details == (("code", expected_code.value),)
        assert failed_outcome.started_monotonic is not None
        assert failed_outcome.execution_ms is None
        assert failed_outcome.cancel_origin is None
        assert loader.calls == []
        assert executor.calls == []

        diagnostics = scheduler.diagnostics()
        assert dict(diagnostics.counters)[
            SchedulerCounterKey.START_GATE_FAILURE.value
        ] == 1
        terminal = [
            event
            for event in events
            if event.analysis_attempt_id == failed.analysis_attempt_id
            and event.state_after is AttemptState.FAILED
        ]
        assert len(terminal) == 1
        assert terminal[0].state_before is AttemptState.RUNNING
        assert terminal[0].load_ms is None
        assert terminal[0].execution_ms is None
        assert terminal[0].details == (("code", expected_code.value),)

        assert scheduler.submit(successor).status is SubmitStatus.ACCEPTED
        succeeded_outcome = sink.wait_for_count(2)[1]
        assert succeeded_outcome.status is AnalysisStatus.SUCCEEDED
        assert succeeded_outcome.cancel_origin is None
        assert gate.calls == [failed, successor]
        assert len(loader.calls) == len(executor.calls) == 1
    finally:
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_running_cancel_wins_and_preserves_the_first_exact_origin():
    gate = GatedStartGate(
        AnalysisStartGateError(
            AnalysisStartGateFailure(
                AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
                "late durable-start failure",
            )
        )
    )
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        start_gate=gate,
        loader=RecordingAudioLoader(),
        executor=RecordingExecutor(),
        sink=sink,
    )
    job = make_job(1)
    try:
        assert scheduler.submit(job).status is SubmitStatus.ACCEPTED
        assert gate.entered.wait(2.0)
        first = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "first cancel",
        )
        second = scheduler.cancel(
            job.run_id,
            job.analysis_attempt_id,
            AnalysisCancelOrigin.RESULT_EVICTION,
            "replacement cancel",
        )
        assert first.status is second.status is CancelStatus.CANCEL_REQUESTED_RUNNING

        gate.release()
        outcome = sink.wait_for_count(1)[0]
        assert outcome.status is AnalysisStatus.CANCELED
        assert outcome.cancel_origin is AnalysisCancelOrigin.USER_STOP
        assert outcome.error.summary == "first cancel"
        assert outcome.execution_ms is None
    finally:
        gate.release()
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


@pytest.mark.parametrize(
    "origin",
    [AnalysisCancelOrigin.USER_STOP, AnalysisCancelOrigin.RESULT_EVICTION],
)
def test_pending_cancel_carries_the_requested_origin(origin: AnalysisCancelOrigin):
    gate = GatedStartGate()
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        start_gate=gate,
        loader=RecordingAudioLoader(),
        executor=RecordingExecutor(),
        sink=sink,
    )
    blocker = make_job(1)
    pending = make_job(2, kind=AnalysisKind.REANALYSIS)
    try:
        assert scheduler.submit(blocker).status is SubmitStatus.ACCEPTED
        assert gate.entered.wait(2.0)
        assert scheduler.submit(pending).status is SubmitStatus.ACCEPTED
        receipt = scheduler.cancel(
            pending.run_id,
            pending.analysis_attempt_id,
            origin,
            "pending cancel",
        )
        assert receipt.status is CancelStatus.CANCELED_PENDING
        outcomes = sink.wait_for_attempt(pending.analysis_attempt_id)
        outcome = next(
            item
            for item in outcomes
            if item.analysis_attempt_id == pending.analysis_attempt_id
        )
        assert outcome.cancel_origin is origin
    finally:
        gate.release()
        sink.wait_for_count(2)
        assert scheduler.shutdown().status is ShutdownStatus.CLOSED


def test_shutdown_cancel_carries_shutdown_origin_for_pending_and_running():
    gate = GatedStartGate()
    sink = OutcomeCollector()
    scheduler = make_scheduler(
        start_gate=gate,
        loader=RecordingAudioLoader(),
        executor=RecordingExecutor(),
        sink=sink,
    )
    running = make_job(1)
    pending = make_job(2)
    reports: list[object] = []
    shutdown_thread = Thread(target=lambda: reports.append(scheduler.shutdown()))
    try:
        assert scheduler.submit(running).status is SubmitStatus.ACCEPTED
        assert gate.entered.wait(2.0)
        assert scheduler.submit(pending).status is SubmitStatus.ACCEPTED
        shutdown_thread.start()
        pending_outcomes = sink.wait_for_attempt(pending.analysis_attempt_id)
        pending_outcome = next(
            item
            for item in pending_outcomes
            if item.analysis_attempt_id == pending.analysis_attempt_id
        )
        assert pending_outcome.cancel_origin is (
            AnalysisCancelOrigin.SCHEDULER_SHUTDOWN
        )

        gate.release()
        outcomes = sink.wait_for_count(2)
        running_outcome = next(
            item
            for item in outcomes
            if item.analysis_attempt_id == running.analysis_attempt_id
        )
        assert running_outcome.cancel_origin is (
            AnalysisCancelOrigin.SCHEDULER_SHUTDOWN
        )
        shutdown_thread.join(2.0)
        assert reports[0].status is ShutdownStatus.CLOSED
    finally:
        gate.release()
        shutdown_thread.join(2.0)
        if not reports:
            assert scheduler.shutdown().status is ShutdownStatus.CLOSED
