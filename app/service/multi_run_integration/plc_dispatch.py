from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from typing import Any

from app.service.plc_result_sequencer import PlcResultBoundaryError, PlcScalar


_RESULT_PAYLOAD_TYPES: dict[str, type] = {
    "finished": bool,
    "error_code": int,
    "sn_code": str,
    "test_result": bool,
    "score": float,
    "spl": float,
    "loudness": float,
    "vibrate": float,
    "ai_score": float,
}


def validate_plc_id(plc_id: object) -> int:
    if type(plc_id) is not int or plc_id <= 0:
        raise PlcResultBoundaryError(
            "plc_id",
            "plc_id must be a positive non-bool integer",
        )
    return plc_id


def validate_plc_result_payload(
    payload: object,
) -> dict[str, PlcScalar]:
    if not isinstance(payload, Mapping):
        raise PlcResultBoundaryError(
            "payload",
            "PLC result payload must be a mapping",
        )
    try:
        exact_keys = set(payload)
    except Exception:
        raise PlcResultBoundaryError(
            "payload",
            "PLC result payload keys are invalid",
        ) from None
    if exact_keys != set(_RESULT_PAYLOAD_TYPES):
        raise PlcResultBoundaryError(
            "payload",
            "PLC result payload must contain exactly the complete physical fields",
        )

    copied: dict[str, PlcScalar] = {}
    for field_name, expected_type in _RESULT_PAYLOAD_TYPES.items():
        try:
            value = payload[field_name]
        except Exception:
            raise PlcResultBoundaryError(
                field_name,
                f"{field_name} is unavailable",
            ) from None
        if type(value) is not expected_type:
            raise PlcResultBoundaryError(
                field_name,
                f"{field_name} must be {expected_type.__name__}",
            )
        if expected_type is float and not math.isfinite(value):
            raise PlcResultBoundaryError(
                field_name,
                f"{field_name} must be finite",
            )
        copied[field_name] = value
    return copied


class _DispatchCommand:
    def __init__(
        self,
        *,
        operation: str,
        plc_id: int,
        value: object,
        boundary_field: str,
    ) -> None:
        self._operation = operation
        self._plc_id = plc_id
        self._value = value
        self._boundary_field = boundary_field
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._cancelled = False
        self._started = False
        self.error: PlcResultBoundaryError | None = None

    def execute(self, service: object) -> None:
        with self._lock:
            if self._cancelled:
                self._event.set()
                return
            self._started = True
        try:
            if self._operation == "result":
                service.write_result(self._plc_id, self._value)
            else:
                service.write_testing_state(self._plc_id, self._value)
        except PlcResultBoundaryError as exc:
            self.error = exc
        except Exception:
            self.error = PlcResultBoundaryError(
                self._boundary_field,
                "PLC target dispatch failed",
            )
        finally:
            self._event.set()

    def wait(self, timeout_seconds: float) -> bool:
        return self._event.wait(timeout_seconds)

    def wait_until_complete(self) -> None:
        self._event.wait()

    def cancel_if_pending(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._cancelled = True
            return True

    def fail_if_pending(self, reason: str) -> bool:
        """Complete a command that an output owner can no longer accept."""

        with self._lock:
            if self._started or self._cancelled or self._event.is_set():
                return False
            self._cancelled = True
            self.error = PlcResultBoundaryError(
                self._boundary_field,
                reason,
            )
            self._event.set()
            return True


class PlcDispatchPorts:
    """Synchronous one-attempt adapters to PLC-service-owned Qt threads."""

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
        validated_plc_id = validate_plc_id(plc_id)
        copied_payload = validate_plc_result_payload(payload)
        self._dispatch(
            plc_id=validated_plc_id,
            operation="result",
            value=copied_payload,
            boundary_field="result_dispatch",
        )

    def write_testing_state(self, plc_id: int, testing: bool) -> None:
        validated_plc_id = validate_plc_id(plc_id)
        if type(testing) is not bool:
            raise PlcResultBoundaryError(
                "testing",
                "testing must be a bool",
            )
        self._dispatch(
            plc_id=validated_plc_id,
            operation="testing",
            value=testing,
            boundary_field="testing_dispatch",
        )

    def _dispatch(
        self,
        *,
        plc_id: int,
        operation: str,
        value: object,
        boundary_field: str,
    ) -> None:
        try:
            service = self._plc_services.get(plc_id)
            thread = self._plc_threads.get(plc_id)
        except Exception:
            raise PlcResultBoundaryError(
                boundary_field,
                "PLC target lookup failed",
            ) from None
        if service is None or thread is None:
            raise PlcResultBoundaryError(
                boundary_field,
                "PLC target is unavailable",
            )

        try:
            running = thread.isRunning()
        except Exception:
            raise PlcResultBoundaryError(
                boundary_field,
                "PLC target thread state is unavailable",
            ) from None
        if running is not True:
            raise PlcResultBoundaryError(
                boundary_field,
                "PLC target thread is not running",
            )

        signal = getattr(service, "plc_dispatch_requested", None)
        emit = getattr(signal, "emit", None)
        if not callable(emit):
            raise PlcResultBoundaryError(
                boundary_field,
                "PLC target dispatch is unavailable",
            )

        command = _DispatchCommand(
            operation=operation,
            plc_id=plc_id,
            value=value,
            boundary_field=boundary_field,
        )
        try:
            emit(command)
        except Exception:
            raise PlcResultBoundaryError(
                boundary_field,
                "PLC target dispatch was rejected",
            ) from None

        if not command.wait(self._dispatch_timeout_seconds):
            if command.cancel_if_pending():
                raise PlcResultBoundaryError(
                    boundary_field,
                    "PLC target dispatch did not complete promptly",
                )
            command.wait_until_complete()
        if command.error is not None:
            raise command.error
