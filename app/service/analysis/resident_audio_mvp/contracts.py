"""Small immutable contracts for the resident audio MVP."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

import numpy as np


MAX_BOUNDED_TEXT_CHARS = 256
MAX_INDEX = (1 << 31) - 1
MAX_SAMPLE_RATE_HZ = 384_000
MAX_INPUT_CHANNELS = 64
MAX_FRAMES_PER_BUFFER = 1_048_576
MAX_ALLOCATABLE_FRAMES = (1 << 31) - 1
MAX_RING_BYTES_PER_STREAM = 256 * 1024 * 1024
MAX_CAPTURE_BUFFER_BYTES = 256 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 300.0
MAX_RESIDENT_STREAMS = 6
MAX_CAPTURE_BINDINGS = 6
FLOAT32_BYTES = np.dtype(np.float32).itemsize


class ResidentErrorCode(str, Enum):
    INVALID = "INVALID"
    CAPACITY = "CAPACITY"
    NOT_READY = "NOT_READY"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAULT = "FAULT"
    CLOSING = "CLOSING"
    BUSY = "BUSY"
    NON_CLEAN = "NON_CLEAN"


class CaptureOutcomeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class CancelStatus(str, Enum):
    CANCELLED = "CANCELLED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    INVALID = "INVALID"


class MaintenanceStatus(str, Enum):
    READY = "READY"
    BUSY = "BUSY"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class StreamOpenStatus(str, Enum):
    READY = "READY"
    FAULTED = "FAULTED"


class ShutdownStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"
    NON_CLEAN = "NON_CLEAN"


class BackendCallbackStatus(str, Enum):
    OK = "OK"
    INPUT_OVERFLOW = "INPUT_OVERFLOW"
    DEVICE_FAULT = "DEVICE_FAULT"
    BACKEND_FAULT = "BACKEND_FAULT"


class BackendCallbackDirective(str, Enum):
    CONTINUE = "CONTINUE"
    STOP = "STOP"


class BackendCloseStatus(str, Enum):
    CLOSED = "CLOSED"
    NOT_CONFIRMED = "NOT_CONFIRMED"


def _bounded_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_BOUNDED_TEXT_CHARS:
        raise _invalid(f"{field_name} must be a non-empty bounded string")
    return value


def _exact_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int = MAX_INDEX,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise _invalid(
            f"{field_name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _bounded_id_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise _invalid(f"{field_name} must be a tuple")
    normalized = tuple(_bounded_text(item, field_name) for item in value)
    if normalized != tuple(sorted(set(normalized))):
        raise _invalid(f"{field_name} must be unique and stable-sorted")
    return normalized


@dataclass(frozen=True, slots=True)
class ResidentError:
    code: ResidentErrorCode
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ResidentErrorCode):
            raise _invalid("error code must be a ResidentErrorCode")
        _bounded_text(self.summary, "error summary")


class ResidentRequestError(Exception):
    """Synchronous request failure carrying one bounded value."""

    def __init__(self, error: ResidentError) -> None:
        if not isinstance(error, ResidentError):
            raise TypeError("error must be a ResidentError")
        super().__init__(error.summary)
        self.error = error


def _invalid(summary: str) -> ResidentRequestError:
    bounded_summary = (
        summary[:MAX_BOUNDED_TEXT_CHARS]
        if type(summary) is str and summary
        else "invalid request"
    )
    error = object.__new__(ResidentError)
    object.__setattr__(error, "code", ResidentErrorCode.INVALID)
    object.__setattr__(error, "summary", bounded_summary)
    return ResidentRequestError(error)


@dataclass(frozen=True, slots=True)
class ResidentStreamSpec:
    hardware_id: str
    device_index: int
    device_name: str
    hostapi_index: int
    hostapi_name: str
    sample_rate_hz: int
    input_channel_count: int
    capture_channel_indices: tuple[int, ...]
    sample_format: str
    frames_per_buffer: int

    def __post_init__(self) -> None:
        _bounded_text(self.hardware_id, "hardware_id")
        _exact_int(self.device_index, "device_index", minimum=0)
        _bounded_text(self.device_name, "device_name")
        _exact_int(self.hostapi_index, "hostapi_index", minimum=0)
        _bounded_text(self.hostapi_name, "hostapi_name")
        _exact_int(
            self.sample_rate_hz,
            "sample_rate_hz",
            minimum=1,
            maximum=MAX_SAMPLE_RATE_HZ,
        )
        _exact_int(
            self.input_channel_count,
            "input_channel_count",
            minimum=1,
            maximum=MAX_INPUT_CHANNELS,
        )
        if self.sample_format != "float32" or type(self.sample_format) is not str:
            raise _invalid("sample_format must be float32")
        _exact_int(
            self.frames_per_buffer,
            "frames_per_buffer",
            minimum=1,
            maximum=MAX_FRAMES_PER_BUFFER,
        )
        indices = self.capture_channel_indices
        if type(indices) is not tuple or not indices:
            raise _invalid("capture_channel_indices must be a non-empty tuple")
        for index in indices:
            _exact_int(
                index,
                "capture_channel_indices item",
                minimum=0,
                maximum=self.input_channel_count - 1,
            )
        if indices != tuple(sorted(set(indices))):
            raise _invalid("capture_channel_indices must be unique and ordered")


class CaptureCompletion:
    """Stable one-shot completion owned by the caller's binding."""

    __slots__ = ("_event", "_outcome", "_terminal_proof")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._outcome: CaptureOutcome | None = None
        self._terminal_proof: object | None = None

    def wait(self, timeout_seconds: float | None) -> CaptureOutcome | None:
        timeout: int | float | None
        if timeout_seconds is None:
            timeout = None
        else:
            if type(timeout_seconds) not in (int, float):
                raise _invalid(
                    "timeout_seconds must be finite non-negative seconds or None"
                )
            timeout = timeout_seconds
            if (
                timeout < 0
                or timeout > threading.TIMEOUT_MAX
                or not math.isfinite(timeout)
            ):
                raise _invalid(
                    "timeout_seconds must be finite non-negative seconds or None"
                )
        if not self._event.wait(timeout):
            return None
        return self._outcome

    def result_if_ready(self) -> CaptureOutcome | None:
        if not self._event.is_set():
            return None
        return self._outcome

    def _stored_outcome(self) -> CaptureOutcome | None:
        return self._outcome


@dataclass(frozen=True, slots=True, init=False, eq=False)
class CaptureBinding:
    binding_id: str
    run_id: str
    hardware_id: str
    _owner_nonce: object = field(repr=False)
    _completion: CaptureCompletion = field(repr=False)

    def __new__(cls) -> CaptureBinding:
        raise TypeError("CaptureBinding is created only by ResidentAudioPool")

    def __repr__(self) -> str:
        return (
            "CaptureBinding("
            f"binding_id={self.binding_id!r}, "
            f"run_id={self.run_id!r}, "
            f"hardware_id={self.hardware_id!r})"
        )


def _validate_capture_binding(
    value: object,
    field_name: str = "binding",
) -> CaptureBinding:
    if type(value) is not CaptureBinding:
        raise _invalid(f"{field_name} must be an exact CaptureBinding")
    try:
        binding_id = value.binding_id
        run_id = value.run_id
        hardware_id = value.hardware_id
        owner_nonce = value._owner_nonce
        completion = value._completion
    except AttributeError:
        raise _invalid(f"{field_name} is malformed") from None
    _bounded_text(binding_id, f"{field_name} binding_id")
    _bounded_text(run_id, f"{field_name} run_id")
    _bounded_text(hardware_id, f"{field_name} hardware_id")
    if owner_nonce is None or type(completion) is not CaptureCompletion:
        raise _invalid(f"{field_name} is malformed")
    return value


@dataclass(frozen=True, slots=True)
class CapturedAudio:
    binding: CaptureBinding
    run_id: str
    hardware_id: str
    trigger_monotonic_ns: int
    start_frame_cursor: int
    frame_count: int
    sample_rate_hz: int
    channel_indices: tuple[int, ...]
    samples: np.ndarray
    completion_monotonic_ns: int

    def __post_init__(self) -> None:
        _validate_capture_binding(self.binding, "captured audio binding")
        if self.run_id != self.binding.run_id:
            raise _invalid("captured audio run_id must match binding")
        if self.hardware_id != self.binding.hardware_id:
            raise _invalid("captured audio hardware_id must match binding")
        _exact_int(
            self.trigger_monotonic_ns,
            "trigger_monotonic_ns",
            minimum=0,
            maximum=(1 << 63) - 1,
        )
        _exact_int(
            self.start_frame_cursor,
            "start_frame_cursor",
            minimum=0,
            maximum=(1 << 63) - 1,
        )
        _exact_int(
            self.frame_count,
            "frame_count",
            minimum=1,
            maximum=MAX_ALLOCATABLE_FRAMES,
        )
        _exact_int(
            self.sample_rate_hz,
            "sample_rate_hz",
            minimum=1,
            maximum=MAX_SAMPLE_RATE_HZ,
        )
        if type(self.channel_indices) is not tuple or not self.channel_indices:
            raise _invalid("channel_indices must be a non-empty ordered tuple")
        for index in self.channel_indices:
            _exact_int(
                index,
                "channel_indices item",
                minimum=0,
                maximum=MAX_INPUT_CHANNELS - 1,
            )
        if self.channel_indices != tuple(sorted(set(self.channel_indices))):
            raise _invalid("channel_indices must be unique and strictly ordered")
        samples = self.samples
        if (
            not isinstance(samples, np.ndarray)
            or samples.dtype != np.dtype(np.float32)
            or samples.ndim != 2
            or not samples.flags.c_contiguous
            or samples.flags.writeable
            or samples.base is not None
            or not samples.flags.owndata
            or samples.shape != (self.frame_count, len(self.channel_indices))
        ):
            raise _invalid(
                "samples must be owned read-only contiguous float32 audio"
            )
        immutable_samples = np.ndarray(
            samples.shape,
            dtype=np.float32,
            buffer=samples.tobytes(order="C"),
            order="C",
        )
        object.__setattr__(self, "samples", immutable_samples)
        _exact_int(
            self.completion_monotonic_ns,
            "completion_monotonic_ns",
            minimum=0,
            maximum=(1 << 63) - 1,
        )


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    status: CaptureOutcomeStatus
    binding: CaptureBinding
    audio: CapturedAudio | None
    error: ResidentError | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CaptureOutcomeStatus):
            raise _invalid("capture outcome status is invalid")
        _validate_capture_binding(self.binding, "capture outcome binding")
        if self.status is CaptureOutcomeStatus.COMPLETED:
            if (
                not isinstance(self.audio, CapturedAudio)
                or self.audio.binding is not self.binding
                or self.error is not None
            ):
                raise _invalid("COMPLETED requires matching audio and no error")
        elif self.status is CaptureOutcomeStatus.CANCELLED:
            if (
                self.audio is not None
                or not isinstance(self.error, ResidentError)
                or self.error.code is not ResidentErrorCode.CANCELLED
            ):
                raise _invalid("CANCELLED requires its error and no audio")
        elif (
            self.audio is not None
            or not isinstance(self.error, ResidentError)
            or self.error.code is ResidentErrorCode.CANCELLED
        ):
            raise _invalid("FAILED requires a non-cancel error and no audio")


@dataclass(frozen=True, slots=True)
class CancelResult:
    status: CancelStatus
    outcome: CaptureOutcome | None = None
    error: ResidentError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CancelStatus):
            raise _invalid("cancel status is invalid")
        if self.status is CancelStatus.INVALID:
            if self.outcome is not None or not isinstance(self.error, ResidentError):
                raise _invalid("INVALID cancel requires only an error")
            if self.error.code is not ResidentErrorCode.INVALID:
                raise _invalid("INVALID cancel requires an INVALID error")
        elif (
            not isinstance(self.outcome, CaptureOutcome)
            or self.error is not None
            or (
                self.status is CancelStatus.CANCELLED
                and self.outcome.status is not CaptureOutcomeStatus.CANCELLED
            )
        ):
            raise _invalid("successful cancel result requires only an outcome")


@dataclass(frozen=True, slots=True)
class MaintenanceReadiness:
    status: MaintenanceStatus
    active_binding_ids: tuple[str, ...]
    owned_hardware_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, MaintenanceStatus):
            raise _invalid("maintenance status is invalid")
        active = _bounded_id_tuple(self.active_binding_ids, "active_binding_ids")
        owned = _bounded_id_tuple(self.owned_hardware_ids, "owned_hardware_ids")
        _bounded_text(self.reason, "maintenance reason")
        if self.status is MaintenanceStatus.READY and (active or owned):
            raise _invalid("READY maintenance cannot contain blockers")


@dataclass(frozen=True, slots=True)
class StreamOpenResult:
    hardware_id: str
    status: StreamOpenStatus
    error: ResidentError | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.hardware_id, "hardware_id")
        if not isinstance(self.status, StreamOpenStatus):
            raise _invalid("stream open status is invalid")
        if self.status is StreamOpenStatus.READY:
            if self.error is not None:
                raise _invalid("READY stream cannot contain an error")
        elif not isinstance(self.error, ResidentError):
            raise _invalid("FAULTED stream requires an error")


@dataclass(frozen=True, slots=True)
class OpenResult:
    pool: object
    streams: tuple[StreamOpenResult, ...]
    ready_count: int

    def __post_init__(self) -> None:
        if type(self.streams) is not tuple or any(
            not isinstance(item, StreamOpenResult) for item in self.streams
        ):
            raise _invalid("streams must contain stream open results")
        hardware_ids = tuple(item.hardware_id for item in self.streams)
        if hardware_ids != tuple(sorted(set(hardware_ids))):
            raise _invalid("open streams must be unique and stable-sorted")
        _exact_int(
            self.ready_count,
            "ready_count",
            minimum=0,
            maximum=MAX_RESIDENT_STREAMS,
        )
        if self.ready_count != sum(
            item.status is StreamOpenStatus.READY for item in self.streams
        ):
            raise _invalid("ready_count must match READY stream results")


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    status: ShutdownStatus
    started_monotonic_ns: int
    finished_monotonic_ns: int | None
    unclosed_hardware_ids: tuple[str, ...]
    unjoined_worker_ids: tuple[str, ...]
    non_terminal_binding_ids: tuple[str, ...]
    errors: tuple[ResidentError, ...]
    deadline_monotonic_ns: int
    operator_action: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ShutdownStatus):
            raise _invalid("shutdown status is invalid")
        _exact_int(
            self.started_monotonic_ns,
            "started_monotonic_ns",
            minimum=0,
            maximum=(1 << 63) - 1,
        )
        _exact_int(
            self.deadline_monotonic_ns,
            "deadline_monotonic_ns",
            minimum=1,
            maximum=(1 << 63) - 1,
        )
        if self.deadline_monotonic_ns < self.started_monotonic_ns:
            raise _invalid("shutdown deadline cannot precede start")
        if self.status is not ShutdownStatus.IN_PROGRESS:
            if (
                type(self.finished_monotonic_ns) is not int
                or self.finished_monotonic_ns < self.started_monotonic_ns
            ):
                raise _invalid("final shutdown requires a valid finish time")
        _bounded_id_tuple(self.unclosed_hardware_ids, "unclosed_hardware_ids")
        _bounded_id_tuple(self.unjoined_worker_ids, "unjoined_worker_ids")
        _bounded_id_tuple(
            self.non_terminal_binding_ids, "non_terminal_binding_ids"
        )
        if type(self.errors) is not tuple or any(
            not isinstance(error, ResidentError) for error in self.errors
        ):
            raise _invalid("shutdown errors must be a tuple of ResidentError")
        if (
            type(self.operator_action) is not str
            or len(self.operator_action) > MAX_BOUNDED_TEXT_CHARS
        ):
            raise _invalid("operator_action must be a bounded string")
        has_leftover = bool(
            self.unclosed_hardware_ids
            or self.unjoined_worker_ids
            or self.non_terminal_binding_ids
            or self.errors
        )
        if self.status is ShutdownStatus.CLOSED:
            if has_leftover or self.operator_action != "":
                raise _invalid(
                    "CLOSED requires no leftovers, errors, or operator action"
                )
        elif self.status is ShutdownStatus.IN_PROGRESS:
            if (
                self.finished_monotonic_ns is not None
                or self.operator_action
                != "等待 owning shutdown caller 的最终报告"
            ):
                raise _invalid(
                    "IN_PROGRESS requires no finish and its exact operator action"
                )
        elif (
            not has_leftover
            or self.operator_action
            != "不要做相关 maintenance；停止并重启进程"
        ):
            raise _invalid(
                "NON_CLEAN requires leftovers and its exact operator action"
            )


@dataclass(frozen=True, slots=True)
class BackendFault:
    status: BackendCallbackStatus
    summary: str

    def __post_init__(self) -> None:
        if type(self.status) is not BackendCallbackStatus or self.status not in (
            BackendCallbackStatus.INPUT_OVERFLOW,
            BackendCallbackStatus.DEVICE_FAULT,
            BackendCallbackStatus.BACKEND_FAULT,
        ):
            raise _invalid("backend fault status must be fatal")
        _bounded_text(self.summary, "backend fault summary")


@dataclass(frozen=True, slots=True)
class BackendCloseResult:
    status: BackendCloseStatus
    error: ResidentError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, BackendCloseStatus):
            raise _invalid("backend close status is invalid")
        if self.status is BackendCloseStatus.CLOSED:
            if self.error is not None:
                raise _invalid("CLOSED backend result cannot contain an error")
        elif (
            not isinstance(self.error, ResidentError)
            or self.error.code is not ResidentErrorCode.NON_CLEAN
        ):
            raise _invalid("NOT_CONFIRMED requires a NON_CLEAN error")


AudioInputCallback = Callable[
    [np.ndarray, int, int, BackendCallbackStatus],
    BackendCallbackDirective,
]
AudioInputFaultCallback = Callable[[BackendFault], None]


class BackendInputStream(Protocol):
    def start(self, deadline_monotonic_ns: int) -> None: ...

    def request_stop(self) -> None: ...

    def close(self, deadline_monotonic_ns: int) -> BackendCloseResult: ...


class AudioInputBackend(Protocol):
    def create_input(
        self,
        spec: ResidentStreamSpec,
        callback: AudioInputCallback,
        on_fault: AudioInputFaultCallback,
    ) -> BackendInputStream: ...
