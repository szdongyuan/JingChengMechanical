"""Bounded value-only finality ledger for resident stream owners."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import owner_finality_canonical as canonical
from .owner_finality_crypto import OwnerFinalityAuthenticator
from .owner_finality_types import (
    FinalityDurableProgress, MAX_OWNER_FINALITY_LIMIT,
    OwnerFinalityEntrySnapshot, OwnerFinalitySnapshot, OwnerFinalityState,
    PrepareAdmission, PrepareClaim,
)
from .owner_identity import (
    FinalityAckReceipt, FinalityPruneOutcome, FinalityPruneReceipt,
    MAX_SIGNED_64, OwnerInstanceId, OwnerNonAdmissionProof,
    OwnerNonAdmissionReason, OwnerRemovalOutcome, OwnerRemovalReceipt,
    StreamInstanceId, _require_positive_ledger_version,
)
DEFAULT_OWNER_FINALITY_LIMIT = MAX_OWNER_FINALITY_LIMIT

class PrepareConflictError(RuntimeError):
    """A retained identity or lifecycle fact forbids another prepare action."""


@dataclass(slots=True)
class _OwnerFinalityEntry:
    _ledger_version: int
    state: OwnerFinalityState = OwnerFinalityState.PREPARE_RESERVED
    reservation_open: bool = True
    queued: bool = False
    claimed: bool = False
    active: bool = False
    prepare_resolved_absent: bool = False
    removal_requested: bool = False
    final_receipt: OwnerRemovalReceipt | None = None
    ack_receipt: FinalityAckReceipt | None = None

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_ledger_version" and hasattr(self, "_ledger_version"):
            raise AttributeError("retained ledger version is immutable")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        _require_positive_ledger_version(
            self._ledger_version, owner=type(self).__name__
        )

    @property
    def ledger_version(self) -> int:
        return self._ledger_version


class OwnerFinalityLedger:
    """Thread-safe hard-bounded ledger retaining only immutable owner values."""

    def __init__(
        self,
        stream_instance_id: StreamInstanceId,
        *,
        limit: int = DEFAULT_OWNER_FINALITY_LIMIT,
    ) -> None:
        stream_instance_id = canonical.canonical_stream_instance_id(
            stream_instance_id
        )
        if type(limit) is not int:
            raise TypeError("owner finality ledger limit must be an integer")
        if not 0 < limit <= DEFAULT_OWNER_FINALITY_LIMIT:
            raise ValueError(
                "owner finality ledger limit must be in 1..64"
            )
        self._stream_instance_id = stream_instance_id
        self._limit = limit
        self._guard = threading.RLock()
        self._entries: dict[OwnerInstanceId, _OwnerFinalityEntry] = {}
        self._next_ledger_version = 0
        self._pruned_entry_count = 0
        self._backpressure_rejection_count = 0
        self._authenticator = OwnerFinalityAuthenticator(stream_instance_id)

    def reserve_prepare(
        self, instance_id: OwnerInstanceId
    ) -> PrepareAdmission | OwnerNonAdmissionProof:
        instance_id = self._validate_instance_id(instance_id)
        self._require_stream_match(instance_id)
        with self._guard:
            retained = self._entries.get(instance_id)
            if retained is not None:
                raise PrepareConflictError(
                    "owner instance is already retained by the ledger"
                )
            if len(self._entries) >= self._limit:
                self._backpressure_rejection_count = self._increment_bounded(
                    self._backpressure_rejection_count
                )
                return OwnerNonAdmissionProof(
                    owner_instance_id=instance_id,
                    reason=OwnerNonAdmissionReason.LEDGER_FULL,
                    command_enqueued=False,
                    command_claimed=False,
                )
            if self._next_ledger_version >= MAX_SIGNED_64:
                raise OverflowError("owner finality ledger version exhausted")
            self._next_ledger_version += 1
            version = self._next_ledger_version
            self._entries[instance_id] = _OwnerFinalityEntry(version)
            return PrepareAdmission(instance_id, version)

    def mark_prepare_enqueued(self, instance_id: OwnerInstanceId) -> None:
        instance_id = self._validate_instance_id(instance_id)
        if not self._matches_stream(instance_id):
            return
        with self._guard:
            entry = self._entries.get(instance_id)
            if (
                entry is None
                or not entry.reservation_open
                or entry.ack_receipt is not None
            ):
                return
            entry.reservation_open = False
            entry.queued = True
            self._synchronize_entry_state(entry)

    def claim_prepare(self, instance_id: OwnerInstanceId) -> PrepareClaim:
        instance_id = self._validate_instance_id(instance_id)
        self._require_stream_match(instance_id)
        with self._guard:
            entry = self._require_entry(instance_id)
            if (
                entry.claimed
                or entry.active
                or entry.ack_receipt is not None
                or not (entry.reservation_open or entry.queued)
            ):
                raise PrepareConflictError(
                    "owner prepare claim conflicts with retained lifecycle"
                )
            entry.reservation_open = False
            entry.queued = False
            activation_allowed = (
                not entry.removal_requested
                and entry.final_receipt is None
                and entry.state is not OwnerFinalityState.ACKNOWLEDGED
            )
            entry.claimed = True
            self._synchronize_entry_state(entry)
            return PrepareClaim(
                instance_id,
                entry.ledger_version,
                activation_allowed,
            )

    def authorize_prepare_publication(
        self, instance_id: OwnerInstanceId
    ) -> bool:
        """Return the pure pre-publication gate without mutating ledger state.

        Task 3 must call this immediately before registry mutation and must
        resolve the prepare in ``finally``. Tombstoned late-active cleanup
        remains owned by the exact removal protocol.
        """

        instance_id = self._validate_instance_id(instance_id)
        if not self._matches_stream(instance_id):
            return False
        with self._guard:
            entry = self._entries.get(instance_id)
            if entry is None:
                return False
            return (
                entry.claimed
                and not entry.reservation_open
                and not entry.queued
                and not entry.active
                and not entry.removal_requested
                and entry.final_receipt is None
                and entry.ack_receipt is None
                and entry.state is OwnerFinalityState.PREPARE_PENDING
            )

    def resolve_queued_prepare_cancellation(
        self, instance_id: OwnerInstanceId, *, cancelled: bool
    ) -> OwnerRemovalReceipt:
        instance_id = self._validate_instance_id(instance_id)
        if type(cancelled) is not bool:
            raise TypeError("cancelled must be bool")
        if not self._matches_stream(instance_id):
            return OwnerRemovalReceipt(
                instance_id, OwnerRemovalOutcome.MISMATCH
            )
        with self._guard:
            entry = self._entries.get(instance_id)
            if entry is None:
                return OwnerRemovalReceipt(
                    instance_id, OwnerRemovalOutcome.MISMATCH
                )
            if (
                cancelled
                and entry.removal_requested
                and entry.queued
                and not entry.claimed
            ):
                entry.queued = False
                if entry.final_receipt is not None:
                    self._synchronize_entry_state(entry)
                    return canonical.canonical_owner_removal_receipt(entry.final_receipt)
                return self._record_finality(
                    instance_id,
                    entry,
                    OwnerRemovalOutcome.TOMBSTONED_ABSENT,
                )
            if entry.final_receipt is not None:
                return canonical.canonical_owner_removal_receipt(entry.final_receipt)
            else:
                return OwnerRemovalReceipt(
                    instance_id, OwnerRemovalOutcome.INDETERMINATE
                )

    def abort_prepare_reservation(
        self, instance_id: OwnerInstanceId, *, command_not_enqueued: bool
    ) -> OwnerRemovalReceipt:
        instance_id = self._validate_instance_id(instance_id)
        if type(command_not_enqueued) is not bool:
            raise TypeError("command_not_enqueued must be bool")
        if not self._matches_stream(instance_id):
            return OwnerRemovalReceipt(
                instance_id, OwnerRemovalOutcome.MISMATCH
            )
        with self._guard:
            entry = self._entries.get(instance_id)
            if entry is None:
                return OwnerRemovalReceipt(
                    instance_id, OwnerRemovalOutcome.MISMATCH
                )
            if (
                command_not_enqueued
                and entry.reservation_open
                and not entry.queued
                and not entry.claimed
            ):
                entry.reservation_open = False
                if (
                    entry.final_receipt is None
                    and not entry.removal_requested
                ):
                    entry.prepare_resolved_absent = True
                    self._synchronize_entry_state(entry)
                if entry.final_receipt is not None:
                    self._synchronize_entry_state(entry)
                    return canonical.canonical_owner_removal_receipt(entry.final_receipt)
                if entry.removal_requested:
                    return self._record_finality(
                        instance_id,
                        entry,
                        OwnerRemovalOutcome.TOMBSTONED_ABSENT,
                    )
            if entry.final_receipt is not None:
                return canonical.canonical_owner_removal_receipt(entry.final_receipt)
            else:
                return OwnerRemovalReceipt(
                    instance_id, OwnerRemovalOutcome.INDETERMINATE
                )

    def resolve_prepare(
        self, instance_id: OwnerInstanceId, *, activated: bool
    ) -> None:
        instance_id = self._validate_instance_id(instance_id)
        if type(activated) is not bool:
            raise TypeError("activated must be bool")
        if not self._matches_stream(instance_id):
            return
        with self._guard:
            entry = self._entries.get(instance_id)
            if entry is None or not entry.claimed:
                return
            entry.reservation_open = False
            entry.queued = False
            entry.claimed = False
            if entry.final_receipt is not None:
                entry.active = False
                self._synchronize_entry_state(entry)
                return
            if entry.removal_requested:
                if activated:
                    entry.active = True
                else:
                    self._record_finality(
                        instance_id,
                        entry,
                        OwnerRemovalOutcome.TOMBSTONED_ABSENT,
                    )
            elif activated:
                entry.active = True
            else:
                entry.prepare_resolved_absent = True
            self._synchronize_entry_state(entry)

    def request_removal(
        self, instance_id: OwnerInstanceId, *, registration_present: bool
    ) -> OwnerRemovalReceipt:
        instance_id = self._validate_instance_id(instance_id)
        if type(registration_present) is not bool:
            raise TypeError("registration_present must be bool")
        if not self._matches_stream(instance_id):
            return OwnerRemovalReceipt(
                instance_id, OwnerRemovalOutcome.MISMATCH
            )
        with self._guard:
            entry = self._entries.get(instance_id)
            if entry is None:
                return OwnerRemovalReceipt(
                    instance_id, OwnerRemovalOutcome.MISMATCH
                )
            if entry.final_receipt is not None:
                return canonical.canonical_owner_removal_receipt(entry.final_receipt)
            entry.removal_requested = True
            if registration_present:
                entry.active = False
                return self._record_finality(
                    instance_id, entry, OwnerRemovalOutcome.REMOVED
                )
            if entry.reservation_open or entry.queued or entry.claimed:
                self._synchronize_entry_state(entry)
                return OwnerRemovalReceipt(
                    instance_id, OwnerRemovalOutcome.INDETERMINATE
                )
            entry.active = False
            return self._record_finality(
                instance_id,
                entry,
                OwnerRemovalOutcome.TOMBSTONED_ABSENT,
            )

    def _acknowledge_entry(
        self,
        instance_id: OwnerInstanceId,
        positive_receipt: OwnerRemovalReceipt,
        durable_progress: FinalityDurableProgress,
    ) -> FinalityAckReceipt | None:
        instance_id = self._validate_instance_id(instance_id)
        try:
            positive_receipt = canonical.canonical_owner_removal_receipt(
                positive_receipt)
            durable_progress = canonical.canonical_durable_progress(
                durable_progress)
        except (TypeError, ValueError):
            return None
        if not self._matches_stream(instance_id):
            return None
        with self._guard:
            entry = self._entries.get(instance_id)
            if entry is None or not positive_receipt.confirms(instance_id):
                return None
            if (
                entry.final_receipt != positive_receipt
                or entry.reservation_open
                or entry.active
                or entry.queued
                or entry.claimed
            ):
                return None
            if entry.ack_receipt is not None:
                return canonical.canonical_finality_ack_receipt(entry.ack_receipt)
            if not self._durable_progress_confirms(
                instance_id, positive_receipt, durable_progress
            ):
                return None
            entry.ack_receipt = canonical.canonical_finality_ack_receipt(
                self._authenticator.issue(
                    instance_id, entry.ledger_version
                )
            )
            self._synchronize_entry_state(entry)
            return canonical.canonical_finality_ack_receipt(entry.ack_receipt)

    def _prune_entry(
        self,
        instance_id: OwnerInstanceId,
        ack_receipt: FinalityAckReceipt,
    ) -> FinalityPruneReceipt:
        instance_id = self._validate_instance_id(instance_id)
        if not self._matches_stream(instance_id):
            return FinalityPruneReceipt(
                instance_id, FinalityPruneOutcome.MISMATCH
            )
        try:
            ack_receipt = canonical.canonical_finality_ack_receipt(
                ack_receipt)
        except (TypeError, ValueError):
            return FinalityPruneReceipt(
                instance_id, FinalityPruneOutcome.MISMATCH
            )
        with self._guard:
            entry = self._entries.get(instance_id)
            expected_version = (
                None if entry is None else entry.ledger_version
            )
            if not self._authenticator.validates(
                instance_id, ack_receipt, expected_version
            ):
                return FinalityPruneReceipt(
                    instance_id, FinalityPruneOutcome.MISMATCH
                )
            if entry is None:
                return FinalityPruneReceipt(
                    instance_id,
                    FinalityPruneOutcome.ALREADY_PRUNED,
                )
            if (
                entry.state is not OwnerFinalityState.ACKNOWLEDGED
                or entry.ack_receipt != ack_receipt
                or entry.reservation_open
                or entry.active
                or entry.queued
                or entry.claimed
            ):
                return FinalityPruneReceipt(
                    instance_id, FinalityPruneOutcome.MISMATCH
                )
            del self._entries[instance_id]
            self._pruned_entry_count = self._increment_bounded(
                self._pruned_entry_count
            )
            return FinalityPruneReceipt(
                instance_id, FinalityPruneOutcome.PRUNED
            )

    def snapshot(self) -> OwnerFinalitySnapshot:
        with self._guard:
            ordered = sorted(
                self._entries.items(),
                key=lambda item: item[1].ledger_version,
            )
            entries = tuple(
                OwnerFinalityEntrySnapshot(
                    pool_epoch=instance_id.pool_epoch,
                    hardware_id=instance_id.hardware_id,
                    stream_generation=instance_id.stream_generation,
                    run_id=instance_id.run_id,
                    commit_attempt_id=instance_id.commit_attempt_id,
                    instance_nonce=instance_id.instance_nonce,
                    state=entry.state,
                    prepare_pending=(
                        entry.reservation_open or entry.queued or entry.claimed
                    ),
                )
                for instance_id, entry in ordered
            )
            counts = tuple(
                (
                    state,
                    sum(
                        entry.state is state
                        for entry in self._entries.values()
                    ),
                )
                for state in OwnerFinalityState
            )
            return OwnerFinalitySnapshot(
                stream_instance_id=self._stream_instance_id,
                hardware_id=self._stream_instance_id.hardware_id,
                entries=entries,
                state_counts=counts,
                ledger_limit=self._limit,
                pending_late_commands=sum(
                    entry.reservation_open or entry.queued or entry.claimed
                    for entry in self._entries.values()
                ),
                final_unacknowledged_entries=sum(
                    entry.state
                    in (
                        OwnerFinalityState.FINAL_REMOVED,
                        OwnerFinalityState.FINAL_ABSENT,
                    )
                    for entry in self._entries.values()
                ),
                acknowledged_awaiting_prune=sum(
                    entry.state is OwnerFinalityState.ACKNOWLEDGED
                    for entry in self._entries.values()
                ),
                pruned_entry_count=self._pruned_entry_count,
                backpressure_rejection_count=(
                    self._backpressure_rejection_count
                ),
            )

    def _require_entry(
        self, instance_id: OwnerInstanceId
    ) -> _OwnerFinalityEntry:
        instance_id = self._validate_instance_id(instance_id)
        try:
            return self._entries[instance_id]
        except KeyError:
            raise KeyError(
                "owner instance has no retained finality entry"
            ) from None

    @staticmethod
    def _validate_instance_id(instance_id: object) -> OwnerInstanceId:
        return canonical.canonical_owner_instance_id(instance_id)

    def _matches_stream(self, instance_id: OwnerInstanceId) -> bool:
        instance_id = self._validate_instance_id(instance_id)
        return (
            instance_id.pool_epoch == self._stream_instance_id.pool_epoch
            and instance_id.hardware_id == self._stream_instance_id.hardware_id
            and instance_id.stream_generation
            == self._stream_instance_id.stream_generation
        )
    def _require_stream_match(
        self, instance_id: OwnerInstanceId
    ) -> None:
        if not self._matches_stream(instance_id):
            raise ValueError(
                "owner identity does not match this stream instance"
            )

    @staticmethod
    def _increment_bounded(value: int) -> int:
        return min(value + 1, MAX_SIGNED_64)

    @classmethod
    def _record_finality(
        cls,
        instance_id: OwnerInstanceId,
        entry: _OwnerFinalityEntry,
        outcome: OwnerRemovalOutcome,
    ) -> OwnerRemovalReceipt:
        if entry.final_receipt is None:
            entry.final_receipt = OwnerRemovalReceipt(
                instance_id, outcome
            )
        cls._synchronize_entry_state(entry)
        return canonical.canonical_owner_removal_receipt(entry.final_receipt)

    @staticmethod
    def _synchronize_entry_state(entry: _OwnerFinalityEntry) -> None:
        pending = entry.reservation_open or entry.queued or entry.claimed
        if entry.ack_receipt is not None:
            state = OwnerFinalityState.ACKNOWLEDGED
        elif pending and (
            entry.removal_requested or entry.final_receipt is not None
        ):
            state = OwnerFinalityState.REMOVAL_REQUESTED
        elif entry.final_receipt is not None:
            state = (
                OwnerFinalityState.FINAL_REMOVED
                if entry.final_receipt.outcome
                is OwnerRemovalOutcome.REMOVED
                else OwnerFinalityState.FINAL_ABSENT
            )
        elif entry.removal_requested:
            state = OwnerFinalityState.REMOVAL_REQUESTED
        elif entry.active:
            state = OwnerFinalityState.ACTIVE
        elif entry.prepare_resolved_absent:
            state = OwnerFinalityState.PREPARE_RESOLVED_ABSENT
        elif entry.queued or entry.claimed:
            state = OwnerFinalityState.PREPARE_PENDING
        else:
            state = OwnerFinalityState.PREPARE_RESERVED
        entry.state = state

    @staticmethod
    def _durable_progress_confirms(
        instance_id: OwnerInstanceId,
        positive_receipt: OwnerRemovalReceipt,
        progress: FinalityDurableProgress,
    ) -> bool:
        try:
            instance_id = canonical.canonical_owner_instance_id(instance_id)
            positive_receipt = canonical.canonical_owner_removal_receipt(
                positive_receipt)
            progress = canonical.canonical_durable_progress(progress)
        except (TypeError, ValueError):
            return False
        return (
            progress.removal_receipt == positive_receipt
            and progress.removal_receipt.confirms(instance_id)
            and progress.owner_signaled is True
            and progress.owner_joined is True
            and progress.late_owner_commands_resolved is True
            and progress.remove_retry_disabled is True
        )


__all__ = [
    "DEFAULT_OWNER_FINALITY_LIMIT",
    "FinalityDurableProgress",
    "OwnerFinalityEntrySnapshot",
    "OwnerFinalityLedger",
    "OwnerFinalitySnapshot",
    "OwnerFinalityState",
    "PrepareAdmission",
    "PrepareClaim",
    "PrepareConflictError",
]
