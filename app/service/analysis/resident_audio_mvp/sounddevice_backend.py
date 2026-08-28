"""Deadline-aware adapter for one exact sounddevice input resolution."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .contracts import (
    MAX_BOUNDED_TEXT_CHARS,
    AudioInputCallback,
    AudioInputFaultCallback,
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseResult,
    BackendCloseStatus,
    BackendFault,
    ResidentError,
    ResidentErrorCode,
    ResidentStreamSpec,
)


def _safe_message(exc: Exception) -> str:
    try:
        message = str(exc)
    except Exception:
        message = "<message unavailable>"
    return f"{type(exc).__name__}: {message}"[:MAX_BOUNDED_TEXT_CHARS]


def _not_confirmed(summary: str) -> BackendCloseResult:
    return BackendCloseResult(
        BackendCloseStatus.NOT_CONFIRMED,
        ResidentError(
            ResidentErrorCode.NON_CLEAN,
            summary[:MAX_BOUNDED_TEXT_CHARS],
        ),
    )


@dataclass(slots=True)
class _RestartRequest:
    request_id: int
    deadline_monotonic_ns: int
    completion: threading.Event = field(default_factory=threading.Event)
    result: bool = False
    timed_out: bool = False


class _NativeLifecycleState(str, Enum):
    CALL_RUNNING = "CALL_RUNNING"
    CALL_TIMED_OUT = "CALL_TIMED_OUT"
    CLOSE_RETRYABLE = "CLOSE_RETRYABLE"
    CLOSED = "CLOSED"


class _SoundDeviceInputHandle:
    def __init__(
        self,
        sounddevice_module: object,
        spec: ResidentStreamSpec,
        callback: AudioInputCallback,
        on_fault: AudioInputFaultCallback,
    ) -> None:
        self._sounddevice = sounddevice_module
        self._spec = spec
        self._callback = callback
        self._on_fault = on_fault
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._start_done = threading.Event()
        self._control_done = threading.Event()
        self._lifecycle_settled = threading.Event()
        self._lifecycle_settled.set()
        self._thread: threading.Thread | None = None
        self._native: object | None = None
        self._start_error: Exception | None = None
        self._start_timed_out = False
        self._start_failure_stage: str | None = None
        self._close_result: BackendCloseResult | None = None
        self._close_attempt_result: BackendCloseResult | None = None
        self._close_waiter: threading.Event | None = None
        self._stop_requested = False
        self._close_requested = False
        self._fault_reported = False
        self._fault_delivery_error: ResidentError | None = None
        self._restart_request: _RestartRequest | None = None
        self._next_restart_request_id = 1
        self._native_call_active: str | None = None
        self._lifecycle_state: _NativeLifecycleState | None = None
        self._retired = False
        self._callback_priority_applied = False

    def _now_ns(self) -> int:
        return time.monotonic_ns()

    def start(self, deadline_monotonic_ns: int) -> None:
        if (
            type(deadline_monotonic_ns) is not int
            or deadline_monotonic_ns <= self._now_ns()
        ):
            raise ValueError("start deadline must be a future integer")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("sounddevice input handle is single-use")
            if (
                self._retired
                or self._close_result is not None
                or self._stop_requested
                or self._close_requested
            ):
                raise RuntimeError(
                    "sounddevice input handle is retired or closed"
                )
            thread = threading.Thread(
                name=f"resident-sounddevice-{self._spec.hardware_id}",
                target=self._control,
                daemon=True,
            )
            self._thread = thread
            self._begin_native_call_locked("open")
            thread.start()
        remaining = max(
            0.0,
            (deadline_monotonic_ns - self._now_ns()) / 1_000_000_000,
        )
        if not self._start_done.wait(remaining):
            with self._lock:
                self._start_timed_out = True
                self._start_failure_stage = (
                    "open" if self._native is None else "start"
                )
                self._retired = True
                self._stop_requested = True
                self._close_requested = True
                self._mark_native_call_timed_out_locked()
                self._wake.set()
            raise TimeoutError(
                f"sounddevice start deadline expired for {self._spec.hardware_id}"
            )
        with self._lock:
            error = self._start_error
        if error is not None:
            raise error

    @property
    def start_failure_stage(self) -> str | None:
        with self._lock:
            return self._start_failure_stage

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            self._wake.set()

    def restart_native_once(self, deadline_monotonic_ns: int) -> bool:
        if type(deadline_monotonic_ns) is not int:
            return False
        try:
            now_ns = self._now_ns()
        except Exception:
            return False
        if deadline_monotonic_ns <= now_ns:
            return False
        with self._lock:
            if (
                self._thread is None
                or self._native is None
                or self._start_error is not None
                or self._stop_requested
                or self._close_requested
                or self._fault_reported
                or self._retired
                or self._restart_request is not None
                or self._control_done.is_set()
            ):
                return False
            request = _RestartRequest(
                self._next_restart_request_id,
                deadline_monotonic_ns,
            )
            self._next_restart_request_id += 1
            self._restart_request = request
            self._wake.set()
        try:
            remaining = max(
                0.0,
                (deadline_monotonic_ns - self._now_ns())
                / 1_000_000_000,
            )
        except Exception:
            return False
        if not request.completion.wait(remaining):
            with self._lock:
                if request.completion.is_set():
                    return request.result
                request.timed_out = True
                self._retired = True
                self._mark_native_call_timed_out_locked()
            return False
        with self._lock:
            return request.result

    def close(self, deadline_monotonic_ns: int) -> BackendCloseResult:
        if type(deadline_monotonic_ns) is not int:
            raise ValueError("close deadline must be an integer")
        with self._lock:
            if (
                self._close_result is not None
                and self._close_result.status is BackendCloseStatus.CLOSED
            ):
                return self._close_result
            if self._thread is None:
                self._retired = True
                self._close_requested = True
                self._close_result = BackendCloseResult(
                    BackendCloseStatus.CLOSED
                )
                self._lifecycle_state = _NativeLifecycleState.CLOSED
                self._lifecycle_settled.set()
                self._control_done.set()
                return self._close_result
            self._retired = True
            self._stop_requested = True
            waiter = self._close_waiter
            if waiter is None or waiter.is_set():
                waiter = threading.Event()
                self._close_waiter = waiter
                self._close_attempt_result = None
                self._close_requested = True
            self._wake.set()
        remaining = max(
            0.0,
            (deadline_monotonic_ns - self._now_ns()) / 1_000_000_000,
        )
        if not waiter.wait(remaining):
            with self._lock:
                self._mark_native_call_timed_out_locked()
            return _not_confirmed(
                f"sounddevice close {self._spec.hardware_id} not confirmed"
            )
        with self._lock:
            return self._close_result or self._close_attempt_result or _not_confirmed(
                f"sounddevice close {self._spec.hardware_id} missing result"
            )

    @property
    def lifecycle_state(self) -> _NativeLifecycleState | None:
        with self._lock:
            return self._lifecycle_state

    def wait_for_lifecycle_settle(self, timeout_seconds: float) -> bool:
        return self._lifecycle_settled.wait(timeout_seconds)

    @property
    def fault_delivery_error(self) -> ResidentError | None:
        return self._fault_delivery_error

    def _begin_native_call_locked(self, operation: str) -> None:
        self._native_call_active = operation
        self._lifecycle_state = _NativeLifecycleState.CALL_RUNNING
        self._lifecycle_settled.clear()

    def _begin_native_call(self, operation: str) -> None:
        with self._lock:
            self._begin_native_call_locked(operation)

    def _mark_native_call_timed_out_locked(self) -> None:
        if self._native_call_active is not None:
            self._lifecycle_state = _NativeLifecycleState.CALL_TIMED_OUT
            self._lifecycle_settled.clear()

    def _finish_native_call(
        self,
        settled_state: _NativeLifecycleState | None,
    ) -> bool:
        with self._lock:
            timed_out = (
                self._lifecycle_state
                is _NativeLifecycleState.CALL_TIMED_OUT
            )
            self._native_call_active = None
            self._lifecycle_state = settled_state
            self._lifecycle_settled.set()
            return timed_out

    def _finish_cleanup_stop_call(self) -> bool:
        with self._lock:
            timed_out = (
                self._lifecycle_state
                is _NativeLifecycleState.CALL_TIMED_OUT
            )
            self._native_call_active = None
            self._lifecycle_state = _NativeLifecycleState.CLOSE_RETRYABLE
            if timed_out:
                self._close_requested = False
                self._close_attempt_result = _not_confirmed(
                    f"sounddevice stop {self._spec.hardware_id} timed out"
                )
                waiter = self._close_waiter
                if waiter is not None:
                    waiter.set()
            self._lifecycle_settled.set()
            return timed_out

    def _control(self) -> None:
        native: object | None = None
        try:
            native = self._sounddevice.InputStream(
                device=self._spec.device_index,
                samplerate=self._spec.sample_rate_hz,
                channels=self._spec.input_channel_count,
                dtype=self._spec.sample_format,
                blocksize=self._spec.frames_per_buffer,
                latency="high",
                callback=self._sounddevice_callback,
            )
        except Exception as exc:
            with self._lock:
                self._start_error = exc
                self._start_failure_stage = "open"
                self._native = native
                self._native_call_active = None
            self._start_done.set()
            if native is None:
                with self._lock:
                    self._close_result = BackendCloseResult(
                        BackendCloseStatus.CLOSED
                    )
                    self._lifecycle_state = _NativeLifecycleState.CLOSED
                    self._lifecycle_settled.set()
                self._control_done.set()
                return
        else:
            with self._lock:
                self._native = native
                skip_start = self._start_timed_out or self._close_requested
            self._finish_native_call(
                _NativeLifecycleState.CLOSE_RETRYABLE
                if skip_start
                else None
            )
            if not skip_start:
                self._begin_native_call("start")
                try:
                    native.start()
                except Exception as exc:
                    with self._lock:
                        self._start_error = exc
                        self._start_failure_stage = "start"
                    self._finish_native_call(
                        _NativeLifecycleState.CLOSE_RETRYABLE
                    )
                else:
                    self._finish_native_call(None)
            self._start_done.set()
        assert native is not None

        stopped = False
        while True:
            with self._lock:
                immediate = self._stop_requested or self._close_requested
            if not immediate:
                self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    stop_requested = self._stop_requested
                    close_requested = self._close_requested
                    fault_reported = self._fault_reported
                    request = self._restart_request
                    if (
                        request is not None
                        and (stop_requested or close_requested or fault_reported)
                    ):
                        self._restart_request = None
                        request.result = False
                        request.completion.set()
                        request = None
                if stop_requested and not stopped:
                    self._begin_native_call("stop")
                    try:
                        native.stop()
                    except Exception as exc:
                        stop_timed_out = (
                            self._finish_cleanup_stop_call()
                        )
                        self._report_fault(
                            BackendCallbackStatus.BACKEND_FAULT,
                            f"sounddevice stop {_safe_message(exc)}",
                        )
                    else:
                        stop_timed_out = (
                            self._finish_cleanup_stop_call()
                        )
                        stopped = True
                    with self._lock:
                        self._stop_requested = False
                    if stop_timed_out:
                        break
                if close_requested:
                    self._begin_native_call("close")
                    try:
                        native.close()
                    except Exception as exc:
                        result = _not_confirmed(
                            f"sounddevice close {_safe_message(exc)}"
                        )
                        closed_evidence = self._native_reports_closed(native)
                    else:
                        result = BackendCloseResult(
                            BackendCloseStatus.CLOSED
                        )
                        closed_evidence = True
                    if closed_evidence:
                        result = BackendCloseResult(BackendCloseStatus.CLOSED)
                        settled_state = _NativeLifecycleState.CLOSED
                    else:
                        settled_state = _NativeLifecycleState.CLOSE_RETRYABLE
                    self._finish_native_call(settled_state)
                    with self._lock:
                        self._close_requested = False
                        self._close_attempt_result = result
                        waiter = self._close_waiter
                        if settled_state is _NativeLifecycleState.CLOSED:
                            self._close_result = result
                        if waiter is not None:
                            waiter.set()
                    if settled_state is _NativeLifecycleState.CLOSED:
                        self._control_done.set()
                        return
                    break
                if stop_requested or fault_reported:
                    break
                if request is None:
                    break
                restart_stopped = self._execute_restart(native, request)
                if restart_stopped is not None:
                    stopped = restart_stopped

    def _execute_restart(
        self,
        native: object,
        request: _RestartRequest,
    ) -> bool | None:
        try:
            if request.deadline_monotonic_ns <= self._now_ns():
                self._complete_restart(request, False)
                return None
            self._begin_native_call("restart-stop")
            native.stop()
        except Exception as exc:
            self._finish_native_call(
                _NativeLifecycleState.CLOSE_RETRYABLE
            )
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"sounddevice restart stop {_safe_message(exc)}",
            )
            self._complete_restart(request, False)
            return None
        stop_timed_out = self._finish_native_call(
            _NativeLifecycleState.CLOSE_RETRYABLE
        )
        with self._lock:
            try:
                live = (
                    not self._stop_requested
                    and not self._close_requested
                    and not self._fault_reported
                    and not self._retired
                    and not request.timed_out
                    and not stop_timed_out
                    and request.deadline_monotonic_ns > self._now_ns()
                )
            except Exception:
                live = False
        if not live:
            self._complete_restart(request, False)
            return True
        restart_error: Exception | None = None
        self._before_native_restart()
        with self._lock:
            try:
                start_authorized = (
                    self._restart_request is request
                    and not request.timed_out
                    and not self._retired
                    and not self._stop_requested
                    and not self._close_requested
                    and not self._fault_reported
                    and request.deadline_monotonic_ns > self._now_ns()
                )
            except Exception:
                start_authorized = False
            if start_authorized:
                self._begin_native_call_locked("restart-start")
        if not start_authorized:
            self._complete_restart(request, False)
            return True
        try:
            native.start()
        except Exception as exc:
            restart_error = exc
        start_timed_out = self._finish_native_call(
            _NativeLifecycleState.CLOSE_RETRYABLE
        )
        restarted = not start_timed_out and restart_error is None
        if restart_error is not None:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"sounddevice restart start {_safe_message(restart_error)}",
            )
        self._complete_restart(request, restarted)
        if restarted:
            with self._lock:
                self._lifecycle_state = None
                self._lifecycle_settled.set()
        return not restarted

    def _before_native_restart(self) -> None:
        pass

    def _complete_restart(
        self,
        request: _RestartRequest,
        result: bool,
    ) -> None:
        with self._lock:
            if self._restart_request is request:
                self._restart_request = None
            request.result = result
            request.completion.set()

    @staticmethod
    def _native_reports_closed(native: object) -> bool:
        try:
            return (
                getattr(native, "closed", None) is True
                and getattr(native, "active", None) is False
            )
        except Exception:
            return False

    def _sounddevice_callback(
        self,
        samples: np.ndarray,
        frame_count: int,
        time_info: object,
        native_status: object,
    ) -> None:
        directive = self._native_callback(
            samples,
            frame_count,
            time_info,
            native_status,
        )
        if directive is BackendCallbackDirective.STOP:
            self._retired = True
            self._stop_requested = True
            self._close_requested = True
            self._wake.set()
            raise self._sounddevice.CallbackStop

    def _native_callback(
        self,
        samples: np.ndarray,
        frame_count: int,
        _time_info: object,
        native_status: object,
    ) -> BackendCallbackDirective:
        entry_ns = time.monotonic_ns()
        try:
            status = self._map_status(native_status)
        except Exception as exc:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"sounddevice status {_safe_message(exc)}",
            )
            return BackendCallbackDirective.STOP
        try:
            directive = self._callback(
                samples,
                frame_count,
                entry_ns,
                status,
            )
        except Exception as exc:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"resident callback {_safe_message(exc)}",
            )
            return BackendCallbackDirective.STOP
        if status is not BackendCallbackStatus.OK:
            try:
                status_summary = str(native_status)
            except Exception as exc:
                status_summary = f"status {_safe_message(exc)}"
            self._report_fault(status, status_summary)
            return BackendCallbackDirective.STOP
        if type(directive) is not BackendCallbackDirective:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                "resident callback returned an invalid directive",
            )
            return BackendCallbackDirective.STOP
        return directive

    @staticmethod
    def _map_status(native_status: object) -> BackendCallbackStatus:
        if type(native_status) is BackendCallbackStatus:
            return native_status
        if not native_status:
            return BackendCallbackStatus.OK
        if bool(getattr(native_status, "input_overflow", False)):
            return BackendCallbackStatus.INPUT_OVERFLOW
        return BackendCallbackStatus.BACKEND_FAULT

    def _report_fault(
        self,
        status: BackendCallbackStatus,
        summary: str,
    ) -> None:
        if self._fault_reported:
            return
        self._fault_reported = True
        self._retired = True
        text = summary if type(summary) is str and summary else status.value
        try:
            self._on_fault(
                BackendFault(status, text[:MAX_BOUNDED_TEXT_CHARS])
            )
        except Exception as exc:
            self._fault_delivery_error = ResidentError(
                ResidentErrorCode.NON_CLEAN,
                (
                    f"sounddevice on_fault {_safe_message(exc)}"
                )[:MAX_BOUNDED_TEXT_CHARS],
            )


class SoundDeviceAudioBackend:
    """Construct single-use deadline-aware handles without device discovery."""

    def __init__(self, sounddevice_module: object | None = None) -> None:
        if sounddevice_module is None:
            import sounddevice as sounddevice_module

        if not callable(getattr(sounddevice_module, "InputStream", None)):
            raise TypeError("sounddevice module must provide InputStream")
        self._sounddevice = sounddevice_module

    def create_input(
        self,
        spec: ResidentStreamSpec,
        callback: AudioInputCallback,
        on_fault: AudioInputFaultCallback,
    ) -> _SoundDeviceInputHandle:
        if type(spec) is not ResidentStreamSpec:
            raise TypeError("spec must be an exact ResidentStreamSpec")
        if not callable(callback) or not callable(on_fault):
            raise TypeError("callback and on_fault must be callable")
        return _SoundDeviceInputHandle(
            self._sounddevice,
            spec,
            callback,
            on_fault,
        )


__all__ = ["SoundDeviceAudioBackend"]
