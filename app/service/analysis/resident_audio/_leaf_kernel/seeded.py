"""Private field-closed construction seam for dormant leaf tests."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any
from weakref import WeakValueDictionary, finalize

from .rows import SeededSlotState
from .values import (
    SeededSlotId,
    _canonical_seeded_slot_id,
    _exact_enum,
    _required_field,
    _runtime_final,
)


@_runtime_final
@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class _SeededSlotOutcome:
    slot_id: SeededSlotId
    state: SeededSlotState

    def __post_init__(self) -> None:
        owner = type(self).__name__
        slot_id = _canonical_seeded_slot_id(
            _required_field(self, "slot_id", owner), owner, "slot_id"
        )
        state = _exact_enum(
            _required_field(self, "state", owner),
            SeededSlotState,
            owner,
            "state",
        )
        object.__setattr__(self, "slot_id", slot_id)
        object.__setattr__(self, "state", state)

    def __repr__(self) -> str:
        return "_SeededSlotOutcome(<redacted>, state=ISSUED)"


_issued_outcomes: WeakValueDictionary[int, _SeededSlotOutcome] = (
    WeakValueDictionary()
)
_issued_slot_numbers: dict[int, int] = {}
_seeded_outcomes_lock = RLock()


def _discard_seeded_slot_number(outcome_id: int) -> None:
    with _seeded_outcomes_lock:
        _issued_slot_numbers.pop(outcome_id, None)


def _issue_seeded_slots() -> tuple[_SeededSlotOutcome, ...]:
    outcomes = tuple(
        _SeededSlotOutcome(
            SeededSlotId(slot_number), SeededSlotState.ISSUED
        )
        for slot_number in range(1, 7)
    )
    with _seeded_outcomes_lock:
        for outcome in outcomes:
            _issued_outcomes[id(outcome)] = outcome
            _issued_slot_numbers[id(outcome)] = outcome.slot_id.value
            finalize(
                outcome,
                _discard_seeded_slot_number,
                id(outcome),
            )
    return outcomes


def _validate_seeded_slot_outcome(value: Any) -> SeededSlotId:
    with _seeded_outcomes_lock:
        if (
            type(value) is not _SeededSlotOutcome
            or not _SeededSlotOutcome._is_constructor_issued(value)
            or _issued_outcomes.get(id(value)) is not value
        ):
            raise PermissionError(
                "seeded slot outcome must be issuer-issued"
            )
        state = _exact_enum(
            _required_field(
                value, "state", _SeededSlotOutcome.__name__
            ),
            SeededSlotState,
            _SeededSlotOutcome.__name__,
            "state",
        )
        if state is not SeededSlotState.ISSUED:
            raise PermissionError("seeded slot outcome is not issued")
        slot_id = _canonical_seeded_slot_id(
            _required_field(
                value, "slot_id", _SeededSlotOutcome.__name__
            ),
            _SeededSlotOutcome.__name__,
            "slot_id",
        )
        if _issued_slot_numbers.get(id(value)) != slot_id.value:
            raise PermissionError(
                "seeded slot outcome was altered after issuance"
            )
        return slot_id


def _consume_seeded_slot_outcome(value: Any) -> SeededSlotId:
    with _seeded_outcomes_lock:
        slot_id = _validate_seeded_slot_outcome(value)
        _issued_outcomes.pop(id(value), None)
        _issued_slot_numbers.pop(id(value), None)
        return slot_id
