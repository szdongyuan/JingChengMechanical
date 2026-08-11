"""Value-only state and diagnostic records for owner finality."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .owner_identity import (
    OwnerInstanceId,
    OwnerRemovalReceipt,
    StreamInstanceId,
)


MAX_OWNER_FINALITY_LIMIT = 64


class OwnerFinalityState(str, Enum):
    PREPARE_RESERVED = "PREPARE_RESERVED"
    PREPARE_PENDING = "PREPARE_PENDING"
    PREPARE_RESOLVED_ABSENT = "PREPARE_RESOLVED_ABSENT"
    ACTIVE = "ACTIVE"
    REMOVAL_REQUESTED = "REMOVAL_REQUESTED"
    FINAL_REMOVED = "FINAL_REMOVED"
    FINAL_ABSENT = "FINAL_ABSENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"


@dataclass(frozen=True, slots=True)
class PrepareAdmission:
    owner_instance_id: OwnerInstanceId
    ledger_version: int

    def __post_init__(self) -> None:
        from .owner_finality_canonical import canonical_owner_instance_id
        from .owner_finality_validation import validate_prepare_admission

        validate_prepare_admission(self)
        object.__setattr__(
            self,
            "owner_instance_id",
            canonical_owner_instance_id(self.owner_instance_id),
        )


@dataclass(frozen=True, slots=True)
class PrepareClaim:
    owner_instance_id: OwnerInstanceId
    ledger_version: int
    activation_allowed: bool

    def __post_init__(self) -> None:
        from .owner_finality_canonical import canonical_owner_instance_id
        from .owner_finality_validation import validate_prepare_claim

        validate_prepare_claim(self)
        object.__setattr__(
            self,
            "owner_instance_id",
            canonical_owner_instance_id(self.owner_instance_id),
        )


@dataclass(frozen=True, slots=True)
class FinalityDurableProgress:
    removal_receipt: OwnerRemovalReceipt | None
    owner_signaled: bool
    owner_joined: bool
    late_owner_commands_resolved: bool
    remove_retry_disabled: bool

    def __post_init__(self) -> None:
        from .owner_finality_canonical import (
            canonical_owner_removal_receipt,
        )
        from .owner_finality_validation import (
            validate_finality_durable_progress,
        )

        validate_finality_durable_progress(self)
        if self.removal_receipt is not None:
            object.__setattr__(
                self,
                "removal_receipt",
                canonical_owner_removal_receipt(self.removal_receipt),
            )


@dataclass(frozen=True, slots=True)
class OwnerFinalityEntrySnapshot:
    pool_epoch: str | int
    hardware_id: str
    stream_generation: int
    run_id: str
    commit_attempt_id: str | int
    instance_nonce: str
    state: OwnerFinalityState
    prepare_pending: bool = True

    def __post_init__(self) -> None:
        from .owner_finality_validation import (
            validate_owner_finality_entry_snapshot,
        )

        validate_owner_finality_entry_snapshot(self)


@dataclass(frozen=True, slots=True)
class OwnerFinalitySnapshot:
    stream_instance_id: StreamInstanceId
    hardware_id: str
    entries: tuple[OwnerFinalityEntrySnapshot, ...]
    state_counts: tuple[tuple[OwnerFinalityState, int], ...]
    ledger_limit: int
    pending_late_commands: int
    final_unacknowledged_entries: int
    acknowledged_awaiting_prune: int
    pruned_entry_count: int
    backpressure_rejection_count: int

    def __post_init__(self) -> None:
        from .owner_finality_canonical import (
            canonical_entry_snapshot,
            canonical_stream_instance_id,
        )
        from .owner_finality_snapshot_validation import (
            validate_owner_finality_snapshot,
        )

        validate_owner_finality_snapshot(self)
        object.__setattr__(
            self,
            "stream_instance_id",
            canonical_stream_instance_id(self.stream_instance_id),
        )
        object.__setattr__(
            self,
            "entries",
            tuple(canonical_entry_snapshot(entry) for entry in self.entries),
        )
        object.__setattr__(
            self,
            "state_counts",
            tuple((state, count) for state, count in self.state_counts),
        )

    @property
    def entry_count(self) -> int:
        from .owner_finality_snapshot_validation import (
            validate_owner_finality_snapshot,
        )

        validate_owner_finality_snapshot(self)
        return len(self.entries)

    def count(self, state: OwnerFinalityState) -> int:
        from .owner_finality_snapshot_validation import (
            require_owner_finality_state,
            validate_owner_finality_snapshot,
        )

        validate_owner_finality_snapshot(self)
        require_owner_finality_state(state)
        return next(
            count
            for candidate, count in self.state_counts
            if candidate is state
        )


__all__ = [
    "FinalityDurableProgress",
    "MAX_OWNER_FINALITY_LIMIT",
    "OwnerFinalityEntrySnapshot",
    "OwnerFinalitySnapshot",
    "OwnerFinalityState",
    "PrepareAdmission",
    "PrepareClaim",
]
