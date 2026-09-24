from __future__ import annotations

import threading
from dataclasses import replace
from functools import partial
from pathlib import Path
import struct
from typing import Callable

import pytest

from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisJob,
    AnalysisKind,
    AnalysisOutcome,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStartGateCode,
    AnalysisStartGateError,
    AnalysisStatus,
    AudioStorageKind,
    CancelReceipt,
    CancelStatus,
    FrozenResultObject,
    PersistedAudioReference,
    ShutdownReport,
    ShutdownStatus,
    SubmitReceipt,
    SubmitStatus,
)
from app.service.run_persistence import (
    AnalysisPersistenceBoundary,
    AttemptKind,
    AttemptState,
    AudioRecord,
    BridgeAuthorityStatus,
    BridgeCancelRequest,
    BridgeCancelStatus,
    BridgeDatabaseCloseStatus,
    BridgeInputError,
    BridgeSchedulerShutdownStatus,
    BridgeSubmissionStatus,
    BridgeTerminalStatus,
    DatabaseLifecycleCode,
    DatabaseUnavailableError,
    InitialAnalysisRequest,
    Judgment,
    ReanalysisRequest,
    RunPersistenceSchedulerBridge,
    RunRecord,
    TransactionResult,
    TransactionState,
    freeze_analysis_envelope,
)
from app.service.run_persistence import run_persistence_scheduler_bridge as bridge_module
from tests.analysis_scheduler.fakes import (
    ManualClock,
    RecordingAudioLoader,
    RecordingExecutor,
)


RUN_ID = "11111111-1111-4111-8111-111111111111"
AUDIO_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
OTHER_RUN_ID = "44444444-4444-4444-8444-444444444444"
OTHER_ATTEMPT_ID = "55555555-5555-4555-8555-555555555555"
NOW = "2026-07-27T10:11:12.123456Z"


def _envelope_json() -> str:
    return freeze_analysis_envelope(
        {
            "schema": "analysis-input/v1",
            "config": {"gain": 2, "selected_channels": [0]},
            "workorder": {"part": "P-7"},
            "channel_mapping": {"left": 0},
            "analysis": {
                "window_start_seconds": 0.0,
                "window_end_seconds": 1.0,
                "algorithm_revision": "algorithm-r4",
                "execution_timeout_seconds": 30.0,
                "queue_timeout_seconds": 4.0,
                "toolchain": None,
            },
        }
    )


def _run_record(
    *,
    ingress_kind: str = "CAPTURED",
    state: str = "ACTIVE",
    config_snapshot_json: str | None = None,
) -> RunRecord:
    terminal = state != "ACTIVE"
    return RunRecord(
        run_id=RUN_ID,
        ingress_kind=ingress_kind,
        trigger_source="PLC" if ingress_kind == "CAPTURED" else "IMPORT",
        config_id=7,
        hardware_id=9 if ingress_kind == "CAPTURED" else None,
        imported_source_identity=(
            "sha256:scheduler-bridge-lifecycle-import"
            if ingress_kind == "IMPORTED"
            else None
        ),
        imported_source_path=(
            r"C:\managed\scheduler-bridge-lifecycle-import.wav"
            if ingress_kind == "IMPORTED"
            else None
        ),
        imported_expected_frame_count=(
            10 if ingress_kind == "IMPORTED" else None
        ),
        causal_revision=0,
        config_snapshot_json=(
            _envelope_json()
            if config_snapshot_json is None
            else config_snapshot_json
        ),
        hardware_snapshot_json="{}",
        calibration_snapshot_json='{"offset":1}',
        state=state,
        judgment="PASS" if state == "SUCCEEDED" else None,
        judgment_source="AUTOMATIC" if state == "SUCCEEDED" else "NONE",
        judged_at=NOW if state == "SUCCEEDED" else None,
        terminal_code="ANALYSIS_SUCCEEDED" if terminal else None,
        terminal_summary=None,
        accepted_at=NOW,
        terminal_at=NOW if terminal else None,
        updated_at=NOW,
    )


def _audio_record(path: Path) -> AudioRecord:
    return AudioRecord(
        audio_id=AUDIO_ID,
        save_job_id="22222222-2222-4222-8222-222222222222",
        run_id=RUN_ID,
        source_kind="CAPTURED_AUDIO",
        source_identity="managed-input:scheduler-bridge-lifecycle",
        content_revision=1,
        final_path=str(path.resolve()),
        container_format="WAV_PCM",
        sample_rate_hz=48_000,
        channel_count=2,
        frame_count=10,
        duration_ms=1,
        byte_size=path.stat().st_size,
        closed_verified_at=NOW,
        created_at=NOW,
    )


def _reference(record: AudioRecord) -> PersistedAudioReference:
    return PersistedAudioReference(
        reference_id=record.audio_id,
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=record.final_path,
        sample_rate_hz=record.sample_rate_hz,
        channel_count=record.channel_count,
        frame_count=record.frame_count,
        dtype="<i2",
        content_revision=f"{record.audio_id}:{record.byte_size}",
        audio_data_id=record.audio_id,
    )


def _committed(value: bool) -> TransactionResult[bool]:
    return TransactionResult(state=TransactionState.COMMITTED, value=value)


def _rolled_back(reason: str = "write failed") -> TransactionResult[bool]:
    return TransactionResult(
        state=TransactionState.ROLLED_BACK,
        reason=reason,
    )


def _unknown(reason: str = "commit unknown") -> TransactionResult[bool]:
    return TransactionResult(
        state=TransactionState.UNKNOWN,
        reason=reason,
        restart_required=True,
    )


class _Runtime:
    def __init__(self, *, ready: bool = True, closed: bool = False) -> None:
        self._ready = ready
        self._closed = closed
        self.ready_probes = 0
        self.closed_probes = 0
        self.quarantine_calls = 0
        self.close_calls = 0

    @property
    def ready(self) -> bool:
        self.ready_probes += 1
        return self._ready and not self._closed

    @property
    def closed(self) -> bool:
        self.closed_probes += 1
        return self._closed

    def quarantine_unknown(self) -> None:
        self.quarantine_calls += 1
        self._ready = False
        self._closed = True

    def close_after_upstream_stopped(self) -> None:
        self.close_calls += 1
        self._ready = False
        self._closed = True


class _RunRepository:
    def __init__(self, record: RunRecord | None) -> None:
        self.record = record
        self.calls: list[str] = []

    def get(self, run_id: str) -> RunRecord | None:
        self.calls.append(run_id)
        return self.record


class _AudioRepository:
    def __init__(self, record: AudioRecord | None) -> None:
        self.record = record
        self.calls: list[str] = []

    def get_for_run(self, run_id: str) -> AudioRecord | None:
        self.calls.append(run_id)
        return self.record


class _AttemptRepository:
    def __init__(
        self,
        *,
        create_result: object = None,
        mark_result: object = None,
        terminal_result: object = None,
    ) -> None:
        self.create_result = _committed(True) if create_result is None else create_result
        self.mark_result = _committed(True) if mark_result is None else mark_result
        self.terminal_result = (
            _committed(True) if terminal_result is None else terminal_result
        )
        self.created = []
        self.marked = []
        self.queued_finished = []
        self.running_finished = []
        self.replacements = []

    def create_queued(self, attempt):
        self.created.append(attempt)
        if isinstance(self.create_result, BaseException):
            raise self.create_result
        return self.create_result

    def mark_running(self, attempt_id: str, now: str):
        self.marked.append((attempt_id, now))
        if isinstance(self.mark_result, BaseException):
            raise self.mark_result
        return self.mark_result

    def finish_queued_and_project(self, attempt_id: str, outcome, now: str):
        self.queued_finished.append((attempt_id, outcome, now))
        if isinstance(self.terminal_result, BaseException):
            raise self.terminal_result
        return self.terminal_result

    def finish_running_and_project(self, attempt_id: str, outcome, now: str):
        self.running_finished.append((attempt_id, outcome, now))
        if isinstance(self.terminal_result, BaseException):
            raise self.terminal_result
        return self.terminal_result

    def replace_succeeded_result(self, **arguments):
        self.replacements.append(arguments)
        if isinstance(self.terminal_result, BaseException):
            raise self.terminal_result
        return self.terminal_result


class _Scheduler:
    def __init__(
        self,
        *,
        submit_value: object | Callable[[AnalysisJob], object] | None = None,
        cancel_value: object | None = None,
        shutdown_value: object | None = None,
        start_gate: object | None = None,
        outcome_sink: object | None = None,
    ) -> None:
        self.submit_value = submit_value
        self.cancel_value = cancel_value
        self.shutdown_value = shutdown_value
        self.start_gate = start_gate
        self.outcome_sink = outcome_sink
        self.submitted: list[AnalysisJob] = []
        self.canceled = []
        self.shutdown_calls = []

    def submit(self, job: AnalysisJob):
        self.submitted.append(job)
        value = self.submit_value
        if callable(value):
            return value(job)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            return _accepted_receipt(job)
        return value

    def cancel(self, run_id, analysis_attempt_id, origin, reason):
        self.canceled.append((run_id, analysis_attempt_id, origin, reason))
        if callable(self.cancel_value):
            return self.cancel_value(
                run_id,
                analysis_attempt_id,
                origin,
                reason,
            )
        if isinstance(self.cancel_value, BaseException):
            raise self.cancel_value
        return self.cancel_value

    def shutdown(self, timeout_seconds=None):
        self.shutdown_calls.append(timeout_seconds)
        if callable(self.shutdown_value):
            return self.shutdown_value(timeout_seconds)
        return self.shutdown_value


def _accepted_receipt(job: AnalysisJob) -> SubmitReceipt:
    return SubmitReceipt(
        status=SubmitStatus.ACCEPTED,
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        submission_sequence=1,
        error=None,
    )


def _rejected_receipt(job: AnalysisJob, status: SubmitStatus) -> SubmitReceipt:
    category = {
        SubmitStatus.INVALID_JOB: AnalysisErrorCategory.INVALID_JOB,
        SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID:
            AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
        SubmitStatus.QUEUE_FULL: AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
        SubmitStatus.NOT_ACCEPTING:
            AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
    }[status]
    return SubmitReceipt(
        status=status,
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        submission_sequence=None,
        error=AnalysisError(
            category=category,
            stage=AnalysisStage.ADMISSION,
            summary=status.value,
        ),
    )


def _queue_timeout_outcome(job: AnalysisJob) -> AnalysisOutcome:
    return AnalysisOutcome(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        kind=job.kind,
        status=AnalysisStatus.FAILED,
        audio_reference_id=job.audio.reference_id,
        result_payload=None,
        error=AnalysisError(
            category=AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,
            stage=AnalysisStage.QUEUE,
            summary="queue wait expired",
        ),
        submitted_monotonic=1.0,
        started_monotonic=None,
        finished_monotonic=2.0,
        queue_wait_ms=1000,
        execution_ms=None,
        cancel_origin=None,
    )


def _succeeded_outcome(
    job: AnalysisJob,
    *,
    payload: object | None = None,
) -> AnalysisOutcome:
    if payload is None:
        payload = FrozenResultObject(
            items=(
                ("result", FrozenResultObject(items=(("score", 7),))),
                ("judgment", "PASS"),
            )
        )
    return AnalysisOutcome(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        kind=job.kind,
        status=AnalysisStatus.SUCCEEDED,
        audio_reference_id=job.audio.reference_id,
        result_payload=payload,
        error=None,
        submitted_monotonic=1.0,
        started_monotonic=2.0,
        finished_monotonic=3.0,
        queue_wait_ms=1000,
        execution_ms=1000,
        cancel_origin=None,
    )


def _failed_outcome(
    job: AnalysisJob,
    category: AnalysisErrorCategory,
    stage: AnalysisStage,
) -> AnalysisOutcome:
    return AnalysisOutcome(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        kind=job.kind,
        status=AnalysisStatus.FAILED,
        audio_reference_id=job.audio.reference_id,
        result_payload=None,
        error=AnalysisError(
            category=category,
            stage=stage,
            summary=category.value,
        ),
        submitted_monotonic=1.0,
        started_monotonic=2.0,
        finished_monotonic=3.0,
        queue_wait_ms=1000,
        execution_ms=1000 if stage is AnalysisStage.EXECUTE else None,
        cancel_origin=None,
    )


def _canceled_outcome(
    job: AnalysisJob,
    origin: AnalysisCancelOrigin,
    *,
    started: bool,
) -> AnalysisOutcome:
    return AnalysisOutcome(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        kind=job.kind,
        status=AnalysisStatus.CANCELED,
        audio_reference_id=job.audio.reference_id,
        result_payload=None,
        error=AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_CANCELED,
            stage=AnalysisStage.CANCEL,
            summary="canceled",
        ),
        submitted_monotonic=1.0,
        started_monotonic=2.0 if started else None,
        finished_monotonic=3.0,
        queue_wait_ms=1000,
        execution_ms=None,
        cancel_origin=origin,
    )


def _start_gate_failure_outcome(
    job: AnalysisJob,
    code: AnalysisStartGateCode,
) -> AnalysisOutcome:
    return AnalysisOutcome(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        kind=job.kind,
        status=AnalysisStatus.FAILED,
        audio_reference_id=job.audio.reference_id,
        result_payload=None,
        error=AnalysisError(
            category=AnalysisErrorCategory.ATTEMPT_START_FAILED,
            stage=AnalysisStage.START_GATE,
            summary="start failed",
            details=(("code", code.value),),
        ),
        submitted_monotonic=1.0,
        started_monotonic=2.0,
        finished_monotonic=3.0,
        queue_wait_ms=1000,
        execution_ms=None,
        cancel_origin=None,
    )


def _cancel_receipt(
    run_id: str,
    attempt_id: str,
    status: CancelStatus,
) -> CancelReceipt:
    error = None
    if status in (CancelStatus.NOT_FOUND, CancelStatus.RUN_ID_MISMATCH):
        error = AnalysisError(
            category=AnalysisErrorCategory.INVALID_JOB,
            stage=AnalysisStage.CANCEL,
            summary=status.value,
        )
    return CancelReceipt(
        status=status,
        run_id=run_id,
        analysis_attempt_id=attempt_id,
        drain_pending=status is CancelStatus.CANCEL_REQUESTED_RUNNING,
        error=error,
    )


def _shutdown_report(status: ShutdownStatus) -> ShutdownReport:
    remaining = status is not ShutdownStatus.CLOSED
    return ShutdownReport(
        status=status,
        started_monotonic=1.0,
        finished_monotonic=2.0,
        canceled_pending=0,
        cancel_requested_running=1 if remaining else 0,
        remaining_attempt_ids=(ATTEMPT_ID,) if remaining else (),
        remaining_worker_ids=(1,) if remaining else (),
        remaining_run_lanes=(RUN_ID,) if remaining else (),
        remaining_toolchain_lanes=(),
        control_thread_alive=remaining,
    )


def _make_bridge(
    tmp_path: Path,
    *,
    runtime: _Runtime | None = None,
    run: RunRecord | None | object = ...,
    audio: AudioRecord | None | object = ...,
    attempt_repository: _AttemptRepository | None = None,
    scheduler: _Scheduler | None = None,
    now_utc: Callable[[], str] = lambda: NOW,
    terminal_report_sink: Callable[[object], None] | None = None,
    enrichment_sink: Callable[[object], None] | None = None,
):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF")
    actual_run = _run_record() if run is ... else run
    actual_audio = _audio_record(audio_path) if audio is ... else audio
    runtime = _Runtime() if runtime is None else runtime
    attempts = (
        _AttemptRepository()
        if attempt_repository is None
        else attempt_repository
    )
    reports = []

    def record_report(report: object) -> None:
        reports.append(report)
        if terminal_report_sink is not None:
            terminal_report_sink(report)

    boundary = AnalysisPersistenceBoundary(
        runtime=runtime,
        attempt_repository=attempts,
        now_utc=now_utc,
        enrichment_sink=enrichment_sink,
    )
    scheduler = _Scheduler() if scheduler is None else scheduler
    bridge = RunPersistenceSchedulerBridge(
        runtime=runtime,
        run_repository=_RunRepository(actual_run),
        audio_repository=_AudioRepository(actual_audio),
        attempt_repository=attempts,
        boundary=boundary,
        scheduler=scheduler,
        now_utc=now_utc,
    )
    bridge.submit_initial = partial(
        bridge.submit_initial,
        terminal_sink=record_report,
    )
    bridge.submit_reanalysis = partial(
        bridge.submit_reanalysis,
        terminal_sink=record_report,
    )
    return bridge, boundary, scheduler, attempts, reports, actual_audio


def test_construction_uses_one_direct_boundary_for_both_scheduler_ports(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    attempts = _AttemptRepository()
    reports = []
    boundary = AnalysisPersistenceBoundary(
        runtime=runtime,
        attempt_repository=attempts,
        now_utc=lambda: NOW,
    )
    scheduler = _Scheduler(start_gate=boundary, outcome_sink=boundary)
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF")

    bridge = RunPersistenceSchedulerBridge(
        runtime=runtime,
        run_repository=_RunRepository(_run_record()),
        audio_repository=_AudioRepository(_audio_record(audio_path)),
        attempt_repository=attempts,
        boundary=boundary,
        scheduler=scheduler,
        now_utc=lambda: NOW,
    )

    assert scheduler.start_gate is boundary
    assert scheduler.outcome_sink is boundary
    assert bridge.boundary is boundary
    for forbidden in ("bind", "set_boundary", "set_outcome_sink", "proxy"):
        assert not hasattr(boundary, forbidden)


def test_non_callable_per_submit_sink_fails_before_any_side_effect(
    tmp_path: Path,
) -> None:
    bridge, _, scheduler, attempts, _, audio = _make_bridge(tmp_path)
    raw_submit = bridge.submit_initial.func

    with pytest.raises(BridgeInputError):
        raw_submit(
            InitialAnalysisRequest(RUN_ID, _reference(audio)),
            None,
        )

    assert attempts.created == []
    assert scheduler.submitted == []


def test_attempt_context_stores_exactly_one_sink_only_after_queue_authority(
    tmp_path: Path,
) -> None:
    rejected_bridge, rejected_boundary, _, _, _, rejected_audio = _make_bridge(
        tmp_path,
        attempt_repository=_AttemptRepository(create_result=_rolled_back()),
    )
    rejected_bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(rejected_audio))
    )

    assert rejected_boundary._context_count == 0

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    custom_reports: list[object] = []
    custom_sink = custom_reports.append
    raw_submit = bridge.submit_initial.func
    result = raw_submit(
        InitialAnalysisRequest(RUN_ID, _reference(audio)),
        custom_sink,
    )
    context = boundary._contexts[result.analysis_attempt_id]

    assert [name for name in context.__slots__ if "sink" in name] == [
        "terminal_sink"
    ]
    assert context.terminal_sink is custom_sink
    assert attempts.created[0].attempt_id == result.analysis_attempt_id
    assert scheduler.submitted[0] is context.job


@pytest.mark.parametrize(
    ("ready", "closed", "code", "restart"),
    [
        (False, False, "ANALYSIS_ADMISSION_CLOSED", False),
        (False, True, "ANALYSIS_DB_CLOSED", True),
    ],
)
def test_pre_attempt_runtime_matrix(
    tmp_path: Path,
    ready: bool,
    closed: bool,
    code: str,
    restart: bool,
) -> None:
    bridge, _, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=_Runtime(ready=ready, closed=closed),
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (result.status, result.code, result.restart_required) == (
        BridgeSubmissionStatus.REJECTED,
        code,
        restart,
    )
    assert result.analysis_attempt_id is None
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


@pytest.mark.parametrize(
    ("kind", "run", "audio_missing", "code"),
    [
        ("initial", None, False, "ANALYSIS_RUN_NOT_FOUND"),
        ("initial", _run_record(state="FAILED"), False, "ANALYSIS_RUN_NOT_ACTIVE"),
        ("reanalysis", None, False, "REANALYSIS_RUN_NOT_FOUND"),
        ("reanalysis", _run_record(state="ACTIVE"), False, "REANALYSIS_RUN_NOT_TERMINAL"),
        ("initial", _run_record(), True, "ANALYSIS_AUDIO_NOT_FOUND"),
        ("reanalysis", _run_record(state="SUCCEEDED"), True, "ANALYSIS_AUDIO_NOT_FOUND"),
    ],
)
def test_pre_attempt_run_and_audio_matrix(
    tmp_path: Path,
    kind: str,
    run: RunRecord | None,
    audio_missing: bool,
    code: str,
) -> None:
    bridge, _, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        run=run,
        audio=None if audio_missing else ...,
    )
    if kind == "initial" and audio is None:
        request_path = tmp_path / "request-only.wav"
        request_path.write_bytes(b"RIFF")
        request_audio = _audio_record(request_path)
    else:
        request_audio = audio
    request = (
        InitialAnalysisRequest(RUN_ID, _reference(request_audio))
        if kind == "initial"
        else ReanalysisRequest(RUN_ID, 0.25, 0.75)
    )

    result = (
        bridge.submit_initial(request)
        if kind == "initial"
        else bridge.submit_reanalysis(request)
    )

    assert (result.status, result.code) == (
        BridgeSubmissionStatus.REJECTED,
        code,
    )
    assert result.analysis_attempt_id is None
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


def test_pre_attempt_initial_audio_identity_mismatch(tmp_path: Path) -> None:
    bridge, _, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    request = InitialAnalysisRequest(
        RUN_ID,
        replace(_reference(audio), frame_count=audio.frame_count + 1),
    )

    result = bridge.submit_initial(request)

    assert result.code == "ANALYSIS_AUDIO_IDENTITY_MISMATCH"
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


def test_pre_attempt_reanalysis_requires_readable_final(tmp_path: Path) -> None:
    final_path = tmp_path / "will-exist.wav"
    final_path.write_bytes(b"RIFF")
    missing = _audio_record(final_path)
    Path(missing.final_path).unlink()
    bridge, _, scheduler, attempts, reports, _ = _make_bridge(
        tmp_path,
        run=_run_record(state="SUCCEEDED"),
        audio=missing,
    )

    result = bridge.submit_reanalysis(ReanalysisRequest(RUN_ID, 0.25, 0.75))

    assert result.code == "REANALYSIS_AUDIO_UNREADABLE"
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


def test_reanalysis_selected_window_reaches_exact_durable_scheduler_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_start = struct.unpack("!d", bytes.fromhex("3fd3333333333333"))[0]
    selected_end = struct.unpack("!d", bytes.fromhex("3feccccccccccccd"))[0]
    monkeypatch.setattr(bridge_module, "new_uuid4", lambda: ATTEMPT_ID)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        run=_run_record(state="SUCCEEDED"),
    )

    result = bridge.submit_reanalysis(
        ReanalysisRequest(RUN_ID, selected_start, selected_end)
    )

    assert result.status is BridgeSubmissionStatus.ACCEPTED
    assert len(scheduler.submitted) == 1
    job = scheduler.submitted[0]
    assert selected_start != 0.0
    assert selected_end != 1.0
    assert job.snapshot.analysis_window_start_seconds == selected_start
    assert job.snapshot.analysis_window_end_seconds == selected_end
    assert struct.pack(
        "!d", job.snapshot.analysis_window_start_seconds
    ) == struct.pack("!d", selected_start)
    assert struct.pack(
        "!d", job.snapshot.analysis_window_end_seconds
    ) == struct.pack("!d", selected_end)
    assert job.audio == _reference(audio)
    assert job.snapshot.config_snapshot_json.encode("utf-8") == (
        b'{"gain":2,"selected_channels":[0]}'
    )
    assert (
        job.snapshot.calibration_snapshot_json.encode("utf-8")
        == b'{"offset":1}'
    )
    assert (
        result.analysis_attempt_id
        == attempts.created[0].attempt_id
        == job.analysis_attempt_id
        == ATTEMPT_ID
    )

    boundary.before_audio_load(job)
    boundary(_succeeded_outcome(job))

    assert len(reports) == 1
    assert reports[0].analysis_attempt_id == ATTEMPT_ID
    assert reports[0].attempt_kind is AttemptKind.REANALYSIS
    assert boundary._contexts == {}


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (float("nan"), 0.75),
        (0.25, float("nan")),
        (float("inf"), 0.75),
        (float("-inf"), 0.75),
        (0.25, float("inf")),
        (0.25, float("-inf")),
        (-0.25, 0.75),
        (0.75, 0.75),
        (0.9, 0.75),
    ],
)
def test_malformed_reanalysis_windows_create_zero_work(
    tmp_path: Path,
    start: float,
    end: float,
) -> None:
    bridge, boundary, scheduler, attempts, reports, _ = _make_bridge(
        tmp_path,
        run=_run_record(state="SUCCEEDED"),
    )
    request = ReanalysisRequest(RUN_ID, 0.25, 0.75)
    object.__setattr__(request, "analysis_window_start_sec", start)
    object.__setattr__(request, "analysis_window_end_sec", end)
    claim_calls = []
    original_claim = boundary._claim_submission

    def recording_claim():
        claim_calls.append(True)
        return original_claim()

    boundary._claim_submission = recording_claim

    with pytest.raises(BridgeInputError):
        bridge.submit_reanalysis(request)

    assert claim_calls == []
    assert bridge.run_repository.calls == []
    assert bridge.audio_repository.calls == []
    assert attempts.created == []
    assert scheduler.submitted == []
    assert boundary._contexts == {}
    assert reports == []


def test_reanalysis_differing_window_early_and_late_outcomes_are_one_shot(
    tmp_path: Path,
) -> None:
    selected_start = struct.unpack("!d", bytes.fromhex("3fd3333333333333"))[0]
    selected_end = struct.unpack("!d", bytes.fromhex("3feccccccccccccd"))[0]
    attempts = _AttemptRepository(terminal_result=_committed(True))
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, _ = _make_bridge(
        tmp_path,
        run=_run_record(state="SUCCEEDED"),
        attempt_repository=attempts,
        scheduler=scheduler,
    )
    delivered = []

    def submit_with_duplicate_early_outcome(job: AnalysisJob) -> SubmitReceipt:
        outcome = _queue_timeout_outcome(job)
        delivered.append(outcome)
        boundary(outcome)
        boundary(outcome)
        return _accepted_receipt(job)

    scheduler.submit_value = submit_with_duplicate_early_outcome

    result = bridge.submit_reanalysis(
        ReanalysisRequest(RUN_ID, selected_start, selected_end)
    )
    boundary(delivered[0])

    assert result.status is BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL
    assert result.scheduler_accepted is True
    assert result.terminal_delivered_before_return is True
    assert len(scheduler.submitted) == 1
    assert (
        scheduler.submitted[0].snapshot.analysis_window_start_seconds
        == selected_start
    )
    assert (
        scheduler.submitted[0].snapshot.analysis_window_end_seconds
        == selected_end
    )
    assert len(attempts.queued_finished) == 1
    assert attempts.running_finished == []
    assert len(reports) == 1
    assert reports[0].attempt_kind is AttemptKind.REANALYSIS
    assert boundary._contexts == {}


@pytest.mark.parametrize(
    ("run", "code"),
    [
        (
            _run_record(config_snapshot_json='{"schema":"wrong"}'),
            "ANALYSIS_ENVELOPE_INVALID",
        ),
        (
            replace(_run_record(), calibration_snapshot_json="{"),
            "ANALYSIS_ENVELOPE_INVALID",
        ),
        (
            replace(_run_record(), ingress_kind="UNKNOWN"),
            "ANALYSIS_KIND_INVALID",
        ),
    ],
)
def test_pre_attempt_envelope_and_kind_matrix(
    tmp_path: Path,
    run: RunRecord,
    code: str,
) -> None:
    bridge, _, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        run=run,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.code == code
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


@pytest.mark.parametrize("clock_value", [RuntimeError("clock failed"), "bad-time"])
def test_pre_attempt_queue_timestamp_failure(
    tmp_path: Path,
    clock_value: object,
) -> None:
    def clock() -> str:
        if isinstance(clock_value, BaseException):
            raise clock_value
        return clock_value  # type: ignore[return-value]

    bridge, _, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        now_utc=clock,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.code == "ANALYSIS_QUEUE_TIMESTAMP_FAILED"
    assert result.analysis_attempt_id is None
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


def test_pre_attempt_wrong_exact_request_has_zero_side_effects(
    tmp_path: Path,
) -> None:
    bridge, _, scheduler, attempts, reports, _ = _make_bridge(tmp_path)

    with pytest.raises(BridgeInputError):
        bridge.submit_initial(object())

    assert bridge.run_repository.calls == []
    assert bridge.audio_repository.calls == []
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


def test_pre_attempt_invalid_request_fields_raise_typed_input_before_calls(
    tmp_path: Path,
) -> None:
    bridge, _, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    malformed = object.__new__(InitialAnalysisRequest)
    object.__setattr__(malformed, "run_id", "bad-run-id")
    object.__setattr__(malformed, "audio_reference", _reference(audio))

    with pytest.raises(BridgeInputError):
        bridge.submit_initial(malformed)

    assert bridge.run_repository.calls == []
    assert bridge.audio_repository.calls == []
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


def test_pre_attempt_job_constructor_failure_returns_allocated_attempt_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, _, scheduler, attempts, reports, audio = _make_bridge(tmp_path)

    def fail_job(*args, **kwargs):
        raise ValueError("job invalid")

    monkeypatch.setattr(
        bridge_module,
        "build_prevalidated_submission",
        fail_job,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.status is BridgeSubmissionStatus.REJECTED
    assert result.code == "ANALYSIS_JOB_INVALID"
    assert result.analysis_attempt_id is not None
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []


@pytest.mark.parametrize(
    ("create_value", "status", "code", "restart"),
    [
        (_rolled_back(), BridgeSubmissionStatus.REJECTED, "ATTEMPT_QUEUE_DB_FAILED", False),
        (_unknown(), BridgeSubmissionStatus.STATE_UNKNOWN, "ANALYSIS_DB_STATE_UNKNOWN", True),
        (_committed(False), BridgeSubmissionStatus.STATE_UNKNOWN, "ATTEMPT_QUEUE_RESULT_INVALID", True),
        (
            TransactionResult(
                state=TransactionState.COMMITTED,
                value=1,
            ),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ATTEMPT_QUEUE_RESULT_INVALID",
            True,
        ),
        (
            TransactionResult(
                state=TransactionState.ROLLED_BACK,
                value=True,
                reason="illegal",
            ),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ATTEMPT_QUEUE_RESULT_INVALID",
            True,
        ),
        (object(), BridgeSubmissionStatus.STATE_UNKNOWN, "ATTEMPT_QUEUE_RESULT_INVALID", True),
        (
            DatabaseUnavailableError(
                DatabaseLifecycleCode.DATABASE_CLOSED,
                "closed",
            ),
            BridgeSubmissionStatus.REJECTED,
            "DATABASE_CLOSED",
            True,
        ),
        (
            RuntimeError("create exploded"),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ATTEMPT_QUEUE_CALL_FAILED",
            True,
        ),
    ],
)
def test_pre_attempt_create_queued_closed_matrix(
    tmp_path: Path,
    create_value: object,
    status: BridgeSubmissionStatus,
    code: str,
    restart: bool,
) -> None:
    attempts = _AttemptRepository(create_result=create_value)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (result.status, result.code, result.restart_required) == (
        status,
        code,
        restart,
    )
    assert result.analysis_attempt_id is not None
    assert len(attempts.created) == 1
    assert attempts.created[0].attempt_id == result.analysis_attempt_id
    assert scheduler.submitted == []
    assert reports == []
    assert boundary._context_count == 0


def test_pre_attempt_repository_unknown_latch_blocks_later_repository_calls(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(create_result=_unknown())
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )

    first = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    second = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert first.status is BridgeSubmissionStatus.STATE_UNKNOWN
    assert (second.status, second.code, second.restart_required) == (
        BridgeSubmissionStatus.REJECTED,
        "ANALYSIS_DB_CLOSED",
        True,
    )
    assert len(attempts.created) == 1
    assert len(bridge.run_repository.calls) == 1
    assert scheduler.submitted == []


def test_submit_happens_only_after_exact_durable_queued_commit(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(create_result=_committed(True))

    def submit(job: AnalysisJob) -> SubmitReceipt:
        assert len(attempts.created) == 1
        assert attempts.created[0].attempt_id == job.analysis_attempt_id
        return _accepted_receipt(job)

    scheduler = _Scheduler(submit_value=submit)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.status is BridgeSubmissionStatus.ACCEPTED
    assert result.scheduler_accepted is True
    assert result.submit_receipt is not None
    assert scheduler.submitted[0] is boundary._contexts[result.analysis_attempt_id].job
    assert reports == []


@pytest.mark.parametrize(
    ("submit_status", "code"),
    [
        (SubmitStatus.INVALID_JOB, "ANALYSIS_INVALID_JOB"),
        (
            SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID,
            "ANALYSIS_DUPLICATE_ATTEMPT_ID",
        ),
        (SubmitStatus.QUEUE_FULL, "ANALYSIS_QUEUE_FULL"),
        (
            SubmitStatus.NOT_ACCEPTING,
            "ANALYSIS_SCHEDULER_NOT_ACCEPTING",
        ),
    ],
)
def test_submit_rejection_receipt_terminalizes_queued_once(
    tmp_path: Path,
    submit_status: SubmitStatus,
    code: str,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))
    scheduler = _Scheduler(
        submit_value=lambda job: _rejected_receipt(job, submit_status)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (result.status, result.code, result.restart_required) == (
        BridgeSubmissionStatus.REJECTED,
        code,
        False,
    )
    assert result.terminal_delivered_before_return is True
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].code == code
    assert result.submit_receipt is not None
    assert len(attempts.queued_finished) == 1
    assert attempts.running_finished == []
    assert boundary._context_count == 0
    assert len(reports) == 1


@pytest.mark.parametrize(
    ("submit_value", "code"),
    [
        (RuntimeError("submit exploded"), "ANALYSIS_SCHEDULER_SUBMIT_FAILED"),
        (object(), "SCHEDULER_RECEIPT_INVALID"),
    ],
)
def test_submit_exception_or_invalid_receipt_writes_one_queued_terminal(
    tmp_path: Path,
    submit_value: object,
    code: str,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=_Scheduler(submit_value=submit_value),
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (result.status, result.code) == (
        BridgeSubmissionStatus.REJECTED,
        code,
    )
    assert result.terminal_delivered_before_return is True
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert len(attempts.queued_finished) == 1
    assert boundary._context_count == 0
    assert len(reports) == 1


def test_submit_receipt_identity_mismatch_never_rewrites_or_resubmits(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))

    def mismatched(job: AnalysisJob) -> SubmitReceipt:
        return SubmitReceipt(
            status=SubmitStatus.ACCEPTED,
            run_id=OTHER_RUN_ID,
            analysis_attempt_id=job.analysis_attempt_id,
            submission_sequence=1,
            error=None,
        )

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=_Scheduler(submit_value=mismatched),
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.code == "SCHEDULER_RECEIPT_IDENTITY_MISMATCH"
    assert len(scheduler.submitted) == 1
    assert len(attempts.created) == 1
    assert len(attempts.queued_finished) == 1
    assert boundary._context_count == 0
    assert len(reports) == 1


def test_submit_exact_receipt_with_illegal_coherence_is_invalid(
    tmp_path: Path,
) -> None:
    illegal = object.__new__(SubmitReceipt)
    object.__setattr__(illegal, "status", SubmitStatus.ACCEPTED)
    object.__setattr__(illegal, "run_id", RUN_ID)
    object.__setattr__(illegal, "analysis_attempt_id", ATTEMPT_ID)
    object.__setattr__(illegal, "submission_sequence", None)
    object.__setattr__(illegal, "error", None)

    def return_illegal(job: AnalysisJob) -> SubmitReceipt:
        object.__setattr__(illegal, "analysis_attempt_id", job.analysis_attempt_id)
        return illegal

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=_Scheduler(submit_value=return_illegal),
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.status is BridgeSubmissionStatus.REJECTED
    assert result.code == "SCHEDULER_RECEIPT_INVALID"
    assert len(attempts.queued_finished) == 1
    assert boundary._context_count == 0


@pytest.mark.parametrize(
    ("failure_mode", "code"),
    [
        ("exception", "ANALYSIS_SCHEDULER_SUBMIT_FAILED"),
        ("malformed", "SCHEDULER_RECEIPT_INVALID"),
        ("mismatched", "SCHEDULER_RECEIPT_IDENTITY_MISMATCH"),
        ("rejected", "ANALYSIS_QUEUE_FULL"),
    ],
)
def test_direct_submit_failure_after_durable_start_uses_running_writer(
    tmp_path: Path,
    failure_mode: str,
    code: str,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))

    def submit_after_start(job: AnalysisJob):
        boundary.before_audio_load(job)
        if failure_mode == "exception":
            raise RuntimeError("submit failed after durable start")
        if failure_mode == "malformed":
            return object()
        if failure_mode == "rejected":
            return _rejected_receipt(job, SubmitStatus.QUEUE_FULL)
        return SubmitReceipt(
            status=SubmitStatus.ACCEPTED,
            run_id=OTHER_RUN_ID,
            analysis_attempt_id=job.analysis_attempt_id,
            submission_sequence=1,
            error=None,
        )

    scheduler = _Scheduler(submit_value=submit_after_start)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (
        result.status,
        result.authority,
        result.code,
        result.terminal_delivered_before_return,
    ) == (
        BridgeSubmissionStatus.REJECTED,
        BridgeAuthorityStatus.ACQUIRED,
        code,
        True,
    )
    assert len(attempts.marked) == 1
    assert attempts.queued_finished == []
    assert len(attempts.running_finished) == 1
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].code == code
    assert boundary._context_count == 0


@pytest.mark.parametrize(
    ("failure_mode", "code"),
    [
        ("exception", "ANALYSIS_SCHEDULER_SUBMIT_FAILED"),
        ("malformed", "SCHEDULER_RECEIPT_INVALID"),
        ("mismatched", "SCHEDULER_RECEIPT_IDENTITY_MISMATCH"),
        ("rejected", "ANALYSIS_QUEUE_FULL"),
    ],
)
def test_direct_submit_failure_waits_for_inflight_start_and_blocks_entrants(
    tmp_path: Path,
    failure_mode: str,
    code: str,
) -> None:
    class BlockingMarkAttempts(_AttemptRepository):
        def __init__(self) -> None:
            super().__init__(terminal_result=_committed(True))
            self.mark_entered = threading.Event()
            self.release_mark = threading.Event()

        def mark_running(self, attempt_id: str, now: str):
            self.marked.append((attempt_id, now))
            self.mark_entered.set()
            assert self.release_mark.wait(5), "mark_running was not released"
            return self.mark_result

    attempts = BlockingMarkAttempts()
    start_threads: list[threading.Thread] = []
    start_errors: list[BaseException] = []

    def run_start(job: AnalysisJob) -> None:
        try:
            boundary.before_audio_load(job)
        except BaseException as exc:
            start_errors.append(exc)

    def submit_during_start(job: AnalysisJob):
        start_thread = threading.Thread(target=run_start, args=(job,))
        start_threads.append(start_thread)
        start_thread.start()
        assert attempts.mark_entered.wait(5), "start did not enter mark_running"
        if failure_mode == "exception":
            raise RuntimeError("submit failed during durable start")
        if failure_mode == "malformed":
            return object()
        if failure_mode == "rejected":
            return _rejected_receipt(job, SubmitStatus.QUEUE_FULL)
        return SubmitReceipt(
            status=SubmitStatus.ACCEPTED,
            run_id=OTHER_RUN_ID,
            analysis_attempt_id=job.analysis_attempt_id,
            submission_sequence=1,
            error=None,
        )

    scheduler = _Scheduler(submit_value=submit_during_start)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )
    direct_claim_waiting = threading.Event()
    original_wait = boundary._submit_condition.wait

    def observe_direct_claim_wait(timeout: float | None = None) -> bool:
        direct_claim_waiting.set()
        return original_wait(timeout)

    boundary._submit_condition.wait = observe_direct_claim_wait
    returned: list[object] = []
    submit_thread = threading.Thread(
        target=lambda: returned.append(
            bridge.submit_initial(
                InitialAnalysisRequest(RUN_ID, _reference(audio))
            )
        )
    )
    submit_thread.start()
    claimed = False
    try:
        assert attempts.mark_entered.wait(5), "start did not block"
        claimed = direct_claim_waiting.wait(0.25)
        if claimed:
            job = scheduler.submitted[0]
            canceled = bridge.cancel(
                BridgeCancelRequest(
                    run_id=RUN_ID,
                    analysis_attempt_id=job.analysis_attempt_id,
                    origin=AnalysisCancelOrigin.USER_STOP,
                    reason="stop after direct terminal claim",
                )
            )
            assert canceled.status is BridgeCancelStatus.REJECTED
            assert canceled.code == "ANALYSIS_CANCEL_CONTEXT_INVALID"
            assert scheduler.canceled == []
            with pytest.raises(AnalysisStartGateError) as raised:
                boundary.before_audio_load(job)
            assert (
                raised.value.failure.code
                is AnalysisStartGateCode.ATTEMPT_START_CONTEXT_INVALID
            )
            boundary(_queue_timeout_outcome(job))
            assert reports == []
    finally:
        attempts.release_mark.set()
        submit_thread.join(5)
        for start_thread in start_threads:
            start_thread.join(5)

    assert claimed, "direct submit failure did not wait after terminal claim"
    assert not submit_thread.is_alive()
    assert all(not thread.is_alive() for thread in start_threads)
    assert start_errors == []
    result = returned[0]
    assert result.code == code
    assert attempts.queued_finished == []
    assert len(attempts.running_finished) == 1
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert boundary._context_count == 0


@pytest.mark.parametrize(
    ("terminal_result", "status", "code", "restart"),
    [
        (
            _committed(False),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "LATE_OUTCOME_IGNORED",
            True,
        ),
        (
            _rolled_back(),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ATTEMPT_TERMINAL_DB_FAILED",
            True,
        ),
        (
            _unknown(),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ANALYSIS_DB_STATE_UNKNOWN",
            True,
        ),
        (
            object(),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ATTEMPT_TERMINAL_RESULT_INVALID",
            True,
        ),
        (
            RuntimeError("terminal exploded"),
            BridgeSubmissionStatus.STATE_UNKNOWN,
            "ATTEMPT_TERMINAL_CALL_FAILED",
            True,
        ),
    ],
)
def test_submit_rejection_terminal_result_matrix(
    tmp_path: Path,
    terminal_result: object,
    status: BridgeSubmissionStatus,
    code: str,
    restart: bool,
) -> None:
    attempts = _AttemptRepository(terminal_result=terminal_result)
    scheduler = _Scheduler(
        submit_value=lambda job: _rejected_receipt(
            job,
            SubmitStatus.NOT_ACCEPTING,
        )
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (result.status, result.code, result.restart_required) == (
        status,
        code,
        restart,
    )
    assert result.terminal_delivered_before_return is True
    assert len(attempts.queued_finished) == 1
    assert boundary._context_count == 0
    assert len(reports) == 1


@pytest.mark.parametrize(
    (
        "terminal_result",
        "report_status",
        "code",
        "quarantine_calls",
    ),
    [
        (
            DatabaseUnavailableError(
                DatabaseLifecycleCode.DATABASE_CLOSED,
                "closed",
            ),
            BridgeTerminalStatus.DB_UNKNOWN,
            "ANALYSIS_DB_STATE_UNKNOWN",
            0,
        ),
        (
            DatabaseUnavailableError(
                DatabaseLifecycleCode.DATABASE_NOT_READY,
                "not ready",
            ),
            BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
            "DATABASE_NOT_READY",
            0,
        ),
        (
            TransactionResult(
                state=TransactionState.ROLLED_BACK,
                value=True,
                reason="illegal value",
            ),
            BridgeTerminalStatus.DB_UNKNOWN,
            "ATTEMPT_TERMINAL_RESULT_INVALID",
            1,
        ),
        (
            TransactionResult(
                state=TransactionState.COMMITTED,
                value=1,
            ),
            BridgeTerminalStatus.DB_UNKNOWN,
            "ATTEMPT_TERMINAL_RESULT_INVALID",
            1,
        ),
    ],
)
def test_submit_rejection_terminalization_unavailable_and_invalid_result_proofs(
    tmp_path: Path,
    terminal_result: object,
    report_status: BridgeTerminalStatus,
    code: str,
    quarantine_calls: int,
) -> None:
    runtime = _Runtime()
    attempts = _AttemptRepository(terminal_result=terminal_result)
    scheduler = _Scheduler(
        submit_value=lambda job: _rejected_receipt(
            job,
            SubmitStatus.NOT_ACCEPTING,
        )
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (
        result.status,
        result.code,
        result.restart_required,
        result.scheduler_accepted,
    ) == (
        BridgeSubmissionStatus.STATE_UNKNOWN,
        code,
        True,
        False,
    )
    assert (
        reports[0].status,
        reports[0].code,
        reports[0].restart_required,
        reports[0].persistence_result,
    ) == (report_status, code, True, None)
    assert len(attempts.created) == 1
    assert len(scheduler.submitted) == 1
    assert len(attempts.queued_finished) == 1
    assert attempts.running_finished == []
    assert runtime.quarantine_calls == quarantine_calls
    assert boundary._context_count == 0
    assert len(reports) == 1


def test_early_matching_outcome_is_held_terminalized_and_callback_once(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    def submit_with_early_outcome(job: AnalysisJob) -> SubmitReceipt:
        outcome = _queue_timeout_outcome(job)
        boundary(outcome)
        boundary(outcome)
        return _accepted_receipt(job)

    scheduler.submit_value = submit_with_early_outcome
    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.status is BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL
    assert result.scheduler_accepted is True
    assert result.terminal_delivered_before_return is True
    assert reports[0].code == "ANALYSIS_QUEUE_TIMEOUT"
    assert len(reports) == 1
    assert len(attempts.queued_finished) == 1
    assert boundary._context_count == 0


def test_early_success_late_ignored_allows_absent_report_summary(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(False))
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )

    def submit_with_early_success(job: AnalysisJob) -> SubmitReceipt:
        boundary.before_audio_load(job)
        boundary(_succeeded_outcome(job))
        return _accepted_receipt(job)

    scheduler.submit_value = submit_with_early_success

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert (
        result.status,
        result.authority,
        result.scheduler_accepted,
        result.terminal_delivered_before_return,
        result.restart_required,
        result.code,
        result.reason,
    ) == (
        BridgeSubmissionStatus.STATE_UNKNOWN,
        BridgeAuthorityStatus.ACQUIRED,
        True,
        True,
        True,
        "LATE_OUTCOME_IGNORED",
        None,
    )
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.LATE_IGNORED
    assert reports[0].summary is None
    assert len(attempts.running_finished) == 1
    assert attempts.queued_finished == []
    assert boundary._context_count == 0


def test_outcome_between_receipt_reconciliation_and_return_is_early(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )
    receipt_reconciled = threading.Event()
    release_return_decision = threading.Event()
    original_reconcile = boundary._reconcile_accepted

    def reconcile_then_pause(job: AnalysisJob):
        early = original_reconcile(job)
        assert early is None
        receipt_reconciled.set()
        assert release_return_decision.wait(5), "return decision was not released"
        return early

    boundary._reconcile_accepted = reconcile_then_pause
    returned: list[object] = []
    submit_thread = threading.Thread(
        target=lambda: returned.append(
            bridge.submit_initial(
                InitialAnalysisRequest(RUN_ID, _reference(audio))
            )
        )
    )
    submit_thread.start()
    try:
        assert receipt_reconciled.wait(5), "receipt was not reconciled"
        boundary(_queue_timeout_outcome(scheduler.submitted[0]))
        assert reports == []
    finally:
        release_return_decision.set()
        submit_thread.join(5)

    assert not submit_thread.is_alive()
    result = returned[0]
    assert (
        result.status,
        result.authority,
        result.scheduler_accepted,
        result.terminal_delivered_before_return,
        result.restart_required,
        result.code,
    ) == (
        BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL,
        BridgeAuthorityStatus.ACQUIRED,
        True,
        True,
        False,
        None,
    )
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].code == "ANALYSIS_QUEUE_TIMEOUT"
    assert boundary._context_count == 0


def test_outcome_during_accepted_return_handoff_is_early(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))
    scheduler = _Scheduler()
    sink_entered = threading.Event()
    release_sink = threading.Event()
    sink_completed = threading.Event()
    reports = []

    def blocking_terminal_report_sink(report: object) -> None:
        reports.append(report)
        sink_entered.set()
        assert release_sink.wait(5), "terminal sink was not released"
        sink_completed.set()

    bridge, boundary, scheduler, attempts, _, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
        terminal_report_sink=blocking_terminal_report_sink,
    )
    return_linearized = threading.Event()
    release_handoff = threading.Event()
    original_linearize = boundary._linearize_accepted_return

    def linearize_then_pause(job: AnalysisJob):
        early = original_linearize(job)
        assert early is None
        return_linearized.set()
        assert release_handoff.wait(5), "accepted return handoff was not released"
        return early

    boundary._linearize_accepted_return = linearize_then_pause
    returned: list[object] = []
    submit_returned = threading.Event()
    outcome_errors: list[BaseException] = []

    def submit() -> None:
        returned.append(
            bridge.submit_initial(
                InitialAnalysisRequest(RUN_ID, _reference(audio))
            )
        )
        submit_returned.set()

    def deliver_outcome() -> None:
        try:
            boundary(_queue_timeout_outcome(scheduler.submitted[0]))
        except BaseException as exc:
            outcome_errors.append(exc)

    submit_thread = threading.Thread(target=submit)
    outcome_thread = threading.Thread(target=deliver_outcome)
    submit_thread.start()
    sink_entered_before_handoff = False
    submit_returned_before_sink_release = False
    try:
        assert return_linearized.wait(5), "accepted return was not linearized"
        outcome_thread.start()
        sink_entered_before_handoff = sink_entered.wait(0.2)
        release_handoff.set()
        assert sink_entered.wait(5), "terminal sink was not entered"
        submit_returned_before_sink_release = submit_returned.wait(0.2)
    finally:
        release_handoff.set()
        release_sink.set()
        submit_thread.join(5)
        if outcome_thread.ident is not None:
            outcome_thread.join(5)

    assert sink_entered_before_handoff is False
    assert submit_returned_before_sink_release is False
    assert not submit_thread.is_alive()
    assert not outcome_thread.is_alive()
    assert outcome_errors == []
    assert sink_completed.is_set()
    result = returned[0]
    assert (
        result.status,
        result.authority,
        result.scheduler_accepted,
        result.terminal_delivered_before_return,
        result.restart_required,
        result.code,
    ) == (
        BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL,
        BridgeAuthorityStatus.ACQUIRED,
        True,
        True,
        False,
        None,
    )
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].code == "ANALYSIS_QUEUE_TIMEOUT"
    assert boundary._context_count == 0


def test_outcome_after_final_accepted_linearization_remains_async(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(terminal_result=_committed(True))
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
    )
    raw_submit = bridge.submit_initial.func
    return_linearized = threading.Event()
    release_submit = threading.Event()
    submit_returned = threading.Event()
    sink_completed = threading.Event()
    sink_reports: list[object] = []
    sink_thread_ids: list[int] = []
    outcome_thread_ids: list[int] = []
    stored_sinks: list[object] = []

    def direct_terminal_sink(report: object) -> None:
        sink_reports.append(report)
        sink_thread_ids.append(threading.get_ident())
        sink_completed.set()

    original_complete = boundary._complete_accepted_return

    def complete_then_pause(job: AnalysisJob):
        early = original_complete(job)
        assert early is None
        with boundary._lock:
            context = boundary._contexts[job.analysis_attempt_id]
            assert context.submission_return_linearized is True
            stored_sinks.append(context.terminal_sink)
        return_linearized.set()
        assert release_submit.wait(5), "public submit return was not released"
        return early

    boundary._complete_accepted_return = complete_then_pause
    returned: list[object] = []
    submit_errors: list[BaseException] = []
    outcome_errors: list[BaseException] = []

    def submit() -> None:
        try:
            returned.append(
                raw_submit(
                    InitialAnalysisRequest(RUN_ID, _reference(audio)),
                    direct_terminal_sink,
                )
            )
            submit_returned.set()
        except BaseException as exc:
            submit_errors.append(exc)

    outcome = None

    def deliver_outcome() -> None:
        try:
            outcome_thread_ids.append(threading.get_ident())
            assert outcome is not None
            boundary(outcome)
        except BaseException as exc:
            outcome_errors.append(exc)

    submit_thread = threading.Thread(target=submit)
    outcome_thread = threading.Thread(target=deliver_outcome)
    submit_thread.start()
    try:
        assert return_linearized.wait(5), "accepted return was not linearized"
        outcome = _queue_timeout_outcome(scheduler.submitted[0])
        outcome_thread.start()
        assert sink_completed.wait(5), "terminal sink did not complete"
        outcome_thread.join(5)

        assert not outcome_thread.is_alive()
        assert submit_returned.wait(0.2) is False
        assert boundary._context_count == 0
        assert len(attempts.queued_finished) == 1
    finally:
        release_submit.set()
        submit_thread.join(5)
        if outcome_thread.ident is not None:
            outcome_thread.join(5)

    assert not submit_thread.is_alive()
    assert not outcome_thread.is_alive()
    assert submit_errors == []
    assert outcome_errors == []
    assert submit_returned.is_set()
    assert stored_sinks == [direct_terminal_sink]
    assert sink_thread_ids == outcome_thread_ids
    result = returned[0]
    assert (
        result.status,
        result.authority,
        result.scheduler_accepted,
        result.terminal_delivered_before_return,
        result.restart_required,
        result.code,
    ) == (
        BridgeSubmissionStatus.ACCEPTED,
        BridgeAuthorityStatus.ACQUIRED,
        True,
        False,
        False,
        None,
    )
    assert result.submit_receipt is not None
    assert result.submit_receipt.status is SubmitStatus.ACCEPTED
    assert not hasattr(result, "ack")
    assert reports == []
    assert len(sink_reports) == 1
    assert sink_reports[0].status is BridgeTerminalStatus.COMMITTED
    assert sink_reports[0].code == "ANALYSIS_QUEUE_TIMEOUT"
    assert len(scheduler.submitted) == 1
    assert len(attempts.queued_finished) == 1
    assert attempts.running_finished == []
    assert boundary._context_count == 0

    durable_write = attempts.queued_finished[0]
    delivered_report = sink_reports[0]
    boundary(outcome)

    assert attempts.queued_finished == [durable_write]
    assert sink_reports == [delivered_report]
    assert len(scheduler.submitted) == 1
    assert boundary._context_count == 0


def test_early_matching_outcome_callback_exception_is_isolated(
    tmp_path: Path,
) -> None:
    terminal_result = _committed(True)
    attempts = _AttemptRepository(terminal_result=terminal_result)
    scheduler = _Scheduler()
    callback_attempts = []

    def raising_terminal_report_sink(report: object) -> None:
        callback_attempts.append(report)
        raise RuntimeError("coordinator callback failed")

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        scheduler=scheduler,
        terminal_report_sink=raising_terminal_report_sink,
    )
    held_outcomes = []

    def submit_with_early_outcome(job: AnalysisJob) -> SubmitReceipt:
        outcome = _queue_timeout_outcome(job)
        held_outcomes.append(outcome)
        boundary(outcome)
        return _accepted_receipt(job)

    scheduler.submit_value = submit_with_early_outcome

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.status is BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL
    assert result.scheduler_accepted is True
    assert result.restart_required is False
    assert result.code is None
    assert result.terminal_delivered_before_return is True
    assert reports[0] is callback_attempts[0]
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].code == "ANALYSIS_QUEUE_TIMEOUT"
    assert reports[0].persistence_result is terminal_result
    assert len(attempts.queued_finished) == 1
    durable_write = attempts.queued_finished[0]
    assert attempts.running_finished == []
    assert len(callback_attempts) == 1
    assert boundary._context_count == 0

    boundary(held_outcomes[0])

    assert attempts.queued_finished == [durable_write]
    assert callback_attempts == reports
    assert boundary._context_count == 0


def test_before_audio_load_marks_running_before_return_and_sets_durable_started(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(mark_result=_committed(True))
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]

    boundary.before_audio_load(job)

    assert attempts.marked == [(job.analysis_attempt_id, NOW)]
    assert boundary._contexts[result.analysis_attempt_id].durable_started is True
    assert reports == []


def test_start_gate_requires_same_active_job_object(tmp_path: Path) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    copied_job = replace(scheduler.submitted[0])

    with pytest.raises(AnalysisStartGateError) as raised:
        boundary.before_audio_load(copied_job)

    assert raised.value.failure.code is AnalysisStartGateCode.ATTEMPT_START_CONTEXT_INVALID
    assert attempts.marked == []
    assert reports == []


@pytest.mark.parametrize(
    ("mark_result", "code"),
    [
        (_committed(False), AnalysisStartGateCode.ATTEMPT_START_NOT_QUEUED),
        (_rolled_back(), AnalysisStartGateCode.ATTEMPT_START_DB_FAILED),
        (_unknown(), AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN),
        (object(), AnalysisStartGateCode.ATTEMPT_START_RESULT_INVALID),
        (RuntimeError("mark exploded"), AnalysisStartGateCode.ATTEMPT_START_GATE_EXCEPTION),
    ],
)
def test_start_gate_mark_running_closed_matrix(
    tmp_path: Path,
    mark_result: object,
    code: AnalysisStartGateCode,
) -> None:
    attempts = _AttemptRepository(mark_result=mark_result)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]

    with pytest.raises(AnalysisStartGateError) as raised:
        boundary.before_audio_load(job)

    assert raised.value.failure.code is code
    assert boundary._contexts[result.analysis_attempt_id].durable_started is False
    assert len(attempts.marked) == 1
    assert reports == []


@pytest.mark.parametrize("clock_value", [RuntimeError("clock"), "not-utc"])
def test_start_gate_timestamp_failure_calls_mark_running_zero_times(
    tmp_path: Path,
    clock_value: object,
) -> None:
    calls = 0

    def now_utc() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return NOW
        if isinstance(clock_value, BaseException):
            raise clock_value
        return clock_value  # type: ignore[return-value]

    attempts = _AttemptRepository()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        now_utc=now_utc,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))

    with pytest.raises(AnalysisStartGateError) as raised:
        boundary.before_audio_load(scheduler.submitted[0])

    assert raised.value.failure.code is AnalysisStartGateCode.ATTEMPT_START_TIMESTAMP_FAILED
    assert calls == 2
    assert attempts.marked == []
    assert reports == []


def test_start_gate_unknown_latches_no_write_for_later_outcome(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(mark_result=_unknown())
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]

    with pytest.raises(AnalysisStartGateError):
        boundary.before_audio_load(job)
    boundary(_queue_timeout_outcome(job))

    assert attempts.queued_finished == []
    assert attempts.running_finished == []
    assert reports[0].status is BridgeTerminalStatus.DB_UNKNOWN
    assert reports[0].code == "ANALYSIS_DB_STATE_UNKNOWN"


def test_start_gate_quarantine_latch_blocks_other_context_mark_running(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(mark_result=_committed(True))
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    first_job, second_job = scheduler.submitted
    attempts.mark_result = _unknown()
    with pytest.raises(AnalysisStartGateError):
        boundary.before_audio_load(first_job)
    attempts.mark_result = _committed(True)

    with pytest.raises(AnalysisStartGateError) as raised:
        boundary.before_audio_load(second_job)

    assert raised.value.failure.code is AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN
    assert len(attempts.marked) == 1


def test_start_gate_malformed_result_terminal_report_requires_restart(
    tmp_path: Path,
) -> None:
    attempts = _AttemptRepository(mark_result=object())
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]

    with pytest.raises(AnalysisStartGateError):
        boundary.before_audio_load(job)
    boundary(
        _start_gate_failure_outcome(
            job,
            AnalysisStartGateCode.ATTEMPT_START_RESULT_INVALID,
        )
    )

    assert len(attempts.queued_finished) == 1
    assert reports[0].status is BridgeTerminalStatus.DB_UNKNOWN
    assert reports[0].code == "ATTEMPT_START_RESULT_INVALID"
    assert reports[0].restart_required is True


@pytest.mark.parametrize(
    "code",
    [
        AnalysisStartGateCode.ATTEMPT_START_NOT_QUEUED,
        AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
        AnalysisStartGateCode.ATTEMPT_START_CONTEXT_INVALID,
        AnalysisStartGateCode.ATTEMPT_START_TIMESTAMP_FAILED,
        AnalysisStartGateCode.ATTEMPT_START_GATE_EXCEPTION,
    ],
)
def test_start_gate_non_unknown_terminal_outcomes_write_queued_failed_once(
    tmp_path: Path,
    code: AnalysisStartGateCode,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]
    assert boundary._contexts[submitted.analysis_attempt_id].durable_started is False
    outcome = _start_gate_failure_outcome(job, code)

    boundary(outcome)
    boundary(outcome)

    assert len(attempts.queued_finished) == 1
    assert attempts.running_finished == []
    persisted = attempts.queued_finished[0][1]
    assert (persisted.state, persisted.code) == (AttemptState.FAILED, code.value)
    assert len(reports) == 1
    assert (
        reports[0].status,
        reports[0].attempt_state,
        reports[0].code,
        reports[0].restart_required,
    ) == (
        BridgeTerminalStatus.COMMITTED,
        AttemptState.FAILED,
        code.value,
        False,
    )
    assert boundary._context_count == 0


def test_scheduler_cancel_wins_while_mark_running_fails_uses_queued_terminal(
    tmp_path: Path,
) -> None:
    class BlockingMarkAttempts(_AttemptRepository):
        def __init__(self) -> None:
            super().__init__(mark_result=_rolled_back("durable start failed"))
            self.mark_barrier = threading.Barrier(2)
            self.release_mark = threading.Event()

        def mark_running(self, attempt_id: str, now: str):
            self.marked.append((attempt_id, now))
            self.mark_barrier.wait(timeout=5)
            assert self.release_mark.wait(5), "mark_running was not released"
            return self.mark_result

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF")
    audio = _audio_record(audio_path)
    runtime = _Runtime()
    attempts = BlockingMarkAttempts()
    reports = []
    callback_completed = threading.Event()

    def terminal_report_sink(report: object) -> None:
        reports.append(report)
        callback_completed.set()

    boundary = AnalysisPersistenceBoundary(
        runtime=runtime,
        attempt_repository=attempts,
        now_utc=lambda: NOW,
    )
    loader = RecordingAudioLoader()
    executor = RecordingExecutor()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(worker_count=1),
        audio_loader=loader,
        analysis_executor=executor,
        start_gate=boundary,
        outcome_sink=boundary,
        clock=ManualClock(1.0),
    )
    bridge = RunPersistenceSchedulerBridge(
        runtime=runtime,
        run_repository=_RunRepository(_run_record()),
        audio_repository=_AudioRepository(audio),
        attempt_repository=attempts,
        boundary=boundary,
        scheduler=scheduler,
        now_utc=lambda: NOW,
    )
    try:
        submitted = bridge.submit_initial(
            InitialAnalysisRequest(RUN_ID, _reference(audio)),
            terminal_report_sink,
        )
        assert submitted.status is BridgeSubmissionStatus.ACCEPTED
        attempts.mark_barrier.wait(timeout=5)

        canceled = bridge.cancel(
            BridgeCancelRequest(
                run_id=RUN_ID,
                analysis_attempt_id=submitted.analysis_attempt_id,
                origin=AnalysisCancelOrigin.USER_STOP,
                reason="operator stop",
            )
        )

        assert canceled.status is BridgeCancelStatus.CANCEL_REQUESTED
        assert boundary._contexts[
            submitted.analysis_attempt_id
        ].durable_started is False
        attempts.release_mark.set()
        assert callback_completed.wait(5), "terminal callback did not complete"

        assert attempts.marked == [(submitted.analysis_attempt_id, NOW)]
        assert len(attempts.queued_finished) == 1
        assert attempts.running_finished == []
        persisted = attempts.queued_finished[0][1]
        assert (persisted.state, persisted.code) == (
            AttemptState.CANCELED,
            "USER_CANCELED",
        )
        assert loader.calls == []
        assert executor.calls == []
        assert len(reports) == 1
        assert (
            reports[0].status,
            reports[0].attempt_state,
            reports[0].code,
            reports[0].restart_required,
        ) == (
            BridgeTerminalStatus.COMMITTED,
            AttemptState.CANCELED,
            "USER_CANCELED",
            False,
        )
        assert boundary._context_count == 0
    finally:
        attempts.release_mark.set()
        assert scheduler.shutdown(5.0).status is ShutdownStatus.CLOSED


@pytest.mark.parametrize("winner", ["pending_cancel", "start"])
def test_start_vs_pending_cancel_race_has_one_durable_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    race_ready = threading.Barrier(2)
    first_path_finished = threading.Event()
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]

    def cancel_with_outcome(
        run_id: str,
        attempt_id: str,
        origin: AnalysisCancelOrigin,
        reason: str,
    ) -> CancelReceipt:
        boundary(_canceled_outcome(job, origin, started=winner == "start"))
        return _cancel_receipt(
            run_id,
            attempt_id,
            (
                CancelStatus.CANCELED_PENDING
                if winner == "pending_cancel"
                else CancelStatus.CANCEL_REQUESTED_RUNNING
            ),
        )

    scheduler.cancel_value = cancel_with_outcome
    start_errors: list[AnalysisStartGateError] = []
    cancel_results: list[object] = []

    def start_path() -> None:
        race_ready.wait()
        if winner == "pending_cancel":
            assert first_path_finished.wait(5)
        try:
            boundary.before_audio_load(job)
        except AnalysisStartGateError as error:
            start_errors.append(error)
        finally:
            if winner == "start":
                first_path_finished.set()

    def cancel_path() -> None:
        race_ready.wait()
        if winner == "start":
            assert first_path_finished.wait(5)
        cancel_results.append(
            bridge.cancel(
                BridgeCancelRequest(
                    run_id=RUN_ID,
                    analysis_attempt_id=submitted.analysis_attempt_id,
                    origin=AnalysisCancelOrigin.USER_STOP,
                    reason="race cancel",
                )
            )
        )
        if winner == "pending_cancel":
            first_path_finished.set()

    start_thread = threading.Thread(target=start_path)
    cancel_thread = threading.Thread(target=cancel_path)
    start_thread.start()
    cancel_thread.start()
    start_thread.join(5)
    cancel_thread.join(5)
    assert not start_thread.is_alive()
    assert not cancel_thread.is_alive()

    assert len(cancel_results) == 1
    assert cancel_results[0].status is BridgeCancelStatus.CANCEL_REQUESTED
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert boundary._context_count == 0
    if winner == "pending_cancel":
        assert len(start_errors) == 1
        assert attempts.marked == []
        assert len(attempts.queued_finished) == 1
        assert attempts.running_finished == []
    else:
        assert start_errors == []
        assert len(attempts.marked) == 1
        assert attempts.queued_finished == []
        assert len(attempts.running_finished) == 1


@pytest.mark.parametrize("winner", ["queue_timeout", "start"])
def test_start_vs_queue_timeout_race_has_one_durable_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    race_ready = threading.Barrier(2)
    first_path_finished = threading.Event()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    loader_calls = 0
    start_errors: list[AnalysisStartGateError] = []

    def start_path() -> None:
        nonlocal loader_calls
        race_ready.wait()
        if winner == "queue_timeout":
            assert first_path_finished.wait(5)
        try:
            boundary.before_audio_load(job)
        except AnalysisStartGateError as error:
            start_errors.append(error)
        else:
            loader_calls += 1
            boundary(_succeeded_outcome(job))
        finally:
            if winner == "start":
                first_path_finished.set()

    def timeout_path() -> None:
        race_ready.wait()
        if winner == "start":
            assert first_path_finished.wait(5)
            return
        boundary(_queue_timeout_outcome(job))
        first_path_finished.set()

    start_thread = threading.Thread(target=start_path)
    timeout_thread = threading.Thread(target=timeout_path)
    start_thread.start()
    timeout_thread.start()
    start_thread.join(5)
    timeout_thread.join(5)
    assert not start_thread.is_alive()
    assert not timeout_thread.is_alive()

    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert boundary._context_count == 0
    if winner == "queue_timeout":
        assert len(start_errors) == 1
        assert attempts.marked == []
        assert loader_calls == 0
        assert len(attempts.queued_finished) == 1
        assert attempts.running_finished == []
    else:
        assert start_errors == []
        assert len(attempts.marked) == 1
        assert loader_calls == 1
        assert attempts.queued_finished == []
        assert len(attempts.running_finished) == 1


def test_outcome_success_decodes_canonical_payload_and_writes_running(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)

    boundary(_succeeded_outcome(job))

    assert attempts.queued_finished == []
    assert len(attempts.running_finished) == 1
    persisted = attempts.running_finished[0][1]
    assert persisted.state is AttemptState.SUCCEEDED
    assert persisted.judgment is Judgment.PASS
    assert persisted.result_payload_json == (
        '{"judgment":"PASS","result":{"score":7}}'
    )
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].code == "ANALYSIS_SUCCEEDED"


def test_outcome_invalid_success_payload_becomes_failed(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    invalid_payload = FrozenResultObject(items=(("not-the-root", 1),))

    boundary(_succeeded_outcome(job, payload=invalid_payload))

    persisted = attempts.running_finished[0][1]
    assert persisted.state is AttemptState.FAILED
    assert persisted.code == "ANALYSIS_RESULT_PAYLOAD_INVALID"
    assert reports[0].code == "ANALYSIS_RESULT_PAYLOAD_INVALID"


def test_outcome_no_result_writes_running_exact_code(tmp_path: Path) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    outcome = AnalysisOutcome(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        kind=job.kind,
        status=AnalysisStatus.NO_RESULT,
        audio_reference_id=job.audio.reference_id,
        result_payload=None,
        error=AnalysisError(
            category=AnalysisErrorCategory.ANALYSIS_NO_RESULT,
            stage=AnalysisStage.EXECUTE,
            summary="nothing detected",
        ),
        submitted_monotonic=1.0,
        started_monotonic=2.0,
        finished_monotonic=3.0,
        queue_wait_ms=1000,
        execution_ms=1000,
        cancel_origin=None,
    )

    boundary(outcome)

    assert attempts.running_finished[0][1].state is AttemptState.NO_RESULT
    assert attempts.running_finished[0][1].code == "ANALYSIS_NO_RESULT"
    assert reports[0].code == "ANALYSIS_NO_RESULT"


@pytest.mark.parametrize(
    ("category", "stage", "code"),
    [
        (
            AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
            AnalysisStage.AUDIO_LOAD,
            "AUDIO_REFERENCE_UNREADABLE",
        ),
        (
            AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
            AnalysisStage.AUDIO_LOAD,
            "AUDIO_REFERENCE_CHANGED",
        ),
        (
            AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
            AnalysisStage.AUDIO_LOAD,
            "AUDIO_FORMAT_MISMATCH",
        ),
        (
            AnalysisErrorCategory.AUDIO_LOAD_FAILED,
            AnalysisStage.AUDIO_LOAD,
            "AUDIO_LOAD_FAILED",
        ),
        (
            AnalysisErrorCategory.ANALYSIS_TIMEOUT,
            AnalysisStage.EXECUTE,
            "ANALYSIS_TIMEOUT",
        ),
        (
            AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            AnalysisStage.EXECUTE,
            "ANALYSIS_EXECUTION_FAILED",
        ),
    ],
)
def test_outcome_running_failure_category_matrix(
    tmp_path: Path,
    category: AnalysisErrorCategory,
    stage: AnalysisStage,
    code: str,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)

    boundary(_failed_outcome(job, category, stage))

    assert attempts.queued_finished == []
    assert attempts.running_finished[0][1].code == code
    assert reports[0].code == code


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        (AnalysisCancelOrigin.USER_STOP, "USER_CANCELED"),
        (
            AnalysisCancelOrigin.SCHEDULER_SHUTDOWN,
            "ANALYSIS_SCHEDULER_SHUTDOWN",
        ),
    ],
)
def test_outcome_pending_cancel_uses_durable_fact_not_started_monotonic(
    tmp_path: Path,
    origin: AnalysisCancelOrigin,
    code: str,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]

    boundary(_canceled_outcome(job, origin, started=True))

    assert attempts.running_finished == []
    assert attempts.queued_finished[0][1].code == code
    assert reports[0].code == code


def test_scheduler_canceled_outcome_origin_overrides_facade_return_order(
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    scheduler.cancel_value = lambda run_id, attempt_id, origin, reason: (
        _cancel_receipt(run_id, attempt_id, CancelStatus.CANCELED_PENDING)
    )
    bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="stop",
        )
    )
    job = scheduler.submitted[0]

    boundary(
        _canceled_outcome(
            job,
            AnalysisCancelOrigin.SCHEDULER_SHUTDOWN,
            started=False,
        )
    )

    assert len(attempts.queued_finished) == 1
    assert (
        attempts.queued_finished[0][1].code
        == "ANALYSIS_SCHEDULER_SHUTDOWN"
    )
    assert attempts.running_finished == []
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert boundary._context_count == 0


def test_concurrent_running_cancels_use_scheduler_first_cancel_origin(
    tmp_path: Path,
) -> None:
    first_cancel_entered = threading.Event()
    release_first_return = threading.Event()
    scheduler_lock = threading.Lock()
    scheduler_origin: AnalysisCancelOrigin | None = None

    def reverse_return_cancel(
        run_id: str,
        attempt_id: str,
        origin: AnalysisCancelOrigin,
        reason: str,
    ) -> CancelReceipt:
        nonlocal scheduler_origin
        with scheduler_lock:
            if scheduler_origin is None:
                scheduler_origin = origin
                first_cancel_entered.set()
                is_first = True
            else:
                is_first = False
        if is_first:
            assert release_first_return.wait(5)
        return _cancel_receipt(
            run_id,
            attempt_id,
            CancelStatus.CANCEL_REQUESTED_RUNNING,
        )

    scheduler = _Scheduler(cancel_value=reverse_return_cancel)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    results: dict[str, object] = {}

    def request_cancel(
        key: str,
        origin: AnalysisCancelOrigin,
    ) -> None:
        results[key] = bridge.cancel(
            BridgeCancelRequest(
                run_id=RUN_ID,
                analysis_attempt_id=submitted.analysis_attempt_id,
                origin=origin,
                reason=key,
            )
        )

    first = threading.Thread(
        target=request_cancel,
        args=("first", AnalysisCancelOrigin.USER_STOP),
    )
    second = threading.Thread(
        target=request_cancel,
        args=("second", AnalysisCancelOrigin.SCHEDULER_SHUTDOWN),
    )
    first.start()
    assert first_cancel_entered.wait(5)
    second.start()
    second.join(5)
    assert not second.is_alive()
    release_first_return.set()
    first.join(5)
    assert not first.is_alive()
    assert scheduler_origin is AnalysisCancelOrigin.USER_STOP

    boundary(
        _canceled_outcome(
            job,
            AnalysisCancelOrigin.USER_STOP,
            started=True,
        )
    )

    assert {
        result.status for result in results.values()
    } == {BridgeCancelStatus.CANCEL_REQUESTED}
    assert attempts.queued_finished == []
    assert len(attempts.running_finished) == 1
    assert attempts.running_finished[0][1].code == "USER_CANCELED"
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert boundary._context_count == 0


def test_scheduler_shutdown_racing_explicit_cancel_uses_shutdown_origin(
    tmp_path: Path,
) -> None:
    shutdown_linearized = threading.Event()
    release_shutdown_outcome = threading.Event()
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)

    def shutdown_with_authoritative_outcome(
        timeout_seconds: float | None,
    ) -> ShutdownReport:
        shutdown_linearized.set()
        assert release_shutdown_outcome.wait(5)
        boundary(
            _canceled_outcome(
                job,
                AnalysisCancelOrigin.SCHEDULER_SHUTDOWN,
                started=True,
            )
        )
        return _shutdown_report(ShutdownStatus.CLOSED)

    scheduler.shutdown_value = shutdown_with_authoritative_outcome
    scheduler.cancel_value = lambda run_id, attempt_id, origin, reason: (
        _cancel_receipt(
            run_id,
            attempt_id,
            CancelStatus.CANCEL_REQUESTED_RUNNING,
        )
    )
    shutdown_results: list[object] = []
    shutdown_thread = threading.Thread(
        target=lambda: shutdown_results.append(
            bridge.stop_and_join_scheduler(5.0)
        )
    )
    shutdown_thread.start()
    assert shutdown_linearized.wait(5)

    explicit_result = bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="operator stop",
        )
    )
    release_shutdown_outcome.set()
    shutdown_thread.join(5)
    assert not shutdown_thread.is_alive()

    assert explicit_result.status is BridgeCancelStatus.CANCEL_REQUESTED
    assert len(shutdown_results) == 1
    assert attempts.queued_finished == []
    assert len(attempts.running_finished) == 1
    assert (
        attempts.running_finished[0][1].code
        == "ANALYSIS_SCHEDULER_SHUTDOWN"
    )
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert boundary._context_count == 0


def test_outcome_result_eviction_reanalysis_maps_exact_code(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, reports, _ = _make_bridge(
        tmp_path,
        run=_run_record(state="SUCCEEDED"),
    )
    bridge.submit_reanalysis(ReanalysisRequest(RUN_ID, 0.25, 0.75))
    job = scheduler.submitted[0]

    boundary(
        _canceled_outcome(
            job,
            AnalysisCancelOrigin.RESULT_EVICTION,
            started=False,
        )
    )

    assert attempts.queued_finished[0][1].code == "REANALYSIS_EVICTED"
    assert reports[0].attempt_kind is AttemptKind.REANALYSIS


@pytest.mark.parametrize(
    ("receipt_status", "bridge_status"),
    [
        (CancelStatus.CANCELED_PENDING, BridgeCancelStatus.CANCEL_REQUESTED),
        (
            CancelStatus.CANCEL_REQUESTED_RUNNING,
            BridgeCancelStatus.CANCEL_REQUESTED,
        ),
        (CancelStatus.ALREADY_TERMINAL, BridgeCancelStatus.ALREADY_TERMINAL),
        (CancelStatus.NOT_FOUND, BridgeCancelStatus.NOT_FOUND),
        (CancelStatus.RUN_ID_MISMATCH, BridgeCancelStatus.RUN_ID_MISMATCH),
    ],
)
def test_cancel_receipt_matrix_never_writes_persistence_directly(
    tmp_path: Path,
    receipt_status: CancelStatus,
    bridge_status: BridgeCancelStatus,
) -> None:
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    scheduler.cancel_value = lambda run_id, attempt_id, origin, reason: (
        _cancel_receipt(run_id, attempt_id, receipt_status)
    )

    result = bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="operator stop",
        )
    )

    assert result.status is bridge_status
    assert len(scheduler.canceled) == 1
    assert attempts.queued_finished == []
    assert attempts.running_finished == []
    assert reports == []


def test_cancel_rejects_initial_result_eviction_before_scheduler(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    result = bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.RESULT_EVICTION,
            reason="evict",
        )
    )

    assert result.status is BridgeCancelStatus.REJECTED
    assert result.code == "RESULT_EVICTION_REQUIRES_REANALYSIS"
    assert scheduler.canceled == []


@pytest.mark.parametrize(
    ("cancel_value", "code"),
    [
        (RuntimeError("cancel exploded"), "SCHEDULER_CANCEL_FAILED"),
        (object(), "SCHEDULER_CANCEL_RECEIPT_INVALID"),
    ],
)
def test_cancel_exception_or_malformed_receipt_is_rejected_without_db(
    tmp_path: Path,
    cancel_value: object,
    code: str,
) -> None:
    scheduler = _Scheduler(cancel_value=cancel_value)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    result = bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="stop",
        )
    )

    assert (result.status, result.code) == (BridgeCancelStatus.REJECTED, code)
    assert attempts.queued_finished == []
    assert attempts.running_finished == []
    assert reports == []


def test_cancel_exact_receipt_with_illegal_coherence_is_rejected(
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    illegal = object.__new__(CancelReceipt)
    object.__setattr__(illegal, "status", CancelStatus.CANCEL_REQUESTED_RUNNING)
    object.__setattr__(illegal, "run_id", RUN_ID)
    object.__setattr__(illegal, "analysis_attempt_id", submitted.analysis_attempt_id)
    object.__setattr__(illegal, "drain_pending", False)
    object.__setattr__(illegal, "error", None)
    scheduler.cancel_value = illegal

    result = bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="stop",
        )
    )

    assert result.status is BridgeCancelStatus.REJECTED
    assert result.code == "SCHEDULER_CANCEL_RECEIPT_INVALID"
    assert attempts.queued_finished == []
    assert attempts.running_finished == []


def test_cancel_receipt_identity_mismatch_is_rejected_without_retry(
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    submitted = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )
    scheduler.cancel_value = _cancel_receipt(
        OTHER_RUN_ID,
        submitted.analysis_attempt_id,
        CancelStatus.CANCELED_PENDING,
    )

    result = bridge.cancel(
        BridgeCancelRequest(
            run_id=RUN_ID,
            analysis_attempt_id=submitted.analysis_attempt_id,
            origin=AnalysisCancelOrigin.USER_STOP,
            reason="stop",
        )
    )

    assert result.status is BridgeCancelStatus.REJECTED
    assert result.code == "SCHEDULER_CANCEL_RECEIPT_IDENTITY_MISMATCH"
    assert len(scheduler.canceled) == 1
    assert attempts.queued_finished == []


@pytest.mark.parametrize(
    ("terminal_result", "report_status", "code"),
    [
        (_committed(False), BridgeTerminalStatus.LATE_IGNORED, "LATE_OUTCOME_IGNORED"),
        (_rolled_back(), BridgeTerminalStatus.DB_FAILED, "ATTEMPT_TERMINAL_DB_FAILED"),
        (_unknown(), BridgeTerminalStatus.DB_UNKNOWN, "ANALYSIS_DB_STATE_UNKNOWN"),
        (object(), BridgeTerminalStatus.DB_UNKNOWN, "ATTEMPT_TERMINAL_RESULT_INVALID"),
        (
            RuntimeError("terminal exploded"),
            BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
            "ATTEMPT_TERMINAL_CALL_FAILED",
        ),
    ],
)
def test_terminal_result_matrix_always_callbacks_once(
    tmp_path: Path,
    terminal_result: object,
    report_status: BridgeTerminalStatus,
    code: str,
) -> None:
    attempts = _AttemptRepository(terminal_result=terminal_result)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]

    boundary(_queue_timeout_outcome(job))
    boundary(_queue_timeout_outcome(job))

    assert len(reports) == 1
    assert (reports[0].status, reports[0].code) == (report_status, code)
    assert len(attempts.queued_finished) == 1


def test_outcome_callback_exception_does_not_change_durable_terminal_result(
    tmp_path: Path,
) -> None:
    terminal_result = _committed(True)
    attempts = _AttemptRepository(terminal_result=terminal_result)
    callback_attempts = []

    def raising_terminal_report_sink(report: object) -> None:
        callback_attempts.append(report)
        raise RuntimeError("coordinator callback failed")

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
        terminal_report_sink=raising_terminal_report_sink,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    outcome = _queue_timeout_outcome(scheduler.submitted[0])

    boundary(outcome)

    assert len(attempts.queued_finished) == 1
    durable_write = attempts.queued_finished[0]
    persisted_outcome = durable_write[1]
    assert persisted_outcome.state is AttemptState.FAILED
    assert persisted_outcome.code == "ANALYSIS_QUEUE_TIMEOUT"
    assert attempts.running_finished == []
    assert len(reports) == 1
    report = reports[0]
    assert report is callback_attempts[0]
    assert report.status is BridgeTerminalStatus.COMMITTED
    assert report.code == "ANALYSIS_QUEUE_TIMEOUT"
    assert report.persistence_result is terminal_result
    assert len(callback_attempts) == 1
    assert boundary._context_count == 0

    boundary(outcome)

    assert attempts.queued_finished == [durable_write]
    assert reports == [report]
    assert callback_attempts == [report]
    assert boundary._context_count == 0


@pytest.mark.parametrize(
    "terminal_result",
    [
        TransactionResult(
            state=TransactionState.ROLLED_BACK,
            value=True,
            reason="illegal value",
        ),
        TransactionResult(
            state=TransactionState.COMMITTED,
            value=1,
        ),
    ],
)
def test_outcome_invalid_exact_terminal_results_quarantine_and_callback_once(
    tmp_path: Path,
    terminal_result: TransactionResult[object],
) -> None:
    runtime = _Runtime()
    attempts = _AttemptRepository(terminal_result=terminal_result)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    outcome = _queue_timeout_outcome(scheduler.submitted[0])

    boundary(outcome)
    boundary(outcome)

    assert len(attempts.queued_finished) == 1
    assert attempts.running_finished == []
    assert runtime.quarantine_calls == 1
    assert len(reports) == 1
    assert (
        reports[0].status,
        reports[0].code,
        reports[0].restart_required,
        reports[0].persistence_result,
    ) == (
        BridgeTerminalStatus.DB_UNKNOWN,
        "ATTEMPT_TERMINAL_RESULT_INVALID",
        True,
        None,
    )
    assert boundary._context_count == 0


def test_outcome_runtime_already_closed_callbacks_without_terminal_write(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    outcome = _queue_timeout_outcome(scheduler.submitted[0])
    runtime._ready = False
    runtime._closed = True

    boundary(outcome)
    boundary(outcome)

    assert attempts.queued_finished == []
    assert attempts.running_finished == []
    assert runtime.quarantine_calls == 0
    assert len(reports) == 1
    assert (
        reports[0].status,
        reports[0].code,
        reports[0].restart_required,
        reports[0].terminal_at,
        reports[0].persistence_result,
    ) == (
        BridgeTerminalStatus.DB_UNKNOWN,
        "ANALYSIS_DB_STATE_UNKNOWN",
        True,
        None,
        None,
    )
    assert boundary._context_count == 0


@pytest.mark.parametrize(
    ("terminal_failure", "status", "code"),
    [
        (
            DatabaseUnavailableError(
                DatabaseLifecycleCode.DATABASE_CLOSED,
                "closed",
            ),
            BridgeTerminalStatus.DB_UNKNOWN,
            "ANALYSIS_DB_STATE_UNKNOWN",
        ),
        (
            DatabaseUnavailableError(
                DatabaseLifecycleCode.DATABASE_NOT_READY,
                "not ready",
            ),
            BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
            "DATABASE_NOT_READY",
        ),
    ],
)
def test_terminal_database_unavailable_matrix_callbacks_once(
    tmp_path: Path,
    terminal_failure: DatabaseUnavailableError,
    status: BridgeTerminalStatus,
    code: str,
) -> None:
    attempts = _AttemptRepository(terminal_result=terminal_failure)
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))

    boundary(_queue_timeout_outcome(scheduler.submitted[0]))

    assert len(attempts.queued_finished) == 1
    assert len(reports) == 1
    assert (reports[0].status, reports[0].code) == (status, code)


@pytest.mark.parametrize("clock_value", [RuntimeError("clock"), "bad-utc"])
def test_terminal_timestamp_failure_writes_zero_and_callbacks_once(
    tmp_path: Path,
    clock_value: object,
) -> None:
    calls = 0

    def clock() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return NOW
        if isinstance(clock_value, BaseException):
            raise clock_value
        return clock_value  # type: ignore[return-value]

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        now_utc=clock,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))

    boundary(_queue_timeout_outcome(scheduler.submitted[0]))

    assert attempts.queued_finished == []
    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.TERMINAL_WRITE_FAILED
    assert reports[0].code == "ANALYSIS_TERMINAL_TIMESTAMP_FAILED"


def test_unsupported_scheduler_outcome_callbacks_without_db_write(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]

    boundary(_succeeded_outcome(job))

    assert attempts.queued_finished == []
    assert attempts.running_finished == []
    assert reports[0].status is BridgeTerminalStatus.UNSUPPORTED
    assert reports[0].code == "UNSUPPORTED_SCHEDULER_OUTCOME"


@pytest.mark.parametrize(
    "malformed_case",
    [
        "queue_wait_timeout",
        "start_gate",
        "audio_load",
        "execute",
        "no_result",
        "canceled",
    ],
)
def test_malformed_outcome_coherence_is_unsupported_without_side_effects(
    tmp_path: Path,
    malformed_case: str,
) -> None:
    clock_calls = 0

    def clock() -> str:
        nonlocal clock_calls
        clock_calls += 1
        return NOW

    runtime = _Runtime()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        now_utc=clock,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    if malformed_case in ("audio_load", "execute", "no_result"):
        boundary.before_audio_load(job)

    if malformed_case == "queue_wait_timeout":
        outcome = _queue_timeout_outcome(job)
        assert outcome.error is not None
        object.__setattr__(outcome.error, "stage", AnalysisStage.AUDIO_LOAD)
    elif malformed_case == "start_gate":
        outcome = _start_gate_failure_outcome(
            job,
            AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN,
        )
        object.__setattr__(outcome, "execution_ms", 1)
    elif malformed_case == "audio_load":
        outcome = _failed_outcome(
            job,
            AnalysisErrorCategory.AUDIO_LOAD_FAILED,
            AnalysisStage.AUDIO_LOAD,
        )
        object.__setattr__(outcome, "execution_ms", 1)
    elif malformed_case == "execute":
        outcome = _failed_outcome(
            job,
            AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            AnalysisStage.EXECUTE,
        )
        object.__setattr__(outcome, "execution_ms", None)
    elif malformed_case == "no_result":
        outcome = AnalysisOutcome(
            run_id=job.run_id,
            analysis_attempt_id=job.analysis_attempt_id,
            kind=job.kind,
            status=AnalysisStatus.NO_RESULT,
            audio_reference_id=job.audio.reference_id,
            result_payload=None,
            error=AnalysisError(
                category=AnalysisErrorCategory.ANALYSIS_NO_RESULT,
                stage=AnalysisStage.EXECUTE,
                summary="no result",
            ),
            submitted_monotonic=1.0,
            started_monotonic=2.0,
            finished_monotonic=3.0,
            queue_wait_ms=1000,
            execution_ms=1000,
            cancel_origin=None,
        )
        assert outcome.error is not None
        object.__setattr__(outcome.error, "stage", AnalysisStage.CANCEL)
    else:
        outcome = _canceled_outcome(
            job,
            AnalysisCancelOrigin.USER_STOP,
            started=False,
        )
        assert outcome.error is not None
        object.__setattr__(outcome.error, "stage", AnalysisStage.EXECUTE)

    clock_calls_before_outcome = clock_calls
    boundary(outcome)
    boundary(outcome)

    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.UNSUPPORTED
    assert reports[0].code == "UNSUPPORTED_SCHEDULER_OUTCOME"
    assert attempts.queued_finished == []
    assert attempts.running_finished == []
    assert clock_calls == clock_calls_before_outcome
    assert runtime.quarantine_calls == 0
    assert job.analysis_attempt_id not in boundary._contexts


def test_malformed_terminal_result_quarantines_once_and_blocks_later_write(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    attempts = _AttemptRepository(terminal_result=object())
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        attempt_repository=attempts,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    first_job = scheduler.submitted[0]
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    second_job = scheduler.submitted[1]

    boundary(_queue_timeout_outcome(first_job))
    boundary(_queue_timeout_outcome(second_job))

    assert runtime.quarantine_calls == 1
    assert len(attempts.queued_finished) == 1
    assert [report.status for report in reports] == [
        BridgeTerminalStatus.DB_UNKNOWN,
        BridgeTerminalStatus.DB_UNKNOWN,
    ]


@pytest.mark.parametrize(
    ("scheduler_status", "bridge_status", "code"),
    [
        (
            ShutdownStatus.CLOSED,
            BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED,
            None,
        ),
        (
            ShutdownStatus.IN_PROGRESS,
            BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS,
            "SCHEDULER_IN_PROGRESS",
        ),
        (
            ShutdownStatus.SHUTDOWN_INCOMPLETE,
            BridgeSchedulerShutdownStatus.SCHEDULER_INCOMPLETE,
            "SCHEDULER_INCOMPLETE",
        ),
    ],
)
def test_shutdown_phase_a_maps_once_and_never_closes_database(
    tmp_path: Path,
    scheduler_status: ShutdownStatus,
    bridge_status: BridgeSchedulerShutdownStatus,
    code: str | None,
) -> None:
    runtime = _Runtime()
    scheduler = _Scheduler(shutdown_value=_shutdown_report(scheduler_status))
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        scheduler=scheduler,
    )

    result = bridge.stop_and_join_scheduler(2.5)

    assert (result.status, result.code) == (bridge_status, code)
    assert result.scheduler_report is scheduler.shutdown_value
    assert len(scheduler.shutdown_calls) == 1
    assert 0.0 < scheduler.shutdown_calls[0] <= 2.5
    assert runtime.close_calls == 0


@pytest.mark.parametrize(
    ("durable_started", "writer_name"),
    [
        (False, "queued_writer"),
        (True, "running_writer"),
    ],
)
def test_shutdown_outcome_handoff_completes_before_successful_phase_b_close(
    tmp_path: Path,
    durable_started: bool,
    writer_name: str,
) -> None:
    order: list[str] = []
    writer_completed = threading.Event()
    callback_completed = threading.Event()

    class OrderedAttempts(_AttemptRepository):
        def finish_queued_and_project(self, attempt_id: str, outcome, now: str):
            result = super().finish_queued_and_project(attempt_id, outcome, now)
            order.append("queued_writer")
            writer_completed.set()
            return result

        def finish_running_and_project(self, attempt_id: str, outcome, now: str):
            result = super().finish_running_and_project(attempt_id, outcome, now)
            order.append("running_writer")
            writer_completed.set()
            return result

    class OrderedRuntime(_Runtime):
        def close_after_upstream_stopped(self) -> None:
            assert writer_completed.is_set()
            assert callback_completed.is_set()
            order.append("database_close")
            super().close_after_upstream_stopped()

    def terminal_report_sink(report: object) -> None:
        assert writer_completed.is_set()
        order.append("terminal_callback")
        callback_completed.set()

    runtime = OrderedRuntime()
    attempts = OrderedAttempts()
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        attempt_repository=attempts,
        scheduler=scheduler,
        terminal_report_sink=terminal_report_sink,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    job = scheduler.submitted[0]
    if durable_started:
        boundary.before_audio_load(job)
    order.clear()

    def shutdown_with_terminal_handoff(timeout_seconds: float | None):
        order.append("scheduler_shutdown")
        boundary(
            _canceled_outcome(
                job,
                AnalysisCancelOrigin.SCHEDULER_SHUTDOWN,
                started=durable_started,
            )
        )
        assert callback_completed.is_set()
        order.append("scheduler_closed")
        return _shutdown_report(ShutdownStatus.CLOSED)

    scheduler.shutdown_value = shutdown_with_terminal_handoff

    phase_a = bridge.stop_and_join_scheduler(3.0)

    assert phase_a.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
    assert runtime.close_calls == 0
    assert writer_completed.is_set()
    assert callback_completed.is_set()
    assert boundary._context_count == 0
    assert len(reports) == 1
    assert (
        reports[0].status,
        reports[0].attempt_state,
        reports[0].code,
        reports[0].restart_required,
    ) == (
        BridgeTerminalStatus.COMMITTED,
        AttemptState.CANCELED,
        "ANALYSIS_SCHEDULER_SHUTDOWN",
        False,
    )

    phase_b = bridge.close_persistence_after_upstream_stopped()

    assert (
        phase_b.status,
        phase_b.database_closed,
        phase_b.restart_required,
        phase_b.code,
    ) == (
        BridgeDatabaseCloseStatus.DATABASE_CLOSED,
        True,
        False,
        None,
    )
    assert order == [
        "scheduler_shutdown",
        writer_name,
        "terminal_callback",
        "scheduler_closed",
        "database_close",
    ]
    assert runtime.close_calls == 1
    assert len(attempts.queued_finished) == (0 if durable_started else 1)
    assert len(attempts.running_finished) == (1 if durable_started else 0)


def test_shutdown_phase_b_without_cached_closed_report_is_uncached_blocker(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
    )

    first = bridge.close_persistence_after_upstream_stopped()
    second = bridge.close_persistence_after_upstream_stopped()

    assert first is not second
    assert first.status is BridgeDatabaseCloseStatus.BLOCKED
    assert first.code == "SCHEDULER_NOT_CLOSED"
    assert runtime.close_calls == 0


def test_shutdown_phase_a_can_be_invoked_again_after_in_progress(
    tmp_path: Path,
) -> None:
    reports = iter(
        (
            _shutdown_report(ShutdownStatus.IN_PROGRESS),
            _shutdown_report(ShutdownStatus.CLOSED),
        )
    )
    scheduler = _Scheduler(shutdown_value=lambda timeout: next(reports))
    runtime = _Runtime()
    bridge, boundary, scheduler, attempts, callbacks, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        scheduler=scheduler,
    )

    first = bridge.stop_and_join_scheduler(0.5)
    second = bridge.stop_and_join_scheduler(1.0)
    closed = bridge.close_persistence_after_upstream_stopped()

    assert first.status is BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS
    assert second.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
    assert len(scheduler.shutdown_calls) == 2
    assert 0.0 < scheduler.shutdown_calls[0] <= 0.5
    assert 0.0 < scheduler.shutdown_calls[1] <= 1.0
    assert closed.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED


def test_shutdown_phase_b_closes_once_and_identity_caches_without_probes(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        scheduler=scheduler,
    )
    bridge.stop_and_join_scheduler()

    first = bridge.close_persistence_after_upstream_stopped()
    probes_after_first = runtime.closed_probes
    second = bridge.close_persistence_after_upstream_stopped()

    assert first.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED
    assert second is first
    assert runtime.close_calls == 1
    assert runtime.closed_probes == probes_after_first


def test_shutdown_phase_b_quarantine_latch_is_independent_cached_result(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    attempts = _AttemptRepository(create_result=_unknown())
    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        attempt_repository=attempts,
        scheduler=scheduler,
    )
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))
    bridge.stop_and_join_scheduler()

    first = bridge.close_persistence_after_upstream_stopped()
    second = bridge.close_persistence_after_upstream_stopped()

    assert first is second
    assert first.status is BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED
    assert first.code == "DATABASE_QUARANTINED"
    assert first.restart_required is True
    assert runtime.close_calls == 0


def test_shutdown_phase_b_classifies_unexpected_external_close(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(closed=True)
    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        scheduler=scheduler,
    )
    bridge.stop_and_join_scheduler()

    result = bridge.close_persistence_after_upstream_stopped()

    assert result.status is BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED
    assert result.code == "DATABASE_CLOSED_UNEXPECTED"
    assert result.restart_required is True
    assert runtime.close_calls == 0


def test_phase_a_stop_wins_before_submit_with_zero_side_effects(
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )

    stopped = bridge.stop_and_join_scheduler(1.0)
    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert stopped.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
    assert (
        result.status,
        result.code,
        result.analysis_attempt_id,
        result.scheduler_accepted,
        result.terminal_delivered_before_return,
    ) == (
        BridgeSubmissionStatus.REJECTED,
        "ANALYSIS_ADMISSION_CLOSED",
        None,
        False,
        False,
    )
    assert attempts.created == []
    assert scheduler.submitted == []
    assert reports == []
    assert boundary._context_count == 0


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        True,
        False,
        1,
        0.0,
        -1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        "1.0",
    ],
)
def test_phase_a_invalid_timeout_preserves_submission_admission(
    tmp_path: Path,
    timeout_seconds: object,
) -> None:
    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )

    with pytest.raises(ValueError):
        bridge.stop_and_join_scheduler(timeout_seconds)  # type: ignore[arg-type]

    result = bridge.submit_initial(
        InitialAnalysisRequest(RUN_ID, _reference(audio))
    )

    assert result.status is BridgeSubmissionStatus.ACCEPTED
    assert scheduler.shutdown_calls == []
    assert len(attempts.created) == 1
    assert boundary._submission_admission_open is True


def test_phase_a_none_uses_finite_default_budget_for_blocked_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight_entered = threading.Event()
    release_preflight = threading.Event()

    class BlockingRunRepository(_RunRepository):
        def get(self, run_id: str) -> RunRecord | None:
            preflight_entered.set()
            assert release_preflight.wait(5), "preflight was not released"
            return super().get(run_id)

    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    bridge.run_repository = BlockingRunRepository(_run_record())
    submit_thread = threading.Thread(
        target=lambda: bridge.submit_initial(
            InitialAnalysisRequest(RUN_ID, _reference(audio))
        )
    )
    submit_thread.start()
    assert preflight_entered.wait(5), "submit did not enter preflight"

    observed_waits: list[float | None] = []

    def bounded_wait(timeout: float | None = None) -> bool:
        observed_waits.append(timeout)
        if timeout is None:
            raise AssertionError("Phase A used an unbounded Condition wait")
        return False

    monotonic_samples = iter((100.0, 100.0, 105.0, 105.0))
    monkeypatch.setattr(boundary._submit_condition, "wait", bounded_wait)
    monkeypatch.setattr(
        bridge_module.time,
        "monotonic",
        lambda: next(monotonic_samples),
    )
    try:
        result = bridge.stop_and_join_scheduler()
    finally:
        release_preflight.set()
        submit_thread.join(5)

    assert not submit_thread.is_alive()
    assert observed_waits == [pytest.approx(5.0)]
    assert result.status is BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS
    assert result.code == "BRIDGE_SUBMIT_IN_FLIGHT"
    assert scheduler.shutdown_calls == []
    assert bridge.runtime.close_calls == 0


def test_phase_a_timeout_during_winning_submit_keeps_database_open(
    tmp_path: Path,
) -> None:
    preflight_entered = threading.Event()
    release_preflight = threading.Event()

    class BlockingRunRepository(_RunRepository):
        def get(self, run_id: str) -> RunRecord | None:
            preflight_entered.set()
            assert release_preflight.wait(5), "preflight was not released"
            return super().get(run_id)

    scheduler = _Scheduler(
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED)
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )
    bridge.run_repository = BlockingRunRepository(_run_record())
    returned: list[object] = []
    submit_thread = threading.Thread(
        target=lambda: returned.append(
            bridge.submit_initial(
                InitialAnalysisRequest(RUN_ID, _reference(audio))
            )
        )
    )
    submit_thread.start()
    assert preflight_entered.wait(5), "submit did not enter preflight"

    try:
        timed_out = bridge.stop_and_join_scheduler(0.01)

        assert timed_out.status is BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS
        assert timed_out.code == "BRIDGE_SUBMIT_IN_FLIGHT"
        assert scheduler.shutdown_calls == []
        assert bridge.runtime.close_calls == 0
    finally:
        release_preflight.set()
        submit_thread.join(5)
    assert not submit_thread.is_alive()
    assert returned[0].status is BridgeSubmissionStatus.ACCEPTED

    closed = bridge.stop_and_join_scheduler(1.0)
    assert closed.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
    assert len(scheduler.shutdown_calls) == 1
    assert 0.0 < scheduler.shutdown_calls[0] <= 1.0


@pytest.mark.parametrize("blocking_stage", ["queued", "scheduler"])
def test_phase_a_timeout_while_submit_is_queued_or_in_scheduler(
    tmp_path: Path,
    blocking_stage: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingQueueAttempts(_AttemptRepository):
        def create_queued(self, attempt):
            if blocking_stage == "queued":
                entered.set()
                assert release.wait(5), "QUEUED write was not released"
            return super().create_queued(attempt)

    def submit_value(job: AnalysisJob) -> SubmitReceipt:
        if blocking_stage == "scheduler":
            entered.set()
            assert release.wait(5), "Scheduler submit was not released"
        return _accepted_receipt(job)

    scheduler = _Scheduler(
        submit_value=submit_value,
        shutdown_value=_shutdown_report(ShutdownStatus.CLOSED),
    )
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        attempt_repository=BlockingQueueAttempts(),
        scheduler=scheduler,
    )
    returned: list[object] = []
    submit_thread = threading.Thread(
        target=lambda: returned.append(
            bridge.submit_initial(
                InitialAnalysisRequest(RUN_ID, _reference(audio))
            )
        )
    )
    submit_thread.start()
    try:
        assert entered.wait(5), f"submit did not enter {blocking_stage}"
        timed_out = bridge.stop_and_join_scheduler(0.01)

        assert timed_out.status is BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS
        assert timed_out.code == "BRIDGE_SUBMIT_IN_FLIGHT"
        assert scheduler.shutdown_calls == []
        assert bridge.runtime.close_calls == 0
    finally:
        release.set()
        submit_thread.join(5)

    assert not submit_thread.is_alive()
    assert returned[0].status is BridgeSubmissionStatus.ACCEPTED
    closed = bridge.stop_and_join_scheduler(1.0)
    assert closed.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED


def test_phase_a_waits_for_early_sink_and_receipt_reconciliation(
    tmp_path: Path,
) -> None:
    sink_entered = threading.Event()
    release_sink = threading.Event()
    shutdown_called = threading.Event()
    scheduler = _Scheduler()
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        scheduler=scheduler,
    )

    def early_submit(job: AnalysisJob) -> SubmitReceipt:
        boundary(_queue_timeout_outcome(job))
        return _accepted_receipt(job)

    scheduler.submit_value = early_submit
    scheduler.shutdown_value = lambda timeout: (
        shutdown_called.set() or _shutdown_report(ShutdownStatus.CLOSED)
    )
    delivered: list[object] = []

    def blocking_sink(report: object) -> None:
        context = boundary._contexts[scheduler.submitted[0].analysis_attempt_id]
        assert context.receipt_reconciled is True
        acquired = boundary._lock.acquire(blocking=False)
        assert acquired, "sink was invoked while the bridge lock was held"
        boundary._lock.release()
        delivered.append(report)
        sink_entered.set()
        assert release_sink.wait(5), "sink was not released"

    raw_submit = bridge.submit_initial.func
    submit_thread = threading.Thread(
        target=lambda: raw_submit(
            InitialAnalysisRequest(RUN_ID, _reference(audio)),
            blocking_sink,
        )
    )
    submit_thread.start()
    assert sink_entered.wait(5), "early sink was not entered"

    phase_a_results: list[object] = []
    phase_a_thread = threading.Thread(
        target=lambda: phase_a_results.append(
            bridge.stop_and_join_scheduler(5.0)
        )
    )
    phase_a_thread.start()
    assert not shutdown_called.wait(0.1)

    release_sink.set()
    submit_thread.join(5)
    phase_a_thread.join(5)
    assert not submit_thread.is_alive()
    assert not phase_a_thread.is_alive()
    assert len(delivered) == 1
    assert shutdown_called.is_set()
    assert phase_a_results[0].status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED


def test_boundary_uses_one_lock_and_one_condition_for_submit_admission(
    tmp_path: Path,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(tmp_path)

    assert boundary._submit_condition._lock is boundary._lock

    lock_observations: list[bool] = []
    original_get = bridge.run_repository.get

    def checked_get(run_id: str) -> RunRecord | None:
        acquired = boundary._lock.acquire(blocking=False)
        lock_observations.append(acquired)
        if acquired:
            boundary._lock.release()
        return original_get(run_id)

    bridge.run_repository.get = checked_get
    bridge.submit_initial(InitialAnalysisRequest(RUN_ID, _reference(audio)))

    assert lock_observations == [True]
