"""Single-use startup handoff between ResidentStream.start and close."""

from __future__ import annotations

import math
import threading

from .backend import production_block_frames
from .contracts import CaptureErrorCode, StreamState
from .ring import TimestampedBlockRing
from .stream_types import StreamFailureEvent
from .timeline import StreamTimeline


class StartupHandoff:
    """Coordinates one blocking open with concurrent cancellation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settled = threading.Event()
        self._settled.set()
        self._in_flight = False
        self._cancel_requested = False

    def begin(self) -> None:
        with self._lock:
            if self._in_flight:
                raise RuntimeError("startup is already in flight")
            self._cancel_requested = False
            self._in_flight = True
            self._settled.clear()

    def request_cancel(self, deactivate) -> tuple[bool, threading.Event]:
        with self._lock:
            deactivate()
            was_in_flight = self._in_flight
            if was_in_flight:
                self._cancel_requested = True
            return was_in_flight, self._settled

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def claim_open_result(self) -> bool:
        with self._lock:
            return self._in_flight and not self._cancel_requested

    def finish_success(self, start_worker) -> bool:
        """Start the worker and settle atomically against close cancellation."""

        with self._lock:
            if not self._in_flight or self._cancel_requested:
                return False
            start_worker()
            self._in_flight = False
            self._settled.set()
            return True

    def settle(self) -> None:
        with self._lock:
            self._in_flight = False
            self._settled.set()


class ResidentStreamStartupMixin:
    def start(self) -> None:
        with self._state_lock:
            if self._start_consumed:
                raise RuntimeError(
                    "ResidentStream instances are single-use; "
                    "create a new generation to reopen"
                )
            self._start_consumed = True
            self._state = StreamState.OPENING
            self._opened_at = float(self._clock())
            self._startup.begin()

        handle = None
        installed = False
        successful = False
        try:
            handle = self._backend.open_input(
                self._resolved,
                block_frames=production_block_frames(
                    self._resolved.sample_rate
                ),
                callback=self._callback,
            )
            if not self._startup.claim_open_result():
                self.track_late_handle(handle)
                self._startup_cleanup_clean = (
                    self.control_cleanup_canceled_startup_handle()
                )
                return

            with self._handle_lock:
                self._handle = handle
                self._handle_closed = False
                self._handle_close_attempted = False
                self._handle_abort_succeeded = None
                self._handle_stop_succeeded = None
            installed = True
            if self._cancel_startup_if_requested():
                return

            latency = _normalized_latency(handle.input_latency)
            self._timeline = StreamTimeline(
                sample_rate=self._resolved.sample_rate,
                input_latency=latency,
                monotonic_clock=self._clock,
            )
            self._ring = TimestampedBlockRing(
                pool_epoch=self._pool_epoch,
                hardware_id=self._resolved.hardware_id,
                stream_generation=self._generation_number,
                sample_rate=self._resolved.sample_rate,
                timeline=self._timeline,
            )
            if self._cancel_startup_if_requested():
                return

            handle.start()
            if self._cancel_startup_if_requested():
                return

            self._running = True
            self._worker = threading.Thread(
                target=self._ingest_loop,
                name=(
                    f"resident-ingest-{self._resolved.hardware_id}-"
                    f"{self._generation_number}"
                ),
                daemon=True,
            )
            if not self._startup.finish_success(
                lambda: self._command_admission.start_worker(self._worker)
            ):
                self._running = False
                self._startup_cleanup_clean = (
                    self.control_cleanup_canceled_startup_handle()
                )
                return
            successful = True
        except Exception as error:
            self._running = False
            if self._startup.cancellation_requested:
                if installed:
                    self._startup_cleanup_clean = (
                        self.control_cleanup_canceled_startup_handle()
                    )
                elif handle is not None:
                    self.track_late_handle(handle)
                    self._startup_cleanup_clean = (
                        self.control_cleanup_canceled_startup_handle()
                    )
                else:
                    self._startup_cleanup_clean = True
                return
            self._fail_start(error)
        finally:
            if not successful:
                self._startup.settle()

    def _cancel_startup_if_requested(self) -> bool:
        if not self._startup.cancellation_requested:
            return False
        self._running = False
        self._startup_cleanup_clean = (
            self.control_cleanup_canceled_startup_handle()
        )
        return True

    def _fail_start(self, error: Exception) -> None:
        self._generation.deactivate()
        self._running = False
        with self._state_lock:
            self._state = StreamState.FAILED
            self._failure_code = CaptureErrorCode.STREAM_OPEN_FAILED
        self.control_close_handle()
        # Startup resources have settled before potentially blocking external
        # notification. close() may now complete independently of the sink.
        self._startup.settle()
        try:
            self._failure_sink(
                StreamFailureEvent(
                    hardware_id=self._resolved.hardware_id,
                    pool_epoch=self._pool_epoch,
                    stream_generation=self._generation_number,
                    error_code=CaptureErrorCode.STREAM_OPEN_FAILED,
                    message=f"input stream open/start failed: {error}",
                    retry_delay=self._retry.next_delay(),
                    exception_type=type(error).__name__,
                )
            )
        except Exception:
            pass


def _normalized_latency(value: object) -> float:
    try:
        latency = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return latency if math.isfinite(latency) and latency >= 0 else 0.0


__all__ = ["ResidentStreamStartupMixin", "StartupHandoff"]
