"""Pool-owned Resident generation recovery records and bounded retry policy."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .contracts import LifecycleFuture, ResidentError, _LifecyclePublisher


HEARTBEAT_FRESH_NS = 1_000_000_000
STALE_OBSERVATION_SECONDS = 1.0
RECOVERY_ATTEMPT_NS = 5_000_000_000
RETRY_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
MAX_ORPHANS_PER_HARDWARE = 2
MAX_ORPHANS_GLOBAL = 12


def retry_delay(attempt: int) -> float:
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    return RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]


@dataclass(slots=True)
class _OrphanRecord:
    hardware_id: str
    slot: object
    handle: object
    cleanup_attempt: int = 0
    last_error: ResidentError | None = None


@dataclass(slots=True)
class _RecoveryState:
    desired_revision: int
    wake: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    future: LifecycleFuture | None = None
    publisher: _LifecyclePublisher | None = None
    worker: threading.Thread | None = None
    starting: bool = False
    attempt: int = 0
    deadline_monotonic_ns: int | None = None


__all__ = [
    "HEARTBEAT_FRESH_NS",
    "MAX_ORPHANS_GLOBAL",
    "MAX_ORPHANS_PER_HARDWARE",
    "RECOVERY_ATTEMPT_NS",
    "RETRY_BACKOFF_SECONDS",
    "STALE_OBSERVATION_SECONDS",
]
