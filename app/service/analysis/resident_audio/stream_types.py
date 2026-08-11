"""Public identities, events, snapshots, and retry state for resident streams."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .contracts import CaptureErrorCode, StreamState
from .owner_identity import (
    OwnerInstanceId,
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
)


RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0)


class RetryBackoff:
    """Thread-safe, per-hardware retry intent with deterministic capping."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempt = 0
        self._canceled = False

    @property
    def attempt(self) -> int:
        with self._lock:
            return self._attempt

    def next_delay(self) -> float | None:
        with self._lock:
            if self._canceled:
                return None
            index = min(self._attempt, len(RETRY_DELAYS_SECONDS) - 1)
            self._attempt += 1
            return RETRY_DELAYS_SECONDS[index]

    def reset(self) -> None:
        with self._lock:
            self._attempt = 0
            self._canceled = False

    def cancel(self) -> None:
        with self._lock:
            self._canceled = True


@dataclass(frozen=True, slots=True)
class StreamFailureEvent:
    hardware_id: str
    pool_epoch: str
    stream_generation: int
    error_code: CaptureErrorCode
    message: str
    retry_delay: float | None
    exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class ResidentStreamSnapshot:
    hardware_id: str
    pool_epoch: str
    stream_generation: int
    state: StreamState
    cursor: int
    ring_blocks: int
    owner_count: int
    ingress_depth: int
    generation_alive: bool
    handle_open: bool
    handle_close_attempted: bool
    handle_abort_succeeded: bool | None
    handle_stop_succeeded: bool | None
    failure_code: CaptureErrorCode | None
    ingest_thread_name: str | None
    ingest_thread_alive: bool
    sample_rate: float
    channels: int
    callback_frames: int
    ring_earliest_sample: int | None
    ring_latest_sample: int | None
    ring_earliest_monotonic: float | None
    ring_latest_monotonic: float | None
    command_depth: int
    old_generation_drops: int
    overflow_count: int
    recovery_count: int
    backlog_count: int
    finality_entry_count: int


__all__ = [
    "OwnerInstanceId",
    "OwnerRemovalOutcome",
    "OwnerRemovalReceipt",
    "RETRY_DELAYS_SECONDS",
    "ResidentStreamSnapshot",
    "RetryBackoff",
    "StreamFailureEvent",
]
