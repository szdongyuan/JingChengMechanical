from __future__ import annotations

import dataclasses
import inspect
import math
from enum import Enum
from pathlib import PureWindowsPath
from typing import get_type_hints

import pytest

import app.service.analysis.scheduler as scheduler_contracts
from app.service.analysis.scheduler import (
    MAX_CANCEL_REASON_UTF8_BYTES,
    MAX_ERROR_DETAIL_ITEMS,
    MAX_ERROR_DETAIL_KEY_UTF8_BYTES,
    MAX_ERROR_DETAIL_VALUE_UTF8_BYTES,
    MAX_ERROR_SUMMARY_UTF8_BYTES,
    MAX_FROZEN_RESULT_DEPTH,
    MAX_FROZEN_RESULT_NODES,
    MAX_FROZEN_RESULT_STRING_UTF8_BYTES,
    MAX_FROZEN_RESULT_TOTAL_STRING_UTF8_BYTES,
    AiToolchainSnapshot,
    AnalysisCancelOrigin,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisExecutionError,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    AnalysisExecutor,
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AnalysisOutcome,
    AnalysisSchedulerConfig,
    AnalysisStartGate,
    AnalysisStartGateCode,
    AnalysisStartGateError,
    AnalysisStartGateFailure,
    AnalysisStage,
    AnalysisStatus,
    AttemptDiagnostic,
    AttemptState,
    AudioLoadError,
    AudioLoader,
    AudioStorageKind,
    CancelReceipt,
    CancelStatus,
    CancellationToken,
    DiagnosticSink,
    FrozenResultObject,
    LoadedAudio,
    MonotonicClock,
    OutcomeSink,
    PersistedAudioReference,
    SchedulerCounterKey,
    SchedulerDiagnosticEvent,
    SchedulerDiagnosticEventKind,
    SchedulerDiagnostics,
    SchedulerLifecycle,
    ShutdownReport,
    ShutdownStatus,
    SubmitReceipt,
    SubmitStatus,
    TerminalReceiptDiagnostic,
    ToolchainLaneDiagnostic,
    canonical_toolchain_lane_key,
    validate_frozen_result_payload,
)
from tests.analysis_scheduler.fakes import FullAudioPayloadProbe, MutableContextProbe


WINDOWS_AUDIO_PATH = str(PureWindowsPath("C:/analysis/final/run-1.wav"))


def _enum_shape(enum_type: type[Enum], expected: list[tuple[str, object]]) -> None:
    assert list(enum_type.__members__.items()) == [
        (name, enum_type(value)) for name, value in expected
    ]
    assert [(member.name, member.value) for member in enum_type] == expected


ENUM_SHAPES = [
    (
        AudioStorageKind,
        [
            ("CAPTURED_FINAL_WAV", "captured_final_wav"),
            ("IMPORTED_FINAL_AUDIO", "imported_final_audio"),
        ],
    ),
    (
        AnalysisKind,
        [
            ("AUTOMATIC", "automatic"),
            ("REANALYSIS", "reanalysis"),
            ("IMPORTED", "imported"),
        ],
    ),
    (
        AnalysisExecutionStatus,
        [("SUCCEEDED", "succeeded"), ("NO_RESULT", "no_result")],
    ),
    (
        AnalysisStatus,
        [
            ("SUCCEEDED", "succeeded"),
            ("NO_RESULT", "no_result"),
            ("FAILED", "failed"),
            ("CANCELED", "canceled"),
        ],
    ),
    (
        SubmitStatus,
        [
            ("ACCEPTED", "accepted"),
            ("INVALID_JOB", "invalid_job"),
            ("DUPLICATE_ANALYSIS_ATTEMPT_ID", "duplicate_analysis_attempt_id"),
            ("QUEUE_FULL", "queue_full"),
            ("NOT_ACCEPTING", "not_accepting"),
        ],
    ),
    (
        CancelStatus,
        [
            ("CANCELED_PENDING", "canceled_pending"),
            ("CANCEL_REQUESTED_RUNNING", "cancel_requested_running"),
            ("ALREADY_TERMINAL", "already_terminal"),
            ("NOT_FOUND", "not_found"),
            ("RUN_ID_MISMATCH", "run_id_mismatch"),
        ],
    ),
    (
        AnalysisCancelOrigin,
        [
            ("USER_STOP", "user_stop"),
            ("RESULT_EVICTION", "result_eviction"),
            ("SCHEDULER_SHUTDOWN", "scheduler_shutdown"),
        ],
    ),
    (
        AnalysisStartGateCode,
        [
            ("ATTEMPT_START_NOT_QUEUED", "ATTEMPT_START_NOT_QUEUED"),
            ("ATTEMPT_START_DB_FAILED", "ATTEMPT_START_DB_FAILED"),
            ("ATTEMPT_START_DB_UNKNOWN", "ATTEMPT_START_DB_UNKNOWN"),
            ("ATTEMPT_START_CONTEXT_INVALID", "ATTEMPT_START_CONTEXT_INVALID"),
            (
                "ATTEMPT_START_TIMESTAMP_FAILED",
                "ATTEMPT_START_TIMESTAMP_FAILED",
            ),
            ("ATTEMPT_START_RESULT_INVALID", "ATTEMPT_START_RESULT_INVALID"),
            (
                "ATTEMPT_START_GATE_EXCEPTION",
                "ATTEMPT_START_GATE_EXCEPTION",
            ),
        ],
    ),
    (
        AnalysisStage,
        [
            ("ADMISSION", "admission"),
            ("QUEUE", "queue"),
            ("START_GATE", "start_gate"),
            ("AUDIO_LOAD", "audio_load"),
            ("EXECUTE", "execute"),
            ("CANCEL", "cancel"),
            ("OUTCOME_SINK", "outcome_sink"),
            ("SHUTDOWN", "shutdown"),
        ],
    ),
    (
        AnalysisErrorCategory,
        [
            ("INVALID_JOB", "invalid_job"),
            ("DUPLICATE_ANALYSIS_ATTEMPT_ID", "duplicate_analysis_attempt_id"),
            ("ANALYSIS_QUEUE_FULL", "analysis_queue_full"),
            ("SCHEDULER_NOT_ACCEPTING", "scheduler_not_accepting"),
            ("QUEUE_WAIT_TIMEOUT", "queue_wait_timeout"),
            ("ATTEMPT_START_FAILED", "attempt_start_failed"),
            ("AUDIO_REFERENCE_UNREADABLE", "audio_reference_unreadable"),
            ("AUDIO_REFERENCE_CHANGED", "audio_reference_changed"),
            ("AUDIO_FORMAT_MISMATCH", "audio_format_mismatch"),
            ("AUDIO_LOAD_FAILED", "audio_load_failed"),
            ("ANALYSIS_TIMEOUT", "analysis_timeout"),
            ("ANALYSIS_EXECUTION_FAILED", "analysis_execution_failed"),
            ("ANALYSIS_NO_RESULT", "analysis_no_result"),
            ("ANALYSIS_CANCELED", "analysis_canceled"),
            ("OUTCOME_SINK_FAILED", "outcome_sink_failed"),
            ("SHUTDOWN_INCOMPLETE", "shutdown_incomplete"),
        ],
    ),
    (
        ShutdownStatus,
        [
            ("CLOSED", "closed"),
            ("SHUTDOWN_INCOMPLETE", "shutdown_incomplete"),
            ("IN_PROGRESS", "in_progress"),
        ],
    ),
    (
        SchedulerLifecycle,
        [
            ("RUNNING", "running"),
            ("SHUTTING_DOWN", "shutting_down"),
            ("CLOSED", "closed"),
            ("SHUTDOWN_INCOMPLETE", "shutdown_incomplete"),
        ],
    ),
    (
        AttemptState,
        [
            ("PENDING", "pending"),
            ("RUNNING", "running"),
            ("CANCEL_REQUESTED", "cancel_requested"),
            ("SUCCEEDED", "succeeded"),
            ("NO_RESULT", "no_result"),
            ("FAILED", "failed"),
            ("CANCELED", "canceled"),
        ],
    ),
    (
        SchedulerCounterKey,
        [
            ("ACCEPTED", "accepted"),
            ("INVALID_JOB_REJECTED", "invalid_job_rejected"),
            ("DUPLICATE_ATTEMPT_REJECTED", "duplicate_attempt_rejected"),
            ("QUEUE_FULL_REJECTED", "queue_full_rejected"),
            ("NOT_ACCEPTING_REJECTED", "not_accepting_rejected"),
            ("SUCCEEDED", "succeeded"),
            ("NO_RESULT", "no_result"),
            ("FAILED", "failed"),
            ("CANCELED", "canceled"),
            ("QUEUE_TIMEOUT", "queue_timeout"),
            ("START_GATE_FAILURE", "start_gate_failure"),
            ("AUDIO_LOAD_FAILURE", "audio_load_failure"),
            ("ANALYSIS_TIMEOUT", "analysis_timeout"),
            ("EXECUTOR_FAILURE", "executor_failure"),
            ("LATE_RESULTS_SUPPRESSED", "late_results_suppressed"),
            ("OUTCOME_SINK_FAILURE", "outcome_sink_failure"),
            ("DIAGNOSTIC_SINK_FAILURE", "diagnostic_sink_failure"),
            ("SHUTDOWN_INCOMPLETE", "shutdown_incomplete"),
        ],
    ),
    (
        SchedulerDiagnosticEventKind,
        [
            ("SUBMIT_ACCEPTED", "submit_accepted"),
            ("SUBMIT_REJECTED", "submit_rejected"),
            ("ATTEMPT_DISPATCHED", "attempt_dispatched"),
            ("CANCEL_REQUESTED", "cancel_requested"),
            ("ATTEMPT_TERMINAL", "attempt_terminal"),
            ("OUTCOME_SINK_FAILED", "outcome_sink_failed"),
            ("LIFECYCLE_CHANGED", "lifecycle_changed"),
        ],
    ),
]


@pytest.mark.parametrize(("enum_type", "expected"), ENUM_SHAPES)
def test_public_enum_shapes_are_exact(enum_type, expected):
    _enum_shape(enum_type, expected)


def _field_shape(cls):
    return [
        (
            field.name,
            field.default if field.default is not dataclasses.MISSING else None,
        )
        for field in dataclasses.fields(cls)
    ]


def test_contract_dataclass_fields_and_defaults_are_exact():
    assert _field_shape(PersistedAudioReference) == [
        ("reference_id", None),
        ("storage_kind", None),
        ("absolute_path", None),
        ("sample_rate_hz", None),
        ("channel_count", None),
        ("frame_count", None),
        ("dtype", None),
        ("content_revision", None),
        ("audio_data_id", None),
    ]
    assert [f.name for f in dataclasses.fields(AiToolchainSnapshot)] == [
        "toolchain_id",
        "runtime_lane_key",
        "execution_revision",
        "execution_mode",
        "entrypoint",
        "package_dir",
        "params_json",
        "timeout_seconds",
    ]
    assert [f.name for f in dataclasses.fields(AnalysisInputSnapshot)] == [
        "config_snapshot_json",
        "calibration_snapshot_json",
        "workorder_snapshot_json",
        "channel_mapping_json",
        "analysis_window_start_seconds",
        "analysis_window_end_seconds",
        "algorithm_revision",
        "ai_toolchain",
    ]
    assert [f.name for f in dataclasses.fields(AnalysisJob)] == [
        "run_id",
        "analysis_attempt_id",
        "kind",
        "audio",
        "snapshot",
        "execution_timeout_seconds",
        "queue_timeout_seconds",
    ]
    assert _field_shape(AnalysisSchedulerConfig) == [
        ("worker_count", 2),
        ("pending_capacity", 64),
        ("terminal_receipt_capacity", 256),
        ("shutdown_timeout_seconds", 5.0),
    ]
    assert [f.name for f in dataclasses.fields(FrozenResultObject)] == ["items"]
    assert _field_shape(AnalysisExecutionResult) == [
        ("status", None),
        ("result_payload", None),
        ("summary", None),
    ]
    assert _field_shape(AnalysisError) == [
        ("category", None),
        ("stage", None),
        ("summary", None),
        ("details", ()),
    ]
    assert _field_shape(AnalysisStartGateFailure) == [
        ("code", None),
        ("summary", None),
    ]
    assert [f.name for f in dataclasses.fields(AnalysisOutcome)] == [
        "run_id",
        "analysis_attempt_id",
        "kind",
        "status",
        "audio_reference_id",
        "result_payload",
        "error",
        "submitted_monotonic",
        "started_monotonic",
        "finished_monotonic",
        "queue_wait_ms",
        "execution_ms",
        "cancel_origin",
    ]
    assert [f.name for f in dataclasses.fields(SubmitReceipt)] == [
        "status",
        "run_id",
        "analysis_attempt_id",
        "submission_sequence",
        "error",
    ]
    assert [f.name for f in dataclasses.fields(CancelReceipt)] == [
        "status",
        "run_id",
        "analysis_attempt_id",
        "drain_pending",
        "error",
    ]
    shutdown_report_fields = dataclasses.fields(ShutdownReport)
    assert [f.name for f in shutdown_report_fields] == [
        "status",
        "started_monotonic",
        "finished_monotonic",
        "canceled_pending",
        "cancel_requested_running",
        "remaining_attempt_ids",
        "remaining_worker_ids",
        "remaining_run_lanes",
        "remaining_toolchain_lanes",
        "control_thread_alive",
    ]
    assert shutdown_report_fields[-1].default is dataclasses.MISSING
    assert [f.name for f in dataclasses.fields(AttemptDiagnostic)] == [
        "run_id",
        "analysis_attempt_id",
        "kind",
        "state",
        "submission_sequence",
        "worker_id",
        "runtime_lane_key",
        "wait_ms",
        "execution_ms",
    ]
    assert [f.name for f in dataclasses.fields(ToolchainLaneDiagnostic)] == [
        "runtime_lane_key",
        "running",
        "pending",
    ]
    assert [f.name for f in dataclasses.fields(TerminalReceiptDiagnostic)] == [
        "run_id",
        "analysis_attempt_id",
        "terminal_status",
        "finished_monotonic",
    ]
    assert [f.name for f in dataclasses.fields(SchedulerDiagnostics)] == [
        "lifecycle",
        "worker_count",
        "pending_capacity",
        "terminal_receipt_capacity",
        "pending",
        "running",
        "cancel_requested",
        "full_audio_residency_count",
        "peak_full_audio_residency_count",
        "peak_running_workers",
        "pending_by_kind",
        "oldest_pending_wait_ms",
        "blocked_on_run_lane",
        "blocked_on_toolchain_lane",
        "attempts",
        "toolchain_lanes",
        "terminal_receipts",
        "counters",
    ]
    assert [f.name for f in dataclasses.fields(SchedulerDiagnosticEvent)] == [
        "event_kind",
        "occurred_monotonic",
        "run_id",
        "analysis_attempt_id",
        "analysis_kind",
        "audio_reference_id",
        "submission_sequence",
        "state_before",
        "state_after",
        "worker_id",
        "runtime_lane_key",
        "queue_wait_ms",
        "load_ms",
        "execution_ms",
        "error_category",
        "error_stage",
        "summary",
        "lifecycle_before",
        "lifecycle_after",
        "details",
    ]
    for cls in (
        PersistedAudioReference,
        AiToolchainSnapshot,
        AnalysisInputSnapshot,
        AnalysisJob,
        AnalysisSchedulerConfig,
        FrozenResultObject,
        AnalysisExecutionResult,
        AnalysisError,
        AnalysisStartGateFailure,
        AnalysisOutcome,
        SubmitReceipt,
        CancelReceipt,
        ShutdownReport,
        AttemptDiagnostic,
        ToolchainLaneDiagnostic,
        TerminalReceiptDiagnostic,
        SchedulerDiagnostics,
        SchedulerDiagnosticEvent,
    ):
        params = cls.__dataclass_params__
        assert params.frozen
        assert "__slots__" in cls.__dict__


def test_task_one_package_exports_are_exact_and_in_frozen_order():
    assert scheduler_contracts.__all__ == (
        "AnalysisScheduler",
        "AnalysisSchedulerConfig",
        "AnalysisJob",
        "AnalysisKind",
        "PersistedAudioReference",
        "AudioStorageKind",
        "AiToolchainSnapshot",
        "AnalysisInputSnapshot",
        "AnalysisExecutionResult",
        "AnalysisExecutionStatus",
        "FrozenResultScalar",
        "FrozenResultValue",
        "FrozenResultPayload",
        "FrozenResultObject",
        "validate_frozen_result_payload",
        "MAX_FROZEN_RESULT_DEPTH",
        "MAX_FROZEN_RESULT_NODES",
        "MAX_FROZEN_RESULT_STRING_UTF8_BYTES",
        "MAX_FROZEN_RESULT_TOTAL_STRING_UTF8_BYTES",
        "AnalysisOutcome",
        "AnalysisStatus",
        "AnalysisError",
        "AnalysisErrorCategory",
        "AnalysisStage",
        "SubmitReceipt",
        "SubmitStatus",
        "CancelReceipt",
        "CancelStatus",
        "AnalysisCancelOrigin",
        "MAX_CANCEL_REASON_UTF8_BYTES",
        "ShutdownReport",
        "ShutdownStatus",
        "SchedulerLifecycle",
        "AttemptState",
        "AttemptDiagnostic",
        "ToolchainLaneDiagnostic",
        "TerminalReceiptDiagnostic",
        "SchedulerDiagnostics",
        "SchedulerCounterKey",
        "SchedulerDiagnosticEvent",
        "SchedulerDiagnosticEventKind",
        "MAX_ERROR_SUMMARY_UTF8_BYTES",
        "MAX_ERROR_DETAIL_ITEMS",
        "MAX_ERROR_DETAIL_KEY_UTF8_BYTES",
        "MAX_ERROR_DETAIL_VALUE_UTF8_BYTES",
        "canonical_toolchain_lane_key",
        "CancellationToken",
        "LoadedAudio",
        "AudioLoader",
        "AnalysisExecutor",
        "AnalysisStartGate",
        "OutcomeSink",
        "MonotonicClock",
        "DiagnosticSink",
        "AudioLoadError",
        "AnalysisExecutionError",
        "AnalysisStartGateCode",
        "AnalysisStartGateFailure",
        "AnalysisStartGateError",
    )


def test_public_limits_are_exact():
    assert MAX_FROZEN_RESULT_DEPTH == 32
    assert MAX_FROZEN_RESULT_NODES == 65_536
    assert MAX_FROZEN_RESULT_STRING_UTF8_BYTES == 65_536
    assert MAX_FROZEN_RESULT_TOTAL_STRING_UTF8_BYTES == 4_194_304
    assert MAX_ERROR_SUMMARY_UTF8_BYTES == 1024
    assert MAX_ERROR_DETAIL_ITEMS == 16
    assert MAX_ERROR_DETAIL_KEY_UTF8_BYTES == 64
    assert MAX_ERROR_DETAIL_VALUE_UTF8_BYTES == 256
    assert MAX_CANCEL_REASON_UTF8_BYTES == 512


def test_public_function_and_protocol_signatures_are_exact():
    assert str(inspect.signature(canonical_toolchain_lane_key)) == (
        "(toolchain_id: 'str') -> 'str'"
    )
    assert str(inspect.signature(validate_frozen_result_payload)) == (
        "(payload: 'FrozenResultPayload') -> 'None'"
    )
    assert str(inspect.signature(AudioLoadError)) == (
        "(error: 'AnalysisError', /) -> 'None'"
    )
    assert str(inspect.signature(AnalysisExecutionError)) == (
        "(error: 'AnalysisError', /) -> 'None'"
    )
    assert str(inspect.signature(AnalysisStartGateError)) == (
        "(failure: 'AnalysisStartGateFailure', /) -> 'None'"
    )
    assert str(inspect.signature(CancellationToken.wait)) == (
        "(self, timeout_seconds: 'float | None' = None) -> 'bool'"
    )
    assert str(inspect.signature(AudioLoader.open)) == (
        "(self, reference: 'PersistedAudioReference', "
        "cancel_token: 'CancellationToken') -> 'LoadedAudio'"
    )
    assert str(inspect.signature(AnalysisExecutor.execute)) == (
        "(self, job: 'AnalysisJob', audio: 'LoadedAudio', "
        "cancel_token: 'CancellationToken') -> 'AnalysisExecutionResult'"
    )
    assert str(inspect.signature(AnalysisStartGate.before_audio_load)) == (
        "(self, job: 'AnalysisJob') -> 'None'"
    )
    assert str(inspect.signature(OutcomeSink.__call__)) == (
        "(self, outcome: 'AnalysisOutcome') -> 'None'"
    )
    assert str(inspect.signature(MonotonicClock.monotonic)) == (
        "(self) -> 'float'"
    )
    assert str(inspect.signature(DiagnosticSink.__call__)) == (
        "(self, event: 'SchedulerDiagnosticEvent') -> 'None'"
    )
    assert {
        name for name in LoadedAudio.__dict__ if not name.startswith("_")
    } == {"samples", "sample_rate_hz", "channel_count", "frame_count", "close"}
    assert str(inspect.signature(LoadedAudio.close)) == "(self) -> 'None'"
    assert str(inspect.signature(CancellationToken.is_cancelled.fget)) == (
        "(self) -> 'bool'"
    )
    assert str(inspect.signature(CancellationToken.reason.fget)) == (
        "(self) -> 'str | None'"
    )
    assert str(inspect.signature(LoadedAudio.samples.fget)) == (
        "(self) -> 'object'"
    )


def test_ordinary_contract_constructors_allow_positional_and_keyword_arguments():
    assert AnalysisSchedulerConfig(3, 4, 5, 6.0).worker_count == 3
    assert AnalysisSchedulerConfig(worker_count=3).worker_count == 3
    audio = make_audio()
    assert PersistedAudioReference(
        audio.reference_id,
        audio.storage_kind,
        audio.absolute_path,
        audio.sample_rate_hz,
        audio.channel_count,
        audio.frame_count,
        audio.dtype,
        audio.content_revision,
        audio.audio_data_id,
    ) == audio


def make_audio(**changes) -> PersistedAudioReference:
    values = dict(
        reference_id="audio-1",
        storage_kind=AudioStorageKind.CAPTURED_FINAL_WAV,
        absolute_path=WINDOWS_AUDIO_PATH,
        sample_rate_hz=48_000,
        channel_count=2,
        frame_count=96_000,
        dtype="float32",
        content_revision="sha256:abc",
        audio_data_id=None,
    )
    values.update(changes)
    return PersistedAudioReference(**values)


def make_toolchain(**changes) -> AiToolchainSnapshot:
    values = dict(
        toolchain_id="ai-1",
        runtime_lane_key="ai-toolchain:ai-1",
        execution_revision="revision-1",
        execution_mode="resident",
        entrypoint="analyze.py",
        package_dir="C:/analysis/ai-1",
        params_json="{}",
        timeout_seconds=10.0,
    )
    values.update(changes)
    return AiToolchainSnapshot(**values)


def make_snapshot(**changes) -> AnalysisInputSnapshot:
    values = dict(
        config_snapshot_json="{}",
        calibration_snapshot_json="{}",
        workorder_snapshot_json="{}",
        channel_mapping_json="{}",
        analysis_window_start_seconds=0.0,
        analysis_window_end_seconds=2.0,
        algorithm_revision="algo-1",
        ai_toolchain=None,
    )
    values.update(changes)
    return AnalysisInputSnapshot(**values)


def make_job(**changes) -> AnalysisJob:
    values = dict(
        run_id="run-1",
        analysis_attempt_id="attempt-1",
        kind=AnalysisKind.AUTOMATIC,
        audio=make_audio(),
        snapshot=make_snapshot(),
        execution_timeout_seconds=30.0,
        queue_timeout_seconds=None,
    )
    values.update(changes)
    return AnalysisJob(**values)


@pytest.mark.parametrize(
    "changes, exception",
    [
        ({"reference_id": ""}, ValueError),
        ({"reference_id": 3}, TypeError),
        ({"storage_kind": "captured_final_wav"}, TypeError),
        ({"absolute_path": "relative.wav"}, ValueError),
        ({"absolute_path": "C:/analysis/../run.wav"}, ValueError),
        ({"sample_rate_hz": True}, TypeError),
        ({"sample_rate_hz": 0}, ValueError),
        ({"channel_count": 1.5}, TypeError),
        ({"frame_count": -1}, ValueError),
        ({"dtype": ""}, ValueError),
        ({"dtype": object()}, TypeError),
        ({"content_revision": ""}, ValueError),
        ({"audio_data_id": 4}, TypeError),
        ({"audio_data_id": ""}, ValueError),
    ],
)
def test_persisted_audio_reference_rejects_invalid_values(changes, exception):
    with pytest.raises(exception):
        make_audio(**changes)


@pytest.mark.parametrize(
    "root_relative_path",
    [
        "/analysis/final/run.wav",
        r"\analysis\final\run.wav",
    ],
)
def test_persisted_audio_reference_rejects_windows_root_relative_paths(
    root_relative_path,
):
    with pytest.raises(ValueError):
        make_audio(absolute_path=root_relative_path)


@pytest.mark.parametrize(
    "fully_qualified_path",
    [
        r"C:\analysis\final\run.wav",
        r"\\analysis-server\audio-share\final\run.wav",
    ],
)
def test_persisted_audio_reference_accepts_fully_qualified_windows_paths(
    fully_qualified_path,
):
    assert make_audio(absolute_path=fully_qualified_path).absolute_path == (
        fully_qualified_path
    )


def test_optional_audio_data_id_is_allowed_for_both_storage_kinds():
    assert make_audio(audio_data_id=None).audio_data_id is None
    assert (
        make_audio(
            storage_kind=AudioStorageKind.IMPORTED_FINAL_AUDIO,
            audio_data_id=None,
        ).audio_data_id
        is None
    )


@pytest.mark.parametrize(
    "value",
    [[], {}, bytearray(), memoryview(b"x"), object(), MutableContextProbe()],
)
def test_audio_reference_rejects_hostile_payloads(value):
    with pytest.raises((TypeError, ValueError)):
        make_audio(absolute_path=value)


def test_audio_reference_rejects_numpy_arrays_memmaps_and_scalars(tmp_path):
    numpy = pytest.importorskip("numpy")
    mmap_path = tmp_path / "samples.dat"
    mmap = numpy.memmap(mmap_path, dtype="float32", mode="w+", shape=(1,))
    try:
        for value in (numpy.array([1.0]), mmap, numpy.int64(48_000)):
            with pytest.raises((TypeError, ValueError)):
                make_audio(sample_rate_hz=value)
    finally:
        mmap._mmap.close()


def test_snapshot_fields_are_canonical_immutable_strings():
    for field_name in (
        "config_snapshot_json",
        "calibration_snapshot_json",
        "workorder_snapshot_json",
        "channel_mapping_json",
    ):
        with pytest.raises(TypeError):
            make_snapshot(**{field_name: {}})
        with pytest.raises(ValueError):
            make_snapshot(**{field_name: ""})


@pytest.mark.parametrize(
    "changes, exception",
    [
        ({"analysis_window_start_seconds": True}, TypeError),
        ({"analysis_window_start_seconds": math.inf}, ValueError),
        ({"analysis_window_start_seconds": -0.1}, ValueError),
        ({"analysis_window_end_seconds": math.nan}, ValueError),
        ({"analysis_window_end_seconds": 0.0}, ValueError),
        ({"algorithm_revision": ""}, ValueError),
        ({"algorithm_revision": 1}, TypeError),
        ({"ai_toolchain": object()}, TypeError),
    ],
)
def test_analysis_input_snapshot_rejects_invalid_values(changes, exception):
    with pytest.raises(exception):
        make_snapshot(**changes)


@pytest.mark.parametrize(
    "field_name",
    [
        "toolchain_id",
        "runtime_lane_key",
        "execution_revision",
        "execution_mode",
        "entrypoint",
        "package_dir",
        "params_json",
    ],
)
def test_ai_toolchain_text_fields_are_non_empty(field_name):
    with pytest.raises(ValueError):
        make_toolchain(**{field_name: ""})
    with pytest.raises(TypeError):
        make_toolchain(**{field_name: []})


@pytest.mark.parametrize("timeout", [True, 0, -1, math.nan, math.inf, "1"])
def test_ai_toolchain_timeout_is_finite_positive(timeout):
    with pytest.raises((TypeError, ValueError)):
        make_toolchain(timeout_seconds=timeout)


def test_canonical_toolchain_lane_only_normalizes_surrounding_whitespace():
    assert canonical_toolchain_lane_key(" tool ") == "ai-toolchain:tool"
    assert canonical_toolchain_lane_key("tool") == "ai-toolchain:tool"
    with pytest.raises(ValueError, match="toolchain_id must not be empty"):
        canonical_toolchain_lane_key(" \t ")
    with pytest.raises(TypeError):
        canonical_toolchain_lane_key(1)
    assert (
        make_toolchain(execution_revision="r1").runtime_lane_key
        == make_toolchain(execution_revision="r2").runtime_lane_key
    )


def test_job_rejects_forged_toolchain_lane():
    with pytest.raises(ValueError):
        make_job(snapshot=make_snapshot(ai_toolchain=make_toolchain(runtime_lane_key="x")))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        True,
        False,
        -(2**63),
        2**63 - 1,
        0.0,
        "value",
        (),
        FrozenResultObject(()),
        (1, "x", FrozenResultObject((("a", False),))),
    ],
)
def test_frozen_result_payload_accepts_exact_recursive_values(payload):
    assert validate_frozen_result_payload(payload) is None


@pytest.mark.parametrize(
    "payload, exception",
    [
        (2**63, ValueError),
        (-(2**63) - 1, ValueError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        ([], TypeError),
        ({}, TypeError),
        (set(), TypeError),
        (b"x", TypeError),
        (bytearray(b"x"), TypeError),
        (memoryview(b"x"), TypeError),
        (FullAudioPayloadProbe(), TypeError),
        (lambda: None, TypeError),
        (ValueError("x"), TypeError),
    ],
)
def test_frozen_result_payload_rejects_invalid_values(payload, exception):
    with pytest.raises(exception):
        validate_frozen_result_payload(payload)


def test_frozen_result_payload_rejects_numpy_values_and_scalar_subclasses():
    numpy = pytest.importorskip("numpy")

    class IntSubclass(int):
        pass

    class StrSubclass(str):
        pass

    for payload in (
        numpy.int64(1),
        numpy.float64(1.0),
        numpy.array([1.0]),
        IntSubclass(1),
        StrSubclass("x"),
    ):
        with pytest.raises(TypeError):
            validate_frozen_result_payload(payload)


def test_frozen_result_object_rejects_duplicate_and_invalid_keys():
    with pytest.raises(ValueError):
        FrozenResultObject((("x", 1), ("x", 2)))
    with pytest.raises(ValueError):
        FrozenResultObject((("", 1),))
    with pytest.raises(TypeError):
        FrozenResultObject((("x", 1), ["y", 2]))


def test_frozen_payload_boundaries():
    validate_frozen_result_payload("x" * MAX_FROZEN_RESULT_STRING_UTF8_BYTES)
    with pytest.raises(ValueError):
        validate_frozen_result_payload(
            "x" * (MAX_FROZEN_RESULT_STRING_UTF8_BYTES + 1)
        )
    value = None
    for _ in range(MAX_FROZEN_RESULT_DEPTH - 1):
        value = (value,)
    validate_frozen_result_payload(value)
    with pytest.raises(ValueError):
        validate_frozen_result_payload((value,))
    validate_frozen_result_payload(tuple(None for _ in range(MAX_FROZEN_RESULT_NODES - 1)))
    with pytest.raises(ValueError):
        validate_frozen_result_payload(
            tuple(None for _ in range(MAX_FROZEN_RESULT_NODES))
        )
    text = "x" * MAX_FROZEN_RESULT_STRING_UTF8_BYTES
    validate_frozen_result_payload(tuple(text for _ in range(64)))
    with pytest.raises(ValueError):
        validate_frozen_result_payload(tuple(text for _ in range(65)))


def test_frozen_payload_rejects_cycles_even_if_constructor_was_bypassed():
    forged = object.__new__(FrozenResultObject)
    object.__setattr__(forged, "items", (("self", forged),))
    with pytest.raises(ValueError):
        validate_frozen_result_payload(forged)


def test_frozen_payload_has_no_looks_like_audio_heuristic():
    validate_frozen_result_payload((0.0,) * 1000)
    validate_frozen_result_payload("base64-looking-audio-data")


def test_execution_result_coherence():
    assert AnalysisExecutionResult(
        AnalysisExecutionStatus.SUCCEEDED, FrozenResultObject(()), None
    )
    assert AnalysisExecutionResult(AnalysisExecutionStatus.NO_RESULT, None, None)
    assert AnalysisExecutionResult(
        AnalysisExecutionStatus.NO_RESULT, None, "not detected"
    )
    invalid = [
        (AnalysisExecutionStatus.SUCCEEDED, None, None),
        (AnalysisExecutionStatus.SUCCEEDED, (), "summary"),
        (AnalysisExecutionStatus.NO_RESULT, (), None),
        (AnalysisExecutionStatus.NO_RESULT, None, ""),
        ("succeeded", (), None),
    ]
    for args in invalid:
        with pytest.raises((TypeError, ValueError)):
            AnalysisExecutionResult(*args)


@pytest.mark.parametrize("worker_count", range(1, 7))
def test_scheduler_config_accepts_worker_counts_one_through_six(worker_count):
    assert AnalysisSchedulerConfig(worker_count=worker_count).worker_count == worker_count


@pytest.mark.parametrize("worker_count", [0, 7, True, 2.0, "2"])
def test_scheduler_config_rejects_invalid_worker_counts(worker_count):
    with pytest.raises((TypeError, ValueError)):
        AnalysisSchedulerConfig(worker_count=worker_count)


@pytest.mark.parametrize(
    "changes",
    [
        {"pending_capacity": 0},
        {"pending_capacity": True},
        {"terminal_receipt_capacity": 0},
        {"terminal_receipt_capacity": 2.0},
        {"shutdown_timeout_seconds": 0},
        {"shutdown_timeout_seconds": True},
        {"shutdown_timeout_seconds": math.nan},
        {"shutdown_timeout_seconds": math.inf},
    ],
)
def test_scheduler_config_rejects_invalid_capacities_and_timeout(changes):
    with pytest.raises((TypeError, ValueError)):
        AnalysisSchedulerConfig(**changes)


@pytest.mark.parametrize("kind", list(AnalysisKind))
def test_job_accepts_all_analysis_kinds(kind):
    assert make_job(kind=kind).kind is kind


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": ""},
        {"run_id": MutableContextProbe()},
        {"analysis_attempt_id": ""},
        {"kind": "automatic"},
        {"audio": object()},
        {"snapshot": object()},
        {"execution_timeout_seconds": True},
        {"execution_timeout_seconds": 0},
        {"execution_timeout_seconds": math.inf},
        {"queue_timeout_seconds": True},
        {"queue_timeout_seconds": 0},
        {"queue_timeout_seconds": math.nan},
    ],
)
def test_job_rejects_invalid_identity_context_and_timeouts(changes):
    with pytest.raises((TypeError, ValueError)):
        make_job(**changes)


def test_reanalysis_and_imported_jobs_keep_distinct_id_rules():
    original = make_job()
    reanalysis = make_job(
        kind=AnalysisKind.REANALYSIS,
        run_id=original.run_id,
        analysis_attempt_id="attempt-2",
    )
    imported = make_job(
        kind=AnalysisKind.IMPORTED,
        run_id="run-imported",
        analysis_attempt_id="attempt-imported",
    )
    assert reanalysis.run_id == original.run_id
    assert reanalysis.analysis_attempt_id != original.analysis_attempt_id
    assert imported.run_id != original.run_id
    assert imported.analysis_attempt_id != original.analysis_attempt_id


LEGAL_ERROR_PAIRS = {
    AnalysisErrorCategory.INVALID_JOB: {AnalysisStage.ADMISSION, AnalysisStage.CANCEL},
    AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID: {AnalysisStage.ADMISSION},
    AnalysisErrorCategory.ANALYSIS_QUEUE_FULL: {AnalysisStage.ADMISSION},
    AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING: {AnalysisStage.ADMISSION},
    AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT: {AnalysisStage.QUEUE},
    AnalysisErrorCategory.ATTEMPT_START_FAILED: {AnalysisStage.START_GATE},
    AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE: {AnalysisStage.AUDIO_LOAD},
    AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED: {AnalysisStage.AUDIO_LOAD},
    AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH: {AnalysisStage.AUDIO_LOAD},
    AnalysisErrorCategory.AUDIO_LOAD_FAILED: {AnalysisStage.AUDIO_LOAD},
    AnalysisErrorCategory.ANALYSIS_TIMEOUT: {AnalysisStage.EXECUTE},
    AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED: {AnalysisStage.EXECUTE},
    AnalysisErrorCategory.ANALYSIS_NO_RESULT: {AnalysisStage.EXECUTE},
    AnalysisErrorCategory.ANALYSIS_CANCELED: {AnalysisStage.CANCEL},
    AnalysisErrorCategory.OUTCOME_SINK_FAILED: {AnalysisStage.OUTCOME_SINK},
    AnalysisErrorCategory.SHUTDOWN_INCOMPLETE: {AnalysisStage.SHUTDOWN},
}


def make_error(
    category=AnalysisErrorCategory.INVALID_JOB,
    stage=AnalysisStage.ADMISSION,
    summary="summary",
    details=(),
):
    return AnalysisError(category, stage, summary, details)


def test_analysis_error_accepts_only_legal_category_stage_pairs():
    for category, legal_stages in LEGAL_ERROR_PAIRS.items():
        for stage in AnalysisStage:
            if stage in legal_stages:
                assert make_error(category, stage)
            else:
                with pytest.raises(ValueError):
                    make_error(category, stage)


def test_analysis_error_bounded_details():
    make_error(summary="x" * MAX_ERROR_SUMMARY_UTF8_BYTES)
    make_error(
        details=tuple(
            (f"k{i}", "v") for i in range(MAX_ERROR_DETAIL_ITEMS)
        )
    )
    invalid = [
        {"summary": ""},
        {"summary": "x" * (MAX_ERROR_SUMMARY_UTF8_BYTES + 1)},
        {"details": []},
        {"details": (("x", "1"), ("x", "2"))},
        {"details": (("", "v"),)},
        {"details": (("x" * (MAX_ERROR_DETAIL_KEY_UTF8_BYTES + 1), "v"),)},
        {"details": (("k", "v" * (MAX_ERROR_DETAIL_VALUE_UTF8_BYTES + 1)),)},
    ]
    for changes in invalid:
        with pytest.raises((TypeError, ValueError)):
            make_error(**changes)


def test_known_exceptions_are_positional_only_and_bounded():
    audio_error = make_error(
        AnalysisErrorCategory.AUDIO_LOAD_FAILED, AnalysisStage.AUDIO_LOAD
    )
    execute_error = make_error(
        AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED, AnalysisStage.EXECUTE
    )
    assert AudioLoadError(audio_error).error is audio_error
    assert AnalysisExecutionError(execute_error).error is execute_error
    with pytest.raises(TypeError):
        AudioLoadError(error=audio_error)
    with pytest.raises(ValueError):
        AudioLoadError(execute_error)
    with pytest.raises(ValueError):
        AnalysisExecutionError(audio_error)


def make_outcome(**changes):
    values = dict(
        run_id="run-1",
        analysis_attempt_id="attempt-1",
        kind=AnalysisKind.AUTOMATIC,
        status=AnalysisStatus.SUCCEEDED,
        audio_reference_id="audio-1",
        result_payload=FrozenResultObject(()),
        error=None,
        submitted_monotonic=1.0,
        started_monotonic=2.0,
        finished_monotonic=3.0,
        queue_wait_ms=1000,
        execution_ms=1000,
        cancel_origin=None,
    )
    values.update(changes)
    return AnalysisOutcome(**values)


def test_outcome_status_coherence_and_timing():
    make_outcome()
    make_outcome(
        status=AnalysisStatus.NO_RESULT,
        result_payload=None,
        error=make_error(
            AnalysisErrorCategory.ANALYSIS_NO_RESULT, AnalysisStage.EXECUTE
        ),
    )
    make_outcome(
        status=AnalysisStatus.FAILED,
        result_payload=None,
        error=make_error(
            AnalysisErrorCategory.ANALYSIS_TIMEOUT, AnalysisStage.EXECUTE
        ),
    )
    make_outcome(
        status=AnalysisStatus.CANCELED,
        result_payload=None,
        error=make_error(
            AnalysisErrorCategory.ANALYSIS_CANCELED, AnalysisStage.CANCEL
        ),
        cancel_origin=AnalysisCancelOrigin.USER_STOP,
    )
    make_outcome(
        status=AnalysisStatus.CANCELED,
        result_payload=None,
        error=make_error(
            AnalysisErrorCategory.ANALYSIS_CANCELED, AnalysisStage.CANCEL
        ),
        started_monotonic=None,
        execution_ms=None,
        cancel_origin=AnalysisCancelOrigin.RESULT_EVICTION,
    )
    invalid = [
        {"status": AnalysisStatus.SUCCEEDED, "result_payload": None},
        {
            "status": AnalysisStatus.NO_RESULT,
            "result_payload": None,
            "error": None,
        },
        {
            "status": AnalysisStatus.FAILED,
            "result_payload": None,
            "error": make_error(AnalysisErrorCategory.INVALID_JOB, AnalysisStage.ADMISSION),
        },
        {"started_monotonic": None, "execution_ms": 1},
        {"submitted_monotonic": 2.1, "started_monotonic": 2.0},
        {"finished_monotonic": 1.9},
        {"queue_wait_ms": True},
    ]
    for changes in invalid:
        with pytest.raises((TypeError, ValueError)):
            make_outcome(**changes)
    with pytest.raises(ValueError):
        make_outcome(
            status=AnalysisStatus.FAILED,
            result_payload=None,
            error=make_error(
                AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                AnalysisStage.AUDIO_LOAD,
            ),
            execution_ms=1,
        )
    with pytest.raises(ValueError):
        make_outcome(
            status=AnalysisStatus.FAILED,
            result_payload=None,
            error=make_error(
                AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,
                AnalysisStage.QUEUE,
            ),
            started_monotonic=2.0,
            execution_ms=None,
        )


def test_submit_receipt_coherence():
    SubmitReceipt(SubmitStatus.ACCEPTED, "run", "attempt", 1, None)
    rejection_pairs = {
        SubmitStatus.INVALID_JOB: AnalysisErrorCategory.INVALID_JOB,
        SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID:
            AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
        SubmitStatus.QUEUE_FULL: AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
        SubmitStatus.NOT_ACCEPTING: AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
    }
    for status, category in rejection_pairs.items():
        SubmitReceipt(
            status,
            "run",
            "attempt",
            None,
            make_error(category, AnalysisStage.ADMISSION),
        )
    with pytest.raises((TypeError, ValueError)):
        SubmitReceipt(SubmitStatus.ACCEPTED, "run", "attempt", None, None)
    with pytest.raises(ValueError):
        SubmitReceipt(
            SubmitStatus.INVALID_JOB,
            "run",
            "attempt",
            1,
            make_error(AnalysisErrorCategory.INVALID_JOB, AnalysisStage.ADMISSION),
        )


def test_cancel_receipt_coherence_including_bounded_absence():
    for status, drain in (
        (CancelStatus.CANCELED_PENDING, False),
        (CancelStatus.CANCEL_REQUESTED_RUNNING, True),
        (CancelStatus.ALREADY_TERMINAL, False),
    ):
        CancelReceipt(status, "run", "attempt", drain, None)
    for status in (CancelStatus.NOT_FOUND, CancelStatus.RUN_ID_MISMATCH):
        CancelReceipt(
            status,
            "run",
            "attempt",
            False,
            make_error(AnalysisErrorCategory.INVALID_JOB, AnalysisStage.CANCEL),
        )
    with pytest.raises(ValueError):
        CancelReceipt(
            CancelStatus.NOT_FOUND,
            "run",
            "attempt",
            True,
            make_error(AnalysisErrorCategory.INVALID_JOB, AnalysisStage.CANCEL),
        )


def make_shutdown_report(**changes):
    values = dict(
        status=ShutdownStatus.CLOSED,
        started_monotonic=1.0,
        finished_monotonic=2.0,
        canceled_pending=0,
        cancel_requested_running=0,
        remaining_attempt_ids=(),
        remaining_worker_ids=(),
        remaining_run_lanes=(),
        remaining_toolchain_lanes=(),
        control_thread_alive=False,
    )
    values.update(changes)
    return ShutdownReport(**values)


def test_shutdown_report_status_and_sorted_resources():
    make_shutdown_report()
    make_shutdown_report(
        status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
        remaining_attempt_ids=("a", "b"),
        remaining_worker_ids=(1, 2),
        remaining_run_lanes=("r1", "r2"),
        remaining_toolchain_lanes=("ai-toolchain:a", "ai-toolchain:b"),
    )
    with pytest.raises(ValueError):
        make_shutdown_report(remaining_attempt_ids=("a",))
    with pytest.raises(ValueError):
        make_shutdown_report(status=ShutdownStatus.SHUTDOWN_INCOMPLETE)
    with pytest.raises(ValueError):
        make_shutdown_report(
            status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
            remaining_attempt_ids=("b", "a"),
        )


def test_shutdown_report_requires_exact_control_thread_liveness():
    make_shutdown_report(control_thread_alive=False)
    with pytest.raises(
        TypeError,
        match="control_thread_alive must be an exact bool",
    ):
        make_shutdown_report(control_thread_alive=0)


def test_shutdown_report_closed_requires_no_live_control_or_resources():
    with pytest.raises(
        ValueError,
        match="CLOSED report cannot contain remaining resources",
    ):
        make_shutdown_report(control_thread_alive=True)
    with pytest.raises(
        ValueError,
        match="CLOSED report cannot contain remaining resources",
    ):
        make_shutdown_report(
            control_thread_alive=False,
            remaining_worker_ids=(1,),
        )


@pytest.mark.parametrize(
    "remaining",
    [
        {"control_thread_alive": True},
        {
            "control_thread_alive": False,
            "remaining_attempt_ids": ("attempt",),
        },
        {
            "control_thread_alive": False,
            "remaining_worker_ids": (1,),
        },
        {
            "control_thread_alive": False,
            "remaining_run_lanes": ("run",),
        },
        {
            "control_thread_alive": False,
            "remaining_toolchain_lanes": ("ai-toolchain:tool",),
        },
        {
            "control_thread_alive": True,
            "remaining_worker_ids": (1,),
        },
    ],
)
def test_shutdown_incomplete_accepts_control_or_existing_remaining_fact(
    remaining,
):
    make_shutdown_report(
        status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
        **remaining,
    )


def test_shutdown_incomplete_rejects_false_control_and_empty_resources():
    with pytest.raises(
        ValueError,
        match="SHUTDOWN_INCOMPLETE report requires remaining resources",
    ):
        make_shutdown_report(
            status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
            control_thread_alive=False,
        )


@pytest.mark.parametrize(
    "remaining",
    [
        {},
        {
            "control_thread_alive": True,
            "remaining_attempt_ids": ("attempt-a", "attempt-b"),
            "remaining_worker_ids": (1, 6),
            "remaining_run_lanes": ("run-a", "run-b"),
            "remaining_toolchain_lanes": (
                "ai-toolchain:tool-a",
                "ai-toolchain:tool-b",
            ),
        },
    ],
)
def test_shutdown_in_progress_accepts_empty_or_nonempty_remaining_snapshot(
    remaining,
):
    report = make_shutdown_report(
        status=ShutdownStatus.IN_PROGRESS,
        **remaining,
    )

    assert report.status is ShutdownStatus.IN_PROGRESS


@pytest.mark.parametrize(
    ("changes", "exception"),
    [
        ({"started_monotonic": 1}, TypeError),
        ({"finished_monotonic": 2}, TypeError),
        ({"started_monotonic": 3.0}, ValueError),
        ({"canceled_pending": True}, TypeError),
        ({"cancel_requested_running": 1.0}, TypeError),
        ({"remaining_attempt_ids": ["attempt"]}, TypeError),
        ({"remaining_attempt_ids": ("b", "a")}, ValueError),
        ({"remaining_attempt_ids": ("a", "a")}, ValueError),
        ({"remaining_worker_ids": (7,)}, ValueError),
        ({"remaining_run_lanes": ("b", "a")}, ValueError),
        (
            {"remaining_toolchain_lanes": ("external-toolchain",)},
            ValueError,
        ),
        ({"control_thread_alive": 0}, TypeError),
    ],
)
def test_shutdown_in_progress_retains_exact_snapshot_validation(
    changes,
    exception,
):
    with pytest.raises(exception):
        make_shutdown_report(
            status=ShutdownStatus.IN_PROGRESS,
            **changes,
        )


@pytest.mark.parametrize(
    ("field_name", "oversized_value"),
    [
        (
            "remaining_attempt_ids",
            tuple(f"attempt-{index}" for index in range(7)),
        ),
        ("remaining_worker_ids", tuple(range(1, 8))),
        (
            "remaining_run_lanes",
            tuple(f"run-{index}" for index in range(7)),
        ),
        (
            "remaining_toolchain_lanes",
            tuple(f"ai-toolchain:tool-{index}" for index in range(7)),
        ),
    ],
)
def test_shutdown_report_rejects_each_oversized_remaining_resource_tuple(
    field_name,
    oversized_value,
):
    with pytest.raises(ValueError):
        make_shutdown_report(
            status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
            **{field_name: oversized_value},
        )


def test_shutdown_report_rejects_more_than_six_running_cancel_requests():
    with pytest.raises(ValueError):
        make_shutdown_report(
            status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
            cancel_requested_running=7,
            remaining_attempt_ids=("attempt",),
        )


def test_shutdown_report_rejects_combined_oversized_remaining_resources():
    with pytest.raises(ValueError):
        make_shutdown_report(
            status=ShutdownStatus.SHUTDOWN_INCOMPLETE,
            cancel_requested_running=7,
            remaining_attempt_ids=tuple(
                f"attempt-{index}" for index in range(7)
            ),
            remaining_worker_ids=tuple(range(1, 8)),
            remaining_run_lanes=tuple(f"run-{index}" for index in range(7)),
            remaining_toolchain_lanes=tuple(
                f"ai-toolchain:tool-{index}" for index in range(7)
            ),
        )


def _counter_tuple(**overrides):
    values = {key.value: 0 for key in SchedulerCounterKey}
    values.update(overrides)
    return tuple(values.items())


def make_attempt(**changes):
    values = dict(
        run_id="run",
        analysis_attempt_id="attempt",
        kind=AnalysisKind.AUTOMATIC,
        state=AttemptState.PENDING,
        submission_sequence=1,
        worker_id=None,
        runtime_lane_key=None,
        wait_ms=0,
        execution_ms=None,
    )
    values.update(changes)
    return AttemptDiagnostic(**values)


def make_diagnostics(**changes):
    attempts = changes.pop("attempts", ())
    values = dict(
        lifecycle=SchedulerLifecycle.RUNNING,
        worker_count=2,
        pending_capacity=64,
        terminal_receipt_capacity=256,
        pending=sum(a.state is AttemptState.PENDING for a in attempts),
        running=sum(a.state is AttemptState.RUNNING for a in attempts),
        cancel_requested=sum(
            a.state is AttemptState.CANCEL_REQUESTED for a in attempts
        ),
        full_audio_residency_count=0,
        peak_full_audio_residency_count=0,
        peak_running_workers=(
            sum(
                a.state in (AttemptState.RUNNING, AttemptState.CANCEL_REQUESTED)
                for a in attempts
            )
        ),
        pending_by_kind=(
            (AnalysisKind.AUTOMATIC, 0),
            (AnalysisKind.REANALYSIS, 0),
            (AnalysisKind.IMPORTED, 0),
        ),
        oldest_pending_wait_ms=0,
        blocked_on_run_lane=0,
        blocked_on_toolchain_lane=0,
        attempts=attempts,
        toolchain_lanes=(),
        terminal_receipts=(),
        counters=_counter_tuple(),
    )
    if attempts:
        pending_counts = tuple(
            (kind, sum(a.state is AttemptState.PENDING and a.kind is kind for a in attempts))
            for kind in AnalysisKind
        )
        values["pending_by_kind"] = pending_counts
        values["oldest_pending_wait_ms"] = max(
            (a.wait_ms for a in attempts if a.state is AttemptState.PENDING),
            default=0,
        )
    values.update(changes)
    return SchedulerDiagnostics(**values)


def test_diagnostics_accepts_coherent_snapshot_and_exact_counter_order():
    make_diagnostics()
    with pytest.raises(ValueError):
        make_diagnostics(counters=tuple(reversed(_counter_tuple())))
    with pytest.raises(ValueError):
        make_diagnostics(counters=_counter_tuple(accepted=-1))
    with pytest.raises(ValueError):
        make_diagnostics(
            attempts=(make_attempt(),),
            pending=0,
            pending_by_kind=tuple((kind, 0) for kind in AnalysisKind),
        )
    with pytest.raises(ValueError):
        make_diagnostics(
            attempts=(
                make_attempt(
                    state=AttemptState.RUNNING, worker_id=1, execution_ms=0
                ),
            ),
            full_audio_residency_count=2,
        )
    with pytest.raises(ValueError):
        make_diagnostics(
            lifecycle=SchedulerLifecycle.CLOSED,
            attempts=(make_attempt(),),
        )


def test_diagnostics_rejects_lane_capacity_terminal_and_aggregate_contradictions():
    running = make_attempt(
        state=AttemptState.RUNNING,
        worker_id=1,
        runtime_lane_key="ai-toolchain:ai-1",
        execution_ms=0,
    )
    with pytest.raises(ValueError):
        make_diagnostics(
            attempts=(running,),
            toolchain_lanes=(),
            counters=_counter_tuple(accepted=1),
        )
    with pytest.raises(ValueError):
        make_diagnostics(
            pending_capacity=1,
            pending=2,
            attempts=(),
            pending_by_kind=tuple((kind, 0) for kind in AnalysisKind),
        )
    with pytest.raises(ValueError):
        make_diagnostics(
            terminal_receipt_capacity=1,
            terminal_receipts=(
                TerminalReceiptDiagnostic(
                    "run-1", "attempt-1", AnalysisStatus.SUCCEEDED, 2.0
                ),
                TerminalReceiptDiagnostic(
                    "run-2", "attempt-2", AnalysisStatus.SUCCEEDED, 1.0
                ),
            ),
        )
    with pytest.raises(ValueError):
        make_diagnostics(counters=_counter_tuple(failed=1))


@pytest.mark.parametrize(
    "second_state",
    [AttemptState.RUNNING, AttemptState.CANCEL_REQUESTED],
)
def test_diagnostics_rejects_duplicate_run_id_among_live_worker_owners(
    second_state,
):
    attempts = (
        make_attempt(
            run_id="same-run",
            analysis_attempt_id="attempt-1",
            state=AttemptState.RUNNING,
            submission_sequence=1,
            worker_id=1,
            execution_ms=0,
        ),
        make_attempt(
            run_id="same-run",
            analysis_attempt_id="attempt-2",
            state=second_state,
            submission_sequence=2,
            worker_id=2,
            execution_ms=0,
        ),
    )
    with pytest.raises(ValueError):
        make_diagnostics(
            attempts=attempts,
            counters=_counter_tuple(accepted=2),
        )


def test_diagnostics_rejects_attempt_id_present_in_active_and_terminal_sets():
    active = make_attempt(
        run_id="run-1",
        analysis_attempt_id="shared-attempt",
    )
    terminal = TerminalReceiptDiagnostic(
        "run-1",
        "shared-attempt",
        AnalysisStatus.SUCCEEDED,
        1.0,
    )
    with pytest.raises(ValueError):
        make_diagnostics(
            attempts=(active,),
            terminal_receipts=(terminal,),
            counters=_counter_tuple(accepted=2, succeeded=1),
        )


def test_attempt_lane_and_terminal_diagnostics_are_strict():
    make_attempt()
    make_attempt(
        state=AttemptState.RUNNING,
        worker_id=1,
        runtime_lane_key="ai-toolchain:ai-1",
        execution_ms=0,
    )
    with pytest.raises(ValueError):
        make_attempt(state=AttemptState.PENDING, worker_id=1)
    with pytest.raises(ValueError):
        make_attempt(runtime_lane_key="forged")
    ToolchainLaneDiagnostic("ai-toolchain:ai-1", 1, 0)
    with pytest.raises(ValueError):
        ToolchainLaneDiagnostic("ai-toolchain:ai-1", 0, 0)
    TerminalReceiptDiagnostic("run", "attempt", AnalysisStatus.SUCCEEDED, 1.0)


def make_event(kind, **changes):
    values = dict(event_kind=kind, occurred_monotonic=1.0)
    values.update(changes)
    return SchedulerDiagnosticEvent(**values)


FULL_ID = dict(
    run_id="run",
    analysis_attempt_id="attempt",
    analysis_kind=AnalysisKind.AUTOMATIC,
    audio_reference_id="audio",
    submission_sequence=1,
    runtime_lane_key=None,
)


def test_each_diagnostic_event_matrix_has_a_valid_row():
    make_event(
        SchedulerDiagnosticEventKind.SUBMIT_ACCEPTED,
        **FULL_ID,
        state_after=AttemptState.PENDING,
        queue_wait_ms=0,
    )
    make_event(
        SchedulerDiagnosticEventKind.SUBMIT_REJECTED,
        error_category=AnalysisErrorCategory.INVALID_JOB,
        error_stage=AnalysisStage.ADMISSION,
        summary="invalid",
    )
    make_event(
        SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED,
        **FULL_ID,
        state_before=AttemptState.PENDING,
        state_after=AttemptState.RUNNING,
        worker_id=1,
        queue_wait_ms=0,
    )
    make_event(
        SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
        **FULL_ID,
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.CANCEL_REQUESTED,
        worker_id=1,
        queue_wait_ms=0,
        error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
        error_stage=AnalysisStage.CANCEL,
        summary="stop",
    )
    make_event(
        SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
        **FULL_ID,
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.SUCCEEDED,
        worker_id=1,
        queue_wait_ms=0,
        load_ms=0,
        execution_ms=0,
    )
    make_event(
        SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
        **FULL_ID,
        state_before=AttemptState.SUCCEEDED,
        state_after=AttemptState.SUCCEEDED,
        queue_wait_ms=0,
        load_ms=0,
        execution_ms=0,
        error_category=AnalysisErrorCategory.OUTCOME_SINK_FAILED,
        error_stage=AnalysisStage.OUTCOME_SINK,
        summary="sink failed",
    )
    make_event(
        SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
        lifecycle_before=SchedulerLifecycle.RUNNING,
        lifecycle_after=SchedulerLifecycle.SHUTTING_DOWN,
    )


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (AnalysisErrorCategory.INVALID_JOB, SubmitStatus.INVALID_JOB),
        (
            AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
            SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID,
        ),
        (AnalysisErrorCategory.ANALYSIS_QUEUE_FULL, SubmitStatus.QUEUE_FULL),
        (
            AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
            SubmitStatus.NOT_ACCEPTING,
        ),
    ],
)
def test_submit_rejected_event_accepts_every_admission_error_row(category, status):
    del status
    make_event(
        SchedulerDiagnosticEventKind.SUBMIT_REJECTED,
        run_id="",
        analysis_attempt_id=None,
        analysis_kind=AnalysisKind.IMPORTED,
        audio_reference_id=None,
        error_category=category,
        error_stage=AnalysisStage.ADMISSION,
        summary="rejected",
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(
            state_before=AttemptState.PENDING,
            state_after=AttemptState.FAILED,
            worker_id=None,
            load_ms=None,
            execution_ms=None,
            error_category=AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,
            error_stage=AnalysisStage.QUEUE,
            summary="timed out",
        ),
        dict(
            state_before=AttemptState.PENDING,
            state_after=AttemptState.CANCELED,
            worker_id=None,
            load_ms=None,
            execution_ms=None,
            error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
            error_stage=AnalysisStage.CANCEL,
            summary="canceled",
        ),
        dict(
            state_before=AttemptState.RUNNING,
            state_after=AttemptState.SUCCEEDED,
            worker_id=1,
            load_ms=0,
            execution_ms=0,
        ),
        dict(
            state_before=AttemptState.RUNNING,
            state_after=AttemptState.NO_RESULT,
            worker_id=1,
            load_ms=0,
            execution_ms=0,
            error_category=AnalysisErrorCategory.ANALYSIS_NO_RESULT,
            error_stage=AnalysisStage.EXECUTE,
            summary="no result",
        ),
        dict(
            state_before=AttemptState.RUNNING,
            state_after=AttemptState.FAILED,
            worker_id=1,
            load_ms=0,
            execution_ms=None,
            error_category=AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
            error_stage=AnalysisStage.AUDIO_LOAD,
            summary="unreadable",
        ),
        dict(
            state_before=AttemptState.RUNNING,
            state_after=AttemptState.FAILED,
            worker_id=1,
            load_ms=0,
            execution_ms=0,
            error_category=AnalysisErrorCategory.ANALYSIS_TIMEOUT,
            error_stage=AnalysisStage.EXECUTE,
            summary="timed out",
        ),
        dict(
            state_before=AttemptState.CANCEL_REQUESTED,
            state_after=AttemptState.CANCELED,
            worker_id=1,
            load_ms=None,
            execution_ms=None,
            error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
            error_stage=AnalysisStage.CANCEL,
            summary="canceled",
        ),
    ],
)
def test_terminal_event_accepts_every_local_matrix_row(changes):
    make_event(
        SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
        **FULL_ID,
        queue_wait_ms=0,
        **changes,
    )


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        (
            SchedulerLifecycle.RUNNING,
            SchedulerLifecycle.SHUTTING_DOWN,
            {},
        ),
        (
            SchedulerLifecycle.SHUTTING_DOWN,
            SchedulerLifecycle.CLOSED,
            {},
        ),
        (
            SchedulerLifecycle.SHUTTING_DOWN,
            SchedulerLifecycle.SHUTDOWN_INCOMPLETE,
            dict(
                error_category=AnalysisErrorCategory.SHUTDOWN_INCOMPLETE,
                error_stage=AnalysisStage.SHUTDOWN,
                summary="analysis scheduler shutdown incomplete",
            ),
        ),
    ],
)
def test_lifecycle_event_accepts_every_transition(before, after, error):
    make_event(
        SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
        lifecycle_before=before,
        lifecycle_after=after,
        **error,
    )


def _assert_event_mutation_rejected(kind, valid, mutation):
    hostile = dict(valid)
    hostile.update(mutation)
    with pytest.raises((TypeError, ValueError)):
        make_event(kind, **hostile)


SUBMIT_ACCEPTED_EVENT = dict(
    **FULL_ID,
    state_after=AttemptState.PENDING,
    queue_wait_ms=0,
)
SUBMIT_REJECTED_EVENT = dict(
    error_category=AnalysisErrorCategory.INVALID_JOB,
    error_stage=AnalysisStage.ADMISSION,
    summary="invalid",
)
DISPATCHED_EVENT = dict(
    **FULL_ID,
    state_before=AttemptState.PENDING,
    state_after=AttemptState.RUNNING,
    worker_id=1,
    queue_wait_ms=0,
)
CANCEL_REQUESTED_EVENT = dict(
    **FULL_ID,
    state_before=AttemptState.RUNNING,
    state_after=AttemptState.CANCEL_REQUESTED,
    worker_id=1,
    queue_wait_ms=0,
    error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
    error_stage=AnalysisStage.CANCEL,
    summary="stop",
)
TERMINAL_SUCCESS_EVENT = dict(
    **FULL_ID,
    state_before=AttemptState.RUNNING,
    state_after=AttemptState.SUCCEEDED,
    worker_id=1,
    queue_wait_ms=0,
    load_ms=0,
    execution_ms=0,
)
SINK_FAILED_EVENT = dict(
    **FULL_ID,
    state_before=AttemptState.SUCCEEDED,
    state_after=AttemptState.SUCCEEDED,
    queue_wait_ms=0,
    load_ms=0,
    execution_ms=0,
    error_category=AnalysisErrorCategory.OUTCOME_SINK_FAILED,
    error_stage=AnalysisStage.OUTCOME_SINK,
    summary="sink failed",
)
LIFECYCLE_EVENT = dict(
    lifecycle_before=SchedulerLifecycle.RUNNING,
    lifecycle_after=SchedulerLifecycle.SHUTTING_DOWN,
)


class ForeignAttemptState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"


class ForeignSchedulerLifecycle(str, Enum):
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"


class DeceptiveEquality:
    def __init__(self):
        self.mutable_state = []

    def __eq__(self, other):
        return other is None


@pytest.mark.parametrize(
    ("kind", "valid", "field_name", "raw_value", "foreign_value"),
    [
        (
            SchedulerDiagnosticEventKind.SUBMIT_ACCEPTED,
            SUBMIT_ACCEPTED_EVENT,
            "state_after",
            "pending",
            ForeignAttemptState.PENDING,
        ),
        (
            SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED,
            DISPATCHED_EVENT,
            "state_before",
            "pending",
            ForeignAttemptState.PENDING,
        ),
        (
            SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED,
            DISPATCHED_EVENT,
            "state_after",
            "running",
            ForeignAttemptState.RUNNING,
        ),
        (
            SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
            CANCEL_REQUESTED_EVENT,
            "state_before",
            "running",
            ForeignAttemptState.RUNNING,
        ),
        (
            SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
            CANCEL_REQUESTED_EVENT,
            "state_after",
            "cancel_requested",
            ForeignAttemptState.CANCEL_REQUESTED,
        ),
        (
            SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
            SINK_FAILED_EVENT,
            "state_before",
            "succeeded",
            ForeignAttemptState.SUCCEEDED,
        ),
        (
            SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
            SINK_FAILED_EVENT,
            "state_after",
            "succeeded",
            ForeignAttemptState.SUCCEEDED,
        ),
        (
            SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
            LIFECYCLE_EVENT,
            "lifecycle_before",
            "running",
            ForeignSchedulerLifecycle.RUNNING,
        ),
        (
            SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
            LIFECYCLE_EVENT,
            "lifecycle_after",
            "shutting_down",
            ForeignSchedulerLifecycle.SHUTTING_DOWN,
        ),
    ],
)
def test_event_state_and_lifecycle_fields_require_exact_enum_types(
    kind,
    valid,
    field_name,
    raw_value,
    foreign_value,
):
    for hostile_value in (raw_value, foreign_value):
        _assert_event_mutation_rejected(
            kind,
            valid,
            {field_name: hostile_value},
        )


@pytest.mark.parametrize(
    "hostile_state",
    ["succeeded", ForeignAttemptState.SUCCEEDED],
)
def test_sink_failed_event_rejects_matching_non_exact_terminal_states(
    hostile_state,
):
    _assert_event_mutation_rejected(
        SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
        SINK_FAILED_EVENT,
        {
            "state_before": hostile_state,
            "state_after": hostile_state,
        },
    )


@pytest.mark.parametrize(
    ("kind", "valid", "mutation"),
    [
        *[
            (
                SchedulerDiagnosticEventKind.SUBMIT_ACCEPTED,
                SUBMIT_ACCEPTED_EVENT,
                mutation,
            )
            for mutation in (
                {"run_id": None},
                {"analysis_attempt_id": None},
                {"analysis_kind": None},
                {"audio_reference_id": None},
                {"submission_sequence": None},
                {"runtime_lane_key": "forged"},
                {"state_before": AttemptState.PENDING},
                {"state_after": AttemptState.RUNNING},
                {"worker_id": 1},
                {"queue_wait_ms": 1},
                {"load_ms": 0},
                {"execution_ms": 0, "load_ms": 0},
                {"error_category": AnalysisErrorCategory.INVALID_JOB},
                {"error_stage": AnalysisStage.ADMISSION},
                {"summary": "forbidden"},
                {"details": (("key", "value"),)},
                {"lifecycle_before": SchedulerLifecycle.RUNNING},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
        *[
            (
                SchedulerDiagnosticEventKind.SUBMIT_REJECTED,
                SUBMIT_REJECTED_EVENT,
                mutation,
            )
            for mutation in (
                {"runtime_lane_key": "forged"},
                {"submission_sequence": 1},
                {"state_before": AttemptState.PENDING},
                {"state_after": AttemptState.PENDING},
                {"worker_id": 1},
                {"queue_wait_ms": 0},
                {"load_ms": 0},
                {"execution_ms": 0, "load_ms": 0},
                {"error_category": None},
                {"error_category": AnalysisErrorCategory.ANALYSIS_CANCELED},
                {"error_stage": None},
                {"error_stage": AnalysisStage.CANCEL},
                {"summary": None},
                {"details": (("key", "value"),)},
                {"lifecycle_before": SchedulerLifecycle.RUNNING},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
        *[
            (
                SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED,
                DISPATCHED_EVENT,
                mutation,
            )
            for mutation in (
                {"run_id": None},
                {"analysis_attempt_id": None},
                {"analysis_kind": None},
                {"audio_reference_id": None},
                {"submission_sequence": None},
                {"runtime_lane_key": "forged"},
                {"state_before": None},
                {"state_after": AttemptState.CANCEL_REQUESTED},
                {"worker_id": None},
                {"queue_wait_ms": None},
                {"load_ms": 0},
                {"execution_ms": 0, "load_ms": 0},
                {"error_category": AnalysisErrorCategory.INVALID_JOB},
                {"error_stage": AnalysisStage.ADMISSION},
                {"summary": "forbidden"},
                {"details": (("key", "value"),)},
                {"lifecycle_before": SchedulerLifecycle.RUNNING},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
        *[
            (
                SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
                CANCEL_REQUESTED_EVENT,
                mutation,
            )
            for mutation in (
                {"run_id": None},
                {"analysis_attempt_id": None},
                {"analysis_kind": None},
                {"audio_reference_id": None},
                {"submission_sequence": None},
                {"runtime_lane_key": "forged"},
                {"state_before": AttemptState.PENDING},
                {"state_after": AttemptState.CANCELED},
                {"worker_id": None},
                {"queue_wait_ms": None},
                {"execution_ms": 0, "load_ms": None},
                {"error_category": AnalysisErrorCategory.INVALID_JOB},
                {"error_stage": AnalysisStage.ADMISSION},
                {"summary": None},
                {"details": (("key", "value"),)},
                {"lifecycle_before": SchedulerLifecycle.RUNNING},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
        *[
            (
                SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
                TERMINAL_SUCCESS_EVENT,
                mutation,
            )
            for mutation in (
                {"run_id": None},
                {"analysis_attempt_id": None},
                {"analysis_kind": None},
                {"audio_reference_id": None},
                {"submission_sequence": None},
                {"runtime_lane_key": "forged"},
                {"state_before": None},
                {"state_after": None},
                {"queue_wait_ms": None},
                {"lifecycle_before": SchedulerLifecycle.RUNNING},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
        *[
            (
                SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
                SINK_FAILED_EVENT,
                mutation,
            )
            for mutation in (
                {"run_id": None},
                {"analysis_attempt_id": None},
                {"analysis_kind": None},
                {"audio_reference_id": None},
                {"submission_sequence": None},
                {"runtime_lane_key": "forged"},
                {"state_before": AttemptState.RUNNING},
                {"state_after": AttemptState.FAILED},
                {"worker_id": 1},
                {"queue_wait_ms": None},
                {"execution_ms": 0, "load_ms": None},
                {"error_category": AnalysisErrorCategory.ANALYSIS_CANCELED},
                {"error_stage": AnalysisStage.CANCEL},
                {"summary": None},
                {"details": (("key", "value"),)},
                {"lifecycle_before": SchedulerLifecycle.RUNNING},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
        *[
            (
                SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
                LIFECYCLE_EVENT,
                mutation,
            )
            for mutation in (
                {"run_id": "run"},
                {"analysis_attempt_id": "attempt"},
                {"analysis_kind": AnalysisKind.AUTOMATIC},
                {"audio_reference_id": "audio"},
                {"submission_sequence": 1},
                {"state_before": AttemptState.PENDING},
                {"state_after": AttemptState.CANCELED},
                {"worker_id": 1},
                {"runtime_lane_key": "ai-toolchain:ai"},
                {"queue_wait_ms": 0},
                {"load_ms": 0},
                {"execution_ms": 0, "load_ms": 0},
                {"error_category": AnalysisErrorCategory.INVALID_JOB},
                {"error_stage": AnalysisStage.ADMISSION},
                {"summary": "forbidden"},
                {"details": (("key", "value"),)},
                {"lifecycle_before": SchedulerLifecycle.CLOSED},
                {"lifecycle_after": SchedulerLifecycle.CLOSED},
            )
        ],
    ],
)
def test_diagnostic_event_kinds_reject_every_required_forbidden_or_fixed_mutation(
    kind,
    valid,
    mutation,
):
    _assert_event_mutation_rejected(kind, valid, mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        {"error_category": None},
        {"error_category": AnalysisErrorCategory.INVALID_JOB},
        {"error_stage": None},
        {"error_stage": AnalysisStage.ADMISSION},
        {"summary": None},
        {"summary": "wrong fixed summary"},
        {"details": (("key", "value"),)},
    ],
)
def test_incomplete_lifecycle_event_rejects_every_fixed_error_mutation(mutation):
    valid = dict(
        lifecycle_before=SchedulerLifecycle.SHUTTING_DOWN,
        lifecycle_after=SchedulerLifecycle.SHUTDOWN_INCOMPLETE,
        error_category=AnalysisErrorCategory.SHUTDOWN_INCOMPLETE,
        error_stage=AnalysisStage.SHUTDOWN,
        summary="analysis scheduler shutdown incomplete",
    )
    _assert_event_mutation_rejected(
        SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
        valid,
        mutation,
    )


TERMINAL_EVENT_ROWS = [
    dict(
        state_before=AttemptState.PENDING,
        state_after=AttemptState.FAILED,
        worker_id=None,
        load_ms=None,
        execution_ms=None,
        error_category=AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,
        error_stage=AnalysisStage.QUEUE,
        summary="timed out",
    ),
    dict(
        state_before=AttemptState.PENDING,
        state_after=AttemptState.CANCELED,
        worker_id=None,
        load_ms=None,
        execution_ms=None,
        error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
        error_stage=AnalysisStage.CANCEL,
        summary="canceled",
    ),
    dict(
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.SUCCEEDED,
        worker_id=1,
        load_ms=0,
        execution_ms=0,
    ),
    dict(
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.NO_RESULT,
        worker_id=1,
        load_ms=0,
        execution_ms=0,
        error_category=AnalysisErrorCategory.ANALYSIS_NO_RESULT,
        error_stage=AnalysisStage.EXECUTE,
        summary="no result",
    ),
    dict(
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.FAILED,
        worker_id=1,
        load_ms=None,
        execution_ms=None,
        error_category=AnalysisErrorCategory.ATTEMPT_START_FAILED,
        error_stage=AnalysisStage.START_GATE,
        summary="start failed",
        details=(("code", AnalysisStartGateCode.ATTEMPT_START_DB_FAILED.value),),
    ),
    dict(
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.FAILED,
        worker_id=1,
        load_ms=0,
        execution_ms=None,
        error_category=AnalysisErrorCategory.AUDIO_LOAD_FAILED,
        error_stage=AnalysisStage.AUDIO_LOAD,
        summary="load failed",
    ),
    dict(
        state_before=AttemptState.RUNNING,
        state_after=AttemptState.FAILED,
        worker_id=1,
        load_ms=0,
        execution_ms=0,
        error_category=AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
        error_stage=AnalysisStage.EXECUTE,
        summary="execute failed",
    ),
    dict(
        state_before=AttemptState.CANCEL_REQUESTED,
        state_after=AttemptState.CANCELED,
        worker_id=1,
        load_ms=0,
        execution_ms=0,
        error_category=AnalysisErrorCategory.ANALYSIS_CANCELED,
        error_stage=AnalysisStage.CANCEL,
        summary="canceled",
    ),
]
TERMINAL_EVENT_HOSTILE_MUTATIONS = [
    [
        {"worker_id": 1},
        {"load_ms": 0},
        {"execution_ms": 0, "load_ms": 0},
        {"error_category": AnalysisErrorCategory.ANALYSIS_CANCELED},
        {"error_stage": AnalysisStage.CANCEL},
        {"summary": None},
    ],
    [
        {"worker_id": 1},
        {"load_ms": 0},
        {"execution_ms": 0, "load_ms": 0},
        {"error_category": AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT},
        {"error_stage": AnalysisStage.QUEUE},
        {"summary": None},
    ],
    [
        {"worker_id": None},
        {"load_ms": None},
        {"execution_ms": None},
        {"error_category": AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED},
        {"error_stage": AnalysisStage.EXECUTE},
        {"summary": "forbidden"},
    ],
    [
        {"worker_id": None},
        {"load_ms": None},
        {"execution_ms": None},
        {"error_category": AnalysisErrorCategory.ANALYSIS_TIMEOUT},
        {"error_stage": AnalysisStage.CANCEL},
        {"summary": None},
    ],
    [
        {"worker_id": None},
        {"load_ms": 0},
        {"execution_ms": 0},
        {"error_category": AnalysisErrorCategory.AUDIO_LOAD_FAILED},
        {"error_stage": AnalysisStage.AUDIO_LOAD},
        {"summary": None},
    ],
    [
        {"worker_id": None},
        {"load_ms": None},
        {"execution_ms": 0},
        {"error_category": AnalysisErrorCategory.ANALYSIS_TIMEOUT},
        {"error_stage": AnalysisStage.EXECUTE},
        {"summary": None},
    ],
    [
        {"worker_id": None},
        {"load_ms": None},
        {"execution_ms": None},
        {"error_category": AnalysisErrorCategory.AUDIO_LOAD_FAILED},
        {"error_stage": AnalysisStage.AUDIO_LOAD},
        {"summary": None},
    ],
    [
        {"worker_id": None},
        {"execution_ms": 0, "load_ms": None},
        {"error_category": AnalysisErrorCategory.ANALYSIS_TIMEOUT},
        {"error_stage": AnalysisStage.EXECUTE},
        {"summary": None},
    ],
]


@pytest.mark.parametrize(
    ("row_index", "mutation"),
    [
        (row_index, mutation)
        for row_index, mutations in enumerate(TERMINAL_EVENT_HOSTILE_MUTATIONS)
        for mutation in mutations
    ],
)
def test_each_terminal_event_row_rejects_worker_timing_and_error_mutations(
    row_index,
    mutation,
):
    valid = dict(FULL_ID, queue_wait_ms=0, **TERMINAL_EVENT_ROWS[row_index])
    _assert_event_mutation_rejected(
        SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
        valid,
        mutation,
    )


NONE_REQUIRED_EVENT_FIELDS = [
    *[
        (
            SchedulerDiagnosticEventKind.SUBMIT_ACCEPTED,
            SUBMIT_ACCEPTED_EVENT,
            field_name,
        )
        for field_name in (
            "state_before",
            "worker_id",
            "load_ms",
            "execution_ms",
            "error_category",
            "error_stage",
            "summary",
            "lifecycle_before",
            "lifecycle_after",
        )
    ],
    *[
        (
            SchedulerDiagnosticEventKind.SUBMIT_REJECTED,
            SUBMIT_REJECTED_EVENT,
            field_name,
        )
        for field_name in (
            "submission_sequence",
            "state_before",
            "state_after",
            "worker_id",
            "queue_wait_ms",
            "load_ms",
            "execution_ms",
            "lifecycle_before",
            "lifecycle_after",
        )
    ],
    *[
        (
            SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED,
            DISPATCHED_EVENT,
            field_name,
        )
        for field_name in (
            "load_ms",
            "execution_ms",
            "error_category",
            "error_stage",
            "summary",
            "lifecycle_before",
            "lifecycle_after",
        )
    ],
    *[
        (
            SchedulerDiagnosticEventKind.CANCEL_REQUESTED,
            CANCEL_REQUESTED_EVENT,
            field_name,
        )
        for field_name in ("lifecycle_before", "lifecycle_after")
    ],
    *[
        (
            SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL,
            dict(FULL_ID, queue_wait_ms=0, **TERMINAL_EVENT_ROWS[row_index]),
            field_name,
        )
        for row_index, field_names in (
            (
                0,
                (
                    "worker_id",
                    "load_ms",
                    "execution_ms",
                    "lifecycle_before",
                    "lifecycle_after",
                ),
            ),
            (
                1,
                (
                    "worker_id",
                    "load_ms",
                    "execution_ms",
                    "lifecycle_before",
                    "lifecycle_after",
                ),
            ),
            (
                2,
                (
                    "error_category",
                    "error_stage",
                    "summary",
                    "lifecycle_before",
                    "lifecycle_after",
                ),
            ),
            (3, ("lifecycle_before", "lifecycle_after")),
            (
                4,
                ("execution_ms", "lifecycle_before", "lifecycle_after"),
            ),
            (5, ("lifecycle_before", "lifecycle_after")),
            (6, ("lifecycle_before", "lifecycle_after")),
        )
        for field_name in field_names
    ],
    *[
        (
            SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED,
            SINK_FAILED_EVENT,
            field_name,
        )
        for field_name in (
            "worker_id",
            "lifecycle_before",
            "lifecycle_after",
        )
    ],
    *[
        (
            SchedulerDiagnosticEventKind.LIFECYCLE_CHANGED,
            LIFECYCLE_EVENT,
            field_name,
        )
        for field_name in (
            "run_id",
            "analysis_attempt_id",
            "analysis_kind",
            "audio_reference_id",
            "submission_sequence",
            "state_before",
            "state_after",
            "worker_id",
            "runtime_lane_key",
            "queue_wait_ms",
            "load_ms",
            "execution_ms",
            "error_category",
            "error_stage",
            "summary",
        )
    ],
]


@pytest.mark.parametrize(
    ("kind", "valid", "field_name"),
    NONE_REQUIRED_EVENT_FIELDS,
)
def test_event_none_requirements_resist_deceptive_mutable_equality(
    kind,
    valid,
    field_name,
):
    _assert_event_mutation_rejected(
        kind,
        valid,
        {field_name: DeceptiveEquality()},
    )


def test_dataclass_representations_exclude_audio_and_snapshot_payloads():
    reference = make_audio()
    job = make_job(
        audio=reference,
        snapshot=make_snapshot(ai_toolchain=make_toolchain()),
    )
    assert "absolute_path=" not in repr(reference)
    assert repr(WINDOWS_AUDIO_PATH)[1:-1] not in repr(reference)
    assert WINDOWS_AUDIO_PATH not in repr(job)
    assert "config_snapshot_json" not in repr(job)
    execution = AnalysisExecutionResult(
        AnalysisExecutionStatus.SUCCEEDED, ("secret-payload",), None
    )
    assert "secret-payload" not in repr(execution)
    outcome = make_outcome(result_payload=("secret-payload",))
    assert "secret-payload" not in repr(outcome)
