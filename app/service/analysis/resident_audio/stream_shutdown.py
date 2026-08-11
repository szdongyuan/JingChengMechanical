"""Logical and physical shutdown control for one resident stream."""

from __future__ import annotations

import threading
import time

from .contracts import StreamState
from .stream_control import StreamCommand, StreamCommandIndeterminate


class ResidentStreamShutdownMixin:
    def control_begin_shutdown(self):
        """Deactivate and wake this generation without touching its handle."""

        with self._shutdown_begin_lock:
            if self._shutdown_begin_result is not None:
                in_flight, settled, command = self._shutdown_begin_result
                if in_flight and settled.is_set():
                    self._shutdown_begin_result = (
                        False,
                        settled,
                        command,
                    )
                self._wake.set()
                return self._shutdown_begin_result
            with self._state_lock:
                self._start_consumed = True
            startup_in_flight, startup_settled = (
                self._startup.request_cancel(self._generation.deactivate)
            )
            if startup_in_flight:
                with self._state_lock:
                    if self._state is StreamState.OPENING:
                        self._state = StreamState.CLOSING
            command = None
            worker = self._worker
            if not startup_in_flight and worker is not None:
                command = StreamCommand("close")
                command.ack = threading.Event()
                if not self._command_admission.submit(
                    command, self._commands, self._wake
                ):
                    command = None
            self._wake.set()
            self._shutdown_begin_result = (
                startup_in_flight,
                startup_settled,
                command,
            )
            return self._shutdown_begin_result

    def control_require_shutdown_close_authorization(self) -> None:
        """Reserve physical teardown for the pool's global step-six path."""

        with self._shutdown_begin_lock:
            self._shutdown_control_close_required = True
            self._shutdown_control_close_authorized = False

    def control_authorize_shutdown_close(self) -> None:
        """Authorize physical teardown after the pool-wide step-five barrier."""

        with self._shutdown_begin_lock:
            if self._shutdown_control_close_required:
                self._shutdown_control_close_authorized = True

    def control_revoke_shutdown_close_authorization(self) -> None:
        """Retain failed teardown for a later bounded management retry."""

        with self._shutdown_begin_lock:
            if self._shutdown_control_close_required:
                self._shutdown_control_close_authorized = False

    def _shutdown_control_close_permitted(
        self,
        control_path: bool,
        *,
        close_lease=None,
        begin_physical: bool = False,
    ) -> bool:
        with self._shutdown_begin_lock:
            stream_permits = (
                not self._shutdown_control_close_required
                or (
                    control_path
                    and self._shutdown_control_close_authorized
                )
            )
        pool_gate = self._physical_close_gate
        return stream_permits and (
            pool_gate is None
            or pool_gate.permits(
                self,
                control_path=control_path,
                close_lease=close_lease,
                begin_physical=begin_physical,
            )
        )

    def control_cleanup_canceled_startup_handle(self) -> bool | None:
        """Clean standalone cancellation, but defer pool-owned shutdown."""

        with self._shutdown_begin_lock:
            pool_managed = self._shutdown_control_close_required
        if pool_managed:
            return None
        return self.control_close_handle()

    def close(
        self,
        timeout: float | None = 2.0,
        *,
        _control_path: bool = False,
        _close_lease=None,
    ) -> bool:
        owned_lease = None
        pool_gate = self._physical_close_gate
        if pool_gate is not None and _close_lease is None:
            owned_lease = pool_gate.acquire(
                self, control_path=_control_path
            )
            if owned_lease is None:
                return False
            _close_lease = owned_lease
        try:
            return self._close_with_lease(
                timeout,
                control_path=_control_path,
                close_lease=_close_lease,
            )
        finally:
            if owned_lease is not None:
                owned_lease.release()

    def _close_with_lease(
        self,
        timeout: float | None,
        *,
        control_path: bool,
        close_lease,
    ) -> bool:
        deadline = (
            None
            if timeout is None
            else time.monotonic() + max(0.0, float(timeout))
        )
        (
            startup_in_flight,
            startup_settled,
            close_command,
        ) = self.control_begin_shutdown()
        if startup_in_flight:
            if not startup_settled.wait(_remaining(deadline)):
                return False
            cleanup_clean = self._startup_cleanup_clean
            with self._handle_lock:
                cleanup_incomplete = (
                    self._tracked_cleanup_handle is not None
                    or (self._handle is not None and not self._handle_closed)
                )
            if cleanup_clean is not None:
                if cleanup_incomplete:
                    return False
                with self._state_lock:
                    self._state = StreamState.CLOSED
                return cleanup_clean

        if not self._shutdown_control_close_permitted(
            control_path, close_lease=close_lease
        ):
            return False
        worker = self._worker
        command_error: Exception | None = None
        if (
            close_command is not None
            and not close_command.ack.wait(_remaining(deadline))
        ):
            command_error = StreamCommandIndeterminate(close_command)
        cleanup_clean = self.control_close_handle(
            _control_path=control_path,
            _close_lease=close_lease,
        )
        if worker is not None:
            worker.join(_remaining(deadline))
        worker_stopped = worker is None or not worker.is_alive()
        with self._handle_lock:
            physically_closed = (
                self._tracked_cleanup_handle is None
                and (self._handle is None or self._handle_closed)
            )
        with self._state_lock:
            if worker_stopped and physically_closed:
                self._state = StreamState.CLOSED
        if command_error is not None:
            raise command_error
        return worker_stopped and cleanup_clean

    def control_close_handle(
        self, *, _control_path: bool = False, _close_lease=None
    ) -> bool:
        """Abort/stop/close once; intended for the pool control path."""

        owned_lease = None
        pool_gate = self._physical_close_gate
        if pool_gate is not None and _close_lease is None:
            owned_lease = pool_gate.acquire(
                self, control_path=_control_path
            )
            if owned_lease is None:
                return False
            _close_lease = owned_lease
        try:
            return self._control_close_handle_with_lease(
                control_path=_control_path,
                close_lease=_close_lease,
            )
        finally:
            if owned_lease is not None:
                owned_lease.release()

    def _control_close_handle_with_lease(
        self, *, control_path: bool, close_lease
    ) -> bool:
        with self._handle_close_lock:
            if not self._shutdown_control_close_permitted(
                control_path,
                close_lease=close_lease,
                begin_physical=True,
            ):
                return False
            if self._generation.alive:
                self._generation.deactivate()
            with self._handle_lock:
                if self._handle is not None and not self._handle_closed:
                    handle = self._handle
                    late_cleanup = False
                elif self._tracked_cleanup_handle is not None:
                    handle = self._tracked_cleanup_handle
                    late_cleanup = True
                else:
                    return True
                self._handle_close_attempted = True
            return self._close_handle_locked(handle, late_cleanup)

    @property
    def tracked_cleanup_handle(self):
        with self._handle_lock:
            return self._tracked_cleanup_handle

    def track_late_handle(self, handle) -> None:
        """Transfer one late-open handle without performing physical teardown."""

        with self._handle_close_lock:
            with self._handle_lock:
                if (
                    self._tracked_cleanup_handle is not None
                    or (self._handle is not None and not self._handle_closed)
                ):
                    raise RuntimeError(
                        "stream already owns a handle requiring cleanup"
                    )
                self._tracked_cleanup_handle = handle
                self._handle_close_attempted = False

    def _close_handle_locked(self, handle, late_cleanup: bool) -> bool:
        with self._handle_lock:
            abort_already_succeeded = (
                late_cleanup and self._handle_abort_succeeded is True
            )
            stop_already_succeeded = (
                late_cleanup and self._handle_stop_succeeded is True
            )
        abort_succeeded = (
            True
            if abort_already_succeeded
            else _attempt_handle_operation(handle.abort)
        )
        stop_succeeded = (
            True
            if stop_already_succeeded
            else _attempt_handle_operation(handle.stop)
        )
        close_succeeded = _attempt_handle_operation(handle.close)
        with self._handle_lock:
            self._handle_abort_succeeded = abort_succeeded
            self._handle_stop_succeeded = stop_succeeded
            if late_cleanup:
                if close_succeeded:
                    self._tracked_cleanup_handle = None
            else:
                self._handle_closed = close_succeeded
        return abort_succeeded and stop_succeeded and close_succeeded


def _attempt_handle_operation(operation) -> bool:
    try:
        operation()
    except Exception:
        return False
    return True


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


__all__ = ["ResidentStreamShutdownMixin"]
