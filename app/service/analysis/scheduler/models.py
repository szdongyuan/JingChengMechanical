"""Immutable public contracts for the parallel analysis scheduler."""

from __future__ import annotations

import math
import ntpath
from dataclasses import dataclass, field
from enum import Enum


MAX_FROZEN_RESULT_DEPTH = 32
# A terminal result can contain six full-resolution STFT matrices.  The former
# 65k cap rejected normal recordings while converting the already-computed
# result, so size the payload contract for the supported six-channel case.
MAX_FROZEN_RESULT_NODES = 4_194_304
MAX_FROZEN_RESULT_STRING_UTF8_BYTES = 65_536
MAX_FROZEN_RESULT_TOTAL_STRING_UTF8_BYTES = 67_108_864

MAX_ERROR_SUMMARY_UTF8_BYTES = 1024
MAX_ERROR_DETAIL_ITEMS = 16
MAX_ERROR_DETAIL_KEY_UTF8_BYTES = 64
MAX_ERROR_DETAIL_VALUE_UTF8_BYTES = 256
MAX_CANCEL_REASON_UTF8_BYTES = 512

_SIGNED_64_MIN = -(2**63)
_SIGNED_64_MAX = 2**63 - 1


class AudioStorageKind(str, Enum):
    CAPTURED_FINAL_WAV = "captured_final_wav"
    IMPORTED_FINAL_AUDIO = "imported_final_audio"


class AnalysisKind(Enum):
    AUTOMATIC = "automatic"
    REANALYSIS = "reanalysis"
    IMPORTED = "imported"


class AnalysisExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    NO_RESULT = "no_result"


class AnalysisStatus(str, Enum):
    SUCCEEDED = "succeeded"
    NO_RESULT = "no_result"
    FAILED = "failed"
    CANCELED = "canceled"


class SubmitStatus(str, Enum):
    ACCEPTED = "accepted"
    INVALID_JOB = "invalid_job"
    DUPLICATE_ANALYSIS_ATTEMPT_ID = "duplicate_analysis_attempt_id"
    QUEUE_FULL = "queue_full"
    NOT_ACCEPTING = "not_accepting"


class CancelStatus(str, Enum):
    CANCELED_PENDING = "canceled_pending"
    CANCEL_REQUESTED_RUNNING = "cancel_requested_running"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"
    RUN_ID_MISMATCH = "run_id_mismatch"


class AnalysisCancelOrigin(str, Enum):
    USER_STOP = "user_stop"
    RESULT_EVICTION = "result_eviction"
    SCHEDULER_SHUTDOWN = "scheduler_shutdown"


class AnalysisStartGateCode(str, Enum):
    ATTEMPT_START_NOT_QUEUED = "ATTEMPT_START_NOT_QUEUED"
    ATTEMPT_START_DB_FAILED = "ATTEMPT_START_DB_FAILED"
    ATTEMPT_START_DB_UNKNOWN = "ATTEMPT_START_DB_UNKNOWN"
    ATTEMPT_START_CONTEXT_INVALID = "ATTEMPT_START_CONTEXT_INVALID"
    ATTEMPT_START_TIMESTAMP_FAILED = "ATTEMPT_START_TIMESTAMP_FAILED"
    ATTEMPT_START_RESULT_INVALID = "ATTEMPT_START_RESULT_INVALID"
    ATTEMPT_START_GATE_EXCEPTION = "ATTEMPT_START_GATE_EXCEPTION"


class AnalysisStage(str, Enum):
    ADMISSION = "admission"
    QUEUE = "queue"
    START_GATE = "start_gate"
    AUDIO_LOAD = "audio_load"
    EXECUTE = "execute"
    CANCEL = "cancel"
    OUTCOME_SINK = "outcome_sink"
    SHUTDOWN = "shutdown"


class AnalysisErrorCategory(str, Enum):
    INVALID_JOB = "invalid_job"
    DUPLICATE_ANALYSIS_ATTEMPT_ID = "duplicate_analysis_attempt_id"
    ANALYSIS_QUEUE_FULL = "analysis_queue_full"
    SCHEDULER_NOT_ACCEPTING = "scheduler_not_accepting"
    QUEUE_WAIT_TIMEOUT = "queue_wait_timeout"
    ATTEMPT_START_FAILED = "attempt_start_failed"
    AUDIO_REFERENCE_UNREADABLE = "audio_reference_unreadable"
    AUDIO_REFERENCE_CHANGED = "audio_reference_changed"
    AUDIO_FORMAT_MISMATCH = "audio_format_mismatch"
    AUDIO_LOAD_FAILED = "audio_load_failed"
    ANALYSIS_TIMEOUT = "analysis_timeout"
    ANALYSIS_EXECUTION_FAILED = "analysis_execution_failed"
    ANALYSIS_NO_RESULT = "analysis_no_result"
    ANALYSIS_CANCELED = "analysis_canceled"
    OUTCOME_SINK_FAILED = "outcome_sink_failed"
    SHUTDOWN_INCOMPLETE = "shutdown_incomplete"


class ShutdownStatus(str, Enum):
    CLOSED = "closed"
    SHUTDOWN_INCOMPLETE = "shutdown_incomplete"
    IN_PROGRESS = "in_progress"


class SchedulerLifecycle(str, Enum):
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"
    SHUTDOWN_INCOMPLETE = "shutdown_incomplete"


class AttemptState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    NO_RESULT = "no_result"
    FAILED = "failed"
    CANCELED = "canceled"


class SchedulerCounterKey(str, Enum):
    ACCEPTED = "accepted"
    INVALID_JOB_REJECTED = "invalid_job_rejected"
    DUPLICATE_ATTEMPT_REJECTED = "duplicate_attempt_rejected"
    QUEUE_FULL_REJECTED = "queue_full_rejected"
    NOT_ACCEPTING_REJECTED = "not_accepting_rejected"
    SUCCEEDED = "succeeded"
    NO_RESULT = "no_result"
    FAILED = "failed"
    CANCELED = "canceled"
    QUEUE_TIMEOUT = "queue_timeout"
    START_GATE_FAILURE = "start_gate_failure"
    AUDIO_LOAD_FAILURE = "audio_load_failure"
    ANALYSIS_TIMEOUT = "analysis_timeout"
    EXECUTOR_FAILURE = "executor_failure"
    LATE_RESULTS_SUPPRESSED = "late_results_suppressed"
    OUTCOME_SINK_FAILURE = "outcome_sink_failure"
    DIAGNOSTIC_SINK_FAILURE = "diagnostic_sink_failure"
    SHUTDOWN_INCOMPLETE = "shutdown_incomplete"


class SchedulerDiagnosticEventKind(str, Enum):
    SUBMIT_ACCEPTED = "submit_accepted"
    SUBMIT_REJECTED = "submit_rejected"
    ATTEMPT_DISPATCHED = "attempt_dispatched"
    CANCEL_REQUESTED = "cancel_requested"
    ATTEMPT_TERMINAL = "attempt_terminal"
    OUTCOME_SINK_FAILED = "outcome_sink_failed"
    LIFECYCLE_CHANGED = "lifecycle_changed"


def _require_exact_enum(value: object, enum_type: type[Enum], name: str) -> None:
    if type(value) is not enum_type:
        raise TypeError(f"{name} must be an exact {enum_type.__name__}")


def _require_non_empty_str(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_optional_non_empty_str(value: object, name: str) -> None:
    if value is None:
        return
    _require_non_empty_str(value, name)


def _require_positive_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_non_negative_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _require_optional_non_negative_int(value: object, name: str) -> None:
    if value is None:
        return
    _require_non_negative_int(value, name)


def _require_finite_float(value: object, name: str) -> None:
    if type(value) is not float:
        raise TypeError(f"{name} must be an exact float")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _require_non_negative_float(value: object, name: str) -> None:
    _require_finite_float(value, name)
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")


def _require_positive_float(value: object, name: str) -> None:
    _require_finite_float(value, name)
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")


def _utf8_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _require_bounded_non_empty_str(
    value: object,
    name: str,
    maximum_utf8_bytes: int,
) -> None:
    _require_non_empty_str(value, name)
    if _utf8_length(value) > maximum_utf8_bytes:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit")


def _require_canonical_absolute_path(value: object, name: str) -> None:
    _require_non_empty_str(value, name)
    if not ntpath.isabs(value):
        raise ValueError(f"{name} must be absolute")
    normalized = ntpath.normpath(value)
    if normalized != value:
        raise ValueError(f"{name} must be normalized")


def canonical_toolchain_lane_key(toolchain_id: str) -> str:
    if type(toolchain_id) is not str:
        raise TypeError("toolchain_id must be an exact str")
    normalized = toolchain_id.strip()
    if not normalized:
        raise ValueError("toolchain_id must not be empty")
    return f"ai-toolchain:{normalized}"


def _require_local_lane(value: object, name: str = "runtime_lane_key") -> None:
    if value is None:
        return
    _require_non_empty_str(value, name)
    prefix = "ai-toolchain:"
    if not value.startswith(prefix):
        raise ValueError(f"{name} must be canonical")
    suffix = value[len(prefix) :]
    if canonical_toolchain_lane_key(suffix) != value:
        raise ValueError(f"{name} must be canonical")


@dataclass(frozen=True, slots=True)
class FrozenResultObject:
    items: tuple[tuple[str, FrozenResultValue], ...]

    def __post_init__(self) -> None:
        validate_frozen_result_payload(self)


type FrozenResultScalar = None | bool | int | float | str
type FrozenResultValue = (
    FrozenResultScalar
    | tuple[FrozenResultValue, ...]
    | FrozenResultObject
)
type FrozenResultPayload = FrozenResultValue


def validate_frozen_result_payload(payload: FrozenResultPayload) -> None:
    node_count = 0
    total_string_bytes = 0
    ancestors: set[int] = set()

    def add_string(value: str, name: str) -> None:
        nonlocal total_string_bytes
        length = _utf8_length(value)
        if length > MAX_FROZEN_RESULT_STRING_UTF8_BYTES:
            raise ValueError(f"{name} exceeds its UTF-8 byte limit")
        total_string_bytes += length
        if total_string_bytes > MAX_FROZEN_RESULT_TOTAL_STRING_UTF8_BYTES:
            raise ValueError("payload exceeds total string UTF-8 byte limit")

    def add_node() -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_FROZEN_RESULT_NODES:
            raise ValueError("payload exceeds node limit")

    def visit(value: object, depth: int) -> None:
        if depth > MAX_FROZEN_RESULT_DEPTH:
            raise ValueError("payload exceeds depth limit")
        add_node()
        value_type = type(value)
        if value is None or value_type is bool:
            return
        if value_type is int:
            if value < _SIGNED_64_MIN or value > _SIGNED_64_MAX:
                raise ValueError("payload int is outside signed 64-bit range")
            return
        if value_type is float:
            if not math.isfinite(value):
                raise ValueError("payload float must be finite")
            return
        if value_type is str:
            add_string(value, "payload string")
            return
        if value_type is tuple:
            identity = id(value)
            if identity in ancestors:
                raise ValueError("payload contains a cycle")
            ancestors.add(identity)
            try:
                for item in value:
                    visit(item, depth + 1)
            finally:
                ancestors.remove(identity)
            return
        if value_type is FrozenResultObject:
            identity = id(value)
            if identity in ancestors:
                raise ValueError("payload contains a cycle")
            ancestors.add(identity)
            try:
                if type(value.items) is not tuple:
                    raise TypeError("FrozenResultObject.items must be an exact tuple")
                seen_keys: set[str] = set()
                for pair in value.items:
                    if type(pair) is not tuple or len(pair) != 2:
                        raise TypeError(
                            "FrozenResultObject entries must be exact two-item tuples"
                        )
                    key, item = pair
                    if type(key) is not str:
                        raise TypeError("FrozenResultObject keys must be exact str")
                    if not key:
                        raise ValueError("FrozenResultObject keys must not be empty")
                    if key in seen_keys:
                        raise ValueError("FrozenResultObject keys must be unique")
                    seen_keys.add(key)
                    add_node()
                    add_string(key, "FrozenResultObject key")
                    visit(item, depth + 1)
            finally:
                ancestors.remove(identity)
            return
        raise TypeError(f"unsupported frozen result payload type: {value_type.__name__}")

    visit(payload, 1)


@dataclass(frozen=True, slots=True)
class PersistedAudioReference:
    reference_id: str
    storage_kind: AudioStorageKind
    absolute_path: str = field(repr=False)
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    dtype: str
    content_revision: str
    audio_data_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.reference_id, "reference_id")
        _require_exact_enum(self.storage_kind, AudioStorageKind, "storage_kind")
        _require_canonical_absolute_path(self.absolute_path, "absolute_path")
        _require_positive_int(self.sample_rate_hz, "sample_rate_hz")
        _require_positive_int(self.channel_count, "channel_count")
        _require_positive_int(self.frame_count, "frame_count")
        _require_non_empty_str(self.dtype, "dtype")
        _require_non_empty_str(self.content_revision, "content_revision")
        _require_optional_non_empty_str(self.audio_data_id, "audio_data_id")


@dataclass(frozen=True, slots=True)
class AiToolchainSnapshot:
    toolchain_id: str
    runtime_lane_key: str
    execution_revision: str
    execution_mode: str
    entrypoint: str
    package_dir: str
    params_json: str = field(repr=False)
    timeout_seconds: float

    def __post_init__(self) -> None:
        _require_non_empty_str(self.toolchain_id, "toolchain_id")
        _require_non_empty_str(self.runtime_lane_key, "runtime_lane_key")
        _require_non_empty_str(self.execution_revision, "execution_revision")
        _require_non_empty_str(self.execution_mode, "execution_mode")
        _require_non_empty_str(self.entrypoint, "entrypoint")
        _require_non_empty_str(self.package_dir, "package_dir")
        _require_non_empty_str(self.params_json, "params_json")
        _require_positive_float(self.timeout_seconds, "timeout_seconds")


@dataclass(frozen=True, slots=True)
class AnalysisInputSnapshot:
    config_snapshot_json: str = field(repr=False)
    calibration_snapshot_json: str = field(repr=False)
    workorder_snapshot_json: str = field(repr=False)
    channel_mapping_json: str = field(repr=False)
    analysis_window_start_seconds: float
    analysis_window_end_seconds: float
    algorithm_revision: str
    current_trace_json: str | None = field(default=None, repr=False)
    ai_toolchain: AiToolchainSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_non_empty_str(self.config_snapshot_json, "config_snapshot_json")
        _require_non_empty_str(
            self.calibration_snapshot_json, "calibration_snapshot_json"
        )
        _require_non_empty_str(self.workorder_snapshot_json, "workorder_snapshot_json")
        _require_non_empty_str(self.channel_mapping_json, "channel_mapping_json")
        if self.current_trace_json is not None:
            _require_non_empty_str(
                self.current_trace_json,
                "current_trace_json",
            )
        _require_non_negative_float(
            self.analysis_window_start_seconds,
            "analysis_window_start_seconds",
        )
        _require_non_negative_float(
            self.analysis_window_end_seconds,
            "analysis_window_end_seconds",
        )
        if self.analysis_window_end_seconds <= self.analysis_window_start_seconds:
            raise ValueError("analysis window end must be greater than start")
        _require_non_empty_str(self.algorithm_revision, "algorithm_revision")
        if self.ai_toolchain is not None and type(self.ai_toolchain) is not AiToolchainSnapshot:
            raise TypeError("ai_toolchain must be an exact AiToolchainSnapshot or None")


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    run_id: str
    analysis_attempt_id: str
    kind: AnalysisKind
    audio: PersistedAudioReference = field(repr=False)
    snapshot: AnalysisInputSnapshot = field(repr=False)
    execution_timeout_seconds: float
    queue_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.kind, AnalysisKind, "kind")
        if type(self.audio) is not PersistedAudioReference:
            raise TypeError("audio must be an exact PersistedAudioReference")
        if type(self.snapshot) is not AnalysisInputSnapshot:
            raise TypeError("snapshot must be an exact AnalysisInputSnapshot")
        _require_positive_float(
            self.execution_timeout_seconds, "execution_timeout_seconds"
        )
        if self.queue_timeout_seconds is not None:
            _require_positive_float(
                self.queue_timeout_seconds, "queue_timeout_seconds"
            )
        toolchain = self.snapshot.ai_toolchain
        if toolchain is not None:
            expected = canonical_toolchain_lane_key(toolchain.toolchain_id)
            if toolchain.runtime_lane_key != expected:
                raise ValueError("runtime_lane_key does not match toolchain_id")


@dataclass(frozen=True, slots=True)
class AnalysisExecutionResult:
    status: AnalysisExecutionStatus
    result_payload: FrozenResultPayload | None = field(repr=False)
    summary: str | None = None

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, AnalysisExecutionStatus, "status")
        if self.status is AnalysisExecutionStatus.SUCCEEDED:
            if self.result_payload is None:
                raise ValueError("SUCCEEDED requires a non-None result payload")
            validate_frozen_result_payload(self.result_payload)
            if self.summary is not None:
                raise ValueError("SUCCEEDED requires summary=None")
            return
        if self.result_payload is not None:
            raise ValueError("NO_RESULT requires result_payload=None")
        if self.summary is not None:
            _require_bounded_non_empty_str(
                self.summary, "summary", MAX_ERROR_SUMMARY_UTF8_BYTES
            )


_LEGAL_ERROR_STAGES: dict[AnalysisErrorCategory, frozenset[AnalysisStage]] = {
    AnalysisErrorCategory.INVALID_JOB: frozenset(
        (AnalysisStage.ADMISSION, AnalysisStage.CANCEL)
    ),
    AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID: frozenset(
        (AnalysisStage.ADMISSION,)
    ),
    AnalysisErrorCategory.ANALYSIS_QUEUE_FULL: frozenset(
        (AnalysisStage.ADMISSION,)
    ),
    AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING: frozenset(
        (AnalysisStage.ADMISSION,)
    ),
    AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT: frozenset((AnalysisStage.QUEUE,)),
    AnalysisErrorCategory.ATTEMPT_START_FAILED: frozenset(
        (AnalysisStage.START_GATE,)
    ),
    AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE: frozenset(
        (AnalysisStage.AUDIO_LOAD,)
    ),
    AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED: frozenset(
        (AnalysisStage.AUDIO_LOAD,)
    ),
    AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH: frozenset(
        (AnalysisStage.AUDIO_LOAD,)
    ),
    AnalysisErrorCategory.AUDIO_LOAD_FAILED: frozenset(
        (AnalysisStage.AUDIO_LOAD,)
    ),
    AnalysisErrorCategory.ANALYSIS_TIMEOUT: frozenset((AnalysisStage.EXECUTE,)),
    AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED: frozenset(
        (AnalysisStage.EXECUTE,)
    ),
    AnalysisErrorCategory.ANALYSIS_NO_RESULT: frozenset((AnalysisStage.EXECUTE,)),
    AnalysisErrorCategory.ANALYSIS_CANCELED: frozenset((AnalysisStage.CANCEL,)),
    AnalysisErrorCategory.OUTCOME_SINK_FAILED: frozenset(
        (AnalysisStage.OUTCOME_SINK,)
    ),
    AnalysisErrorCategory.SHUTDOWN_INCOMPLETE: frozenset(
        (AnalysisStage.SHUTDOWN,)
    ),
}


@dataclass(frozen=True, slots=True)
class AnalysisStartGateFailure:
    code: AnalysisStartGateCode
    summary: str

    def __post_init__(self) -> None:
        _require_exact_enum(self.code, AnalysisStartGateCode, "code")
        _require_bounded_non_empty_str(
            self.summary, "summary", MAX_ERROR_SUMMARY_UTF8_BYTES
        )


def _validate_error_details(details: object) -> None:
    if type(details) is not tuple:
        raise TypeError("details must be an exact tuple")
    if len(details) > MAX_ERROR_DETAIL_ITEMS:
        raise ValueError("details exceeds item limit")
    keys: set[str] = set()
    for pair in details:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError("detail entries must be exact two-item tuples")
        key, value = pair
        _require_bounded_non_empty_str(
            key, "detail key", MAX_ERROR_DETAIL_KEY_UTF8_BYTES
        )
        if type(value) is not str:
            raise TypeError("detail value must be an exact str")
        if _utf8_length(value) > MAX_ERROR_DETAIL_VALUE_UTF8_BYTES:
            raise ValueError("detail value exceeds its UTF-8 byte limit")
        if key in keys:
            raise ValueError("detail keys must be unique")
        keys.add(key)


@dataclass(frozen=True, slots=True)
class AnalysisError:
    category: AnalysisErrorCategory
    stage: AnalysisStage
    summary: str
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_exact_enum(self.category, AnalysisErrorCategory, "category")
        _require_exact_enum(self.stage, AnalysisStage, "stage")
        if self.stage not in _LEGAL_ERROR_STAGES[self.category]:
            raise ValueError("illegal analysis error category/stage combination")
        _require_bounded_non_empty_str(
            self.summary, "summary", MAX_ERROR_SUMMARY_UTF8_BYTES
        )
        _validate_error_details(self.details)


_FAILED_OUTCOME_CATEGORIES = frozenset(
    (
        AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,
        AnalysisErrorCategory.ATTEMPT_START_FAILED,
        AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
        AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
        AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
        AnalysisErrorCategory.AUDIO_LOAD_FAILED,
        AnalysisErrorCategory.ANALYSIS_TIMEOUT,
        AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
    )
)


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    run_id: str
    analysis_attempt_id: str
    kind: AnalysisKind
    status: AnalysisStatus
    audio_reference_id: str
    result_payload: FrozenResultPayload | None = field(repr=False)
    error: AnalysisError | None
    submitted_monotonic: float
    started_monotonic: float | None
    finished_monotonic: float
    queue_wait_ms: int
    execution_ms: int | None
    cancel_origin: AnalysisCancelOrigin | None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.kind, AnalysisKind, "kind")
        _require_exact_enum(self.status, AnalysisStatus, "status")
        _require_non_empty_str(self.audio_reference_id, "audio_reference_id")
        if self.status is AnalysisStatus.CANCELED:
            _require_exact_enum(
                self.cancel_origin,
                AnalysisCancelOrigin,
                "cancel_origin",
            )
        elif self.cancel_origin is not None:
            raise ValueError("non-CANCELED outcome requires cancel_origin=None")
        if self.error is not None and type(self.error) is not AnalysisError:
            raise TypeError("error must be an exact AnalysisError or None")

        if self.status is AnalysisStatus.SUCCEEDED:
            if self.result_payload is None or self.error is not None:
                raise ValueError("SUCCEEDED outcome coherence failure")
            validate_frozen_result_payload(self.result_payload)
        elif self.status is AnalysisStatus.NO_RESULT:
            if self.result_payload is not None or self.error is None:
                raise ValueError("NO_RESULT outcome coherence failure")
            if (
                self.error.category is not AnalysisErrorCategory.ANALYSIS_NO_RESULT
                or self.error.stage is not AnalysisStage.EXECUTE
            ):
                raise ValueError("NO_RESULT requires ANALYSIS_NO_RESULT / EXECUTE")
        elif self.status is AnalysisStatus.FAILED:
            if self.result_payload is not None or self.error is None:
                raise ValueError("FAILED outcome coherence failure")
            if self.error.category not in _FAILED_OUTCOME_CATEGORIES:
                raise ValueError("FAILED outcome has an illegal error category")
        else:
            if self.result_payload is not None or self.error is None:
                raise ValueError("CANCELED outcome coherence failure")
            if (
                self.error.category is not AnalysisErrorCategory.ANALYSIS_CANCELED
                or self.error.stage is not AnalysisStage.CANCEL
            ):
                raise ValueError("CANCELED requires ANALYSIS_CANCELED / CANCEL")

        _require_non_negative_float(
            self.submitted_monotonic, "submitted_monotonic"
        )
        if self.started_monotonic is not None:
            _require_non_negative_float(
                self.started_monotonic, "started_monotonic"
            )
        _require_non_negative_float(self.finished_monotonic, "finished_monotonic")
        _require_non_negative_int(self.queue_wait_ms, "queue_wait_ms")
        _require_optional_non_negative_int(self.execution_ms, "execution_ms")

        if self.started_monotonic is None:
            if self.execution_ms is not None:
                raise ValueError("pre-dispatch outcome requires execution_ms=None")
            allowed_pending_terminal = (
                self.status is AnalysisStatus.CANCELED
                or (
                    self.status is AnalysisStatus.FAILED
                    and self.error is not None
                    and self.error.category
                    is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT
                )
            )
            if not allowed_pending_terminal:
                raise ValueError("started_monotonic=None is only valid before dispatch")
            if self.finished_monotonic < self.submitted_monotonic:
                raise ValueError("finished_monotonic precedes submitted_monotonic")
        else:
            if not (
                self.submitted_monotonic
                <= self.started_monotonic
                <= self.finished_monotonic
            ):
                raise ValueError("outcome timestamps are out of order")
            executor_required = (
                self.status in (AnalysisStatus.SUCCEEDED, AnalysisStatus.NO_RESULT)
                or (
                    self.status is AnalysisStatus.FAILED
                    and self.error is not None
                    and self.error.stage is AnalysisStage.EXECUTE
                )
            )
            if executor_required and self.execution_ms is None:
                raise ValueError("executor terminal outcome requires execution_ms")
            if (
                self.status is AnalysisStatus.FAILED
                and self.error is not None
                and self.error.stage
                in (
                    AnalysisStage.QUEUE,
                    AnalysisStage.START_GATE,
                    AnalysisStage.AUDIO_LOAD,
                )
                and self.execution_ms is not None
            ):
                raise ValueError(
                    "pre-executor failure requires execution_ms=None"
                )
            if (
                self.status is AnalysisStatus.FAILED
                and self.error is not None
                and self.error.stage is AnalysisStage.QUEUE
            ):
                raise ValueError("queue timeout must occur before dispatch")


@dataclass(frozen=True, slots=True)
class AnalysisSchedulerConfig:
    worker_count: int = 2
    pending_capacity: int = 64
    terminal_receipt_capacity: int = 256
    shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        _require_positive_int(self.worker_count, "worker_count")
        if self.worker_count > 6:
            raise ValueError("worker_count must be at most 6")
        _require_positive_int(self.pending_capacity, "pending_capacity")
        _require_positive_int(
            self.terminal_receipt_capacity, "terminal_receipt_capacity"
        )
        _require_positive_float(
            self.shutdown_timeout_seconds, "shutdown_timeout_seconds"
        )


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    status: SubmitStatus
    run_id: str
    analysis_attempt_id: str
    submission_sequence: int | None
    error: AnalysisError | None

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, SubmitStatus, "status")
        if type(self.run_id) is not str or type(self.analysis_attempt_id) is not str:
            raise TypeError("receipt IDs must be exact str")
        if self.error is not None and type(self.error) is not AnalysisError:
            raise TypeError("error must be an exact AnalysisError or None")
        if self.status is SubmitStatus.ACCEPTED:
            _require_non_empty_str(self.run_id, "run_id")
            _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
            _require_positive_int(self.submission_sequence, "submission_sequence")
            if self.error is not None:
                raise ValueError("accepted receipt requires error=None")
            return
        if self.submission_sequence is not None:
            raise ValueError("rejected receipt requires submission_sequence=None")
        if self.error is None:
            raise ValueError("rejected receipt requires error")
        expected = {
            SubmitStatus.INVALID_JOB: AnalysisErrorCategory.INVALID_JOB,
            SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID:
                AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
            SubmitStatus.QUEUE_FULL: AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
            SubmitStatus.NOT_ACCEPTING:
                AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
        }[self.status]
        if (
            self.error.category is not expected
            or self.error.stage is not AnalysisStage.ADMISSION
            or self.error.details != ()
        ):
            raise ValueError("rejected receipt error does not match status")
        if self.status is not SubmitStatus.INVALID_JOB:
            _require_non_empty_str(self.run_id, "run_id")
            _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    status: CancelStatus
    run_id: str
    analysis_attempt_id: str
    drain_pending: bool
    error: AnalysisError | None

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, CancelStatus, "status")
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        if type(self.drain_pending) is not bool:
            raise TypeError("drain_pending must be an exact bool")
        if self.error is not None and type(self.error) is not AnalysisError:
            raise TypeError("error must be an exact AnalysisError or None")
        expected_drain = self.status is CancelStatus.CANCEL_REQUESTED_RUNNING
        if self.drain_pending is not expected_drain:
            raise ValueError("drain_pending does not match cancel status")
        if self.status in (
            CancelStatus.NOT_FOUND,
            CancelStatus.RUN_ID_MISMATCH,
        ):
            if (
                self.error is None
                or self.error.category is not AnalysisErrorCategory.INVALID_JOB
                or self.error.stage is not AnalysisStage.CANCEL
            ):
                raise ValueError("cancel miss requires INVALID_JOB / CANCEL")
        elif self.error is not None:
            raise ValueError("successful or terminal cancel receipt requires error=None")


def _validate_sorted_unique_strings(value: object, name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be an exact tuple")
    for item in value:
        _require_non_empty_str(item, f"{name} item")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} items must be unique")
    if value != tuple(sorted(value)):
        raise ValueError(f"{name} must be lexically sorted")


def _validate_sorted_unique_worker_ids(value: object) -> None:
    if type(value) is not tuple:
        raise TypeError("remaining_worker_ids must be an exact tuple")
    for item in value:
        _require_positive_int(item, "remaining worker ID")
        if item > 6:
            raise ValueError("remaining worker IDs cannot exceed six workers")
    if len(set(value)) != len(value):
        raise ValueError("remaining worker IDs must be unique")
    if value != tuple(sorted(value)):
        raise ValueError("remaining worker IDs must be numerically sorted")


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    status: ShutdownStatus
    started_monotonic: float
    finished_monotonic: float
    canceled_pending: int
    cancel_requested_running: int
    remaining_attempt_ids: tuple[str, ...]
    remaining_worker_ids: tuple[int, ...]
    remaining_run_lanes: tuple[str, ...]
    remaining_toolchain_lanes: tuple[str, ...]
    control_thread_alive: bool

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, ShutdownStatus, "status")
        _require_non_negative_float(
            self.started_monotonic, "started_monotonic"
        )
        _require_non_negative_float(
            self.finished_monotonic, "finished_monotonic"
        )
        if self.started_monotonic > self.finished_monotonic:
            raise ValueError("shutdown timestamps are out of order")
        _require_non_negative_int(self.canceled_pending, "canceled_pending")
        _require_non_negative_int(self.cancel_requested_running, "cancel_requested_running")
        _validate_sorted_unique_strings(
            self.remaining_attempt_ids, "remaining_attempt_ids"
        )
        _validate_sorted_unique_worker_ids(self.remaining_worker_ids)
        _validate_sorted_unique_strings(
            self.remaining_run_lanes, "remaining_run_lanes"
        )
        _validate_sorted_unique_strings(
            self.remaining_toolchain_lanes, "remaining_toolchain_lanes"
        )
        for lane in self.remaining_toolchain_lanes:
            _require_local_lane(lane, "remaining_toolchain_lane")
        if type(self.control_thread_alive) is not bool:
            raise TypeError("control_thread_alive must be an exact bool")
        remaining_resources = (
            self.remaining_attempt_ids,
            self.remaining_worker_ids,
            self.remaining_run_lanes,
            self.remaining_toolchain_lanes,
        )
        if self.cancel_requested_running > 6 or any(
            len(resources) > 6 for resources in remaining_resources
        ):
            raise ValueError("shutdown remaining resources cannot exceed six workers")
        remaining = self.control_thread_alive or any(remaining_resources)
        if self.status is ShutdownStatus.CLOSED and remaining:
            raise ValueError("CLOSED report cannot contain remaining resources")
        if self.status is ShutdownStatus.SHUTDOWN_INCOMPLETE and not remaining:
            raise ValueError(
                "SHUTDOWN_INCOMPLETE report requires remaining resources"
            )


_ACTIVE_STATES = frozenset(
    (AttemptState.PENDING, AttemptState.RUNNING, AttemptState.CANCEL_REQUESTED)
)
_TERMINAL_STATES = frozenset(
    (
        AttemptState.SUCCEEDED,
        AttemptState.NO_RESULT,
        AttemptState.FAILED,
        AttemptState.CANCELED,
    )
)


@dataclass(frozen=True, slots=True)
class AttemptDiagnostic:
    run_id: str
    analysis_attempt_id: str
    kind: AnalysisKind
    state: AttemptState
    submission_sequence: int
    worker_id: int | None
    runtime_lane_key: str | None
    wait_ms: int
    execution_ms: int | None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.kind, AnalysisKind, "kind")
        _require_exact_enum(self.state, AttemptState, "state")
        if self.state not in _ACTIVE_STATES:
            raise ValueError("AttemptDiagnostic state must be active")
        _require_positive_int(self.submission_sequence, "submission_sequence")
        _require_non_negative_int(self.wait_ms, "wait_ms")
        _require_optional_non_negative_int(self.execution_ms, "execution_ms")
        _require_local_lane(self.runtime_lane_key)
        if self.state is AttemptState.PENDING:
            if self.worker_id is not None or self.execution_ms is not None:
                raise ValueError("pending diagnostic cannot own a worker")
        else:
            _require_positive_int(self.worker_id, "worker_id")


@dataclass(frozen=True, slots=True)
class ToolchainLaneDiagnostic:
    runtime_lane_key: str
    running: int
    pending: int

    def __post_init__(self) -> None:
        _require_local_lane(self.runtime_lane_key)
        if self.runtime_lane_key is None:
            raise ValueError("toolchain lane key cannot be None")
        _require_non_negative_int(self.running, "running")
        _require_non_negative_int(self.pending, "pending")
        if self.running > 1:
            raise ValueError("toolchain lane running count cannot exceed one")
        if self.running == 0 and self.pending == 0:
            raise ValueError("empty toolchain lane diagnostics are forbidden")


@dataclass(frozen=True, slots=True)
class TerminalReceiptDiagnostic:
    run_id: str
    analysis_attempt_id: str
    terminal_status: AnalysisStatus
    finished_monotonic: float

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.terminal_status, AnalysisStatus, "terminal_status")
        _require_non_negative_float(
            self.finished_monotonic, "finished_monotonic"
        )


@dataclass(frozen=True, slots=True)
class SchedulerDiagnostics:
    lifecycle: SchedulerLifecycle
    worker_count: int
    pending_capacity: int
    terminal_receipt_capacity: int
    pending: int
    running: int
    cancel_requested: int
    full_audio_residency_count: int
    peak_full_audio_residency_count: int
    peak_running_workers: int
    pending_by_kind: tuple[tuple[AnalysisKind, int], ...]
    oldest_pending_wait_ms: int
    blocked_on_run_lane: int
    blocked_on_toolchain_lane: int
    attempts: tuple[AttemptDiagnostic, ...]
    toolchain_lanes: tuple[ToolchainLaneDiagnostic, ...]
    terminal_receipts: tuple[TerminalReceiptDiagnostic, ...]
    counters: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _require_exact_enum(self.lifecycle, SchedulerLifecycle, "lifecycle")
        _require_positive_int(self.worker_count, "worker_count")
        if self.worker_count > 6:
            raise ValueError("worker_count cannot exceed six")
        _require_positive_int(self.pending_capacity, "pending_capacity")
        _require_positive_int(
            self.terminal_receipt_capacity, "terminal_receipt_capacity"
        )
        for name in (
            "pending",
            "running",
            "cancel_requested",
            "full_audio_residency_count",
            "peak_full_audio_residency_count",
            "peak_running_workers",
            "oldest_pending_wait_ms",
            "blocked_on_run_lane",
            "blocked_on_toolchain_lane",
        ):
            _require_non_negative_int(getattr(self, name), name)
        if self.pending > self.pending_capacity:
            raise ValueError("pending exceeds capacity")
        active_owners = self.running + self.cancel_requested
        if active_owners > self.worker_count:
            raise ValueError("active worker owners exceed worker count")
        if self.full_audio_residency_count > active_owners:
            raise ValueError("audio residency exceeds active worker owners")
        if not (
            self.full_audio_residency_count
            <= self.peak_full_audio_residency_count
            <= self.worker_count
        ):
            raise ValueError("peak audio residency is contradictory")
        if not active_owners <= self.peak_running_workers <= self.worker_count:
            raise ValueError("peak running workers is contradictory")
        if self.blocked_on_run_lane > self.pending:
            raise ValueError("run-lane blocked count exceeds pending")
        if self.blocked_on_toolchain_lane > self.pending:
            raise ValueError("toolchain-lane blocked count exceeds pending")

        if type(self.attempts) is not tuple:
            raise TypeError("attempts must be an exact tuple")
        if any(type(item) is not AttemptDiagnostic for item in self.attempts):
            raise TypeError("attempts must contain exact AttemptDiagnostic values")
        sequences = tuple(item.submission_sequence for item in self.attempts)
        if any(left >= right for left, right in zip(sequences, sequences[1:])):
            raise ValueError("attempts must have strictly increasing sequence")
        attempt_ids = tuple(item.analysis_attempt_id for item in self.attempts)
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("active attempt IDs must be unique")
        worker_ids = tuple(
            item.worker_id
            for item in self.attempts
            if item.worker_id is not None
        )
        if len(set(worker_ids)) != len(worker_ids):
            raise ValueError("active worker IDs must be unique")
        live_run_ids = tuple(
            item.run_id for item in self.attempts
            if item.state is not AttemptState.PENDING
        )
        if len(set(live_run_ids)) != len(live_run_ids):
            raise ValueError("live run lane owners must be unique")
        actual_state_counts = {
            state: sum(item.state is state for item in self.attempts)
            for state in _ACTIVE_STATES
        }
        if (
            len(self.attempts) != self.pending + self.running + self.cancel_requested
            or actual_state_counts[AttemptState.PENDING] != self.pending
            or actual_state_counts[AttemptState.RUNNING] != self.running
            or actual_state_counts[AttemptState.CANCEL_REQUESTED]
            != self.cancel_requested
        ):
            raise ValueError("active attempt counts are contradictory")

        if type(self.pending_by_kind) is not tuple:
            raise TypeError("pending_by_kind must be an exact tuple")
        for pair in self.pending_by_kind:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError(
                    "pending_by_kind entries must be exact two-item tuples"
                )
            kind, count = pair
            _require_exact_enum(kind, AnalysisKind, "pending kind")
            _require_non_negative_int(count, "pending kind count")
        expected_pending_kinds = tuple(
            (
                kind,
                sum(
                    attempt.state is AttemptState.PENDING
                    and attempt.kind is kind
                    for attempt in self.attempts
                ),
            )
            for kind in AnalysisKind
        )
        if self.pending_by_kind != expected_pending_kinds:
            raise ValueError("pending_by_kind is contradictory or out of order")
        expected_oldest = max(
            (
                attempt.wait_ms
                for attempt in self.attempts
                if attempt.state is AttemptState.PENDING
            ),
            default=0,
        )
        if self.oldest_pending_wait_ms != expected_oldest:
            raise ValueError("oldest pending wait is contradictory")

        if type(self.toolchain_lanes) is not tuple:
            raise TypeError("toolchain_lanes must be an exact tuple")
        if any(
            type(item) is not ToolchainLaneDiagnostic
            for item in self.toolchain_lanes
        ):
            raise TypeError(
                "toolchain_lanes must contain exact ToolchainLaneDiagnostic values"
            )
        lane_keys = tuple(item.runtime_lane_key for item in self.toolchain_lanes)
        if lane_keys != tuple(sorted(lane_keys)) or len(set(lane_keys)) != len(
            lane_keys
        ):
            raise ValueError("toolchain lanes must be sorted and unique")
        expected_lanes: dict[str, tuple[int, int]] = {}
        for attempt in self.attempts:
            lane = attempt.runtime_lane_key
            if lane is None:
                continue
            running, pending = expected_lanes.get(lane, (0, 0))
            if attempt.state is AttemptState.PENDING:
                pending += 1
            else:
                running += 1
            expected_lanes[lane] = (running, pending)
        actual_lanes = {
            lane.runtime_lane_key: (lane.running, lane.pending)
            for lane in self.toolchain_lanes
        }
        if actual_lanes != expected_lanes:
            raise ValueError("toolchain lane aggregates are contradictory")

        if type(self.terminal_receipts) is not tuple:
            raise TypeError("terminal_receipts must be an exact tuple")
        if any(
            type(item) is not TerminalReceiptDiagnostic
            for item in self.terminal_receipts
        ):
            raise TypeError(
                "terminal_receipts must contain exact TerminalReceiptDiagnostic values"
            )
        if len(self.terminal_receipts) > self.terminal_receipt_capacity:
            raise ValueError("terminal receipts exceed capacity")
        terminal_ids = tuple(
            item.analysis_attempt_id for item in self.terminal_receipts
        )
        if len(set(terminal_ids)) != len(terminal_ids):
            raise ValueError("terminal receipt IDs must be unique")
        if set(attempt_ids).intersection(terminal_ids):
            raise ValueError("active and terminal attempt IDs must be disjoint")
        terminal_times = tuple(
            item.finished_monotonic for item in self.terminal_receipts
        )
        if any(left > right for left, right in zip(terminal_times, terminal_times[1:])):
            raise ValueError("terminal receipts must be in completion order")

        if type(self.counters) is not tuple:
            raise TypeError("counters must be an exact tuple")
        expected_keys = tuple(key.value for key in SchedulerCounterKey)
        if len(self.counters) != len(expected_keys):
            raise ValueError("counters must contain exactly eighteen keys")
        actual_keys: list[str] = []
        counter_values: dict[str, int] = {}
        for pair in self.counters:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError("counter entries must be exact two-item tuples")
            key, value = pair
            if type(key) is not str:
                raise TypeError("counter keys must be exact str")
            _require_non_negative_int(value, "counter value")
            actual_keys.append(key)
            counter_values[key] = value
        if tuple(actual_keys) != expected_keys:
            raise ValueError("counter keys are missing, duplicated, or out of order")
        failed_parts = (
            counter_values[SchedulerCounterKey.QUEUE_TIMEOUT.value]
            + counter_values[SchedulerCounterKey.START_GATE_FAILURE.value]
            + counter_values[SchedulerCounterKey.AUDIO_LOAD_FAILURE.value]
            + counter_values[SchedulerCounterKey.ANALYSIS_TIMEOUT.value]
            + counter_values[SchedulerCounterKey.EXECUTOR_FAILURE.value]
        )
        if counter_values[SchedulerCounterKey.FAILED.value] != failed_parts:
            raise ValueError("failed counter aggregate is contradictory")
        terminal_total = sum(
            counter_values[key.value]
            for key in (
                SchedulerCounterKey.SUCCEEDED,
                SchedulerCounterKey.NO_RESULT,
                SchedulerCounterKey.FAILED,
                SchedulerCounterKey.CANCELED,
            )
        )
        if (
            counter_values[SchedulerCounterKey.ACCEPTED.value]
            != terminal_total + len(self.attempts)
        ):
            raise ValueError("accepted counter aggregate is contradictory")
        for status in AnalysisStatus:
            retained_count = sum(
                item.terminal_status is status for item in self.terminal_receipts
            )
            if counter_values[status.value] < retained_count:
                raise ValueError("terminal counter is below retained receipts")

        if self.lifecycle is SchedulerLifecycle.CLOSED:
            if (
                self.attempts
                or self.toolchain_lanes
                or self.full_audio_residency_count
                or active_owners
            ):
                raise ValueError("closed diagnostics contain active resources")
        incomplete_count = counter_values[
            SchedulerCounterKey.SHUTDOWN_INCOMPLETE.value
        ]
        if self.lifecycle is SchedulerLifecycle.SHUTDOWN_INCOMPLETE:
            if incomplete_count != 1:
                raise ValueError("incomplete lifecycle requires one counter fact")
        elif incomplete_count != 0:
            raise ValueError("shutdown incomplete counter contradicts lifecycle")


def _event_optional_fields(event: SchedulerDiagnosticEvent) -> dict[str, object]:
    return {
        name: getattr(event, name)
        for name in (
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
        )
    }


@dataclass(frozen=True, slots=True)
class SchedulerDiagnosticEvent:
    event_kind: SchedulerDiagnosticEventKind
    occurred_monotonic: float
    run_id: str | None = None
    analysis_attempt_id: str | None = None
    analysis_kind: AnalysisKind | None = None
    audio_reference_id: str | None = None
    submission_sequence: int | None = None
    state_before: AttemptState | None = None
    state_after: AttemptState | None = None
    worker_id: int | None = None
    runtime_lane_key: str | None = None
    queue_wait_ms: int | None = None
    load_ms: int | None = None
    execution_ms: int | None = None
    error_category: AnalysisErrorCategory | None = None
    error_stage: AnalysisStage | None = None
    summary: str | None = None
    lifecycle_before: SchedulerLifecycle | None = None
    lifecycle_after: SchedulerLifecycle | None = None
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_exact_enum(
            self.event_kind, SchedulerDiagnosticEventKind, "event_kind"
        )
        if any(value is not None and type(value) is not AttemptState for value in (self.state_before, self.state_after)):
            raise TypeError("state fields must be exact AttemptState or None")
        if any(value is not None and type(value) is not SchedulerLifecycle for value in (self.lifecycle_before, self.lifecycle_after)):
            raise TypeError("lifecycle fields must be exact SchedulerLifecycle or None")
        _require_non_negative_float(
            self.occurred_monotonic, "occurred_monotonic"
        )
        for name in ("queue_wait_ms", "load_ms", "execution_ms"):
            _require_optional_non_negative_int(getattr(self, name), name)
        if self.execution_ms is not None and self.load_ms is None:
            raise ValueError("execution timing requires load timing")
        _require_local_lane(self.runtime_lane_key)
        _validate_error_details(self.details)

        kind = self.event_kind
        if kind is SchedulerDiagnosticEventKind.SUBMIT_ACCEPTED:
            self._validate_full_identity()
            self._require_fields(
                state_before=None,
                state_after=AttemptState.PENDING,
                worker_id=None,
                queue_wait_ms=0,
                load_ms=None,
                execution_ms=None,
                lifecycle_before=None,
                lifecycle_after=None,
            )
            self._validate_no_error()
        elif kind is SchedulerDiagnosticEventKind.SUBMIT_REJECTED:
            self._validate_safe_projection()
            self._require_fields(
                submission_sequence=None,
                state_before=None,
                state_after=None,
                worker_id=None,
                queue_wait_ms=None,
                load_ms=None,
                execution_ms=None,
                lifecycle_before=None,
                lifecycle_after=None,
            )
            self._validate_error_quartet()
            if self.error_category not in (
                AnalysisErrorCategory.INVALID_JOB,
                AnalysisErrorCategory.DUPLICATE_ANALYSIS_ATTEMPT_ID,
                AnalysisErrorCategory.ANALYSIS_QUEUE_FULL,
                AnalysisErrorCategory.SCHEDULER_NOT_ACCEPTING,
            ) or self.error_stage is not AnalysisStage.ADMISSION:
                raise ValueError("submit rejection has an illegal admission error")
            if self.details != ():
                raise ValueError("submit rejection details must be empty")
        elif kind is SchedulerDiagnosticEventKind.ATTEMPT_DISPATCHED:
            self._validate_full_identity()
            _require_positive_int(self.worker_id, "worker_id")
            _require_non_negative_int(self.queue_wait_ms, "queue_wait_ms")
            self._require_fields(
                state_before=AttemptState.PENDING,
                state_after=AttemptState.RUNNING,
                load_ms=None,
                execution_ms=None,
                lifecycle_before=None,
                lifecycle_after=None,
            )
            self._validate_no_error()
        elif kind is SchedulerDiagnosticEventKind.CANCEL_REQUESTED:
            self._validate_full_identity()
            _require_positive_int(self.worker_id, "worker_id")
            _require_non_negative_int(self.queue_wait_ms, "queue_wait_ms")
            self._require_fields(
                state_before=AttemptState.RUNNING,
                state_after=AttemptState.CANCEL_REQUESTED,
                lifecycle_before=None,
                lifecycle_after=None,
            )
            self._validate_error_quartet()
            if (
                self.error_category
                is not AnalysisErrorCategory.ANALYSIS_CANCELED
                or self.error_stage is not AnalysisStage.CANCEL
                or self.details != ()
            ):
                raise ValueError("cancel event error is not canonical")
        elif kind is SchedulerDiagnosticEventKind.ATTEMPT_TERMINAL:
            self._validate_full_identity()
            _require_non_negative_int(self.queue_wait_ms, "queue_wait_ms")
            self._require_fields(lifecycle_before=None, lifecycle_after=None)
            self._validate_terminal_event()
        elif kind is SchedulerDiagnosticEventKind.OUTCOME_SINK_FAILED:
            self._validate_full_identity()
            if (
                self.state_before not in _TERMINAL_STATES
                or self.state_after is not self.state_before
            ):
                raise ValueError("sink-failure event requires one terminal state")
            _require_non_negative_int(self.queue_wait_ms, "queue_wait_ms")
            self._require_fields(
                worker_id=None,
                lifecycle_before=None,
                lifecycle_after=None,
            )
            self._validate_error_quartet()
            if (
                self.error_category
                is not AnalysisErrorCategory.OUTCOME_SINK_FAILED
                or self.error_stage is not AnalysisStage.OUTCOME_SINK
                or self.details != ()
            ):
                raise ValueError("sink-failure event error is not canonical")
        else:
            self._validate_lifecycle_event()

    def _require_fields(self, **expected: object) -> None:
        for name, value in expected.items():
            if (actual := getattr(self, name)) is not value and (value is None or actual != value):
                raise ValueError(f"{name} has an illegal value for event kind")

    def _validate_full_identity(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        _require_non_empty_str(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.analysis_kind, AnalysisKind, "analysis_kind")
        _require_non_empty_str(self.audio_reference_id, "audio_reference_id")
        _require_positive_int(self.submission_sequence, "submission_sequence")

    def _validate_safe_projection(self) -> None:
        for name in ("run_id", "analysis_attempt_id", "audio_reference_id"):
            value = getattr(self, name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{name} must be exact str or None")
        if self.analysis_kind is not None:
            _require_exact_enum(self.analysis_kind, AnalysisKind, "analysis_kind")

    def _validate_no_error(self) -> None:
        if (
            self.error_category is not None
            or self.error_stage is not None
            or self.summary is not None
            or self.details != ()
        ):
            raise ValueError("event requires no error")

    def _validate_error_quartet(self) -> None:
        if self.error_category is None or self.error_stage is None:
            raise ValueError("event requires an error quartet")
        AnalysisError(
            self.error_category,
            self.error_stage,
            self.summary,
            self.details,
        )

    def _validate_terminal_event(self) -> None:
        before = self.state_before
        after = self.state_after
        if before is AttemptState.PENDING and after is AttemptState.FAILED:
            self._require_fields(
                worker_id=None,
                load_ms=None,
                execution_ms=None,
            )
            self._require_terminal_error(
                (AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT,),
                AnalysisStage.QUEUE,
            )
        elif before is AttemptState.PENDING and after is AttemptState.CANCELED:
            self._require_fields(
                worker_id=None,
                load_ms=None,
                execution_ms=None,
            )
            self._require_terminal_error(
                (AnalysisErrorCategory.ANALYSIS_CANCELED,),
                AnalysisStage.CANCEL,
            )
        elif before is AttemptState.RUNNING and after is AttemptState.SUCCEEDED:
            _require_positive_int(self.worker_id, "worker_id")
            _require_non_negative_int(self.load_ms, "load_ms")
            _require_non_negative_int(self.execution_ms, "execution_ms")
            self._validate_no_error()
        elif before is AttemptState.RUNNING and after is AttemptState.NO_RESULT:
            _require_positive_int(self.worker_id, "worker_id")
            _require_non_negative_int(self.load_ms, "load_ms")
            _require_non_negative_int(self.execution_ms, "execution_ms")
            self._require_terminal_error(
                (AnalysisErrorCategory.ANALYSIS_NO_RESULT,),
                AnalysisStage.EXECUTE,
            )
        elif before is AttemptState.RUNNING and after is AttemptState.FAILED:
            _require_positive_int(self.worker_id, "worker_id")
            if self.error_stage is AnalysisStage.START_GATE:
                self._require_fields(load_ms=None, execution_ms=None)
                categories = (
                    AnalysisErrorCategory.ATTEMPT_START_FAILED,
                )
            elif self.error_stage is AnalysisStage.AUDIO_LOAD:
                _require_non_negative_int(self.load_ms, "load_ms")
                self._require_fields(execution_ms=None)
                categories = (
                    AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE,
                    AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED,
                    AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH,
                    AnalysisErrorCategory.AUDIO_LOAD_FAILED,
                )
            elif self.error_stage is AnalysisStage.EXECUTE:
                _require_non_negative_int(self.load_ms, "load_ms")
                _require_non_negative_int(self.execution_ms, "execution_ms")
                categories = (
                    AnalysisErrorCategory.ANALYSIS_TIMEOUT,
                    AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED,
                )
            else:
                raise ValueError("running failure has an illegal stage")
            self._require_terminal_error(categories, self.error_stage)
        elif (
            before is AttemptState.CANCEL_REQUESTED
            and after is AttemptState.CANCELED
        ):
            _require_positive_int(self.worker_id, "worker_id")
            self._require_terminal_error(
                (AnalysisErrorCategory.ANALYSIS_CANCELED,),
                AnalysisStage.CANCEL,
            )
        else:
            raise ValueError("illegal terminal event transition")

    def _require_terminal_error(
        self,
        categories: tuple[AnalysisErrorCategory, ...],
        stage: AnalysisStage,
    ) -> None:
        self._validate_error_quartet()
        if self.error_category not in categories or self.error_stage is not stage:
            raise ValueError("terminal event error does not match transition")

    def _validate_lifecycle_event(self) -> None:
        forbidden = _event_optional_fields(self)
        forbidden.pop("lifecycle_before")
        forbidden.pop("lifecycle_after")
        forbidden.pop("error_category")
        forbidden.pop("error_stage")
        forbidden.pop("summary")
        if any(value is not None for value in forbidden.values()):
            raise ValueError("lifecycle event contains attempt fields")
        transitions = (
            (SchedulerLifecycle.RUNNING, SchedulerLifecycle.SHUTTING_DOWN),
            (SchedulerLifecycle.SHUTTING_DOWN, SchedulerLifecycle.CLOSED),
            (
                SchedulerLifecycle.SHUTTING_DOWN,
                SchedulerLifecycle.SHUTDOWN_INCOMPLETE,
            ),
        )
        if (self.lifecycle_before, self.lifecycle_after) not in transitions:
            raise ValueError("illegal lifecycle transition")
        if self.lifecycle_after is SchedulerLifecycle.SHUTDOWN_INCOMPLETE:
            self._validate_error_quartet()
            if (
                self.error_category
                is not AnalysisErrorCategory.SHUTDOWN_INCOMPLETE
                or self.error_stage is not AnalysisStage.SHUTDOWN
                or self.summary != "analysis scheduler shutdown incomplete"
                or self.details != ()
            ):
                raise ValueError("incomplete lifecycle error is not canonical")
        else:
            self._validate_no_error()
