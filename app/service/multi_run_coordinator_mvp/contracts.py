from __future__ import annotations

import itertools
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Callable, Final, TypeAlias

from app.service.plc_result_sequencer import (
    PlcIngressTicket,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
)


def require_uuid4(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a canonical UUIDv4")
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UUIDv4")
    return value


def require_bounded_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{field_name} must be text without NUL")
    if len(value.encode("utf-8")) > 512:
        raise ValueError(f"{field_name} exceeds 512 UTF-8 bytes")
    return value


def _require_nonempty_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be nonempty text")
    return value


def _require_positive_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_nonnegative_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return value


def _require_bool(value: bool, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a bool")
    return value


def _require_absolute_path(value: str, field_name: str) -> str:
    _require_nonempty_text(value, field_name)
    if not (
        PurePath(value).is_absolute()
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
    ):
        raise ValueError(f"{field_name} must be an absolute path")
    return value


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value


def _require_optional_uuid4(value: str | None, field_name: str) -> None:
    if value is not None:
        require_uuid4(value, field_name)


def _require_optional_nonempty_text(value: str | None, field_name: str) -> None:
    if value is not None:
        _require_nonempty_text(value, field_name)


def _bounded_errors(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("errors must be a non-scalar iterable")
    try:
        errors = tuple(itertools.islice(iter(values), 32))  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("errors must be iterable") from exc
    for error in errors:
        require_bounded_text(error, "error")
    return errors


def _sorted_uuid_values(values: object, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a non-scalar iterable")
    try:
        result = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field_name} must be iterable") from exc
    for value in result:
        require_uuid4(value, field_name)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicate identities")
    return tuple(sorted(result))


def _sorted_text_values(values: object, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a non-scalar iterable")
    try:
        result = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field_name} must be iterable") from exc
    for value in result:
        _require_nonempty_text(value, field_name)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicate identities")
    return tuple(sorted(result))


class TriggerSource(Enum):
    PLC = auto()
    MANUAL = auto()
    SCANNER = auto()
    IMPORT = auto()


class CaptureReserveStatus(Enum):
    RESERVED = auto()
    CAPACITY = auto()
    NOT_READY = auto()
    FAULT = auto()
    CLOSING = auto()


class CaptureOutcomeStatus(Enum):
    COMPLETED = auto()
    CANCELLED = auto()
    FAILED = auto()


class CoordinatorPhase(Enum):
    PREFLIGHT = auto()
    RESERVING = auto()
    ACTIVE_COMMITTING = auto()
    REGISTERING = auto()
    STARTING = auto()
    RECORDING = auto()
    SAVE_SUBMITTING = auto()
    SAVING = auto()
    ATTEMPT_COMMITTING = auto()
    ANALYSIS_SUBMITTING = auto()
    ANALYSIS_PENDING = auto()
    TERMINAL_COMMITTING = auto()
    PUBLISHING = auto()


class StartStatus(Enum):
    ACCEPTED_ACTIVE = auto()
    ACCEPTED_TERMINAL = auto()
    INVALID_REQUEST = auto()
    DUPLICATE_RUN_ID = auto()
    INGRESS_CLOSED = auto()
    PLC_INGRESS_REJECTED = auto()
    PREFLIGHT_REJECTED = auto()
    INVALID_FROZEN_INPUT = auto()
    CAPTURE_CAPACITY = auto()
    RESIDENT_UNAVAILABLE = auto()
    ACTIVE_COMMIT_REJECTED = auto()


class TerminalConclusion(Enum):
    OK = auto()
    NG = auto()
    NO_RESULT = auto()
    ERROR = auto()
    CAPACITY_REJECTED = auto()
    MANUAL_STOPPED = auto()


class TerminalErrorCategory(Enum):
    INVALID_REQUEST = auto()
    INVALID_FROZEN_INPUT = auto()
    PREFLIGHT_REJECTED = auto()
    PLC_INGRESS_REJECTED = auto()
    CAPTURE_CAPACITY = auto()
    RESIDENT_UNAVAILABLE = auto()
    ACTIVE_COMMIT_FAILED = auto()
    PLC_ACTIVATION_FAILED = auto()
    CAPTURE_REGISTRATION_FAILED = auto()
    CAPTURE_START_FAILED = auto()
    CAPTURE_FAILED = auto()
    CAPTURE_CANCELLED = auto()
    CURRENT_TRACE_INVALID = auto()
    CURRENT_TRACE_PERSIST_FAILED = auto()
    AUDIO_SAVE_SATURATED = auto()
    AUDIO_SAVE_FAILED = auto()
    AUDIO_SAVE_CANCELLED = auto()
    AUDIO_PATH_EXISTS = auto()
    ATTEMPT_ID_FAILED = auto()
    ATTEMPT_QUEUE_COMMIT_FAILED = auto()
    ATTEMPT_RUNNING_COMMIT_FAILED = auto()
    ANALYSIS_QUEUE_FULL = auto()
    ANALYSIS_NOT_ACCEPTING = auto()
    ANALYSIS_INVALID = auto()
    ANALYSIS_FAILED = auto()
    ANALYSIS_CANCELLED = auto()
    MANUAL_STOPPED = auto()
    SHUTDOWN_CANCELLED = auto()
    PERSISTENCE_TERMINAL_FAILED = auto()


_PREACCEPT_ERROR_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.INVALID_FROZEN_INPUT,
        TerminalErrorCategory.PREFLIGHT_REJECTED,
        TerminalErrorCategory.PLC_INGRESS_REJECTED,
        TerminalErrorCategory.RESIDENT_UNAVAILABLE,
        TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
    }
)
_PREACCEPT_ONLY_CATEGORIES = _PREACCEPT_ERROR_CATEGORIES | frozenset(
    {TerminalErrorCategory.CAPTURE_CAPACITY}
)
_CAPTURE_POSTACCEPT_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.CAPTURE_REGISTRATION_FAILED,
        TerminalErrorCategory.CAPTURE_START_FAILED,
        TerminalErrorCategory.CAPTURE_FAILED,
        TerminalErrorCategory.CAPTURE_CANCELLED,
        TerminalErrorCategory.CURRENT_TRACE_INVALID,
        TerminalErrorCategory.CURRENT_TRACE_PERSIST_FAILED,
    }
)
_SAVE_ERROR_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.AUDIO_SAVE_SATURATED,
        TerminalErrorCategory.AUDIO_SAVE_FAILED,
        TerminalErrorCategory.AUDIO_SAVE_CANCELLED,
        TerminalErrorCategory.AUDIO_PATH_EXISTS,
    }
)
_ATTEMPT_AND_ANALYSIS_ERROR_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.ATTEMPT_ID_FAILED,
        TerminalErrorCategory.ATTEMPT_QUEUE_COMMIT_FAILED,
        TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
        TerminalErrorCategory.ANALYSIS_QUEUE_FULL,
        TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING,
        TerminalErrorCategory.ANALYSIS_INVALID,
        TerminalErrorCategory.ANALYSIS_FAILED,
        TerminalErrorCategory.ANALYSIS_CANCELLED,
    }
)
_ATTEMPT_ID_REQUIRED_ERROR_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.ATTEMPT_QUEUE_COMMIT_FAILED,
        TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
        TerminalErrorCategory.ANALYSIS_QUEUE_FULL,
        TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING,
        TerminalErrorCategory.ANALYSIS_INVALID,
        TerminalErrorCategory.ANALYSIS_FAILED,
        TerminalErrorCategory.ANALYSIS_CANCELLED,
    }
)
_REANALYSIS_ERROR_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
        TerminalErrorCategory.ANALYSIS_INVALID,
        TerminalErrorCategory.ANALYSIS_FAILED,
        TerminalErrorCategory.ANALYSIS_CANCELLED,
    }
)
_PREACCEPT_START_FACTS = (
    (
        StartStatus.PLC_INGRESS_REJECTED,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PLC_INGRESS_REJECTED,
    ),
    (
        StartStatus.PREFLIGHT_REJECTED,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PREFLIGHT_REJECTED,
    ),
    (
        StartStatus.INVALID_FROZEN_INPUT,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.INVALID_FROZEN_INPUT,
    ),
    (
        StartStatus.CAPTURE_CAPACITY,
        TerminalConclusion.CAPACITY_REJECTED,
        TerminalErrorCategory.CAPTURE_CAPACITY,
    ),
    (
        StartStatus.RESIDENT_UNAVAILABLE,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.RESIDENT_UNAVAILABLE,
    ),
    (
        StartStatus.ACTIVE_COMMIT_REJECTED,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
    ),
)
_INITIAL_IMPORTED_ERROR_CATEGORIES = (
    _SAVE_ERROR_CATEGORIES
    | _ATTEMPT_AND_ANALYSIS_ERROR_CATEGORIES
    | frozenset({TerminalErrorCategory.SHUTDOWN_CANCELLED})
)
_INITIAL_CAPTURED_ERROR_CATEGORIES = (
    _CAPTURE_POSTACCEPT_CATEGORIES
    | _SAVE_ERROR_CATEGORIES
    | _ATTEMPT_AND_ANALYSIS_ERROR_CATEGORIES
    | frozenset(
        {
            TerminalErrorCategory.PLC_ACTIVATION_FAILED,
            TerminalErrorCategory.SHUTDOWN_CANCELLED,
            TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
        }
    )
)
_INITIAL_CAPTURED_PLC_ONLY_ERROR_CATEGORIES = frozenset(
    {
        TerminalErrorCategory.PLC_ACTIVATION_FAILED,
        TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
    }
)


class OperationStatus(Enum):
    COMPLETED = auto()
    SUBMITTED = auto()
    INVALID_REQUEST = auto()
    INGRESS_CLOSED = auto()
    NOT_FOUND = auto()
    NOT_ALLOWED = auto()
    OPERATION_BUSY = auto()
    STALE_REVISION = auto()
    QUEUE_FULL = auto()
    PERSISTENCE_REJECTED = auto()
    SCHEDULER_REJECTED = auto()
    EVICTED = auto()


class ResultOperationPhase(Enum):
    LOADING_FACTS = auto()
    MANUAL_COMMITTING = auto()
    ATTEMPT_QUEUE_COMMITTING = auto()
    SCHEDULER_SUBMITTING = auto()
    ANALYSIS_PENDING = auto()
    CANCEL_COMMITTING = auto()
    TERMINAL_COMMITTING = auto()


class ManualProjectionStatus(Enum):
    COMMITTED = auto()
    REJECTED = auto()
    UNAVAILABLE = auto()


class StopStatus(Enum):
    SETTLED = auto()
    IN_PROGRESS = auto()
    INCOMPLETE = auto()
    CLOSED = auto()


class CoordinatorShutdownStatus(Enum):
    CLOSED = auto()
    INCOMPLETE = auto()


class ResidentCancelStatus(Enum):
    CANCELLED = auto()
    ALREADY_TERMINAL = auto()
    INVALID = auto()


class SaveCancelStatus(Enum):
    CANCEL_REQUESTED = auto()
    ALREADY_TERMINAL = auto()
    NOT_FOUND = auto()


class SchedulerCancelStatus(Enum):
    CANCELED_PENDING = auto()
    CANCEL_REQUESTED_RUNNING = auto()
    ALREADY_TERMINAL = auto()
    NOT_FOUND = auto()
    RUN_ID_MISMATCH = auto()


class CapturedSourceFormat(Enum):
    FLOAT32_INTERLEAVED = auto()
    INT16_INTERLEAVED = auto()


class ImportedSourceFormat(Enum):
    PCM16_WAV = auto()


class PersistedAudioFormat(Enum):
    PCM16_WAV = auto()


class AnalysisAttemptKind(Enum):
    INITIAL_CAPTURED = auto()
    INITIAL_IMPORTED = auto()
    REANALYSIS = auto()


class AnalysisOutcomeStatus(Enum):
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


class ExternalActionKind(Enum):
    PLC_REGISTER = auto()
    FREEZE = auto()
    RESIDENT_RESERVE = auto()
    ACTIVE_COMMIT = auto()
    PLC_MARK = auto()
    CAPTURE_REGISTER = auto()
    ACTIVE_HISTORY = auto()
    CAPTURE_START = auto()
    SAVE_SUBMIT = auto()
    ATTEMPT_COMMIT = auto()
    WAITING_HISTORY = auto()
    SCHEDULER_SUBMIT = auto()
    ANALYZING_HISTORY = auto()
    TERMINAL_COMMIT = auto()
    RESULT_FACTS_LOAD = auto()
    MANUAL_COMMIT = auto()
    REANALYSIS_CANCEL = auto()
    CHILD_CANCEL = auto()
    TERMINAL_HISTORY = auto()
    PLC_SETTLEMENT = auto()


class TerminalContenderKind(Enum):
    CAPTURE_OUTCOME = auto()
    SAVE_OUTCOME = auto()
    ANALYSIS_OUTCOME = auto()
    STOP = auto()
    SHUTDOWN = auto()
    EXTERNAL_FAILURE = auto()


class ResultProjectionPhase(Enum):
    RECORDING = auto()
    WAITING_SAVE = auto()
    WAITING_ANALYSIS = auto()
    ANALYZING = auto()
    REANALYZING = auto()
    TERMINAL = auto()


class HistoryFactKind(Enum):
    RECORDING = auto()
    WAITING_SAVE = auto()
    WAITING_ANALYSIS = auto()
    ANALYZING = auto()
    RUN_TERMINATED = auto()
    REANALYSIS_STARTED = auto()
    REANALYSIS_COMPLETED = auto()
    REANALYSIS_CANCELLED = auto()
    TERMINAL_RESULT_REPLACED = auto()


class AudioSaveSourceKind(Enum):
    CAPTURED_AUDIO = auto()
    IMPORTED_SOURCE = auto()


class AudioSaveStatus(Enum):
    SAVED = auto()
    CANCELLED = auto()
    FAILED = auto()
    PATH_EXISTS = auto()


class SaveSubmitStatus(Enum):
    ACCEPTED = auto()
    SATURATED = auto()
    CLOSED = auto()
    INVALID = auto()


class SaveDispatcherShutdownStatus(Enum):
    CLOSED = auto()
    INCOMPLETE = auto()


class CommitResult(Enum):
    COMMITTED = auto()
    REJECTED = auto()
    UNAVAILABLE = auto()


class SchedulerSubmitStatus(Enum):
    ACCEPTED = auto()
    QUEUE_FULL = auto()
    NOT_ACCEPTING = auto()
    INVALID = auto()


class CoordinatorLifecycle(Enum):
    OPEN = auto()
    STOPPING = auto()
    CLOSED = auto()


class _AbsentRevision:
    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _AbsentRevision()
ExpectedRevision: TypeAlias = int | _AbsentRevision


@dataclass(frozen=True, slots=True)
class FrozenPayload:
    value_id: str
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        _require_nonempty_text(self.value_id, "value_id")
        if type(self.canonical_bytes) is not bytes or not self.canonical_bytes:
            raise ValueError("canonical_bytes must be nonempty immutable bytes")


@dataclass(frozen=True, slots=True)
class CapturedStartRequest:
    run_id: str
    trigger_monotonic_ns: int
    source: TriggerSource
    ingress_payload: FrozenPayload
    fallback_history_labels: FrozenPayload
    plc_id: int | None

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_positive_int(self.trigger_monotonic_ns, "trigger_monotonic_ns")
        if self.source not in {
            TriggerSource.PLC,
            TriggerSource.MANUAL,
            TriggerSource.SCANNER,
        }:
            raise ValueError("captured source must be PLC, MANUAL, or SCANNER")
        if not isinstance(self.ingress_payload, FrozenPayload):
            raise ValueError("ingress_payload must be a FrozenPayload")
        if not isinstance(self.fallback_history_labels, FrozenPayload):
            raise ValueError("fallback_history_labels must be a FrozenPayload")
        if self.source is TriggerSource.PLC:
            _require_positive_int(self.plc_id, "plc_id")
        elif self.plc_id is not None:
            raise ValueError("non-PLC source must not include a PLC ID")


@dataclass(frozen=True, slots=True)
class ImportedStartRequest:
    run_id: str
    trigger_monotonic_ns: int
    source: TriggerSource
    ingress_payload: FrozenPayload
    fallback_history_labels: FrozenPayload

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_positive_int(self.trigger_monotonic_ns, "trigger_monotonic_ns")
        if self.source is not TriggerSource.IMPORT:
            raise ValueError("imported source must be IMPORT")
        if not isinstance(self.ingress_payload, FrozenPayload):
            raise ValueError("ingress_payload must be a FrozenPayload")
        if not isinstance(self.fallback_history_labels, FrozenPayload):
            raise ValueError("fallback_history_labels must be a FrozenPayload")


@dataclass(frozen=True, slots=True)
class ManualJudgmentRequest:
    run_id: str
    expected_revision: int
    conclusion: TerminalConclusion
    fault_type: str | None
    bounded_note: str

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_positive_int(self.expected_revision, "expected_revision")
        if self.conclusion not in {TerminalConclusion.OK, TerminalConclusion.NG}:
            raise ValueError("manual conclusion must be OK or NG")
        _require_optional_nonempty_text(self.fault_type, "fault_type")
        require_bounded_text(self.bounded_note, "bounded_note")


@dataclass(frozen=True, slots=True)
class ReanalysisRequest:
    run_id: str
    expected_revision: int
    start_sec: float
    end_sec: float

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_positive_int(self.expected_revision, "expected_revision")
        if type(self.start_sec) is not float:
            raise TypeError("start_sec must be an exact float")
        if type(self.end_sec) is not float:
            raise TypeError("end_sec must be an exact float")
        if (
            not math.isfinite(self.start_sec)
            or not math.isfinite(self.end_sec)
            or self.start_sec < 0.0
            or self.end_sec <= self.start_sec
        ):
            raise ValueError(
                "reanalysis window must be finite and satisfy 0 <= start < end"
            )


@dataclass(frozen=True, slots=True)
class ReanalysisCancelRequest:
    run_id: str
    attempt_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        require_uuid4(self.attempt_id, "attempt_id")
        _require_positive_int(self.expected_revision, "expected_revision")


@dataclass(frozen=True, slots=True)
class CapturedAudioValue:
    source_identity: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    format: CapturedSourceFormat
    interleaved_bytes: bytes
    channel_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_nonempty_text(self.source_identity, "source_identity")
        _require_positive_int(self.sample_rate_hz, "sample_rate_hz")
        _require_positive_int(self.channel_count, "channel_count")
        _require_positive_int(self.frame_count, "frame_count")
        if not isinstance(self.format, CapturedSourceFormat):
            raise ValueError("format must be a CapturedSourceFormat")
        if type(self.interleaved_bytes) is not bytes:
            raise ValueError("interleaved_bytes must be immutable bytes")
        if (
            type(self.channel_indices) is not tuple
            or len(self.channel_indices) != self.channel_count
            or len(set(self.channel_indices)) != len(self.channel_indices)
            or any(
                type(channel) is not int or channel < 0
                for channel in self.channel_indices
            )
        ):
            raise ValueError(
                "channel_indices must be exact unique nonnegative integers"
            )
        bytes_per_sample = (
            4
            if self.format is CapturedSourceFormat.FLOAT32_INTERLEAVED
            else 2
        )
        expected_length = self.frame_count * self.channel_count * bytes_per_sample
        if len(self.interleaved_bytes) != expected_length:
            raise ValueError("interleaved_bytes length does not match audio metadata")


@dataclass(frozen=True, slots=True)
class ImportedSourceReference:
    source_identity: str
    absolute_path: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    format: ImportedSourceFormat

    def __post_init__(self) -> None:
        _require_nonempty_text(self.source_identity, "source_identity")
        _require_absolute_path(self.absolute_path, "absolute_path")
        _require_positive_int(self.sample_rate_hz, "sample_rate_hz")
        _require_positive_int(self.channel_count, "channel_count")
        _require_positive_int(self.frame_count, "frame_count")
        if self.format is not ImportedSourceFormat.PCM16_WAV:
            raise ValueError("imported format must be PCM16_WAV")


@dataclass(frozen=True, slots=True)
class FinalPathRequest:
    final_path: str

    def __post_init__(self) -> None:
        _require_absolute_path(self.final_path, "final_path")


@dataclass(frozen=True, slots=True)
class AudioMetadataConstraints:
    expected_sample_rate_hz: int
    expected_channel_count: int
    expected_frame_count: int
    format: PersistedAudioFormat

    def __post_init__(self) -> None:
        _require_positive_int(
            self.expected_sample_rate_hz, "expected_sample_rate_hz"
        )
        _require_positive_int(
            self.expected_channel_count, "expected_channel_count"
        )
        _require_positive_int(self.expected_frame_count, "expected_frame_count")
        if self.format is not PersistedAudioFormat.PCM16_WAV:
            raise ValueError("persisted format must be PCM16_WAV")


@dataclass(frozen=True, slots=True)
class FrozenCapturedRun:
    run_id: str
    trigger_monotonic_ns: int
    source: TriggerSource
    config_id: str
    hardware_id: str
    target_frame_count: int
    config_payload: FrozenPayload
    hardware_payload: FrozenPayload
    calibration_payload: FrozenPayload
    work_order_payload: FrozenPayload
    history_labels: FrozenPayload
    analysis_input: FrozenPayload
    final_path_request: FinalPathRequest
    metadata: AudioMetadataConstraints

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_positive_int(self.trigger_monotonic_ns, "trigger_monotonic_ns")
        if self.source not in {
            TriggerSource.PLC,
            TriggerSource.MANUAL,
            TriggerSource.SCANNER,
        }:
            raise ValueError("captured source must be PLC, MANUAL, or SCANNER")
        _require_nonempty_text(self.config_id, "config_id")
        _require_nonempty_text(self.hardware_id, "hardware_id")
        _require_positive_int(self.target_frame_count, "target_frame_count")
        for field_name in (
            "config_payload",
            "hardware_payload",
            "calibration_payload",
            "work_order_payload",
            "history_labels",
            "analysis_input",
        ):
            if not isinstance(getattr(self, field_name), FrozenPayload):
                raise ValueError(f"{field_name} must be a FrozenPayload")
        if not isinstance(self.final_path_request, FinalPathRequest):
            raise ValueError("final_path_request must be a FinalPathRequest")
        if not isinstance(self.metadata, AudioMetadataConstraints):
            raise ValueError("metadata must be AudioMetadataConstraints")
        if self.target_frame_count != self.metadata.expected_frame_count:
            raise ValueError("target frame count must match expected metadata")


@dataclass(frozen=True, slots=True)
class FrozenImportedRun:
    run_id: str
    trigger_monotonic_ns: int
    source: TriggerSource
    config_id: str
    source_reference: ImportedSourceReference
    config_payload: FrozenPayload
    calibration_payload: FrozenPayload
    work_order_payload: FrozenPayload
    history_labels: FrozenPayload
    analysis_input: FrozenPayload
    final_path_request: FinalPathRequest
    metadata: AudioMetadataConstraints

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_positive_int(self.trigger_monotonic_ns, "trigger_monotonic_ns")
        if self.source is not TriggerSource.IMPORT:
            raise ValueError("imported source must be IMPORT")
        _require_nonempty_text(self.config_id, "config_id")
        if not isinstance(self.source_reference, ImportedSourceReference):
            raise ValueError("source_reference must be an ImportedSourceReference")
        for field_name in (
            "config_payload",
            "calibration_payload",
            "work_order_payload",
            "history_labels",
            "analysis_input",
        ):
            if not isinstance(getattr(self, field_name), FrozenPayload):
                raise ValueError(f"{field_name} must be a FrozenPayload")
        if not isinstance(self.final_path_request, FinalPathRequest):
            raise ValueError("final_path_request must be a FinalPathRequest")
        if not isinstance(self.metadata, AudioMetadataConstraints):
            raise ValueError("metadata must be AudioMetadataConstraints")
        _validate_metadata_match(self.source_reference, self.metadata)


@dataclass(frozen=True, slots=True)
class CaptureReservation:
    reservation_id: str
    run_id: str
    hardware_id: str

    def __post_init__(self) -> None:
        _require_nonempty_text(self.reservation_id, "reservation_id")
        require_uuid4(self.run_id, "run_id")
        _require_nonempty_text(self.hardware_id, "hardware_id")


@dataclass(frozen=True, slots=True, init=False, eq=False)
class CaptureReserveClaim:
    claim_id: str
    run_id: str
    hardware_id: str
    trigger_monotonic_ns: int
    deadline_monotonic_ns: int
    _owner_nonce: object = field(repr=False)

    def __new__(cls) -> CaptureReserveClaim:
        raise TypeError("CaptureReserveClaim is issued only by its Resident port")


def _issue_capture_reserve_claim(
    *,
    claim_id: str,
    run_id: str,
    hardware_id: str,
    trigger_monotonic_ns: int,
    deadline_monotonic_ns: int,
    owner_nonce: object,
) -> CaptureReserveClaim:
    _require_nonempty_text(claim_id, "claim_id")
    require_uuid4(run_id, "run_id")
    _require_nonempty_text(hardware_id, "hardware_id")
    if type(trigger_monotonic_ns) is not int or trigger_monotonic_ns <= 0:
        raise ValueError("trigger_monotonic_ns must be a positive integer")
    if type(deadline_monotonic_ns) is not int or deadline_monotonic_ns <= 0:
        raise ValueError("deadline_monotonic_ns must be a positive integer")
    if owner_nonce is None:
        raise ValueError("owner_nonce must authenticate a Resident port")
    claim = object.__new__(CaptureReserveClaim)
    object.__setattr__(claim, "claim_id", claim_id)
    object.__setattr__(claim, "run_id", run_id)
    object.__setattr__(claim, "hardware_id", hardware_id)
    object.__setattr__(claim, "trigger_monotonic_ns", trigger_monotonic_ns)
    object.__setattr__(claim, "deadline_monotonic_ns", deadline_monotonic_ns)
    object.__setattr__(claim, "_owner_nonce", owner_nonce)
    return claim


def _validate_capture_reserve_claim(
    value: object,
    *,
    owner_nonce: object,
) -> CaptureReserveClaim:
    if type(value) is not CaptureReserveClaim:
        raise ValueError("claim must be an exact CaptureReserveClaim")
    try:
        claim_id = value.claim_id
        run_id = value.run_id
        hardware_id = value.hardware_id
        trigger_monotonic_ns = value.trigger_monotonic_ns
        deadline_monotonic_ns = value.deadline_monotonic_ns
        actual_owner = value._owner_nonce
    except AttributeError:
        raise ValueError("capture reserve claim is malformed") from None
    _require_nonempty_text(claim_id, "claim_id")
    require_uuid4(run_id, "run_id")
    _require_nonempty_text(hardware_id, "hardware_id")
    if type(trigger_monotonic_ns) is not int or trigger_monotonic_ns <= 0:
        raise ValueError("capture reserve claim trigger is malformed")
    if type(deadline_monotonic_ns) is not int or deadline_monotonic_ns <= 0:
        raise ValueError("capture reserve claim deadline is malformed")
    if actual_owner is not owner_nonce:
        raise ValueError("capture reserve claim belongs to another Resident port")
    return value


@dataclass(frozen=True, slots=True, init=False, eq=False)
class CaptureReserveWakeSignal:
    claim_id: str
    run_id: str
    deadline_monotonic_ns: int
    _owner_nonce: object = field(repr=False)
    _wait: Callable[[float | None], object] = field(repr=False)
    _cancelled: threading.Event = field(repr=False)

    def __new__(cls) -> CaptureReserveWakeSignal:
        raise TypeError(
            "CaptureReserveWakeSignal is issued only by its Resident port"
        )

    def wait(self) -> None:
        while not self._cancelled.is_set():
            remaining_ns = self.deadline_monotonic_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                return
            outcome = self._wait(
                min(remaining_ns / 1_000_000_000, 0.05)
            )
            if outcome is not None:
                return

    def _cancel(self) -> None:
        self._cancelled.set()


def _issue_capture_reserve_wake_signal(
    *,
    claim: CaptureReserveClaim,
    owner_nonce: object,
    wait: Callable[[float | None], object],
) -> CaptureReserveWakeSignal:
    if type(claim) is not CaptureReserveClaim:
        raise ValueError("wake signal requires an exact CaptureReserveClaim")
    if claim._owner_nonce is not owner_nonce:
        raise ValueError("wake signal claim belongs to another Resident port")
    if not callable(wait):
        raise ValueError("wake signal wait must be callable")
    signal = object.__new__(CaptureReserveWakeSignal)
    object.__setattr__(signal, "claim_id", claim.claim_id)
    object.__setattr__(signal, "run_id", claim.run_id)
    object.__setattr__(
        signal, "deadline_monotonic_ns", claim.deadline_monotonic_ns
    )
    object.__setattr__(signal, "_owner_nonce", owner_nonce)
    object.__setattr__(signal, "_wait", wait)
    object.__setattr__(signal, "_cancelled", threading.Event())
    return signal


@dataclass(frozen=True, slots=True)
class CaptureReservePending:
    run_id: str
    hardware_id: str
    claim: CaptureReserveClaim
    wake_signal: CaptureReserveWakeSignal

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        _require_nonempty_text(self.hardware_id, "hardware_id")
        if type(self.claim) is not CaptureReserveClaim:
            raise ValueError("claim must be an exact CaptureReserveClaim")
        if type(self.wake_signal) is not CaptureReserveWakeSignal:
            raise ValueError(
                "wake_signal must be an exact CaptureReserveWakeSignal"
            )
        if (
            self.claim.run_id != self.run_id
            or self.claim.hardware_id != self.hardware_id
            or self.wake_signal.run_id != self.run_id
            or self.wake_signal.claim_id != self.claim.claim_id
            or self.wake_signal.deadline_monotonic_ns
            != self.claim.deadline_monotonic_ns
            or self.wake_signal._owner_nonce is not self.claim._owner_nonce
        ):
            raise ValueError(
                "pending claim and wake signal identity must match its result"
            )


@dataclass(frozen=True, slots=True)
class CaptureReserveResult:
    status: CaptureReserveStatus
    run_id: str
    hardware_id: str
    reservation: CaptureReservation | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, CaptureReserveStatus):
            raise ValueError("status must be a CaptureReserveStatus")
        require_uuid4(self.run_id, "run_id")
        _require_nonempty_text(self.hardware_id, "hardware_id")
        require_bounded_text(self.reason, "reason")
        if self.status is CaptureReserveStatus.RESERVED:
            if not isinstance(self.reservation, CaptureReservation) or self.reason:
                raise ValueError("RESERVED requires a reservation and no reason")
            if (
                self.reservation.run_id != self.run_id
                or self.reservation.hardware_id != self.hardware_id
            ):
                raise ValueError("reservation identity must match result")
        elif self.reservation is not None:
            raise ValueError("non-RESERVED result must not include a reservation")


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    run_id: str
    reservation: CaptureReservation
    status: CaptureOutcomeStatus
    audio: CapturedAudioValue | None
    error_category: TerminalErrorCategory | None
    reason: str

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.reservation, CaptureReservation):
            raise ValueError("reservation must be a CaptureReservation")
        if self.reservation.run_id != self.run_id:
            raise ValueError("reservation run identity must match outcome")
        if not isinstance(self.status, CaptureOutcomeStatus):
            raise ValueError("status must be a CaptureOutcomeStatus")
        require_bounded_text(self.reason, "reason")
        if self.status is CaptureOutcomeStatus.COMPLETED:
            if (
                not isinstance(self.audio, CapturedAudioValue)
                or self.error_category is not None
                or self.reason
            ):
                raise ValueError("COMPLETED requires audio and no error")
        else:
            expected_category = (
                TerminalErrorCategory.CAPTURE_CANCELLED
                if self.status is CaptureOutcomeStatus.CANCELLED
                else TerminalErrorCategory.CAPTURE_FAILED
            )
            if (
                self.audio is not None
                or self.error_category is not expected_category
                or not self.reason
            ):
                raise ValueError("capture failure outcome is incoherent")


@dataclass(frozen=True, slots=True)
class PersistedAudioReference:
    audio_id: str
    save_job_id: str
    run_id: str
    source_kind: AudioSaveSourceKind
    source_identity: str
    final_path: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    byte_size: int
    format: PersistedAudioFormat
    content_revision: int

    def __post_init__(self) -> None:
        require_uuid4(self.audio_id, "audio_id")
        require_uuid4(self.save_job_id, "save_job_id")
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.source_kind, AudioSaveSourceKind):
            raise ValueError("source_kind must be an AudioSaveSourceKind")
        _require_nonempty_text(self.source_identity, "source_identity")
        _require_absolute_path(self.final_path, "final_path")
        _require_positive_int(self.sample_rate_hz, "sample_rate_hz")
        _require_positive_int(self.channel_count, "channel_count")
        _require_positive_int(self.frame_count, "frame_count")
        _require_positive_int(self.byte_size, "byte_size")
        if self.format is not PersistedAudioFormat.PCM16_WAV:
            raise ValueError("persisted format must be PCM16_WAV")
        _require_positive_int(self.content_revision, "content_revision")


def _validate_attempt_audio_kind(
    attempt_kind: AnalysisAttemptKind,
    audio: PersistedAudioReference,
) -> None:
    if not isinstance(attempt_kind, AnalysisAttemptKind):
        raise ValueError("attempt_kind must be an AnalysisAttemptKind")
    if (
        attempt_kind is AnalysisAttemptKind.INITIAL_CAPTURED
        and audio.source_kind is not AudioSaveSourceKind.CAPTURED_AUDIO
    ):
        raise ValueError("captured attempt requires captured audio")
    if (
        attempt_kind is AnalysisAttemptKind.INITIAL_IMPORTED
        and audio.source_kind is not AudioSaveSourceKind.IMPORTED_SOURCE
    ):
        raise ValueError("imported attempt requires imported audio")


@dataclass(frozen=True, slots=True)
class PreparedAnalysisAttempt:
    attempt_id: str
    run_id: str
    attempt_kind: AnalysisAttemptKind
    audio: PersistedAudioReference
    analysis_input: FrozenPayload

    def __post_init__(self) -> None:
        _validate_analysis_input(self)


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    attempt_id: str
    run_id: str
    attempt_kind: AnalysisAttemptKind
    audio: PersistedAudioReference
    analysis_input: FrozenPayload

    def __post_init__(self) -> None:
        _validate_analysis_input(self)


def _validate_analysis_input(value: PreparedAnalysisAttempt | AnalysisJob) -> None:
    require_uuid4(value.attempt_id, "attempt_id")
    require_uuid4(value.run_id, "run_id")
    if not isinstance(value.audio, PersistedAudioReference):
        raise ValueError("audio must be a PersistedAudioReference")
    if value.audio.run_id != value.run_id:
        raise ValueError("audio run identity must match analysis input")
    _validate_attempt_audio_kind(value.attempt_kind, value.audio)
    if not isinstance(value.analysis_input, FrozenPayload):
        raise ValueError("analysis_input must be a FrozenPayload")


def _validate_terminal_combination(
    conclusion: TerminalConclusion | None,
    error_category: TerminalErrorCategory | None,
    reason: str,
    result_payload: FrozenPayload | None,
    *,
    allow_none: bool,
) -> None:
    require_bounded_text(reason, "reason")
    if conclusion is None:
        if allow_none and error_category is None and not reason and result_payload is None:
            return
        raise ValueError("terminal conclusion combination is incomplete")
    if not isinstance(conclusion, TerminalConclusion):
        raise ValueError("conclusion must be a TerminalConclusion")
    if error_category is not None and not isinstance(
        error_category, TerminalErrorCategory
    ):
        raise ValueError("error_category must be a TerminalErrorCategory")
    if result_payload is not None and not isinstance(result_payload, FrozenPayload):
        raise ValueError("result_payload must be a FrozenPayload")
    if conclusion in {TerminalConclusion.OK, TerminalConclusion.NG}:
        if result_payload is None or error_category is not None or reason:
            raise ValueError("OK/NG requires payload and no error")
    elif conclusion is TerminalConclusion.NO_RESULT:
        if result_payload is not None or error_category is not None or reason:
            raise ValueError("NO_RESULT must not include payload or error")
    elif conclusion is TerminalConclusion.ERROR:
        if result_payload is not None or error_category is None:
            raise ValueError("ERROR requires a category and no payload")
    elif conclusion is TerminalConclusion.CAPACITY_REJECTED:
        if (
            result_payload is not None
            or error_category is not TerminalErrorCategory.CAPTURE_CAPACITY
        ):
            raise ValueError("CAPACITY_REJECTED requires CAPTURE_CAPACITY")
    elif conclusion is TerminalConclusion.MANUAL_STOPPED:
        if (
            result_payload is not None
            or error_category is not TerminalErrorCategory.MANUAL_STOPPED
        ):
            raise ValueError("MANUAL_STOPPED requires MANUAL_STOPPED category")


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    attempt_id: str
    run_id: str
    attempt_kind: AnalysisAttemptKind
    audio_id: str
    audio_content_revision: int
    status: AnalysisOutcomeStatus
    conclusion: TerminalConclusion
    result_payload: FrozenPayload | None
    error_category: TerminalErrorCategory | None
    reason: str

    def __post_init__(self) -> None:
        require_uuid4(self.attempt_id, "attempt_id")
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.attempt_kind, AnalysisAttemptKind):
            raise ValueError("attempt_kind must be an AnalysisAttemptKind")
        require_uuid4(self.audio_id, "audio_id")
        _require_positive_int(
            self.audio_content_revision, "audio_content_revision"
        )
        if not isinstance(self.status, AnalysisOutcomeStatus):
            raise ValueError("status must be an AnalysisOutcomeStatus")
        require_bounded_text(self.reason, "reason")
        if self.status is AnalysisOutcomeStatus.COMPLETED:
            if self.conclusion not in {
                TerminalConclusion.OK,
                TerminalConclusion.NG,
                TerminalConclusion.NO_RESULT,
            }:
                raise ValueError("completed analysis conclusion is invalid")
            _validate_terminal_combination(
                self.conclusion,
                self.error_category,
                self.reason,
                self.result_payload,
                allow_none=False,
            )
        elif self.status is AnalysisOutcomeStatus.FAILED:
            if (
                self.conclusion is not TerminalConclusion.ERROR
                or self.result_payload is not None
                or self.error_category
                not in {
                    TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
                    TerminalErrorCategory.ANALYSIS_INVALID,
                    TerminalErrorCategory.ANALYSIS_FAILED,
                }
            ):
                raise ValueError("failed analysis outcome is incoherent")
        elif (
            self.conclusion is not TerminalConclusion.ERROR
            or self.result_payload is not None
            or self.error_category is not TerminalErrorCategory.ANALYSIS_CANCELLED
        ):
            raise ValueError("cancelled analysis outcome is incoherent")


def _validate_terminal_fact_lifecycle(
    source: TriggerSource,
    attempt_kind: AnalysisAttemptKind,
    attempt_id: str | None,
    conclusion: TerminalConclusion,
    error_category: TerminalErrorCategory | None,
) -> None:
    if conclusion is TerminalConclusion.CAPACITY_REJECTED:
        raise ValueError("CAPACITY_REJECTED is preaccept-only")
    if error_category in _PREACCEPT_ONLY_CATEGORIES:
        raise ValueError("preaccept-only category is invalid after acceptance")
    if conclusion in {
        TerminalConclusion.OK,
        TerminalConclusion.NG,
        TerminalConclusion.NO_RESULT,
    } and attempt_id is None:
        raise ValueError("completed terminal requires an attempt_id")
    if (
        error_category in _ATTEMPT_ID_REQUIRED_ERROR_CATEGORIES
        and attempt_id is None
    ):
        raise ValueError("attempt-stage terminal requires an attempt_id")
    if (
        error_category is TerminalErrorCategory.ATTEMPT_ID_FAILED
        and attempt_id is not None
    ):
        raise ValueError("ATTEMPT_ID_FAILED requires no attempt_id")

    if attempt_kind is AnalysisAttemptKind.REANALYSIS:
        if conclusion in {
            TerminalConclusion.OK,
            TerminalConclusion.NG,
            TerminalConclusion.NO_RESULT,
        }:
            return
        if (
            conclusion is TerminalConclusion.ERROR
            and error_category in _REANALYSIS_ERROR_CATEGORIES
        ):
            return
        raise ValueError("reanalysis terminal category is invalid")

    if conclusion in {
        TerminalConclusion.OK,
        TerminalConclusion.NG,
        TerminalConclusion.NO_RESULT,
        TerminalConclusion.MANUAL_STOPPED,
    }:
        return
    if conclusion is not TerminalConclusion.ERROR:
        raise ValueError("initial terminal conclusion is invalid")

    allowed_categories = (
        _INITIAL_IMPORTED_ERROR_CATEGORIES
        if attempt_kind is AnalysisAttemptKind.INITIAL_IMPORTED
        else _INITIAL_CAPTURED_ERROR_CATEGORIES
    )
    if error_category not in allowed_categories:
        raise ValueError("initial terminal category is invalid")
    if (
        attempt_kind is AnalysisAttemptKind.INITIAL_CAPTURED
        and error_category in _INITIAL_CAPTURED_PLC_ONLY_ERROR_CATEGORIES
        and source is not TriggerSource.PLC
    ):
        raise ValueError("PLC-only terminal category requires PLC source")


@dataclass(frozen=True, slots=True)
class TerminalFact:
    run_id: str
    source: TriggerSource
    attempt_kind: AnalysisAttemptKind
    attempt_id: str | None
    conclusion: TerminalConclusion
    error_category: TerminalErrorCategory | None
    reason: str
    result_payload: FrozenPayload | None
    history_labels: FrozenPayload
    terminal_at: datetime

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.source, TriggerSource):
            raise ValueError("source must be a TriggerSource")
        if not isinstance(self.attempt_kind, AnalysisAttemptKind):
            raise ValueError("attempt_kind must be an AnalysisAttemptKind")
        _require_optional_uuid4(self.attempt_id, "attempt_id")
        if (
            self.attempt_kind is AnalysisAttemptKind.REANALYSIS
            and self.attempt_id is None
        ):
            raise ValueError("reanalysis terminal requires an attempt_id")
        if (
            self.attempt_kind is AnalysisAttemptKind.INITIAL_CAPTURED
            and self.source is TriggerSource.IMPORT
        ):
            raise ValueError("captured attempt cannot have IMPORT source")
        if (
            self.attempt_kind is AnalysisAttemptKind.INITIAL_IMPORTED
            and self.source is not TriggerSource.IMPORT
        ):
            raise ValueError("imported attempt requires IMPORT source")
        _validate_terminal_combination(
            self.conclusion,
            self.error_category,
            self.reason,
            self.result_payload,
            allow_none=False,
        )
        _validate_terminal_fact_lifecycle(
            self.source,
            self.attempt_kind,
            self.attempt_id,
            self.conclusion,
            self.error_category,
        )
        if not isinstance(self.history_labels, FrozenPayload):
            raise ValueError("history_labels must be a FrozenPayload")
        _require_aware_datetime(self.terminal_at, "terminal_at")


@dataclass(frozen=True, slots=True)
class PreacceptTerminalFact:
    run_id: str
    source: TriggerSource
    config_id: str | None
    conclusion: TerminalConclusion
    error_category: TerminalErrorCategory
    reason: str
    history_labels: FrozenPayload
    terminal_at: datetime

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.source, TriggerSource):
            raise ValueError("source must be a TriggerSource")
        _require_optional_nonempty_text(self.config_id, "config_id")
        if self.conclusion not in {
            TerminalConclusion.ERROR,
            TerminalConclusion.CAPACITY_REJECTED,
        }:
            raise ValueError("preaccept conclusion must be ERROR or CAPACITY_REJECTED")
        _validate_terminal_combination(
            self.conclusion,
            self.error_category,
            self.reason,
            None,
            allow_none=False,
        )
        if (
            self.conclusion is TerminalConclusion.ERROR
            and self.error_category not in _PREACCEPT_ERROR_CATEGORIES
        ):
            raise ValueError("preaccept error category is invalid")
        if (
            self.error_category is TerminalErrorCategory.PLC_INGRESS_REJECTED
            and self.source is not TriggerSource.PLC
        ):
            raise ValueError("PLC ingress rejection requires PLC source")
        if (
            self.error_category
            in {
                TerminalErrorCategory.CAPTURE_CAPACITY,
                TerminalErrorCategory.RESIDENT_UNAVAILABLE,
            }
            and self.source is TriggerSource.IMPORT
        ):
            raise ValueError("capture preaccept category cannot use IMPORT source")
        if not isinstance(self.history_labels, FrozenPayload):
            raise ValueError("history_labels must be a FrozenPayload")
        _require_aware_datetime(self.terminal_at, "terminal_at")


@dataclass(frozen=True, slots=True)
class StartResult:
    run_id: str
    status: StartStatus
    accepted: bool
    phase: CoordinatorPhase | None
    terminal: TerminalFact | PreacceptTerminalFact | None
    reason: str

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.status, StartStatus):
            raise ValueError("status must be a StartStatus")
        _require_bool(self.accepted, "accepted")
        if self.phase is not None and not isinstance(self.phase, CoordinatorPhase):
            raise ValueError("phase must be a CoordinatorPhase")
        require_bounded_text(self.reason, "reason")
        if (
            isinstance(self.terminal, (TerminalFact, PreacceptTerminalFact))
            and self.terminal.run_id != self.run_id
        ):
            raise ValueError("terminal run identity must match result")
        if self.status is StartStatus.ACCEPTED_ACTIVE:
            if (
                not self.accepted
                or self.phase
                not in {
                    CoordinatorPhase.REGISTERING,
                    CoordinatorPhase.STARTING,
                    CoordinatorPhase.RECORDING,
                    CoordinatorPhase.SAVE_SUBMITTING,
                    CoordinatorPhase.SAVING,
                    CoordinatorPhase.ATTEMPT_COMMITTING,
                    CoordinatorPhase.ANALYSIS_SUBMITTING,
                    CoordinatorPhase.ANALYSIS_PENDING,
                }
                or self.terminal is not None
            ):
                raise ValueError("ACCEPTED_ACTIVE result is incoherent")
        elif self.status is StartStatus.ACCEPTED_TERMINAL:
            if not self.accepted or not isinstance(self.terminal, TerminalFact):
                raise ValueError("ACCEPTED_TERMINAL requires an accepted terminal fact")
        else:
            if self.accepted:
                raise ValueError("preaccept rejection cannot be accepted")
            if self.terminal is not None and not isinstance(
                self.terminal, PreacceptTerminalFact
            ):
                raise ValueError("rejection terminal must be a PreacceptTerminalFact")
            if self.status in {
                StartStatus.INVALID_REQUEST,
                StartStatus.DUPLICATE_RUN_ID,
                StartStatus.INGRESS_CLOSED,
            } and self.terminal is not None:
                raise ValueError("immediate rejection must not include terminal fact")
            if isinstance(self.terminal, PreacceptTerminalFact):
                expected_fact = next(
                    (
                        (conclusion, category)
                        for status, conclusion, category in _PREACCEPT_START_FACTS
                        if status is self.status
                    ),
                    None,
                )
                if expected_fact != (
                    self.terminal.conclusion,
                    self.terminal.error_category,
                ):
                    raise ValueError(
                        "preaccept terminal must match start status"
                    )


@dataclass(frozen=True, slots=True)
class ResultProjection:
    phase: ResultProjectionPhase
    conclusion: TerminalConclusion | None
    error_category: TerminalErrorCategory | None
    reason: str
    history_labels: FrozenPayload
    result_payload: FrozenPayload | None
    fault_type: str | None = None
    manual_judgment_revision: int | None = None
    terminal_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ResultProjectionPhase):
            raise ValueError("phase must be a ResultProjectionPhase")
        require_bounded_text(self.reason, "reason")
        if not isinstance(self.history_labels, FrozenPayload):
            raise ValueError("history_labels must be a FrozenPayload")
        _require_optional_nonempty_text(self.fault_type, "fault_type")
        if self.manual_judgment_revision is not None:
            _require_positive_int(
                self.manual_judgment_revision,
                "manual_judgment_revision",
            )
        if self.terminal_revision is not None:
            if (
                type(self.terminal_revision) is not str
                or len(self.terminal_revision) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.terminal_revision
                )
            ):
                raise ValueError(
                    "terminal_revision must be a lowercase sha256 digest"
                )
        if self.fault_type is not None and (
            self.conclusion is not TerminalConclusion.NG
            or self.manual_judgment_revision is None
        ):
            raise ValueError("fault_type requires a manual NG projection")
        if (
            self.manual_judgment_revision is None
            and self.fault_type is not None
        ):
            raise ValueError("fault_type requires manual_judgment_revision")
        if self.phase in {
            ResultProjectionPhase.RECORDING,
            ResultProjectionPhase.WAITING_SAVE,
            ResultProjectionPhase.WAITING_ANALYSIS,
            ResultProjectionPhase.ANALYZING,
        }:
            if (
                self.conclusion is not None
                or self.error_category is not None
                or self.reason
                or self.result_payload is not None
            ):
                raise ValueError("active projection must not include terminal fields")
        else:
            _validate_terminal_combination(
                self.conclusion,
                self.error_category,
                self.reason,
                self.result_payload,
                allow_none=False,
            )
        if self.terminal_revision is not None and (
            self.phase is not ResultProjectionPhase.TERMINAL
            or self.conclusion
            not in {TerminalConclusion.OK, TerminalConclusion.NG}
            or self.result_payload is None
            or self.manual_judgment_revision is not None
        ):
            raise ValueError(
                "terminal_revision requires an automatic successful terminal projection"
            )


@dataclass(frozen=True, slots=True)
class ResultEnrichmentProjection:
    """Exact-revision chart refresh for one already-committed Decision."""

    run_id: str
    attempt_id: str
    terminal_revision: str
    expected_revision: int
    result_payload: FrozenPayload

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        require_uuid4(self.attempt_id, "attempt_id")
        if (
            type(self.terminal_revision) is not str
            or len(self.terminal_revision) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.terminal_revision
            )
        ):
            raise ValueError(
                "terminal_revision must be a lowercase sha256 digest"
            )
        _require_positive_int(self.expected_revision, "expected_revision")
        if not isinstance(self.result_payload, FrozenPayload):
            raise ValueError("result_payload must be a FrozenPayload")


@dataclass(frozen=True, slots=True)
class ResultControlFacts:
    run_id: str
    config_payload: FrozenPayload
    calibration_payload: FrozenPayload
    analysis_input: FrozenPayload
    audio: PersistedAudioReference | None
    manual_judgment_allowed: bool
    manual_reanalysis_allowed: bool
    current_projection: ResultProjection
    causal_revision: int

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        for field_name in ("config_payload", "calibration_payload", "analysis_input"):
            if not isinstance(getattr(self, field_name), FrozenPayload):
                raise ValueError(f"{field_name} must be a FrozenPayload")
        if self.audio is not None:
            if not isinstance(self.audio, PersistedAudioReference):
                raise ValueError("audio must be a PersistedAudioReference")
            if self.audio.run_id != self.run_id:
                raise ValueError("audio run identity must match facts")
        _require_bool(self.manual_judgment_allowed, "manual_judgment_allowed")
        _require_bool(self.manual_reanalysis_allowed, "manual_reanalysis_allowed")
        if not isinstance(self.current_projection, ResultProjection):
            raise ValueError("current_projection must be a ResultProjection")
        _require_positive_int(self.causal_revision, "causal_revision")


@dataclass(frozen=True, slots=True)
class ManualProjectionResult:
    status: ManualProjectionStatus
    run_id: str
    replacement_projection: ResultProjection | None
    committed_revision: int | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ManualProjectionStatus):
            raise ValueError("status must be a ManualProjectionStatus")
        require_uuid4(self.run_id, "run_id")
        require_bounded_text(self.reason, "reason")
        if self.status is ManualProjectionStatus.COMMITTED:
            if (
                not isinstance(self.replacement_projection, ResultProjection)
                or self.replacement_projection.phase
                is not ResultProjectionPhase.TERMINAL
                or self.committed_revision is None
                or self.reason
            ):
                raise ValueError(
                    "COMMITTED requires a revision, terminal projection, and no reason"
                )
            _require_positive_int(self.committed_revision, "committed_revision")
        elif (
            self.replacement_projection is not None
            or self.committed_revision is not None
        ):
            raise ValueError(
                "non-COMMITTED result must not include revision or projection"
            )


@dataclass(frozen=True, slots=True)
class OperationResult:
    run_id: str
    status: OperationStatus
    attempt_id: str | None
    reason: str

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.status, OperationStatus):
            raise ValueError("status must be an OperationStatus")
        _require_optional_uuid4(self.attempt_id, "attempt_id")
        require_bounded_text(self.reason, "reason")
        if self.status is OperationStatus.SUBMITTED:
            if self.attempt_id is None or self.reason:
                raise ValueError("submitted operation requires attempt ID and no reason")
        elif self.status is OperationStatus.COMPLETED and (
            self.attempt_id is not None or self.reason
        ):
            raise ValueError(
                "completed manual operation must not include attempt ID or reason"
            )


@dataclass(frozen=True, slots=True)
class ResidentCancelResult:
    status: ResidentCancelStatus
    run_id: str
    reservation_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResidentCancelStatus):
            raise ValueError("status must be a ResidentCancelStatus")
        require_uuid4(self.run_id, "run_id")
        _require_nonempty_text(self.reservation_id, "reservation_id")
        require_bounded_text(self.reason, "reason")
        if self.status is ResidentCancelStatus.INVALID:
            if not self.reason:
                raise ValueError("INVALID requires a reason")
        elif self.reason:
            raise ValueError("successful Resident cancel status must not include reason")


@dataclass(frozen=True, slots=True)
class SaveCancelResult:
    status: SaveCancelStatus
    run_id: str
    save_job_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, SaveCancelStatus):
            raise ValueError("status must be a SaveCancelStatus")
        require_uuid4(self.run_id, "run_id")
        require_uuid4(self.save_job_id, "save_job_id")
        require_bounded_text(self.reason, "reason")
        if self.reason:
            raise ValueError("Save cancel result must not include reason")


@dataclass(frozen=True, slots=True)
class SchedulerCancelResult:
    status: SchedulerCancelStatus
    run_id: str
    attempt_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, SchedulerCancelStatus):
            raise ValueError("status must be a SchedulerCancelStatus")
        require_uuid4(self.run_id, "run_id")
        require_uuid4(self.attempt_id, "attempt_id")
        require_bounded_text(self.reason, "reason")
        if self.status is SchedulerCancelStatus.RUN_ID_MISMATCH:
            if not self.reason:
                raise ValueError("RUN_ID_MISMATCH requires a reason")
        elif self.reason:
            raise ValueError("Scheduler cancel result must not include reason")


@dataclass(frozen=True, slots=True)
class ExternalAction:
    kind: ExternalActionKind
    run_id: str
    expected_phase: CoordinatorPhase | ResultOperationPhase
    attempt_id: str | None = None
    save_job_id: str | None = None
    reservation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExternalActionKind):
            raise ValueError("kind must be an ExternalActionKind")
        require_uuid4(self.run_id, "run_id")
        if not isinstance(
            self.expected_phase, (CoordinatorPhase, ResultOperationPhase)
        ):
            raise ValueError("expected_phase must be a coordinator or operation phase")
        _require_optional_uuid4(self.attempt_id, "attempt_id")
        _require_optional_uuid4(self.save_job_id, "save_job_id")
        _require_optional_nonempty_text(self.reservation_id, "reservation_id")
        if (
            sum(
                identity is not None
                for identity in (
                    self.attempt_id,
                    self.save_job_id,
                    self.reservation_id,
                )
            )
            > 1
        ):
            raise ValueError("external action permits at most one identity")


@dataclass(frozen=True, slots=True)
class PendingTerminalContender:
    kind: TerminalContenderKind
    run_id: str
    attempt_id: str | None
    save_job_id: str | None
    conclusion: TerminalConclusion
    error_category: TerminalErrorCategory | None
    reason: str
    result_payload: FrozenPayload | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TerminalContenderKind):
            raise ValueError("kind must be a TerminalContenderKind")
        require_uuid4(self.run_id, "run_id")
        _require_optional_uuid4(self.attempt_id, "attempt_id")
        _require_optional_uuid4(self.save_job_id, "save_job_id")
        _validate_terminal_combination(
            self.conclusion,
            self.error_category,
            self.reason,
            self.result_payload,
            allow_none=False,
        )


def _require_expected_revision(value: ExpectedRevision) -> None:
    if value is not ABSENT:
        _require_positive_int(value, "expected_revision")  # type: ignore[arg-type]


def _require_history_kind_projection(
    kind: HistoryFactKind, projection: ResultProjection
) -> None:
    expected_phase = {
        HistoryFactKind.RECORDING: ResultProjectionPhase.RECORDING,
        HistoryFactKind.WAITING_SAVE: ResultProjectionPhase.WAITING_SAVE,
        HistoryFactKind.WAITING_ANALYSIS: ResultProjectionPhase.WAITING_ANALYSIS,
        HistoryFactKind.ANALYZING: ResultProjectionPhase.ANALYZING,
        HistoryFactKind.RUN_TERMINATED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.REANALYSIS_STARTED: ResultProjectionPhase.REANALYZING,
        HistoryFactKind.REANALYSIS_COMPLETED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.REANALYSIS_CANCELLED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.TERMINAL_RESULT_REPLACED: ResultProjectionPhase.TERMINAL,
    }[kind]
    if projection.phase is not expected_phase:
        raise ValueError("history kind and projection phase are incoherent")


@dataclass(frozen=True, slots=True)
class HistoryProjectionFact:
    kind: HistoryFactKind
    run_id: str
    expected_revision: ExpectedRevision
    operation_id: str | None
    projection: ResultProjection

    def __post_init__(self) -> None:
        if not isinstance(self.kind, HistoryFactKind):
            raise ValueError("kind must be a HistoryFactKind")
        require_uuid4(self.run_id, "run_id")
        _require_expected_revision(self.expected_revision)
        _require_optional_uuid4(self.operation_id, "operation_id")
        if not isinstance(self.projection, ResultProjection):
            raise ValueError("projection must be a ResultProjection")
        _require_history_kind_projection(self.kind, self.projection)


@dataclass(frozen=True, slots=True)
class HistoryEvictionNotice:
    run_id: str

    def __post_init__(self) -> None:
        require_uuid4(self.run_id, "run_id")


@dataclass(frozen=True, slots=True)
class ProjectedHistoryEvent:
    kind: HistoryFactKind
    run_id: str
    expected_revision: ExpectedRevision
    operation_id: str | None
    proposed_revision: int
    projection: ResultProjection

    def __post_init__(self) -> None:
        if not isinstance(self.kind, HistoryFactKind):
            raise ValueError("kind must be a HistoryFactKind")
        require_uuid4(self.run_id, "run_id")
        _require_expected_revision(self.expected_revision)
        _require_optional_uuid4(self.operation_id, "operation_id")
        _require_positive_int(self.proposed_revision, "proposed_revision")
        expected_proposed = (
            1
            if self.expected_revision is ABSENT
            else self.expected_revision + 1  # type: ignore[operator]
        )
        if self.proposed_revision != expected_proposed:
            raise ValueError("proposed revision must follow expected revision")
        if not isinstance(self.projection, ResultProjection):
            raise ValueError("projection must be a ResultProjection")
        _require_history_kind_projection(self.kind, self.projection)


def _validate_metadata_match(
    source: CapturedAudioValue | ImportedSourceReference,
    metadata: AudioMetadataConstraints,
) -> None:
    if (
        source.sample_rate_hz != metadata.expected_sample_rate_hz
        or source.channel_count != metadata.expected_channel_count
        or source.frame_count != metadata.expected_frame_count
    ):
        raise ValueError("source metadata must match expected metadata")


@dataclass(frozen=True, slots=True)
class CapturedAudioSaveRequest:
    save_job_id: str
    run_id: str
    source_kind: AudioSaveSourceKind
    source_identity: str
    audio: CapturedAudioValue
    final_path_request: FinalPathRequest
    metadata: AudioMetadataConstraints

    def __post_init__(self) -> None:
        require_uuid4(self.save_job_id, "save_job_id")
        require_uuid4(self.run_id, "run_id")
        if self.source_kind is not AudioSaveSourceKind.CAPTURED_AUDIO:
            raise ValueError("captured save source kind must be CAPTURED_AUDIO")
        _require_nonempty_text(self.source_identity, "source_identity")
        if not isinstance(self.audio, CapturedAudioValue):
            raise ValueError("audio must be a CapturedAudioValue")
        if self.source_identity != self.audio.source_identity:
            raise ValueError("source identity must match captured audio")
        if not isinstance(self.final_path_request, FinalPathRequest):
            raise ValueError("final_path_request must be a FinalPathRequest")
        if not isinstance(self.metadata, AudioMetadataConstraints):
            raise ValueError("metadata must be AudioMetadataConstraints")
        _validate_metadata_match(self.audio, self.metadata)


@dataclass(frozen=True, slots=True)
class ImportedAudioSaveRequest:
    save_job_id: str
    run_id: str
    source_kind: AudioSaveSourceKind
    source_identity: str
    source: ImportedSourceReference
    final_path_request: FinalPathRequest
    metadata: AudioMetadataConstraints

    def __post_init__(self) -> None:
        require_uuid4(self.save_job_id, "save_job_id")
        require_uuid4(self.run_id, "run_id")
        if self.source_kind is not AudioSaveSourceKind.IMPORTED_SOURCE:
            raise ValueError("imported save source kind must be IMPORTED_SOURCE")
        _require_nonempty_text(self.source_identity, "source_identity")
        if not isinstance(self.source, ImportedSourceReference):
            raise ValueError("source must be an ImportedSourceReference")
        if self.source_identity != self.source.source_identity:
            raise ValueError("source identity must match imported source")
        if not isinstance(self.final_path_request, FinalPathRequest):
            raise ValueError("final_path_request must be a FinalPathRequest")
        if not isinstance(self.metadata, AudioMetadataConstraints):
            raise ValueError("metadata must be AudioMetadataConstraints")
        _validate_metadata_match(self.source, self.metadata)


AudioSaveRequest: TypeAlias = CapturedAudioSaveRequest | ImportedAudioSaveRequest


@dataclass(frozen=True, slots=True)
class AudioSaveOutcome:
    save_job_id: str
    run_id: str
    source_kind: AudioSaveSourceKind
    source_identity: str
    status: AudioSaveStatus
    persisted_audio: PersistedAudioReference | None
    error_category: TerminalErrorCategory | None
    reason: str

    def __post_init__(self) -> None:
        require_uuid4(self.save_job_id, "save_job_id")
        require_uuid4(self.run_id, "run_id")
        if not isinstance(self.source_kind, AudioSaveSourceKind):
            raise ValueError("source_kind must be an AudioSaveSourceKind")
        _require_nonempty_text(self.source_identity, "source_identity")
        if not isinstance(self.status, AudioSaveStatus):
            raise ValueError("status must be an AudioSaveStatus")
        require_bounded_text(self.reason, "reason")
        if self.status is AudioSaveStatus.SAVED:
            if (
                not isinstance(self.persisted_audio, PersistedAudioReference)
                or self.error_category is not None
                or self.reason
            ):
                raise ValueError("SAVED requires persisted audio and no error")
            persisted = self.persisted_audio
            if (
                persisted.save_job_id != self.save_job_id
                or persisted.run_id != self.run_id
                or persisted.source_kind is not self.source_kind
                or persisted.source_identity != self.source_identity
            ):
                raise ValueError("persisted audio identity must match outcome")
        else:
            expected_category = {
                AudioSaveStatus.CANCELLED: TerminalErrorCategory.AUDIO_SAVE_CANCELLED,
                AudioSaveStatus.FAILED: TerminalErrorCategory.AUDIO_SAVE_FAILED,
                AudioSaveStatus.PATH_EXISTS: TerminalErrorCategory.AUDIO_PATH_EXISTS,
            }[self.status]
            if (
                self.persisted_audio is not None
                or self.error_category is not expected_category
            ):
                raise ValueError("save failure outcome is incoherent")


@dataclass(frozen=True, slots=True)
class SaveSubmission:
    status: SaveSubmitStatus
    run_id: str
    save_job_id: str
    source_kind: AudioSaveSourceKind
    source_identity: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, SaveSubmitStatus):
            raise ValueError("status must be a SaveSubmitStatus")
        require_uuid4(self.run_id, "run_id")
        require_uuid4(self.save_job_id, "save_job_id")
        if not isinstance(self.source_kind, AudioSaveSourceKind):
            raise ValueError("source_kind must be an AudioSaveSourceKind")
        _require_nonempty_text(self.source_identity, "source_identity")
        require_bounded_text(self.reason, "reason")
        if self.status is SaveSubmitStatus.ACCEPTED and self.reason:
            raise ValueError("ACCEPTED submission must not include reason")


@dataclass(frozen=True, slots=True)
class SchedulerSubmission:
    status: SchedulerSubmitStatus
    run_id: str
    attempt_id: str
    audio_id: str
    audio_content_revision: int
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, SchedulerSubmitStatus):
            raise ValueError("status must be a SchedulerSubmitStatus")
        require_uuid4(self.run_id, "run_id")
        require_uuid4(self.attempt_id, "attempt_id")
        require_uuid4(self.audio_id, "audio_id")
        _require_positive_int(
            self.audio_content_revision, "audio_content_revision"
        )
        require_bounded_text(self.reason, "reason")
        if self.status is SchedulerSubmitStatus.ACCEPTED and self.reason:
            raise ValueError("ACCEPTED submission must not include reason")


@dataclass(frozen=True, slots=True)
class SaveDispatcherShutdownReport:
    status: SaveDispatcherShutdownStatus
    accepted_count: int
    canceled_count: int
    completed_count: int
    remaining_save_job_ids: tuple[str, ...]
    live_worker_ids: tuple[str, ...]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, SaveDispatcherShutdownStatus):
            raise ValueError("status must be a SaveDispatcherShutdownStatus")
        _require_nonnegative_int(self.accepted_count, "accepted_count")
        _require_nonnegative_int(self.canceled_count, "canceled_count")
        _require_nonnegative_int(self.completed_count, "completed_count")
        if self.canceled_count > self.accepted_count:
            raise ValueError("canceled_count cannot exceed accepted_count")
        if self.completed_count > self.accepted_count:
            raise ValueError("completed_count cannot exceed accepted_count")
        object.__setattr__(
            self,
            "remaining_save_job_ids",
            _sorted_uuid_values(
                self.remaining_save_job_ids, "remaining_save_job_ids"
            ),
        )
        object.__setattr__(
            self,
            "live_worker_ids",
            _sorted_text_values(self.live_worker_ids, "live_worker_ids"),
        )
        object.__setattr__(self, "errors", _bounded_errors(self.errors))
        if self.accepted_count != (
            self.completed_count + len(self.remaining_save_job_ids)
        ):
            raise ValueError(
                "accepted_count must equal completed plus remaining jobs"
            )
        has_remaining_work = bool(
            self.remaining_save_job_ids or self.live_worker_ids
        )
        if (
            self.status is SaveDispatcherShutdownStatus.CLOSED
            and has_remaining_work
        ):
            raise ValueError("CLOSED dispatcher cannot have remaining work")
        if (
            self.status is SaveDispatcherShutdownStatus.INCOMPLETE
            and not has_remaining_work
        ):
            raise ValueError("INCOMPLETE dispatcher requires remaining work")


@dataclass(frozen=True, slots=True)
class StopReport:
    status: StopStatus
    pending_ingress_run_ids: tuple[str, ...]
    pending_publication_run_ids: tuple[str, ...]
    canceled_accepted_run_ids: tuple[str, ...]
    canceled_reanalysis_attempt_ids: tuple[str, ...]
    timed_out: bool
    errors: tuple[str, ...]
    pending_analysis_attempt_ids: tuple[str, ...]
    restart_required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.status, StopStatus):
            raise ValueError("status must be a StopStatus")
        for field_name in (
            "pending_ingress_run_ids",
            "pending_publication_run_ids",
            "canceled_accepted_run_ids",
            "canceled_reanalysis_attempt_ids",
            "pending_analysis_attempt_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _sorted_uuid_values(getattr(self, field_name), field_name),
            )
        _require_bool(self.timed_out, "timed_out")
        object.__setattr__(self, "errors", _bounded_errors(self.errors))
        _require_bool(self.restart_required, "restart_required")
        if self.status is StopStatus.SETTLED and (
            self.pending_ingress_run_ids
            or self.pending_publication_run_ids
            or self.pending_analysis_attempt_ids
            or self.timed_out
            or self.restart_required
        ):
            raise ValueError("SETTLED stop cannot have pending work or timeout")
        if self.status is StopStatus.INCOMPLETE:
            has_blocker_evidence = any(
                (
                    self.pending_ingress_run_ids,
                    self.pending_publication_run_ids,
                    self.canceled_accepted_run_ids,
                    self.canceled_reanalysis_attempt_ids,
                    self.pending_analysis_attempt_ids,
                )
            )
            if not self.restart_required:
                raise ValueError(
                    "INCOMPLETE stop requires timeout and restart"
                )
            if has_blocker_evidence and not self.timed_out:
                raise ValueError(
                    "INCOMPLETE stop requires timeout and restart"
                )
            if (
                not has_blocker_evidence
                and (self.timed_out or not self.errors)
            ):
                raise ValueError(
                    "identity-free INCOMPLETE stop requires errors "
                    "without timeout"
                )
        elif self.timed_out:
            raise ValueError("only INCOMPLETE stop may be timed out")


@dataclass(frozen=True, slots=True)
class CoordinatorShutdownReport:
    status: CoordinatorShutdownStatus
    remaining_ingress_run_ids: tuple[str, ...]
    remaining_run_ids: tuple[str, ...]
    remaining_attempt_ids: tuple[str, ...]
    remaining_save_job_ids: tuple[str, ...]
    pending_publication_run_ids: tuple[str, ...]
    save_dispatcher_report: SaveDispatcherShutdownReport
    errors: tuple[str, ...]
    restart_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, CoordinatorShutdownStatus):
            raise ValueError("status must be a CoordinatorShutdownStatus")
        for field_name in (
            "remaining_ingress_run_ids",
            "remaining_run_ids",
            "remaining_attempt_ids",
            "remaining_save_job_ids",
            "pending_publication_run_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _sorted_uuid_values(getattr(self, field_name), field_name),
            )
        if not isinstance(
            self.save_dispatcher_report, SaveDispatcherShutdownReport
        ):
            raise ValueError(
                "save_dispatcher_report must be a SaveDispatcherShutdownReport"
            )
        object.__setattr__(self, "errors", _bounded_errors(self.errors))
        _require_bool(self.restart_required, "restart_required")
        has_remaining_work = any(
            (
                self.remaining_ingress_run_ids,
                self.remaining_run_ids,
                self.remaining_attempt_ids,
                self.remaining_save_job_ids,
                self.pending_publication_run_ids,
            )
        )
        dispatcher_incomplete = (
            self.save_dispatcher_report.status
            is SaveDispatcherShutdownStatus.INCOMPLETE
        )
        if self.status is CoordinatorShutdownStatus.CLOSED:
            if has_remaining_work or dispatcher_incomplete or self.restart_required:
                raise ValueError("CLOSED coordinator cannot have remaining work")
        elif (
            not has_remaining_work
            and not dispatcher_incomplete
            and not self.errors
            and not self.restart_required
        ):
            raise ValueError(
                "INCOMPLETE coordinator requires remaining work or errors"
            )
        elif not self.restart_required:
            raise ValueError("INCOMPLETE coordinator requires restart")


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    lifecycle: CoordinatorLifecycle
    ingress_run_ids: tuple[str, ...]
    accepted_active_run_ids: tuple[str, ...]
    analysis_attempt_ids: tuple[str, ...]
    result_operation_attempt_ids: tuple[str, ...]
    pending_publication_run_ids: tuple[str, ...]
    restart_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, CoordinatorLifecycle):
            raise ValueError("lifecycle must be a CoordinatorLifecycle")
        for field_name in (
            "ingress_run_ids",
            "accepted_active_run_ids",
            "analysis_attempt_ids",
            "result_operation_attempt_ids",
            "pending_publication_run_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _sorted_uuid_values(getattr(self, field_name), field_name),
            )
        _require_bool(self.restart_required, "restart_required")


@dataclass(frozen=True, slots=True)
class DiagnosticFact:
    stage: str
    run_id: str | None
    attempt_id: str | None
    save_job_id: str | None
    category: TerminalErrorCategory
    message: str

    def __post_init__(self) -> None:
        _require_nonempty_text(self.stage, "stage")
        _require_optional_uuid4(self.run_id, "run_id")
        _require_optional_uuid4(self.attempt_id, "attempt_id")
        _require_optional_uuid4(self.save_job_id, "save_job_id")
        if not isinstance(self.category, TerminalErrorCategory):
            raise ValueError("category must be a TerminalErrorCategory")
        require_bounded_text(self.message, "message")


class SaveCancelToken:
    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def is_cancel_requested(self) -> bool:
        return self._event.is_set()

    def _request_cancel(self) -> None:
        self._event.set()
