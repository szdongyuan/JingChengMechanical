"""Bounded native-control state tests for unresolved handles."""

from __future__ import annotations

import threading
import time

import pytest

from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCloseStatus,
)
from app.service.analysis.resident_audio_mvp.sounddevice_backend import (
    SoundDeviceAudioBackend,
    _NativeLifecycleState,
)
from tests.resident_audio_mvp.test_sounddevice_backend import (
    FakeSoundDevice,
    NativeStream,
    stream_spec,
)


def make_handle(module: FakeSoundDevice):
    return SoundDeviceAudioBackend(module).create_input(
        stream_spec(),
        lambda *_args: BackendCallbackDirective.CONTINUE,
        lambda _fault: None,
    )


def test_close_before_start_is_terminal_without_constructing_native() -> None:
    class ConstructionMustNotRun(FakeSoundDevice):
        def InputStream(self, **kwargs: object) -> NativeStream:
            self.input_stream_calls += 1
            raise AssertionError("retired handle attempted native construction")

    module = ConstructionMustNotRun()
    handle = make_handle(module)

    first = handle.close(time.monotonic_ns() + 1_000_000_000)
    repeated = handle.close(time.monotonic_ns() + 1_000_000_000)

    assert first.status is BackendCloseStatus.CLOSED
    assert repeated is first
    with pytest.raises(RuntimeError, match="retired|closed"):
        handle.start(time.monotonic_ns() + 1_000_000_000)
    assert module.input_stream_calls == 0
    assert module.native.calls == []


def test_timed_out_restart_gets_no_second_command_or_late_start() -> None:
    stop_entered = threading.Event()
    stop_release = threading.Event()

    class BlockingStopStream(NativeStream):
        def stop(self) -> None:
            self.calls.append("stop")
            stop_entered.set()
            stop_release.wait()

    module = FakeSoundDevice()
    module.native = BlockingStopStream()
    handle = make_handle(module)
    handle.start(time.monotonic_ns() + 1_000_000_000)

    results: list[bool] = []
    request = threading.Thread(
        target=lambda: results.append(
            handle.restart_native_once(time.monotonic_ns() + 30_000_000)
        )
    )
    request.start()
    assert stop_entered.wait(1)
    assert handle.lifecycle_state is _NativeLifecycleState.CALL_RUNNING
    request.join(1)
    assert results == [False]
    assert handle.lifecycle_state is _NativeLifecycleState.CALL_TIMED_OUT
    assert not handle.restart_native_once(time.monotonic_ns() + 1_000_000_000)
    assert module.native.calls == ["start", "stop"]

    stop_release.set()
    assert handle.wait_for_lifecycle_settle(1)
    assert handle.lifecycle_state is _NativeLifecycleState.CLOSE_RETRYABLE
    assert module.native.calls == ["start", "stop"]
    result = handle.close(time.monotonic_ns() + 1_000_000_000)
    assert result.status is BackendCloseStatus.CLOSED
    assert module.native.calls == ["start", "stop", "close"]


def test_timed_out_close_is_observed_without_a_duplicate_close_command() -> None:
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
    handle = make_handle(module)
    handle.start(time.monotonic_ns() + 1_000_000_000)

    first = handle.close(time.monotonic_ns() + 30_000_000)
    second = handle.close(time.monotonic_ns() + 30_000_000)

    assert first.status is second.status is BackendCloseStatus.NOT_CONFIRMED
    assert handle.lifecycle_state is _NativeLifecycleState.CALL_TIMED_OUT
    assert module.native.calls == ["start", "stop", "close"]
    close_release.set()
    assert module.native.closed.wait(1)
    assert handle.wait_for_lifecycle_settle(1)
    assert handle.lifecycle_state is _NativeLifecycleState.CLOSED
    confirmed = handle.close(time.monotonic_ns() + 1_000_000_000)
    assert confirmed.status is BackendCloseStatus.CLOSED
    assert module.native.calls == ["start", "stop", "close"]


def test_close_stop_timeout_requires_fresh_cleanup_before_native_close() -> None:
    stop_entered = threading.Event()
    stop_release = threading.Event()
    close_called = threading.Event()

    class BlockingStopStream(NativeStream):
        def stop(self) -> None:
            self.calls.append("stop")
            stop_entered.set()
            stop_release.wait()

        def close(self) -> None:
            self.calls.append("close")
            close_called.set()
            self.closed.set()

    module = FakeSoundDevice()
    module.native = BlockingStopStream()
    handle = make_handle(module)
    handle.start(time.monotonic_ns() + 1_000_000_000)

    first = handle.close(time.monotonic_ns() + 30_000_000)

    assert first.status is BackendCloseStatus.NOT_CONFIRMED
    assert stop_entered.is_set()
    assert handle.lifecycle_state is _NativeLifecycleState.CALL_TIMED_OUT
    assert module.native.calls == ["start", "stop"]
    while_running = handle.close(time.monotonic_ns() + 30_000_000)
    assert while_running.status is BackendCloseStatus.NOT_CONFIRMED
    assert handle.lifecycle_state is _NativeLifecycleState.CALL_TIMED_OUT
    assert module.native.calls == ["start", "stop"]
    stop_release.set()
    assert handle.wait_for_lifecycle_settle(1)
    assert not close_called.wait(0.05)
    assert handle.lifecycle_state is _NativeLifecycleState.CLOSE_RETRYABLE
    assert module.native.calls == ["start", "stop"]

    second = handle.close(time.monotonic_ns() + 1_000_000_000)

    assert second.status is BackendCloseStatus.CLOSED
    assert close_called.is_set()
    assert handle.lifecycle_state is _NativeLifecycleState.CLOSED
    assert module.native.calls == ["start", "stop", "close"]


def test_close_exception_is_retryable_on_the_same_control_worker() -> None:
    class RetryCloseStream(NativeStream):
        def __init__(self) -> None:
            super().__init__()
            self.thread_ids: list[int] = []

        def close(self) -> None:
            self.calls.append("close")
            self.thread_ids.append(threading.get_ident())
            if self.calls.count("close") == 1:
                raise OSError("temporary close failure")
            self.closed.set()

    module = FakeSoundDevice()
    module.native = RetryCloseStream()
    handle = make_handle(module)
    handle.start(time.monotonic_ns() + 1_000_000_000)

    first = handle.close(time.monotonic_ns() + 1_000_000_000)
    assert first.status is BackendCloseStatus.NOT_CONFIRMED
    assert handle.lifecycle_state is _NativeLifecycleState.CLOSE_RETRYABLE
    second = handle.close(time.monotonic_ns() + 1_000_000_000)

    assert second.status is BackendCloseStatus.CLOSED
    assert handle.lifecycle_state is _NativeLifecycleState.CLOSED
    assert module.native.calls == ["start", "stop", "close", "close"]
    assert len(set(module.native.thread_ids)) == 1


@pytest.mark.parametrize(
    ("first_stop_raises", "stop_times_out", "expected_calls"),
    (
        (
            True,
            False,
            ["start", "stop", "close", "stop", "close"],
        ),
        (
            False,
            False,
            ["start", "stop", "close", "close"],
        ),
        (True, True, ["start", "stop", "stop", "close"]),
        (False, True, ["start", "stop", "close"]),
    ),
    ids=(
        "failed-stop-is-retried",
        "successful-stop-is-not-repeated",
        "failed-timed-out-stop-is-retried",
        "successful-timed-out-stop-is-kept",
    ),
)
def test_unconfirmed_close_preserves_stop_retry_eligibility(
    first_stop_raises: bool,
    stop_times_out: bool,
    expected_calls: list[str],
) -> None:
    stop_entered = threading.Event()
    stop_release = threading.Event()

    class StopCloseMatrixStream(NativeStream):
        def __init__(self) -> None:
            super().__init__()
            self.active = False

        def start(self) -> None:
            super().start()
            self.active = True

        def stop(self) -> None:
            super().stop()
            if self.calls.count("stop") == 1:
                if stop_times_out:
                    stop_entered.set()
                    stop_release.wait()
                if first_stop_raises:
                    raise OSError("stop rejected")
            self.active = False

        def close(self) -> None:
            self.calls.append("close")
            if not stop_times_out and self.calls.count("close") == 1:
                if self.active:
                    raise OSError("stream is still active")
                raise OSError("close was not confirmed")
            if self.active:
                raise OSError("stream is still active")
            self.closed.set()

    module = FakeSoundDevice()
    module.native = StopCloseMatrixStream()
    handle = make_handle(module)
    handle.start(time.monotonic_ns() + 1_000_000_000)

    try:
        first = handle.close(
            time.monotonic_ns()
            + (30_000_000 if stop_times_out else 1_000_000_000)
        )
        if stop_times_out:
            assert stop_entered.is_set()
            assert (
                handle.lifecycle_state
                is _NativeLifecycleState.CALL_TIMED_OUT
            )
            stop_release.set()
            assert handle.wait_for_lifecycle_settle(1)
        second = handle.close(time.monotonic_ns() + 1_000_000_000)

        assert first.status is BackendCloseStatus.NOT_CONFIRMED
        assert second.status is BackendCloseStatus.CLOSED
        assert module.native.calls == expected_calls
    finally:
        stop_release.set()
        if handle.lifecycle_state is not _NativeLifecycleState.CLOSED:
            module.native.active = False
            handle.close(time.monotonic_ns() + 1_000_000_000)
