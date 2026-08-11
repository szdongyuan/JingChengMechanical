"""Lightweight control and diagnostic types for capture owners."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .contracts import (
    CaptureErrorCode,
    CaptureOwnerState,
    CapturedAudioBlock,
    FrozenCaptureArtifact,
)
from .timeline import TimelineMapping


OWNER_REFERENCE_CREDITS = 8
MAPPING_TIMEOUT_SECONDS = 2.0


class CommitToken:
    """Single-writer, lock-free gate opened by the pool's commit CAS."""

    __slots__ = ("_state",)

    _PREPARED = 0
    _OPEN = 1
    _CANCELED = 2

    def __init__(self) -> None:
        self._state = self._PREPARED

    @property
    def is_open(self) -> bool:
        return self._state == self._OPEN

    def open(self) -> bool:
        if self._state == self._CANCELED:
            return False
        self._state = self._OPEN
        return True

    def cancel(self) -> bool:
        if self._state == self._CANCELED:
            return False
        self._state = self._CANCELED
        return True


class OwnerControlKind(str, Enum):
    """Commands emitted toward the stream's single ingest writer."""

    REQUEST_LIVE_SWITCH = "REQUEST_LIVE_SWITCH"
    DETACH = "DETACH"


@dataclass(frozen=True, slots=True)
class OwnerControlEvent:
    kind: OwnerControlKind
    owner: object
    run_id: str
    commit_attempt_id: str
    owner_capability: str
    pool_epoch: str
    stream_generation: int


@dataclass(frozen=True, slots=True)
class OwnerTerminalEvent:
    """One lightweight terminal attempt for pool-side lease arbitration."""

    terminal_attempt_id: str
    run_id: str
    commit_attempt_id: str
    owner_capability: str
    pool_epoch: str
    stream_generation: int
    state: CaptureOwnerState
    artifact_id: str
    publication_token: str | None
    artifact: FrozenCaptureArtifact | None
    error_code: CaptureErrorCode | None
    message: str | None


@dataclass(frozen=True, slots=True)
class CaptureOwnerSnapshot:
    state: CaptureOwnerState
    previous_state: CaptureOwnerState | None
    commit_open: bool
    original_trigger_monotonic: float
    mapping_deadline: float | None
    capture_start_cursor: int | None
    capture_end_cursor: int | None
    next_write_cursor: int | None
    written_frames: int
    credits_occupied: int
    peak_credits: int
    catch_up_references: int
    live_queued: int
    in_flight: bool
    terminal_attempt_id: str | None
    drain_thread_name: str | None
    drain_thread_alive: bool


class TimelineMapper(Protocol):
    def map_trigger(self, trigger_monotonic: float) -> TimelineMapping: ...


class ArtifactWriter(Protocol):
    @property
    def artifact_id(self) -> str: ...

    def bind_capture_start(
        self,
        *,
        sample_start: int,
        capture_start_monotonic: float,
        time_quality: str,
    ) -> None: ...

    def write(self, audio_block: CapturedAudioBlock) -> int: ...

    def freeze(self) -> FrozenCaptureArtifact: ...

    def discard(self) -> bool: ...


class LiveReferenceQueue(Protocol):
    def try_push(self, audio_block: CapturedAudioBlock) -> bool: ...

    def popleft(self) -> CapturedAudioBlock: ...

    def clear(self) -> None: ...

    def __len__(self) -> int: ...


class DequeLiveReferenceQueue:
    """Nonblocking owner-local queue; the credit ledger supplies its bound."""

    def __init__(self) -> None:
        self._references: deque[CapturedAudioBlock] = deque()

    def try_push(self, audio_block: CapturedAudioBlock) -> bool:
        self._references.append(audio_block)
        return True

    def popleft(self) -> CapturedAudioBlock:
        return self._references.popleft()

    def clear(self) -> None:
        self._references.clear()

    def __len__(self) -> int:
        return len(self._references)


@dataclass(slots=True)
class TerminalDecision:
    state: CaptureOwnerState
    error_code: CaptureErrorCode | None
    message: str | None
    attempt_id: str


__all__ = [
    "CaptureOwnerSnapshot",
    "CommitToken",
    "MAPPING_TIMEOUT_SECONDS",
    "OWNER_REFERENCE_CREDITS",
    "OwnerControlEvent",
    "OwnerControlKind",
    "OwnerTerminalEvent",
]
