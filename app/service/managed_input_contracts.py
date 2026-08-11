from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import re
import unicodedata
from typing import Protocol, final


SIGNED63_MAX = 2**63 - 1
MAX_MANAGED_AUDIO_INPUTS = 6
MAX_RESIDENT_INPUT_STREAMS = 6
MAX_CALIBRATABLE_INPUT_CHANNELS = 508
_INDEX_MAX = 2**31 - 1
_MAX_CALIBRATION_JSON_BYTES = 64 * 1024
_PROCESS_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _require_exact(value: object, expected: type, field_name: str) -> None:
    if type(value) is not expected:
        raise ValueError(f"{field_name} must be an exact {expected.__name__}")


def _require_int(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int = SIGNED63_MAX,
) -> None:
    _require_exact(value, int, field_name)
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} is out of range")


def _require_float(
    value: object,
    field_name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    positive: bool = False,
) -> None:
    _require_exact(value, float, field_name)
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if positive and value <= 0.0:
        raise ValueError(f"{field_name} must be positive")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} is out of range")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} is out of range")


def _utf8_size(value: str, field_name: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain Unicode scalar values") from exc


def _require_normalized_name(value: object, field_name: str) -> None:
    _require_exact(value, str, field_name)
    size = _utf8_size(value, field_name)
    if not 1 <= size <= 256 or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field_name} must be normalized and bounded")


def _require_opaque_text(value: object, field_name: str) -> None:
    _require_exact(value, str, field_name)
    size = _utf8_size(value, field_name)
    if not 1 <= size <= 256 or value != value.strip():
        raise ValueError(f"{field_name} must be bounded without edge whitespace")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{field_name} must not contain control characters")


def _require_error_code_text(value: object, field_name: str) -> None:
    _require_exact(value, str, field_name)
    if _ERROR_CODE_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be bounded lowercase ASCII")


def _require_tuple(
    value: object,
    item_type: type,
    field_name: str,
    *,
    maximum: int = MAX_MANAGED_AUDIO_INPUTS,
) -> None:
    _require_exact(value, tuple, field_name)
    if len(value) > maximum or any(type(item) is not item_type for item in value):
        raise ValueError(f"{field_name} has invalid items")


def normalize_managed_input_alias(value: str) -> str:
    _require_exact(value, str, "alias")
    normalized = unicodedata.normalize("NFC", value).strip()
    scalar_count = len(normalized)
    byte_count = _utf8_size(normalized, "alias")
    if not 1 <= scalar_count <= 96 or byte_count > 384:
        raise ValueError("alias is out of range")
    return normalized


def validate_shutdown_timeout(timeout_seconds: float) -> None:
    _require_float(
        timeout_seconds,
        "timeout_seconds",
        maximum=60.0,
        positive=True,
    )


@final
@dataclass(frozen=True, slots=True)
class ManagedHardwareId:
    value: int

    def __post_init__(self) -> None:
        _require_int(self.value, "value", minimum=1)


@final
@dataclass(frozen=True, slots=True)
class BackendScanId:
    process_instance_id: str
    sequence: int

    def __post_init__(self) -> None:
        _require_exact(self.process_instance_id, str, "process_instance_id")
        if _PROCESS_ID_RE.fullmatch(self.process_instance_id) is None:
            raise ValueError("process_instance_id is invalid")
        _require_int(self.sequence, "sequence", minimum=1)


@final
@dataclass(frozen=True, slots=True)
class InputEndpointDescriptor:
    device_name: str
    hostapi_name: str
    sample_rate_hz: float
    input_channels: int

    def __post_init__(self) -> None:
        _require_normalized_name(self.device_name, "device_name")
        _require_normalized_name(self.hostapi_name, "hostapi_name")
        _require_float(self.sample_rate_hz, "sample_rate_hz", positive=True)
        _require_int(
            self.input_channels,
            "input_channels",
            minimum=1,
            maximum=_INDEX_MAX,
        )


@final
@dataclass(frozen=True, slots=True)
class InputEndpointObservation:
    scan_id: BackendScanId
    observation_id: str
    device_index: int
    hostapi_index: int
    stable_backend_uid: str | None
    descriptor: InputEndpointDescriptor

    def __post_init__(self) -> None:
        _require_exact(self.scan_id, BackendScanId, "scan_id")
        _require_opaque_text(self.observation_id, "observation_id")
        _require_int(self.device_index, "device_index", maximum=_INDEX_MAX)
        _require_int(self.hostapi_index, "hostapi_index", maximum=_INDEX_MAX)
        if self.stable_backend_uid is not None:
            _require_opaque_text(self.stable_backend_uid, "stable_backend_uid")
        _require_exact(self.descriptor, InputEndpointDescriptor, "descriptor")


@final
@dataclass(frozen=True, slots=True)
class HardwareEndpointIdentity:
    hardware_id: ManagedHardwareId
    scan_id: BackendScanId
    device_index: int
    hostapi_index: int
    stable_backend_uid: str | None
    device_name: str
    hostapi_name: str
    sample_rate_hz: float
    input_channels: int

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_exact(self.scan_id, BackendScanId, "scan_id")
        _require_int(self.device_index, "device_index", maximum=_INDEX_MAX)
        _require_int(self.hostapi_index, "hostapi_index", maximum=_INDEX_MAX)
        if self.stable_backend_uid is not None:
            _require_opaque_text(self.stable_backend_uid, "stable_backend_uid")
        _require_normalized_name(self.device_name, "device_name")
        _require_normalized_name(self.hostapi_name, "hostapi_name")
        _require_float(self.sample_rate_hz, "sample_rate_hz", positive=True)
        _require_int(
            self.input_channels,
            "input_channels",
            minimum=1,
            maximum=_INDEX_MAX,
        )


@final
@dataclass(frozen=True, slots=True)
class ResidentStreamGenerationStamp:
    hardware_id: ManagedHardwareId
    pool_epoch: int
    stream_generation: int

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(self.pool_epoch, "pool_epoch", minimum=1)
        _require_int(self.stream_generation, "stream_generation", minimum=1)


@final
@dataclass(frozen=True, slots=True)
class ResidentReadyStream:
    endpoint: HardwareEndpointIdentity
    generation: ResidentStreamGenerationStamp

    def __post_init__(self) -> None:
        _require_exact(self.endpoint, HardwareEndpointIdentity, "endpoint")
        _require_exact(
            self.generation, ResidentStreamGenerationStamp, "generation"
        )
        if self.endpoint.hardware_id != self.generation.hardware_id:
            raise ValueError("endpoint and generation hardware differ")


@final
@dataclass(frozen=True, slots=True)
class ResidentInputFailureNotice:
    generation: ResidentStreamGenerationStamp
    error_code: str

    def __post_init__(self) -> None:
        _require_exact(
            self.generation, ResidentStreamGenerationStamp, "generation"
        )
        _require_error_code_text(self.error_code, "error_code")


@final
@dataclass(frozen=True, slots=True)
class ResidentOpenFailure:
    hardware_id: ManagedHardwareId
    error_code: str

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_error_code_text(self.error_code, "error_code")


@final
@dataclass(frozen=True, slots=True)
class ResidentStartupOpenResult:
    ready_streams: tuple[ResidentReadyStream, ...]
    failures: tuple[ResidentOpenFailure, ...]

    def __post_init__(self) -> None:
        _require_tuple(
            self.ready_streams, ResidentReadyStream, "ready_streams"
        )
        _require_tuple(self.failures, ResidentOpenFailure, "failures")
        hardware_ids = [
            stream.endpoint.hardware_id for stream in self.ready_streams
        ] + [failure.hardware_id for failure in self.failures]
        if len(hardware_ids) > MAX_RESIDENT_INPUT_STREAMS:
            raise ValueError("startup result exceeds stream limit")
        if len(set(hardware_ids)) != len(hardware_ids):
            raise ValueError("startup result contains duplicate hardware")


@final
@dataclass(frozen=True, slots=True)
class ResidentCalibrationMeasurement:
    generation: ResidentStreamGenerationStamp
    channel_index: int
    measured_level_db: float
    captured_frames: int

    def __post_init__(self) -> None:
        _require_exact(
            self.generation, ResidentStreamGenerationStamp, "generation"
        )
        _require_int(self.channel_index, "channel_index")
        _require_float(self.measured_level_db, "measured_level_db")
        _require_int(self.captured_frames, "captured_frames", minimum=1)


@final
@dataclass(frozen=True, slots=True)
class OfflineMaintenanceBlockerSnapshot:
    active_capture_count: int | None
    target_resource_owned: bool | None

    def __post_init__(self) -> None:
        if (self.active_capture_count is None) != (
            self.target_resource_owned is None
        ):
            raise ValueError("blocker facts must both be known or unknown")
        if self.active_capture_count is not None:
            _require_int(
                self.active_capture_count, "active_capture_count", minimum=0
            )
            _require_exact(
                self.target_resource_owned, bool, "target_resource_owned"
            )


class ManagedInputAvailability(Enum):
    AVAILABLE = "available"
    MAPPED_PENDING_OPEN = "mapped_pending_open"
    UNAVAILABLE = "unavailable"
    NEEDS_USER_SELECTION = "needs_user_selection"


class ManagedInputProcessState(Enum):
    BOOTSTRAPPING = "bootstrapping"
    ACCEPTING_RUNS = "accepting_runs"
    RESTART_REQUIRED = "restart_required"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"
    SHUTDOWN_INCOMPLETE = "shutdown_incomplete"


class ManagedInputCommandKind(Enum):
    ADD = "add"
    SELECT_OR_REPLACE = "select_or_replace"
    REMOVE = "remove"
    REFRESH = "refresh"
    CALIBRATE = "calibrate"


class ManagedInputResultStatus(Enum):
    SUCCEEDED = "succeeded"
    SUCCEEDED_RESTART_REQUIRED = "succeeded_restart_required"
    BUSY = "busy"
    CAPACITY_REJECTED = "capacity_rejected"
    REJECTED = "rejected"
    FAILED = "failed"


class ManagedInputBlocker(Enum):
    MAINTENANCE_IN_PROGRESS = "maintenance_in_progress"
    OWNERSHIP_STATE_UNKNOWN = "ownership_state_unknown"
    ACTIVE_CAPTURE_EXISTS = "active_capture_exists"
    TARGET_RESOURCE_BUSY = "target_resource_busy"


class ManagedInputErrorCode(Enum):
    CAPACITY_REACHED = "capacity_reached"
    ALIAS_ALREADY_EXISTS = "alias_already_exists"
    BACKEND_IDENTITY_CONFLICT = "backend_identity_conflict"
    HARDWARE_NOT_FOUND = "hardware_not_found"
    STALE_HARDWARE_REVISION = "stale_hardware_revision"
    STALE_CALIBRATION_REVISION = "stale_calibration_revision"
    STALE_SCAN = "stale_scan"
    STALE_ENDPOINT_OBSERVATION = "stale_endpoint_observation"
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    ENDPOINT_NEEDS_USER_SELECTION = "endpoint_needs_user_selection"
    ENDPOINT_ALREADY_BOUND = "endpoint_already_bound"
    ENDPOINT_VALIDATION_FAILED = "endpoint_validation_failed"
    CONFIG_REFERENCE_EXISTS = "config_reference_exists"
    RESIDENT_LEASE_EXISTS = "resident_lease_exists"
    RESIDENT_OPEN_FAILED = "resident_open_failed"
    RESIDENT_STREAM_FAILED = "resident_stream_failed"
    CALIBRATION_REQUIRED = "calibration_required"
    CALIBRATION_INVALID = "calibration_invalid"
    CALIBRATION_CAPTURE_FAILED = "calibration_capture_failed"
    DATABASE_TRANSACTION_FAILED = "database_transaction_failed"
    REFRESH_REQUIRES_RESTART = "refresh_requires_restart"
    RESTART_REQUIRED = "restart_required"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN_TIMEOUT = "shutdown_timeout"
    INVARIANT_VIOLATION = "invariant_violation"


_SNAPSHOT_ERROR_CODES_BY_AVAILABILITY = {
    ManagedInputAvailability.AVAILABLE: frozenset({None}),
    ManagedInputAvailability.MAPPED_PENDING_OPEN: frozenset({None}),
    ManagedInputAvailability.NEEDS_USER_SELECTION: frozenset(
        {ManagedInputErrorCode.ENDPOINT_NEEDS_USER_SELECTION}
    ),
    ManagedInputAvailability.UNAVAILABLE: frozenset(
        {
            None,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
            ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED,
            ManagedInputErrorCode.RESIDENT_OPEN_FAILED,
            ManagedInputErrorCode.RESIDENT_STREAM_FAILED,
        }
    ),
}
_SCAN_DIAGNOSTIC_CODES = frozenset(
    {
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    }
)
_PERSISTENT_COMMAND_KINDS = frozenset(
    {
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
        ManagedInputCommandKind.REMOVE,
        ManagedInputCommandKind.CALIBRATE,
    }
)
_RESULT_COMMANDS_BY_STATUS = {
    ManagedInputResultStatus.SUCCEEDED: frozenset(
        {ManagedInputCommandKind.REFRESH}
    ),
    ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED: _PERSISTENT_COMMAND_KINDS,
    ManagedInputResultStatus.BUSY: frozenset(ManagedInputCommandKind),
    ManagedInputResultStatus.CAPACITY_REJECTED: frozenset(
        {ManagedInputCommandKind.ADD}
    ),
    ManagedInputResultStatus.REJECTED: frozenset(ManagedInputCommandKind),
    ManagedInputResultStatus.FAILED: _PERSISTENT_COMMAND_KINDS,
}
_REJECTED_ERROR_CODES_BY_COMMAND = {
    ManagedInputCommandKind.ADD: frozenset(
        {
            ManagedInputErrorCode.ALIAS_ALREADY_EXISTS,
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
            ManagedInputErrorCode.STALE_SCAN,
            ManagedInputErrorCode.STALE_ENDPOINT_OBSERVATION,
            ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND,
            ManagedInputErrorCode.RESTART_REQUIRED,
            ManagedInputErrorCode.SHUTTING_DOWN,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        }
    ),
    ManagedInputCommandKind.SELECT_OR_REPLACE: frozenset(
        {
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
            ManagedInputErrorCode.STALE_SCAN,
            ManagedInputErrorCode.STALE_ENDPOINT_OBSERVATION,
            ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND,
            ManagedInputErrorCode.RESTART_REQUIRED,
            ManagedInputErrorCode.SHUTTING_DOWN,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        }
    ),
    ManagedInputCommandKind.REMOVE: frozenset(
        {
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
            ManagedInputErrorCode.CONFIG_REFERENCE_EXISTS,
            ManagedInputErrorCode.RESIDENT_LEASE_EXISTS,
            ManagedInputErrorCode.RESTART_REQUIRED,
            ManagedInputErrorCode.SHUTTING_DOWN,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        }
    ),
    ManagedInputCommandKind.REFRESH: frozenset(
        {
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
            ManagedInputErrorCode.REFRESH_REQUIRES_RESTART,
            ManagedInputErrorCode.SHUTTING_DOWN,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        }
    ),
    ManagedInputCommandKind.CALIBRATE: frozenset(
        {
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
            ManagedInputErrorCode.STALE_CALIBRATION_REVISION,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
            ManagedInputErrorCode.CALIBRATION_INVALID,
            ManagedInputErrorCode.RESTART_REQUIRED,
            ManagedInputErrorCode.SHUTTING_DOWN,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        }
    ),
}
_FAILED_ERROR_CODES_BY_COMMAND = {
    ManagedInputCommandKind.ADD: frozenset(
        {ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED}
    ),
    ManagedInputCommandKind.SELECT_OR_REPLACE: frozenset(
        {ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED}
    ),
    ManagedInputCommandKind.REMOVE: frozenset(
        {ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED}
    ),
    ManagedInputCommandKind.CALIBRATE: frozenset(
        {
            ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
            ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
        }
    ),
}


@final
@dataclass(frozen=True, slots=True)
class ManagedInputError:
    code: ManagedInputErrorCode
    stage: str
    summary: str

    def __post_init__(self) -> None:
        _require_exact(self.code, ManagedInputErrorCode, "code")
        _require_error_code_text(self.stage, "stage")
        _require_exact(self.summary, str, "summary")
        if (
            not 1 <= len(self.summary) <= 512
            or _utf8_size(self.summary, "summary") > 2048
        ):
            raise ValueError("summary is out of range")


@final
@dataclass(frozen=True, slots=True)
class ManagedInputSnapshot:
    hardware_id: ManagedHardwareId
    alias: str
    hardware_revision: int
    descriptor: InputEndpointDescriptor
    calibration_revision: int
    availability: ManagedInputAvailability
    endpoint: HardwareEndpointIdentity | None
    resident_generation: ResidentStreamGenerationStamp | None
    error_code: ManagedInputErrorCode | None

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        if normalize_managed_input_alias(self.alias) != self.alias:
            raise ValueError("snapshot alias must be canonical")
        _require_int(self.hardware_revision, "hardware_revision", minimum=1)
        _require_exact(self.descriptor, InputEndpointDescriptor, "descriptor")
        _require_int(self.calibration_revision, "calibration_revision")
        _require_exact(self.availability, ManagedInputAvailability, "availability")
        if self.error_code is not None:
            _require_exact(self.error_code, ManagedInputErrorCode, "error_code")
        if (
            self.error_code
            not in _SNAPSHOT_ERROR_CODES_BY_AVAILABILITY[self.availability]
        ):
            raise ValueError("availability and error_code conflict")
        if self.availability is ManagedInputAvailability.AVAILABLE:
            _require_exact(
                self.endpoint, HardwareEndpointIdentity, "endpoint"
            )
            _require_exact(
                self.resident_generation,
                ResidentStreamGenerationStamp,
                "resident_generation",
            )
            if (
                self.endpoint.hardware_id != self.hardware_id
                or self.resident_generation.hardware_id != self.hardware_id
            ):
                raise ValueError("ready snapshot hardware differs")
            endpoint_descriptor = InputEndpointDescriptor(
                self.endpoint.device_name,
                self.endpoint.hostapi_name,
                self.endpoint.sample_rate_hz,
                self.endpoint.input_channels,
            )
            if self.descriptor != endpoint_descriptor:
                raise ValueError("ready snapshot descriptor differs")
        elif self.endpoint is not None or self.resident_generation is not None:
            raise ValueError("non-available snapshot cannot expose ready values")


@final
@dataclass(frozen=True, slots=True)
class ManagedInputInventorySnapshot:
    process_state: ManagedInputProcessState
    restart_required: bool
    active_capture_count: int | None
    scan_id: BackendScanId | None
    scan_error_code: ManagedInputErrorCode | None
    rows: tuple[ManagedInputSnapshot, ...]

    def __post_init__(self) -> None:
        _require_exact(
            self.process_state, ManagedInputProcessState, "process_state"
        )
        _require_exact(self.restart_required, bool, "restart_required")
        if self.restart_required != (
            self.process_state is ManagedInputProcessState.RESTART_REQUIRED
        ):
            raise ValueError("restart_required conflicts with process state")
        if self.active_capture_count is not None:
            _require_int(
                self.active_capture_count, "active_capture_count", minimum=0
            )
        if self.scan_id is not None:
            _require_exact(self.scan_id, BackendScanId, "scan_id")
        if self.scan_error_code is not None:
            _require_exact(
                self.scan_error_code, ManagedInputErrorCode, "scan_error_code"
            )
            if self.scan_error_code not in _SCAN_DIAGNOSTIC_CODES:
                raise ValueError("scan_error_code is not a scan diagnostic")
        _require_tuple(self.rows, ManagedInputSnapshot, "rows")
        if any(
            row.availability is ManagedInputAvailability.MAPPED_PENDING_OPEN
            for row in self.rows
        ) and (
            self.process_state is not ManagedInputProcessState.BOOTSTRAPPING
            or self.scan_id is None
            or self.scan_error_code is not None
        ):
            raise ValueError(
                "mapped-pending rows require a valid bootstrapping scan"
            )
        if (
            self.process_state is ManagedInputProcessState.BOOTSTRAPPING
            and any(
                row.availability is ManagedInputAvailability.AVAILABLE
                for row in self.rows
            )
        ):
            raise ValueError("bootstrapping inventory cannot be available")
        row_keys = [
            (row.alias, row.hardware_id.value)
            for row in self.rows
        ]
        if row_keys != sorted(row_keys) or len(
            {row.hardware_id for row in self.rows}
        ) != len(self.rows):
            raise ValueError("rows must be sorted and unique")


@final
@dataclass(frozen=True, slots=True)
class FrozenRunHardwareSnapshot:
    hardware_id: ManagedHardwareId
    hardware_revision: int
    alias_at_trigger: str
    endpoint: HardwareEndpointIdentity
    calibration_revision: int

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(self.hardware_revision, "hardware_revision", minimum=1)
        if normalize_managed_input_alias(self.alias_at_trigger) != self.alias_at_trigger:
            raise ValueError("alias_at_trigger must be canonical")
        _require_exact(self.endpoint, HardwareEndpointIdentity, "endpoint")
        _require_int(self.calibration_revision, "calibration_revision")
        if self.endpoint.hardware_id != self.hardware_id:
            raise ValueError("endpoint hardware differs")


@final
@dataclass(frozen=True, slots=True)
class FrozenRunCalibrationSnapshot:
    hardware_id: ManagedHardwareId
    hardware_revision: int
    calibration_revision: int
    sample_rate_hz: float
    input_channels: int
    channel_values: tuple[tuple[int, int, float], ...]

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(self.hardware_revision, "hardware_revision", minimum=1)
        _require_int(self.calibration_revision, "calibration_revision")
        _require_float(self.sample_rate_hz, "sample_rate_hz", positive=True)
        _require_int(
            self.input_channels,
            "input_channels",
            minimum=1,
            maximum=_INDEX_MAX,
        )
        _require_exact(self.channel_values, tuple, "channel_values")
        if not self.channel_values:
            raise ValueError("channel_values must be nonempty")
        previous = -1
        for channel in self.channel_values:
            if type(channel) is not tuple or len(channel) != 3:
                raise ValueError("channel value is invalid")
            channel_index, channel_revision, correction_db = channel
            _require_int(channel_index, "channel_index")
            _require_int(
                channel_revision, "channel_calibration_revision"
            )
            _require_float(
                correction_db,
                "correction_db",
                minimum=-400.0,
                maximum=400.0,
            )
            if channel_index <= previous or channel_index >= self.input_channels:
                raise ValueError("channel values must be sorted and in range")
            previous = channel_index


@final
@dataclass(frozen=True, slots=True)
class FrozenRunInputProjection:
    hardware: FrozenRunHardwareSnapshot
    calibration: FrozenRunCalibrationSnapshot

    def __post_init__(self) -> None:
        _require_exact(self.hardware, FrozenRunHardwareSnapshot, "hardware")
        _require_exact(
            self.calibration, FrozenRunCalibrationSnapshot, "calibration"
        )
        if (
            self.hardware.hardware_id != self.calibration.hardware_id
            or self.hardware.hardware_revision
            != self.calibration.hardware_revision
            or self.hardware.calibration_revision
            != self.calibration.calibration_revision
            or self.hardware.endpoint.sample_rate_hz
            != self.calibration.sample_rate_hz
            or self.hardware.endpoint.input_channels
            != self.calibration.input_channels
        ):
            raise ValueError("frozen projection snapshots differ")


@final
@dataclass(frozen=True, slots=True)
class FrozenRunProjectionResult:
    projection: FrozenRunInputProjection | None
    error: ManagedInputError | None

    def __post_init__(self) -> None:
        if (self.projection is None) == (self.error is None):
            raise ValueError("exactly one projection result arm is required")
        if self.projection is not None:
            _require_exact(
                self.projection, FrozenRunInputProjection, "projection"
            )
        if self.error is not None:
            _require_exact(self.error, ManagedInputError, "error")


def _validate_command_observation(
    scan_id: BackendScanId, observation_id: str
) -> None:
    _require_exact(scan_id, BackendScanId, "scan_id")
    _require_opaque_text(observation_id, "observation_id")


@final
@dataclass(frozen=True, slots=True)
class AddManagedInputCommand:
    alias: str
    scan_id: BackendScanId
    observation_id: str

    def __post_init__(self) -> None:
        normalize_managed_input_alias(self.alias)
        _validate_command_observation(self.scan_id, self.observation_id)


@final
@dataclass(frozen=True, slots=True)
class SelectOrReplaceManagedInputCommand:
    hardware_id: ManagedHardwareId
    expected_hardware_revision: int
    scan_id: BackendScanId
    observation_id: str

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(
            self.expected_hardware_revision,
            "expected_hardware_revision",
            minimum=1,
        )
        _validate_command_observation(self.scan_id, self.observation_id)


@final
@dataclass(frozen=True, slots=True)
class RemoveManagedInputCommand:
    hardware_id: ManagedHardwareId
    expected_hardware_revision: int

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(
            self.expected_hardware_revision,
            "expected_hardware_revision",
            minimum=1,
        )


@final
@dataclass(frozen=True, slots=True)
class RefreshManagedInputsCommand:
    pass


@final
@dataclass(frozen=True, slots=True)
class CalibrateManagedInputCommand:
    hardware_id: ManagedHardwareId
    expected_hardware_revision: int
    expected_calibration_revision: int
    channel_index: int
    standard_level_db: float
    duration_frames: int

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(
            self.expected_hardware_revision,
            "expected_hardware_revision",
            minimum=1,
        )
        _require_int(
            self.expected_calibration_revision,
            "expected_calibration_revision",
        )
        _require_int(self.channel_index, "channel_index")
        _require_float(
            self.standard_level_db,
            "standard_level_db",
            minimum=-200.0,
            maximum=200.0,
        )
        _require_int(self.duration_frames, "duration_frames", minimum=1)


@final
@dataclass(frozen=True, slots=True)
class MapStartupEndpointCommand:
    hardware_id: ManagedHardwareId
    expected_hardware_revision: int
    scan_id: BackendScanId
    observation_id: str

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_int(
            self.expected_hardware_revision,
            "expected_hardware_revision",
            minimum=1,
        )
        _validate_command_observation(self.scan_id, self.observation_id)


class StableUidCapability(Enum):
    NONE = "none"
    SUPPORTED = "supported"


class StableUidContract(Enum):
    ABSENT = "absent"
    REVIEWED_OPAQUE_UID = "reviewed_opaque_uid"


@final
@dataclass(frozen=True, slots=True)
class RawHostApiIdentity:
    hostapi_index: int
    hostapi_name: str

    def __post_init__(self) -> None:
        _require_int(self.hostapi_index, "hostapi_index", maximum=_INDEX_MAX)
        _require_normalized_name(self.hostapi_name, "hostapi_name")


@final
@dataclass(frozen=True, slots=True)
class RawBackendInputScan:
    scan_id: BackendScanId
    complete: bool
    stable_uid_capability: StableUidCapability
    stable_uid_contract: StableUidContract
    hostapis: tuple[RawHostApiIdentity, ...]
    observations: tuple[InputEndpointObservation, ...]

    def __post_init__(self) -> None:
        _require_exact(self.scan_id, BackendScanId, "scan_id")
        _require_exact(self.complete, bool, "complete")
        _require_exact(
            self.stable_uid_capability,
            StableUidCapability,
            "stable_uid_capability",
        )
        _require_exact(
            self.stable_uid_contract,
            StableUidContract,
            "stable_uid_contract",
        )
        _require_tuple(
            self.hostapis, RawHostApiIdentity, "hostapis", maximum=_INDEX_MAX
        )
        _require_tuple(
            self.observations,
            InputEndpointObservation,
            "observations",
            maximum=_INDEX_MAX,
        )
        if any(item.scan_id != self.scan_id for item in self.observations):
            raise ValueError("raw observations must use the raw scan id")


@final
@dataclass(frozen=True, slots=True)
class BackendInputScan:
    scan_id: BackendScanId
    stable_uid_capability: StableUidCapability
    observations: tuple[InputEndpointObservation, ...]

    def __post_init__(self) -> None:
        _require_exact(self.scan_id, BackendScanId, "scan_id")
        _require_exact(
            self.stable_uid_capability,
            StableUidCapability,
            "stable_uid_capability",
        )
        _require_tuple(
            self.observations,
            InputEndpointObservation,
            "observations",
            maximum=_INDEX_MAX,
        )
        if any(item.scan_id != self.scan_id for item in self.observations):
            raise ValueError("observations must use the validated scan id")


@final
@dataclass(frozen=True, slots=True)
class BackendDiscoveryResult:
    scan: BackendInputScan | None
    error: ManagedInputError | None

    def __post_init__(self) -> None:
        if (self.scan is None) == (self.error is None):
            raise ValueError("exactly one discovery result arm is required")
        if self.scan is not None:
            _require_exact(self.scan, BackendInputScan, "scan")
        if self.error is not None:
            _require_exact(self.error, ManagedInputError, "error")
            if self.error.code not in _SCAN_DIAGNOSTIC_CODES:
                raise ValueError("discovery error is not a scan failure")


@final
@dataclass(frozen=True, slots=True)
class ManagedInputBootstrapResult:
    process_state: ManagedInputProcessState
    inventory: ManagedInputInventorySnapshot
    ready_streams: tuple[ResidentReadyStream, ...]
    unresolved_hardware_ids: tuple[ManagedHardwareId, ...]
    blocker: ManagedInputBlocker | None
    errors: tuple[ManagedInputError, ...]

    def __post_init__(self) -> None:
        _require_exact(
            self.process_state, ManagedInputProcessState, "process_state"
        )
        _require_exact(
            self.inventory, ManagedInputInventorySnapshot, "inventory"
        )
        if self.inventory.process_state is not self.process_state:
            raise ValueError("bootstrap state conflicts with inventory")
        _require_tuple(
            self.ready_streams, ResidentReadyStream, "ready_streams"
        )
        _require_tuple(
            self.unresolved_hardware_ids,
            ManagedHardwareId,
            "unresolved_hardware_ids",
        )
        _require_tuple(self.errors, ManagedInputError, "errors")
        if self.blocker is not None:
            _require_exact(self.blocker, ManagedInputBlocker, "blocker")
            if (
                self.blocker
                is not ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
                or self.errors
            ):
                raise ValueError("bootstrap blocker is invalid")
        ready_ids = [item.endpoint.hardware_id for item in self.ready_streams]
        endpoint_keys = [
            (item.endpoint.scan_id, item.endpoint.device_index)
            for item in self.ready_streams
        ]
        unresolved_ids = list(self.unresolved_hardware_ids)
        inventory_rows = {
            row.hardware_id: row for row in self.inventory.rows
        }
        expected_ready_ids = {
            row.hardware_id
            for row in self.inventory.rows
            if row.availability is ManagedInputAvailability.AVAILABLE
        }
        expected_unresolved_ids = {
            row.hardware_id
            for row in self.inventory.rows
            if row.availability
            in (
                ManagedInputAvailability.UNAVAILABLE,
                ManagedInputAvailability.NEEDS_USER_SELECTION,
            )
        }
        if (
            ready_ids != sorted(ready_ids, key=lambda item: item.value)
            or unresolved_ids
            != sorted(unresolved_ids, key=lambda item: item.value)
            or len(set(ready_ids)) != len(ready_ids)
            or len(set(endpoint_keys)) != len(endpoint_keys)
            or len(set(unresolved_ids)) != len(unresolved_ids)
            or set(ready_ids).intersection(unresolved_ids)
            or set(ready_ids) != expected_ready_ids
            or set(unresolved_ids) != expected_unresolved_ids
        ):
            raise ValueError("bootstrap hardware sets are invalid")
        for ready_stream in self.ready_streams:
            row = inventory_rows[ready_stream.endpoint.hardware_id]
            if (
                row.availability is not ManagedInputAvailability.AVAILABLE
                or row.endpoint != ready_stream.endpoint
                or row.resident_generation != ready_stream.generation
            ):
                raise ValueError(
                    "ready stream conflicts with the inventory row"
                )
        if self.inventory.scan_error_code is not None:
            if (
                self.process_state is not ManagedInputProcessState.BOOTSTRAPPING
                or self.inventory.scan_id is not None
                or self.ready_streams
                or any(
                    row.availability is not ManagedInputAvailability.UNAVAILABLE
                    for row in self.inventory.rows
                )
                or set(unresolved_ids) != set(inventory_rows)
                or len(self.errors) != 1
                or self.errors[0].code is not self.inventory.scan_error_code
            ):
                raise ValueError("invalid startup projection is incoherent")
        elif len(self.errors) > len(self.inventory.rows):
            if not (
                not self.inventory.rows
                and self.process_state
                is ManagedInputProcessState.BOOTSTRAPPING
                and self.inventory.scan_id is None
                and not self.ready_streams
                and not self.unresolved_hardware_ids
                and len(self.errors) == 1
                and self.errors[0].code
                is ManagedInputErrorCode.INVARIANT_VIOLATION
                and self.errors[0].stage == "resident_open"
            ):
                raise ValueError("bootstrap errors exceed inventory rows")


@final
@dataclass(frozen=True, slots=True)
class ManagedInputResult:
    status: ManagedInputResultStatus
    command_kind: ManagedInputCommandKind
    process_state: ManagedInputProcessState
    blocker: ManagedInputBlocker | None
    inventory: ManagedInputInventorySnapshot | None
    hardware_snapshot: ManagedInputSnapshot | None
    observations: tuple[InputEndpointObservation, ...]
    errors: tuple[ManagedInputError, ...]

    def __post_init__(self) -> None:
        _require_exact(self.status, ManagedInputResultStatus, "status")
        _require_exact(self.command_kind, ManagedInputCommandKind, "command_kind")
        _require_exact(
            self.process_state, ManagedInputProcessState, "process_state"
        )
        if self.blocker is not None:
            _require_exact(self.blocker, ManagedInputBlocker, "blocker")
        if self.inventory is not None:
            _require_exact(
                self.inventory, ManagedInputInventorySnapshot, "inventory"
            )
            if self.inventory.process_state is not self.process_state:
                raise ValueError("result state conflicts with inventory")
        if self.hardware_snapshot is not None:
            _require_exact(
                self.hardware_snapshot,
                ManagedInputSnapshot,
                "hardware_snapshot",
            )
        _require_tuple(
            self.observations, InputEndpointObservation, "observations"
        )
        _require_tuple(self.errors, ManagedInputError, "errors")
        if self.command_kind not in _RESULT_COMMANDS_BY_STATUS[self.status]:
            raise ValueError("status and command_kind conflict")
        if self.status in (
            ManagedInputResultStatus.SUCCEEDED,
            ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
            ManagedInputResultStatus.BUSY,
        ):
            if self.errors:
                raise ValueError("success and busy results cannot carry errors")
        elif self.status is ManagedInputResultStatus.CAPACITY_REJECTED:
            if (
                self.hardware_snapshot is not None
                or len(self.errors) != 1
                or self.errors[0].code
                is not ManagedInputErrorCode.CAPACITY_REACHED
            ):
                raise ValueError("capacity rejection is invalid")
        elif self.status is ManagedInputResultStatus.REJECTED:
            if (
                len(self.errors) != 1
                or self.errors[0].code
                not in _REJECTED_ERROR_CODES_BY_COMMAND[self.command_kind]
            ):
                raise ValueError("rejected result error is invalid")
        elif (
            len(self.errors) != 1
            or self.errors[0].code
            not in _FAILED_ERROR_CODES_BY_COMMAND[self.command_kind]
        ):
            raise ValueError("failed result error is invalid")
        if self.status is ManagedInputResultStatus.BUSY:
            if self.blocker is None:
                raise ValueError("busy result requires one blocker")
            if self.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS:
                if self.inventory is not None:
                    raise ValueError("mutex busy result cannot carry inventory")
            elif self.blocker is ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN:
                if (
                    self.inventory is not None
                    and self.inventory.active_capture_count is not None
                ):
                    raise ValueError(
                        "unknown ownership cannot carry a known capture count"
                    )
            elif self.blocker is ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS:
                if (
                    self.inventory is None
                    or self.inventory.active_capture_count is None
                    or self.inventory.active_capture_count <= 0
                ):
                    raise ValueError(
                        "active-capture blocker requires a positive count"
                    )
            elif (
                self.inventory is None
                or self.inventory.active_capture_count != 0
            ):
                raise ValueError(
                    "target-resource blocker requires a zero capture count"
                )
        else:
            if self.blocker is not None or self.inventory is None:
                raise ValueError("non-busy result requires inventory and no blocker")
        if (
            self.status
            is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
        ):
            if self.process_state is not ManagedInputProcessState.RESTART_REQUIRED:
                raise ValueError(
                    "restart success requires restart-required state"
                )
        scan_error_code = (
            None
            if self.inventory is None
            else self.inventory.scan_error_code
        )
        if (
            scan_error_code is not None
            and self.command_kind is ManagedInputCommandKind.REFRESH
        ):
            if (
                self.status is not ManagedInputResultStatus.REJECTED
                or self.observations
                or len(self.errors) != 1
                or self.errors[0].code is not scan_error_code
            ):
                raise ValueError("latched refresh result is incoherent")
        elif (
            scan_error_code is not None
            and self.command_kind in _PERSISTENT_COMMAND_KINDS
        ):
            if (
                self.status is not ManagedInputResultStatus.REJECTED
                or self.blocker is not None
                or self.observations
                or len(self.errors) != 1
                or self.errors[0].code is not scan_error_code
            ):
                raise ValueError("latched persistent result is incoherent")


class ManagedInputCloseStatus(Enum):
    CLOSED = "closed"
    NOT_OPEN = "not_open"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@final
@dataclass(frozen=True, slots=True)
class ManagedInputCloseResult:
    hardware_id: ManagedHardwareId
    status: ManagedInputCloseStatus
    error: ManagedInputError | None

    def __post_init__(self) -> None:
        _require_exact(self.hardware_id, ManagedHardwareId, "hardware_id")
        _require_exact(self.status, ManagedInputCloseStatus, "status")
        if self.error is not None:
            _require_exact(self.error, ManagedInputError, "error")
        if self.status in (
            ManagedInputCloseStatus.CLOSED,
            ManagedInputCloseStatus.NOT_OPEN,
        ):
            if self.error is not None:
                raise ValueError("closed result cannot carry an error")
        elif self.error is None:
            raise ValueError("unclosed result requires an error")


@final
@dataclass(frozen=True, slots=True)
class ManagedInputShutdownReport:
    clean: bool
    process_state: ManagedInputProcessState
    blocker: ManagedInputBlocker | None
    close_results: tuple[ManagedInputCloseResult, ...]
    error: ManagedInputError | None

    def __post_init__(self) -> None:
        _require_exact(self.clean, bool, "clean")
        _require_exact(
            self.process_state, ManagedInputProcessState, "process_state"
        )
        if self.blocker is not None:
            _require_exact(self.blocker, ManagedInputBlocker, "blocker")
        _require_tuple(
            self.close_results, ManagedInputCloseResult, "close_results"
        )
        if self.error is not None:
            _require_exact(self.error, ManagedInputError, "error")
        ids = [item.hardware_id for item in self.close_results]
        if len(set(ids)) != len(ids):
            raise ValueError("close results must be unique")
        if self.clean:
            if (
                self.process_state is not ManagedInputProcessState.CLOSED
                or self.blocker is not None
                or self.error is not None
                or any(
                    item.status
                    not in (
                        ManagedInputCloseStatus.CLOSED,
                        ManagedInputCloseStatus.NOT_OPEN,
                    )
                    for item in self.close_results
                )
            ):
                raise ValueError("clean shutdown report is invalid")
            return
        if self.process_state is not ManagedInputProcessState.SHUTDOWN_INCOMPLETE:
            raise ValueError("dirty shutdown must be incomplete")
        if self.blocker is not None:
            if self.close_results or self.error is not None:
                raise ValueError(
                    "blocked shutdown cannot carry close results or error"
                )
        elif not self.close_results:
            if (
                self.error is None
                or (
                    self.error.code
                    is not ManagedInputErrorCode.SHUTDOWN_TIMEOUT
                    and (
                        self.error.code
                        is not ManagedInputErrorCode.INVARIANT_VIOLATION
                        or self.error.stage != "resident_close"
                    )
                )
            ):
                raise ValueError(
                    "empty shutdown failure requires timeout or close invariant"
                )
        elif not any(
            item.status
            in (
                ManagedInputCloseStatus.TIMED_OUT,
                ManagedInputCloseStatus.FAILED,
            )
            for item in self.close_results
        ):
            raise ValueError(
                "dirty close report requires an unclosed result"
            )


def _decode_json(raw: str) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("calibration_json is invalid") from exc


def _validate_calibration_channels(
    channels: object,
    expected_channels: int,
) -> tuple[tuple[int, bool, int, float | None], ...]:
    _require_int(
        expected_channels,
        "expected_channels",
        minimum=1,
        maximum=MAX_CALIBRATABLE_INPUT_CHANNELS,
    )
    _require_exact(channels, tuple, "channels")
    if len(channels) != expected_channels:
        raise ValueError("calibration channel count differs")
    canonical: list[tuple[int, bool, int, float | None]] = []
    for entry in channels:
        if type(entry) is not tuple or len(entry) != 4:
            raise ValueError("calibration channel entry is invalid")
        channel_index, trusted, channel_revision, correction_db = entry
        _require_int(channel_index, "channel_index")
        _require_exact(trusted, bool, "trusted")
        _require_int(
            channel_revision, "channel_calibration_revision"
        )
        if trusted:
            _require_float(
                correction_db,
                "correction_db",
                minimum=-400.0,
                maximum=400.0,
            )
        elif correction_db is not None:
            raise ValueError("untrusted channel cannot have a correction")
        canonical.append(
            (channel_index, trusted, channel_revision, correction_db)
        )
    canonical.sort(key=lambda entry: entry[0])
    if [entry[0] for entry in canonical] != list(range(expected_channels)):
        raise ValueError("calibration channels must be exact and contiguous")
    return tuple(canonical)


def serialize_calibration_json(
    channels: tuple[tuple[int, bool, int, float | None], ...],
    *,
    expected_channels: int,
) -> str:
    canonical = _validate_calibration_channels(channels, expected_channels)
    payload = {
        "version": 1,
        "channels": [
            {
                "channel_index": channel_index,
                "trusted": trusted,
                "channel_calibration_revision": channel_revision,
                "correction_db": correction_db,
            }
            for channel_index, trusted, channel_revision, correction_db in canonical
        ],
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("calibration_json is invalid") from exc
    if len(encoded.encode("utf-8")) > _MAX_CALIBRATION_JSON_BYTES:
        raise ValueError("calibration_json exceeds 64 KiB")
    return encoded


def parse_calibration_json(
    raw: str,
    *,
    expected_channels: int,
) -> tuple[tuple[int, bool, int, float | None], ...]:
    _require_int(
        expected_channels,
        "expected_channels",
        minimum=1,
        maximum=MAX_CALIBRATABLE_INPUT_CHANNELS,
    )
    _require_exact(raw, str, "calibration_json")
    if len(raw.encode("utf-8")) > _MAX_CALIBRATION_JSON_BYTES:
        raise ValueError("calibration_json exceeds 64 KiB")
    payload = _decode_json(raw)
    if type(payload) is not dict or set(payload) != {"version", "channels"}:
        raise ValueError("calibration_json has unknown fields")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("calibration_json version is invalid")
    raw_channels = payload["channels"]
    if type(raw_channels) is not list:
        raise ValueError("calibration_json channels must be an array")
    entries: list[tuple[int, bool, int, float | None]] = []
    expected_fields = {
        "channel_index",
        "trusted",
        "channel_calibration_revision",
        "correction_db",
    }
    for raw_entry in raw_channels:
        if type(raw_entry) is not dict or set(raw_entry) != expected_fields:
            raise ValueError("calibration channel fields are invalid")
        entries.append(
            (
                raw_entry["channel_index"],
                raw_entry["trusted"],
                raw_entry["channel_calibration_revision"],
                raw_entry["correction_db"],
            )
        )
    canonical = _validate_calibration_channels(tuple(entries), expected_channels)
    if serialize_calibration_json(
        canonical, expected_channels=expected_channels
    ) != raw:
        raise ValueError("calibration_json is not canonical")
    return canonical


class ManagedInputQueryPort(Protocol):
    def inventory_snapshot(self) -> ManagedInputInventorySnapshot: ...

    def endpoint_snapshot(self) -> tuple[InputEndpointObservation, ...]: ...


class ManagedInputManagementPort(Protocol):
    def add(self, command: AddManagedInputCommand) -> ManagedInputResult: ...

    def select_or_replace(
        self, command: SelectOrReplaceManagedInputCommand
    ) -> ManagedInputResult: ...

    def remove(self, command: RemoveManagedInputCommand) -> ManagedInputResult: ...

    def refresh(self, command: RefreshManagedInputsCommand) -> ManagedInputResult: ...

    def calibrate(
        self, command: CalibrateManagedInputCommand
    ) -> ManagedInputResult: ...


class ManagedInputRuntimePort(Protocol):
    def bootstrap(self) -> ManagedInputBootstrapResult: ...

    def map_startup_endpoint(
        self, command: MapStartupEndpointCommand
    ) -> ManagedInputBootstrapResult: ...

    def finalize_bootstrap(self) -> ManagedInputBootstrapResult: ...

    def report_stream_failure(
        self, notice: ResidentInputFailureNotice
    ) -> ManagedInputSnapshot: ...

    def shutdown(self, timeout_seconds: float) -> ManagedInputShutdownReport: ...


class OfflineMaintenanceBlockerPort(Protocol):
    def snapshot(
        self, hardware_id: ManagedHardwareId | None
    ) -> OfflineMaintenanceBlockerSnapshot: ...


class ResidentManagedInputBoundary(Protocol):
    def open_startup_streams(
        self, endpoints: tuple[HardwareEndpointIdentity, ...]
    ) -> ResidentStartupOpenResult: ...

    def measure_calibration(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
        channel_index: int,
        duration_frames: int,
    ) -> ResidentCalibrationMeasurement: ...

    def close_all(
        self, timeout_seconds: float
    ) -> tuple[ManagedInputCloseResult, ...]: ...


class RawManagedInputBackendPort(Protocol):
    def query_raw_input_endpoints(
        self, scan_id: BackendScanId
    ) -> RawBackendInputScan: ...

    def requery_raw_input_endpoint(
        self, scan_id: BackendScanId, device_index: int
    ) -> InputEndpointObservation | None: ...

    @property
    def refresh_requires_reinitialize(self) -> bool: ...


class ManagedInputDiscoveryBoundary(Protocol):
    @property
    def refresh_requires_reinitialize(self) -> bool: ...

    def discover(self, scan_id: BackendScanId) -> BackendDiscoveryResult: ...

    def requery_validated_input_endpoint(
        self, scan_id: BackendScanId, device_index: int
    ) -> InputEndpointObservation | None: ...


class ManagedInputReferenceBoundary(Protocol):
    def configuration_references(
        self, hardware_id: ManagedHardwareId
    ) -> bool: ...
