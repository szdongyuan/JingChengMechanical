"""Strict private values for the dormant Resident lifecycle ledger."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from weakref import WeakValueDictionary

MAX_SIGNED_63 = (1 << 63) - 1
_MAX_OPAQUE_ID_LENGTH = 128
_OPAQUE_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_.:"
)


def _runtime_final(exact_type: type[Any]) -> type[Any]:
    pending: WeakValueDictionary[int, Any] = WeakValueDictionary()
    initialized: WeakValueDictionary[int, Any] = WeakValueDictionary()
    initializing: set[int] = set()
    original_init = exact_type.__init__
    original_post_init = getattr(exact_type, "__post_init__", None)

    def constructor_new(
        subclass: type[Any], *args: Any, **kwargs: Any
    ) -> Any:
        del args, kwargs
        if subclass is not exact_type:
            raise TypeError(f"{exact_type.__name__} is final")
        instance = super(exact_type, subclass).__new__(subclass)
        pending[id(instance)] = instance
        return instance

    def is_constructor_issued(value: Any) -> bool:
        return initialized.get(id(value)) is value

    def guarded_init(instance: Any, *args: Any, **kwargs: Any) -> None:
        instance_id = id(instance)
        if pending.get(instance_id) is not instance:
            raise TypeError(
                f"{exact_type.__name__} requires its exact constructor"
            )
        initializing.add(instance_id)
        try:
            original_init(instance, *args, **kwargs)
        finally:
            initializing.discard(instance_id)
            pending.pop(instance_id, None)

    def guarded_post_init(instance: Any) -> None:
        instance_id = id(instance)
        if (
            pending.get(instance_id) is not instance
            or instance_id not in initializing
        ):
            raise TypeError(
                f"{exact_type.__name__} requires its exact constructor"
            )
        original_post_init(instance)
        initialized[instance_id] = instance

    def reject_subclass(subclass: type[Any], **kwargs: Any) -> None:
        del kwargs
        raise TypeError(f"{exact_type.__name__} is final")

    if original_post_init is not None:
        exact_type.__new__ = staticmethod(constructor_new)
        exact_type.__init__ = guarded_init
        exact_type.__post_init__ = guarded_post_init
        exact_type._is_constructor_issued = staticmethod(
            is_constructor_issued
        )
    exact_type.__init_subclass__ = classmethod(reject_subclass)
    return exact_type


def _required_field(instance: Any, field: str, owner: str) -> Any:
    try:
        return getattr(instance, field)
    except AttributeError as exc:
        raise TypeError(f"{owner}.{field} is required") from exc


def _opaque_id(value: Any, owner: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{owner}.value must be an exact string")
    if not value or len(value) > _MAX_OPAQUE_ID_LENGTH:
        raise ValueError(f"{owner}.value must be non-empty and bounded")
    if any(character not in _OPAQUE_ID_CHARACTERS for character in value):
        raise ValueError(f"{owner}.value contains an unsafe character")
    return value


def _signed_63(value: Any, owner: str, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{owner}.{field} must be an exact integer")
    if not 0 <= value <= MAX_SIGNED_63:
        raise ValueError(
            f"{owner}.{field} must be in 0..{MAX_SIGNED_63}"
        )
    return value


def _positive_signed_63(value: Any, owner: str, field: str) -> int:
    parsed = _signed_63(value, owner, field)
    if parsed == 0:
        raise ValueError(f"{owner}.{field} must be positive")
    return parsed


def _exact_enum(
    value: Any, enum_type: type[Enum], owner: str, field: str
) -> Enum:
    if type(value) is not enum_type or not any(
        value is member for member in enum_type
    ):
        raise TypeError(
            f"{owner}.{field} must be an exact {enum_type.__name__}"
        )
    return value


def _canonical_opaque_id(
    value: Any, exact_type: type[Any], owner: str, field: str
) -> Any:
    if (
        type(value) is not exact_type
        or not exact_type._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact "
            f"{exact_type.__name__} constructor"
        )
    raw = _required_field(value, "value", exact_type.__name__)
    return exact_type(_opaque_id(raw, exact_type.__name__))


def _canonical_revision(
    value: Any, owner: str, field: str = "revision"
) -> LedgerRevision:
    if (
        type(value) is not LedgerRevision
        or not LedgerRevision._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact LedgerRevision constructor"
        )
    raw = _required_field(value, "value", LedgerRevision.__name__)
    return LedgerRevision(_signed_63(raw, LedgerRevision.__name__, "value"))


class _RedactedOpaqueId:
    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class ShutdownEpochId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class ShutdownAttemptId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class ExecutionId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class ProducerId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class AdmissionIntentId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class BarrierBatchId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class QuarantineId(_RedactedOpaqueId):
    value: str

    def __post_init__(self) -> None:
        _opaque_id(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
        )


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class LedgerRevision:
    value: int

    def __post_init__(self) -> None:
        _signed_63(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
            "value",
        )

    def next(self) -> LedgerRevision:
        if not type(self)._is_constructor_issued(self):
            raise TypeError(
                "LedgerRevision requires its exact constructor"
            )
        value = _signed_63(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
            "value",
        )
        if value == MAX_SIGNED_63:
            raise OverflowError("ledger revision exhausted")
        return LedgerRevision(value + 1)


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class AudioBackendEpoch:
    value: int

    def __post_init__(self) -> None:
        _signed_63(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
            "value",
        )

    def __repr__(self) -> str:
        return "AudioBackendEpoch(<redacted>)"


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class SeededSlotId(_RedactedOpaqueId):
    value: int

    def __post_init__(self) -> None:
        value = _positive_signed_63(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
            "value",
        )
        if value > 6:
            raise ValueError("SeededSlotId.value must be in 1..6")


def _canonical_seeded_slot_id(
    value: Any, owner: str, field: str
) -> SeededSlotId:
    if (
        type(value) is not SeededSlotId
        or not SeededSlotId._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact SeededSlotId constructor"
        )
    raw = _required_field(value, "value", SeededSlotId.__name__)
    return SeededSlotId(
        _positive_signed_63(raw, SeededSlotId.__name__, "value")
    )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class GenerationId(_RedactedOpaqueId):
    slot_id: SeededSlotId
    sequence: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        slot_id = _canonical_seeded_slot_id(
            _required_field(self, "slot_id", owner), owner, "slot_id"
        )
        sequence = _positive_signed_63(
            _required_field(self, "sequence", owner), owner, "sequence"
        )
        object.__setattr__(self, "slot_id", slot_id)
        object.__setattr__(self, "sequence", sequence)


def _canonical_generation_id(
    value: Any, owner: str, field: str
) -> GenerationId:
    if (
        type(value) is not GenerationId
        or not GenerationId._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact GenerationId constructor"
        )
    return GenerationId(
        _required_field(value, "slot_id", GenerationId.__name__),
        _positive_signed_63(
            _required_field(value, "sequence", GenerationId.__name__),
            GenerationId.__name__,
            "sequence",
        ),
    )


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class SampleCursor:
    value: int

    def __post_init__(self) -> None:
        _signed_63(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
            "value",
        )


def _canonical_sample_cursor(
    value: Any, owner: str, field: str
) -> SampleCursor:
    if (
        type(value) is not SampleCursor
        or not SampleCursor._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact SampleCursor constructor"
        )
    return SampleCursor(
        _signed_63(
            _required_field(value, "value", SampleCursor.__name__),
            SampleCursor.__name__,
            "value",
        )
    )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class CallbackGeneration(_RedactedOpaqueId):
    generation_id: GenerationId
    sequence: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        generation_id = _canonical_generation_id(
            _required_field(self, "generation_id", owner),
            owner,
            "generation_id",
        )
        sequence = _positive_signed_63(
            _required_field(self, "sequence", owner), owner, "sequence"
        )
        object.__setattr__(self, "generation_id", generation_id)
        object.__setattr__(self, "sequence", sequence)


def _canonical_callback_generation(
    value: Any, owner: str, field: str
) -> CallbackGeneration:
    if (
        type(value) is not CallbackGeneration
        or not CallbackGeneration._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact "
            "CallbackGeneration constructor"
        )
    return CallbackGeneration(
        _required_field(
            value, "generation_id", CallbackGeneration.__name__
        ),
        _positive_signed_63(
            _required_field(value, "sequence", CallbackGeneration.__name__),
            CallbackGeneration.__name__,
            "sequence",
        ),
    )


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class CallbackSequence:
    value: int

    def __post_init__(self) -> None:
        _signed_63(
            _required_field(self, "value", type(self).__name__),
            type(self).__name__,
            "value",
        )


def _canonical_callback_sequence(
    value: Any, owner: str, field: str
) -> CallbackSequence:
    if (
        type(value) is not CallbackSequence
        or not CallbackSequence._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact "
            "CallbackSequence constructor"
        )
    return CallbackSequence(
        _signed_63(
            _required_field(value, "value", CallbackSequence.__name__),
            CallbackSequence.__name__,
            "value",
        )
    )


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class CallbackTicket(_RedactedOpaqueId):
    callback_generation: CallbackGeneration
    sequence: CallbackSequence

    def __post_init__(self) -> None:
        owner = type(self).__name__
        callback_generation = _canonical_callback_generation(
            _required_field(self, "callback_generation", owner),
            owner,
            "callback_generation",
        )
        sequence = _canonical_callback_sequence(
            _required_field(self, "sequence", owner), owner, "sequence"
        )
        if sequence.value == 0:
            raise ValueError("CallbackTicket.sequence must be positive")
        object.__setattr__(
            self, "callback_generation", callback_generation
        )
        object.__setattr__(self, "sequence", sequence)


def _canonical_callback_ticket(
    value: Any, owner: str, field: str
) -> CallbackTicket:
    if (
        type(value) is not CallbackTicket
        or not CallbackTicket._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact CallbackTicket constructor"
        )
    return CallbackTicket(
        _required_field(
            value, "callback_generation", CallbackTicket.__name__
        ),
        _required_field(value, "sequence", CallbackTicket.__name__),
    )


class RedactedIdentifierKind(str, Enum):
    EPOCH = "EPOCH"
    ATTEMPT = "ATTEMPT"
    EXECUTION = "EXECUTION"
    PRODUCER = "PRODUCER"
    ADMISSION_INTENT = "ADMISSION_INTENT"
    BARRIER_BATCH = "BARRIER_BATCH"
    QUARANTINE = "QUARANTINE"
    SEEDED_SLOT = "SEEDED_SLOT"
    GENERATION = "GENERATION"


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RedactedIdentifier:
    kind: RedactedIdentifierKind
    ordinal: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _exact_enum(
            _required_field(self, "kind", owner),
            RedactedIdentifierKind,
            owner,
            "kind",
        )
        _signed_63(
            _required_field(self, "ordinal", owner), owner, "ordinal"
        )


def _canonical_redacted_identifier(
    value: Any,
    owner: str,
    field: str,
    required_kind: RedactedIdentifierKind | None = None,
) -> RedactedIdentifier:
    if (
        type(value) is not RedactedIdentifier
        or not RedactedIdentifier._is_constructor_issued(value)
    ):
        raise TypeError(
            f"{owner}.{field} must use the exact "
            "RedactedIdentifier constructor"
        )
    kind = _exact_enum(
        _required_field(value, "kind", RedactedIdentifier.__name__),
        RedactedIdentifierKind,
        RedactedIdentifier.__name__,
        "kind",
    )
    ordinal = _signed_63(
        _required_field(value, "ordinal", RedactedIdentifier.__name__),
        RedactedIdentifier.__name__,
        "ordinal",
    )
    if required_kind is not None and kind is not required_kind:
        raise TypeError(f"{owner}.{field} has the wrong identifier kind")
    return RedactedIdentifier(kind, ordinal)
