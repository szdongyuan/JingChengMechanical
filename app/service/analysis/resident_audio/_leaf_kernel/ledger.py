"""Capability-gated shell for the dormant Resident lifecycle ledger."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any
from weakref import WeakValueDictionary

from .generation import _ResidentGeneration, _create_seeded_generation
from .ring import _validate_ring_dimensions
from .rows import (
    GenerationRegistrationRowSnapshot,
    GenerationState,
    LedgerSnapshot,
    ShutdownEpochRowSnapshot,
    ShutdownEpochState,
)
from .seeded import (
    _consume_seeded_slot_outcome,
    _seeded_outcomes_lock,
    _validate_seeded_slot_outcome,
)
from .values import (
    LedgerRevision,
    MAX_SIGNED_63,
    RedactedIdentifier,
    RedactedIdentifierKind,
    SeededSlotId,
    ShutdownEpochId,
    _canonical_opaque_id,
    _runtime_final,
)


class _IssuerSeal:
    __slots__ = ()


@_runtime_final
class _DecisionCapability:
    __slots__ = ("_seal", "__weakref__")

    def __init__(self, issuer_seal: Any) -> None:
        if not _capability_seal_is_valid(issuer_seal):
            raise PermissionError("decision capability is issuer-only")
        self._seal = issuer_seal

    def __repr__(self) -> str:
        return "<resident-decision-capability>"


@dataclass(slots=True, repr=False)
class _ShutdownEpochRow:
    ordinal: int
    state: ShutdownEpochState
    revision: int


@dataclass(slots=True, repr=False)
class _GenerationRegistrationRow:
    slot_ordinal: int
    generation_ordinal: int
    state: GenerationState
    revision: int
    generation: _ResidentGeneration | None


class _SeededGenerationConstructionStage(str, Enum):
    RESERVED = "RESERVED"
    FACTORY_RETURNED = "FACTORY_RETURNED"
    BIND_ROW_REPLACED = "BIND_ROW_REPLACED"
    ACTIVE = "ACTIVE"
    FAILED_CONSTRUCTION = "FAILED_CONSTRUCTION"


@dataclass(slots=True, repr=False)
class _SeededGenerationConstruction:
    stage: _SeededGenerationConstructionStage
    generation: _ResidentGeneration | None
    reconciliation_failures: int = 0


class _ResidentLifecycleLedger:
    __slots__ = (
        "__decision_capability",
        "__revision",
        "__shutdown_epoch_rows",
        "__generation_rows",
        "__decision_lock",
    )

    def __init__(
        self, issuer_seal: Any, decision_capability: _DecisionCapability
    ) -> None:
        if not _ledger_issuance_is_valid(
            issuer_seal, decision_capability
        ):
            raise PermissionError(
                "ledger and decision capability must be factory-issued"
            )
        self.__decision_capability = decision_capability
        self.__revision = 0
        self.__shutdown_epoch_rows: dict[
            ShutdownEpochId, _ShutdownEpochRow
        ] = {}
        self.__generation_rows: dict[
            SeededSlotId, _GenerationRegistrationRow
        ] = {}
        self.__decision_lock = RLock()

    def __repr__(self) -> str:
        return "<resident-lifecycle-ledger>"

    def _require_authority(self, capability: Any) -> None:
        if capability is not self.__decision_capability:
            raise PermissionError("exact decision capability required")

    def _snapshot_row(
        self, row: _ShutdownEpochRow
    ) -> ShutdownEpochRowSnapshot:
        return ShutdownEpochRowSnapshot(
            RedactedIdentifier(
                RedactedIdentifierKind.EPOCH, row.ordinal
            ),
            row.state,
            LedgerRevision(row.revision),
        )

    def reserve_shutdown_epoch(
        self, capability: Any, epoch_id: Any
    ) -> ShutdownEpochRowSnapshot:
        self._require_authority(capability)
        with self.__decision_lock:
            canonical_epoch = _canonical_opaque_id(
                epoch_id,
                ShutdownEpochId,
                type(self).__name__,
                "epoch_id",
            )
            if canonical_epoch in self.__shutdown_epoch_rows:
                raise ValueError("shutdown epoch is already reserved")
            if self.__shutdown_epoch_rows:
                raise ValueError("ledger permits only one shutdown epoch")
            if self.__revision == MAX_SIGNED_63:
                raise OverflowError("ledger revision exhausted")
            next_revision = self.__revision + 1
            row = _ShutdownEpochRow(
                1,
                ShutdownEpochState.LATENT,
                next_revision,
            )
            self.__shutdown_epoch_rows[canonical_epoch] = row
            self.__revision = next_revision
            return self._snapshot_row(row)

    def _snapshot_generation_row(
        self, row: _GenerationRegistrationRow
    ) -> GenerationRegistrationRowSnapshot:
        return GenerationRegistrationRowSnapshot(
            RedactedIdentifier(
                RedactedIdentifierKind.SEEDED_SLOT,
                row.slot_ordinal,
            ),
            RedactedIdentifier(
                RedactedIdentifierKind.GENERATION,
                row.generation_ordinal,
            ),
            row.state,
            LedgerRevision(row.revision),
        )

    def reserve_seeded_generation(
        self,
        capability: Any,
        seeded_outcome: Any,
        capacity_frames: Any,
        channels: Any,
    ) -> _ResidentGeneration:
        self._require_authority(capability)
        _validate_ring_dimensions(capacity_frames, channels)
        slot_id = self._reserve_seeded_generation_row(
            seeded_outcome
        )
        construction = _SeededGenerationConstruction(
            _SeededGenerationConstructionStage.RESERVED,
            None,
        )
        try:
            generation = _create_seeded_generation(
                slot_id, capacity_frames, channels
            )
            construction.generation = generation
            construction.stage = (
                _SeededGenerationConstructionStage.FACTORY_RETURNED
            )
            self._after_seeded_generation_factory()
            self._bind_seeded_generation(
                slot_id, generation, construction
            )
            self._after_seeded_generation_bind()
        except BaseException:
            try:
                self._reconcile_seeded_generation_construction(
                    slot_id, construction
                )
            except BaseException:
                construction.reconciliation_failures += 1
            raise
        return generation

    def _after_seeded_generation_factory(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_seeded_generation_bind_row_replaced(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_seeded_generation_bind_revision(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_seeded_generation_bind(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_seeded_generation_failure_row_replaced(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _reserve_seeded_generation_row(
        self, seeded_outcome: Any
    ) -> SeededSlotId:
        with self.__decision_lock:
            with _seeded_outcomes_lock:
                slot_id = _validate_seeded_slot_outcome(
                    seeded_outcome
                )
                if slot_id in self.__generation_rows:
                    raise ValueError(
                        "seeded generation slot is already reserved"
                    )
                if len(self.__generation_rows) >= 6:
                    raise ValueError(
                        "all six seeded generation slots are reserved"
                    )
                if self.__revision > MAX_SIGNED_63 - 2:
                    raise OverflowError(
                        "ledger revision cannot reserve and bind generation"
                    )
                consumed_slot = _consume_seeded_slot_outcome(
                    seeded_outcome
                )
                if consumed_slot != slot_id:
                    raise RuntimeError(
                        "seeded slot changed during reservation"
                    )
                next_revision = self.__revision + 1
                ordinal = len(self.__generation_rows) + 1
                self.__generation_rows[
                    slot_id
                ] = _GenerationRegistrationRow(
                    slot_ordinal=ordinal,
                    generation_ordinal=ordinal,
                    state=GenerationState.RESERVED,
                    revision=next_revision,
                    generation=None,
                )
                self.__revision = next_revision
                return slot_id

    def _bind_seeded_generation(
        self,
        slot_id: SeededSlotId,
        generation: _ResidentGeneration,
        construction: _SeededGenerationConstruction,
    ) -> None:
        with self.__decision_lock:
            try:
                row = self.__generation_rows.get(slot_id)
                if (
                    row is None
                    or row.state is not GenerationState.RESERVED
                    or row.generation is not None
                    or type(generation) is not _ResidentGeneration
                    or generation.generation_id.slot_id != slot_id
                    or construction.generation is not generation
                    or construction.stage
                    is not _SeededGenerationConstructionStage.FACTORY_RETURNED
                ):
                    raise RuntimeError(
                        "seeded generation reservation cannot be bound"
                    )
                if self.__revision == MAX_SIGNED_63:
                    raise OverflowError("ledger revision exhausted")
                next_revision = self.__revision + 1
                self.__generation_rows[
                    slot_id
                ] = _GenerationRegistrationRow(
                    slot_ordinal=row.slot_ordinal,
                    generation_ordinal=row.generation_ordinal,
                    state=GenerationState.ACTIVE,
                    revision=next_revision,
                    generation=generation,
                )
                construction.stage = (
                    _SeededGenerationConstructionStage.BIND_ROW_REPLACED
                )
                self._after_seeded_generation_bind_row_replaced()
                self.__revision = next_revision
                construction.stage = (
                    _SeededGenerationConstructionStage.ACTIVE
                )
                self._after_seeded_generation_bind_revision()
            except BaseException:
                try:
                    self._reconcile_seeded_generation_construction_locked(
                        slot_id, construction
                    )
                except BaseException:
                    construction.reconciliation_failures += 1
                raise

    def _settle_seeded_generation_construction_failed(
        self, slot_id: SeededSlotId
    ) -> None:
        with self.__decision_lock:
            construction = _SeededGenerationConstruction(
                _SeededGenerationConstructionStage.RESERVED,
                None,
            )
            self._reconcile_seeded_generation_construction_locked(
                slot_id, construction
            )

    def _reconcile_seeded_generation_construction(
        self,
        slot_id: SeededSlotId,
        construction: _SeededGenerationConstruction,
    ) -> None:
        with self.__decision_lock:
            self._reconcile_seeded_generation_construction_locked(
                slot_id, construction
            )

    def _reconcile_seeded_generation_construction_locked(
        self,
        slot_id: SeededSlotId,
        construction: _SeededGenerationConstruction,
    ) -> None:
        row = self.__generation_rows.get(slot_id)
        if row is None:
            raise RuntimeError(
                "seeded generation reconciliation has no reservation"
            )
        generation = construction.generation
        if (
            generation is not None
            and row.generation is generation
            and row.state is GenerationState.ACTIVE
            and generation.generation_id.slot_id == slot_id
        ):
            if (
                construction.stage
                is _SeededGenerationConstructionStage.ACTIVE
            ):
                if row.revision <= self.__revision:
                    return
            elif (
                construction.stage
                is _SeededGenerationConstructionStage.BIND_ROW_REPLACED
                and row.revision == self.__revision + 1
            ):
                self.__revision = row.revision
                construction.stage = (
                    _SeededGenerationConstructionStage.ACTIVE
                )
                return
        if (
            row.state is GenerationState.RESERVED
            and row.generation is None
        ):
            if self.__revision == MAX_SIGNED_63:
                raise OverflowError("ledger revision exhausted")
            next_revision = self.__revision + 1
            self.__generation_rows[
                slot_id
            ] = _GenerationRegistrationRow(
                slot_ordinal=row.slot_ordinal,
                generation_ordinal=row.generation_ordinal,
                state=GenerationState.FAILED_CONSTRUCTION,
                revision=next_revision,
                generation=None,
            )
            try:
                self._after_seeded_generation_failure_row_replaced()
            finally:
                self.__revision = next_revision
                construction.stage = (
                    _SeededGenerationConstructionStage.FAILED_CONSTRUCTION
                )
            return
        if (
            row.state is GenerationState.FAILED_CONSTRUCTION
            and row.generation is None
        ):
            construction.stage = (
                _SeededGenerationConstructionStage.FAILED_CONSTRUCTION
            )
            return
        raise RuntimeError(
            "seeded generation reconciliation found conflicting evidence"
        )

    def snapshot(self) -> LedgerSnapshot:
        with self.__decision_lock:
            rows = tuple(
                self._snapshot_row(row)
                for row in self.__shutdown_epoch_rows.values()
            )
            generation_rows = tuple(
                self._snapshot_generation_row(row)
                for row in self.__generation_rows.values()
            )
            return LedgerSnapshot(
                LedgerRevision(self.__revision),
                rows,
                generation_rows,
            )


def _build_private_issuer():
    issuer_seal = _IssuerSeal()
    issued_capabilities: WeakValueDictionary[
        int, _DecisionCapability
    ] = WeakValueDictionary()
    pending_ledger_construction: set[int] = set()

    def capability_seal_is_valid(candidate: Any) -> bool:
        return candidate is issuer_seal

    def ledger_issuance_is_valid(
        candidate: Any, capability: Any
    ) -> bool:
        valid = (
            candidate is issuer_seal
            and type(capability) is _DecisionCapability
            and issued_capabilities.get(id(capability)) is capability
            and id(capability) in pending_ledger_construction
        )
        if valid:
            pending_ledger_construction.remove(id(capability))
        return valid

    def issue_pair():
        capability = _DecisionCapability(issuer_seal)
        issued_capabilities[id(capability)] = capability
        pending_ledger_construction.add(id(capability))
        try:
            ledger = _ResidentLifecycleLedger(issuer_seal, capability)
        finally:
            pending_ledger_construction.discard(id(capability))
        return ledger, capability

    return (
        issue_pair,
        capability_seal_is_valid,
        ledger_issuance_is_valid,
    )


(
    _issue_pair,
    _capability_seal_is_valid,
    _ledger_issuance_is_valid,
) = _build_private_issuer()


def _issue_ledger() -> tuple[_ResidentLifecycleLedger, _DecisionCapability]:
    return _issue_pair()
