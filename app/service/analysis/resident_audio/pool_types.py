"""Immutable public contracts and private records for the resident audio pool."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .contracts import (
    ArtifactState,
    CaptureErrorCode,
    FrozenCaptureArtifact,
    LeaseState,
    StreamState,
)
from .owner_settlement_types import SettlementDiagnostics
from .owner_types import CaptureOwnerSnapshot


MAX_RESIDENT_STREAMS = 6
MAX_CAPTURE_LEASES = 6
DEFAULT_TERMINAL_DIAGNOSTIC_LIMIT = 256
_ACTIVE_LEASE_STATES = frozenset(
    (LeaseState.RESERVED, LeaseState.COMMITTING, LeaseState.COMMITTED)
)
_TERMINAL_LEASE_STATES = frozenset(
    (LeaseState.RELEASED, LeaseState.FAILED, LeaseState.CANCELED)
)


class PoolState(str, Enum):
    OPEN = "OPEN"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    CLOSED = "CLOSED"
    FAILED_CLOSED = "FAILED_CLOSED"


@dataclass(frozen=True, slots=True)
class DesiredHardware:
    """One desired hardware identity and its required resolved input format."""

    hardware_id: str
    sample_rate: float
    channels: int

    def __post_init__(self) -> None:
        if not isinstance(self.hardware_id, str) or not self.hardware_id:
            raise ValueError("hardware_id must be a non-empty string")
        if isinstance(self.sample_rate, bool) or not isinstance(
            self.sample_rate, (int, float)
        ):
            raise ValueError("sample_rate must be positive and finite")
        rate = float(self.sample_rate)
        if rate <= 0 or rate == float("inf") or rate != rate:
            raise ValueError("sample_rate must be positive and finite")
        if (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, int)
            or self.channels < 1
        ):
            raise ValueError("channels must be a positive non-boolean integer")
        object.__setattr__(self, "sample_rate", rate)


@dataclass(frozen=True, slots=True)
class PoolStreamSnapshot:
    hardware_id: str
    desired_revision: int
    resolution_attempt_id: int
    stream_generation: int
    state: StreamState
    owner_count: int
    ready: bool
    published: bool
    error_code: CaptureErrorCode | None = None
    message: str | None = None
    retry_attempt: int = 0
    retry_delay: float | None = None
    exception_type: str | None = None
    sample_rate: float | None = None
    channels: int | None = None
    callback_frames: int | None = None
    ring_earliest_sample: int | None = None
    ring_latest_sample: int | None = None
    ring_earliest_monotonic: float | None = None
    ring_latest_monotonic: float | None = None
    ingress_depth: int = 0
    command_depth: int = 0
    old_generation_drops: int = 0
    overflow_count: int = 0
    recovery_count: int = 0
    backlog_count: int = 0
    finality_entry_count: int = 0
    previous_state: StreamState | None = None


@dataclass(frozen=True, slots=True)
class ShutdownResourceSnapshot:
    hardware_id: str | None
    stream_generation: int | None
    thread_name: str | None
    thread_alive: bool
    handle_open: bool
    resource_kind: str = "stream"
    run_id: str | None = None
    exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class LeaseOutcome:
    run_id: str
    state: LeaseState
    error_code: CaptureErrorCode | None = None
    message: str | None = None
    artifact_id: str | None = None
    artifact: FrozenCaptureArtifact | None = None
    diagnostic_context: tuple[tuple[str, str | int], ...] = ()

    @property
    def accepted(self) -> bool:
        return self.state in _ACTIVE_LEASE_STATES or self.state is LeaseState.RELEASED


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    run_id: str
    state: LeaseState
    commit_attempt_id: str | None
    artifact_id: str | None
    cancel_requested: bool
    settled: bool
    artifact_state: ArtifactState | None = None
    artifact_path: str | None = None
    previous_state: LeaseState | None = None
    hardware_id: str | None = None
    stream_generation: int | None = None
    owner_snapshot: CaptureOwnerSnapshot | None = None
    artifact_consumer_id: str | None = None
    error_code: CaptureErrorCode | None = None


@dataclass(frozen=True, slots=True)
class ResidentAudioPoolSnapshot:
    pool_epoch: str
    state: PoolState
    desired_hardware_ids: tuple[str, ...]
    published_hardware_ids: tuple[str, ...]
    streams: tuple[PoolStreamSnapshot, ...]
    reserved_leases: int
    committing_leases: int
    committed_leases: int
    terminal_diagnostic_count: int
    unsettled_transactions: int
    shutdown_error_code: CaptureErrorCode | None = None
    shutdown_resources: tuple[ShutdownResourceSnapshot, ...] = ()
    cleanup_deleted_paths: tuple[str, ...] = ()
    cleanup_failed_paths: tuple[str, ...] = ()
    cleanup_retained_paths: tuple[str, ...] = ()
    cleanup_exception_type: str | None = None
    settlement_diagnostics: SettlementDiagnostics | None = None

    @property
    def active_lease_count(self) -> int:
        return (
            self.reserved_leases
            + self.committing_leases
            + self.committed_leases
        )

    def stream_for(self, hardware_id: str) -> PoolStreamSnapshot | None:
        return next(
            (item for item in self.streams if item.hardware_id == hardware_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    accepted: bool
    snapshot: ResidentAudioPoolSnapshot
    error_code: CaptureErrorCode | None = None
    message: str | None = None


@dataclass(slots=True)
class _OpeningAttempt:
    hardware_id: str
    desired: DesiredHardware
    desired_revision: int
    resolution_attempt_id: int
    cancel_token: threading.Event
    stream_generation: int | None = None
    stream: object | None = None
    ready_event: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True, slots=True)
class _OpenAction:
    hardware_id: str
    desired: DesiredHardware
    desired_revision: int


@dataclass(slots=True)
class _StreamRecord:
    stream: object
    desired: DesiredHardware
    desired_revision: int
    resolution_attempt_id: int
    stream_generation: int


@dataclass(frozen=True, slots=True)
class _RemovalAction:
    hardware_id: str
    desired_revision: int
    expected_desired: DesiredHardware | None
    expected_record: _StreamRecord | None


@dataclass(frozen=True, slots=True)
class _ReplacementIdentity:
    hardware_id: str
    desired_revision: int
    resolution_attempt_id: int
    stream_generation: int
    stream: object


@dataclass(slots=True, eq=False)
class _PendingRemoval:
    desired_revision: int
    replacements: tuple[_ReplacementIdentity, ...]
    removals: tuple[_RemovalAction, ...]
    settled_event: threading.Event
    scheduled: bool = False


@dataclass(slots=True, eq=False)
class _ManagedClose:
    action: _RemovalAction
    record: _StreamRecord
    settled_event: threading.Event
    in_progress: bool = True
    failed: bool = False
    message: str | None = None


@dataclass(slots=True, eq=False)
class _ShutdownClose:
    stream: object
    settled_event: threading.Event
    closed: bool = False
    worker: object | None = None


@dataclass(slots=True, eq=False)
class _UnpublishedClose:
    hardware_id: str
    desired_revision: int
    resolution_attempt_id: int
    stream_generation: int
    stream: object
    settled_event: threading.Event
    in_progress: bool = True
    closed: bool = False
    open_attempt: object | None = None


@dataclass(slots=True, eq=False)
class _PendingReconcile:
    desired_revision: int
    opens: tuple[_OpenAction, ...]
    removals: tuple[_RemovalAction, ...]
    require_ready: bool = False
    in_progress: bool = False


@dataclass(slots=True, eq=False)
class _LeaseCleanup:
    owner: object | None
    writer: object | None
    stream: object | None
    identity: object | None
    removal_required: bool
    signal_kind: str
    failure_code: CaptureErrorCode
    failure_message: str
    terminal_state: LeaseState
    terminal_code: CaptureErrorCode
    terminal_message: str
    removal_acknowledged: bool = False
    signal_sent: bool = False
    owner_joined: bool = False
    artifact_clean: bool = False
    in_progress: bool = False


@dataclass(slots=True)
class _LeaseRecord:
    run_id: str
    state: LeaseState
    outcome: LeaseOutcome
    commit_attempt_id: str | None = None
    owner_capability: str | None = None
    artifact_id: str | None = None
    publication_token: str | None = None
    stream: object | None = None
    stream_generation: int | None = None
    owner: object | None = None
    writer: object | None = None
    identity: object | None = None
    owner_instance_id: object | None = None
    artifact: object | None = None
    settlement_claim: object | None = None
    terminal_summary: object | None = None
    non_admission_proof: object | None = None
    diagnostic_context: tuple[tuple[str, str | int], ...] = ()
    commit_token: object | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None
    shutdown_requested: bool = False
    terminal_attempt_id: str | None = None
    settled: bool = True
    commit_settled: threading.Event | None = None
    commit_worker: threading.Thread | None = None
    commit_resources_published: bool = False
    terminal_settled: threading.Event | None = None
    pre_teardown_settled: threading.Event | None = None
    cleanup: _LeaseCleanup | None = None
    previous_state: LeaseState | None = None
    artifact_consumer_id: str | None = None


class CaptureCompletion:
    """Synchronous publication capability for exactly one frozen artifact."""

    __slots__ = ("artifact", "_store", "_resolution", "_claimed")

    def __init__(
        self,
        artifact: FrozenCaptureArtifact,
        store: object,
        claimed: Callable[[str], None] | None = None,
    ) -> None:
        self.artifact = artifact
        self._store = store
        self._resolution: str | None = None
        self._claimed = claimed

    def claim(self, consumer_id: str) -> str | None:
        token = self._store.claim_ready(
            self.artifact.artifact_id,
            self.artifact.publication_token,
            consumer_id,
        )
        if token is not None:
            self._resolution = "CLAIMED"
            if self._claimed is not None:
                try:
                    self._claimed(consumer_id)
                except Exception:
                    pass
        return token

    def decline(self, reason: str) -> bool:
        resolved = self._store.decline_publication(
            self.artifact.artifact_id,
            self.artifact.publication_token,
            reason,
        )
        if resolved is True:
            self._resolution = "DECLINED"
        return resolved

    @property
    def resolution(self) -> str | None:
        return self._resolution


CompletionHandler = Callable[[CaptureCompletion], None]
TerminalRecords = OrderedDict[str, _LeaseRecord]


__all__ = [
    "CaptureCompletion",
    "DEFAULT_TERMINAL_DIAGNOSTIC_LIMIT",
    "DesiredHardware",
    "LeaseOutcome",
    "LeaseSnapshot",
    "MAX_CAPTURE_LEASES",
    "MAX_RESIDENT_STREAMS",
    "PoolState",
    "PoolStreamSnapshot",
    "ReconcileResult",
    "ResidentAudioPoolSnapshot",
    "ShutdownResourceSnapshot",
]
