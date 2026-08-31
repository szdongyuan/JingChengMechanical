"""Exact, read-only endpoint re-resolution for Resident recovery."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.service.analysis.resident_audio_mvp.diagnostics import DiagnosticReason
from app.service.managed_input_contracts import (
    BackendScanId,
    HardwareEndpointIdentity,
    InputEndpointDescriptor,
    InputEndpointObservation,
    RawBackendInputScan,
    RawManagedInputBackendPort,
    StableUidCapability,
    StableUidContract,
)


class EndpointResolutionError(RuntimeError):
    """A bounded exact-resolution failure suitable for lifecycle diagnostics."""

    def __init__(self, reason_code: DiagnosticReason, detail: str) -> None:
        if reason_code not in (
            DiagnosticReason.ENDPOINT_SCAN_FAILED,
            DiagnosticReason.ENDPOINT_MISSING,
            DiagnosticReason.ENDPOINT_AMBIGUOUS,
            DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
        ):
            raise ValueError("reason_code is not an endpoint resolution reason")
        safe_detail = detail[:256] if type(detail) is str and detail else reason_code.value
        super().__init__(safe_detail)
        self.reason_code = reason_code
        self.detail = safe_detail


class EndpointScanTimeout(EndpointResolutionError):
    """The bounded caller stopped waiting for one owned backend scan."""

    is_timeout = True

    def __init__(self, detail: str) -> None:
        super().__init__(DiagnosticReason.ENDPOINT_SCAN_FAILED, detail)


@dataclass(slots=True)
class _ScanOwner:
    scan_id: BackendScanId
    completion: threading.Event = field(default_factory=threading.Event)
    scan: RawBackendInputScan | None = None
    error: Exception | None = None
    discard_result: bool = False
    thread: threading.Thread | None = None
    shutdown_join_attempted: bool = False


class ManagedInputEndpointResolver:
    """Re-enumerate and match only one frozen physical input identity."""

    def __init__(
        self,
        backend: RawManagedInputBackendPort,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not callable(getattr(backend, "query_raw_input_endpoints", None)):
            raise TypeError("backend must provide query_raw_input_endpoints")
        self._backend = backend
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")
        self._monotonic_ns = monotonic_ns
        self._lock = threading.Lock()
        self._last_sequence_by_process: dict[str, int] = {}
        self._scan_owners: dict[object, _ScanOwner] = {}
        self._closing = False

    def resolve(
        self,
        frozen: HardwareEndpointIdentity,
        deadline_monotonic_ns: int | None = None,
    ) -> HardwareEndpointIdentity:
        if type(frozen) is not HardwareEndpointIdentity:
            raise TypeError("frozen must be an exact HardwareEndpointIdentity")
        if deadline_monotonic_ns is not None and (
            type(deadline_monotonic_ns) is not int
            or deadline_monotonic_ns <= self._monotonic_ns()
        ):
            raise EndpointScanTimeout("endpoint scan timed out before it started")
        scan_id, scan = self._owned_scan(frozen, deadline_monotonic_ns)
        if (
            type(scan) is not RawBackendInputScan
            or scan.scan_id != scan_id
            or not scan.complete
        ):
            raise EndpointResolutionError(
                DiagnosticReason.ENDPOINT_SCAN_FAILED,
                "endpoint scan was incomplete or did not match its scan id",
            )
        matches = self._matches(scan, frozen)
        if not matches:
            raise EndpointResolutionError(
                DiagnosticReason.ENDPOINT_MISSING,
                "the configured exact input endpoint is absent",
            )
        if len(matches) != 1:
            raise EndpointResolutionError(
                DiagnosticReason.ENDPOINT_AMBIGUOUS,
                "the configured exact input endpoint is not unique",
            )
        selected = matches[0]
        expected = self._descriptor(frozen)
        if frozen.stable_backend_uid is not None and selected.descriptor != expected:
            raise EndpointResolutionError(
                DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
                "stable endpoint UID matched but its descriptor is incompatible",
            )
        return HardwareEndpointIdentity(
            hardware_id=frozen.hardware_id,
            scan_id=scan_id,
            device_index=selected.device_index,
            hostapi_index=selected.hostapi_index,
            stable_backend_uid=selected.stable_backend_uid,
            device_name=selected.descriptor.device_name,
            hostapi_name=selected.descriptor.hostapi_name,
            sample_rate_hz=selected.descriptor.sample_rate_hz,
            input_channels=selected.descriptor.input_channels,
        )

    def resolve_until(
        self,
        frozen: HardwareEndpointIdentity,
        deadline_monotonic_ns: int,
    ) -> HardwareEndpointIdentity:
        return self.resolve(frozen, deadline_monotonic_ns)

    def shutdown(self, deadline_monotonic_ns: int) -> tuple[str, ...]:
        """Stop scan admission and join owned scan calls within one deadline."""

        if type(deadline_monotonic_ns) is not int or deadline_monotonic_ns < 1:
            raise TypeError("deadline_monotonic_ns must be a positive integer")
        with self._lock:
            self._closing = True
            owners = tuple(self._scan_owners.items())
            join_owners = tuple(
                owner
                for _hardware_id, owner in owners
                if not owner.shutdown_join_attempted
            )
            for owner in join_owners:
                owner.shutdown_join_attempted = True
        for owner in join_owners:
            thread = owner.thread
            if thread is None:
                continue
            remaining_s = max(
                0.0,
                (deadline_monotonic_ns - self._monotonic_ns())
                / 1_000_000_000,
            )
            thread.join(remaining_s)
        unresolved: list[str] = []
        with self._lock:
            for hardware_id, owner in owners:
                thread = owner.thread
                if owner.completion.is_set() or (
                    thread is not None and not thread.is_alive()
                ):
                    if self._scan_owners.get(hardware_id) is owner:
                        del self._scan_owners[hardware_id]
                    continue
                if thread is not None:
                    unresolved.append(thread.name)
        return tuple(sorted(set(unresolved)))

    def _owned_scan(
        self,
        frozen: HardwareEndpointIdentity,
        deadline_monotonic_ns: int | None,
    ) -> tuple[BackendScanId, RawBackendInputScan]:
        hardware_id = frozen.hardware_id
        with self._lock:
            if self._closing:
                raise EndpointResolutionError(
                    DiagnosticReason.ENDPOINT_SCAN_FAILED,
                    "endpoint resolver is shutting down",
                )
            owner = self._scan_owners.get(hardware_id)
            if owner is not None and owner.completion.is_set():
                del self._scan_owners[hardware_id]
                owner = None
            if owner is not None and owner.discard_result:
                raise EndpointScanTimeout(
                    "endpoint scan timed out and remains in flight"
                )
            if owner is None:
                scan_id = self._next_scan_id_locked(frozen.scan_id)
                owner = _ScanOwner(scan_id)
                thread = threading.Thread(
                    name=f"managed-input-scan-{hardware_id.value}",
                    target=self._run_scan,
                    args=(owner,),
                    daemon=True,
                )
                owner.thread = thread
                self._scan_owners[hardware_id] = owner
                try:
                    thread.start()
                except Exception as exc:
                    del self._scan_owners[hardware_id]
                    raise EndpointResolutionError(
                        DiagnosticReason.ENDPOINT_SCAN_FAILED,
                        "endpoint scan worker could not start",
                    ) from exc
        if not self._wait_for_scan(owner, deadline_monotonic_ns):
            with self._lock:
                owner.discard_result = True
            raise EndpointScanTimeout(
                "endpoint scan timed out before the recovery deadline"
            )
        with self._lock:
            if self._scan_owners.get(hardware_id) is owner:
                del self._scan_owners[hardware_id]
            discard_result = owner.discard_result
        if discard_result:
            raise EndpointScanTimeout(
                "timed-out endpoint scan completed; a fresh scan is required"
            )
        if owner.error is not None:
            exc = owner.error
            raise EndpointResolutionError(
                DiagnosticReason.ENDPOINT_SCAN_FAILED,
                f"endpoint scan {type(exc).__name__}: {self._safe_message(exc)}",
            ) from exc
        if type(owner.scan) is not RawBackendInputScan:
            raise EndpointResolutionError(
                DiagnosticReason.ENDPOINT_SCAN_FAILED,
                "endpoint scan returned no result",
            )
        return owner.scan_id, owner.scan

    def _run_scan(self, owner: _ScanOwner) -> None:
        try:
            owner.scan = self._backend.query_raw_input_endpoints(owner.scan_id)
        except Exception as exc:
            owner.error = exc
        finally:
            owner.completion.set()

    def _wait_for_scan(
        self,
        owner: _ScanOwner,
        deadline_monotonic_ns: int | None,
    ) -> bool:
        if deadline_monotonic_ns is None:
            owner.completion.wait()
            return True
        while not owner.completion.is_set():
            remaining_ns = deadline_monotonic_ns - self._monotonic_ns()
            if remaining_ns <= 0:
                return False
            owner.completion.wait(
                min(0.01, remaining_ns / 1_000_000_000)
            )
        return True

    def _next_scan_id(self, previous: BackendScanId) -> BackendScanId:
        with self._lock:
            return self._next_scan_id_locked(previous)

    def _next_scan_id_locked(self, previous: BackendScanId) -> BackendScanId:
        last = max(
            previous.sequence,
            self._last_sequence_by_process.get(previous.process_instance_id, 0),
        )
        next_sequence = last + 1
        self._last_sequence_by_process[previous.process_instance_id] = next_sequence
        return BackendScanId(previous.process_instance_id, next_sequence)

    @classmethod
    def _matches(
        cls,
        scan: RawBackendInputScan,
        frozen: HardwareEndpointIdentity,
    ) -> tuple[InputEndpointObservation, ...]:
        if frozen.stable_backend_uid is not None:
            if (
                scan.stable_uid_capability is not StableUidCapability.SUPPORTED
                or scan.stable_uid_contract
                is not StableUidContract.REVIEWED_OPAQUE_UID
            ):
                raise EndpointResolutionError(
                    DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
                    "the backend no longer provides the configured stable UID contract",
                )
            return tuple(
                item
                for item in scan.observations
                if item.stable_backend_uid == frozen.stable_backend_uid
            )
        descriptor = cls._descriptor(frozen)
        return tuple(
            item
            for item in scan.observations
            if item.descriptor == descriptor
        )

    @staticmethod
    def _descriptor(frozen: HardwareEndpointIdentity) -> InputEndpointDescriptor:
        return InputEndpointDescriptor(
            frozen.device_name,
            frozen.hostapi_name,
            frozen.sample_rate_hz,
            frozen.input_channels,
        )

    @staticmethod
    def _safe_message(exc: Exception) -> str:
        try:
            return str(exc)[:160]
        except Exception:
            return f"<{type(exc).__name__} message unavailable>"


__all__ = [
    "EndpointResolutionError",
    "EndpointScanTimeout",
    "ManagedInputEndpointResolver",
]
