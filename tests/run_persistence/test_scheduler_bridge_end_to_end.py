from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sqlite3
import threading
from uuid import UUID, uuid4
import wave

import numpy as np
import pytest

from app.service.analysis.frames import ChannelJudgment, JudgmentResult
from app.service.analysis.production_executor import ProductionAnalysisExecutor
from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AnalysisErrorCategory,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AnalysisStage,
    FrozenResultObject,
)
from app.service.run_persistence import (
    AnalysisPersistenceBoundary,
    AttemptKind,
    AttemptRepository,
    AudioPublication,
    AudioRepository,
    AudioPersistenceService,
    BridgeAuthorityStatus,
    BridgeDatabaseCloseStatus,
    BridgeSchedulerShutdownStatus,
    BridgeSubmissionStatus,
    BridgeTerminalStatus,
    CapturedAudioSaveRequest,
    ImportedAudioSaveRequest,
    IngressKind,
    InitialAnalysisRequest,
    ProductionAudioStatus,
    ProductionRunAudioAdapter,
    ReanalysisRequest,
    RunInsert,
    RunPersistenceDatabase,
    RunPersistenceSchedulerBridge,
    RunRepository,
    TransactionState,
    TriggerSource,
    decode_analysis_result_payload,
    freeze_run_inputs,
    settle_startup,
)
from tests.analysis_scheduler.fakes import ManualClock
from tests.run_persistence.conftest import make_analysis_envelope
from tests.run_persistence.test_scheduler_bridge_lifecycle import (
    RUN_ID as MATRIX_RUN_ID,
    _AttemptRepository,
    _Runtime,
    _canceled_outcome,
    _failed_outcome,
    _make_bridge,
    _queue_timeout_outcome,
    _reference,
    _run_record,
    _succeeded_outcome,
)


ACCEPTED_AT = "2026-07-27T12:00:00.000000Z"
SAVED_AT = "2026-07-27T12:00:01.000000Z"
QUEUED_AT = "2026-07-27T12:00:02.000000Z"
RUNNING_AT = "2026-07-27T12:00:03.000000Z"
TERMINAL_AT = "2026-07-27T12:00:04.000000Z"
REANALYSIS_QUEUED_AT = "2026-07-27T12:00:05.000000Z"
REANALYSIS_RUNNING_AT = "2026-07-27T12:00:06.000000Z"
REANALYSIS_TERMINAL_AT = "2026-07-27T12:00:07.000000Z"


class _UtcSequence:
    def __init__(self, *values: str) -> None:
        self._values = deque(values)
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            assert self._values, "unexpected persistence timestamp request"
            return self._values.popleft()


class _DeterministicExecutor:
    def __init__(self) -> None:
        self.calls = []
        self._condition = threading.Condition()

    def execute(self, job, audio, cancel_token) -> AnalysisExecutionResult:
        with self._condition:
            self.calls.append((job, audio, cancel_token))
            self._condition.notify_all()
        return AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=FrozenResultObject(
                items=(
                    ("result", FrozenResultObject(items=(("score", 7),))),
                    ("judgment", "PASS"),
                )
            ),
        )

    def wait_for_calls(self, count: int) -> None:
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=5.0,
            ), f"executor did not receive {count} calls"


class _LegacyJudgment:
    def rejudge(
        self,
        _config,
        _samples,
        _sample_rate,
        _calibration,
        _audio_path,
        start_seconds,
        end_seconds,
        **_kwargs,
    ):
        return JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            channel_results=(
                ChannelJudgment(
                    channel_index=0,
                    spl_db=60.0,
                    spl_pass=True,
                    spl_average_db=59.0,
                    spl_average_pass=True,
                ),
            ),
            analysis_window=(start_seconds, end_seconds),
        )


class _RecordingProductionExecutor:
    def __init__(self) -> None:
        self._delegate = ProductionAnalysisExecutor(
            judgment_engine=_LegacyJudgment()
        )
        self.results = []

    def execute(self, job, audio, cancel_token):
        result = self._delegate.execute(job, audio, cancel_token)
        self.results.append(result)
        return result


def _ready_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    assert settle_startup(runtime, ACCEPTED_AT).ready
    return runtime


def _insert_active_run(
    runtime: RunPersistenceDatabase,
    run_id: str,
    ingress_kind: IngressKind,
    *,
    imported_source_path: Path | None = None,
    analysis_envelope=None,
) -> None:
    envelope = analysis_envelope or make_analysis_envelope(
        analysis={
            "window_start_seconds": 0.125,
            "window_end_seconds": 2.875,
            "algorithm_revision": "e2e-r1",
            "execution_timeout_seconds": 30.0,
            "queue_timeout_seconds": None,
            "toolchain": None,
        }
    )
    inserted = RunRepository(runtime).insert_active(
        RunInsert(
            run_id=run_id,
            ingress_kind=ingress_kind,
            trigger_source=(
                TriggerSource.MANUAL
                if ingress_kind is IngressKind.CAPTURED
                else TriggerSource.IMPORT
            ),
            config_id=7,
            hardware_id=9 if ingress_kind is IngressKind.CAPTURED else None,
            imported_source_identity=(
                f"sha256:{run_id}"
                if ingress_kind is IngressKind.IMPORTED
                else None
            ),
            imported_source_path=(
                str(imported_source_path)
                if ingress_kind is IngressKind.IMPORTED
                else None
            ),
            imported_expected_frame_count=(
                4 if ingress_kind is IngressKind.IMPORTED else None
            ),
            snapshots=freeze_run_inputs(
                envelope,
                {"hardware_id": 9}
                if ingress_kind is IngressKind.CAPTURED
                else None,
            ),
            accepted_at=ACCEPTED_AT,
        )
    )
    assert inserted.state is TransactionState.COMMITTED
    assert inserted.value is True


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(8_000)
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.writeframes(b"\x00\x00\x01\x00\x02\x00\x03\x00")


def _persist_audio(
    runtime: RunPersistenceDatabase,
    tmp_path: Path,
    run_id: str,
    ingress_kind: IngressKind,
):
    adapter = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(),
            AudioRepository(runtime),
            RunRepository(runtime),
        )
    )
    final_path = tmp_path / f"{run_id}.wav"
    if ingress_kind is IngressKind.CAPTURED:
        samples = np.array(
            [[0.0], [0.25], [-0.25], [0.5]],
            dtype=np.float32,
        )
        result = adapter.persist_captured(
            CapturedAudioSaveRequest(
                save_job_id="44444444-4444-4444-8444-444444444444",
                run_id=run_id,
                source_identity="managed-input:scheduler-bridge-end-to-end",
                content_revision=1,
                samples=samples,
                sample_rate_hz=8_000,
                channel_count=1,
                frame_count=4,
                final_path=str(final_path),
                now=SAVED_AT,
            )
        )
    else:
        source_path = tmp_path / f"{run_id}-source.wav"
        _write_wav(source_path)
        result = adapter.persist_imported(
            ImportedAudioSaveRequest(
                save_job_id="55555555-5555-4555-8555-555555555555",
                run_id=run_id,
                source_identity=f"sha256:{run_id}",
                content_revision=1,
                source_path=str(source_path),
                final_path=str(final_path),
                expected_sample_rate_hz=8_000,
                expected_channel_count=1,
                expected_frame_count=4,
                now=SAVED_AT,
            )
        )
    assert result.status is ProductionAudioStatus.PERSISTED
    assert result.reference is not None
    return adapter, result.reference


def _build_real_stack(
    runtime: RunPersistenceDatabase,
    loader,
    *,
    now_utc=None,
    executor=None,
    scheduler_wrapper_factory=None,
):
    attempts = AttemptRepository(runtime)
    now_utc = now_utc or _UtcSequence(
            QUEUED_AT,
            RUNNING_AT,
            TERMINAL_AT,
            REANALYSIS_QUEUED_AT,
            REANALYSIS_RUNNING_AT,
            REANALYSIS_TERMINAL_AT,
        )
    boundary = AnalysisPersistenceBoundary(
        runtime=runtime,
        attempt_repository=attempts,
        now_utc=now_utc,
    )
    executor = executor or _DeterministicExecutor()
    scheduler = AnalysisScheduler(
        config=AnalysisSchedulerConfig(worker_count=1),
        audio_loader=loader,
        analysis_executor=executor,
        start_gate=boundary,
        outcome_sink=boundary,
        clock=ManualClock(1.0),
    )
    scheduler_port = (
        scheduler_wrapper_factory(scheduler)
        if scheduler_wrapper_factory is not None
        else scheduler
    )
    bridge = RunPersistenceSchedulerBridge(
        runtime=runtime,
        run_repository=RunRepository(runtime),
        audio_repository=AudioRepository(runtime),
        attempt_repository=attempts,
        boundary=boundary,
        scheduler=scheduler_port,
        now_utc=now_utc,
    )
    return bridge, executor


def _submit_and_wait(submit):
    reports = []
    delivered = threading.Event()

    def sink(report) -> None:
        reports.append(report)
        delivered.set()

    result = submit(sink)
    assert result.authority is BridgeAuthorityStatus.ACQUIRED
    assert result.status in (
        BridgeSubmissionStatus.ACCEPTED,
        BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL,
    )
    assert result.analysis_attempt_id is not None
    assert delivered.wait(5), "bridge terminal sink did not run"
    assert len(reports) == 1
    return result, reports[0]


@pytest.mark.parametrize("ingress_kind", (IngressKind.CAPTURED, IngressKind.IMPORTED))
def test_real_audio_scheduler_bridge_initial_and_selected_window_reanalysis(
    prepared_database_path: Path,
    tmp_path: Path,
    ingress_kind: IngressKind,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    run_id = str(uuid4())
    _insert_active_run(
        runtime,
        run_id,
        ingress_kind,
        imported_source_path=tmp_path / f"{run_id}-source.wav",
    )
    loader, reference = _persist_audio(runtime, tmp_path, run_id, ingress_kind)
    bridge, executor = _build_real_stack(runtime, loader)
    try:
        initial, initial_report = _submit_and_wait(
            lambda sink: bridge.submit_initial(
                InitialAnalysisRequest(run_id, reference),
                sink,
            )
        )
        executor.wait_for_calls(1)

        assert UUID(initial.analysis_attempt_id).version == 4
        assert initial_report.status is BridgeTerminalStatus.COMMITTED
        assert initial_report.run_id == run_id
        assert initial_report.analysis_attempt_id == initial.analysis_attempt_id
        assert initial_report.attempt_kind is AttemptKind.INITIAL
        assert initial_report.terminal_at == TERMINAL_AT
        initial_job = executor.calls[0][0]
        assert initial_job.analysis_attempt_id == initial.analysis_attempt_id
        assert initial_job.audio == reference
        assert (
            initial_job.snapshot.analysis_window_start_seconds,
            initial_job.snapshot.analysis_window_end_seconds,
        ) == (0.125, 2.875)

        selected_start = float.fromhex("0x1.0000000000001p-2")
        selected_end = float.fromhex("0x1.0000000000001p+0")
        reanalysis, reanalysis_report = _submit_and_wait(
            lambda sink: bridge.submit_reanalysis(
                ReanalysisRequest(run_id, selected_start, selected_end),
                sink,
            )
        )
        executor.wait_for_calls(2)

        assert reanalysis.analysis_attempt_id != initial.analysis_attempt_id
        assert UUID(reanalysis.analysis_attempt_id).version == 4
        assert reanalysis_report.status is BridgeTerminalStatus.COMMITTED
        assert reanalysis_report.attempt_kind is AttemptKind.REANALYSIS
        assert (
            reanalysis_report.analysis_attempt_id
            == reanalysis.analysis_attempt_id
        )
        assert reanalysis_report.terminal_at == REANALYSIS_TERMINAL_AT
        reanalysis_job = executor.calls[1][0]
        assert (
            reanalysis_job.analysis_attempt_id
            == reanalysis.analysis_attempt_id
        )
        assert (
            reanalysis_job.snapshot.analysis_window_start_seconds.hex(),
            reanalysis_job.snapshot.analysis_window_end_seconds.hex(),
        ) == (selected_start.hex(), selected_end.hex())
        assert (
            reanalysis_job.snapshot.analysis_window_start_seconds,
            reanalysis_job.snapshot.analysis_window_end_seconds,
        ) != (0.125, 2.875)

        run_row = RunRepository(runtime).get(run_id)
        assert (run_row.state, run_row.judgment, run_row.terminal_code) == (
            "SUCCEEDED",
            "PASS",
            "ANALYSIS_SUCCEEDED",
        )

        with sqlite3.connect(prepared_database_path) as connection:
            rows = connection.execute(
                """
                SELECT attempt_id, run_id, audio_id, attempt_kind, state,
                       queued_at, started_at, terminal_at, result_payload_json
                FROM analysis_attempt
                ORDER BY queued_at
                """
            ).fetchall()
            assert rows == [
                (
                    initial.analysis_attempt_id,
                    run_id,
                    reference.reference_id,
                    "INITIAL",
                    "SUCCEEDED",
                    QUEUED_AT,
                    RUNNING_AT,
                    TERMINAL_AT,
                    '{"judgment":"PASS","result":{"score":7}}',
                ),
                (
                    reanalysis.analysis_attempt_id,
                    run_id,
                    reference.reference_id,
                    "REANALYSIS",
                    "SUCCEEDED",
                    REANALYSIS_QUEUED_AT,
                    REANALYSIS_RUNNING_AT,
                    REANALYSIS_TERMINAL_AT,
                    '{"judgment":"PASS","result":{"score":7}}',
                ),
            ]

        phase_a = bridge.stop_and_join_scheduler(5.0)
        assert phase_a.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
        assert runtime.closed is False
        phase_b = bridge.close_persistence_after_upstream_stopped()
        assert phase_b.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED
        assert runtime.closed is True
        assert bridge.close_persistence_after_upstream_stopped() is phase_b
    finally:
        if not runtime.closed:
            phase_a = bridge.stop_and_join_scheduler(5.0)
            if (
                phase_a.status
                is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
            ):
                bridge.close_persistence_after_upstream_stopped()


def test_legacy_success_payload_survives_scheduler_and_bridge_unchanged(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    run_id = str(uuid4())
    envelope = make_analysis_envelope(
        config={
            "config_id": 7,
            "selected_channels": [0],
            "input_device_samplerate": 8_000.0,
            "channels": [
                {
                    "channel_index": 0,
                    "spl_weighting": "Z",
                }
            ],
        },
        channel_mapping={
            "schema": "audio-channel-map/v1",
            "channel_order": [0],
        },
        analysis={
            "window_start_seconds": 0.0,
            "window_end_seconds": 0.0005,
            "algorithm_revision": "legacy-success-v1",
            "execution_timeout_seconds": 30.0,
            "queue_timeout_seconds": None,
            "toolchain": None,
        },
    )
    _insert_active_run(
        runtime,
        run_id,
        IngressKind.CAPTURED,
        analysis_envelope=envelope,
    )
    loader, reference = _persist_audio(
        runtime,
        tmp_path,
        run_id,
        IngressKind.CAPTURED,
    )
    executor = _RecordingProductionExecutor()
    bridge, _ = _build_real_stack(
        runtime,
        loader,
        executor=executor,
        now_utc=_UtcSequence(QUEUED_AT, RUNNING_AT, TERMINAL_AT),
    )
    try:
        _, report = _submit_and_wait(
            lambda sink: bridge.submit_initial(
                InitialAnalysisRequest(run_id, reference),
                sink,
            )
        )

        assert report.status is BridgeTerminalStatus.COMMITTED
        assert len(executor.results) == 1
        expected_json = decode_analysis_result_payload(
            executor.results[0].result_payload
        ).result_payload_json
        with sqlite3.connect(prepared_database_path) as connection:
            attempt = connection.execute(
                """
                SELECT state, judgment, result_payload_json
                FROM analysis_attempt
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        assert attempt == ("SUCCEEDED", "PASS", expected_json)
        decoded = json.loads(expected_json)
        assert set(decoded) == {"judgment", "result"}
        assert decoded["judgment"] == "PASS"
        assert set(decoded["result"]) == {
            "legacy_overall",
            "limit_pass",
            "ai_pass",
            "ai_score",
            "analysis_window",
            "channel_results",
            "review",
        }
    finally:
        if not runtime.closed:
            phase_a = bridge.stop_and_join_scheduler(5.0)
            if (
                phase_a.status
                is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
            ):
                bridge.close_persistence_after_upstream_stopped()


def test_run_persistence_public_exports_hide_private_prepared_values() -> None:
    import app.service.run_persistence as package

    assert "PreparedAnalysisFacts" not in package.__all__
    assert "PreparedAnalysisSubmission" not in package.__all__
    assert not hasattr(package, "PreparedAnalysisFacts")
    assert not hasattr(package, "PreparedAnalysisSubmission")


@pytest.mark.parametrize(
    ("reanalysis", "started", "outcome_kind", "expected_state", "expected_code"),
    (
        (
            True,
            True,
            "failed",
            "FAILED",
            "ANALYSIS_EXECUTION_FAILED",
        ),
        (True, False, "eviction", "CANCELED", "REANALYSIS_EVICTED"),
        (False, False, "user_stop", "CANCELED", "USER_CANCELED"),
        (
            False,
            True,
            "user_stop",
            "CANCELED",
            "USER_CANCELED",
        ),
        (
            False,
            True,
            "scheduler_shutdown",
            "CANCELED",
            "ANALYSIS_SCHEDULER_SHUTDOWN",
        ),
        (
            False,
            False,
            "queue_timeout",
            "FAILED",
            "ANALYSIS_QUEUE_TIMEOUT",
        ),
    ),
)
def test_bridge_terminal_origin_and_failure_matrix_has_one_durable_sink(
    tmp_path: Path,
    reanalysis: bool,
    started: bool,
    outcome_kind: str,
    expected_state: str,
    expected_code: str,
) -> None:
    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        run=_run_record(state="SUCCEEDED") if reanalysis else _run_record(),
    )
    result = (
        bridge.submit_reanalysis(
            ReanalysisRequest(MATRIX_RUN_ID, 0.25, 0.75)
        )
        if reanalysis
        else bridge.submit_initial(
            InitialAnalysisRequest(MATRIX_RUN_ID, _reference(audio))
        )
    )
    job = scheduler.submitted[0]
    assert result.authority is BridgeAuthorityStatus.ACQUIRED
    if started:
        boundary.before_audio_load(job)
    outcome = {
        "failed": lambda: _failed_outcome(
            job,
            AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
            AnalysisStage.EXECUTE,
        ),
        "eviction": lambda: _canceled_outcome(
            job,
            AnalysisCancelOrigin.RESULT_EVICTION,
            started=started,
        ),
        "user_stop": lambda: _canceled_outcome(
            job,
            AnalysisCancelOrigin.USER_STOP,
            started=started,
        ),
        "scheduler_shutdown": lambda: _canceled_outcome(
            job,
            AnalysisCancelOrigin.SCHEDULER_SHUTDOWN,
            started=started,
        ),
        "queue_timeout": lambda: _queue_timeout_outcome(job),
    }[outcome_kind]()
    boundary(outcome)

    assert len(reports) == 1
    assert reports[0].status is BridgeTerminalStatus.COMMITTED
    assert reports[0].attempt_state.value == expected_state
    assert reports[0].code == expected_code
    assert len(attempts.running_finished if started else attempts.queued_finished) == 1


def test_malformed_terminal_repository_quarantines_and_sink_exception_isolated(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    attempts = _AttemptRepository(terminal_result=object())
    delivered = []

    def raising_sink(report) -> None:
        delivered.append(report)
        raise RuntimeError("test-owned sink failed")

    bridge, boundary, scheduler, _, reports, audio = _make_bridge(
        tmp_path,
        runtime=runtime,
        attempt_repository=attempts,
        terminal_report_sink=raising_sink,
    )
    result = bridge.submit_initial(
        InitialAnalysisRequest(MATRIX_RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    boundary(_succeeded_outcome(job))

    assert result.authority is BridgeAuthorityStatus.ACQUIRED
    assert len(reports) == len(delivered) == 1
    assert reports[0].status is BridgeTerminalStatus.DB_UNKNOWN
    assert reports[0].restart_required is True
    assert runtime.quarantine_calls == 1


def test_frozen_ai_failure_survives_scheduler_bridge_and_durable_attempt(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    bridge, boundary, scheduler, attempts, reports, audio = _make_bridge(
        tmp_path,
        run=_run_record(),
    )
    result = bridge.submit_initial(
        InitialAnalysisRequest(MATRIX_RUN_ID, _reference(audio))
    )
    job = scheduler.submitted[0]
    boundary.before_audio_load(job)
    summary = "FROZEN_AI_UNSUPPORTED:sha256:" + "a" * 64
    original = _failed_outcome(
        job,
        AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
        AnalysisStage.EXECUTE,
    )
    boundary(
        replace(
            original,
            error=replace(original.error, summary=summary),
        )
    )

    assert result.authority is BridgeAuthorityStatus.ACQUIRED
    persisted = attempts.running_finished[0][1]
    assert persisted.code == "ANALYSIS_EXECUTION_FAILED"
    assert persisted.summary == summary
    assert reports[0].summary == summary
