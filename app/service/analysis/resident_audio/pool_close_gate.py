"""Pool-owned linearization gate for physical stream teardown."""

from __future__ import annotations

import threading


class PhysicalCloseLease:
    """One gate entry retained through physical-close outcome publication."""

    def __init__(
        self,
        gate: "PoolPhysicalCloseGate",
        stream,
        control_path: bool,
    ) -> None:
        self._gate = gate
        self.stream = stream
        self.control_path = control_path
        self.active = True

    def release(self) -> None:
        self._gate.release(self)


class PoolPhysicalCloseGate:
    """Exclude or drain physical closes before shutdown becomes visible."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._shutdown = False
        self._authorized_stream_ids: set[int] = set()
        self._leases: set[PhysicalCloseLease] = set()

    def begin_shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            self._authorized_stream_ids.clear()

    def acquire(
        self, stream, *, control_path: bool
    ) -> PhysicalCloseLease | None:
        with self._condition:
            if not self._base_permits_locked(stream, control_path):
                return None
            lease = PhysicalCloseLease(self, stream, control_path)
            self._leases.add(lease)
            return lease

    def permits(
        self,
        stream,
        *,
        control_path: bool,
        close_lease: PhysicalCloseLease | None = None,
        begin_physical: bool = False,
    ) -> bool:
        with self._condition:
            if close_lease is None:
                return self._base_permits_locked(stream, control_path)
            valid = bool(
                close_lease in self._leases
                and close_lease.active
                and close_lease.stream is stream
                and close_lease.control_path is control_path
            )
            return valid

    def release(self, lease: PhysicalCloseLease) -> None:
        with self._condition:
            if not lease.active:
                return
            lease.active = False
            self._leases.discard(lease)
            self._condition.notify_all()

    def wait_for_drain(self, timeout: float | None) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._leases,
                timeout=timeout,
            )

    def authorize(self, stream) -> None:
        with self._condition:
            if self._shutdown:
                self._authorized_stream_ids.add(id(stream))

    def revoke(self, stream) -> None:
        with self._condition:
            self._authorized_stream_ids.discard(id(stream))

    @property
    def active_lease_count(self) -> int:
        with self._condition:
            return len(self._leases)

    def _base_permits_locked(self, stream, control_path: bool) -> bool:
        return (
            not self._shutdown
            or (
                control_path
                and id(stream) in self._authorized_stream_ids
            )
        )


__all__ = ["PhysicalCloseLease", "PoolPhysicalCloseGate"]
