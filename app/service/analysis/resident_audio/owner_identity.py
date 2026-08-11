"""Immutable, owner-free identities and finality receipts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


IdentityScalar: TypeAlias = str | int
DiagnosticScalar: TypeAlias = str | int | float | bool | None
DiagnosticMetadata: TypeAlias = tuple[tuple[str, DiagnosticScalar], ...]

_MAX_DIAGNOSTIC_ITEMS = 8
_MAX_DIAGNOSTIC_KEY_LENGTH = 64
_MAX_DIAGNOSTIC_STRING_LENGTH = 256
_MAX_IDENTITY_STRING_LENGTH = 256
_MAX_ACK_TOKEN_LENGTH = 256
MAX_SIGNED_64 = (1 << 63) - 1


def _require_text(value: object, *, owner: str, field: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_IDENTITY_STRING_LENGTH
    ):
        raise TypeError(f"{owner}.{field} must be a bounded non-empty string")


def _require_scalar_id(value: object, *, owner: str, field: str) -> None:
    if type(value) not in (str, int):
        raise TypeError(f"{owner}.{field} must be a string or integer")
    if type(value) is str:
        _require_text(value, owner=owner, field=field)
    else:
        _require_bounded_integer(
            value, owner=owner, field=field, minimum=0
        )


def _require_bounded_integer(
    value: object,
    *,
    owner: str,
    field: str,
    minimum: int,
) -> None:
    if type(value) is not int:
        raise TypeError(f"{owner}.{field} must be an integer")
    if value < minimum or value > MAX_SIGNED_64:
        raise ValueError(
            f"{owner}.{field} must be in {minimum}..{MAX_SIGNED_64}"
        )


def _require_generation(value: object, *, owner: str) -> None:
    _require_bounded_integer(
        value,
        owner=owner,
        field="stream_generation",
        minimum=0,
    )


def _require_positive_ledger_version(
    value: object, *, owner: str
) -> None:
    _require_bounded_integer(
        value,
        owner=owner,
        field="ledger_version",
        minimum=1,
    )


@dataclass(frozen=True, slots=True)
class OwnerInstanceId:
    """Unique value identity for one owner commit attempt."""

    pool_epoch: IdentityScalar
    hardware_id: str
    stream_generation: int
    run_id: str
    commit_attempt_id: IdentityScalar
    owner_capability: str
    instance_nonce: str

    def __post_init__(self) -> None:
        from .owner_finality_validation import validate_owner_instance_id

        validate_owner_instance_id(self)


@dataclass(frozen=True, slots=True)
class StreamInstanceId:
    """Unique value identity for one resident stream lifetime."""

    pool_epoch: IdentityScalar
    hardware_id: str
    stream_generation: int
    instance_nonce: str

    def __post_init__(self) -> None:
        from .owner_finality_validation import validate_stream_instance_id

        validate_stream_instance_id(self)


class OwnerRemovalOutcome(str, Enum):
    REMOVED = "REMOVED"
    TOMBSTONED_ABSENT = "TOMBSTONED_ABSENT"
    MISMATCH = "MISMATCH"
    INDETERMINATE = "INDETERMINATE"


def _validate_diagnostic_metadata(metadata: DiagnosticMetadata) -> None:
    from .owner_finality_validation import validate_diagnostic_metadata

    validate_diagnostic_metadata(metadata)


@dataclass(frozen=True, slots=True)
class OwnerRemovalReceipt:
    owner_instance_id: OwnerInstanceId
    outcome: OwnerRemovalOutcome
    diagnostic_metadata: DiagnosticMetadata = ()

    def __post_init__(self) -> None:
        from .owner_finality_canonical import canonical_owner_instance_id
        from .owner_finality_validation import (
            validate_owner_removal_receipt,
        )

        validate_owner_removal_receipt(self)
        object.__setattr__(
            self,
            "owner_instance_id",
            canonical_owner_instance_id(self.owner_instance_id),
        )

    @property
    def positive_finality(self) -> bool:
        from .owner_finality_canonical import (
            canonical_owner_removal_receipt,
        )

        try:
            receipt = canonical_owner_removal_receipt(self)
        except (TypeError, ValueError):
            return False
        return receipt.outcome in (
            OwnerRemovalOutcome.REMOVED,
            OwnerRemovalOutcome.TOMBSTONED_ABSENT,
        )

    def confirms(self, expected: OwnerInstanceId) -> bool:
        from .owner_finality_canonical import (
            canonical_owner_instance_id,
            canonical_owner_removal_receipt,
        )

        try:
            receipt = canonical_owner_removal_receipt(self)
            expected_copy = canonical_owner_instance_id(expected)
        except (TypeError, ValueError):
            return False
        return (
            receipt.outcome
            in (
                OwnerRemovalOutcome.REMOVED,
                OwnerRemovalOutcome.TOMBSTONED_ABSENT,
            )
            and receipt.owner_instance_id == expected_copy
        )

    def __bool__(self) -> bool:
        return self.positive_finality


class OwnerNonAdmissionReason(str, Enum):
    LEDGER_FULL = "LEDGER_FULL"


@dataclass(frozen=True, slots=True)
class OwnerNonAdmissionProof:
    owner_instance_id: OwnerInstanceId
    reason: OwnerNonAdmissionReason
    command_enqueued: bool
    command_claimed: bool

    def __post_init__(self) -> None:
        from .owner_finality_canonical import canonical_owner_instance_id
        from .owner_finality_validation import (
            validate_owner_non_admission_proof,
        )

        validate_owner_non_admission_proof(self)
        object.__setattr__(
            self,
            "owner_instance_id",
            canonical_owner_instance_id(self.owner_instance_id),
        )

    def confirms(self, expected: OwnerInstanceId) -> bool:
        from .owner_finality_canonical import (
            canonical_non_admission_proof,
            canonical_owner_instance_id,
        )

        try:
            proof = canonical_non_admission_proof(self)
            expected_copy = canonical_owner_instance_id(expected)
        except (TypeError, ValueError):
            return False
        return (
            proof.owner_instance_id == expected_copy
            and proof.reason is OwnerNonAdmissionReason.LEDGER_FULL
            and proof.command_enqueued is False
            and proof.command_claimed is False
        )


@dataclass(frozen=True, slots=True)
class FinalityAckReceipt:
    stream_instance_id: StreamInstanceId
    owner_instance_id: OwnerInstanceId
    ledger_version: int
    ack_token: str

    def __post_init__(self) -> None:
        from .owner_finality_canonical import (
            canonical_owner_instance_id,
            canonical_stream_instance_id,
        )
        from .owner_finality_validation import (
            validate_finality_ack_receipt,
        )

        validate_finality_ack_receipt(self)
        object.__setattr__(
            self,
            "stream_instance_id",
            canonical_stream_instance_id(self.stream_instance_id),
        )
        object.__setattr__(
            self,
            "owner_instance_id",
            canonical_owner_instance_id(self.owner_instance_id),
        )


class FinalityPruneOutcome(str, Enum):
    PRUNED = "PRUNED"
    ALREADY_PRUNED = "ALREADY_PRUNED"
    MISMATCH = "MISMATCH"


@dataclass(frozen=True, slots=True)
class FinalityPruneReceipt:
    owner_instance_id: OwnerInstanceId
    outcome: FinalityPruneOutcome

    def __post_init__(self) -> None:
        from .owner_finality_canonical import canonical_owner_instance_id
        from .owner_finality_validation import (
            validate_finality_prune_receipt,
        )

        validate_finality_prune_receipt(self)
        object.__setattr__(
            self,
            "owner_instance_id",
            canonical_owner_instance_id(self.owner_instance_id),
        )

    def confirms(self, expected: OwnerInstanceId) -> bool:
        from .owner_finality_canonical import (
            canonical_finality_prune_receipt,
            canonical_owner_instance_id,
        )

        try:
            receipt = canonical_finality_prune_receipt(self)
            expected_copy = canonical_owner_instance_id(expected)
        except (TypeError, ValueError):
            return False
        return (
            receipt.owner_instance_id == expected_copy
            and receipt.outcome
            in (
                FinalityPruneOutcome.PRUNED,
                FinalityPruneOutcome.ALREADY_PRUNED,
            )
        )


__all__ = [
    "DiagnosticMetadata",
    "FinalityAckReceipt",
    "FinalityPruneOutcome",
    "FinalityPruneReceipt",
    "MAX_SIGNED_64",
    "OwnerInstanceId",
    "OwnerNonAdmissionProof",
    "OwnerNonAdmissionReason",
    "OwnerRemovalOutcome",
    "OwnerRemovalReceipt",
    "StreamInstanceId",
]
