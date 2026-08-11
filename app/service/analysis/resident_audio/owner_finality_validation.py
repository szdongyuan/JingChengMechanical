"""Total, recursive validation for owner-finality value objects."""

from __future__ import annotations

import math
from enum import Enum
from typing import Callable

from .owner_finality_types import (
    FinalityDurableProgress,
    OwnerFinalityEntrySnapshot,
    OwnerFinalityState,
    PrepareAdmission,
    PrepareClaim,
)
from .owner_identity import (
    FinalityAckReceipt,
    FinalityPruneOutcome,
    FinalityPruneReceipt,
    MAX_SIGNED_64,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerNonAdmissionReason,
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
    StreamInstanceId,
)


MAX_IDENTITY_STRING_LENGTH = 256
MAX_ACK_TOKEN_LENGTH = 256
MAX_DIAGNOSTIC_ITEMS = 8
MAX_DIAGNOSTIC_KEY_LENGTH = 64
MAX_DIAGNOSTIC_STRING_LENGTH = 256


def require_field(value: object, field: str, owner: str) -> object:
    try:
        return object.__getattribute__(value, field)
    except AttributeError:
        raise TypeError(f"{owner}.{field} is required") from None


def require_exact(value: object, expected: type, owner: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{owner} requires an exact {expected.__name__}")


def require_enum_member(
    value: object, enum_type: type[Enum], owner: str
) -> None:
    require_exact(value, enum_type, owner)
    if not any(value is member for member in enum_type.__members__.values()):
        raise TypeError(f"{owner} requires a canonical {enum_type.__name__}")


def require_text(value: object, owner: str, field: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{owner}.{field} must be a string")
    if not value or len(value) > MAX_IDENTITY_STRING_LENGTH:
        raise TypeError(f"{owner}.{field} must be bounded and non-empty")


def require_scalar_id(value: object, owner: str, field: str) -> None:
    if type(value) is str:
        require_text(value, owner, field)
    elif type(value) is int:
        require_bounded_integer(value, owner, field, 0)
    else:
        raise TypeError(f"{owner}.{field} must be a string or integer")


def require_bounded_integer(
    value: object, owner: str, field: str, minimum: int
) -> None:
    if type(value) is not int:
        raise TypeError(f"{owner}.{field} must be an integer")
    if not minimum <= value <= MAX_SIGNED_64:
        raise ValueError(
            f"{owner}.{field} must be in {minimum}..{MAX_SIGNED_64}"
        )


def require_bool(value: object, owner: str, field: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{owner}.{field} must be an exact bool")


def _validate_fields(
    value: object,
    exact_type: type,
    validators: tuple[tuple[str, Callable[[object], None]], ...],
) -> tuple[object, ...]:
    owner = exact_type.__name__
    require_exact(value, exact_type, owner)
    captured = tuple(
        require_field(value, field, owner) for field, _ in validators
    )
    for (_, validate), field_value in zip(validators, captured):
        validate(field_value)
    return captured


def _nested(
    validator: Callable[[object], None], owner: str, field: str
) -> Callable[[object], None]:
    def validate(value: object) -> None:
        try:
            validator(value)
        except TypeError as exc:
            raise TypeError(f"{owner}.{field} is malformed: {exc}") from None
        except ValueError as exc:
            raise ValueError(f"{owner}.{field} is malformed: {exc}") from None

    return validate


def validate_stream_instance_id(value: object) -> None:
    owner = StreamInstanceId.__name__
    _validate_fields(
        value,
        StreamInstanceId,
        (
            (
                "pool_epoch",
                lambda item: require_scalar_id(
                    item, owner, "pool_epoch"
                ),
            ),
            (
                "hardware_id",
                lambda item: require_text(item, owner, "hardware_id"),
            ),
            (
                "stream_generation",
                lambda item: require_bounded_integer(
                    item, owner, "stream_generation", 0
                ),
            ),
            (
                "instance_nonce",
                lambda item: require_text(item, owner, "instance_nonce"),
            ),
        ),
    )


def validate_owner_instance_id(value: object) -> None:
    owner = OwnerInstanceId.__name__
    _validate_fields(
        value,
        OwnerInstanceId,
        (
            (
                "pool_epoch",
                lambda item: require_scalar_id(
                    item, owner, "pool_epoch"
                ),
            ),
            (
                "hardware_id",
                lambda item: require_text(item, owner, "hardware_id"),
            ),
            (
                "stream_generation",
                lambda item: require_bounded_integer(
                    item, owner, "stream_generation", 0
                ),
            ),
            ("run_id", lambda item: require_text(item, owner, "run_id")),
            (
                "commit_attempt_id",
                lambda item: require_scalar_id(
                    item, owner, "commit_attempt_id"
                ),
            ),
            (
                "owner_capability",
                lambda item: require_text(
                    item, owner, "owner_capability"
                ),
            ),
            (
                "instance_nonce",
                lambda item: require_text(item, owner, "instance_nonce"),
            ),
        ),
    )


def validate_diagnostic_metadata(value: object) -> None:
    owner = OwnerRemovalReceipt.__name__
    if type(value) is not tuple:
        raise TypeError(f"{owner}.diagnostic_metadata must be an exact tuple")
    if len(value) > MAX_DIAGNOSTIC_ITEMS:
        raise ValueError(
            f"diagnostic metadata accepts at most {MAX_DIAGNOSTIC_ITEMS} items"
        )
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("diagnostic metadata items must be exact pairs")
        key, scalar = item
        if type(key) is not str:
            raise TypeError("diagnostic metadata keys must be strings")
        if len(key) > MAX_DIAGNOSTIC_KEY_LENGTH:
            raise ValueError("diagnostic metadata key is too long")
        if type(scalar) not in (str, int, float, bool, type(None)):
            raise TypeError("diagnostic metadata values must be scalar")
        if (
            type(scalar) is str
            and len(scalar) > MAX_DIAGNOSTIC_STRING_LENGTH
        ):
            raise ValueError("diagnostic metadata string is too long")
        if type(scalar) is int and not (
            -MAX_SIGNED_64 <= scalar <= MAX_SIGNED_64
        ):
            raise ValueError("diagnostic metadata integer is out of bounds")
        if type(scalar) is float and not math.isfinite(scalar):
            raise ValueError("diagnostic metadata float must be finite")


def validate_owner_removal_receipt(value: object) -> None:
    owner = OwnerRemovalReceipt.__name__
    _validate_fields(
        value,
        OwnerRemovalReceipt,
        (
            (
                "owner_instance_id",
                _nested(
                    validate_owner_instance_id,
                    owner,
                    "owner_instance_id",
                ),
            ),
            (
                "outcome",
                lambda item: require_enum_member(
                    item, OwnerRemovalOutcome, f"{owner}.outcome"
                ),
            ),
            ("diagnostic_metadata", validate_diagnostic_metadata),
        ),
    )


def validate_owner_non_admission_proof(value: object) -> None:
    owner = OwnerNonAdmissionProof.__name__
    _validate_fields(
        value,
        OwnerNonAdmissionProof,
        (
            (
                "owner_instance_id",
                _nested(
                    validate_owner_instance_id,
                    owner,
                    "owner_instance_id",
                ),
            ),
            (
                "reason",
                lambda item: require_enum_member(
                    item, OwnerNonAdmissionReason, f"{owner}.reason"
                ),
            ),
            (
                "command_enqueued",
                lambda item: require_bool(
                    item, owner, "command_enqueued"
                ),
            ),
            (
                "command_claimed",
                lambda item: require_bool(
                    item, owner, "command_claimed"
                ),
            ),
        ),
    )


def validate_finality_ack_receipt(value: object) -> None:
    owner = FinalityAckReceipt.__name__
    _validate_fields(
        value,
        FinalityAckReceipt,
        (
            (
                "stream_instance_id",
                _nested(
                    validate_stream_instance_id,
                    owner,
                    "stream_instance_id",
                ),
            ),
            (
                "owner_instance_id",
                _nested(
                    validate_owner_instance_id,
                    owner,
                    "owner_instance_id",
                ),
            ),
            (
                "ledger_version",
                lambda item: require_bounded_integer(
                    item, owner, "ledger_version", 1
                ),
            ),
            (
                "ack_token",
                lambda item: _require_ack_token(item, owner),
            ),
        ),
    )


def _require_ack_token(value: object, owner: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{owner}.ack_token must be a string")
    if not value or len(value) > MAX_ACK_TOKEN_LENGTH:
        raise ValueError(f"{owner}.ack_token must be bounded and non-empty")


def validate_finality_prune_receipt(value: object) -> None:
    owner = FinalityPruneReceipt.__name__
    _validate_fields(
        value,
        FinalityPruneReceipt,
        (
            (
                "owner_instance_id",
                _nested(
                    validate_owner_instance_id,
                    owner,
                    "owner_instance_id",
                ),
            ),
            (
                "outcome",
                lambda item: require_enum_member(
                    item, FinalityPruneOutcome, f"{owner}.outcome"
                ),
            ),
        ),
    )


def validate_prepare_admission(value: object) -> None:
    owner = PrepareAdmission.__name__
    _validate_fields(
        value,
        PrepareAdmission,
        (
            (
                "owner_instance_id",
                _nested(
                    validate_owner_instance_id,
                    owner,
                    "owner_instance_id",
                ),
            ),
            (
                "ledger_version",
                lambda item: require_bounded_integer(
                    item, owner, "ledger_version", 1
                ),
            ),
        ),
    )


def validate_prepare_claim(value: object) -> None:
    owner = PrepareClaim.__name__
    _validate_fields(
        value,
        PrepareClaim,
        (
            (
                "owner_instance_id",
                _nested(
                    validate_owner_instance_id,
                    owner,
                    "owner_instance_id",
                ),
            ),
            (
                "ledger_version",
                lambda item: require_bounded_integer(
                    item, owner, "ledger_version", 1
                ),
            ),
            (
                "activation_allowed",
                lambda item: require_bool(
                    item, owner, "activation_allowed"
                ),
            ),
        ),
    )


def validate_finality_durable_progress(value: object) -> None:
    owner = FinalityDurableProgress.__name__

    def validate_receipt(item: object) -> None:
        if item is not None:
            _nested(
                validate_owner_removal_receipt,
                owner,
                "removal_receipt",
            )(item)

    _validate_fields(
        value,
        FinalityDurableProgress,
        (
            ("removal_receipt", validate_receipt),
            (
                "owner_signaled",
                lambda item: require_bool(item, owner, "owner_signaled"),
            ),
            (
                "owner_joined",
                lambda item: require_bool(item, owner, "owner_joined"),
            ),
            (
                "late_owner_commands_resolved",
                lambda item: require_bool(
                    item, owner, "late_owner_commands_resolved"
                ),
            ),
            (
                "remove_retry_disabled",
                lambda item: require_bool(
                    item, owner, "remove_retry_disabled"
                ),
            ),
        ),
    )


def validate_owner_finality_entry_snapshot(value: object) -> None:
    owner = OwnerFinalityEntrySnapshot.__name__
    captured = _validate_fields(
        value,
        OwnerFinalityEntrySnapshot,
        (
            (
                "pool_epoch",
                lambda item: require_scalar_id(
                    item, owner, "pool_epoch"
                ),
            ),
            (
                "hardware_id",
                lambda item: require_text(item, owner, "hardware_id"),
            ),
            (
                "stream_generation",
                lambda item: require_bounded_integer(
                    item, owner, "stream_generation", 0
                ),
            ),
            ("run_id", lambda item: require_text(item, owner, "run_id")),
            (
                "commit_attempt_id",
                lambda item: require_scalar_id(
                    item, owner, "commit_attempt_id"
                ),
            ),
            (
                "instance_nonce",
                lambda item: require_text(item, owner, "instance_nonce"),
            ),
            (
                "state",
                lambda item: require_enum_member(
                    item, OwnerFinalityState, f"{owner}.state"
                ),
            ),
            (
                "prepare_pending",
                lambda item: require_bool(item, owner, "prepare_pending"),
            ),
        ),
    )
    state = captured[6]
    pending = captured[7]
    pending_states = (
        OwnerFinalityState.PREPARE_RESERVED,
        OwnerFinalityState.PREPARE_PENDING,
    )
    nonpending_states = (
        OwnerFinalityState.ACTIVE,
        OwnerFinalityState.PREPARE_RESOLVED_ABSENT,
        OwnerFinalityState.FINAL_REMOVED,
        OwnerFinalityState.FINAL_ABSENT,
        OwnerFinalityState.ACKNOWLEDGED,
    )
    if any(state is member for member in pending_states) and not pending:
        raise ValueError("state requires prepare_pending=True")
    if any(state is member for member in nonpending_states) and pending:
        raise ValueError("state requires prepare_pending=False")
