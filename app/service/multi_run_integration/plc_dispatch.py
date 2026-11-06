from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from typing import Any

from PyQt6.QtCore import QThread

from app.service.plc_result_sequencer import PlcResultBoundaryError, PlcScalar


class _DispatchCommand:
    """One bounded request whose completion acknowledges the physical write."""

    def __init__(
        self,
        *,
        plc_id: int,
        payload: Mapping[str, PlcScalar],
    ) -> None:
        self.plc_id = plc_id
        self.payload = payload
        self._event = threading.Event()
        self.error: PlcResultBoundaryError | None = None

    def execute(self, service: object) -> None:
        try:
            service.write_result(self.plc_id, self.payload)
        except PlcResultBoundaryError as exc:
            self.error = exc
        except Exception as exc:
            self.error = PlcResultBoundaryError(
                "result_dispatch",
                str(exc) or type(exc).__name__,
            )
        finally:
            self._event.set()

    def wait(self, timeout_seconds: float) -> bool:
        return self._event.wait(timeout_seconds)


class PlcDispatchPorts:
    """Acknowledged result writes targeted to the owning PLC Qt thread."""

    def __init__(
        self,
        *,
        plc_services: Mapping[int, Any],
        plc_threads: Mapping[int, Any],
        dispatch_timeout_seconds: float = 1.0,
    ) -> None:
        if not isinstance(plc_services, Mapping):
            raise ValueError("plc_services must be a mapping")
        if not isinstance(plc_threads, Mapping):
            raise ValueError("plc_threads must be a mapping")
        if (
            isinstance(dispatch_timeout_seconds, bool)
            or not isinstance(dispatch_timeout_seconds, (int, float))
            or not math.isfinite(float(dispatch_timeout_seconds))
            or dispatch_timeout_seconds <= 0
        ):
            raise ValueError("dispatch_timeout_seconds must be positive and finite")
        self._plc_services = plc_services
        self._plc_threads = plc_threads
        self._dispatch_timeout_seconds = float(dispatch_timeout_seconds)

    def write_result(
        self,
        plc_id: int,
        payload: Mapping[str, PlcScalar],
    ) -> None:
        if isinstance(plc_id, bool) or not isinstance(plc_id, int) or plc_id <= 0:
            raise PlcResultBoundaryError(
                "plc_id",
                "plc_id must be a positive integer",
            )
        if not isinstance(payload, Mapping):
            raise PlcResultBoundaryError(
                "result_dispatch",
                "PLC result payload must be a mapping",
            )
        try:
            service = self._plc_services.get(plc_id)
            thread = self._plc_threads.get(plc_id)
        except Exception:
            raise PlcResultBoundaryError(
                "result_dispatch",
                "PLC target lookup failed",
            ) from None
        if service is None or thread is None:
            raise PlcResultBoundaryError(
                "result_dispatch",
                "PLC target is unavailable",
            )
        try:
            running = thread.isRunning()
        except Exception:
            raise PlcResultBoundaryError(
                "result_dispatch",
                "PLC target thread state is unavailable",
            ) from None
        if running is not True:
            raise PlcResultBoundaryError(
                "result_dispatch",
                "PLC target thread is not running",
            )

        command = _DispatchCommand(plc_id=plc_id, payload=payload)
        if QThread.currentThread() is thread:
            command.execute(service)
        else:
            signal = getattr(service, "plc_dispatch_requested", None)
            emit = getattr(signal, "emit", None)
            if not callable(emit):
                raise PlcResultBoundaryError(
                    "result_dispatch",
                    "PLC target dispatch is unavailable",
                )
            try:
                emit(command)
            except Exception:
                raise PlcResultBoundaryError(
                    "result_dispatch",
                    "PLC target dispatch was rejected",
                ) from None
            if not command.wait(self._dispatch_timeout_seconds):
                raise PlcResultBoundaryError(
                    "result_dispatch",
                    "PLC target dispatch did not complete promptly",
                )
        if command.error is not None:
            raise command.error
