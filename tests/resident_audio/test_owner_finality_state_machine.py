from __future__ import annotations

import dataclasses
import gc
import math
import weakref

import pytest

from app.service.analysis.resident_audio.owner_finality import (
    FinalityDurableProgress,
    OwnerFinalityLedger,
    OwnerFinalityEntrySnapshot,
    OwnerFinalitySnapshot,
    OwnerFinalityState,
    PrepareAdmission,
    PrepareClaim,
    PrepareConflictError,
    _OwnerFinalityEntry,
)
from app.service.analysis.resident_audio.owner_finality_crypto import (
    OwnerFinalityAuthenticator,
)
from app.service.analysis.resident_audio.owner_identity import (
    FinalityAckReceipt,
    FinalityPruneOutcome,
    OwnerInstanceId,
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
    StreamInstanceId,
)


MAX_VALUE = (1 << 63) - 1


def stream_id(**changes: object) -> StreamInstanceId:
    values = {
        "pool_epoch": 7,
        "hardware_id": "hw-1",
        "stream_generation": 3,
        "instance_nonce": "stream-a",
    }
    values.update(changes)
    return StreamInstanceId(**values)


def owner_id(**changes: object) -> OwnerInstanceId:
    values = {
        "pool_epoch": 7,
        "hardware_id": "hw-1",
        "stream_generation": 3,
        "run_id": "run-9",
        "commit_attempt_id": 11,
        "owner_capability": "owner-cap",
        "instance_nonce": "owner-a",
    }
    values.update(changes)
    return OwnerInstanceId(**values)


def completed_progress(
    receipt: OwnerRemovalReceipt,
) -> FinalityDurableProgress:
    return FinalityDurableProgress(
        removal_receipt=receipt,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )


def reserve(ledger: OwnerFinalityLedger, identity: OwnerInstanceId) -> None:
    assert isinstance(ledger.reserve_prepare(identity), PrepareAdmission)


def activate(ledger: OwnerFinalityLedger, identity: OwnerInstanceId) -> None:
    reserve(ledger, identity)
    ledger.mark_prepare_enqueued(identity)
    assert ledger.claim_prepare(identity).activation_allowed is True
    assert ledger.authorize_prepare_publication(identity) is True
    ledger.resolve_prepare(identity, activated=True)


def acknowledge(
    ledger: OwnerFinalityLedger,
    identity: OwnerInstanceId,
    receipt: OwnerRemovalReceipt,
) -> FinalityAckReceipt:
    ack = ledger._acknowledge_entry(
        identity, receipt, completed_progress(receipt)
    )
    assert isinstance(ack, FinalityAckReceipt)
    return ack


@pytest.mark.parametrize(
    "retained_stage",
    [
        "open",
        "queued",
        "claimed",
        "active",
        "removal_requested",
        "final",
        "acknowledged",
    ],
)
def test_duplicate_reserve_is_uniformly_rejected_without_mutation(
    retained_stage: str,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    identity = owner_id(instance_nonce=retained_stage)
    reserve(ledger, identity)
    if retained_stage != "open":
        ledger.mark_prepare_enqueued(identity)
    if retained_stage in {
        "claimed",
        "active",
        "removal_requested",
        "final",
        "acknowledged",
    }:
        ledger.claim_prepare(identity)
    if retained_stage == "active":
        ledger.resolve_prepare(identity, activated=True)
    elif retained_stage == "removal_requested":
        ledger.request_removal(identity, registration_present=False)
    elif retained_stage in {"final", "acknowledged"}:
        ledger.resolve_prepare(identity, activated=False)
        receipt = ledger.request_removal(
            identity, registration_present=False
        )
        if retained_stage == "acknowledged":
            acknowledge(ledger, identity, receipt)
    before = ledger.snapshot()

    with pytest.raises(PrepareConflictError, match="already retained"):
        ledger.reserve_prepare(identity)

    assert ledger.snapshot() == before


def test_one_reservation_cannot_authorize_two_claims():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    reserve(ledger, identity)
    ledger.mark_prepare_enqueued(identity)
    first = ledger.claim_prepare(identity)
    before_duplicate = ledger.snapshot()

    with pytest.raises(PrepareConflictError, match="claim"):
        ledger.claim_prepare(identity)

    assert first.activation_allowed is True
    assert ledger.snapshot() == before_duplicate
    assert ledger.authorize_prepare_publication(identity) is True


def test_handler_before_enqueue_marker_is_a_valid_one_shot_claim():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    reserve(ledger, identity)

    claim = ledger.claim_prepare(identity)
    ledger.mark_prepare_enqueued(identity)

    assert claim.activation_allowed is True
    assert ledger.snapshot().pending_late_commands == 1
    assert ledger.authorize_prepare_publication(identity) is True
    ledger.resolve_prepare(identity, activated=True)
    assert ledger.snapshot().count(OwnerFinalityState.ACTIVE) == 1


def test_resolve_without_outstanding_claim_is_a_zero_mutation_noop():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    reserve(ledger, identity)
    before = ledger.snapshot()

    ledger.resolve_prepare(identity, activated=True)

    assert ledger.snapshot() == before
    assert ledger.authorize_prepare_publication(identity) is False


def test_publication_authorization_is_pure_fail_closed_for_ineligible_identities():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    claimed = owner_id(instance_nonce="claimed")
    active = owner_id(instance_nonce="active")
    reserve(ledger, claimed)
    ledger.mark_prepare_enqueued(claimed)
    ledger.claim_prepare(claimed)
    activate(ledger, active)
    candidates = (
        owner_id(instance_nonce="absent"),
        owner_id(pool_epoch=8),
        owner_id(hardware_id="hw-2"),
        owner_id(stream_generation=4),
        active,
    )

    for candidate in candidates:
        before = ledger.snapshot()
        result = ledger.authorize_prepare_publication(candidate)
        assert type(result) is bool
        assert result is False
        assert ledger.snapshot() == before

    before = ledger.snapshot()
    assert ledger.authorize_prepare_publication(claimed) is True
    assert ledger.snapshot() == before


@pytest.mark.parametrize(
    "change",
    [
        {"pool_epoch": 8},
        {"hardware_id": "hw-2"},
        {"stream_generation": 4},
    ],
)
def test_stream_identity_mismatch_is_zero_mutation_across_all_ledger_gates(
    change: dict[str, object],
):
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    protected = owner_id(instance_nonce="protected")
    activate(ledger, protected)
    mismatched = owner_id(instance_nonce="stale", **change)
    positive = OwnerRemovalReceipt(
        mismatched, OwnerRemovalOutcome.TOMBSTONED_ABSENT
    )
    forged_ack = FinalityAckReceipt(
        stream_instance_id=stream_id(),
        owner_instance_id=mismatched,
        ledger_version=1,
        ack_token="forged",
    )
    before = ledger.snapshot()

    with pytest.raises(ValueError, match="stream instance"):
        ledger.reserve_prepare(mismatched)
    assert ledger.snapshot() == before
    assert ledger.authorize_prepare_publication(mismatched) is False
    assert ledger.snapshot() == before
    removal = ledger.request_removal(
        mismatched, registration_present=True
    )
    assert removal.owner_instance_id == mismatched
    assert removal.outcome is OwnerRemovalOutcome.MISMATCH
    assert ledger.snapshot() == before
    assert (
        ledger._acknowledge_entry(
            mismatched, positive, completed_progress(positive)
        )
        is None
    )
    assert ledger.snapshot() == before
    assert (
        ledger._prune_entry(mismatched, forged_ack).outcome
        is FinalityPruneOutcome.MISMATCH
    )
    assert ledger.snapshot() == before


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (owner_id, "pool_epoch"),
        (owner_id, "stream_generation"),
        (owner_id, "commit_attempt_id"),
        (stream_id, "pool_epoch"),
        (stream_id, "stream_generation"),
    ],
)
@pytest.mark.parametrize("bad_value", [True, -1, 1 << 63])
def test_identity_integer_fields_are_exact_nonnegative_signed_64_bit(
    factory, field: str, bad_value: object
):
    with pytest.raises((TypeError, ValueError), match=field):
        factory(**{field: bad_value})


def test_identity_integer_subclasses_are_rejected_and_maximum_is_accepted():
    class IntSubclass(int):
        pass

    with pytest.raises(TypeError, match="pool_epoch"):
        owner_id(pool_epoch=IntSubclass(7))
    exact = owner_id(
        pool_epoch=MAX_VALUE,
        stream_generation=MAX_VALUE,
        commit_attempt_id=MAX_VALUE,
    )
    exact_stream = stream_id(
        pool_epoch=MAX_VALUE,
        stream_generation=MAX_VALUE,
    )

    assert exact.pool_epoch == MAX_VALUE
    assert exact.stream_generation == MAX_VALUE
    assert exact.commit_attempt_id == MAX_VALUE
    assert exact_stream.stream_generation == MAX_VALUE


@pytest.mark.parametrize("bad_float", [math.nan, math.inf, -math.inf])
def test_diagnostic_metadata_rejects_non_finite_float(bad_float: float):
    with pytest.raises(ValueError, match="finite"):
        OwnerRemovalReceipt(
            owner_id(),
            OwnerRemovalOutcome.REMOVED,
            diagnostic_metadata=(("duration", bad_float),),
        )


@pytest.mark.parametrize("bad_int", [-(1 << 63), 1 << 63])
def test_diagnostic_metadata_rejects_unbounded_integer(bad_int: int):
    with pytest.raises(ValueError, match="integer"):
        OwnerRemovalReceipt(
            owner_id(),
            OwnerRemovalOutcome.REMOVED,
            diagnostic_metadata=(("retry", bad_int),),
        )


@pytest.mark.parametrize("ledger_version", [1, MAX_VALUE])
def test_ack_receipt_accepts_every_positive_bounded_ledger_version(
    ledger_version: int,
):
    receipt = FinalityAckReceipt(
        stream_instance_id=stream_id(),
        owner_instance_id=owner_id(),
        ledger_version=ledger_version,
        ack_token="token",
    )

    assert receipt.ledger_version == ledger_version


@pytest.mark.parametrize("ledger_version", [True, 0, -1, 1 << 63])
def test_ack_receipt_rejects_invalid_ledger_version(
    ledger_version: object,
):
    with pytest.raises((TypeError, ValueError), match="ledger_version"):
        FinalityAckReceipt(
            stream_instance_id=stream_id(),
            owner_instance_id=owner_id(),
            ledger_version=ledger_version,
            ack_token="token",
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda version: PrepareAdmission(owner_id(), version),
        lambda version: PrepareClaim(owner_id(), version, True),
        lambda version: _OwnerFinalityEntry(version),
        lambda version: OwnerFinalityAuthenticator(stream_id()).issue(
            owner_id(), version
        ),
        lambda version: OwnerFinalityAuthenticator(stream_id())._token(
            owner_id(), version
        ),
    ],
)
@pytest.mark.parametrize("ledger_version", [True, 0, -1, 1 << 63])
def test_every_ledger_version_carrier_rejects_non_positive_or_unbounded_values(
    factory, ledger_version: object
):
    with pytest.raises((TypeError, ValueError), match="ledger.version"):
        factory(ledger_version)


def test_every_ledger_version_carrier_rejects_integer_subclasses():
    class IntSubclass(int):
        pass

    factories = (
        lambda: FinalityAckReceipt(
            stream_instance_id=stream_id(),
            owner_instance_id=owner_id(),
            ledger_version=IntSubclass(1),
            ack_token="token",
        ),
        lambda: PrepareAdmission(owner_id(), IntSubclass(1)),
        lambda: PrepareClaim(owner_id(), IntSubclass(1), True),
        lambda: _OwnerFinalityEntry(IntSubclass(1)),
        lambda: OwnerFinalityAuthenticator(stream_id()).issue(
            owner_id(), IntSubclass(1)
        ),
        lambda: OwnerFinalityAuthenticator(stream_id())._token(
            owner_id(), IntSubclass(1)
        ),
    )

    for factory in factories:
        with pytest.raises(TypeError, match="ledger.version"):
            factory()


def test_retained_ledger_version_is_read_only_and_first_version_is_one():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()

    admission = ledger.reserve_prepare(identity)
    entry = ledger._entries[identity]

    assert isinstance(admission, PrepareAdmission)
    assert admission.ledger_version == 1
    assert entry.ledger_version == 1
    with pytest.raises(AttributeError):
        entry.ledger_version = 0
    with pytest.raises(AttributeError):
        entry._ledger_version = 0
    assert entry.ledger_version == 1
    assert ledger.snapshot().entry_count == 1


def test_zero_version_crypto_and_prune_attempts_do_not_mutate_ledger():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    reserve(ledger, identity)
    before = ledger.snapshot()
    authenticator = ledger._authenticator

    with pytest.raises(ValueError, match="ledger.version"):
        authenticator.issue(identity, 0)
    assert ledger.snapshot() == before
    forged = object.__new__(FinalityAckReceipt)
    object.__setattr__(forged, "stream_instance_id", stream_id())
    object.__setattr__(forged, "owner_instance_id", identity)
    object.__setattr__(forged, "ledger_version", 0)
    object.__setattr__(forged, "ack_token", "forged")

    assert authenticator.validates(identity, forged, 1) is False
    assert (
        ledger._prune_entry(identity, forged).outcome
        is FinalityPruneOutcome.MISMATCH
    )
    assert ledger.snapshot() == before


def test_ledger_version_exhaustion_fails_before_retaining_new_identity():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    protected = owner_id(instance_nonce="protected")
    reserve(ledger, protected)
    ledger._next_ledger_version = MAX_VALUE
    before = ledger.snapshot()

    with pytest.raises(OverflowError, match="ledger version"):
        ledger.reserve_prepare(owner_id(instance_nonce="new"))

    assert ledger.snapshot() == before


def test_hmac_canonicalizes_maximum_accepted_identity_and_version_values():
    exact_stream = stream_id(
        pool_epoch=MAX_VALUE,
        stream_generation=MAX_VALUE,
    )
    exact_owner = owner_id(
        pool_epoch=MAX_VALUE,
        stream_generation=MAX_VALUE,
        commit_attempt_id=MAX_VALUE,
    )
    authenticator = OwnerFinalityAuthenticator(exact_stream)

    receipt = authenticator.issue(exact_owner, MAX_VALUE)

    assert authenticator.validates(exact_owner, receipt, MAX_VALUE) is True


class ExpectedVersionIntSubclass(int):
    pass


class ExpectedVersionEqualitySpoof:
    def __eq__(self, _other: object) -> bool:
        return True


@pytest.mark.parametrize(
    "expected_version",
    [
        True,
        False,
        1.0,
        ExpectedVersionIntSubclass(1),
        0,
        -1,
        MAX_VALUE + 1,
        "1",
        object(),
        ExpectedVersionEqualitySpoof(),
    ],
)
def test_authenticator_rejects_malformed_expected_version_without_mutation(
    expected_version: object,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce="expected-version")
    admission = ledger.reserve_prepare(identity)
    assert isinstance(admission, PrepareAdmission)
    receipt = ledger._authenticator.issue(
        identity, admission.ledger_version
    )
    before = ledger.snapshot()

    result = ledger._authenticator.validates(
        identity, receipt, expected_version
    )

    assert type(result) is bool
    assert result is False
    assert ledger.snapshot() == before


def test_authenticator_none_is_stateless_and_exact_versions_validate():
    authenticator = OwnerFinalityAuthenticator(stream_id())
    identity = owner_id()
    receipt = authenticator.issue(identity, 1)
    maximum_receipt = authenticator.issue(identity, MAX_VALUE)

    assert authenticator.validates(identity, receipt, None) is True
    assert authenticator.validates(identity, receipt, 1) is True
    assert (
        authenticator.validates(
            identity, maximum_receipt, MAX_VALUE
        )
        is True
    )


def test_monotonic_diagnostic_counters_saturate_at_the_integer_bound():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce="retained")
    reserve(ledger, identity)
    ledger._backpressure_rejection_count = MAX_VALUE
    ledger.reserve_prepare(owner_id(instance_nonce="blocked"))
    assert ledger.snapshot().backpressure_rejection_count == MAX_VALUE
    ledger.mark_prepare_enqueued(identity)
    ledger.claim_prepare(identity)
    ledger.resolve_prepare(identity, activated=True)
    receipt = ledger.request_removal(identity, registration_present=True)
    ack = acknowledge(ledger, identity, receipt)
    ledger._pruned_entry_count = MAX_VALUE

    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert ledger.snapshot().pruned_entry_count == MAX_VALUE


@dataclasses.dataclass(frozen=True)
class ReferenceFacts:
    state: OwnerFinalityState
    pending: int
    entry_count: int = 1


def assert_facts(
    ledger: OwnerFinalityLedger, expected: ReferenceFacts
) -> None:
    snapshot = ledger.snapshot()
    assert snapshot.entry_count == expected.entry_count
    assert snapshot.entry_count <= snapshot.ledger_limit
    assert snapshot.pending_late_commands == expected.pending
    if expected.entry_count:
        assert snapshot.count(expected.state) == 1


@pytest.mark.parametrize(
    "removal_boundary",
    ["reserved", "queued", "claimed", "active"],
)
def test_reference_model_removal_at_each_prepare_boundary(
    removal_boundary: str,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce=removal_boundary)
    reserve(ledger, identity)
    assert_facts(
        ledger,
        ReferenceFacts(OwnerFinalityState.PREPARE_RESERVED, pending=1),
    )
    if removal_boundary != "reserved":
        ledger.mark_prepare_enqueued(identity)
        assert_facts(
            ledger,
            ReferenceFacts(OwnerFinalityState.PREPARE_PENDING, pending=1),
        )
    if removal_boundary in {"claimed", "active"}:
        ledger.claim_prepare(identity)
        assert ledger.authorize_prepare_publication(identity) is True
        assert_facts(
            ledger,
            ReferenceFacts(OwnerFinalityState.PREPARE_PENDING, pending=1),
        )
    if removal_boundary == "active":
        ledger.resolve_prepare(identity, activated=True)
        assert_facts(
            ledger, ReferenceFacts(OwnerFinalityState.ACTIVE, pending=0)
        )

    positive = ledger.request_removal(
        identity, registration_present=removal_boundary == "active"
    )
    if removal_boundary == "active":
        assert positive.outcome is OwnerRemovalOutcome.REMOVED
        assert_facts(
            ledger,
            ReferenceFacts(OwnerFinalityState.FINAL_REMOVED, pending=0),
        )
    else:
        assert positive.outcome is OwnerRemovalOutcome.INDETERMINATE
        assert_facts(
            ledger,
            ReferenceFacts(
                OwnerFinalityState.REMOVAL_REQUESTED, pending=1
            ),
        )
        forged = OwnerRemovalReceipt(
            identity, OwnerRemovalOutcome.TOMBSTONED_ABSENT
        )
        assert (
            ledger._acknowledge_entry(
                identity, forged, completed_progress(forged)
            )
            is None
        )
        if removal_boundary == "reserved":
            positive = ledger.abort_prepare_reservation(
                identity, command_not_enqueued=True
            )
        elif removal_boundary == "queued":
            positive = ledger.resolve_queued_prepare_cancellation(
                identity, cancelled=True
            )
        else:
            ledger.resolve_prepare(identity, activated=False)
            positive = ledger.request_removal(
                identity, registration_present=False
            )
        assert (
            positive.outcome
            is OwnerRemovalOutcome.TOMBSTONED_ABSENT
        )
        assert_facts(
            ledger,
            ReferenceFacts(OwnerFinalityState.FINAL_ABSENT, pending=0),
        )

    before_gate = ledger.snapshot()
    assert ledger.authorize_prepare_publication(identity) is False
    assert ledger.snapshot() == before_gate
    ack = acknowledge(ledger, identity, positive)
    assert_facts(
        ledger,
        ReferenceFacts(OwnerFinalityState.ACKNOWLEDGED, pending=0),
    )
    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert_facts(
        ledger,
        ReferenceFacts(
            OwnerFinalityState.ACKNOWLEDGED,
            pending=0,
            entry_count=0,
        ),
    )
    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.ALREADY_PRUNED
    )


def test_out_of_order_and_duplicate_operations_never_reauthorize():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    reserve(ledger, identity)
    before_abort = ledger.snapshot()
    assert (
        ledger.resolve_queued_prepare_cancellation(
            identity, cancelled=True
        ).outcome
        is OwnerRemovalOutcome.INDETERMINATE
    )
    assert ledger.snapshot() == before_abort
    ledger.mark_prepare_enqueued(identity)
    after_enqueue = ledger.snapshot()
    ledger.mark_prepare_enqueued(identity)
    assert ledger.snapshot() == after_enqueue
    ledger.claim_prepare(identity)
    ledger.resolve_prepare(identity, activated=False)
    resolved = ledger.snapshot()

    ledger.resolve_prepare(identity, activated=True)

    assert ledger.snapshot() == resolved
    assert ledger.authorize_prepare_publication(identity) is False
    with pytest.raises(PrepareConflictError, match="claim"):
        ledger.claim_prepare(identity)
    assert ledger.snapshot() == resolved


def assert_diagnostic_phase(
    ledger: OwnerFinalityLedger,
    *,
    state: OwnerFinalityState,
    pending: int,
    final_unacknowledged: int,
    acknowledged: int = 0,
) -> None:
    snapshot = ledger.snapshot()
    assert snapshot.entry_count == 1
    assert snapshot.count(state) == 1
    assert snapshot.pending_late_commands == pending
    assert (
        snapshot.final_unacknowledged_entries
        == final_unacknowledged
    )
    assert snapshot.acknowledged_awaiting_prune == acknowledged


@pytest.mark.parametrize(
    "last_pending_clear",
    [
        "open_abort",
        "queued_cancel",
        "claimed_rejected",
        "claimed_late_active",
    ],
)
def test_positive_removed_receipt_restores_deterministic_final_state(
    last_pending_clear: str,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce=last_pending_clear)
    reserve(ledger, identity)
    receipt = ledger.request_removal(
        identity, registration_present=True
    )

    assert receipt.outcome is OwnerRemovalOutcome.REMOVED
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        pending=1,
        final_unacknowledged=0,
    )
    if last_pending_clear == "open_abort":
        resolved = ledger.abort_prepare_reservation(
            identity, command_not_enqueued=True
        )
    else:
        ledger.mark_prepare_enqueued(identity)
        assert_diagnostic_phase(
            ledger,
            state=OwnerFinalityState.REMOVAL_REQUESTED,
            pending=1,
            final_unacknowledged=0,
        )
        if last_pending_clear == "queued_cancel":
            resolved = ledger.resolve_queued_prepare_cancellation(
                identity, cancelled=True
            )
        else:
            claim = ledger.claim_prepare(identity)
            assert claim.activation_allowed is False
            assert_diagnostic_phase(
                ledger,
                state=OwnerFinalityState.REMOVAL_REQUESTED,
                pending=1,
                final_unacknowledged=0,
            )
            ledger.resolve_prepare(
                identity,
                activated=last_pending_clear == "claimed_late_active",
            )
            resolved = ledger.request_removal(
                identity, registration_present=False
            )

    assert resolved == receipt
    assert resolved is not receipt
    assert resolved.owner_instance_id is not receipt.owner_instance_id
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.FINAL_REMOVED,
        pending=0,
        final_unacknowledged=1,
    )
    ack = acknowledge(ledger, identity, receipt)
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.ACKNOWLEDGED,
        pending=0,
        final_unacknowledged=0,
        acknowledged=1,
    )
    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    snapshot = ledger.snapshot()
    assert snapshot.entry_count == 0
    assert snapshot.pruned_entry_count == 1


@pytest.mark.parametrize(
    "last_pending_clear",
    ["open_abort", "queued_cancel", "claimed_rejected"],
)
def test_tombstoned_absent_orderings_share_the_same_final_diagnostics(
    last_pending_clear: str,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce=f"absent-{last_pending_clear}")
    reserve(ledger, identity)
    pending = ledger.request_removal(
        identity, registration_present=False
    )

    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        pending=1,
        final_unacknowledged=0,
    )
    if last_pending_clear == "open_abort":
        receipt = ledger.abort_prepare_reservation(
            identity, command_not_enqueued=True
        )
    else:
        ledger.mark_prepare_enqueued(identity)
        if last_pending_clear == "queued_cancel":
            receipt = ledger.resolve_queued_prepare_cancellation(
                identity, cancelled=True
            )
        else:
            claim = ledger.claim_prepare(identity)
            assert claim.activation_allowed is False
            ledger.resolve_prepare(identity, activated=False)
            receipt = ledger.request_removal(
                identity, registration_present=False
            )

    assert receipt.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.FINAL_ABSENT,
        pending=0,
        final_unacknowledged=1,
    )
    ack = acknowledge(ledger, identity, receipt)
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.ACKNOWLEDGED,
        pending=0,
        final_unacknowledged=0,
        acknowledged=1,
    )
    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert ledger.snapshot().entry_count == 0
    assert ledger.snapshot().pruned_entry_count == 1


def test_late_active_resolution_stays_requested_until_exact_removed_receipt():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce="late-active-confirmation")
    reserve(ledger, identity)
    pending = ledger.request_removal(
        identity, registration_present=False
    )
    ledger.mark_prepare_enqueued(identity)
    claim = ledger.claim_prepare(identity)

    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert claim.activation_allowed is False
    ledger.resolve_prepare(identity, activated=True)
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        pending=0,
        final_unacknowledged=0,
    )
    assert (
        ledger._acknowledge_entry(
            identity,
            OwnerRemovalReceipt(
                identity, OwnerRemovalOutcome.REMOVED
            ),
            completed_progress(
                OwnerRemovalReceipt(
                    identity, OwnerRemovalOutcome.REMOVED
                )
            ),
        )
        is None
    )

    receipt = ledger.request_removal(
        identity, registration_present=True
    )

    assert receipt.outcome is OwnerRemovalOutcome.REMOVED
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.FINAL_REMOVED,
        pending=0,
        final_unacknowledged=1,
    )


@pytest.mark.parametrize("resolution", ["handler_rejected", "enqueue_aborted"])
def test_resolved_without_registration_has_explicit_one_shot_state(
    resolution: str,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce=resolution)
    reserve(ledger, identity)
    if resolution == "handler_rejected":
        ledger.mark_prepare_enqueued(identity)
        ledger.claim_prepare(identity)
        ledger.resolve_prepare(identity, activated=False)
    else:
        result = ledger.abort_prepare_reservation(
            identity, command_not_enqueued=True
        )
        assert result.outcome is OwnerRemovalOutcome.INDETERMINATE

    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.PREPARE_RESOLVED_ABSENT,
        pending=0,
        final_unacknowledged=0,
    )
    resolved = ledger.snapshot()
    with pytest.raises(PrepareConflictError, match="already retained"):
        ledger.reserve_prepare(identity)
    ledger.mark_prepare_enqueued(identity)
    assert ledger.snapshot() == resolved
    with pytest.raises(PrepareConflictError, match="claim"):
        ledger.claim_prepare(identity)
    assert ledger.authorize_prepare_publication(identity) is False
    ledger.resolve_prepare(identity, activated=True)
    assert ledger.snapshot() == resolved

    receipt = ledger.request_removal(
        identity, registration_present=False
    )

    assert receipt.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.FINAL_ABSENT,
        pending=0,
        final_unacknowledged=1,
    )
    ack = acknowledge(ledger, identity, receipt)
    assert_diagnostic_phase(
        ledger,
        state=OwnerFinalityState.ACKNOWLEDGED,
        pending=0,
        final_unacknowledged=0,
        acknowledged=1,
    )
    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert ledger.snapshot().entry_count == 0


def entry_snapshot(
    *,
    state: OwnerFinalityState = OwnerFinalityState.PREPARE_RESERVED,
    **changes: object,
) -> OwnerFinalityEntrySnapshot:
    values = {
        "pool_epoch": 7,
        "hardware_id": "hw-1",
        "stream_generation": 3,
        "run_id": "run-9",
        "commit_attempt_id": 11,
        "instance_nonce": "owner-a",
        "state": state,
        "prepare_pending": state
        in (
            OwnerFinalityState.PREPARE_RESERVED,
            OwnerFinalityState.PREPARE_PENDING,
            OwnerFinalityState.REMOVAL_REQUESTED,
        ),
    }
    values.update(changes)
    return OwnerFinalityEntrySnapshot(**values)


def state_counts_for(
    entries: tuple[OwnerFinalityEntrySnapshot, ...],
) -> tuple[tuple[OwnerFinalityState, int], ...]:
    return tuple(
        (
            state,
            sum(entry.state is state for entry in entries),
        )
        for state in OwnerFinalityState
    )


def finality_snapshot(
    **changes: object,
) -> OwnerFinalitySnapshot:
    entries = changes.pop("entries", (entry_snapshot(),))
    derived_counts = (
        state_counts_for(entries)
        if type(entries) is tuple
        and all(type(entry) is OwnerFinalityEntrySnapshot for entry in entries)
        else state_counts_for((entry_snapshot(),))
    )
    values = {
        "stream_instance_id": stream_id(),
        "hardware_id": "hw-1",
        "entries": entries,
        "state_counts": derived_counts,
        "ledger_limit": 64,
        "pending_late_commands": 1,
        "final_unacknowledged_entries": 0,
        "acknowledged_awaiting_prune": 0,
        "pruned_entry_count": 0,
        "backpressure_rejection_count": 0,
    }
    values.update(changes)
    return OwnerFinalitySnapshot(**values)


class CarrierIntSubclass(int):
    pass


class CarrierStringSubclass(str):
    pass


class CarrierTupleSubclass(tuple):
    pass


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PrepareAdmission(object(), 1),
        lambda: PrepareClaim(object(), 1, True),
        lambda: PrepareClaim(owner_id(), 1, 1),
        lambda: PrepareClaim(owner_id(), 1, CarrierIntSubclass(1)),
    ],
)
def test_prepare_value_carriers_require_exact_runtime_types(factory):
    with pytest.raises(TypeError):
        factory()


@pytest.mark.parametrize(
    "change",
    [
        {"pool_epoch": object()},
        {"pool_epoch": True},
        {"pool_epoch": CarrierIntSubclass(7)},
        {"pool_epoch": -1},
        {"pool_epoch": MAX_VALUE + 1},
        {"pool_epoch": ""},
        {"pool_epoch": "x" * 257},
        {"hardware_id": object()},
        {"hardware_id": CarrierStringSubclass("hw-1")},
        {"hardware_id": ""},
        {"hardware_id": "x" * 257},
        {"stream_generation": True},
        {"stream_generation": -1},
        {"stream_generation": MAX_VALUE + 1},
        {"stream_generation": math.nan},
        {"run_id": object()},
        {"run_id": CarrierStringSubclass("run-9")},
        {"run_id": ""},
        {"run_id": "x" * 257},
        {"commit_attempt_id": object()},
        {"commit_attempt_id": True},
        {"commit_attempt_id": -1},
        {"commit_attempt_id": MAX_VALUE + 1},
        {"commit_attempt_id": ""},
        {"commit_attempt_id": "x" * 257},
        {"instance_nonce": object()},
        {"instance_nonce": CarrierStringSubclass("owner-a")},
        {"instance_nonce": ""},
        {"instance_nonce": "x" * 257},
        {"state": "PREPARE_RESERVED"},
        {"state": object()},
        {"prepare_pending": 1},
        {"prepare_pending": 0},
        {"prepare_pending": CarrierIntSubclass(1)},
        {"prepare_pending": object()},
    ],
)
def test_entry_snapshot_rejects_malformed_or_unbounded_fields(
    change: dict[str, object],
):
    with pytest.raises((TypeError, ValueError)):
        entry_snapshot(**change)


def malformed_state_counts(
    kind: str,
) -> object:
    valid = list(state_counts_for((entry_snapshot(),)))
    if kind == "missing":
        return tuple(valid[:-1])
    if kind == "duplicate":
        valid[-1] = valid[0]
        return tuple(valid)
    if kind == "wrong_count":
        valid[0] = (valid[0][0], 2)
        return tuple(valid)
    if kind == "wrong_state":
        valid[0] = ("PREPARE_RESERVED", 1)
        return tuple(valid)
    if kind == "bool_count":
        valid[0] = (valid[0][0], True)
        return tuple(valid)
    if kind == "negative_count":
        valid[0] = (valid[0][0], -1)
        return tuple(valid)
    if kind == "huge_count":
        valid[0] = (valid[0][0], MAX_VALUE + 1)
        return tuple(valid)
    if kind == "item_subclass":
        valid[0] = CarrierTupleSubclass(valid[0])
        return tuple(valid)
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "change",
    [
        {"stream_instance_id": object()},
        {"hardware_id": ""},
        {"hardware_id": "x" * 257},
        {"hardware_id": CarrierStringSubclass("hw-1")},
        {"hardware_id": "hw-2"},
        {"entries": []},
        {"entries": CarrierTupleSubclass((entry_snapshot(),))},
        {"entries": (object(),)},
        {
            "entries": (
                entry_snapshot(pool_epoch=8),
            )
        },
        {
            "entries": (
                entry_snapshot(hardware_id="hw-2"),
            )
        },
        {
            "entries": (
                entry_snapshot(stream_generation=4),
            )
        },
        {
            "entries": tuple(
                entry_snapshot(instance_nonce=f"owner-{index}")
                for index in range(65)
            )
        },
        {"state_counts": []},
        {
            "state_counts": CarrierTupleSubclass(
                state_counts_for((entry_snapshot(),))
            )
        },
        *[
            {"state_counts": malformed_state_counts(kind)}
            for kind in (
                "missing",
                "duplicate",
                "wrong_count",
                "wrong_state",
                "bool_count",
                "negative_count",
                "huge_count",
                "item_subclass",
            )
        ],
        {"ledger_limit": True},
        {"ledger_limit": CarrierIntSubclass(1)},
        {"ledger_limit": 0},
        {"ledger_limit": 65},
        {
            "entries": (entry_snapshot(), entry_snapshot()),
            "ledger_limit": 1,
        },
        {"pending_late_commands": True},
        {"pending_late_commands": -1},
        {"pending_late_commands": MAX_VALUE + 1},
        {"pending_late_commands": math.inf},
        {"pending_late_commands": 2},
        {"final_unacknowledged_entries": True},
        {"final_unacknowledged_entries": -1},
        {"final_unacknowledged_entries": MAX_VALUE + 1},
        {"final_unacknowledged_entries": 1},
        {"acknowledged_awaiting_prune": True},
        {"acknowledged_awaiting_prune": -1},
        {"acknowledged_awaiting_prune": MAX_VALUE + 1},
        {"acknowledged_awaiting_prune": 1},
        {"pruned_entry_count": True},
        {"pruned_entry_count": -1},
        {"pruned_entry_count": MAX_VALUE + 1},
        {"backpressure_rejection_count": True},
        {"backpressure_rejection_count": -1},
        {"backpressure_rejection_count": MAX_VALUE + 1},
    ],
)
def test_finality_snapshot_rejects_malformed_shapes_counts_and_bounds(
    change: dict[str, object],
):
    with pytest.raises((TypeError, ValueError)):
        finality_snapshot(**change)


def test_snapshot_accepts_exact_final_and_acknowledged_count_projections():
    final_entry = entry_snapshot(state=OwnerFinalityState.FINAL_REMOVED)
    final = finality_snapshot(
        entries=(final_entry,),
        state_counts=state_counts_for((final_entry,)),
        pending_late_commands=0,
        final_unacknowledged_entries=1,
    )
    ack_entry = entry_snapshot(state=OwnerFinalityState.ACKNOWLEDGED)
    acknowledged = finality_snapshot(
        entries=(ack_entry,),
        state_counts=state_counts_for((ack_entry,)),
        pending_late_commands=0,
        acknowledged_awaiting_prune=1,
    )

    assert final.final_unacknowledged_entries == 1
    assert acknowledged.acknowledged_awaiting_prune == 1


def test_ledger_and_snapshot_limit_cannot_exceed_production_bound():
    with pytest.raises(ValueError, match="limit"):
        OwnerFinalityLedger(stream_id(), limit=65)


def test_rejected_carrier_payloads_are_not_strongly_retained():
    class Payload:
        pass

    class RetainingTuple(tuple):
        pass

    def reject_payload() -> weakref.ReferenceType[Payload]:
        payload = Payload()
        reference = weakref.ref(payload)
        retaining_entries = RetainingTuple((entry_snapshot(),))
        retaining_entries.payload = payload
        with pytest.raises(TypeError):
            finality_snapshot(entries=retaining_entries)
        return reference

    reference = reject_payload()
    gc.collect()

    assert reference() is None


@pytest.mark.parametrize(
    "state",
    [
        OwnerFinalityState.ACTIVE,
        OwnerFinalityState.PREPARE_RESOLVED_ABSENT,
        OwnerFinalityState.FINAL_REMOVED,
        OwnerFinalityState.FINAL_ABSENT,
        OwnerFinalityState.ACKNOWLEDGED,
    ],
)
def test_nonpending_entry_states_reject_pending_prepare_fact(
    state: OwnerFinalityState,
):
    with pytest.raises(ValueError, match="prepare_pending"):
        entry_snapshot(state=state, prepare_pending=True)


@pytest.mark.parametrize(
    "state",
    [
        OwnerFinalityState.PREPARE_RESERVED,
        OwnerFinalityState.PREPARE_PENDING,
    ],
)
def test_pending_entry_states_require_pending_prepare_fact(
    state: OwnerFinalityState,
):
    with pytest.raises(ValueError, match="prepare_pending"):
        entry_snapshot(state=state, prepare_pending=False)


def test_removal_requested_accepts_pending_and_resolved_forms():
    pending = entry_snapshot(
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        prepare_pending=True,
    )
    resolved = entry_snapshot(
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        prepare_pending=False,
    )
    pending_snapshot = finality_snapshot(
        entries=(pending,),
        state_counts=state_counts_for((pending,)),
        pending_late_commands=1,
    )
    resolved_snapshot = finality_snapshot(
        entries=(resolved,),
        state_counts=state_counts_for((resolved,)),
        pending_late_commands=0,
    )

    assert pending_snapshot.pending_late_commands == 1
    assert resolved_snapshot.pending_late_commands == 0


@pytest.mark.parametrize(
    ("entry", "aggregate"),
    [
        (
            entry_snapshot(
                state=OwnerFinalityState.PREPARE_RESOLVED_ABSENT,
                prepare_pending=False,
            ),
            1,
        ),
        (
            entry_snapshot(
                state=OwnerFinalityState.PREPARE_RESERVED,
                prepare_pending=True,
            ),
            0,
        ),
        (
            entry_snapshot(
                state=OwnerFinalityState.REMOVAL_REQUESTED,
                prepare_pending=True,
            ),
            0,
        ),
        (
            entry_snapshot(
                state=OwnerFinalityState.REMOVAL_REQUESTED,
                prepare_pending=False,
            ),
            1,
        ),
    ],
)
def test_snapshot_pending_aggregate_must_equal_entry_facts(
    entry: OwnerFinalityEntrySnapshot, aggregate: int
):
    with pytest.raises(ValueError, match="pending"):
        finality_snapshot(
            entries=(entry,),
            state_counts=state_counts_for((entry,)),
            pending_late_commands=aggregate,
        )


def assert_ledger_pending_projection(
    ledger: OwnerFinalityLedger,
    *,
    state: OwnerFinalityState,
    prepare_pending: bool,
) -> None:
    snapshot = ledger.snapshot()
    assert snapshot.entry_count == 1
    assert snapshot.entries[0].state is state
    assert type(snapshot.entries[0].prepare_pending) is bool
    assert snapshot.entries[0].prepare_pending is prepare_pending
    assert snapshot.pending_late_commands == int(prepare_pending)


def test_ledger_snapshot_pending_fact_tracks_every_prepare_boundary():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce="pending-projection")
    reserve(ledger, identity)
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.PREPARE_RESERVED,
        prepare_pending=True,
    )
    ledger.mark_prepare_enqueued(identity)
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.PREPARE_PENDING,
        prepare_pending=True,
    )
    ledger.claim_prepare(identity)
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.PREPARE_PENDING,
        prepare_pending=True,
    )
    pending = ledger.request_removal(
        identity, registration_present=False
    )
    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        prepare_pending=True,
    )
    ledger.resolve_prepare(identity, activated=True)
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.REMOVAL_REQUESTED,
        prepare_pending=False,
    )
    receipt = ledger.request_removal(
        identity, registration_present=True
    )
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.FINAL_REMOVED,
        prepare_pending=False,
    )
    ack = acknowledge(ledger, identity, receipt)
    assert_ledger_pending_projection(
        ledger,
        state=OwnerFinalityState.ACKNOWLEDGED,
        prepare_pending=False,
    )
    assert (
        ledger._prune_entry(identity, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert ledger.snapshot().entries == ()
    assert ledger.snapshot().pending_late_commands == 0


@pytest.mark.parametrize(
    ("terminal_stage", "expected_state"),
    [
        ("active", OwnerFinalityState.ACTIVE),
        (
            "resolved_absent",
            OwnerFinalityState.PREPARE_RESOLVED_ABSENT,
        ),
    ],
)
def test_ledger_snapshot_nonpending_prepare_outcomes_are_exact(
    terminal_stage: str, expected_state: OwnerFinalityState
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id(instance_nonce=terminal_stage)
    reserve(ledger, identity)
    ledger.mark_prepare_enqueued(identity)
    ledger.claim_prepare(identity)
    ledger.resolve_prepare(
        identity, activated=terminal_stage == "active"
    )

    assert_ledger_pending_projection(
        ledger,
        state=expected_state,
        prepare_pending=False,
    )
