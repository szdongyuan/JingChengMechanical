"""Deterministic backend fakes for the resident-audio MVP tests."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time

import numpy as np

from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseResult,
    BackendCloseStatus,
    BackendFault,
    ResidentError,
    ResidentErrorCode,
    ResidentStreamSpec,
)


class ManualMonotonicClock:
    def __init__(self, initial_ns: int = 1_000_000_000) -> None:
        self._now_ns = initial_ns

    def __call__(self) -> int:
        return self._now_ns

    def advance_ns(self, amount_ns: int) -> None:
        self._now_ns += amount_ns

    def advance_seconds(self, amount_s: float) -> None:
        self.advance_ns(int(amount_s * 1_000_000_000))


StartAction = Callable[["FakeInputHandle"], None]


class FakeInputHandle:
    def __init__(
        self,
        spec: ResidentStreamSpec,
        callback: Callable[
            [np.ndarray, int, int, BackendCallbackStatus],
            BackendCallbackDirective,
        ],
        on_fault: Callable[[BackendFault], None],
        *,
        clock: ManualMonotonicClock,
        start_action: StartAction | None,
        close_result: BackendCloseResult,
        request_stop_error: Exception | None = None,
        close_error: Exception | None = None,
        close_entered: threading.Event | None = None,
        close_release: threading.Event | None = None,
    ) -> None:
        self.spec = spec
        self.callback = callback
        self.on_fault = on_fault
        self.clock = clock
        self.start_action = start_action
        self.close_result = close_result
        self.request_stop_error = request_stop_error
        self.close_error = close_error
        self.close_entered = close_entered
        self.close_release = close_release
        self.start_deadlines: list[int] = []
        self.request_stop_calls = 0
        self.close_deadlines: list[int] = []
        self.callback_directives: list[BackendCallbackDirective] = []

    def start(self, deadline_monotonic_ns: int) -> None:
        self.start_deadlines.append(deadline_monotonic_ns)
        if self.start_action is not None:
            self.start_action(self)

    def request_stop(self) -> None:
        self.request_stop_calls += 1
        if self.request_stop_error is not None:
            raise self.request_stop_error

    def close(self, deadline_monotonic_ns: int) -> BackendCloseResult:
        self.close_deadlines.append(deadline_monotonic_ns)
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            remaining_seconds = max(
                0.0,
                (deadline_monotonic_ns - time.monotonic_ns())
                / 1_000_000_000,
            )
            if not self.close_release.wait(remaining_seconds):
                return BackendCloseResult(
                    BackendCloseStatus.NOT_CONFIRMED,
                    ResidentError(
                        ResidentErrorCode.NON_CLEAN,
                        "fake native close not confirmed",
                    ),
                )
        if self.close_error is not None:
            raise self.close_error
        return self.close_result

    def emit(
        self,
        samples: np.ndarray,
        *,
        frame_count: int | None = None,
        callback_monotonic_ns: int | None = None,
        status: BackendCallbackStatus = BackendCallbackStatus.OK,
    ) -> BackendCallbackDirective:
        directive = self.callback(
            samples,
            samples.shape[0] if frame_count is None else frame_count,
            self.clock() if callback_monotonic_ns is None else callback_monotonic_ns,
            status,
        )
        self.callback_directives.append(directive)
        return directive

    def emit_valid(self, frames: int | None = None) -> BackendCallbackDirective:
        frame_count = frames or self.spec.frames_per_buffer
        samples = np.arange(
            frame_count * self.spec.input_channel_count,
            dtype=np.float32,
        ).reshape(frame_count, self.spec.input_channel_count)
        return self.emit(samples)

    def emit_numbered_frames(
        self,
        *,
        first: int,
        count: int,
    ) -> None:
        remaining = count
        next_frame = first
        while remaining:
            frame_count = min(remaining, self.spec.frames_per_buffer)
            frame_numbers = np.arange(
                next_frame,
                next_frame + frame_count,
                dtype=np.float32,
            )
            samples = (
                frame_numbers[:, None]
                + np.arange(
                    self.spec.input_channel_count,
                    dtype=np.float32,
                )[None, :]
                * 1_000
            )
            self.clock.advance_ns(
                round(
                    frame_count
                    * 1_000_000_000
                    / self.spec.sample_rate_hz
                )
            )
            self.emit(samples)
            next_frame += frame_count
            remaining -= frame_count

    def emit_status(
        self, status: BackendCallbackStatus
    ) -> BackendCallbackDirective:
        samples = np.zeros(
            (self.spec.frames_per_buffer, self.spec.input_channel_count),
            dtype=np.float32,
        )
        return self.emit(samples, status=status)

    def fault(
        self,
        status: BackendCallbackStatus = BackendCallbackStatus.DEVICE_FAULT,
        summary: str = "device fault",
    ) -> None:
        self.on_fault(BackendFault(status, summary))


class FakeBackend:
    def __init__(
        self,
        *,
        clock: ManualMonotonicClock | None = None,
        start_actions: dict[str, StartAction] | None = None,
        create_errors: dict[str, Exception] | None = None,
        close_results: dict[str, BackendCloseResult] | None = None,
        request_stop_errors: dict[str, Exception] | None = None,
        close_errors: dict[str, Exception] | None = None,
        close_controls: dict[
            str, tuple[threading.Event, threading.Event]
        ] | None = None,
        emit_valid_by_default: bool = True,
    ) -> None:
        self.clock = clock or ManualMonotonicClock()
        self.start_actions = start_actions or {}
        self.create_errors = create_errors or {}
        self.close_results = close_results or {}
        self.request_stop_errors = request_stop_errors or {}
        self.close_errors = close_errors or {}
        self.close_controls = close_controls or {}
        self.emit_valid_by_default = emit_valid_by_default
        self.create_calls: list[ResidentStreamSpec] = []
        self.handles: dict[str, FakeInputHandle] = {}

    def create_input(
        self,
        spec: ResidentStreamSpec,
        callback,
        on_fault,
    ) -> FakeInputHandle:
        self.create_calls.append(spec)
        error = self.create_errors.get(spec.hardware_id)
        if error is not None:
            raise error
        action = self.start_actions.get(spec.hardware_id)
        if action is None and self.emit_valid_by_default:
            action = lambda handle: handle.emit_valid()
        close_result = self.close_results.get(
            spec.hardware_id,
            BackendCloseResult(BackendCloseStatus.CLOSED),
        )
        close_control = self.close_controls.get(spec.hardware_id)
        handle = FakeInputHandle(
            spec,
            callback,
            on_fault,
            clock=self.clock,
            start_action=action,
            close_result=close_result,
            request_stop_error=self.request_stop_errors.get(spec.hardware_id),
            close_error=self.close_errors.get(spec.hardware_id),
            close_entered=None if close_control is None else close_control[0],
            close_release=None if close_control is None else close_control[1],
        )
        self.handles[spec.hardware_id] = handle
        return handle
