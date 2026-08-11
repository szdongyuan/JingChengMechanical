"""Adversarial exact-class constructor-bypass coverage for owner finality."""

from __future__ import annotations

import ast
import gc
import importlib
import threading
import weakref
from dataclasses import fields
from enum import Enum
from pathlib import Path

import pytest

from app.service.analysis.resident_audio.owner_finality import (
    FinalityDurableProgress,
    OwnerFinalityEntrySnapshot,
    OwnerFinalityLedger,
    OwnerFinalitySnapshot,
    OwnerFinalityState,
    PrepareAdmission,
    PrepareClaim,
)
from app.service.analysis.resident_audio.owner_finality_crypto import (
    OwnerFinalityAuthenticator,
)
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


def stream_id() -> StreamInstanceId:
    return StreamInstanceId(7, "hw-1", 3, "stream-a")


def owner_id() -> OwnerInstanceId:
    return OwnerInstanceId(
        7, "hw-1", 3, "run-9", 11, "owner-cap", "owner-a"
    )


def entry_snapshot() -> OwnerFinalityEntrySnapshot:
    return OwnerFinalityEntrySnapshot(
        7,
        "hw-1",
        3,
        "run-9",
        11,
        "owner-a",
        OwnerFinalityState.PREPARE_RESERVED,
        True,
    )


def state_counts(
    entries: tuple[OwnerFinalityEntrySnapshot, ...],
) -> tuple[tuple[OwnerFinalityState, int], ...]:
    return tuple(
        (state, sum(entry.state is state for entry in entries))
        for state in OwnerFinalityState
    )


def snapshot() -> OwnerFinalitySnapshot:
    entries = (entry_snapshot(),)
    return OwnerFinalitySnapshot(
        stream_id(),
        "hw-1",
        entries,
        state_counts(entries),
        64,
        1,
        0,
        0,
        0,
        0,
    )


def receipt() -> OwnerRemovalReceipt:
    return OwnerRemovalReceipt(owner_id(), OwnerRemovalOutcome.REMOVED)


def canonical_values() -> tuple[object, ...]:
    removal = receipt()
    return (
        stream_id(),
        owner_id(),
        removal,
        OwnerNonAdmissionProof(
            owner_id(),
            OwnerNonAdmissionReason.LEDGER_FULL,
            False,
            False,
        ),
        FinalityAckReceipt(stream_id(), owner_id(), 1, "token"),
        FinalityPruneReceipt(owner_id(), FinalityPruneOutcome.PRUNED),
        FinalityDurableProgress(removal, True, True, True, True),
        PrepareAdmission(owner_id(), 1),
        PrepareClaim(owner_id(), 1, True),
        entry_snapshot(),
        snapshot(),
    )


def forge(
    exact_type: type[object],
    values: dict[str, object],
) -> object:
    forged = object.__new__(exact_type)
    for name, value in values.items():
        object.__setattr__(forged, name, value)
    return forged


def values_of(value: object) -> dict[str, object]:
    return {field.name: getattr(value, field.name) for field in fields(value)}


@pytest.mark.parametrize("value", canonical_values())
def test_every_canonical_value_normalizes_each_missing_slot_to_type_error(
    value: object,
):
    exact_type = type(value)
    valid = values_of(value)
    for missing in valid:
        malformed = forge(
            exact_type,
            {name: item for name, item in valid.items() if name != missing},
        )
        with pytest.raises(TypeError):
            exact_type.__post_init__(malformed)


@pytest.mark.parametrize("value", canonical_values())
def test_every_canonical_value_revalidates_each_present_field(value: object):
    exact_type = type(value)
    valid = values_of(value)
    for changed in valid:
        malformed = forge(exact_type, {**valid, changed: object()})
        with pytest.raises((TypeError, ValueError)):
            exact_type.__post_init__(malformed)


@pytest.mark.parametrize(
    ("outer", "nested_field", "nested"),
    [
        (receipt(), "owner_instance_id", owner_id()),
        (
            OwnerNonAdmissionProof(
                owner_id(),
                OwnerNonAdmissionReason.LEDGER_FULL,
                False,
                False,
            ),
            "owner_instance_id",
            owner_id(),
        ),
        (
            FinalityAckReceipt(stream_id(), owner_id(), 1, "token"),
            "stream_instance_id",
            stream_id(),
        ),
        (
            FinalityAckReceipt(stream_id(), owner_id(), 1, "token"),
            "owner_instance_id",
            owner_id(),
        ),
        (
            FinalityPruneReceipt(
                owner_id(), FinalityPruneOutcome.PRUNED
            ),
            "owner_instance_id",
            owner_id(),
        ),
        (
            FinalityDurableProgress(receipt(), True, True, True, True),
            "removal_receipt",
            receipt(),
        ),
        (PrepareAdmission(owner_id(), 1), "owner_instance_id", owner_id()),
        (PrepareClaim(owner_id(), 1, True), "owner_instance_id", owner_id()),
        (snapshot(), "stream_instance_id", stream_id()),
        (snapshot(), "entries", entry_snapshot()),
    ],
)
def test_outer_values_recursively_reject_forged_exact_nested_values(
    outer: object, nested_field: str, nested: object
):
    nested_type = type(nested)
    nested_values = values_of(nested)
    nested_values.pop(next(iter(nested_values)))
    malformed_nested = forge(nested_type, nested_values)
    outer_values = values_of(outer)
    outer_values[nested_field] = (
        (malformed_nested,) if nested_field == "entries" else malformed_nested
    )
    malformed_outer = forge(type(outer), outer_values)

    with pytest.raises(TypeError):
        type(outer).__post_init__(malformed_outer)


class Probe:
    def __init__(self) -> None:
        self.eq_calls = 0
        self.hash_calls = 0

    def __eq__(self, other: object) -> bool:
        self.eq_calls += 1
        raise AssertionError("malicious equality executed")

    def __hash__(self) -> int:
        self.hash_calls += 1
        raise AssertionError("malicious hash executed")


def forged_owner(**changes: object) -> OwnerInstanceId:
    values = values_of(owner_id())
    values.update(changes)
    return forge(OwnerInstanceId, values)  # type: ignore[return-value]


def test_ledger_rejects_forged_key_before_equality_hash_or_mutation():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    before = ledger.snapshot()
    equality_probe = Probe()
    hashing_probe = Probe()

    with pytest.raises(TypeError):
        ledger.reserve_prepare(forged_owner(pool_epoch=equality_probe))
    with pytest.raises(TypeError):
        ledger.reserve_prepare(forged_owner(run_id=hashing_probe))

    assert equality_probe.eq_calls == equality_probe.hash_calls == 0
    assert hashing_probe.eq_calls == hashing_probe.hash_calls == 0
    assert ledger.snapshot() == before
    assert ledger._entries == {}


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
        lambda ledger, key: ledger.abort_prepare_reservation(
            key, command_not_enqueued=True
        ),
        lambda ledger, key: ledger.resolve_prepare(key, activated=False),
        lambda ledger, key: ledger.request_removal(
            key, registration_present=False
        ),
        lambda ledger, key: ledger._acknowledge_entry(
            key,
            receipt(),
            FinalityDurableProgress(receipt(), True, True, True, True),
        ),
        lambda ledger, key: ledger._prune_entry(
            key,
            FinalityAckReceipt(stream_id(), owner_id(), 1, "token"),
        ),
        lambda ledger, key: ledger._require_entry(key),
        lambda ledger, key: ledger._matches_stream(key),
        lambda ledger, key: ledger._require_stream_match(key),
    ],
)
def test_every_ledger_identity_boundary_rejects_before_hash_or_equality(
    operation,
):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    before = ledger.snapshot()
    probe = Probe()

    with pytest.raises(TypeError):
        operation(ledger, forged_owner(run_id=probe))

    assert probe.eq_calls == probe.hash_calls == 0
    assert ledger.snapshot() == before
    assert ledger._entries == {}


def test_receipt_confirmation_rejects_forged_values_without_equality():
    probe = Probe()
    exact = receipt()
    forged_expected = forged_owner(run_id=probe)
    malformed_receipt = forge(
        OwnerRemovalReceipt,
        {
            "owner_instance_id": forged_owner(run_id=probe),
            "outcome": OwnerRemovalOutcome.REMOVED,
            "diagnostic_metadata": (),
        },
    )

    assert exact.confirms(forged_expected) is False
    assert malformed_receipt.confirms(owner_id()) is False
    assert probe.eq_calls == probe.hash_calls == 0


def test_authenticator_validates_every_value_before_comparison_or_hmac():
    constructor_probe = Probe()
    with pytest.raises(TypeError):
        OwnerFinalityAuthenticator(
            forge(
                StreamInstanceId,
                {
                    **values_of(stream_id()),
                    "pool_epoch": constructor_probe,
                },
            )
        )

    authenticator = OwnerFinalityAuthenticator(stream_id())
    owner_probe = Probe()
    malformed_owner = forged_owner(run_id=owner_probe)
    with pytest.raises(TypeError):
        authenticator.issue(malformed_owner, 1)
    malformed_ack = forge(
        FinalityAckReceipt,
        {
            "stream_instance_id": stream_id(),
            "owner_instance_id": malformed_owner,
            "ledger_version": 1,
            "ack_token": "token",
        },
    )
    assert authenticator.validates(owner_id(), malformed_ack, 1) is False
    assert constructor_probe.eq_calls == constructor_probe.hash_calls == 0
    assert owner_probe.eq_calls == owner_probe.hash_calls == 0


def test_snapshot_rejects_forged_uncounted_state_before_aggregation():
    forged_entry = forge(
        OwnerFinalityEntrySnapshot,
        {
            **values_of(entry_snapshot()),
            "state": "ACTIVE",
            "prepare_pending": False,
        },
    )
    values = values_of(snapshot())
    values.update(
        entries=(forged_entry,),
        state_counts=tuple((state, 0) for state in OwnerFinalityState),
        pending_late_commands=0,
    )

    with pytest.raises(TypeError):
        OwnerFinalitySnapshot(**values)


def test_ledger_ack_and_prune_fail_closed_without_state_poisoning():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    ledger.reserve_prepare(identity)
    ledger.mark_prepare_enqueued(identity)
    ledger.claim_prepare(identity)
    ledger.resolve_prepare(identity, activated=True)
    positive = ledger.request_removal(identity, registration_present=True)
    before = ledger.snapshot()
    forged_progress = forge(FinalityDurableProgress, {})
    forged_ack = forge(FinalityAckReceipt, {})

    assert (
        ledger._acknowledge_entry(identity, positive, forged_progress) is None
    )
    prune = ledger._prune_entry(identity, forged_ack)

    assert prune.outcome is FinalityPruneOutcome.MISMATCH
    assert ledger.snapshot() == before


def test_rejected_payloads_are_not_retained_by_any_boundary():
    class Payload:
        pass

    def reject() -> tuple[weakref.ReferenceType[Payload], ...]:
        ledger_payload = Payload()
        auth_payload = Payload()
        snapshot_payload = Payload()
        references = tuple(
            weakref.ref(value)
            for value in (ledger_payload, auth_payload, snapshot_payload)
        )
        ledger = OwnerFinalityLedger(stream_id())
        try:
            ledger.reserve_prepare(forged_owner(run_id=ledger_payload))
        except TypeError:
            pass
        try:
            OwnerFinalityAuthenticator(
                forge(
                    StreamInstanceId,
                    {
                        **values_of(stream_id()),
                        "instance_nonce": auth_payload,
                    },
                )
            )
        except TypeError:
            pass
        forged_entry = forge(
            OwnerFinalityEntrySnapshot,
            {
                **values_of(entry_snapshot()),
                "run_id": snapshot_payload,
            },
        )
        values = values_of(snapshot())
        values["entries"] = (forged_entry,)
        try:
            OwnerFinalitySnapshot(**values)
        except (TypeError, ValueError):
            pass
        return references

    references = reject()
    gc.collect()
    gc.collect()

    assert all(reference() is None for reference in references)


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (owner_id(), "pool_epoch"),
        (owner_id(), "hardware_id"),
        (owner_id(), "run_id"),
        (owner_id(), "commit_attempt_id"),
        (owner_id(), "owner_capability"),
        (owner_id(), "instance_nonce"),
        (stream_id(), "pool_epoch"),
        (stream_id(), "hardware_id"),
        (stream_id(), "instance_nonce"),
        (entry_snapshot(), "pool_epoch"),
        (entry_snapshot(), "hardware_id"),
        (entry_snapshot(), "run_id"),
        (entry_snapshot(), "commit_attempt_id"),
        (entry_snapshot(), "instance_nonce"),
        (snapshot(), "hardware_id"),
    ],
)
@pytest.mark.parametrize("malformed", ["", "x" * 257])
def test_identity_text_bounds_preserve_prior_type_error_contract(
    value: object, field: str, malformed: str
):
    values = values_of(value)
    values[field] = malformed

    with pytest.raises(TypeError):
        type(value)(**values)


def forged_enum_member(enum_type: type[Enum]) -> Enum:
    forged = str.__new__(enum_type, "FORGED")
    assert type(forged) is enum_type
    return forged


@pytest.mark.parametrize(
    ("value", "field", "enum_type"),
    [
        (receipt(), "outcome", OwnerRemovalOutcome),
        (
            OwnerNonAdmissionProof(
                owner_id(),
                OwnerNonAdmissionReason.LEDGER_FULL,
                False,
                False,
            ),
            "reason",
            OwnerNonAdmissionReason,
        ),
        (
            FinalityPruneReceipt(
                owner_id(), FinalityPruneOutcome.PRUNED
            ),
            "outcome",
            FinalityPruneOutcome,
        ),
        (entry_snapshot(), "state", OwnerFinalityState),
    ],
)
def test_exact_enum_non_members_are_rejected_without_enum_internal_leaks(
    value: object, field: str, enum_type: type[Enum]
):
    values = values_of(value)
    values[field] = forged_enum_member(enum_type)

    with pytest.raises(TypeError):
        type(value)(**values)


def test_snapshot_rejects_forged_enum_state_before_aggregation():
    forged_entry = forge(
        OwnerFinalityEntrySnapshot,
        {
            **values_of(entry_snapshot()),
            "state": forged_enum_member(OwnerFinalityState),
            "prepare_pending": False,
        },
    )
    values = values_of(snapshot())
    values.update(
        entries=(forged_entry,),
        state_counts=tuple((state, 0) for state in OwnerFinalityState),
        pending_late_commands=0,
    )

    with pytest.raises(TypeError):
        OwnerFinalitySnapshot(**values)


def test_snapshot_count_rejects_exact_enum_non_member_before_hashing():
    forged = forged_enum_member(OwnerFinalityState)

    with pytest.raises(TypeError):
        snapshot().count(forged)


class TraversalSentinel:
    def __init__(self, counters: dict[str, int]) -> None:
        object.__setattr__(self, "_counters", counters)

    def __getattribute__(self, name: str) -> object:
        if name != "_counters":
            counters = object.__getattribute__(self, "_counters")
            counters["attribute"] += 1
            raise AssertionError("oversized entry was traversed")
        return object.__getattribute__(self, name)

    def __eq__(self, other: object) -> bool:
        counters = object.__getattribute__(self, "_counters")
        counters["equality"] += 1
        raise AssertionError("oversized entry equality executed")

    def __hash__(self) -> int:
        counters = object.__getattribute__(self, "_counters")
        counters["hash"] += 1
        raise AssertionError("oversized entry hash executed")


def test_snapshot_preflights_entry_bound_before_visiting_items_and_retains_none():
    counters = {"attribute": 0, "equality": 0, "hash": 0}

    def reject() -> weakref.ReferenceType[TraversalSentinel]:
        sentinel = TraversalSentinel(counters)
        reference = weakref.ref(sentinel)
        values = values_of(snapshot())
        values.update(entries=(sentinel, sentinel), ledger_limit=1)
        with pytest.raises(ValueError, match="entry count exceeds"):
            OwnerFinalitySnapshot(**values)
        return reference

    reference = reject()
    gc.collect()

    assert counters == {"attribute": 0, "equality": 0, "hash": 0}
    assert reference() is None


@pytest.mark.parametrize("cardinality", [7, 9])
def test_snapshot_preflights_state_count_cardinality_before_hashing(
    monkeypatch: pytest.MonkeyPatch, cardinality: int
):
    counters = {"equality": 0, "hash": 0}

    def raising_hash(self: OwnerFinalityState) -> int:
        counters["hash"] += 1
        raise AssertionError("state count was hashed before preflight")

    def raising_equality(
        self: OwnerFinalityState, other: object
    ) -> bool:
        counters["equality"] += 1
        raise AssertionError("state count equality ran before preflight")

    values = values_of(snapshot())
    counts = list(state_counts((entry_snapshot(),)))
    reference = None
    if cardinality < len(counts):
        counts = counts[:cardinality]
    else:
        sentinel = TraversalSentinel(counters)
        reference = weakref.ref(sentinel)
        counts.append((sentinel, 0))
    values["state_counts"] = tuple(counts)
    monkeypatch.setattr(OwnerFinalityState, "__hash__", raising_hash)
    monkeypatch.setattr(OwnerFinalityState, "__eq__", raising_equality)

    with pytest.raises(ValueError, match="state_counts cardinality"):
        OwnerFinalitySnapshot(**values)

    assert counters == {"equality": 0, "hash": 0}
    if reference is not None:
        del sentinel, counts, values
        gc.collect()
        assert reference() is None


def module_tree(module_name: str) -> ast.Module:
    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "app"
        / "service"
        / "analysis"
        / "resident_audio"
        / f"{module_name}.py"
    )
    return ast.parse(path.read_text(encoding="utf-8"))


def reference_tokens(tree: ast.AST) -> set[str]:
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            tokens.add(node.module or "")
            tokens.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            tokens.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            tokens.add(node.value)
    return tokens


def test_snapshot_validation_import_graph_is_strictly_one_way():
    core = module_tree("owner_finality_validation")
    snapshot_validator = module_tree(
        "owner_finality_snapshot_validation"
    )
    core_tokens = reference_tokens(core)
    snapshot_imports = {
        node.module
        for node in ast.walk(snapshot_validator)
        if isinstance(node, ast.ImportFrom)
    }

    assert "owner_finality_snapshot_validation" not in core_tokens
    assert "validate_owner_finality_snapshot" not in core_tokens
    assert "owner_finality_validation" in snapshot_imports


def test_snapshot_value_boundaries_import_snapshot_validator_directly():
    tree = module_tree("owner_finality_types")
    snapshot_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "OwnerFinalitySnapshot"
    )
    boundaries = {
        node.name: node
        for node in snapshot_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"__post_init__", "entry_count", "count"}
    }

    assert set(boundaries) == {"__post_init__", "entry_count", "count"}
    for boundary in boundaries.values():
        direct_imports = {
            node.module
            for node in ast.walk(boundary)
            if isinstance(node, ast.ImportFrom)
        }
        assert "owner_finality_snapshot_validation" in direct_imports
        assert "owner_finality_validation" not in direct_imports


def test_snapshot_validation_modules_import_and_enforce_preflight_at_runtime():
    core = importlib.import_module(
        "app.service.analysis.resident_audio.owner_finality_validation"
    )
    snapshot_validator = importlib.import_module(
        "app.service.analysis.resident_audio."
        "owner_finality_snapshot_validation"
    )
    valid = snapshot()
    values = values_of(valid)
    values.update(entries=(object(), object()), ledger_limit=1)

    assert not hasattr(core, "validate_owner_finality_snapshot")
    snapshot_validator.validate_owner_finality_snapshot(valid)
    with pytest.raises(ValueError, match="entry count exceeds"):
        OwnerFinalitySnapshot(**values)


STREAM_FIELDS = tuple(field.name for field in fields(StreamInstanceId))
OWNER_FIELDS = tuple(field.name for field in fields(OwnerInstanceId))


def activate(ledger: OwnerFinalityLedger, identity: OwnerInstanceId) -> None:
    ledger.reserve_prepare(identity)
    ledger.mark_prepare_enqueued(identity)
    ledger.claim_prepare(identity)
    ledger.resolve_prepare(identity, activated=True)


def completed_progress(
    removal: OwnerRemovalReceipt,
) -> FinalityDurableProgress:
    return FinalityDurableProgress(removal, True, True, True, True)


@pytest.mark.parametrize("field", STREAM_FIELDS)
def test_ledger_constructor_detaches_every_stream_identity_field(field: str):
    original = stream_id()
    ledger = OwnerFinalityLedger(original, limit=1)
    probe = Probe()

    object.__setattr__(original, field, probe)
    admission = ledger.reserve_prepare(owner_id())
    current = ledger.snapshot()

    assert isinstance(admission, PrepareAdmission)
    assert ledger._stream_instance_id is not original
    assert current.stream_instance_id is not ledger._stream_instance_id
    assert current.stream_instance_id == stream_id()
    assert probe.eq_calls == probe.hash_calls == 0


@pytest.mark.parametrize("field", OWNER_FIELDS)
def test_reserve_detaches_every_owner_key_field_before_storage(field: str):
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    original = owner_id()
    admission = ledger.reserve_prepare(original)
    internal = next(iter(ledger._entries))
    probe = Probe()

    object.__setattr__(original, field, probe)
    ledger.mark_prepare_enqueued(owner_id())
    claim = ledger.claim_prepare(owner_id())

    assert internal is not original
    assert admission.owner_instance_id is not original
    assert admission.owner_instance_id is not internal
    assert claim.owner_instance_id is not internal
    assert claim.owner_instance_id is not admission.owner_instance_id
    assert ledger.snapshot().pending_late_commands == 1
    assert probe.eq_calls == probe.hash_calls == 0


def test_admission_claim_and_nonadmission_outputs_are_deeply_detached():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    admission = ledger.reserve_prepare(identity)
    internal = next(iter(ledger._entries))
    ledger.mark_prepare_enqueued(owner_id())
    claim_input = owner_id()
    claim = ledger.claim_prepare(claim_input)
    blocked_input = OwnerInstanceId(
        7, "hw-1", 3, "run-10", 12, "owner-cap-2", "owner-b"
    )
    proof = ledger.reserve_prepare(blocked_input)

    assert isinstance(admission, PrepareAdmission)
    assert isinstance(claim, PrepareClaim)
    assert isinstance(proof, OwnerNonAdmissionProof)
    assert admission.owner_instance_id is not internal
    assert claim.owner_instance_id is not internal
    assert claim.owner_instance_id is not claim_input
    assert proof.owner_instance_id is not blocked_input
    assert len(
        {
            id(internal),
            id(admission.owner_instance_id),
            id(claim.owner_instance_id),
            id(proof.owner_instance_id),
        }
    ) == 4
    probe = Probe()
    object.__setattr__(admission.owner_instance_id, "run_id", probe)
    object.__setattr__(claim.owner_instance_id, "run_id", probe)
    object.__setattr__(proof.owner_instance_id, "run_id", probe)
    ledger.resolve_prepare(owner_id(), activated=True)
    assert ledger.snapshot().count(OwnerFinalityState.ACTIVE) == 1
    assert probe.eq_calls == probe.hash_calls == 0


def test_returned_removal_receipts_never_alias_retained_receipt_or_key():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    activate(ledger, identity)
    first = ledger.request_removal(
        owner_id(), registration_present=True
    )
    internal_key = next(iter(ledger._entries))
    internal_receipt = ledger._entries[internal_key].final_receipt
    probe = Probe()

    assert internal_receipt is not None
    assert first is not internal_receipt
    assert first.owner_instance_id is not internal_key
    assert first.owner_instance_id is not internal_receipt.owner_instance_id
    object.__setattr__(first.owner_instance_id, "run_id", probe)
    second = ledger.request_removal(
        owner_id(), registration_present=False
    )

    assert second.outcome is OwnerRemovalOutcome.REMOVED
    assert second is not first
    assert second is not internal_receipt
    assert second.owner_instance_id == owner_id()
    assert second.owner_instance_id is not internal_receipt.owner_instance_id
    assert probe.eq_calls == probe.hash_calls == 0


def test_returned_ack_receipts_never_alias_retained_ack_or_authenticator():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    activate(ledger, identity)
    removal = ledger.request_removal(
        owner_id(), registration_present=True
    )
    first = ledger._acknowledge_entry(
        owner_id(), removal, completed_progress(removal)
    )
    internal_key = next(iter(ledger._entries))
    internal_ack = ledger._entries[internal_key].ack_receipt
    probe = Probe()

    assert isinstance(first, FinalityAckReceipt)
    assert internal_ack is not None
    assert first is not internal_ack
    assert first.stream_instance_id is not ledger._stream_instance_id
    assert first.owner_instance_id is not internal_key
    object.__setattr__(first.stream_instance_id, "pool_epoch", probe)
    object.__setattr__(first.owner_instance_id, "run_id", probe)
    second = ledger._acknowledge_entry(
        owner_id(), removal, completed_progress(removal)
    )

    assert isinstance(second, FinalityAckReceipt)
    assert second is not first
    assert second is not internal_ack
    assert second.stream_instance_id == stream_id()
    assert second.owner_instance_id == owner_id()
    assert second.stream_instance_id is not internal_ack.stream_instance_id
    assert second.owner_instance_id is not internal_ack.owner_instance_id
    assert (
        ledger._prune_entry(owner_id(), second).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert probe.eq_calls == probe.hash_calls == 0


def test_prune_and_snapshot_outputs_are_detached_on_every_call():
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    identity = owner_id()
    activate(ledger, identity)
    removal = ledger.request_removal(
        owner_id(), registration_present=True
    )
    ack = ledger._acknowledge_entry(
        owner_id(), removal, completed_progress(removal)
    )
    before = ledger.snapshot()

    assert isinstance(ack, FinalityAckReceipt)
    object.__setattr__(before.stream_instance_id, "hardware_id", "poison")
    after = ledger.snapshot()
    pruned = ledger._prune_entry(owner_id(), ack)
    repeated = ledger._prune_entry(owner_id(), ack)

    assert before.stream_instance_id is not ledger._stream_instance_id
    assert after.stream_instance_id is not before.stream_instance_id
    assert after.stream_instance_id == stream_id()
    assert pruned.owner_instance_id is not identity
    assert repeated.owner_instance_id is not pruned.owner_instance_id
    assert pruned.outcome is FinalityPruneOutcome.PRUNED
    assert repeated.outcome is FinalityPruneOutcome.ALREADY_PRUNED
    probe = Probe()
    object.__setattr__(pruned.owner_instance_id, "run_id", probe)
    third = ledger._prune_entry(owner_id(), ack)
    assert third.outcome is FinalityPruneOutcome.ALREADY_PRUNED
    assert third.owner_instance_id == owner_id()
    assert probe.eq_calls == probe.hash_calls == 0


@pytest.mark.parametrize("field", STREAM_FIELDS)
def test_authenticator_detaches_stream_and_issued_receipts(field: str):
    original_stream = stream_id()
    authenticator = OwnerFinalityAuthenticator(original_stream)
    original_owner = owner_id()
    first = authenticator.issue(original_owner, 1)
    probe = Probe()

    object.__setattr__(original_stream, field, probe)
    object.__setattr__(first.stream_instance_id, field, probe)
    object.__setattr__(first.owner_instance_id, "run_id", probe)
    second = authenticator.issue(owner_id(), 1)

    assert authenticator._stream_instance_id is not original_stream
    assert first.stream_instance_id is not authenticator._stream_instance_id
    assert first.owner_instance_id is not original_owner
    assert second.stream_instance_id is not first.stream_instance_id
    assert second.owner_instance_id is not first.owner_instance_id
    assert authenticator.validates(owner_id(), second, 1) is True
    assert probe.eq_calls == probe.hash_calls == 0


def test_poisoned_caller_alias_payloads_are_not_retained():
    class Payload:
        pass

    def exercise() -> tuple[
        weakref.ReferenceType[Payload], weakref.ReferenceType[Payload]
    ]:
        stream_payload = Payload()
        owner_payload = Payload()
        references = (
            weakref.ref(stream_payload),
            weakref.ref(owner_payload),
        )
        original_stream = stream_id()
        original_owner = owner_id()
        ledger = OwnerFinalityLedger(original_stream, limit=1)
        ledger.reserve_prepare(original_owner)
        object.__setattr__(
            original_stream, "instance_nonce", stream_payload
        )
        object.__setattr__(original_owner, "run_id", owner_payload)
        ledger.mark_prepare_enqueued(owner_id())
        ledger.claim_prepare(owner_id())
        assert ledger.snapshot().stream_instance_id == stream_id()
        return references

    references = exercise()
    gc.collect()
    gc.collect()

    assert all(reference() is None for reference in references)


OWNER_CARRIERS = (
    "removal",
    "nonadmission",
    "ack",
    "prune",
    "progress",
    "admission",
    "claim",
)


def owner_carrier(
    kind: str, source: OwnerInstanceId
) -> tuple[object, OwnerInstanceId]:
    if kind == "removal":
        value = OwnerRemovalReceipt(source, OwnerRemovalOutcome.REMOVED)
        return value, value.owner_instance_id
    if kind == "nonadmission":
        value = OwnerNonAdmissionProof(
            source,
            OwnerNonAdmissionReason.LEDGER_FULL,
            False,
            False,
        )
        return value, value.owner_instance_id
    if kind == "ack":
        value = FinalityAckReceipt(stream_id(), source, 1, "token")
        return value, value.owner_instance_id
    if kind == "prune":
        value = FinalityPruneReceipt(
            source, FinalityPruneOutcome.PRUNED
        )
        return value, value.owner_instance_id
    if kind == "progress":
        receipt_value = OwnerRemovalReceipt(
            source, OwnerRemovalOutcome.REMOVED
        )
        value = FinalityDurableProgress(
            receipt_value, True, True, True, True
        )
        assert value.removal_receipt is not None
        assert value.removal_receipt is not receipt_value
        return value, value.removal_receipt.owner_instance_id
    if kind == "admission":
        value = PrepareAdmission(source, 1)
        return value, value.owner_instance_id
    if kind == "claim":
        value = PrepareClaim(source, 1, True)
        return value, value.owner_instance_id
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", OWNER_CARRIERS)
@pytest.mark.parametrize("field", OWNER_FIELDS)
def test_every_owner_value_carrier_detaches_every_identity_field(
    kind: str, field: str
):
    source = owner_id()
    value, retained = owner_carrier(kind, source)
    probe = Probe()

    object.__setattr__(source, field, probe)

    assert retained is not source
    assert retained == owner_id()
    assert value is not None
    assert probe.eq_calls == probe.hash_calls == 0


@pytest.mark.parametrize("kind", ["ack", "snapshot"])
@pytest.mark.parametrize("field", STREAM_FIELDS)
def test_every_stream_value_carrier_detaches_every_identity_field(
    kind: str, field: str
):
    source = stream_id()
    if kind == "ack":
        value = FinalityAckReceipt(source, owner_id(), 1, "token")
        retained = value.stream_instance_id
    else:
        values = values_of(snapshot())
        values["stream_instance_id"] = source
        value = OwnerFinalitySnapshot(**values)
        retained = value.stream_instance_id
    probe = Probe()

    object.__setattr__(source, field, probe)

    assert retained is not source
    assert retained == stream_id()
    assert probe.eq_calls == probe.hash_calls == 0


@pytest.mark.parametrize(
    "field",
    tuple(field.name for field in fields(OwnerFinalityEntrySnapshot)),
)
def test_snapshot_detaches_every_nested_entry_field(field: str):
    source = entry_snapshot()
    values = values_of(snapshot())
    values["entries"] = (source,)
    value = OwnerFinalitySnapshot(**values)
    probe = Probe()

    object.__setattr__(source, field, probe)

    assert value.entries[0] is not source
    assert value.entries[0] == entry_snapshot()
    assert probe.eq_calls == probe.hash_calls == 0


def test_reserve_uses_captured_owner_copy_if_caller_mutates_concurrently(
    monkeypatch: pytest.MonkeyPatch,
):
    canonical_values = importlib.import_module(
        "app.service.analysis.resident_audio.owner_finality_canonical"
    )
    original_copy = canonical_values.canonical_owner_instance_id
    caller = owner_id()
    captured = threading.Event()
    release = threading.Event()
    result: list[object] = []

    def controlled_copy(value: object) -> OwnerInstanceId:
        copied = original_copy(value)
        if value is caller:
            captured.set()
            assert release.wait(timeout=5)
        return copied

    monkeypatch.setattr(
        canonical_values,
        "canonical_owner_instance_id",
        controlled_copy,
    )
    ledger = OwnerFinalityLedger(stream_id(), limit=1)
    worker = threading.Thread(
        target=lambda: result.append(ledger.reserve_prepare(caller))
    )
    worker.start()
    assert captured.wait(timeout=5)
    probe = Probe()
    object.__setattr__(caller, "run_id", probe)
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert isinstance(result[0], PrepareAdmission)
    assert result[0].owner_instance_id == owner_id()
    assert next(iter(ledger._entries)) == owner_id()
    assert probe.eq_calls == probe.hash_calls == 0
