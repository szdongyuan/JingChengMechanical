"""Deadline-aware adapter for one exact sounddevice input resolution."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from app.service.analysis.thread_priority import (
    THREAD_PRIORITY_HIGHEST,
    set_current_thread_priority,
)

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
        self._thread: threading.Thread | None = None
        self._native: object | None = None
        self._start_error: Exception | None = None
        self._close_result: BackendCloseResult | None = None
        self._stop_requested = False
        self._close_requested = False
        self._fault_reported = False
        self._restart_requests: deque[_RestartRequest] = deque()
        self._next_restart_request_id = 1
        self._callback_priority_applied = False

    def _now_ns(self) -> int:
        return time.monotonic_ns()

    def start(self, deadline_monotonic_ns: int) -> None:
        if (
            type(deadline_monotonic_ns) is not int
            or deadline_monotonic_ns <= time.monotonic_ns()
        ):
            raise ValueError("start deadline must be a future integer")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("sounddevice input handle is single-use")
            thread = threading.Thread(
                name=f"resident-sounddevice-{self._spec.hardware_id}",
                target=self._control,
                daemon=True,
            )
            self._thread = thread
            thread.start()
        remaining = max(
            0.0,
            (deadline_monotonic_ns - time.monotonic_ns()) / 1_000_000_000,
        )
        if not self._start_done.wait(remaining):
            with self._lock:
                self._stop_requested = True
                self._close_requested = True
                self._wake.set()
            raise TimeoutError(
                f"sounddevice start deadline expired for {self._spec.hardware_id}"
            )
        with self._lock:
            error = self._start_error
        if error is not None:
            raise error

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
                or self._control_done.is_set()
            ):
                return False
            request = _RestartRequest(
                self._next_restart_request_id,
                deadline_monotonic_ns,
            )
            self._next_restart_request_id += 1
            self._restart_requests.append(request)
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
            return False
        with self._lock:
            return request.result

    def close(self, deadline_monotonic_ns: int) -> BackendCloseResult:
        if type(deadline_monotonic_ns) is not int:
            raise ValueError("close deadline must be an integer")
        with self._lock:
            if self._close_result is not None:
                return self._close_result
            if self._thread is None:
                self._close_result = BackendCloseResult(
                    BackendCloseStatus.CLOSED
                )
                return self._close_result
            self._stop_requested = True
            self._close_requested = True
            self._wake.set()
        remaining = max(
            0.0,
            (deadline_monotonic_ns - time.monotonic_ns()) / 1_000_000_000,
        )
        if not self._control_done.wait(remaining):
            return _not_confirmed(
                f"sounddevice close {self._spec.hardware_id} not confirmed"
            )
        with self._lock:
            return self._close_result or _not_confirmed(
                f"sounddevice close {self._spec.hardware_id} missing result"
            )

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
            native.start()
        except Exception as exc:
            with self._lock:
                self._start_error = exc
                self._native = native
            self._start_done.set()
            if native is None:
                with self._lock:
                    self._close_result = BackendCloseResult(
                        BackendCloseStatus.CLOSED
                    )
                self._control_done.set()
                return
        else:
            with self._lock:
                self._native = native
            self._start_done.set()
        assert native is not None

        stopped = False
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    stop_requested = self._stop_requested
                    close_requested = self._close_requested
                    fault_reported = self._fault_reported
                    if stop_requested or close_requested or fault_reported:
                        self._complete_pending_restarts_locked()
                    request = (
                        self._restart_requests.popleft()
                        if not (
                            stop_requested
                            or close_requested
                            or fault_reported
                        )
                        and self._restart_requests
                        else None
                    )
                if stop_requested and not stopped:
                    try:
                        native.stop()
                    except Exception as exc:
                        self._report_fault(
                            BackendCallbackStatus.BACKEND_FAULT,
                            f"sounddevice stop {_safe_message(exc)}",
                        )
                    stopped = True
                if close_requested:
                    try:
                        native.close()
                    except Exception as exc:
                        result = _not_confirmed(
                            f"sounddevice close {_safe_message(exc)}"
                        )
                    else:
                        result = BackendCloseResult(
                            BackendCloseStatus.CLOSED
                        )
                    with self._lock:
                        self._complete_pending_restarts_locked()
                        self._close_result = result
                    self._control_done.set()
                    return
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
            native.stop()
        except Exception as exc:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"sounddevice restart stop {_safe_message(exc)}",
            )
            self._complete_restart(request, False)
            return None

        restart_error: Exception | None = None
        restarted = False
        with self._lock:
            try:
                live = (
                    not self._stop_requested
                    and not self._close_requested
                    and not self._fault_reported
                    and request.deadline_monotonic_ns > self._now_ns()
                )
            except Exception:
                live = False
            if live:
                try:
                    self._before_native_restart()
                    native.start()
                except Exception as exc:
                    restart_error = exc
                else:
                    restarted = True
        if restart_error is not None:
            self._report_fault(
                BackendCallbackStatus.BACKEND_FAULT,
                f"sounddevice restart start {_safe_message(restart_error)}",
            )
        self._complete_restart(request, restarted)
        return not restarted

    def _before_native_restart(self) -> None:
        pass

    def _complete_restart(
        self,
        request: _RestartRequest,
        result: bool,
    ) -> None:
        with self._lock:
            request.result = result
            request.completion.set()

    def _complete_pending_restarts_locked(self) -> None:
        while self._restart_requests:
            request = self._restart_requests.popleft()
            request.result = False
            request.completion.set()

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
            raise self._sounddevice.CallbackStop

    def _native_callback(
        self,
        samples: np.ndarray,
        frame_count: int,
        _time_info: object,
        native_status: object,
    ) -> BackendCallbackDirective:
        if not self._callback_priority_applied:
            set_current_thread_priority(THREAD_PRIORITY_HIGHEST)
            self._callback_priority_applied = True
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
        with self._lock:
            if self._fault_reported:
                return
            self._fault_reported = True
        text = summary if type(summary) is str and summary else status.value
        try:
            self._on_fault(
                BackendFault(status, text[:MAX_BOUNDED_TEXT_CHARS])
            )
        except Exception:
            pass


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
