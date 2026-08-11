"""Hardware-free tests for the deadline-aware sounddevice adapter."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseStatus,
    BackendFault,
    ResidentStreamSpec,
)
from app.service.analysis.resident_audio_mvp.sounddevice_backend import (
    SoundDeviceAudioBackend,
)


def stream_spec() -> ResidentStreamSpec:
    return ResidentStreamSpec(
        hardware_id="mic-1",
        device_index=7,
        device_name="Resolved microphone",
        hostapi_index=3,
        hostapi_name="ASIO",
        sample_rate_hz=48_000,
        input_channel_count=4,
        capture_channel_indices=(0, 2),
        sample_format="float32",
        frames_per_buffer=2_400,
    )


class NativeStream:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = threading.Event()

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")
        self.closed.set()


class FakeCallbackStop(Exception):
    pass


class FakeSoundDevice:
    CallbackStop = FakeCallbackStop

    def __init__(
        self,
        *,
        construction_entered: threading.Event | None = None,
        construction_release: threading.Event | None = None,
    ) -> None:
        self.native = NativeStream()
        self.kwargs: dict[str, object] | None = None
        self.input_stream_calls = 0
        self.construction_entered = construction_entered
        self.construction_release = construction_release

    def InputStream(self, **kwargs: object) -> NativeStream:
        self.input_stream_calls += 1
        self.kwargs = dict(kwargs)
        if self.construction_entered is not None:
            self.construction_entered.set()
        if self.construction_release is not None:
            self.construction_release.wait()
        return self.native

    def invoke_callback(
        self,
        samples: np.ndarray,
        frame_count: int,
        time_info: object,
        status: object,
    ) -> object:
        assert self.kwargs is not None
        callback = self.kwargs["callback"]
        assert callable(callback)
        return callback(samples, frame_count, time_info, status)


class NativeStatus:
    def __init__(self, *, input_overflow: bool = False) -> None:
        self.input_overflow = input_overflow

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return "input overflow" if self.input_overflow else "device unavailable"


def test_create_is_local_and_start_uses_only_exact_frozen_resolution() -> None:
    module = FakeSoundDevice()
    backend = SoundDeviceAudioBackend(module)
    callbacks: list[tuple[object, int, int, BackendCallbackStatus]] = []
    faults: list[BackendFault] = []
    handle = backend.create_input(
        stream_spec(),
        lambda *args: (
            callbacks.append(args),
            BackendCallbackDirective.CONTINUE,
        )[1],
        faults.append,
    )
    assert module.input_stream_calls == 0

    handle.start(time.monotonic_ns() + 1_000_000_000)

    assert module.input_stream_calls == 1
    assert module.kwargs is not None
    assert {
        key: value
        for key, value in module.kwargs.items()
        if key != "callback"
    } == {
        "device": 7,
        "samplerate": 48_000,
        "channels": 4,
        "dtype": "float32",
        "blocksize": 2_400,
    }
    assert module.native.calls == ["start"]
    assert not hasattr(module, "query_devices")
    assert not hasattr(module, "query_hostapis")
    assert faults == []

    samples = np.ones((8, 4), dtype=np.float32)
    before = time.monotonic_ns()
    directive = module.invoke_callback(samples, 8, object(), False)
    after = time.monotonic_ns()
    assert directive is None
    assert callbacks[0][0] is samples
    assert callbacks[0][1] == 8
    assert before <= callbacks[0][2] <= after
    assert callbacks[0][3] is BackendCallbackStatus.OK

    handle.request_stop()
    handle.request_stop()
    result = handle.close(time.monotonic_ns() + 1_000_000_000)
    assert result.status is BackendCloseStatus.CLOSED
    assert module.native.calls == ["start", "stop", "close"]


def test_native_callback_raises_library_stop_for_delegated_ok_stop() -> None:
    module = FakeSoundDevice()
    backend = SoundDeviceAudioBackend(module)
    faults: list[BackendFault] = []
    handle = backend.create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.STOP,
        faults.append,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)

    with pytest.raises(module.CallbackStop):
        module.invoke_callback(
            np.zeros((4, 4), dtype=np.float32),
            4,
            object(),
            False,
        )

    assert faults == []
    handle.close(time.monotonic_ns() + 1_000_000_000)


def test_native_status_maps_to_fatal_backend_value_and_reports_once() -> None:
    module = FakeSoundDevice()
    backend = SoundDeviceAudioBackend(module)
    statuses: list[BackendCallbackStatus] = []
    faults: list[BackendFault] = []

    def callback(
        _samples: np.ndarray,
        _frames: int,
        _entry_ns: int,
        status: BackendCallbackStatus,
    ) -> BackendCallbackDirective:
        statuses.append(status)
        return BackendCallbackDirective.CONTINUE

    handle = backend.create_input(stream_spec(), callback, faults.append)
    handle.start(time.monotonic_ns() + 1_000_000_000)
    samples = np.zeros((4, 4), dtype=np.float32)

    with pytest.raises(module.CallbackStop):
        module.invoke_callback(
            samples,
            4,
            object(),
            NativeStatus(input_overflow=True),
        )
    assert statuses == [BackendCallbackStatus.INPUT_OVERFLOW]
    assert len(faults) == 1
    assert faults[0].status is BackendCallbackStatus.INPUT_OVERFLOW
    with pytest.raises(module.CallbackStop):
        module.invoke_callback(samples, 4, object(), NativeStatus())
    assert statuses[-1] is BackendCallbackStatus.BACKEND_FAULT
    assert len(faults) == 1
    handle.close(time.monotonic_ns() + 1_000_000_000)


def test_blocked_native_construction_times_out_and_late_open_closes_once() -> None:
    construction_entered = threading.Event()
    construction_release = threading.Event()
    module = FakeSoundDevice(
        construction_entered=construction_entered,
        construction_release=construction_release,
    )
    backend = SoundDeviceAudioBackend(module)
    handle = backend.create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )
    deadline = time.monotonic_ns() + 50_000_000

    with pytest.raises(TimeoutError):
        handle.start(deadline)

    assert construction_entered.is_set()
    assert time.monotonic_ns() < deadline + 100_000_000
    construction_release.set()
    assert module.native.closed.wait(1)
    assert module.native.calls == ["start", "stop", "close"]
    result = handle.close(time.monotonic_ns() + 1_000_000_000)
    assert result.status is BackendCloseStatus.CLOSED
    assert module.native.calls.count("close") == 1


def test_close_returns_not_confirmed_when_native_close_misses_deadline() -> None:
    close_entered = threading.Event()
    close_release = threading.Event()

    class BlockingCloseStream(NativeStream):
        def close(self) -> None:
            self.calls.append("close")
            close_entered.set()
            close_release.wait()
            self.closed.set()

    module = FakeSoundDevice()
    module.native = BlockingCloseStream()
    backend = SoundDeviceAudioBackend(module)
    handle = backend.create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)

    result = handle.close(time.monotonic_ns() + 50_000_000)

    assert close_entered.is_set()
    assert result.status is BackendCloseStatus.NOT_CONFIRMED
    assert result.error is not None
    close_release.set()
    assert module.native.closed.wait(1)


def test_native_start_exception_keeps_handle_owned_until_one_close_attempt() -> None:
    class StartFailingStream(NativeStream):
        def start(self) -> None:
            self.calls.append("start")
            raise OSError("native start rejected")

    module = FakeSoundDevice()
    module.native = StartFailingStream()
    backend = SoundDeviceAudioBackend(module)
    handle = backend.create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )

    with pytest.raises(OSError, match="native start rejected"):
        handle.start(time.monotonic_ns() + 1_000_000_000)
    handle.request_stop()
    result = handle.close(time.monotonic_ns() + 1_000_000_000)

    assert result.status is BackendCloseStatus.CLOSED
    assert module.native.calls == ["start", "stop", "close"]


def test_throwing_native_status_is_contained_as_one_backend_fault() -> None:
    class ThrowingStatus:
        def __bool__(self) -> bool:
            raise RuntimeError("status conversion failed")

    module = FakeSoundDevice()
    faults: list[BackendFault] = []
    backend = SoundDeviceAudioBackend(module)
    handle = backend.create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        faults.append,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)
    with pytest.raises(module.CallbackStop):
        module.invoke_callback(
            np.zeros((4, 4), dtype=np.float32),
            4,
            object(),
            ThrowingStatus(),
        )
    assert len(faults) == 1
    assert faults[0].status is BackendCallbackStatus.BACKEND_FAULT
    handle.close(time.monotonic_ns() + 1_000_000_000)


def test_restart_native_once_reuses_stream_and_control_thread() -> None:
    class ThreadRecordingStream(NativeStream):
        def __init__(self) -> None:
            super().__init__()
            self.control_thread_ids = [threading.get_ident()]

        def start(self) -> None:
            self.control_thread_ids.append(threading.get_ident())
            super().start()

        def stop(self) -> None:
            self.control_thread_ids.append(threading.get_ident())
            super().stop()

    class ConstructingSoundDevice(FakeSoundDevice):
        def __init__(self) -> None:
            super().__init__()
            self.native = None

        def InputStream(self, **kwargs: object) -> ThreadRecordingStream:
            self.input_stream_calls += 1
            self.kwargs = dict(kwargs)
            self.native = ThreadRecordingStream()
            return self.native

    module = ConstructingSoundDevice()
    handle = SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )

    handle.start(time.monotonic_ns() + 1_000_000_000)
    assert handle.restart_native_once(
        time.monotonic_ns() + 1_000_000_000
    ) is True

    assert module.native is not None
    assert module.native.calls == ["start", "stop", "start"]
    assert module.input_stream_calls == 1
    assert len(set(module.native.control_thread_ids)) == 1
    handle.close(time.monotonic_ns() + 1_000_000_000)


def test_restart_native_once_rejects_expired_closed_and_faulted_handles() -> None:
    module = FakeSoundDevice()
    faults: list[BackendFault] = []
    handle = SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        faults.append,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)

    assert handle.restart_native_once(time.monotonic_ns()) is False
    handle._report_fault(BackendCallbackStatus.BACKEND_FAULT, "fault")
    assert handle.restart_native_once(
        time.monotonic_ns() + 1_000_000_000
    ) is False
    assert module.native.calls == ["start"]
    assert len(faults) == 1
    handle.close(time.monotonic_ns() + 1_000_000_000)
    assert handle.restart_native_once(
        time.monotonic_ns() + 1_000_000_000
    ) is False


def test_restart_native_once_close_wins_while_native_stop_is_blocked() -> None:
    stop_entered = threading.Event()
    stop_release = threading.Event()

    class BlockingStopStream(NativeStream):
        def stop(self) -> None:
            super().stop()
            stop_entered.set()
            stop_release.wait()

    module = FakeSoundDevice()
    module.native = BlockingStopStream()
    handle = SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)
    restart_result: list[bool] = []
    restart = threading.Thread(
        target=lambda: restart_result.append(
            handle.restart_native_once(
                time.monotonic_ns() + 1_000_000_000
            )
        )
    )
    restart.start()
    assert stop_entered.wait(1)
    close_result = []
    close = threading.Thread(
        target=lambda: close_result.append(
            handle.close(time.monotonic_ns() + 1_000_000_000)
        )
    )
    close.start()
    stop_release.set()
    restart.join(1)
    close.join(1)

    assert restart_result == [False]
    assert close_result[0].status is BackendCloseStatus.CLOSED
    assert module.native.calls == ["start", "stop", "close"]


def test_restart_native_once_fault_wins_while_native_stop_is_blocked() -> None:
    stop_entered = threading.Event()
    stop_release = threading.Event()

    class BlockingStopStream(NativeStream):
        def stop(self) -> None:
            super().stop()
            stop_entered.set()
            stop_release.wait()

    module = FakeSoundDevice()
    module.native = BlockingStopStream()
    faults: list[BackendFault] = []
    handle = SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        faults.append,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)
    restart_result: list[bool] = []
    restart = threading.Thread(
        target=lambda: restart_result.append(
            handle.restart_native_once(
                time.monotonic_ns() + 1_000_000_000
            )
        )
    )
    restart.start()
    assert stop_entered.wait(1)
    handle._report_fault(BackendCallbackStatus.BACKEND_FAULT, "fault")
    stop_release.set()
    restart.join(1)

    assert restart_result == [False]
    assert module.native.calls == ["start", "stop"]
    assert len(faults) == 1
    handle.close(time.monotonic_ns() + 1_000_000_000)


@pytest.mark.parametrize("failing_operation", ("stop", "start"))
def test_restart_native_once_exception_reports_one_fault(
    failing_operation: str,
) -> None:
    class RestartFailingStream(NativeStream):
        def start(self) -> None:
            super().start()
            if (
                failing_operation == "start"
                and self.calls.count("start") == 2
            ):
                raise OSError("restart rejected")

        def stop(self) -> None:
            super().stop()
            if failing_operation == "stop":
                raise OSError("restart rejected")

    module = FakeSoundDevice()
    module.native = RestartFailingStream()
    faults: list[BackendFault] = []
    handle = SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        faults.append,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)

    assert handle.restart_native_once(
        time.monotonic_ns() + 1_000_000_000
    ) is False
    assert handle.restart_native_once(
        time.monotonic_ns() + 1_000_000_000
    ) is False

    expected_calls = (
        ["start", "stop", "start"]
        if failing_operation == "start"
        else ["start", "stop"]
    )
    assert module.native.calls == expected_calls
    assert len(faults) == 1
    assert faults[0].status is BackendCallbackStatus.BACKEND_FAULT
    handle.close(time.monotonic_ns() + 1_000_000_000)


def test_restart_native_once_late_request_cannot_complete_later_request() -> None:
    first_stop_entered = threading.Event()
    first_stop_release = threading.Event()
    second_stop_entered = threading.Event()
    second_stop_release = threading.Event()

    class SequencedStopStream(NativeStream):
        def stop(self) -> None:
            super().stop()
            stop_count = self.calls.count("stop")
            if stop_count == 1:
                first_stop_entered.set()
                first_stop_release.wait()
            elif stop_count == 2:
                second_stop_entered.set()
                second_stop_release.wait()

    module = FakeSoundDevice()
    module.native = SequencedStopStream()
    handle = SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )
    handle.start(time.monotonic_ns() + 1_000_000_000)
    results: dict[str, bool] = {}
    request_a = threading.Thread(
        target=lambda: results.__setitem__(
            "a",
            handle.restart_native_once(time.monotonic_ns() + 50_000_000),
        )
    )
    request_a.start()
    assert first_stop_entered.wait(1)
    request_a.join(1)
    assert results == {"a": False}

    request_b = threading.Thread(
        target=lambda: results.__setitem__(
            "b",
            handle.restart_native_once(
                time.monotonic_ns() + 1_000_000_000
            ),
        )
    )
    request_b.start()
    first_stop_release.set()
    assert second_stop_entered.wait(1)
    assert request_b.is_alive()
    second_stop_release.set()
    request_b.join(1)

    assert results == {"a": False, "b": True}
    assert module.native.calls == ["start", "stop", "stop", "start"]
    handle.close(time.monotonic_ns() + 1_000_000_000)
    assert module.native.calls == [
        "start",
        "stop",
        "stop",
        "start",
        "stop",
        "close",
    ]
