"""Adversarial constructor-bypass tests for shutdown proof boundaries."""

from __future__ import annotations

from dataclasses import fields

import pytest

from app.service.analysis.resident_audio.shutdown_registry import (
    ShutdownObligationRegistry,
)
from app.service.analysis.resident_audio.shutdown_types import (
    LogicalBarrierCandidate,
    PhysicalCloseCandidate,
    PhysicalCloseOutcome,
    ProducerCandidate,
    ShutdownAcceptedOutcome,
    ShutdownAttemptResult,
    ShutdownAttemptStatus,
    ShutdownAttemptId,
    ShutdownCreationSite,
    ShutdownEpochId,
    ShutdownEvidenceItem,
    ShutdownObligationIdentity,
    ShutdownObligationReceipt,
    ShutdownPhase,
    ShutdownCoordinatorSnapshot,
    ShutdownIdentifierKind,
    ShutdownLifetimeState,
    ShutdownRedactedIdentifier,
    ShutdownResourceKind,
    ShutdownWorkId,
    VerificationCandidate,
)


def forge(exact_type: type[object], values: dict[str, object]) -> object:
    value = object.__new__(exact_type)
    for name, item in values.items():
        object.__setattr__(value, name, item)
    return value


def values_of(value: object) -> dict[str, object]:
    return {field.name: getattr(value, field.name) for field in fields(value)}


def identity() -> ShutdownObligationIdentity:
    return ShutdownObligationIdentity(
        ShutdownEpochId("epoch-a"),
        ShutdownWorkId("work-a"),
        ShutdownResourceKind.PHYSICAL_HANDLE,
        ShutdownCreationSite.BACKEND_OPEN_INPUT,
    )


def candidate() -> PhysicalCloseCandidate:
    return PhysicalCloseCandidate(
        identity(),
        ShutdownAttemptId("attempt-a"),
        PhysicalCloseOutcome.CLOSED,
    )


def receipt() -> ShutdownObligationReceipt:
    return ShutdownObligationReceipt(
        identity(),
        ShutdownAttemptId("attempt-a"),
        ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        (),
        "issuer",
    )


def registered_physical_handle(registry: ShutdownObligationRegistry):
    attempt = registry.register(
        ShutdownResourceKind.ATTEMPT_CONTROL,
        ShutdownCreationSite.ATTEMPT_ENTRY,
        VerificationCandidate,
    )
    resolver = registry.register_child(
        attempt,
        ShutdownResourceKind.RESOLVER_PRODUCER,
        ShutdownCreationSite.RESOLVER_EXECUTION,
        ProducerCandidate,
    )
    opened = registry.register_child(
        resolver,
        ShutdownResourceKind.OPEN_PRODUCER,
        ShutdownCreationSite.OPEN_ATTEMPT,
        ProducerCandidate,
    )
    stream = registry.register_child(
        opened,
        ShutdownResourceKind.STREAM_GENERATION,
        ShutdownCreationSite.STREAM_FACTORY,
        LogicalBarrierCandidate,
    )
    return registry.register_child(
        stream,
        ShutdownResourceKind.PHYSICAL_HANDLE,
        ShutdownCreationSite.BACKEND_OPEN_INPUT,
        PhysicalCloseCandidate,
    )


def registered_close_work(registry: ShutdownObligationRegistry):
    parent = registered_physical_handle(registry)
    registry.bind(parent, object())
    registry.activate(parent)
    return registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )


@pytest.mark.parametrize(
    "value",
    [
        ShutdownEpochId("epoch-a"),
        ShutdownAttemptId("attempt-a"),
        ShutdownWorkId("work-a"),
        identity(),
        ShutdownEvidenceItem("worker_alive", False),
        candidate(),
        receipt(),
    ],
)
def test_every_contract_revalidates_each_missing_slot(value: object):
    exact_type = type(value)
    valid = values_of(value)
    for missing in valid:
        malformed = forge(
            exact_type,
            {name: item for name, item in valid.items() if name != missing},
        )
        with pytest.raises(TypeError):
            exact_type.__post_init__(malformed)


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


def test_registry_rejects_forged_identity_before_hash_or_equality():
    issuer = object()
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=issuer
    )
    exact = registered_physical_handle(registry)
    probe = Probe()
    malformed_work = forge(ShutdownWorkId, {"value": probe})
    malformed = forge(
        ShutdownObligationIdentity,
        {**values_of(exact), "shutdown_work_id": malformed_work},
    )

    with pytest.raises(TypeError):
        registry.bind(malformed, object())

    assert probe.eq_calls == probe.hash_calls == 0
    assert registry.state(exact).name == "REGISTERED"


def test_registry_rejects_missing_nested_slots_without_mutation():
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=object()
    )
    exact = registered_physical_handle(registry)
    malformed_epoch = forge(ShutdownEpochId, {})
    malformed = forge(
        ShutdownObligationIdentity,
        {**values_of(exact), "shutdown_epoch_id": malformed_epoch},
    )
    before = registry.snapshot()

    with pytest.raises(TypeError):
        registry.activate(malformed)

    assert registry.snapshot() == before


def test_candidate_subclass_and_forged_nested_candidate_are_not_proof():
    class CandidateSubclass(PhysicalCloseCandidate):
        pass

    issuer = object()
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=issuer
    )
    exact = registered_close_work(registry)
    registry.bind(exact, object())
    registry.activate(exact)
    exact_candidate = PhysicalCloseCandidate(
        exact,
        ShutdownAttemptId("attempt-a"),
        PhysicalCloseOutcome.CLOSED,
    )
    subclass = CandidateSubclass(
        exact,
        ShutdownAttemptId("attempt-a"),
        PhysicalCloseOutcome.CLOSED,
    )
    malformed = forge(
        PhysicalCloseCandidate,
        {**values_of(exact_candidate), "identity": forge(
            ShutdownObligationIdentity, {}
        )},
    )

    with pytest.raises(TypeError):
        registry.accept_candidate(
            exact,
            subclass,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )
    with pytest.raises(TypeError):
        registry.accept_candidate(
            exact,
            malformed,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.state(exact).name == "ACTIVE"


def test_forged_issuer_proof_and_copied_proof_do_not_validate():
    issuer = object()
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=issuer
    )
    exact = registered_close_work(registry)
    registry.bind(exact, object())
    registry.activate(exact)
    accepted = registry.accept_candidate(
        exact,
        PhysicalCloseCandidate(
            exact,
            ShutdownAttemptId("attempt-a"),
            PhysicalCloseOutcome.CLOSED,
        ),
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    forged_proof = ShutdownObligationReceipt(
        accepted.identity,
        accepted.attempt_id,
        accepted.phase,
        accepted.accepted_outcome,
        accepted.evidence,
        "forged",
    )
    copied_proof = ShutdownObligationReceipt(
        accepted.identity,
        ShutdownAttemptId("attempt-b"),
        accepted.phase,
        accepted.accepted_outcome,
        accepted.evidence,
        accepted.issuer_proof,
    )

    assert registry.validates_receipt(accepted)
    assert not registry.validates_receipt(forged_proof)
    assert not registry.validates_receipt(copied_proof)


def test_wrong_epoch_and_wrong_work_candidate_never_discharge():
    issuer = object()
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=issuer
    )
    exact = registered_close_work(registry)
    registry.bind(exact, object())
    registry.activate(exact)
    wrong_epoch = ShutdownObligationIdentity(
        ShutdownEpochId("epoch-b"),
        exact.shutdown_work_id,
        exact.resource_kind,
        exact.creation_site,
    )
    wrong_work = ShutdownObligationIdentity(
        exact.shutdown_epoch_id,
        ShutdownWorkId("wrong-work"),
        exact.resource_kind,
        exact.creation_site,
    )

    for wrong in (wrong_epoch, wrong_work):
        with pytest.raises(ValueError):
            registry.accept_candidate(
                exact,
                PhysicalCloseCandidate(
                    wrong,
                    ShutdownAttemptId("attempt-a"),
                    PhysicalCloseOutcome.CLOSED,
                ),
                attempt_id=ShutdownAttemptId("attempt-a"),
                phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
                issuer=issuer,
            )

    assert registry.state(exact).name == "ACTIVE"


def test_malformed_candidate_observation_uses_authenticated_attempt_context():
    issuer = object()
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=issuer
    )
    parent = registered_physical_handle(registry)
    registry.bind(parent, object())
    registry.activate(parent)
    child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    registry.bind(child, object())
    registry.activate(child)
    trusted_attempt = ShutdownAttemptId("trusted-attempt")
    malformed = forge(
        PhysicalCloseCandidate,
        {
            "attempt_id": ShutdownAttemptId("attacker-attempt"),
            "outcome": PhysicalCloseOutcome.CLOSED,
            "evidence": (),
        },
    )

    with pytest.raises(TypeError):
        registry.accept_candidate(
            child,
            malformed,
            attempt_id=trusted_attempt,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    observation = registry.latest_observation(child)
    assert observation is not None
    assert observation.attempt_id == trusted_attempt
    assert observation.kind.name == "MALFORMED_RESULT"
    assert observation.attempt_id.value != "malformed-result"


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        ("shutdown_epoch_id", ShutdownEpochId("raw-epoch-secret")),
        ("active_attempt_id", ShutdownAttemptId("raw-attempt-secret")),
    ],
)
def test_snapshot_constructor_bypass_rejects_raw_internal_ids(
    field: str, raw_value: object
):
    values = {
        "shutdown_epoch_id": ShutdownRedactedIdentifier(
            ShutdownIdentifierKind.EPOCH, 1
        ),
        "active_attempt_id": ShutdownRedactedIdentifier(
            ShutdownIdentifierKind.ATTEMPT, 1
        ),
        "completed_phase": ShutdownPhase.ADMISSION_CLOSED,
        "first_incomplete_phase": ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
        "registry_version": 1,
        "retry_count": 0,
        "force_invoked": False,
        "obligation_counts": ((ShutdownResourceKind.ATTEMPT_CONTROL, 1),),
        "state_counts": ((ShutdownLifetimeState.ACTIVE, 1),),
        "accepted_receipt_count": 0,
    }
    values[field] = raw_value
    malformed = forge(ShutdownCoordinatorSnapshot, values)

    with pytest.raises(TypeError):
        ShutdownCoordinatorSnapshot.__post_init__(malformed)


def test_attempt_result_constructor_bypass_rejects_false_closed_state():
    malformed = forge(
        ShutdownAttemptResult,
        {
            "shutdown_epoch_id": ShutdownEpochId("epoch-a"),
            "attempt_id": ShutdownAttemptId("attempt-a"),
            "status": ShutdownAttemptStatus.CLOSED,
            "completed_phase": ShutdownPhase.VERIFYING,
            "first_incomplete_phase": None,
            "evidence": (),
            "active_obligation_count": 1,
            "registry_version": 1,
        },
    )

    with pytest.raises(ValueError, match="coherent"):
        ShutdownAttemptResult.__post_init__(malformed)
