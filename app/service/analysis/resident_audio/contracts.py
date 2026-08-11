"""Immutable contracts shared by the resident audio capture components."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np


FLOAT32_BYTES = np.dtype(np.float32).itemsize
MAX_CAPTURE_DURATION_SECONDS = 3600.0
MAX_CAPTURE_ARTIFACT_BYTES = 8 * 1024**3


class CaptureErrorCode(str, Enum):
    TOO_MANY_REQUIRED_HARDWARE = "TOO_MANY_REQUIRED_HARDWARE"
    CAPTURE_CAPACITY_FULL = "CAPTURE_CAPACITY_FULL"
    INVALID_CAPTURE_REQUEST = "INVALID_CAPTURE_REQUEST"
    CAPTURE_STORAGE_CAPACITY = "CAPTURE_STORAGE_CAPACITY"
    HARDWARE_RESOLUTION_CONFLICT = "HARDWARE_RESOLUTION_CONFLICT"
    STREAM_OPEN_FAILED = "STREAM_OPEN_FAILED"
    STREAM_NOT_READY = "STREAM_NOT_READY"
    STREAM_GENERATION_CHANGED = "STREAM_GENERATION_CHANGED"
    FIRST_VALID_BLOCK_TIMEOUT = "FIRST_VALID_BLOCK_TIMEOUT"
    TIMING_UNAVAILABLE = "TIMING_UNAVAILABLE"
    TRIGGER_AUDIO_EXPIRED = "TRIGGER_AUDIO_EXPIRED"
    INPUT_OVERFLOW_OR_GAP = "INPUT_OVERFLOW_OR_GAP"
    INPUT_INGRESS_BACKLOG = "INPUT_INGRESS_BACKLOG"
    INPUT_DATA_TIMEOUT = "INPUT_DATA_TIMEOUT"
    CAPTURE_PERSISTENCE_BACKLOG = "CAPTURE_PERSISTENCE_BACKLOG"
    CAPTURE_ARTIFACT_IO_FAILED = "CAPTURE_ARTIFACT_IO_FAILED"
    CAPTURE_CANCELED = "CAPTURE_CANCELED"
    POOL_SHUTTING_DOWN = "POOL_SHUTTING_DOWN"
    SHUTDOWN_INCOMPLETE = "SHUTDOWN_INCOMPLETE"


class LeaseState(str, Enum):
    RESERVED = "RESERVED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class StreamState(str, Enum):
    CLOSED = "CLOSED"
    OPENING = "OPENING"
    READY = "READY"
    TIMING_RECOVERY = "TIMING_RECOVERY"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    CLOSING = "CLOSING"


class CaptureOwnerState(str, Enum):
    PREPARED = "PREPARED"
    WAITING_FOR_MAPPING = "WAITING_FOR_MAPPING"
    CATCHING_UP = "CATCHING_UP"
    LIVE = "LIVE"
    FREEZING = "FREEZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ArtifactState(str, Enum):
    PARTIAL = "PARTIAL"
    READY_UNCLAIMED = "READY_UNCLAIMED"
    CLAIMED = "CLAIMED"
    TRANSFERRED = "TRANSFERRED"
    RELEASED = "RELEASED"
    DELETED = "DELETED"
    DELETE_PENDING = "DELETE_PENDING"


CaptureArtifactOwnership = ArtifactState


class BlockTimeQuality(str, Enum):
    CONFIRMED = "CONFIRMED"
    RECOVERY_UNCONFIRMED = "RECOVERY_UNCONFIRMED"


def _freeze_lightweight(
    value: object,
    *,
    field_name: str,
    active_containers: set[int] | None = None,
) -> object:
    """Validate and recursively freeze lightweight diagnostic metadata."""

    if value is None or isinstance(value, Enum):
        return value
    if type(value) in (str, bool, int, float):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    active = active_containers if active_containers is not None else set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise TypeError(f"{field_name} must not contain recursive containers")
        active.add(identity)
        try:
            frozen: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{field_name} mapping keys must be strings")
                frozen[key] = _freeze_lightweight(
                    item,
                    field_name=f"{field_name}.{key}",
                    active_containers=active,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise TypeError(f"{field_name} must not contain recursive containers")
        active.add(identity)
        try:
            return tuple(
                _freeze_lightweight(
                    item,
                    field_name=f"{field_name}[{index}]",
                    active_containers=active,
                )
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise TypeError(
        f"{field_name} must contain only lightweight values; "
        "audio arrays and structured objects are not allowed"
    )


class CaptureLayerError(Exception):
    """A stable error category plus lightweight diagnostic context."""

    def __init__(
        self,
        code: CaptureErrorCode,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(code, CaptureErrorCode):
            raise TypeError("code must be a CaptureErrorCode")
        details = _freeze_lightweight(
            {} if context is None else context,
            field_name="diagnostic context",
        )
        if not isinstance(details, Mapping):
            raise TypeError("diagnostic context must be a mapping")
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = details


def _invalid_request(message: str, **context: object) -> CaptureLayerError:
    return CaptureLayerError(CaptureErrorCode.INVALID_CAPTURE_REQUEST, message, context)


def _require_nonempty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_integer(value: object, field_name: str, minimum: int) -> None:
    description = "positive" if minimum else "non-negative"
    invalid_type = isinstance(value, (bool, np.bool_)) or not isinstance(
        value, (int, np.integer)
    )
    if invalid_type or int(value) < minimum:
        raise ValueError(f"{field_name} must be a non-boolean {description} integer")


def _require_finite_positive(value: object, field_name: str) -> None:
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise ValueError(f"{field_name} must be positive and finite")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{field_name} must be positive and finite")


def _normalized_timestamp(
    value: object,
    field_name: str,
    *,
    require_finite: bool,
) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise TypeError(f"{field_name} must be numeric")
    normalized = float(value)
    if require_finite and not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _validate_audio_matrix(data: object, frames: int) -> np.ndarray:
    if not isinstance(data, np.ndarray):
        raise TypeError("data must be a NumPy array")
    if data.dtype != np.dtype(np.float32):
        raise TypeError("data must use float32 dtype")
    if data.ndim != 2:
        raise ValueError("data must have shape [frames, channels]")
    if not data.flags.c_contiguous:
        raise ValueError("data must be C-contiguous")
    if data.shape[0] != frames:
        raise ValueError("data frame count must match frames")
    if data.shape[1] < 1:
        raise ValueError("data must contain at least one channel")
    return data


@dataclass(frozen=True, slots=True)
class ResolvedInputDevice:
    hardware_id: str
    device_index: int
    name: str
    host_api: str
    sample_rate: float
    channels: int
    dtype: str = "float32"
    resolved_at_monotonic: float | None = None
    resolution_version: str | int | None = None

    def __post_init__(self) -> None:
        _require_nonempty_string(self.hardware_id, "hardware_id")
        _require_integer(self.device_index, "device_index", 0)
        _require_nonempty_string(self.name, "name")
        _require_nonempty_string(self.host_api, "host_api")
        _require_finite_positive(self.sample_rate, "sample_rate")
        _require_integer(self.channels, "channels", 1)
        if self.dtype != "float32":
            raise ValueError("resident input dtype must be float32")
        if self.resolved_at_monotonic is None and self.resolution_version is None:
            raise ValueError(
                "resolution provenance requires resolved_at_monotonic "
                "or resolution_version"
            )
        if self.resolved_at_monotonic is not None:
            _require_finite_positive(
                self.resolved_at_monotonic, "resolved_at_monotonic"
            )
        if self.resolution_version is not None:
            version = self.resolution_version
            if isinstance(version, str):
                if not version.strip():
                    raise ValueError(
                        "resolution_version must be a non-empty string"
                    )
            elif isinstance(version, (bool, np.bool_)) or not isinstance(
                version, (int, np.integer)
            ):
                raise TypeError(
                    "resolution_version must be a string or non-boolean integer"
                )
            elif int(version) < 0:
                raise ValueError(
                    "integer resolution_version must be non-negative"
                )
            else:
                object.__setattr__(self, "resolution_version", int(version))


@dataclass(frozen=True, slots=True)
class StreamReadyToken:
    hardware_id: str
    pool_epoch: str
    stream_generation: int
    sample_rate: float
    channels: int
    earliest_mappable_monotonic: float
    latest_mappable_monotonic: float
    earliest_sample: int
    latest_sample: int

    def __post_init__(self) -> None:
        _require_nonempty_string(self.hardware_id, "hardware_id")
        _require_nonempty_string(self.pool_epoch, "pool_epoch")
        _require_integer(self.stream_generation, "stream_generation", 1)
        _require_finite_positive(self.sample_rate, "sample_rate")
        _require_integer(self.channels, "channels", 1)
        if not math.isfinite(self.earliest_mappable_monotonic):
            raise ValueError("earliest_mappable_monotonic must be finite")
        if not math.isfinite(self.latest_mappable_monotonic):
            raise ValueError("latest_mappable_monotonic must be finite")
        if (
            self.latest_mappable_monotonic
            < self.earliest_mappable_monotonic
        ):
            raise ValueError("mappable monotonic range is reversed")
        _require_integer(self.earliest_sample, "earliest_sample", 0)
        _require_integer(self.latest_sample, "latest_sample", 0)
        if self.latest_sample < self.earliest_sample:
            raise ValueError("mappable sample range is reversed")


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    run_id: str
    trigger_monotonic: float
    record_duration: float
    stream_token: StreamReadyToken
    artifact_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_string(self.run_id, "run_id")
        if not isinstance(self.stream_token, StreamReadyToken):
            raise TypeError("stream_token must be a StreamReadyToken")
        metadata = _freeze_lightweight(
            self.artifact_metadata,
            field_name="artifact metadata",
        )
        if not isinstance(metadata, Mapping):
            raise TypeError("artifact metadata must be a mapping")
        object.__setattr__(self, "artifact_metadata", metadata)


@dataclass(frozen=True, slots=True)
class RawCallbackBlock:
    pool_epoch: str
    hardware_id: str
    stream_generation: int
    callback_sequence: int
    callback_monotonic: float
    input_buffer_adc_time: float
    current_time: float
    status: object
    frames: int
    data: np.ndarray = field(repr=False)

    def __post_init__(self) -> None:
        _require_nonempty_string(self.pool_epoch, "pool_epoch")
        _require_nonempty_string(self.hardware_id, "hardware_id")
        _require_integer(self.stream_generation, "stream_generation", 1)
        _require_integer(self.callback_sequence, "callback_sequence", 0)
        object.__setattr__(
            self,
            "callback_monotonic",
            _normalized_timestamp(
                self.callback_monotonic,
                "callback_monotonic",
                require_finite=True,
            ),
        )
        object.__setattr__(
            self,
            "input_buffer_adc_time",
            _normalized_timestamp(
                self.input_buffer_adc_time,
                "input_buffer_adc_time",
                require_finite=False,
            ),
        )
        object.__setattr__(
            self,
            "current_time",
            _normalized_timestamp(
                self.current_time,
                "current_time",
                require_finite=False,
            ),
        )
        _require_integer(self.frames, "frames", 1)
        _validate_audio_matrix(self.data, self.frames)


@dataclass(frozen=True, slots=True)
class CapturedAudioBlock:
    pool_epoch: str
    hardware_id: str
    stream_generation: int
    sample_start: int
    sample_end: int
    data: np.ndarray = field(repr=False)
    time_quality: BlockTimeQuality
    monotonic_start: float | None = None
    monotonic_end: float | None = None
    status_summary: str = ""

    def __post_init__(self) -> None:
        _require_nonempty_string(self.pool_epoch, "pool_epoch")
        _require_nonempty_string(self.hardware_id, "hardware_id")
        _require_integer(self.stream_generation, "stream_generation", 1)
        _require_integer(self.sample_start, "sample_start", 0)
        _require_integer(self.sample_end, "sample_end", 0)
        if self.sample_end < self.sample_start:
            raise ValueError("sample range must be a non-negative half-open range")
        try:
            data = _validate_audio_matrix(
                self.data, self.sample_end - self.sample_start
            )
        except ValueError as error:
            if "frame count" in str(error):
                raise ValueError(
                    "sample range must match the data frame count"
                ) from error
            raise
        if not isinstance(self.time_quality, BlockTimeQuality):
            raise TypeError("time_quality must be a BlockTimeQuality")
        has_start = self.monotonic_start is not None
        has_end = self.monotonic_end is not None
        if self.time_quality is BlockTimeQuality.CONFIRMED:
            if not has_start or not has_end:
                raise ValueError(
                    "confirmed time quality requires both monotonic bounds"
                )
        elif has_start or has_end:
            raise ValueError(
                "recovery/unconfirmed time quality requires absent monotonic bounds"
            )
        if not isinstance(self.status_summary, str):
            raise TypeError("status_summary must be a string")
        if self.monotonic_start is not None and not math.isfinite(
            self.monotonic_start
        ):
            raise ValueError("monotonic_start must be finite when present")
        if self.monotonic_end is not None and not math.isfinite(self.monotonic_end):
            raise ValueError("monotonic_end must be finite when present")
        if (
            self.monotonic_start is not None
            and self.monotonic_end is not None
            and self.monotonic_end < self.monotonic_start
        ):
            raise ValueError("monotonic range is reversed")
        data.setflags(write=False)


@dataclass(frozen=True, slots=True)
class FrozenCaptureArtifact:
    run_id: str
    artifact_id: str
    path: str
    format_version: int
    dtype: str
    sample_rate: float
    channels: int
    captured_frames: int
    target_frames: int
    sample_start: int
    sample_end: int
    pool_epoch: str
    stream_generation: int
    ownership: ArtifactState
    trigger_monotonic: float
    capture_start_monotonic: float
    time_quality: str
    publication_token: str

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "artifact_id",
            "path",
            "pool_epoch",
            "time_quality",
            "publication_token",
        ):
            _require_nonempty_string(getattr(self, field_name), field_name)
        _require_integer(self.format_version, "format_version", 1)
        if self.dtype != "float32":
            raise ValueError("artifact dtype must be float32")
        _require_finite_positive(self.sample_rate, "sample_rate")
        _require_integer(self.channels, "channels", 1)
        _require_integer(self.captured_frames, "captured_frames", 0)
        _require_integer(self.target_frames, "target_frames", 1)
        _require_integer(self.sample_start, "sample_start", 0)
        _require_integer(self.sample_end, "sample_end", 0)
        if self.sample_end - self.sample_start != self.captured_frames:
            raise ValueError("artifact sample range must match captured_frames")
        if self.captured_frames != self.target_frames:
            raise ValueError("frozen artifact must contain exactly target_frames")
        _require_integer(self.stream_generation, "stream_generation", 1)
        if not isinstance(self.ownership, ArtifactState):
            raise TypeError("ownership must be an ArtifactState")
        _require_finite_positive(self.trigger_monotonic, "trigger_monotonic")
        _require_finite_positive(
            self.capture_start_monotonic, "capture_start_monotonic"
        )


@dataclass(frozen=True, slots=True)
class CaptureSize:
    target_frames: int
    artifact_bytes: int


def checked_capture_size(
    record_duration: float,
    sample_rate: float,
    channels: int,
    *,
    max_artifact_bytes: int = MAX_CAPTURE_ARTIFACT_BYTES,
) -> CaptureSize:
    """Calculate capture allocation dimensions without platform-int overflow."""

    try:
        duration = float(record_duration)
        rate = float(sample_rate)
    except (TypeError, ValueError, OverflowError) as error:
        raise _invalid_request("duration and sample rate must be numeric") from error
    if isinstance(record_duration, (bool, np.bool_)):
        raise _invalid_request("record duration must be positive and finite")
    if not math.isfinite(duration) or duration <= 0:
        raise _invalid_request("record duration must be positive and finite")
    if duration > MAX_CAPTURE_DURATION_SECONDS:
        raise _invalid_request(
            "record duration exceeds 3600 second limit",
            record_duration=duration,
        )
    if isinstance(sample_rate, (bool, np.bool_)):
        raise _invalid_request("sample rate must be positive and finite")
    if not math.isfinite(rate) or rate <= 0:
        raise _invalid_request("sample rate must be positive and finite")
    if isinstance(channels, (bool, np.bool_)) or not isinstance(
        channels, (int, np.integer)
    ):
        raise _invalid_request("channels must be a non-boolean positive integer")
    channel_count = int(channels)
    if channel_count <= 0:
        raise _invalid_request("channels must be a non-boolean positive integer")
    if (
        isinstance(max_artifact_bytes, (bool, np.bool_))
        or not isinstance(max_artifact_bytes, (int, np.integer))
        or int(max_artifact_bytes) <= 0
        or int(max_artifact_bytes) > MAX_CAPTURE_ARTIFACT_BYTES
    ):
        raise ValueError("max_artifact_bytes must be within the 8 GiB product limit")

    frame_product = duration * rate
    if not math.isfinite(frame_product) or frame_product > sys.maxsize:
        raise _invalid_request("target frame count exceeds platform integer range")
    target_frames = math.ceil(frame_product)
    if target_frames < 1:
        raise _invalid_request("target frame count must be at least one")
    if target_frames > sys.maxsize // channel_count:
        raise _invalid_request("artifact element count exceeds platform integer range")
    sample_count = target_frames * channel_count
    if sample_count > sys.maxsize // FLOAT32_BYTES:
        raise _invalid_request("artifact byte count exceeds platform integer range")
    artifact_bytes = sample_count * FLOAT32_BYTES
    if artifact_bytes > int(max_artifact_bytes):
        raise _invalid_request(
            "artifact byte count exceeds capture safety limit",
            artifact_bytes=artifact_bytes,
            max_artifact_bytes=int(max_artifact_bytes),
        )
    return CaptureSize(target_frames=target_frames, artifact_bytes=artifact_bytes)


def checked_capture_request(
    request: CaptureRequest,
    *,
    commit_monotonic: float,
    max_artifact_bytes: int = MAX_CAPTURE_ARTIFACT_BYTES,
) -> CaptureSize:
    """Validate a request completely before a lease enters COMMITTING."""

    if not isinstance(request, CaptureRequest):
        raise TypeError("request must be a CaptureRequest")
    trigger = request.trigger_monotonic
    if isinstance(trigger, (bool, np.bool_)) or not isinstance(
        trigger, (int, float, np.integer, np.floating)
    ):
        raise _invalid_request("trigger_monotonic must be positive and finite")
    trigger_value = float(trigger)
    if not math.isfinite(trigger_value) or trigger_value <= 0:
        raise _invalid_request("trigger_monotonic must be positive and finite")
    if not math.isfinite(commit_monotonic):
        raise _invalid_request("commit_monotonic must be finite")
    tolerance = 1.0 / request.stream_token.sample_rate
    if trigger_value > commit_monotonic + tolerance:
        raise _invalid_request(
            "trigger_monotonic is later than commit time tolerance",
            trigger_monotonic=trigger_value,
            commit_monotonic=commit_monotonic,
        )
    return checked_capture_size(
        request.record_duration,
        request.stream_token.sample_rate,
        request.stream_token.channels,
        max_artifact_bytes=max_artifact_bytes,
    )
