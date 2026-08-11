"""One generation-gated resident input slot."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .contracts import (
    MAX_BOUNDED_TEXT_CHARS,
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendFault,
    AudioInputCallback,
    AudioInputFaultCallback,
    ResidentError,
    ResidentErrorCode,
    ResidentStreamSpec,
)
from .ring import _ResidentRing


def _safe_detail(value: object) -> str:
    try:
        text = str(value)
    except Exception:
        text = f"<{type(value).__name__} message unavailable>"
    return text[:MAX_BOUNDED_TEXT_CHARS]


def _fault_error(
    *,
    phase: str,
    hardware_id: str,
    detail: str,
) -> ResidentError:
    bounded_detail = detail[:MAX_BOUNDED_TEXT_CHARS]
    summary = f"{phase} {hardware_id}: {bounded_detail}"
    return ResidentError(ResidentErrorCode.FAULT, summary[:MAX_BOUNDED_TEXT_CHARS])


@dataclass(slots=True)
class _DrainOwner:
    worker_id: str
    work_event: threading.Event
    control_event: threading.Event
    stop_event: threading.Event
    thread: threading.Thread

    @classmethod
    def create(
        cls,
        hardware_id: str,
        work_event: threading.Event,
        control_event: threading.Event,
        progress: Callable[[str], bool],
    ) -> _DrainOwner:
        stop_event = threading.Event()

        def drain() -> None:
            while True:
                control_event.wait()
                control_event.clear()
                work_event.clear()
                if stop_event.is_set():
                    return
                if progress(hardware_id):
                    work_event.set()
                    control_event.set()

        thread = threading.Thread(
            name=f"resident-audio-{hardware_id}",
            target=drain,
            daemon=True,
        )
        owner = cls(
            worker_id=f"stream:{hardware_id}",
            work_event=work_event,
            control_event=control_event,
            stop_event=stop_event,
            thread=thread,
        )
        thread.start()
        return owner


class _ResidentStreamSlot:
    __slots__ = (
        "_accepting",
        "_close_attempted",
        "_close_result",
        "_control_event",
        "_decision_lock",
        "_drain_owner",
        "_dropped_callback_count",
        "_fault",
        "_fault_monotonic_ns",
        "_first_block_event",
        "_first_block_seen",
        "_first_block_monotonic_ns",
        "_generation",
        "_handle",
        "_lock",
        "_monotonic_ns",
        "_ring",
        "_spec",
        "_startup_close_owned",
        "_state",
        "_work_event",
    )

    def __init__(
        self,
        spec: ResidentStreamSpec,
        *,
        ring_capacity_frames: int,
        monotonic_ns: Callable[[], int],
        decision_lock: object | None = None,
    ) -> None:
        self._spec = spec
        self._lock = threading.Lock()
        self._decision_lock = decision_lock
        self._monotonic_ns = monotonic_ns
        self._ring = _ResidentRing(
            ring_capacity_frames,
            len(spec.capture_channel_indices),
        )
        configure_timeline = getattr(self._ring, "configure_timeline", None)
        if configure_timeline is not None:
            configure_timeline(
                sample_rate_hz=spec.sample_rate_hz,
                frames_per_buffer=spec.frames_per_buffer,
            )
        self._generation = object()
        self._first_block_event = threading.Event()
        self._work_event: threading.Event | None = None
        self._control_event: threading.Event | None = None
        self._drain_owner: _DrainOwner | None = None
        self._handle = None
        self._state = "OPENING"
        self._accepting = True
        self._first_block_seen = False
        self._first_block_monotonic_ns: int | None = None
        self._fault: ResidentError | None = None
        self._fault_monotonic_ns: int | None = None
        self._close_attempted = False
        self._close_result = None
        self._startup_close_owned = True
        self._dropped_callback_count = 0

    def backend_entrypoints(
        self,
    ) -> tuple[AudioInputCallback, AudioInputFaultCallback]:
        generation = self._generation

        def callback(
            samples: np.ndarray,
            frame_count: int,
            callback_monotonic_ns: int,
            status: BackendCallbackStatus,
        ) -> BackendCallbackDirective:
            return self._callback_for_generation(
                generation,
                samples,
                frame_count,
                callback_monotonic_ns,
                status,
            )

        def on_fault(fault: BackendFault) -> None:
            self._on_fault_for_generation(generation, fault)

        return callback, on_fault

    def publish_drain_owner(
        self,
        progress: Callable[[str], bool] | None = None,
    ) -> None:
        work_event = threading.Event()
        control_event = threading.Event()
        drain_owner = _DrainOwner.create(
            self._spec.hardware_id,
            work_event,
            control_event,
            progress if progress is not None else lambda _hardware_id: False,
        )
        self._work_event = work_event
        self._control_event = control_event
        self._drain_owner = drain_owner

    def _callback_for_generation(
        self,
        generation: object,
        samples: np.ndarray,
        frame_count: int,
        callback_monotonic_ns: int,
        status: BackendCallbackStatus,
    ) -> BackendCallbackDirective:
        decision_lock = self._decision_lock
        if decision_lock is None:
            return self._callback_under_decision_lock(
                generation,
                samples,
                frame_count,
                callback_monotonic_ns,
                status,
            )
        with decision_lock:
            return self._callback_under_decision_lock(
                generation,
                samples,
                frame_count,
                callback_monotonic_ns,
                status,
            )

    def _callback_under_decision_lock(
        self,
        generation: object,
        samples: np.ndarray,
        frame_count: int,
        callback_monotonic_ns: int,
        status: BackendCallbackStatus,
    ) -> BackendCallbackDirective:
        with self._lock:
            if generation is not self._generation or not self._accepting:
                self._record_stale_entry_locked()
                return BackendCallbackDirective.STOP
            trusted_monotonic_ns = self._monotonic_ns()
            try:
                if type(status) is not BackendCallbackStatus:
                    raise ValueError("invalid callback status")
                if status is not BackendCallbackStatus.OK:
                    self._latch_fault_locked(
                        _fault_error(
                            phase="callback status",
                            hardware_id=self._spec.hardware_id,
                            detail=status.value,
                        ),
                        trusted_monotonic_ns,
                    )
                    return BackendCallbackDirective.STOP
                if (
                    not isinstance(samples, np.ndarray)
                    or samples.dtype != np.dtype(np.float32)
                    or samples.ndim != 2
                    or not samples.flags.c_contiguous
                    or type(frame_count) is not int
                    or frame_count < 1
                    or frame_count > self._spec.frames_per_buffer
                    or samples.shape
                    != (frame_count, self._spec.input_channel_count)
                    or type(callback_monotonic_ns) is not int
                    or callback_monotonic_ns < 0
                    or callback_monotonic_ns > trusted_monotonic_ns
                ):
                    raise ValueError("invalid callback audio metadata")
                selected = np.ascontiguousarray(
                    samples[:, self._spec.capture_channel_indices],
                    dtype=np.float32,
                )
                self._ring.append(
                    selected,
                    callback_monotonic_ns=callback_monotonic_ns,
                )
                self._first_block_seen = True
                self._first_block_monotonic_ns = callback_monotonic_ns
                self._first_block_event.set()
                work_event = self._work_event
                control_event = self._control_event
                if work_event is None or control_event is None:
                    raise RuntimeError("drain owner is not published")
                work_event.set()
                control_event.set()
                return BackendCallbackDirective.CONTINUE
            except Exception as exc:
                self._latch_fault_locked(
                    _fault_error(
                        phase="callback",
                        hardware_id=self._spec.hardware_id,
                        detail=f"{type(exc).__name__}: {_safe_detail(exc)}",
                    ),
                    trusted_monotonic_ns,
                )
                return BackendCallbackDirective.STOP

    def _on_fault_for_generation(
        self,
        generation: object,
        fault: BackendFault,
    ) -> None:
        decision_lock = self._decision_lock
        if decision_lock is None:
            self._on_fault_under_decision_lock(generation, fault)
            return
        with decision_lock:
            self._on_fault_under_decision_lock(generation, fault)

    def _on_fault_under_decision_lock(
        self,
        generation: object,
        fault: BackendFault,
    ) -> None:
        with self._lock:
            if generation is not self._generation or not self._accepting:
                self._record_stale_entry_locked()
                return
            trusted_monotonic_ns = self._monotonic_ns()
            if not isinstance(fault, BackendFault):
                error = _fault_error(
                    phase="backend fault",
                    hardware_id=self._spec.hardware_id,
                    detail="invalid BackendFault",
                )
            else:
                error = _fault_error(
                    phase="backend fault",
                    hardware_id=self._spec.hardware_id,
                    detail=f"{fault.status.value}: {fault.summary}",
                )
            self._latch_fault_locked(error, trusted_monotonic_ns)

    def _record_stale_entry_locked(self) -> None:
        self._dropped_callback_count = min(
            self._dropped_callback_count + 1,
            (1 << 63) - 1,
        )

    def _latch_fault_locked(
        self,
        error: ResidentError,
        fault_monotonic_ns: int,
    ) -> None:
        if self._fault is not None:
            return
        self._fault = error
        self._fault_monotonic_ns = fault_monotonic_ns
        self._accepting = False
        self._generation = None
        self._state = "FAULTED"
        self._first_block_event.set()
        if self._control_event is not None:
            self._control_event.set()

    def latch_owner_fault(
        self,
        error: ResidentError,
    ) -> None:
        with self._lock:
            self._latch_fault_locked(error, self._monotonic_ns())

    def finish_start(
        self,
        deadline_monotonic_ns: int,
        owner_monotonic_ns: int,
    ) -> tuple[bool, ResidentError | None]:
        with self._lock:
            if (
                self._fault is None
                and self._first_block_seen
                and self._first_block_monotonic_ns is not None
                and self._first_block_monotonic_ns <= deadline_monotonic_ns
                and owner_monotonic_ns <= deadline_monotonic_ns
            ):
                self._state = "READY"
                return True, None
            if self._fault is None:
                self._latch_fault_locked(
                    _fault_error(
                        phase="first block",
                        hardware_id=self._spec.hardware_id,
                        detail="deadline expired",
                    ),
                    owner_monotonic_ns,
                )
            return False, self._fault

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._state == "READY"

    def map_trigger(self, trigger_monotonic_ns: int):
        with self._lock:
            return self._ring.map_trigger(trigger_monotonic_ns)

    def ring_bounds(self) -> tuple[int, int]:
        with self._lock:
            return self._ring.oldest_cursor, self._ring.write_cursor

    def copy_into(
        self,
        *,
        start_cursor: int,
        destination: np.ndarray,
        destination_start: int,
        frame_count: int,
    ) -> None:
        with self._lock:
            self._ring.copy_into(
                start_cursor,
                destination,
                destination_start,
                frame_count,
            )

    def signal_work(self) -> None:
        work_event = self._work_event
        control_event = self._control_event
        if work_event is not None and control_event is not None:
            work_event.set()
            control_event.set()

    def fault_details_under_decision_lock(
        self,
    ) -> tuple[ResidentError | None, int | None]:
        return self._fault, self._fault_monotonic_ns

    def invalidate_generation_under_decision_lock(self, state: str) -> None:
        self._accepting = False
        self._generation = None
        self._state = state

    def claim_close_attempt_under_decision_lock(self) -> tuple[object, bool]:
        if (
            self._startup_close_owned
            or self._close_attempted
            or self._handle is None
        ):
            return self._handle, False
        self._close_attempted = True
        return self._handle, True

    def resolve_startup_close_under_decision_lock(
        self,
        *,
        failed: bool,
    ) -> tuple[object, bool]:
        self._startup_close_owned = False
        if not failed or self._close_attempted or self._handle is None:
            return self._handle, False
        self._close_attempted = True
        return self._handle, True

    def record_close_result_from_control_owner(self, result: object) -> None:
        self._close_result = result

    def close_result_snapshot(self) -> object:
        return self._close_result

    def stop_worker(self) -> None:
        owner = self._drain_owner
        if owner is None:
            return
        owner.stop_event.set()
        owner.control_event.set()

    def join_worker(self, timeout_seconds: float) -> bool:
        owner = self._drain_owner
        if owner is None:
            return True
        owner.thread.join(timeout_seconds)
        return not owner.thread.is_alive()

    def worker_id(self) -> str | None:
        owner = self._drain_owner
        return None if owner is None else owner.worker_id
