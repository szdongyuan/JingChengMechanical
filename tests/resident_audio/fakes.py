"""Deterministic fakes for resident stream tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.service.analysis.resident_audio.contracts import ResolvedInputDevice


class ManualClock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += float(seconds)
        return self.value


@dataclass
class ScheduledCall:
    delay: float
    callback: Callable[[], None]
    canceled: bool = False

    def cancel(self) -> None:
        self.canceled = True

    def run(self, *, even_if_canceled: bool = False) -> None:
        if not self.canceled or even_if_canceled:
            self.callback()


class ManualScheduler:
    """Records retry tasks and lets tests run stale callbacks deterministically."""

    def __init__(self) -> None:
        self.calls: list[ScheduledCall] = []

    def schedule(
        self, delay: float, callback: Callable[[], None]
    ) -> ScheduledCall:
        call = ScheduledCall(float(delay), callback)
        self.calls.append(call)
        return call

class FakeCancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ResolveCall:
    hardware_id: str
    desired_revision: int
    resolution_attempt_id: int
    cancel_token: object


class FakeResolver:
    def __init__(self, resolved: ResolvedInputDevice) -> None:
        self.resolved = resolved
        self.calls: list[ResolveCall] = []

    def resolve(
        self,
        hardware_id: str,
        desired_revision: int,
        resolution_attempt_id: int,
        cancel_token: object,
    ) -> ResolvedInputDevice:
        self.calls.append(
            ResolveCall(
                hardware_id,
                desired_revision,
                resolution_attempt_id,
                cancel_token,
            )
        )
        return self.resolved


@dataclass(frozen=True)
class OpenCall:
    resolved: ResolvedInputDevice
    block_frames: int
    callback: Callable[..., None]


class FakeInputHandle:
    def __init__(self, *, input_latency: float = 0.01) -> None:
        self._input_latency = input_latency
        self.latency_error: Exception | None = None
        self.callback: Callable[..., None] | None = None
        self.calls: list[str] = []
        self.raise_on_start: Exception | None = None
        self.abort_errors: list[Exception] = []
        self.stop_errors: list[Exception] = []
        self.close_errors: list[Exception] = []
        self.physically_closed = False

    @property
    def input_latency(self) -> float:
        if self.latency_error is not None:
            raise self.latency_error
        return self._input_latency

    def start(self) -> None:
        if self.physically_closed:
            raise AssertionError("start called after physical close")
        self.calls.append("start")
        if self.raise_on_start is not None:
            raise self.raise_on_start

    def abort(self) -> None:
        if self.physically_closed:
            raise AssertionError("abort called after physical close")
        self.calls.append("abort")
        if self.abort_errors:
            raise self.abort_errors.pop(0)

    def stop(self) -> None:
        if self.physically_closed:
            raise AssertionError("stop called after physical close")
        self.calls.append("stop")
        if self.stop_errors:
            raise self.stop_errors.pop(0)

    def close(self) -> None:
        if self.physically_closed:
            raise AssertionError("close called after physical close")
        self.calls.append("close")
        if self.close_errors:
            raise self.close_errors.pop(0)
        self.physically_closed = True

    def emit(
        self,
        *,
        frames: int,
        callback_monotonic: float,
        adc_time: float | None = None,
        current_time: float | None = None,
        status: object = "",
        value: float = 1.0,
        channels: int = 1,
    ) -> None:
        if self.callback is None:
            raise RuntimeError("handle has not been opened")
        adc = callback_monotonic if adc_time is None else adc_time
        current = callback_monotonic if current_time is None else current_time
        data = np.full((frames, channels), value, dtype=np.float32)
        self.callback(
            data,
            frames,
            {
                "inputBufferAdcTime": adc,
                "currentTime": current,
            },
            status,
        )


class BlockingCloseInputHandle(FakeInputHandle):
    """Lets shutdown tests hold the backend close call deterministically."""

    def __init__(self, *, input_latency: float = 0.01) -> None:
        super().__init__(input_latency=input_latency)
        self.close_started = threading.Event()
        self.release_close = threading.Event()

    def close(self) -> None:
        if self.physically_closed:
            raise AssertionError("close called after physical close")
        self.calls.append("close")
        self.close_started.set()
        self.release_close.wait()
        self.physically_closed = True


class FakeAudioBackend:
    def __init__(self, handle: FakeInputHandle | None = None) -> None:
        self.handle = handle or FakeInputHandle()
        self.open_calls: list[OpenCall] = []
        self.open_attempts = 0
        self.force_terminate_calls = 0
        self.open_error: Exception | None = None

    def open_input(
        self,
        resolved: ResolvedInputDevice,
        *,
        block_frames: int,
        callback: Callable[..., None],
    ) -> FakeInputHandle:
        self.open_attempts += 1
        if self.open_error is not None:
            raise self.open_error
        self.open_calls.append(OpenCall(resolved, block_frames, callback))
        self.handle.callback = callback
        return self.handle

    def force_terminate(self) -> None:
        self.force_terminate_calls += 1


class BlockingOpenAudioBackend(FakeAudioBackend):
    def __init__(self, handle: FakeInputHandle | None = None) -> None:
        super().__init__(handle)
        self.open_started = threading.Event()
        self.release_open = threading.Event()

    def open_input(
        self,
        resolved: ResolvedInputDevice,
        *,
        block_frames: int,
        callback: Callable[..., None],
    ) -> FakeInputHandle:
        self.open_attempts += 1
        self.open_calls.append(OpenCall(resolved, block_frames, callback))
        self.open_started.set()
        assert self.release_open.wait(2.0)
        if self.open_error is not None:
            raise self.open_error
        self.handle.callback = callback
        return self.handle


class FakeSoundDeviceModule:
    """Records InputStream kwargs and intentionally has no query API."""

    def __init__(self, handle: FakeInputHandle | None = None) -> None:
        self.handle = handle or FakeInputHandle()
        self.kwargs: dict[str, object] | None = None
        self.terminate_calls = 0

    def InputStream(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.handle.callback = kwargs["callback"]
        return self.handle

    def _terminate(self) -> None:
        self.terminate_calls += 1
