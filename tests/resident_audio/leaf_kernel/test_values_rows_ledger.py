"""Strict contract tests for the dormant Resident lifecycle ledger shell."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from types import new_class

import pytest

import app.service.analysis.resident_audio._leaf_kernel as leaf
from app.service.analysis.resident_audio._leaf_kernel import ledger as ledger_module
from app.service.analysis.resident_audio._leaf_kernel.ledger import (
    _DecisionCapability,
    _IssuerSeal,
    _ResidentLifecycleLedger,
    _issue_ledger,
)
from app.service.analysis.resident_audio._leaf_kernel.rows import (
    LedgerDiagnostic,
    LedgerDiagnosticCode,
    LedgerSnapshot,
    ShutdownEpochRowSnapshot,
    ShutdownEpochState,
)
from app.service.analysis.resident_audio._leaf_kernel.values import (
    MAX_SIGNED_63,
    AdmissionIntentId,
    AudioBackendEpoch,
    BarrierBatchId,
    ExecutionId,
    LedgerRevision,
    ProducerId,
    QuarantineId,
    RedactedIdentifier,
    RedactedIdentifierKind,
    ShutdownAttemptId,
    ShutdownEpochId,
)


ID_TYPES = (
    ShutdownEpochId,
    ShutdownAttemptId,
    ExecutionId,
    ProducerId,
    AdmissionIntentId,
    BarrierBatchId,
    QuarantineId,
)

EXACT_VALUE_TYPES = (
    *ID_TYPES,
    LedgerRevision,
    AudioBackendEpoch,
    RedactedIdentifier,
    ShutdownEpochRowSnapshot,
    LedgerDiagnostic,
    LedgerSnapshot,
)

EXACT_ENUM_TYPES = (
    RedactedIdentifierKind,
    ShutdownEpochState,
    LedgerDiagnosticCode,
)


def _valid_value(exact_type: type):
    if exact_type in ID_TYPES:
        return exact_type("opaque-a")
    if exact_type is LedgerRevision:
        return LedgerRevision(1)
    if exact_type is AudioBackendEpoch:
        return AudioBackendEpoch(1)
    if exact_type is RedactedIdentifier:
        return RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1)
    if exact_type is ShutdownEpochRowSnapshot:
        return ShutdownEpochRowSnapshot(
            RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1),
            ShutdownEpochState.LATENT,
            LedgerRevision(1),
        )
    if exact_type is LedgerDiagnostic:
        return LedgerDiagnostic(
            LedgerDiagnosticCode.AUTHORITY_REJECTED,
            LedgerRevision(1),
            RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1),
        )
    if exact_type is LedgerSnapshot:
        return LedgerSnapshot(
            LedgerRevision(1),
            (
                ShutdownEpochRowSnapshot(
                    RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1),
                    ShutdownEpochState.LATENT,
                    LedgerRevision(1),
                ),
            ),
        )
    raise AssertionError(f"missing fixture for {exact_type.__name__}")


def test_leaf_package_exports_nothing_and_has_no_public_ledger():
    assert leaf.__all__ == ()
    assert not hasattr(leaf, "LeafLedger")


@pytest.mark.parametrize("identifier_type", ID_TYPES)
def test_opaque_ids_are_exact_frozen_slotted_and_redacted(identifier_type):
    secret = "opaque-secret-a"
    value = identifier_type(secret)

    assert type(value) is identifier_type
    assert hasattr(identifier_type, "__slots__")
    assert secret not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.value = "changed"
    with pytest.raises(TypeError):
        type("ForbiddenSubclass", (identifier_type,), {})


@pytest.mark.parametrize("identifier_type", ID_TYPES)
@pytest.mark.parametrize(
    "bad_value",
    ("", "x" * 129, "contains/slash", "contains space", 1, True, None),
)
def test_opaque_ids_reject_empty_oversized_or_unsafe_values(
    identifier_type, bad_value
):
    with pytest.raises((TypeError, ValueError)):
        identifier_type(bad_value)


@pytest.mark.parametrize("bounded_type", (LedgerRevision, AudioBackendEpoch))
@pytest.mark.parametrize("bad_value", (True, False, -1, MAX_SIGNED_63 + 1, "1"))
def test_signed_63_values_reject_booleans_and_out_of_range_values(
    bounded_type, bad_value
):
    with pytest.raises((TypeError, ValueError)):
        bounded_type(bad_value)


def test_revision_next_is_monotonic_and_fails_closed_at_exhaustion():
    assert LedgerRevision(7).next() == LedgerRevision(8)

    with pytest.raises(OverflowError):
        LedgerRevision(MAX_SIGNED_63).next()

    forged = object.__new__(LedgerRevision)
    object.__setattr__(forged, "value", 7)
    with pytest.raises(TypeError, match="constructor"):
        forged.next()


@pytest.mark.parametrize("exact_type", EXACT_VALUE_TYPES)
def test_exact_value_types_are_runtime_final(exact_type):
    with pytest.raises(TypeError):
        type("ForbiddenSubclass", (exact_type,), {})


@pytest.mark.parametrize("exact_type", EXACT_VALUE_TYPES)
def test_forged_values_with_missing_slots_are_rejected(exact_type):
    forged = object.__new__(exact_type)

    with pytest.raises(TypeError):
        exact_type.__post_init__(forged)


@pytest.mark.parametrize("exact_type", EXACT_VALUE_TYPES)
def test_forged_values_with_every_public_slot_populated_are_rejected(exact_type):
    valid = _valid_value(exact_type)
    forged = object.__new__(exact_type)
    for field in fields(exact_type):
        object.__setattr__(forged, field.name, getattr(valid, field.name))

    with pytest.raises(TypeError, match="constructor"):
        exact_type.__post_init__(forged)


@pytest.mark.parametrize("enum_type", EXACT_ENUM_TYPES)
def test_closed_enums_are_nonempty_and_cannot_be_subclassed(enum_type):
    assert tuple(enum_type)
    with pytest.raises(TypeError):
        new_class("ForbiddenEnumSubclass", (enum_type,))


def test_enum_fields_reject_raw_string_lookalikes():
    with pytest.raises(TypeError):
        RedactedIdentifier("EPOCH", 1)
    with pytest.raises(TypeError):
        ShutdownEpochRowSnapshot(
            RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1),
            "LATENT",
            LedgerRevision(1),
        )
    with pytest.raises(TypeError):
        LedgerDiagnostic("AUTHORITY_REJECTED", LedgerRevision(1))


@pytest.mark.parametrize("exact_type", EXACT_VALUE_TYPES)
def test_every_exact_contract_rejects_malicious_structural_lookalikes(
    exact_type,
):
    class Malicious:
        def __eq__(self, other):
            raise AssertionError("malicious equality executed")

        def __hash__(self):
            raise AssertionError("malicious hash executed")

    malicious = Malicious()
    if exact_type in ID_TYPES:
        construct = lambda: exact_type(malicious)
    elif exact_type in (LedgerRevision, AudioBackendEpoch):
        construct = lambda: exact_type(malicious)
    elif exact_type is RedactedIdentifier:
        construct = lambda: RedactedIdentifier(malicious, 1)
    elif exact_type is ShutdownEpochRowSnapshot:
        construct = lambda: ShutdownEpochRowSnapshot(
            malicious,
            ShutdownEpochState.LATENT,
            LedgerRevision(1),
        )
    elif exact_type is LedgerDiagnostic:
        construct = lambda: LedgerDiagnostic(
            malicious, LedgerRevision(1)
        )
    elif exact_type is LedgerSnapshot:
        construct = lambda: LedgerSnapshot(malicious, ())
    else:
        raise AssertionError(f"missing hostile fixture for {exact_type}")

    with pytest.raises(TypeError):
        construct()


@pytest.mark.parametrize("bad_ordinal", (True, -1, MAX_SIGNED_63 + 1))
def test_redacted_identifier_rejects_invalid_integer_ordinals(bad_ordinal):
    with pytest.raises((TypeError, ValueError)):
        RedactedIdentifier(RedactedIdentifierKind.EPOCH, bad_ordinal)


def test_nested_values_reject_wrong_exact_types_without_calling_equality():
    class Malicious:
        def __eq__(self, other):
            raise AssertionError("malicious equality executed")

        def __hash__(self):
            raise AssertionError("malicious hash executed")

    with pytest.raises(TypeError):
        RedactedIdentifier(Malicious(), 1)
    with pytest.raises(TypeError):
        ShutdownEpochRowSnapshot(
            Malicious(),
            ShutdownEpochState.LATENT,
            LedgerRevision(1),
        )
    with pytest.raises(TypeError):
        LedgerSnapshot(LedgerRevision(1), (Malicious(),))


def test_epoch_snapshot_rejects_wrong_nested_identifier_kind_and_forged_id():
    with pytest.raises(TypeError):
        ShutdownEpochRowSnapshot(
            RedactedIdentifier(RedactedIdentifierKind.ATTEMPT, 1),
            ShutdownEpochState.LATENT,
            LedgerRevision(1),
        )

    forged = object.__new__(RedactedIdentifier)
    object.__setattr__(forged, "kind", RedactedIdentifierKind.EPOCH)
    object.__setattr__(forged, "ordinal", True)
    with pytest.raises(TypeError):
        ShutdownEpochRowSnapshot(
            forged,
            ShutdownEpochState.LATENT,
            LedgerRevision(1),
        )


def test_fully_populated_forged_nested_identifier_is_rejected():
    valid = RedactedIdentifier(RedactedIdentifierKind.EPOCH, 7)
    forged = object.__new__(RedactedIdentifier)
    for field in fields(RedactedIdentifier):
        object.__setattr__(forged, field.name, getattr(valid, field.name))

    with pytest.raises(TypeError, match="constructor"):
        ShutdownEpochRowSnapshot(
            forged,
            ShutdownEpochState.LATENT,
            LedgerRevision(1),
        )


def test_audio_backend_epoch_repr_redacts_its_raw_value():
    raw_epoch = 424242

    assert str(raw_epoch) not in repr(AudioBackendEpoch(raw_epoch))


def test_snapshots_and_diagnostics_are_frozen_slotted_and_redacted():
    epoch_secret = "epoch-secret-value"
    ledger, authority = _issue_ledger()
    ledger.reserve_shutdown_epoch(authority, ShutdownEpochId(epoch_secret))

    snapshot = ledger.snapshot()
    diagnostic = LedgerDiagnostic(
        LedgerDiagnosticCode.AUTHORITY_REJECTED,
        snapshot.revision,
        snapshot.shutdown_epochs[0].epoch,
    )

    assert epoch_secret not in repr(snapshot)
    assert epoch_secret not in repr(diagnostic)
    assert snapshot.shutdown_epochs[0].epoch.kind is RedactedIdentifierKind.EPOCH
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = LedgerRevision(2)


def test_named_epoch_mutation_increments_revision_once_and_returns_snapshot():
    epoch_secret = "epoch-a"
    ledger, authority = _issue_ledger()

    before = ledger.snapshot()
    row = ledger.reserve_shutdown_epoch(authority, ShutdownEpochId(epoch_secret))
    after = ledger.snapshot()

    assert before == LedgerSnapshot(LedgerRevision(0), ())
    assert after.revision == LedgerRevision(1)
    assert after.shutdown_epochs == (row,)
    assert row.revision == LedgerRevision(1)
    assert epoch_secret not in repr(row)
    assert epoch_secret not in repr(ledger)


def test_duplicate_epoch_is_rejected_without_revision_or_row_change():
    ledger, authority = _issue_ledger()
    epoch_id = ShutdownEpochId("epoch-a")
    ledger.reserve_shutdown_epoch(authority, epoch_id)
    before = ledger.snapshot()

    with pytest.raises(ValueError):
        ledger.reserve_shutdown_epoch(authority, epoch_id)

    assert ledger.snapshot() == before


def test_mutation_requires_the_exact_decision_capability_without_side_effects():
    ledger, authority = _issue_ledger()
    other_ledger, other_authority = _issue_ledger()
    del authority, other_ledger
    before = ledger.snapshot()

    with pytest.raises(PermissionError):
        ledger.reserve_shutdown_epoch(other_authority, ShutdownEpochId("epoch-a"))

    forged = object.__new__(_DecisionCapability)
    with pytest.raises(PermissionError):
        ledger.reserve_shutdown_epoch(forged, ShutdownEpochId("epoch-a"))

    assert ledger.snapshot() == before


def test_issuer_seal_is_not_exposed_and_direct_ledger_construction_rejects():
    assert not hasattr(ledger_module, "_ISSUER_SEAL")

    forged_capability = object.__new__(_DecisionCapability)
    object.__setattr__(forged_capability, "_seal", _IssuerSeal())
    with pytest.raises(PermissionError):
        _ResidentLifecycleLedger(_IssuerSeal(), forged_capability)


def test_decision_capability_must_be_factory_issued_and_initialized():
    with pytest.raises(PermissionError):
        _DecisionCapability(_IssuerSeal())

    forged_capability = object.__new__(_DecisionCapability)
    object.__setattr__(forged_capability, "_seal", _IssuerSeal())
    with pytest.raises(PermissionError):
        _ResidentLifecycleLedger(_IssuerSeal(), forged_capability)

    ledger, issued_capability = _issue_ledger()
    assert repr(issued_capability) == "<resident-decision-capability>"
    ledger.reserve_shutdown_epoch(
        issued_capability, ShutdownEpochId("epoch-issued")
    )
    assert ledger.snapshot().revision == LedgerRevision(1)


def test_even_an_issued_capability_cannot_directly_construct_a_second_ledger():
    ledger, issued_capability = _issue_ledger()
    del ledger

    with pytest.raises(PermissionError):
        _ResidentLifecycleLedger(
            issued_capability._seal, issued_capability
        )


def test_structural_lookalikes_are_not_accepted_as_authority_or_identity():
    class AuthorityLookalike:
        pass

    class EpochLookalike:
        value = "epoch-a"

    ledger, authority = _issue_ledger()
    before = ledger.snapshot()

    with pytest.raises(PermissionError):
        ledger.reserve_shutdown_epoch(
            AuthorityLookalike(), ShutdownEpochId("epoch-a")
        )
    with pytest.raises(TypeError):
        ledger.reserve_shutdown_epoch(authority, EpochLookalike())

    assert ledger.snapshot() == before


def test_reads_return_frozen_value_snapshots_and_never_internal_rows():
    ledger, authority = _issue_ledger()
    returned = ledger.reserve_shutdown_epoch(
        authority, ShutdownEpochId("epoch-a")
    )

    assert type(returned) is ShutdownEpochRowSnapshot
    assert type(ledger.snapshot()) is LedgerSnapshot
    assert not hasattr(ledger, "shutdown_epoch_rows")


def _assert_exact_single_epoch_snapshot(snapshot):
    assert snapshot.revision == LedgerRevision(1)
    assert len(snapshot.shutdown_epochs) == 1
    row = snapshot.shutdown_epochs[0]
    assert row.epoch == RedactedIdentifier(
        RedactedIdentifierKind.EPOCH, 1
    )
    assert row.state is ShutdownEpochState.LATENT
    assert row.revision == LedgerRevision(1)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda row: object.__setattr__(row, "state", "CORRUPTED"),
        lambda row: object.__setattr__(
            row, "revision", LedgerRevision(41)
        ),
        lambda row: object.__setattr__(
            row,
            "epoch",
            RedactedIdentifier(RedactedIdentifierKind.EPOCH, 41),
        ),
        lambda row: object.__setattr__(row.revision, "value", 41),
        lambda row: object.__setattr__(
            row.epoch, "kind", RedactedIdentifierKind.ATTEMPT
        ),
        lambda row: object.__setattr__(row.epoch, "ordinal", 41),
    ),
)
def test_hostile_mutation_of_returned_row_never_changes_ledger_or_reads(
    mutate,
):
    ledger, authority = _issue_ledger()
    returned = ledger.reserve_shutdown_epoch(
        authority, ShutdownEpochId("epoch-a")
    )
    earlier = ledger.snapshot()

    mutate(returned)

    _assert_exact_single_epoch_snapshot(earlier)
    _assert_exact_single_epoch_snapshot(ledger.snapshot())


@pytest.mark.parametrize(
    "mutate",
    (
        lambda snapshot: object.__setattr__(
            snapshot, "revision", LedgerRevision(41)
        ),
        lambda snapshot: object.__setattr__(
            snapshot.revision, "value", 41
        ),
        lambda snapshot: object.__setattr__(snapshot, "shutdown_epochs", ()),
        lambda snapshot: object.__setattr__(
            snapshot.shutdown_epochs[0], "state", "CORRUPTED"
        ),
        lambda snapshot: object.__setattr__(
            snapshot.shutdown_epochs[0],
            "revision",
            LedgerRevision(41),
        ),
        lambda snapshot: object.__setattr__(
            snapshot.shutdown_epochs[0],
            "epoch",
            RedactedIdentifier(RedactedIdentifierKind.EPOCH, 41),
        ),
        lambda snapshot: object.__setattr__(
            snapshot.shutdown_epochs[0].revision, "value", 41
        ),
        lambda snapshot: object.__setattr__(
            snapshot.shutdown_epochs[0].epoch, "ordinal", 41
        ),
    ),
)
def test_hostile_mutation_of_read_snapshot_never_changes_other_reads(
    mutate,
):
    ledger, authority = _issue_ledger()
    ledger.reserve_shutdown_epoch(authority, ShutdownEpochId("epoch-a"))
    hostile = ledger.snapshot()
    earlier = ledger.snapshot()

    mutate(hostile)

    _assert_exact_single_epoch_snapshot(earlier)
    _assert_exact_single_epoch_snapshot(ledger.snapshot())


@pytest.mark.parametrize(
    "mutate",
    (
        lambda diagnostic: object.__setattr__(
            diagnostic, "code", "CORRUPTED"
        ),
        lambda diagnostic: object.__setattr__(
            diagnostic, "revision", LedgerRevision(41)
        ),
        lambda diagnostic: object.__setattr__(
            diagnostic,
            "subject",
            RedactedIdentifier(RedactedIdentifierKind.EPOCH, 41),
        ),
        lambda diagnostic: object.__setattr__(
            diagnostic.revision, "value", 41
        ),
        lambda diagnostic: object.__setattr__(
            diagnostic.subject, "ordinal", 41
        ),
    ),
)
def test_hostile_mutation_of_diagnostic_projection_cannot_change_ledger(
    mutate,
):
    ledger, authority = _issue_ledger()
    ledger.reserve_shutdown_epoch(authority, ShutdownEpochId("epoch-a"))
    current = ledger.snapshot()
    diagnostic = LedgerDiagnostic(
        LedgerDiagnosticCode.AUTHORITY_REJECTED,
        current.revision,
        current.shutdown_epochs[0].epoch,
    )

    mutate(diagnostic)

    _assert_exact_single_epoch_snapshot(current)
    _assert_exact_single_epoch_snapshot(ledger.snapshot())


def test_row_constructor_copies_every_caller_supplied_nested_value():
    supplied_epoch = RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1)
    supplied_revision = LedgerRevision(1)

    row = ShutdownEpochRowSnapshot(
        supplied_epoch, ShutdownEpochState.LATENT, supplied_revision
    )
    object.__setattr__(supplied_epoch, "ordinal", 41)
    object.__setattr__(supplied_revision, "value", 41)

    assert row.epoch == RedactedIdentifier(
        RedactedIdentifierKind.EPOCH, 1
    )
    assert row.revision == LedgerRevision(1)
    assert row.epoch is not supplied_epoch
    assert row.revision is not supplied_revision


def test_diagnostic_constructor_copies_every_caller_supplied_nested_value():
    supplied_revision = LedgerRevision(1)
    supplied_subject = RedactedIdentifier(
        RedactedIdentifierKind.EPOCH, 1
    )

    diagnostic = LedgerDiagnostic(
        LedgerDiagnosticCode.AUTHORITY_REJECTED,
        supplied_revision,
        supplied_subject,
    )
    object.__setattr__(supplied_revision, "value", 41)
    object.__setattr__(supplied_subject, "ordinal", 41)

    assert diagnostic.revision == LedgerRevision(1)
    assert diagnostic.subject == RedactedIdentifier(
        RedactedIdentifierKind.EPOCH, 1
    )
    assert diagnostic.revision is not supplied_revision
    assert diagnostic.subject is not supplied_subject


def test_ledger_snapshot_constructor_rebuilds_rows_and_all_nested_values():
    supplied_revision = LedgerRevision(1)
    supplied_epoch = RedactedIdentifier(RedactedIdentifierKind.EPOCH, 1)
    supplied_row_revision = LedgerRevision(1)
    supplied_row = ShutdownEpochRowSnapshot(
        supplied_epoch,
        ShutdownEpochState.LATENT,
        supplied_row_revision,
    )
    supplied_rows = (supplied_row,)

    projection = LedgerSnapshot(supplied_revision, supplied_rows)
    object.__setattr__(supplied_revision, "value", 41)
    object.__setattr__(supplied_row, "state", "CORRUPTED")
    object.__setattr__(supplied_row.epoch, "ordinal", 41)
    object.__setattr__(supplied_row.revision, "value", 41)

    _assert_exact_single_epoch_snapshot(projection)
    assert projection.shutdown_epochs is not supplied_rows
    assert projection.shutdown_epochs[0] is not supplied_row
    assert projection.shutdown_epochs[0].epoch is not supplied_epoch
    assert projection.shutdown_epochs[0].revision is not supplied_row_revision


def test_outward_snapshots_never_share_nested_identity_with_each_other():
    ledger, authority = _issue_ledger()
    returned = ledger.reserve_shutdown_epoch(
        authority, ShutdownEpochId("epoch-a")
    )
    first = ledger.snapshot()
    second = ledger.snapshot()

    assert returned is not first.shutdown_epochs[0]
    assert returned.revision is not first.revision
    assert returned.revision is not first.shutdown_epochs[0].revision
    assert returned.epoch is not first.shutdown_epochs[0].epoch
    assert first is not second
    assert first.revision is not second.revision
    assert first.shutdown_epochs is not second.shutdown_epochs
    assert first.shutdown_epochs[0] is not second.shutdown_epochs[0]
    assert (
        first.shutdown_epochs[0].revision
        is not second.shutdown_epochs[0].revision
    )
    assert (
        first.shutdown_epochs[0].epoch
        is not second.shutdown_epochs[0].epoch
    )


def test_authorized_mutation_remains_exact_after_hostile_prior_snapshot_changes():
    ledger, authority = _issue_ledger()
    hostile = ledger.snapshot()
    object.__setattr__(hostile.revision, "value", MAX_SIGNED_63)
    object.__setattr__(
        hostile,
        "shutdown_epochs",
        (
            ShutdownEpochRowSnapshot(
                RedactedIdentifier(RedactedIdentifierKind.EPOCH, 99),
                ShutdownEpochState.LATENT,
                LedgerRevision(99),
            ),
        ),
    )

    returned = ledger.reserve_shutdown_epoch(
        authority, ShutdownEpochId("epoch-a")
    )

    assert returned.revision == LedgerRevision(1)
    _assert_exact_single_epoch_snapshot(ledger.snapshot())
