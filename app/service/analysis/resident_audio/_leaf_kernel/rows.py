"""Frozen row and diagnostic projections for the Resident ledger shell."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .values import (
    CallbackGeneration,
    CallbackSequence,
    GenerationId,
    LedgerRevision,
    RedactedIdentifier,
    RedactedIdentifierKind,
    _canonical_callback_generation,
    _canonical_callback_sequence,
    _canonical_generation_id,
    _canonical_redacted_identifier,
    _canonical_revision,
    _exact_enum,
    _required_field,
    _runtime_final,
    _signed_63,
)


class ShutdownEpochState(str, Enum):
    LATENT = "LATENT"


class LedgerDiagnosticCode(str, Enum):
    AUTHORITY_REJECTED = "AUTHORITY_REJECTED"
    DUPLICATE_EPOCH = "DUPLICATE_EPOCH"
    REVISION_EXHAUSTED = "REVISION_EXHAUSTED"


class SeededSlotState(str, Enum):
    ISSUED = "ISSUED"


class GenerationState(str, Enum):
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    FAILED_CONSTRUCTION = "FAILED_CONSTRUCTION"


class CallbackLaneState(str, Enum):
    ADMITTING = "ADMITTING"
    ADMISSION_CLOSED = "ADMISSION_CLOSED"
    QUIESCENT = "QUIESCENT"


class CallbackRejectionCode(str, Enum):
    BUSY = "BUSY"
    OVERLAP = "OVERLAP"


def _exact_bool(value: Any, owner: str, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{owner}.{field} must be an exact bool")
    return value


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class CallbackLaneRejected:
    callback_generation: CallbackGeneration
    code: CallbackRejectionCode

    def __post_init__(self) -> None:
        owner = type(self).__name__
        callback_generation = _canonical_callback_generation(
            _required_field(self, "callback_generation", owner),
            owner,
            "callback_generation",
        )
        code = _exact_enum(
            _required_field(self, "code", owner),
            CallbackRejectionCode,
            owner,
            "code",
        )
        object.__setattr__(
            self, "callback_generation", callback_generation
        )
        object.__setattr__(self, "code", code)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class CallbackLaneSnapshot:
    callback_generation: CallbackGeneration
    state: CallbackLaneState
    admitted_count: int
    in_flight_count: int
    returned_count: int
    last_admitted_sequence: CallbackSequence
    last_ingress_sequence: CallbackSequence
    last_returned_sequence: CallbackSequence

    def __post_init__(self) -> None:
        owner = type(self).__name__
        callback_generation = _canonical_callback_generation(
            _required_field(self, "callback_generation", owner),
            owner,
            "callback_generation",
        )
        state = _exact_enum(
            _required_field(self, "state", owner),
            CallbackLaneState,
            owner,
            "state",
        )
        admitted = _signed_63(
            _required_field(self, "admitted_count", owner),
            owner,
            "admitted_count",
        )
        in_flight = _signed_63(
            _required_field(self, "in_flight_count", owner),
            owner,
            "in_flight_count",
        )
        returned = _signed_63(
            _required_field(self, "returned_count", owner),
            owner,
            "returned_count",
        )
        last_admitted = _canonical_callback_sequence(
            _required_field(self, "last_admitted_sequence", owner),
            owner,
            "last_admitted_sequence",
        )
        last_ingress = _canonical_callback_sequence(
            _required_field(self, "last_ingress_sequence", owner),
            owner,
            "last_ingress_sequence",
        )
        last_returned = _canonical_callback_sequence(
            _required_field(self, "last_returned_sequence", owner),
            owner,
            "last_returned_sequence",
        )
        if in_flight not in (0, 1):
            raise ValueError(
                f"{owner}.in_flight_count must be 0 or 1"
            )
        if admitted != in_flight + returned:
            raise ValueError(
                f"{owner} counts must satisfy admitted = in-flight + returned"
            )
        if last_admitted.value != admitted:
            raise ValueError(
                f"{owner}.last_admitted_sequence must match admitted_count"
            )
        if last_ingress.value > admitted:
            raise ValueError(
                f"{owner}.last_ingress_sequence exceeds admitted_count"
            )
        if last_returned.value != returned:
            raise ValueError(
                f"{owner}.last_returned_sequence must match returned_count"
            )
        if state is CallbackLaneState.QUIESCENT and in_flight != 0:
            raise ValueError(f"{owner}.QUIESCENT requires zero in-flight")
        if (
            state is CallbackLaneState.ADMISSION_CLOSED
            and in_flight == 0
        ):
            raise ValueError(
                f"{owner}.ADMISSION_CLOSED requires in-flight callbacks"
            )
        object.__setattr__(
            self, "callback_generation", callback_generation
        )
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self, "last_admitted_sequence", last_admitted
        )
        object.__setattr__(self, "last_ingress_sequence", last_ingress)
        object.__setattr__(self, "last_returned_sequence", last_returned)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class CallbackQuiescenceOutcome:
    generation_id: GenerationId
    callback_generation: CallbackGeneration
    admission_closed_generation: CallbackGeneration
    in_flight_count: int
    last_admitted_sequence: CallbackSequence
    last_drained_ingress_sequence: CallbackSequence
    can_admit_again: bool

    def __post_init__(self) -> None:
        owner = type(self).__name__
        generation_id = _canonical_generation_id(
            _required_field(self, "generation_id", owner),
            owner,
            "generation_id",
        )
        callback_generation = _canonical_callback_generation(
            _required_field(self, "callback_generation", owner),
            owner,
            "callback_generation",
        )
        admission_closed_generation = _canonical_callback_generation(
            _required_field(self, "admission_closed_generation", owner),
            owner,
            "admission_closed_generation",
        )
        in_flight = _signed_63(
            _required_field(self, "in_flight_count", owner),
            owner,
            "in_flight_count",
        )
        last_admitted = _canonical_callback_sequence(
            _required_field(self, "last_admitted_sequence", owner),
            owner,
            "last_admitted_sequence",
        )
        last_ingress = _canonical_callback_sequence(
            _required_field(self, "last_drained_ingress_sequence", owner),
            owner,
            "last_drained_ingress_sequence",
        )
        can_admit = _exact_bool(
            _required_field(self, "can_admit_again", owner),
            owner,
            "can_admit_again",
        )
        if (
            callback_generation.generation_id != generation_id
            or admission_closed_generation.generation_id != generation_id
        ):
            raise ValueError(f"{owner} identities must use the same generation")
        if callback_generation != admission_closed_generation:
            raise ValueError(
                f"{owner} callback generations must match exactly"
            )
        if in_flight != 0 or can_admit:
            raise ValueError(
                f"{owner} requires closed zero-in-flight admission"
            )
        if last_ingress.value > last_admitted.value:
            raise ValueError(
                f"{owner}.last_drained_ingress_sequence is impossible"
            )
        object.__setattr__(self, "generation_id", generation_id)
        object.__setattr__(
            self, "callback_generation", callback_generation
        )
        object.__setattr__(
            self,
            "admission_closed_generation",
            admission_closed_generation,
        )
        object.__setattr__(
            self, "last_admitted_sequence", last_admitted
        )
        object.__setattr__(
            self, "last_drained_ingress_sequence", last_ingress
        )
        object.__setattr__(self, "can_admit_again", can_admit)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class GenerationRegistrationRowSnapshot:
    slot: RedactedIdentifier
    generation: RedactedIdentifier
    state: GenerationState
    revision: LedgerRevision

    def __post_init__(self) -> None:
        owner = type(self).__name__
        slot = _canonical_redacted_identifier(
            _required_field(self, "slot", owner),
            owner,
            "slot",
            RedactedIdentifierKind.SEEDED_SLOT,
        )
        generation = _canonical_redacted_identifier(
            _required_field(self, "generation", owner),
            owner,
            "generation",
            RedactedIdentifierKind.GENERATION,
        )
        state = _exact_enum(
            _required_field(self, "state", owner),
            GenerationState,
            owner,
            "state",
        )
        revision = _canonical_revision(
            _required_field(self, "revision", owner), owner
        )
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "revision", revision)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class ShutdownEpochRowSnapshot:
    epoch: RedactedIdentifier
    state: ShutdownEpochState
    revision: LedgerRevision

    def __post_init__(self) -> None:
        owner = type(self).__name__
        epoch = _canonical_redacted_identifier(
            _required_field(self, "epoch", owner),
            owner,
            "epoch",
            RedactedIdentifierKind.EPOCH,
        )
        state = _exact_enum(
            _required_field(self, "state", owner),
            ShutdownEpochState,
            owner,
            "state",
        )
        revision = _canonical_revision(
            _required_field(self, "revision", owner), owner
        )
        object.__setattr__(self, "epoch", epoch)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "revision", revision)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class LedgerDiagnostic:
    code: LedgerDiagnosticCode
    revision: LedgerRevision
    subject: RedactedIdentifier | None = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        code = _exact_enum(
            _required_field(self, "code", owner),
            LedgerDiagnosticCode,
            owner,
            "code",
        )
        revision = _canonical_revision(
            _required_field(self, "revision", owner), owner
        )
        raw_subject = _required_field(self, "subject", owner)
        subject = (
            None
            if raw_subject is None
            else _canonical_redacted_identifier(
                raw_subject, owner, "subject"
            )
        )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "subject", subject)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class LedgerSnapshot:
    revision: LedgerRevision
    shutdown_epochs: tuple[ShutdownEpochRowSnapshot, ...]
    generation_slots: tuple[GenerationRegistrationRowSnapshot, ...] = ()

    def __post_init__(self) -> None:
        owner = type(self).__name__
        revision = _canonical_revision(
            _required_field(self, "revision", owner), owner
        )
        raw_rows = _required_field(self, "shutdown_epochs", owner)
        if type(raw_rows) is not tuple:
            raise TypeError(f"{owner}.shutdown_epochs must be an exact tuple")
        if len(raw_rows) > 1:
            raise ValueError(f"{owner} permits only one shutdown epoch")
        rows: list[ShutdownEpochRowSnapshot] = []
        for raw_row in raw_rows:
            if (
                type(raw_row) is not ShutdownEpochRowSnapshot
                or not ShutdownEpochRowSnapshot._is_constructor_issued(
                    raw_row
                )
            ):
                raise TypeError(
                    f"{owner}.shutdown_epochs items must use the exact "
                    "snapshot constructor"
                )
            row = ShutdownEpochRowSnapshot(
                _required_field(
                    raw_row, "epoch", ShutdownEpochRowSnapshot.__name__
                ),
                _required_field(
                    raw_row, "state", ShutdownEpochRowSnapshot.__name__
                ),
                _required_field(
                    raw_row, "revision", ShutdownEpochRowSnapshot.__name__
                ),
            )
            if row.revision.value > revision.value:
                raise ValueError(
                    f"{owner}.shutdown_epochs cannot exceed revision"
                )
            rows.append(row)
        raw_generation_rows = _required_field(
            self, "generation_slots", owner
        )
        if type(raw_generation_rows) is not tuple:
            raise TypeError(
                f"{owner}.generation_slots must be an exact tuple"
            )
        if len(raw_generation_rows) > 6:
            raise ValueError(f"{owner} permits only six generation slots")
        generation_rows: list[GenerationRegistrationRowSnapshot] = []
        for raw_row in raw_generation_rows:
            if (
                type(raw_row) is not GenerationRegistrationRowSnapshot
                or not GenerationRegistrationRowSnapshot._is_constructor_issued(
                    raw_row
                )
            ):
                raise TypeError(
                    f"{owner}.generation_slots items must use the exact "
                    "snapshot constructor"
                )
            row = GenerationRegistrationRowSnapshot(
                _required_field(
                    raw_row,
                    "slot",
                    GenerationRegistrationRowSnapshot.__name__,
                ),
                _required_field(
                    raw_row,
                    "generation",
                    GenerationRegistrationRowSnapshot.__name__,
                ),
                _required_field(
                    raw_row,
                    "state",
                    GenerationRegistrationRowSnapshot.__name__,
                ),
                _required_field(
                    raw_row,
                    "revision",
                    GenerationRegistrationRowSnapshot.__name__,
                ),
            )
            if row.revision.value > revision.value:
                raise ValueError(
                    f"{owner}.generation_slots cannot exceed revision"
                )
            generation_rows.append(row)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "shutdown_epochs", tuple(rows))
        object.__setattr__(
            self, "generation_slots", tuple(generation_rows)
        )
