"""Validated canonical copies for owner-finality value boundaries."""

from __future__ import annotations

from .owner_finality_types import (
    FinalityDurableProgress,
    OwnerFinalityEntrySnapshot,
)
from .owner_finality_validation import require_exact, require_field
from .owner_identity import (
    FinalityAckReceipt,
    FinalityPruneReceipt,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerRemovalReceipt,
    StreamInstanceId,
)


def canonical_stream_instance_id(value: object) -> StreamInstanceId:
    owner = StreamInstanceId.__name__
    require_exact(value, StreamInstanceId, owner)
    captured = tuple(
        require_field(value, field, owner)
        for field in (
            "pool_epoch",
            "hardware_id",
            "stream_generation",
            "instance_nonce",
        )
    )
    return StreamInstanceId(*captured)


def canonical_owner_instance_id(value: object) -> OwnerInstanceId:
    owner = OwnerInstanceId.__name__
    require_exact(value, OwnerInstanceId, owner)
    captured = tuple(
        require_field(value, field, owner)
        for field in (
            "pool_epoch",
            "hardware_id",
            "stream_generation",
            "run_id",
            "commit_attempt_id",
            "owner_capability",
            "instance_nonce",
        )
    )
    return OwnerInstanceId(*captured)


def canonical_owner_removal_receipt(value: object) -> OwnerRemovalReceipt:
    owner = OwnerRemovalReceipt.__name__
    require_exact(value, OwnerRemovalReceipt, owner)
    identity = require_field(value, "owner_instance_id", owner)
    outcome = require_field(value, "outcome", owner)
    metadata = require_field(value, "diagnostic_metadata", owner)
    return OwnerRemovalReceipt(
        canonical_owner_instance_id(identity), outcome, metadata
    )


def canonical_non_admission_proof(value: object) -> OwnerNonAdmissionProof:
    owner = OwnerNonAdmissionProof.__name__
    require_exact(value, OwnerNonAdmissionProof, owner)
    identity = require_field(value, "owner_instance_id", owner)
    reason = require_field(value, "reason", owner)
    enqueued = require_field(value, "command_enqueued", owner)
    claimed = require_field(value, "command_claimed", owner)
    return OwnerNonAdmissionProof(
        canonical_owner_instance_id(identity), reason, enqueued, claimed
    )


def canonical_finality_ack_receipt(value: object) -> FinalityAckReceipt:
    owner = FinalityAckReceipt.__name__
    require_exact(value, FinalityAckReceipt, owner)
    stream = require_field(value, "stream_instance_id", owner)
    identity = require_field(value, "owner_instance_id", owner)
    version = require_field(value, "ledger_version", owner)
    token = require_field(value, "ack_token", owner)
    return FinalityAckReceipt(
        canonical_stream_instance_id(stream),
        canonical_owner_instance_id(identity),
        version,
        token,
    )


def canonical_finality_prune_receipt(value: object) -> FinalityPruneReceipt:
    owner = FinalityPruneReceipt.__name__
    require_exact(value, FinalityPruneReceipt, owner)
    identity = require_field(value, "owner_instance_id", owner)
    outcome = require_field(value, "outcome", owner)
    return FinalityPruneReceipt(
        canonical_owner_instance_id(identity), outcome
    )


def canonical_durable_progress(value: object) -> FinalityDurableProgress:
    owner = FinalityDurableProgress.__name__
    require_exact(value, FinalityDurableProgress, owner)
    receipt = require_field(value, "removal_receipt", owner)
    signaled = require_field(value, "owner_signaled", owner)
    joined = require_field(value, "owner_joined", owner)
    late = require_field(value, "late_owner_commands_resolved", owner)
    disabled = require_field(value, "remove_retry_disabled", owner)
    return FinalityDurableProgress(
        None
        if receipt is None
        else canonical_owner_removal_receipt(receipt),
        signaled,
        joined,
        late,
        disabled,
    )


def canonical_entry_snapshot(value: object) -> OwnerFinalityEntrySnapshot:
    owner = OwnerFinalityEntrySnapshot.__name__
    require_exact(value, OwnerFinalityEntrySnapshot, owner)
    captured = tuple(
        require_field(value, field, owner)
        for field in (
            "pool_epoch",
            "hardware_id",
            "stream_generation",
            "run_id",
            "commit_attempt_id",
            "instance_nonce",
            "state",
            "prepare_pending",
        )
    )
    return OwnerFinalityEntrySnapshot(*captured)
