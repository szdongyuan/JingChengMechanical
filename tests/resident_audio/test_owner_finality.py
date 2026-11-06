from __future__ import annotations

import dataclasses
import gc
import inspect
import threading
import weakref

import pytest

from app.service.analysis.resident_audio import stream_types
from app.service.analysis.resident_audio.owner_identity import (
    FinalityAckReceipt,
    FinalityPruneOutcome,
    FinalityPruneReceipt,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerNonAdmissionReason,
    OwnerRemovalOutcome,
    OwnerRemovalReceipt,
    StreamInstanceId,
)
from app.service.analysis.resident_audio.owner_finality import (
    FinalityDurableProgress,
    OwnerFinalityLedger,
    OwnerFinalityState,
    PrepareAdmission,
)
def owner_id(*, nonce: str = "nonce-a", capability: str = "owner-cap"):
    return OwnerInstanceId(
        pool_epoch=7,
        hardware_id="hw-1",
        stream_generation=3,
        run_id="run-9",
        commit_attempt_id=11,
        owner_capability=capability,
        instance_nonce=nonce,
    )


def stream_id(*, nonce: str = "stream-a"):
    return StreamInstanceId(
        pool_epoch=7,
        hardware_id="hw-1",
        stream_generation=3,
        instance_nonce=nonce,
    )


def test_owner_instance_id_is_hashable_value_only_and_nonce_distinguishes_reuse():
    first = owner_id()
    second = dataclasses.replace(first, instance_nonce="nonce-b")

    assert first != second
    assert {first: "first", second: "second"} == {
        first: "first",
        second: "second",
    }
    assert dataclasses.is_dataclass(first)
    assert first.__dataclass_params__.frozen
    assert not hasattr(first, "__dict__")


def test_removal_receipt_confirms_only_exact_positive_instance():
    first = owner_id()
    second = dataclasses.replace(first, instance_nonce="nonce-b")

    assert OwnerRemovalReceipt(
        first, OwnerRemovalOutcome.REMOVED
    ).confirms(first)
    assert OwnerRemovalReceipt(
        first, OwnerRemovalOutcome.TOMBSTONED_ABSENT
    ).confirms(first)
    assert not OwnerRemovalReceipt(
        first, OwnerRemovalOutcome.MISMATCH
    ).confirms(first)
    assert not OwnerRemovalReceipt(
        first, OwnerRemovalOutcome.INDETERMINATE
    ).confirms(first)
    assert not OwnerRemovalReceipt(
        first, OwnerRemovalOutcome.REMOVED
    ).confirms(second)


def test_non_admission_proof_requires_pre_enqueue_unclaimed_command():
    instance_id = owner_id()
    valid = OwnerNonAdmissionProof(
        owner_instance_id=instance_id,
        reason=OwnerNonAdmissionReason.LEDGER_FULL,
        command_enqueued=False,
        command_claimed=False,
    )

    assert valid.confirms(instance_id)
    assert not dataclasses.replace(valid, command_enqueued=True).confirms(
        instance_id
    )
    assert not dataclasses.replace(valid, command_claimed=True).confirms(
        instance_id
    )
    assert not valid.confirms(dataclasses.replace(instance_id, instance_nonce="b"))


def test_receipts_and_diagnostics_do_not_retain_owner_objects():
    class Owner:
        pass

    owner = Owner()
    owner_reference = weakref.ref(owner)
    receipt = OwnerRemovalReceipt(
        owner_id(),
        OwnerRemovalOutcome.REMOVED,
        diagnostic_metadata=(("retry_count", 2), ("phase", "remove")),
    )
    ack = FinalityAckReceipt(
        stream_instance_id=stream_id(),
        owner_instance_id=owner_id(),
        ledger_version=4,
        ack_token="authenticated-value",
    )

    del owner
    gc.collect()

    assert owner_reference() is None
    assert {
        field.name for field in dataclasses.fields(receipt)
    } == {"owner_instance_id", "outcome", "diagnostic_metadata"}
    assert all(
        isinstance(value, (str, int, float, bool, type(None)))
        for pair in receipt.diagnostic_metadata
        for value in pair
    )
    assert all(
        field.name not in {"owner", "writer", "ring", "callback", "stream"}
        for value in (receipt, ack)
        for field in dataclasses.fields(value)
    )


def test_removal_receipt_rejects_unbounded_or_non_scalar_metadata():
    with pytest.raises(ValueError, match="at most"):
        OwnerRemovalReceipt(
            owner_id(),
            OwnerRemovalOutcome.REMOVED,
            diagnostic_metadata=tuple((str(index), index) for index in range(9)),
        )
    with pytest.raises(TypeError, match="scalar"):
        OwnerRemovalReceipt(
            owner_id(),
            OwnerRemovalOutcome.REMOVED,
            diagnostic_metadata=(("payload", object()),),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"pool_epoch": object()},
        {"hardware_id": object()},
        {"stream_generation": object()},
        {"stream_generation": True},
        {"run_id": object()},
        {"commit_attempt_id": object()},
        {"owner_capability": object()},
        {"instance_nonce": object()},
    ],
)
def test_owner_instance_id_rejects_object_bearing_or_non_permitted_values(change):
    with pytest.raises(TypeError, match="OwnerInstanceId"):
        dataclasses.replace(owner_id(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"pool_epoch": object()},
        {"hardware_id": object()},
        {"stream_generation": object()},
        {"stream_generation": True},
        {"instance_nonce": object()},
    ],
)
def test_stream_instance_id_rejects_object_bearing_or_non_permitted_values(
    change,
):
    with pytest.raises(TypeError, match="StreamInstanceId"):
        dataclasses.replace(stream_id(), **change)


def test_canonical_removal_receipt_requires_exact_owner_instance_id():
    assert stream_types.OwnerInstanceId is OwnerInstanceId
    assert stream_types.OwnerRemovalReceipt is OwnerRemovalReceipt
    with pytest.raises(TypeError, match="OwnerInstanceId"):
        OwnerRemovalReceipt(object(), OwnerRemovalOutcome.REMOVED)
    with pytest.raises(TypeError, match="OwnerRemovalOutcome"):
        OwnerRemovalReceipt(owner_id(), "REMOVED")

    receipt = OwnerRemovalReceipt(
        owner_id(), OwnerRemovalOutcome.REMOVED
    )
    assert not hasattr(receipt, "owner")
    assert not hasattr(receipt, "identity")


def test_stream_types_has_no_legacy_owner_identity_or_receipt_contract():
    assert not hasattr(stream_types, "OwnerRegistrationIdentity")
    assert not hasattr(stream_types, "_LegacyOwnerRemovalReceipt")


def admitted(
    ledger: OwnerFinalityLedger, instance_id: OwnerInstanceId
):
    admission = ledger.reserve_prepare(instance_id)
    assert isinstance(admission, PrepareAdmission)
    return admission


def claim_and_resolve(
    ledger: OwnerFinalityLedger,
    instance_id: OwnerInstanceId,
    *,
    activated: bool,
):
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    claim = ledger.claim_prepare(instance_id)
    assert claim.activation_allowed
    ledger.resolve_prepare(instance_id, activated=activated)


def completed_progress(receipt: OwnerRemovalReceipt):
    return FinalityDurableProgress(
        removal_receipt=receipt,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )


def removed_active(
    ledger: OwnerFinalityLedger, instance_id: OwnerInstanceId
):
    claim_and_resolve(ledger, instance_id, activated=True)
    receipt = ledger.request_removal(
        instance_id, registration_present=True
    )
    assert receipt.outcome is OwnerRemovalOutcome.REMOVED
    return receipt


def acknowledge_removed(
    ledger: OwnerFinalityLedger, instance_id: OwnerInstanceId
):
    receipt = removed_active(ledger, instance_id)
    ack = ledger._acknowledge_entry(
        instance_id, receipt, completed_progress(receipt)
    )
    assert isinstance(ack, FinalityAckReceipt)
    return ack


def test_prepare_reserves_before_enqueue_and_finally_resolves_pending_marker():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    assert ledger.snapshot().count(
        OwnerFinalityState.PREPARE_RESERVED
    ) == 1

    ledger.mark_prepare_enqueued(instance_id)
    claim = ledger.claim_prepare(instance_id)
    assert claim.activation_allowed
    try:
        raise RuntimeError("prepare handler rejected")
    except RuntimeError:
        pass
    finally:
        ledger.resolve_prepare(instance_id, activated=False)

    assert ledger.snapshot().pending_late_commands == 0
    receipt = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert receipt.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT


def test_remove_queued_prepare_tombstones_and_blocks_late_activation():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)

    receipt = ledger.request_removal(
        instance_id, registration_present=False
    )

    assert receipt.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert ledger.snapshot().pending_late_commands == 1
    assert ledger.snapshot().count(
        OwnerFinalityState.REMOVAL_REQUESTED
    ) == 1
    claimed = ledger.claim_prepare(instance_id)
    assert not claimed.activation_allowed
    assert ledger.snapshot().pending_late_commands == 1
    ledger.resolve_prepare(instance_id, activated=False)
    resolved = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert resolved.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT


def test_queued_prepare_requires_positive_cancellation_before_final_absence():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    ledger.request_removal(instance_id, registration_present=False)

    not_cancelled = ledger.resolve_queued_prepare_cancellation(
        instance_id, cancelled=False
    )
    assert not_cancelled.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert ledger.snapshot().pending_late_commands == 1
    cancelled = ledger.resolve_queued_prepare_cancellation(
        instance_id, cancelled=True
    )
    assert (
        cancelled.outcome
        is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    )
    assert ledger.snapshot().pending_late_commands == 0


@pytest.mark.parametrize("command_state", ["queued", "claimed"])
def test_acknowledgement_and_prune_stay_blocked_while_prepare_is_unresolved(
    command_state,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    if command_state == "claimed":
        ledger.claim_prepare(instance_id)
    pending = ledger.request_removal(
        instance_id, registration_present=False
    )
    forged_positive = OwnerRemovalReceipt(
        instance_id, OwnerRemovalOutcome.TOMBSTONED_ABSENT
    )

    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert (
        ledger._acknowledge_entry(
            instance_id,
            forged_positive,
            completed_progress(forged_positive),
        )
        is None
    )
    forged_ack = FinalityAckReceipt(
        stream_instance_id=stream_id(),
        owner_instance_id=instance_id,
        ledger_version=1,
        ack_token="forged",
    )
    assert (
        ledger._prune_entry(instance_id, forged_ack).outcome.value
        == "MISMATCH"
    )
    assert ledger.snapshot().entry_count == 1


def test_prepare_publication_gate_is_pure_exact_bool_and_state_preserving():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    allowed = owner_id(nonce="allowed")
    tombstoned = owner_id(nonce="tombstoned")

    admitted(ledger, allowed)
    ledger.mark_prepare_enqueued(allowed)
    assert ledger.claim_prepare(allowed).activation_allowed
    before_allowed = ledger.snapshot()
    authorization = ledger.authorize_prepare_publication(allowed)
    assert type(authorization) is bool
    assert authorization is True
    assert ledger.snapshot() == before_allowed
    ledger.resolve_prepare(allowed, activated=True)

    admitted(ledger, tombstoned)
    ledger.mark_prepare_enqueued(tombstoned)
    assert ledger.claim_prepare(tombstoned).activation_allowed
    ledger.request_removal(tombstoned, registration_present=False)
    before_tombstone = ledger.snapshot()
    authorization = ledger.authorize_prepare_publication(tombstoned)
    assert type(authorization) is bool
    assert authorization is False
    assert ledger.snapshot() == before_tombstone
    ledger.resolve_prepare(tombstoned, activated=False)


def test_resolve_activated_after_removal_race_requires_exact_removal_confirmation():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    ledger.claim_prepare(instance_id)
    pending = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE

    ledger.resolve_prepare(instance_id, activated=True)

    assert ledger.snapshot().count(
        OwnerFinalityState.REMOVAL_REQUESTED
    ) == 1
    forged_positive = OwnerRemovalReceipt(
        instance_id, OwnerRemovalOutcome.REMOVED
    )
    assert (
        ledger._acknowledge_entry(
            instance_id,
            forged_positive,
            completed_progress(forged_positive),
        )
        is None
    )
    exact = ledger.request_removal(
        instance_id, registration_present=True
    )
    assert exact.outcome is OwnerRemovalOutcome.REMOVED


def test_remove_claimed_prepare_waits_for_resolution_before_positive_finality():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    assert ledger.claim_prepare(instance_id).activation_allowed

    pending = ledger.request_removal(
        instance_id, registration_present=False
    )

    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert ledger.snapshot().pending_late_commands == 1
    assert ledger.snapshot().count(
        OwnerFinalityState.REMOVAL_REQUESTED
    ) == 1
    ledger.resolve_prepare(instance_id, activated=False)
    resolved = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert resolved.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT


def test_remove_active_exact_instance_never_removes_replacement():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    old = owner_id(nonce="old")
    replacement = owner_id(nonce="replacement")
    claim_and_resolve(ledger, old, activated=True)
    claim_and_resolve(ledger, replacement, activated=True)

    assert ledger.request_removal(
        old, registration_present=True
    ).confirms(old)
    assert ledger.snapshot().count(OwnerFinalityState.ACTIVE) == 1
    stale = dataclasses.replace(old, owner_capability="wrong")
    assert (
        ledger.request_removal(stale, registration_present=True).outcome
        is OwnerRemovalOutcome.MISMATCH
    )
    assert ledger.snapshot().count(OwnerFinalityState.ACTIVE) == 1


def test_repeated_exact_remove_is_idempotent_without_list_growth():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    first = removed_active(ledger, instance_id)

    for _ in range(20):
        assert (
            ledger.request_removal(
                instance_id, registration_present=False
            )
            == first
        )

    snapshot = ledger.snapshot()
    assert snapshot.entry_count == 1
    assert snapshot.count(OwnerFinalityState.FINAL_REMOVED) == 1
    assert not any(
        isinstance(value, list)
        for value in vars(ledger).values()
    )


@pytest.mark.parametrize(
    "progress_change",
    [
        {"removal_receipt": None},
        {"owner_signaled": False},
        {"owner_joined": False},
        {"late_owner_commands_resolved": False},
        {"remove_retry_disabled": False},
    ],
)
def test_ack_requires_positive_receipt_join_and_resolved_late_commands(
    progress_change,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    receipt = removed_active(ledger, instance_id)
    progress = dataclasses.replace(
        completed_progress(receipt), **progress_change
    )

    assert (
        ledger._acknowledge_entry(instance_id, receipt, progress)
        is None
    )
    assert ledger.snapshot().count(
        OwnerFinalityState.FINAL_REMOVED
    ) == 1


def test_ack_is_stable_after_lost_response():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    receipt = removed_active(ledger, instance_id)
    progress = completed_progress(receipt)

    lost_response = ledger._acknowledge_entry(
        instance_id, receipt, progress
    )
    retried = ledger._acknowledge_entry(instance_id, receipt, progress)

    assert isinstance(lost_response, FinalityAckReceipt)
    assert retried == lost_response
    assert retried is not lost_response
    assert retried.stream_instance_id is not lost_response.stream_instance_id
    assert retried.owner_instance_id is not lost_response.owner_instance_id
    assert ledger.snapshot().acknowledged_awaiting_prune == 1


def test_prune_validates_exact_hmac_receipt_and_is_stateless_after_prune():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    ack = acknowledge_removed(ledger, instance_id)

    first = ledger._prune_entry(instance_id, ack)
    retried = ledger._prune_entry(instance_id, ack)

    assert first.outcome.value == "PRUNED"
    assert retried.outcome.value == "ALREADY_PRUNED"
    assert ledger.snapshot().entry_count == 0
    assert ledger.snapshot().pruned_entry_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"stream_instance_id": stream_id(nonce="replacement")},
        {"owner_instance_id": owner_id(nonce="other")},
        {"ledger_version": 999},
        {"ack_token": "forged"},
    ],
)
def test_prune_rejects_wrong_stream_owner_version_and_token(change):
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id()
    ack = acknowledge_removed(ledger, instance_id)
    forged = dataclasses.replace(ack, **change)

    mismatch = ledger._prune_entry(instance_id, forged)

    assert mismatch.outcome.value == "MISMATCH"
    assert ledger.snapshot().acknowledged_awaiting_prune == 1
    assert ledger._prune_entry(instance_id, ack).outcome.value == "PRUNED"


def test_ledger_limit_rejects_before_enqueue_without_evicting_entries():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    first = owner_id(nonce="first")
    second = owner_id(nonce="second")
    third = owner_id(nonce="third")
    admitted(ledger, first)
    ledger.mark_prepare_enqueued(first)
    claim_and_resolve(ledger, second, activated=True)
    enqueue_spy: list[OwnerInstanceId] = []

    proof = ledger.reserve_prepare(third)
    if isinstance(proof, PrepareAdmission):
        enqueue_spy.append(third)

    assert isinstance(proof, OwnerNonAdmissionProof)
    assert proof.owner_instance_id == third
    assert proof.reason is OwnerNonAdmissionReason.LEDGER_FULL
    assert proof.command_enqueued is False
    assert proof.command_claimed is False
    assert enqueue_spy == []
    snapshot = ledger.snapshot()
    assert snapshot.entry_count == 2
    assert snapshot.count(OwnerFinalityState.PREPARE_PENDING) == 1
    assert snapshot.count(OwnerFinalityState.ACTIVE) == 1


def test_pruned_slot_is_reusable_and_backpressure_counter_is_bounded():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    first = owner_id(nonce="first")
    blocked = owner_id(nonce="blocked")
    replacement = owner_id(nonce="replacement")
    acknowledge_removed(ledger, first)
    assert isinstance(
        ledger.reserve_prepare(blocked), OwnerNonAdmissionProof
    )
    ack = ledger._acknowledge_entry(
        first,
        ledger.request_removal(first, registration_present=False),
        completed_progress(
            ledger.request_removal(first, registration_present=False)
        ),
    )
    assert isinstance(ack, FinalityAckReceipt)
    assert ledger._prune_entry(first, ack).outcome.value == "PRUNED"

    admitted(ledger, replacement)
    snapshot = ledger.snapshot()
    assert snapshot.entry_count == 1
    assert snapshot.backpressure_rejection_count == 1
    assert snapshot.pruned_entry_count == 1


def test_stream_diagnostics_report_every_required_bounded_count():
    ledger = OwnerFinalityLedger(stream_id(), limit=8)
    reserved = owner_id(nonce="reserved")
    pending = owner_id(nonce="pending")
    resolved = owner_id(nonce="resolved")
    active = owner_id(nonce="active")
    removal = owner_id(nonce="removal")
    final_removed = owner_id(nonce="removed")
    final_absent = owner_id(nonce="absent")
    acknowledged = owner_id(nonce="ack")

    admitted(ledger, reserved)
    admitted(ledger, pending)
    ledger.mark_prepare_enqueued(pending)
    claim_and_resolve(ledger, resolved, activated=False)
    admitted(ledger, removal)
    ledger.mark_prepare_enqueued(removal)
    ledger.claim_prepare(removal)
    ledger.request_removal(removal, registration_present=False)
    claim_and_resolve(ledger, active, activated=True)
    removed_active(ledger, final_removed)
    admitted(ledger, final_absent)
    ledger.request_removal(final_absent, registration_present=False)
    ledger.abort_prepare_reservation(
        final_absent, command_not_enqueued=True
    )
    acknowledge_removed(ledger, acknowledged)

    snapshot = ledger.snapshot()
    assert snapshot.stream_instance_id == stream_id()
    assert snapshot.hardware_id == "hw-1"
    assert snapshot.ledger_limit == 8
    assert {
        state: snapshot.count(state) for state in OwnerFinalityState
    } == {state: 1 for state in OwnerFinalityState}
    assert snapshot.pending_late_commands == 3
    assert snapshot.final_unacknowledged_entries == 2
    assert snapshot.acknowledged_awaiting_prune == 1
    assert snapshot.pruned_entry_count == 0
    assert snapshot.backpressure_rejection_count == 0


def test_stream_diagnostics_keep_ids_and_counts_but_redact_capability_and_tokens():
    ledger = OwnerFinalityLedger(stream_id(), limit=2)
    instance_id = owner_id(
        nonce="diagnostic-nonce", capability="must-not-appear"
    )
    ack = acknowledge_removed(ledger, instance_id)

    serialized = repr(dataclasses.asdict(ledger.snapshot()))

    assert "run-9" in serialized
    assert "diagnostic-nonce" in serialized
    assert "FINAL" not in ack.ack_token
    assert "must-not-appear" not in serialized
    assert ack.ack_token not in serialized
    assert "_secret" not in serialized


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"owner_instance_id": object()}, TypeError),
        ({"reason": "LEDGER_FULL"}, TypeError),
        ({"command_enqueued": 0}, TypeError),
        ({"command_enqueued": 1}, TypeError),
        ({"command_claimed": 0}, TypeError),
        ({"command_claimed": 1}, TypeError),
    ],
)
def test_non_admission_proof_rejects_non_exact_fields(change, error):
    values = {
        "owner_instance_id": owner_id(),
        "reason": OwnerNonAdmissionReason.LEDGER_FULL,
        "command_enqueued": False,
        "command_claimed": False,
    }
    values.update(change)

    with pytest.raises(error, match="OwnerNonAdmissionProof"):
        OwnerNonAdmissionProof(**values)


def test_non_admission_confirms_requires_exact_false_not_falsiness():
    malformed = object.__new__(OwnerNonAdmissionProof)
    object.__setattr__(malformed, "owner_instance_id", owner_id())
    object.__setattr__(
        malformed, "reason", OwnerNonAdmissionReason.LEDGER_FULL
    )
    object.__setattr__(malformed, "command_enqueued", 0)
    object.__setattr__(malformed, "command_claimed", 0)

    assert not malformed.confirms(owner_id())


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"stream_instance_id": object()}, TypeError),
        ({"owner_instance_id": object()}, TypeError),
        ({"ledger_version": True}, TypeError),
        ({"ledger_version": "1"}, TypeError),
        ({"ledger_version": object()}, TypeError),
        ({"ledger_version": 0}, ValueError),
        ({"ledger_version": -1}, ValueError),
        ({"ledger_version": 1 << 63}, ValueError),
        ({"ack_token": object()}, TypeError),
        ({"ack_token": 7}, TypeError),
        ({"ack_token": "x" * 257}, ValueError),
    ],
)
def test_finality_ack_receipt_rejects_non_exact_or_unbounded_fields(
    change, error
):
    values = {
        "stream_instance_id": stream_id(),
        "owner_instance_id": owner_id(),
        "ledger_version": 1,
        "ack_token": "authenticated-value",
    }
    values.update(change)

    with pytest.raises(error, match="FinalityAckReceipt"):
        FinalityAckReceipt(**values)


@pytest.mark.parametrize(
    "change",
    [
        {"owner_instance_id": object()},
        {"outcome": "PRUNED"},
        {"outcome": object()},
    ],
)
def test_finality_prune_receipt_rejects_non_exact_fields(change):
    values = {
        "owner_instance_id": owner_id(),
        "outcome": FinalityPruneOutcome.PRUNED,
    }
    values.update(change)

    with pytest.raises(TypeError, match="FinalityPruneReceipt"):
        FinalityPruneReceipt(**values)


def test_public_proof_and_receipt_boundaries_do_not_retain_objects():
    class Payload:
        pass

    proof_payload = Payload()
    ack_payload = Payload()
    prune_payload = Payload()
    references = (
        weakref.ref(proof_payload),
        weakref.ref(ack_payload),
        weakref.ref(prune_payload),
    )

    with pytest.raises(TypeError):
        OwnerNonAdmissionProof(
            owner_instance_id=proof_payload,
            reason=OwnerNonAdmissionReason.LEDGER_FULL,
            command_enqueued=False,
            command_claimed=False,
        )
    with pytest.raises(TypeError):
        FinalityAckReceipt(
            stream_instance_id=stream_id(),
            owner_instance_id=owner_id(),
            ledger_version=1,
            ack_token=ack_payload,
        )
    with pytest.raises(TypeError):
        FinalityPruneReceipt(
            owner_instance_id=prune_payload,
            outcome=FinalityPruneOutcome.PRUNED,
        )
    del proof_payload, ack_payload, prune_payload
    gc.collect()

    assert all(reference() is None for reference in references)


def test_identity_scalar_subclasses_cannot_smuggle_object_references():
    class Payload:
        pass

    class RetainingString(str):
        def __new__(cls, value, payload):
            instance = super().__new__(cls, value)
            instance.payload = payload
            return instance

    owner_scalar = RetainingString("hw-1", Payload())
    stream_scalar = RetainingString("stream-a", Payload())

    with pytest.raises(TypeError, match="OwnerInstanceId"):
        dataclasses.replace(owner_id(), hardware_id=owner_scalar)
    with pytest.raises(TypeError, match="StreamInstanceId"):
        dataclasses.replace(stream_id(), instance_nonce=stream_scalar)


def malformed_ack(**changes):
    values = {
        "stream_instance_id": stream_id(),
        "owner_instance_id": owner_id(),
        "ledger_version": 1,
        "ack_token": "forged",
    }
    values.update(changes)
    receipt = object.__new__(FinalityAckReceipt)
    for field, value in values.items():
        object.__setattr__(receipt, field, value)
    return receipt


@pytest.mark.parametrize(
    "malformed",
    [
        malformed_ack(stream_instance_id=object()),
        malformed_ack(owner_instance_id=object()),
        malformed_ack(ledger_version="1"),
        malformed_ack(ledger_version=object()),
        malformed_ack(ack_token=object()),
        malformed_ack(ack_token=7),
    ],
)
def test_prune_fails_closed_for_malformed_ack_receipt(malformed):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    valid_ack = acknowledge_removed(ledger, instance_id)

    result = ledger._prune_entry(instance_id, malformed)

    assert result.outcome is FinalityPruneOutcome.MISMATCH
    assert ledger.snapshot().acknowledged_awaiting_prune == 1
    assert (
        ledger._prune_entry(instance_id, valid_ack).outcome
        is FinalityPruneOutcome.PRUNED
    )


def test_stateless_prune_rejects_cross_type_scalar_identity_collision():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    exact = owner_id()
    ack = acknowledge_removed(ledger, exact)
    assert (
        ledger._prune_entry(exact, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
    unequal = dataclasses.replace(exact, pool_epoch="7")
    collided_receipt = dataclasses.replace(
        ack, owner_instance_id=unequal
    )

    assert exact != unequal
    assert (
        ledger._prune_entry(unequal, collided_receipt).outcome
        is FinalityPruneOutcome.MISMATCH
    )


def test_reserved_removal_stays_indeterminate_until_enqueue_and_cancellation():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)

    pending = ledger.request_removal(
        instance_id, registration_present=False
    )

    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert ledger.snapshot().pending_late_commands == 1
    ledger.mark_prepare_enqueued(instance_id)
    assert ledger.snapshot().pending_late_commands == 1
    assert (
        ledger.request_removal(
            instance_id, registration_present=False
        ).outcome
        is OwnerRemovalOutcome.INDETERMINATE
    )
    cancelled = ledger.resolve_queued_prepare_cancellation(
        instance_id, cancelled=True
    )
    assert (
        cancelled.outcome
        is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    )


def test_reserved_removal_blocks_ack_and_prune_until_reservation_abort():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    pending = ledger.request_removal(
        instance_id, registration_present=False
    )
    forged_positive = OwnerRemovalReceipt(
        instance_id, OwnerRemovalOutcome.TOMBSTONED_ABSENT
    )

    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert (
        ledger._acknowledge_entry(
            instance_id,
            forged_positive,
            completed_progress(forged_positive),
        )
        is None
    )
    aborted = ledger.abort_prepare_reservation(
        instance_id, command_not_enqueued=True
    )
    assert aborted.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    ack = ledger._acknowledge_entry(
        instance_id, aborted, completed_progress(aborted)
    )
    assert isinstance(ack, FinalityAckReceipt)
    assert (
        ledger._prune_entry(instance_id, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )


def test_reserve_remove_delayed_enqueue_race_keeps_command_unresolved():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    release = threading.Barrier(2)
    errors: list[BaseException] = []

    def delayed_enqueue():
        try:
            release.wait()
            ledger.mark_prepare_enqueued(instance_id)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=delayed_enqueue)
    worker.start()
    pending = ledger.request_removal(
        instance_id, registration_present=False
    )
    release.wait()
    worker.join(1.0)

    assert not worker.is_alive()
    assert errors == []
    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert ledger.snapshot().pending_late_commands == 1
    assert (
        ledger.resolve_queued_prepare_cancellation(
            instance_id, cancelled=True
        ).outcome
        is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    )


def test_delayed_enqueue_after_abort_ack_and_prune_is_inert():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    pending = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert pending.outcome is OwnerRemovalOutcome.INDETERMINATE
    aborted = ledger.abort_prepare_reservation(
        instance_id, command_not_enqueued=True
    )
    ack = ledger._acknowledge_entry(
        instance_id, aborted, completed_progress(aborted)
    )
    assert isinstance(ack, FinalityAckReceipt)
    assert (
        ledger._prune_entry(instance_id, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )

    ledger.mark_prepare_enqueued(instance_id)

    assert ledger.snapshot().entry_count == 0
    assert ledger.snapshot().pending_late_commands == 0


def test_reservation_and_cancellation_handshakes_require_exact_booleans():
    reserved = OwnerFinalityLedger(stream_id(), limit=1)
    reserved_id = owner_id(nonce="reserved")
    admitted(reserved, reserved_id)
    reserved.request_removal(reserved_id, registration_present=False)
    with pytest.raises(TypeError, match="bool"):
        reserved.abort_prepare_reservation(
            reserved_id, command_not_enqueued=1
        )

    queued = OwnerFinalityLedger(stream_id(), limit=1)
    queued_id = owner_id(nonce="queued")
    admitted(queued, queued_id)
    queued.mark_prepare_enqueued(queued_id)
    queued.request_removal(queued_id, registration_present=False)
    with pytest.raises(TypeError, match="bool"):
        queued.resolve_queued_prepare_cancellation(
            queued_id, cancelled=1
        )


@pytest.mark.parametrize(
    "operation",
    [
        lambda ledger, key: ledger.reserve_prepare(key),
        lambda ledger, key: ledger.mark_prepare_enqueued(key),
        lambda ledger, key: ledger.claim_prepare(key),
        lambda ledger, key: ledger.authorize_prepare_publication(key),
        lambda ledger, key: ledger.resolve_queued_prepare_cancellation(
            key, cancelled=True
        ),
        lambda ledger, key: ledger.resolve_prepare(key, activated=False),
        lambda ledger, key: ledger.request_removal(
            key, registration_present=False
        ),
        lambda ledger, key: ledger._acknowledge_entry(
            key,
            OwnerRemovalReceipt(
                owner_id(), OwnerRemovalOutcome.TOMBSTONED_ABSENT
            ),
            completed_progress(
                OwnerRemovalReceipt(
                    owner_id(), OwnerRemovalOutcome.TOMBSTONED_ABSENT
                )
            ),
        ),
        lambda ledger, key: ledger._prune_entry(
            key,
            FinalityAckReceipt(
                stream_instance_id=stream_id(),
                owner_instance_id=owner_id(),
                ledger_version=1,
                ack_token="forged",
            ),
        ),
    ],
)
def test_every_ledger_boundary_rejects_arbitrary_hashable_owner_keys(operation):
    class ArbitraryKey:
        pass

    with pytest.raises(TypeError, match="OwnerInstanceId"):
        operation(OwnerFinalityLedger(stream_id()), ArbitraryKey())


def test_ledger_constructor_requires_exact_stream_instance_id():
    with pytest.raises(TypeError, match="StreamInstanceId"):
        OwnerFinalityLedger(object())


def test_rejected_arbitrary_ledger_key_is_not_retained():
    class ArbitraryKey:
        pass

    ledger = OwnerFinalityLedger(stream_id())
    key = ArbitraryKey()
    reference = weakref.ref(key)
    rejected = False
    try:
        ledger.reserve_prepare(key)
    except TypeError:
        rejected = True
    del key
    gc.collect()

    assert reference() is None
    assert rejected
    assert ledger.snapshot().entry_count == 0


def test_metadata_subclasses_cannot_retain_attached_objects():
    class Payload:
        pass

    class RetainingTuple(tuple):
        pass

    class RetainingString(str):
        def __new__(cls, value, payload):
            instance = super().__new__(cls, value)
            instance.payload = payload
            return instance

    payloads = [Payload(), Payload(), Payload()]
    references = tuple(weakref.ref(payload) for payload in payloads)
    outer = RetainingTuple((("phase", "remove"),))
    outer.payload = payloads[0]
    item = RetainingTuple(("phase", "remove"))
    item.payload = payloads[1]
    key = RetainingString("phase", payloads[2])
    cases = (outer, (item,), ((key, "remove"),))
    retained_receipts = []
    rejected = []
    for metadata in cases:
        try:
            retained_receipts.append(
                OwnerRemovalReceipt(
                    owner_id(),
                    OwnerRemovalOutcome.REMOVED,
                    diagnostic_metadata=metadata,
                )
            )
            rejected.append(False)
        except TypeError:
            rejected.append(True)
    del cases, outer, item, key, payloads, metadata
    gc.collect()

    assert all(reference() is None for reference in references)
    assert rejected == [True, True, True]
    assert retained_receipts == []


def test_valid_metadata_is_stored_as_exact_builtin_values():
    receipt = OwnerRemovalReceipt(
        owner_id(),
        OwnerRemovalOutcome.REMOVED,
        diagnostic_metadata=(("retry_count", 2), ("phase", "remove")),
    )

    assert type(receipt.diagnostic_metadata) is tuple
    assert all(
        type(item) is tuple
        and type(item[0]) is str
        and type(item[1]) in (str, int, float, bool, type(None))
        for item in receipt.diagnostic_metadata
    )


def test_stream_types_exports_only_canonical_owner_finality_values():
    assert "OwnerInstanceId" in stream_types.__all__
    assert "OwnerRemovalReceipt" in stream_types.__all__
    assert "OwnerRegistrationIdentity" not in stream_types.__all__


def test_removal_receipt_rejects_equality_spoof_expected_identity():
    class EqualitySpoof:
        def __eq__(self, _other):
            return True

    class OwnerIdSubclass(OwnerInstanceId):
        __slots__ = ()

        def __eq__(self, _other):
            return True

    exact = owner_id()
    receipt = OwnerRemovalReceipt(
        exact, OwnerRemovalOutcome.REMOVED
    )
    subclass = object.__new__(OwnerIdSubclass)
    for field in dataclasses.fields(exact):
        object.__setattr__(
            subclass, field.name, getattr(exact, field.name)
        )

    assert not receipt.confirms(EqualitySpoof())
    assert not receipt.confirms(subclass)


@pytest.mark.parametrize(
    "malformed", ["present", 1, 0, object()]
)
def test_request_removal_rejects_non_bool_registration_without_mutation(
    malformed,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    claim_and_resolve(ledger, instance_id, activated=True)
    before = ledger.snapshot()
    raised = False
    result = None
    try:
        result = ledger.request_removal(
            instance_id, registration_present=malformed
        )
    except TypeError:
        raised = True
    after = ledger.snapshot()

    assert after == before
    assert raised
    assert result is None


@pytest.mark.parametrize(
    "change",
    [
        {"removal_receipt": object()},
        {"owner_signaled": 1},
        {"owner_joined": 0},
        {"late_owner_commands_resolved": "yes"},
        {"remove_retry_disabled": object()},
    ],
)
def test_durable_progress_rejects_non_exact_receipt_and_flags(change):
    values = {
        "removal_receipt": OwnerRemovalReceipt(
            owner_id(), OwnerRemovalOutcome.REMOVED
        ),
        "owner_signaled": True,
        "owner_joined": True,
        "late_owner_commands_resolved": True,
        "remove_retry_disabled": True,
    }
    values.update(change)

    with pytest.raises(TypeError, match="FinalityDurableProgress"):
        FinalityDurableProgress(**values)


@pytest.mark.parametrize(
    "malformed_field",
    [
        "removal_receipt",
        "owner_signaled",
        "owner_joined",
        "late_owner_commands_resolved",
        "remove_retry_disabled",
    ],
)
def test_forged_durable_progress_cannot_acknowledge_or_enable_prune(
    malformed_field,
):
    class ReceiptEqualitySpoof:
        def __eq__(self, _other):
            return True

        def confirms(self, _expected):
            return True

    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    receipt = removed_active(ledger, instance_id)
    progress = object.__new__(FinalityDurableProgress)
    values = {
        "removal_receipt": receipt,
        "owner_signaled": True,
        "owner_joined": True,
        "late_owner_commands_resolved": True,
        "remove_retry_disabled": True,
    }
    values[malformed_field] = (
        ReceiptEqualitySpoof()
        if malformed_field == "removal_receipt"
        else 1
    )
    for field, value in values.items():
        object.__setattr__(progress, field, value)

    ack = ledger._acknowledge_entry(instance_id, receipt, progress)
    forged_ack = FinalityAckReceipt(
        stream_instance_id=stream_id(),
        owner_instance_id=instance_id,
        ledger_version=1,
        ack_token="forged",
    )
    prune = ledger._prune_entry(instance_id, forged_ack)
    snapshot = ledger.snapshot()

    assert ack is None
    assert prune.outcome is FinalityPruneOutcome.MISMATCH
    assert snapshot.count(OwnerFinalityState.FINAL_REMOVED) == 1
    assert snapshot.acknowledged_awaiting_prune == 0
    assert snapshot.pruned_entry_count == 0


@pytest.mark.parametrize("pending_stage", ["reserved", "queued", "claimed"])
def test_positive_receipt_cannot_ack_or_prune_until_prepare_resolves(
    pending_stage,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id(nonce=pending_stage)
    admission = admitted(ledger, instance_id)
    if pending_stage in {"queued", "claimed"}:
        ledger.mark_prepare_enqueued(instance_id)
    if pending_stage == "claimed":
        ledger.claim_prepare(instance_id)
    positive = ledger.request_removal(
        instance_id, registration_present=True
    )
    progress = completed_progress(positive)
    unrecorded_ack = ledger._authenticator.issue(
        instance_id, admission.ledger_version
    )

    assert positive.outcome is OwnerRemovalOutcome.REMOVED
    assert ledger.snapshot().pending_late_commands == 1
    assert (
        ledger._acknowledge_entry(instance_id, positive, progress)
        is None
    )
    assert (
        ledger._prune_entry(instance_id, unrecorded_ack).outcome
        is FinalityPruneOutcome.MISMATCH
    )
    assert ledger.snapshot().entry_count == 1

    if pending_stage == "reserved":
        ledger.abort_prepare_reservation(
            instance_id, command_not_enqueued=True
        )
    elif pending_stage == "queued":
        ledger.resolve_queued_prepare_cancellation(
            instance_id, cancelled=True
        )
    else:
        ledger.resolve_prepare(instance_id, activated=False)
    ack = ledger._acknowledge_entry(instance_id, positive, progress)
    assert isinstance(ack, FinalityAckReceipt)
    assert (
        ledger._prune_entry(instance_id, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )


def test_claimed_positive_prepare_remains_pending_until_explicit_resolve():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    positive = ledger.request_removal(
        instance_id, registration_present=True
    )

    claim = ledger.claim_prepare(instance_id)

    assert not claim.activation_allowed
    assert ledger.snapshot().pending_late_commands == 1
    assert (
        ledger._acknowledge_entry(
            instance_id, positive, completed_progress(positive)
        )
        is None
    )
    ledger.resolve_prepare(instance_id, activated=False)
    assert isinstance(
        ledger._acknowledge_entry(
            instance_id, positive, completed_progress(positive)
        ),
        FinalityAckReceipt,
    )


def test_ledger_limit_requires_exact_positive_builtin_integer():
    class IntegerSubclass(int):
        pass

    invalid = (
        (True, TypeError),
        (False, TypeError),
        (1.5, TypeError),
        (IntegerSubclass(1), TypeError),
        (0, ValueError),
        (-1, ValueError),
    )
    for limit, expected_error in invalid:
        ledger = None
        with pytest.raises(expected_error, match="limit"):
            ledger = OwnerFinalityLedger(stream_id(), limit=limit)
        assert ledger is None


@pytest.mark.parametrize(
    "malformed", ["active", 1, 0, object()]
)
def test_resolve_prepare_rejects_non_bool_without_state_mutation(malformed):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id()
    admitted(ledger, instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    ledger.claim_prepare(instance_id)
    before = ledger.snapshot()

    with pytest.raises(TypeError, match="activated"):
        ledger.resolve_prepare(instance_id, activated=malformed)

    assert ledger.snapshot() == before
    assert ledger.snapshot().pending_late_commands == 1


def test_publication_gate_has_no_callback_or_external_code_api():
    parameters = inspect.signature(
        OwnerFinalityLedger.authorize_prepare_publication
    ).parameters

    assert tuple(parameters) == ("self", "instance_id")
    assert not hasattr(
        OwnerFinalityLedger, "publish_prepare_if_allowed"
    )


@pytest.mark.parametrize(
    "removal_stage", ["reserved", "queued", "claimed"]
)
def test_positive_removal_survives_full_enqueue_claim_lifecycle(
    removal_stage,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    instance_id = owner_id(nonce=removal_stage)
    admission = admitted(ledger, instance_id)
    if removal_stage != "reserved":
        ledger.mark_prepare_enqueued(instance_id)
    if removal_stage == "claimed":
        ledger.claim_prepare(instance_id)
    positive = ledger.request_removal(
        instance_id, registration_present=True
    )
    if removal_stage == "reserved":
        ledger.mark_prepare_enqueued(instance_id)
    if removal_stage != "claimed":
        ledger.claim_prepare(instance_id)

    assert positive.outcome is OwnerRemovalOutcome.REMOVED
    repeated = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert repeated == positive
    assert repeated is not positive
    assert repeated.owner_instance_id is not positive.owner_instance_id
    assert ledger.snapshot().pending_late_commands == 1
    before_gate = ledger.snapshot()
    authorization = ledger.authorize_prepare_publication(instance_id)
    assert type(authorization) is bool
    assert authorization is False
    assert ledger.snapshot() == before_gate
    assert (
        ledger._acknowledge_entry(
            instance_id, positive, completed_progress(positive)
        )
        is None
    )
    unrecorded_ack = ledger._authenticator.issue(
        instance_id, admission.ledger_version
    )
    assert (
        ledger._prune_entry(instance_id, unrecorded_ack).outcome
        is FinalityPruneOutcome.MISMATCH
    )

    ledger.resolve_prepare(instance_id, activated=False)
    ack = ledger._acknowledge_entry(
        instance_id, positive, completed_progress(positive)
    )
    assert isinstance(ack, FinalityAckReceipt)
    assert (
        ledger._prune_entry(instance_id, ack).outcome
        is FinalityPruneOutcome.PRUNED
    )
