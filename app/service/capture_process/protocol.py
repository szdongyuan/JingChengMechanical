"""Bounded value contracts, deliberately independent of Qt and numerical runtimes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import re
import math


PROTOCOL_VERSION = 1
# Match Resident's existing bounds without importing its numerical runtime.
MAX_CAPTURES = 6
MAX_CAPTURE_BUFFER_BYTES = 256 * 1024 * 1024
MAX_CHANNELS = 64
MAX_SAMPLE_RATE_HZ = 384_000
MAX_FRAMES = (1 << 31) - 1
MAX_COUNTER = (1 << 63) - 1
MAX_TEXT_CHARS = 256
FLOAT32_BYTES = 4
MAX_PREVIEW_FRAMES = 1_048_576
MAX_PREVIEW_SLOTS = 4


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    frames_per_buffer: int
    max_capture_frames: int
    startup_stream_timeout_s: float = 3.0
    fault_close_timeout_s: float = 1.0
    shutdown_timeout_s: float = 2.0
    calibration_wait_margin_s: float = 1.0
    control_timeout_s: float = 3.0
    max_captures: int = 6
    max_retained_bytes: int | None = None

    def __post_init__(self):
        bounded_int(self.frames_per_buffer, "frames_per_buffer", MAX_PREVIEW_FRAMES)
        bounded_int(self.max_capture_frames, "max_capture_frames", MAX_FRAMES)
        bounded_int(self.max_captures, "max_captures", MAX_CAPTURES)
        for name in ("startup_stream_timeout_s", "fault_close_timeout_s",
                     "shutdown_timeout_s", "calibration_wait_margin_s", "control_timeout_s"):
            value = getattr(self, name)
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError(f"{name} must be positive bounded seconds")


class TransportCapacityError(RuntimeError):
    """Active and unacknowledged captures have consumed the configured budget."""


def bounded_int(value: object, name: str, maximum: int, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def bounded_text(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_TEXT_CHARS:
        raise ValueError(f"{name} must be nonempty bounded text")
    return value


def resource_id(value: object, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError(f"{name} must be a 32-character lowercase hexadecimal ID")
    return value


def pcm_bytes(frames: int, channels: int, sample_rate_hz: int) -> int:
    bounded_int(frames, "frames", MAX_FRAMES)
    bounded_int(channels, "channels", MAX_CHANNELS)
    bounded_int(sample_rate_hz, "sample_rate_hz", MAX_SAMPLE_RATE_HZ)
    size = frames * channels * FLOAT32_BYTES
    bounded_int(size, "PCM bytes", MAX_CAPTURE_BUFFER_BYTES)
    return size


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    max_capture_frames: int
    channel_counts: tuple[int, ...]
    max_captures: int = MAX_CAPTURES
    max_retained_bytes: int | None = None

    def __post_init__(self):
        bounded_int(self.max_capture_frames, "max_capture_frames", MAX_FRAMES)
        bounded_int(self.max_captures, "max_captures", MAX_CAPTURES)
        if (
            type(self.channel_counts) is not tuple
            or not 1 <= len(self.channel_counts) <= MAX_CAPTURES
        ):
            raise ValueError("channel_counts must describe one through six streams")
        for channels in self.channel_counts:
            pcm_bytes(self.max_capture_frames, channels, 1)
        budget = (
            self.max_captures * self.max_capture_frames
            * max(self.channel_counts) * FLOAT32_BYTES
        )
        if self.max_retained_bytes is None:
            object.__setattr__(self, "max_retained_bytes", budget)
        bounded_int(self.max_retained_bytes, "max_retained_bytes", budget)

    def validate_pcm(self, frames: int, channels: int, sample_rate_hz: int) -> int:
        size = pcm_bytes(frames, channels, sample_rate_hz)
        if frames > self.max_capture_frames or channels > max(self.channel_counts):
            raise ValueError("PCM exceeds configured capture dimensions")
        return size


@dataclass(frozen=True, slots=True)
class CaptureReservation:
    version: int
    incarnation: str
    owner_id: str
    capture_id: str
    resource_id: str
    frames: int
    channels: int
    sample_rate_hz: int
    nbytes: int

    def __post_init__(self):
        self.validate()

    def validate(self):
        bounded_int(self.version, "version", PROTOCOL_VERSION, PROTOCOL_VERSION)
        resource_id(self.incarnation, "incarnation")
        resource_id(self.owner_id, "owner_id")
        resource_id(self.resource_id, "resource_id")
        bounded_text(self.capture_id, "capture_id")
        size = pcm_bytes(self.frames, self.channels, self.sample_rate_hz)
        bounded_int(self.nbytes, "nbytes", MAX_CAPTURE_BUFFER_BYTES)
        if self.nbytes != size:
            raise ValueError("nbytes does not match float32 PCM dimensions")


@dataclass(frozen=True, slots=True)
class CompletedAudioDescriptor(CaptureReservation):
    shm_name: str

    def validate(self):
        super(CompletedAudioDescriptor, self).validate()
        expected = f"capture_pcm_{self.incarnation}_{self.owner_id}_{self.resource_id}"
        if type(self.shm_name) is not str or self.shm_name != expected:
            raise ValueError("shared-memory name does not match its owner and resource")

    def to_wire(self) -> dict[str, int | str]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_wire(cls, value: object) -> CompletedAudioDescriptor:
        if type(value) is not dict or value.keys() != {field.name for field in fields(cls)}:
            raise ValueError("invalid completed-audio descriptor fields")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class PreviewStreamConfig:
    hardware_id: str
    max_frames: int
    channels: int
    sample_rate_hz: int

    def __post_init__(self):
        bounded_text(self.hardware_id, "hardware_id")
        bounded_int(self.max_frames, "max_frames", MAX_PREVIEW_FRAMES)
        pcm_bytes(self.max_frames, self.channels, self.sample_rate_hz)


@dataclass(frozen=True, slots=True)
class PreviewStreamHandle:
    """Bootstrap-only handle; the lock travels through spawn, never through RPC."""

    config: PreviewStreamConfig
    shm_name: str
    lock: object


@dataclass(frozen=True, slots=True)
class PreviewBootstrap:
    version: int
    incarnation: str
    owner_id: str
    slots_per_stream: int
    streams: tuple[PreviewStreamHandle, ...]

    def __post_init__(self):
        self.validate()

    def validate(self):
        from multiprocessing.synchronize import Lock

        bounded_int(self.version, "version", PROTOCOL_VERSION, PROTOCOL_VERSION)
        resource_id(self.incarnation, "incarnation")
        resource_id(self.owner_id, "owner_id")
        bounded_int(self.slots_per_stream, "slots_per_stream", MAX_PREVIEW_SLOTS)
        if type(self.streams) is not tuple or not 1 <= len(self.streams) <= MAX_CAPTURES:
            raise ValueError("preview requires one through six streams")
        hardware_ids = set()
        for index, handle in enumerate(self.streams):
            if (
                type(handle) is not PreviewStreamHandle
                or type(handle.config) is not PreviewStreamConfig
            ):
                raise ValueError("invalid preview stream handle")
            handle.config.__post_init__()
            expected = f"capture_preview_{self.incarnation}_{self.owner_id}_{index}"
            if type(handle.shm_name) is not str or handle.shm_name != expected:
                raise ValueError("preview mapping does not match its owner and stream")
            if not isinstance(handle.lock, Lock):
                raise ValueError("preview bootstrap requires a multiprocessing Lock")
            if handle.config.hardware_id in hardware_ids:
                raise ValueError("duplicate preview hardware_id")
            hardware_ids.add(handle.config.hardware_id)


@dataclass(frozen=True, slots=True)
class PreviewBlock:
    incarnation: str
    hardware_id: str
    generation_id: str
    sequence: int
    first_sample: int
    monotonic_ns: int
    sample_rate_hz: int
    pcm: object
