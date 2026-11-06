"""Closed-world shutdown obligation registry state-machine tests."""

from __future__ import annotations

import gc
import weakref

import pytest

from app.service.analysis.resident_audio import shutdown_registry
from app.service.analysis.resident_audio import shutdown_types
from app.service.analysis.resident_audio.shutdown_registry import (
    ShutdownObligationRegistry,
)
from app.service.analysis.resident_audio.owner_identity import OwnerInstanceId
from app.service.analysis.resident_audio.shutdown_types import (
    NoResourceCandidate,
    NoResourceOutcome,
    PhysicalCloseCandidate,
    PhysicalCloseOutcome,
    ProducerCandidate,
    ProducerOutcome,
    ShutdownAcceptedOutcome,
    ShutdownAttemptId,
    ShutdownCreationSite,
    ShutdownEpochId,
    ShutdownEvidenceItem,
    ShutdownExceptionCategory,
    ShutdownLifetimeState,
    ShutdownObservationKind,
    ShutdownObligationIdentity,
    ShutdownPhase,
    ShutdownResourceKind,
    VerificationCandidate,
    VerificationOutcome,
)


RESOURCE_SCHEMA_CASES = (
    (ShutdownResourceKind.ATTEMPT_CONTROL, "VerificationCandidate",
     ShutdownPhase.VERIFYING, None, "PARENT"),
    (ShutdownResourceKind.CONTROL_WORKER, "ProducerCandidate",
     ShutdownPhase.GLOBAL_GENERATION_BARRIER,
     ShutdownResourceKind.RESOLVER_PRODUCER, "WORK"),
    (ShutdownResourceKind.RETRY_TIMER, "WorkerJoinCandidate",
     ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
     ShutdownResourceKind.ATTEMPT_CONTROL, "WORK"),
    (ShutdownResourceKind.RESOLVER_PRODUCER, "ProducerCandidate",
     ShutdownPhase.GLOBAL_GENERATION_BARRIER,
     ShutdownResourceKind.ATTEMPT_CONTROL, "PARENT"),
    (ShutdownResourceKind.OPEN_PRODUCER, "ProducerCandidate",
     ShutdownPhase.GLOBAL_GENERATION_BARRIER,
     ShutdownResourceKind.RESOLVER_PRODUCER, "PARENT"),
    (ShutdownResourceKind.STREAM_GENERATION, "LogicalBarrierCandidate",
     ShutdownPhase.GLOBAL_GENERATION_BARRIER,
     ShutdownResourceKind.OPEN_PRODUCER, "PARENT"),
    (ShutdownResourceKind.PHYSICAL_HANDLE, "PhysicalCloseCandidate",
     ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
     ShutdownResourceKind.STREAM_GENERATION, "PARENT"),
    (ShutdownResourceKind.INGEST_WORKER, "WorkerJoinCandidate",
     ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
     ShutdownResourceKind.STREAM_GENERATION, "WORK"),
    (ShutdownResourceKind.OWNER_LEASE, "OwnerMilestoneCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.STREAM_GENERATION, "PARENT"),
    (ShutdownResourceKind.ARTIFACT_WRITER, "ArtifactCleanupCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.OWNER_LEASE, "PARENT"),
    (ShutdownResourceKind.COMMIT_WORKER, "WorkerJoinCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.OWNER_LEASE, "WORK"),
    (ShutdownResourceKind.TERMINAL_WORKER, "WorkerJoinCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.OWNER_LEASE, "WORK"),
    (ShutdownResourceKind.OWNER_DRAIN_WORKER, "OwnerMilestoneCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.OWNER_LEASE, "WORK"),
    (ShutdownResourceKind.SETTLEMENT_WORKER, "WorkerJoinCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.OWNER_LEASE, "WORK"),
    (ShutdownResourceKind.ARTIFACT_TARGET, "ArtifactCleanupCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.OWNER_LEASE, "PARENT"),
    (ShutdownResourceKind.BARRIER_WORKER, "LogicalBarrierCandidate",
     ShutdownPhase.GLOBAL_GENERATION_BARRIER,
     ShutdownResourceKind.STREAM_GENERATION, "WORK"),
    (ShutdownResourceKind.CLOSE_WORKER, "PhysicalCloseCandidate",
     ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
     ShutdownResourceKind.PHYSICAL_HANDLE, "WORK"),
    (ShutdownResourceKind.CLEANUP_WORKER, "ArtifactCleanupCandidate",
     ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
     ShutdownResourceKind.ARTIFACT_TARGET, "WORK"),
    (ShutdownResourceKind.FORCE_WORKER, "BackendForceCandidate",
     ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
     ShutdownResourceKind.ATTEMPT_CONTROL, "WORK"),
    (ShutdownResourceKind.VERIFICATION_WORKER, "VerificationCandidate",
     ShutdownPhase.VERIFYING,
     ShutdownResourceKind.ATTEMPT_CONTROL, "WORK"),
)


@pytest.fixture
def proof_kernel():
    issuer = object()
    registry = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-a"), issuer=issuer
    )
    return registry, issuer


def register_parent(registry: ShutdownObligationRegistry):
    return register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )


def register_child(registry: ShutdownObligationRegistry, parent):
    return registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )


def activate(registry: ShutdownObligationRegistry, identity, bound=None):
    registry.bind(identity, object() if bound is None else bound)
    registry.activate(identity)


def close_candidate(identity, outcome, attempt_value="attempt-a"):
    return PhysicalCloseCandidate(
        identity,
        ShutdownAttemptId(attempt_value),
        outcome,
    )


def primary_candidate_type(schema):
    exceptional = {
        NoResourceCandidate,
        shutdown_types.NotStartedCandidate,
    }
    return next(
        item for item in schema.candidate_types if item not in exceptional
    )


def register_with_lineage(
    registry: ShutdownObligationRegistry,
    kind: ShutdownResourceKind,
    *,
    candidate_type=None,
    **identity_fields,
):
    schema = shutdown_registry.shutdown_resource_schema(kind)
    expected_type = (
        primary_candidate_type(schema)
        if candidate_type is None
        else candidate_type
    )
    if kind is ShutdownResourceKind.ATTEMPT_CONTROL:
        return registry.register(
            kind,
            ShutdownCreationSite.ATTEMPT_ENTRY,
            expected_type,
            **identity_fields,
        )
    parent_kind = schema.allowed_parent_kinds[0]
    parent = register_with_lineage(registry, parent_kind)
    return registry.register_child(
        parent,
        kind,
        ShutdownCreationSite.ATTEMPT_ENTRY,
        expected_type,
        **identity_fields,
    )


def test_resource_schema_is_closed_total_and_exhaustive():
    accessor = getattr(shutdown_registry, "shutdown_resource_schema", None)
    assert accessor is not None
    assert {case[0] for case in RESOURCE_SCHEMA_CASES} == set(
        ShutdownResourceKind
    )

    for kind, candidate_name, phase, parent_kind, role in (
        RESOURCE_SCHEMA_CASES
    ):
        schema = accessor(kind)
        assert getattr(shutdown_types, candidate_name) in schema.candidate_types
        assert phase in schema.phases
        assert (
            parent_kind is None
            or parent_kind in schema.allowed_parent_kinds
        )
        assert schema.role.name == role
        assert schema.accepted_outcomes


def test_unknown_resource_schema_input_is_rejected_before_hash_or_default():
    accessor = getattr(shutdown_registry, "shutdown_resource_schema", None)
    assert accessor is not None
    with pytest.raises(TypeError):
        accessor("CLOSE_WORKER")
    with pytest.raises(TypeError):
        accessor(object())


def test_every_unlisted_candidate_and_resource_pair_is_rejected():
    schemas = {
        kind: shutdown_registry.shutdown_resource_schema(kind)
        for kind in ShutdownResourceKind
    }
    candidate_types = {
        candidate_type
        for schema in schemas.values()
        for candidate_type in schema.candidate_types
    }

    for kind, schema in schemas.items():
        mapped_types = {
            mapping[0] for mapping in schema.accepted_outcomes
        }
        assert mapped_types == set(schema.candidate_types)
        for candidate_type in candidate_types - mapped_types:
            registry = ShutdownObligationRegistry(
                ShutdownEpochId("epoch-a"), issuer=object()
            )
            with pytest.raises(TypeError, match="incompatible"):
                registry.register(
                    kind,
                    ShutdownCreationSite.ATTEMPT_ENTRY,
                    candidate_type,
                )


def test_every_unlisted_phase_is_rejected_for_every_resource_kind():
    for kind in ShutdownResourceKind:
        schema = shutdown_registry.shutdown_resource_schema(kind)
        invalid_phases = set(ShutdownPhase) - set(schema.phases)
        for invalid_phase in invalid_phases:
            issuer = object()
            registry = ShutdownObligationRegistry(
                ShutdownEpochId("epoch-a"), issuer=issuer
            )
            attempt = ShutdownAttemptId("attempt-a")
            if schema.role.name == "PARENT":
                identity = register_with_lineage(
                    registry, kind
                )
                candidate = NoResourceCandidate(
                    identity, attempt, NoResourceOutcome.NO_RESOURCE
                )
                accept = registry.accept_absence
            else:
                candidate_type = next(
                    item
                    for item in schema.candidate_types
                    if item is not shutdown_types.NotStartedCandidate
                )
                identity = register_with_lineage(
                    registry, kind, candidate_type=candidate_type
                )
                registry.bind(identity, object())
                registry.activate(identity)
                outcome = next(
                    mapping[1]
                    for mapping in schema.accepted_outcomes
                    if (
                        mapping[0] is candidate_type
                        and not (
                            candidate_type is ProducerCandidate
                            and mapping[1] is ProducerOutcome.MATERIALIZED
                        )
                    )
                )
                candidate = candidate_type(identity, attempt, outcome)
                accept = registry.accept_candidate

            with pytest.raises(ValueError, match="phase"):
                accept(
                    identity,
                    candidate,
                    attempt_id=attempt,
                    phase=invalid_phase,
                    issuer=issuer,
                )


def test_every_parent_and_child_resource_pair_uses_exact_schema_matrix():
    schemas = {
        kind: shutdown_registry.shutdown_resource_schema(kind)
        for kind in ShutdownResourceKind
    }
    for child_kind, child_schema in schemas.items():
        if not child_schema.allowed_parent_kinds:
            continue
        for parent_kind, parent_schema in schemas.items():
            registry = ShutdownObligationRegistry(
                ShutdownEpochId("epoch-a"), issuer=object()
            )
            parent = register_with_lineage(
                registry,
                parent_kind,
                candidate_type=primary_candidate_type(parent_schema),
            )
            if parent_kind in child_schema.allowed_parent_kinds:
                child = registry.register_child(
                    parent,
                    child_kind,
                    ShutdownCreationSite.ATTEMPT_ENTRY,
                    child_schema.candidate_types[0],
                )
                assert child.parent_work_id == parent.shutdown_work_id
            else:
                with pytest.raises(TypeError, match="parent"):
                    registry.register_child(
                        parent,
                        child_kind,
                        ShutdownCreationSite.ATTEMPT_ENTRY,
                        child_schema.candidate_types[0],
                    )


def test_only_explicit_minimal_root_kind_can_omit_parent():
    root_kinds = {
        kind
        for kind in ShutdownResourceKind
        if shutdown_registry.shutdown_resource_schema(kind).root_allowed
    }
    assert root_kinds == {ShutdownResourceKind.ATTEMPT_CONTROL}

    for kind in ShutdownResourceKind:
        registry = ShutdownObligationRegistry(
            ShutdownEpochId("epoch-a"), issuer=object()
        )
        schema = shutdown_registry.shutdown_resource_schema(kind)
        operation = lambda: registry.register(
            kind,
            ShutdownCreationSite.ATTEMPT_ENTRY,
            primary_candidate_type(schema),
        )
        if kind in root_kinds:
            assert operation().parent_work_id is None
        else:
            with pytest.raises(TypeError, match="parent"):
                operation()


def test_registered_parent_cannot_acquire_stale_positive_child_proof(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )
    child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    activate(registry, child)
    candidate = close_candidate(child, PhysicalCloseOutcome.CLOSED)

    with pytest.raises(RuntimeError, match="ACTIVE parent"):
        registry.accept_candidate(
            child,
            candidate,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.state(parent) is ShutdownLifetimeState.REGISTERED
    assert registry.state(child) is ShutdownLifetimeState.ACTIVE
    activate(registry, parent)
    child_receipt = registry.accept_candidate(
        child,
        candidate,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    parent_receipt = registry.discharge_parent(
        parent,
        candidate,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert registry.validates_receipt(child_receipt)
    assert registry.validates_receipt(parent_receipt)
    assert registry.state(parent) is ShutdownLifetimeState.DISCHARGED


def register_active_producer_lineage(registry):
    attempt_control = register_with_lineage(
        registry, ShutdownResourceKind.ATTEMPT_CONTROL
    )
    producer = registry.register_child(
        attempt_control,
        ShutdownResourceKind.RESOLVER_PRODUCER,
        ShutdownCreationSite.RESOLVER_EXECUTION,
        ProducerCandidate,
    )
    activate(registry, producer)
    execution = registry.register_child(
        producer,
        ShutdownResourceKind.CONTROL_WORKER,
        ShutdownCreationSite.RESOLVER_EXECUTION,
        ProducerCandidate,
    )
    activate(registry, execution)
    return producer, execution


def register_materialized_open(registry, producer, *, active=True):
    child = registry.register_child(
        producer,
        ShutdownResourceKind.OPEN_PRODUCER,
        ShutdownCreationSite.OPEN_ATTEMPT,
        ProducerCandidate,
    )
    if active:
        activate(registry, child)
    return child


def materialized_candidate(execution, child, attempt_value="attempt-a"):
    return ProducerCandidate(
        execution,
        ShutdownAttemptId(attempt_value),
        ProducerOutcome.MATERIALIZED,
        materialized_resource_identity=child,
    )


def test_materialized_producer_authenticates_exact_active_reserved_child(
    proof_kernel,
):
    registry, issuer = proof_kernel
    producer, execution = register_active_producer_lineage(registry)
    materialized = register_materialized_open(registry, producer)
    candidate = materialized_candidate(execution, materialized)

    work_receipt = registry.accept_candidate(
        execution,
        candidate,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        issuer=issuer,
    )
    producer_receipt = registry.discharge_parent(
        producer,
        candidate,
        phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        issuer=issuer,
    )

    assert registry.validates_receipt(work_receipt)
    assert registry.validates_receipt(producer_receipt)
    assert registry.state(producer) is ShutdownLifetimeState.DISCHARGED
    assert registry.state(materialized) is ShutdownLifetimeState.ACTIVE
    assert any(
        item.identity.shutdown_work_id == materialized.shutdown_work_id
        for item in registry.snapshot().active_obligations
    )
    with pytest.raises(RuntimeError, match="positive.*proof"):
        registry.register_child(
            materialized,
            ShutdownResourceKind.STREAM_GENERATION,
            ShutdownCreationSite.STREAM_FACTORY,
            shutdown_types.LogicalBarrierCandidate,
        )


@pytest.mark.parametrize("child_case", ["wrong-kind", "cross-parent", "unregistered"])
def test_materialized_producer_rejects_wrong_child_lineage(
    proof_kernel, child_case
):
    registry, issuer = proof_kernel
    producer, execution = register_active_producer_lineage(registry)
    if child_case == "wrong-kind":
        child = registry.register_child(
            producer,
            ShutdownResourceKind.CONTROL_WORKER,
            ShutdownCreationSite.RESOLVER_EXECUTION,
            ProducerCandidate,
        )
        activate(registry, child)
    elif child_case == "cross-parent":
        other_producer, _ = register_active_producer_lineage(registry)
        child = register_materialized_open(registry, other_producer)
    else:
        child = ShutdownObligationIdentity(
            ShutdownEpochId("epoch-a"),
            shutdown_types.ShutdownWorkId("late-unregistered-child"),
            ShutdownResourceKind.OPEN_PRODUCER,
            ShutdownCreationSite.OPEN_ATTEMPT,
            producer.shutdown_work_id,
        )
    candidate = materialized_candidate(execution, child)
    before = registry.snapshot()

    with pytest.raises((TypeError, ValueError, RuntimeError), match="materialized"):
        registry.accept_candidate(
            execution,
            candidate,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
            issuer=issuer,
        )

    assert registry.snapshot() == before
    assert registry.state(execution) is ShutdownLifetimeState.ACTIVE


def test_materialized_producer_rejects_inactive_reserved_child(proof_kernel):
    registry, issuer = proof_kernel
    producer, execution = register_active_producer_lineage(registry)
    inactive_child = register_materialized_open(
        registry, producer, active=False
    )
    candidate = materialized_candidate(execution, inactive_child)

    with pytest.raises(RuntimeError, match="materialized.*ACTIVE"):
        registry.accept_candidate(
            execution,
            candidate,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
            issuer=issuer,
        )

    assert registry.state(inactive_child) is ShutdownLifetimeState.REGISTERED


def test_producer_discharge_rejects_altered_materialized_child(proof_kernel):
    registry, issuer = proof_kernel
    producer, execution = register_active_producer_lineage(registry)
    authenticated = register_materialized_open(registry, producer)
    other_reserved = register_materialized_open(registry, producer)
    exact = materialized_candidate(execution, authenticated)
    registry.accept_candidate(
        execution,
        exact,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        issuer=issuer,
    )
    altered = materialized_candidate(execution, other_reserved)

    with pytest.raises(ValueError, match="altered.*materialized"):
        registry.discharge_parent(
            producer,
            altered,
            phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
            issuer=issuer,
        )

    assert registry.state(producer) is ShutdownLifetimeState.ACTIVE


def test_positive_disposition_proof_freezes_descendant_registration_and_start(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    proof_child = register_child(registry, parent)
    activate(registry, proof_child)
    registered_sibling = register_child(registry, parent)
    bound_sibling = register_child(registry, parent)
    registry.bind(bound_sibling, object())
    positive = close_candidate(proof_child, PhysicalCloseOutcome.CLOSED)
    registry.accept_candidate(
        proof_child,
        positive,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    after_proof = registry.snapshot()

    with pytest.raises(RuntimeError, match="positive.*proof"):
        register_child(registry, parent)
    with pytest.raises(RuntimeError, match="positive.*proof"):
        registry.bind(registered_sibling, object())
    with pytest.raises(RuntimeError, match="positive.*proof"):
        registry.activate(bound_sibling)

    assert registry.snapshot() == after_proof


def test_accepted_owner_milestones_advance_b_then_e_then_f(proof_kernel):
    registry, issuer = proof_kernel
    owner = register_with_lineage(
        registry, ShutdownResourceKind.OWNER_LEASE
    )
    activate(registry, owner)
    cases = (
        (
            ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
            shutdown_types.OwnerMilestoneOutcome.OWNER_SIGNALLED,
        ),
        (
            ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        ),
        (
            ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.TERMINAL_SETTLED,
        ),
    )
    candidates = []
    for index, (phase, outcome) in enumerate(cases):
        work = registry.register_child(
            owner,
            ShutdownResourceKind.OWNER_DRAIN_WORKER,
            ShutdownCreationSite.OWNER_DRAIN_WORKER,
            shutdown_types.OwnerMilestoneCandidate,
        )
        activate(registry, work)
        candidate = shutdown_types.OwnerMilestoneCandidate(
            work, ShutdownAttemptId(f"attempt-{index}"), outcome
        )
        receipt = registry.accept_candidate(
            work,
            candidate,
            attempt_id=ShutdownAttemptId(f"attempt-{index}"),
            phase=phase,
            issuer=issuer,
        )
        assert receipt.phase is phase
        assert registry.state(owner) is ShutdownLifetimeState.ACTIVE
        if phase is not ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED:
            milestone_receipt = registry.accept_parent_milestone(
                owner,
                candidate,
                phase=phase,
                issuer=issuer,
            )
            assert milestone_receipt.accepted_outcome is (
                ShutdownAcceptedOutcome.MILESTONE_ACCEPTED
            )
            assert registry.state(owner) is ShutdownLifetimeState.ACTIVE
        candidates.append(candidate)

    parent_receipt = registry.discharge_parent(
        owner,
        candidates[-1],
        phase=ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
        issuer=issuer,
    )
    assert registry.validates_receipt(parent_receipt)
    assert registry.state(owner) is ShutdownLifetimeState.DISCHARGED


@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        (
            ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
            shutdown_types.OwnerMilestoneOutcome.OWNER_SIGNALLED,
        ),
        (
            ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        ),
    ],
)
def test_earlier_owner_milestone_cannot_discharge_lease(
    proof_kernel, phase, outcome
):
    registry, issuer = proof_kernel
    owner = register_with_lineage(
        registry, ShutdownResourceKind.OWNER_LEASE
    )
    activate(registry, owner)
    work = registry.register_child(
        owner,
        ShutdownResourceKind.OWNER_DRAIN_WORKER,
        ShutdownCreationSite.OWNER_DRAIN_WORKER,
        shutdown_types.OwnerMilestoneCandidate,
    )
    activate(registry, work)
    candidate = shutdown_types.OwnerMilestoneCandidate(
        work, ShutdownAttemptId("attempt-a"), outcome
    )
    registry.accept_candidate(
        work,
        candidate,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=phase,
        issuer=issuer,
    )

    with pytest.raises(ValueError):
        registry.discharge_parent(
            owner,
            candidate,
            phase=ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
            issuer=issuer,
        )

    assert registry.state(owner) is ShutdownLifetimeState.ACTIVE


def test_phase_c_barrier_receipts_stream_without_disposing_later_topology(
    proof_kernel,
):
    registry, issuer = proof_kernel
    stream = register_with_lineage(
        registry, ShutdownResourceKind.STREAM_GENERATION
    )
    activate(registry, stream)
    handle = registry.register_child(
        stream,
        ShutdownResourceKind.PHYSICAL_HANDLE,
        ShutdownCreationSite.BACKEND_OPEN_INPUT,
        PhysicalCloseCandidate,
    )
    ingest = registry.register_child(
        stream,
        ShutdownResourceKind.INGEST_WORKER,
        ShutdownCreationSite.INGEST_WORKER,
        shutdown_types.WorkerJoinCandidate,
    )
    owner = registry.register_child(
        stream,
        ShutdownResourceKind.OWNER_LEASE,
        ShutdownCreationSite.OWNER_FACTORY,
        shutdown_types.OwnerMilestoneCandidate,
    )
    for item in (handle, ingest, owner):
        activate(registry, item)
    barrier = registry.register_child(
        stream,
        ShutdownResourceKind.BARRIER_WORKER,
        ShutdownCreationSite.LOGICAL_BARRIER_WORKER,
        shutdown_types.LogicalBarrierCandidate,
    )
    activate(registry, barrier)
    attempt = ShutdownAttemptId("attempt-c")
    candidate = shutdown_types.LogicalBarrierCandidate(
        barrier, attempt, shutdown_types.LogicalBarrierOutcome.CLEARED
    )

    work_receipt = registry.accept_candidate(
        barrier,
        candidate,
        attempt_id=attempt,
        phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        issuer=issuer,
    )
    milestone_receipt = registry.accept_parent_milestone(
        stream,
        candidate,
        phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        issuer=issuer,
    )

    assert work_receipt.accepted_outcome is (
        ShutdownAcceptedOutcome.WORK_SETTLED
    )
    assert milestone_receipt.accepted_outcome is (
        ShutdownAcceptedOutcome.MILESTONE_ACCEPTED
    )
    for item in (stream, handle, ingest, owner):
        assert registry.state(item) is ShutdownLifetimeState.ACTIVE
    close_work = registry.register_child(
        handle,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    assert registry.state(close_work) is ShutdownLifetimeState.REGISTERED
    with pytest.raises(RuntimeError, match="sealed"):
        registry.register_child(
            stream,
            ShutdownResourceKind.PHYSICAL_HANDLE,
            ShutdownCreationSite.BACKEND_OPEN_INPUT,
            PhysicalCloseCandidate,
        )
    with pytest.raises((RuntimeError, ValueError), match="milestone"):
        registry.discharge_parent(
            stream,
            candidate,
            phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
            issuer=issuer,
        )


def test_repeated_late_handle_proofs_have_a_hard_retention_bound(
    proof_kernel,
):
    registry, issuer = proof_kernel
    stream = register_with_lineage(
        registry, ShutdownResourceKind.STREAM_GENERATION
    )
    activate(registry, stream)
    receipts = []
    for index in range(100):
        handle = registry.register_child(
            stream,
            ShutdownResourceKind.PHYSICAL_HANDLE,
            ShutdownCreationSite.BACKEND_OPEN_INPUT,
            PhysicalCloseCandidate,
        )
        activate(registry, handle)
        close_work = registry.register_child(
            handle,
            ShutdownResourceKind.CLOSE_WORKER,
            ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
            PhysicalCloseCandidate,
        )
        activate(registry, close_work)
        attempt = ShutdownAttemptId(f"late-{index}")
        candidate = close_candidate(
            close_work, PhysicalCloseOutcome.CLOSED, attempt.value
        )
        receipts.append(registry.accept_candidate(
            close_work,
            candidate,
            attempt_id=attempt,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        ))
        registry.discharge_parent(
            handle,
            candidate,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    snapshot = registry.snapshot()
    retained_proofs = sum(
        type(item) is shutdown_registry._DischargedProof
        for item in registry._records.values()
    )
    assert retained_proofs <= (
        shutdown_registry.MAX_RETAINED_DISCHARGED_OBLIGATIONS
    )
    assert snapshot.retained_receipt_count <= (
        shutdown_registry.MAX_RETAINED_RECEIPTS
    )
    assert not registry.validates_receipt(receipts[0])
    assert registry.validates_receipt(receipts[-1])


@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        (
            ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
            shutdown_types.OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        ),
        (
            ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
            shutdown_types.OwnerMilestoneOutcome.TERMINAL_SETTLED,
        ),
        (
            ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.OWNER_SIGNALLED,
        ),
        (
            ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.TERMINAL_SETTLED,
        ),
        (
            ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.OWNER_SIGNALLED,
        ),
        (
            ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
            shutdown_types.OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        ),
    ],
)
def test_owner_milestone_rejects_every_cross_phase_progress(
    proof_kernel, phase, outcome
):
    registry, issuer = proof_kernel
    owner = register_with_lineage(
        registry, ShutdownResourceKind.OWNER_LEASE
    )
    activate(registry, owner)
    work = registry.register_child(
        owner,
        ShutdownResourceKind.OWNER_DRAIN_WORKER,
        ShutdownCreationSite.OWNER_DRAIN_WORKER,
        shutdown_types.OwnerMilestoneCandidate,
    )
    activate(registry, work)
    candidate = shutdown_types.OwnerMilestoneCandidate(
        work, ShutdownAttemptId("attempt-a"), outcome
    )

    with pytest.raises(ValueError, match="phase"):
        registry.accept_candidate(
            work,
            candidate,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=phase,
            issuer=issuer,
        )

    assert registry.state(owner) is ShutdownLifetimeState.ACTIVE
    assert registry.state(work) is ShutdownLifetimeState.ACTIVE


def test_preregistered_primary_work_accepts_exact_not_started_alternative(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )
    activate(registry, parent)
    child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    attempt = ShutdownAttemptId("attempt-a")
    candidate = shutdown_types.NotStartedCandidate(
        child,
        attempt,
        shutdown_types.NotStartedOutcome.NOT_STARTED,
    )

    receipt = registry.accept_candidate(
        child,
        candidate,
        attempt_id=attempt,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert receipt.accepted_outcome is ShutdownAcceptedOutcome.WORK_SETTLED
    assert any(
        item.key == "candidate_kind" and item.value.name == "NOT_STARTED"
        for item in receipt.evidence
    )
    assert registry.state(parent) is ShutdownLifetimeState.ACTIVE


def test_preregistered_primary_parent_accepts_exact_absence_alternative(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )
    attempt = ShutdownAttemptId("attempt-a")
    candidate = NoResourceCandidate(
        parent, attempt, NoResourceOutcome.NO_RESOURCE
    )

    receipt = registry.accept_absence(
        parent,
        candidate,
        attempt_id=attempt,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert receipt.accepted_outcome is ShutdownAcceptedOutcome.NO_RESOURCE
    assert any(
        item.key == "candidate_kind" and item.value.name == "NO_RESOURCE"
        for item in receipt.evidence
    )


@pytest.mark.parametrize(
    "candidate_type",
    [shutdown_types.NotStartedCandidate, NoResourceCandidate],
)
def test_exceptional_alternative_is_rejected_after_live_bind(
    proof_kernel, candidate_type
):
    registry, issuer = proof_kernel
    if candidate_type is shutdown_types.NotStartedCandidate:
        parent = register_with_lineage(
            registry, ShutdownResourceKind.PHYSICAL_HANDLE
        )
        activate(registry, parent)
        identity = registry.register_child(
            parent,
            ShutdownResourceKind.CLOSE_WORKER,
            ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
            PhysicalCloseCandidate,
        )
        outcome = shutdown_types.NotStartedOutcome.NOT_STARTED
        accept = registry.accept_candidate
    else:
        identity = register_with_lineage(
            registry, ShutdownResourceKind.PHYSICAL_HANDLE
        )
        outcome = NoResourceOutcome.NO_RESOURCE
        accept = registry.accept_absence
    registry.bind(identity, object())
    attempt = ShutdownAttemptId("attempt-a")
    candidate = candidate_type(identity, attempt, outcome)

    with pytest.raises(RuntimeError, match="never"):
        accept(
            identity,
            candidate,
            attempt_id=attempt,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )


@pytest.mark.parametrize(
    "descendant_state",
    [
        ShutdownLifetimeState.REGISTERED,
        ShutdownLifetimeState.BOUND,
        ShutdownLifetimeState.ACTIVE,
    ],
)
def test_parent_absence_rejects_every_live_descendant_state(
    proof_kernel, descendant_state
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )
    child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    if descendant_state in (
        ShutdownLifetimeState.BOUND,
        ShutdownLifetimeState.ACTIVE,
    ):
        registry.bind(child, object())
    if descendant_state is ShutdownLifetimeState.ACTIVE:
        registry.activate(child)
    attempt = ShutdownAttemptId("attempt-a")
    candidate = NoResourceCandidate(
        parent, attempt, NoResourceOutcome.NO_RESOURCE
    )
    before = registry.snapshot()

    with pytest.raises(RuntimeError, match="descendant"):
        registry.accept_absence(
            parent,
            candidate,
            attempt_id=attempt,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.snapshot() == before
    assert registry.state(parent) is ShutdownLifetimeState.REGISTERED
    assert registry.state(child) is descendant_state


@pytest.mark.parametrize(
    "sibling_state",
    [
        ShutdownLifetimeState.REGISTERED,
        ShutdownLifetimeState.BOUND,
        ShutdownLifetimeState.ACTIVE,
    ],
)
def test_parent_discharge_rejects_every_other_live_descendant_state(
    proof_kernel, sibling_state
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )
    activate(registry, parent)
    proof_child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    activate(registry, proof_child)
    sibling = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    if sibling_state in (
        ShutdownLifetimeState.BOUND,
        ShutdownLifetimeState.ACTIVE,
    ):
        registry.bind(sibling, object())
    if sibling_state is ShutdownLifetimeState.ACTIVE:
        registry.activate(sibling)
    candidate = close_candidate(
        proof_child, PhysicalCloseOutcome.CLOSED
    )
    registry.accept_candidate(
        proof_child,
        candidate,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    before = registry.snapshot()

    with pytest.raises(RuntimeError, match="descendant"):
        registry.discharge_parent(
            parent,
            candidate,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.snapshot() == before
    assert registry.state(parent) is ShutdownLifetimeState.ACTIVE
    assert registry.state(sibling) is sibling_state


def test_parent_absence_rejects_contradictory_positive_child_proof(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry, ShutdownResourceKind.PHYSICAL_HANDLE
    )
    activate(registry, parent)
    child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    activate(registry, child)
    positive = close_candidate(child, PhysicalCloseOutcome.CLOSED)
    registry.accept_candidate(
        child,
        positive,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    absence = NoResourceCandidate(
        parent,
        ShutdownAttemptId("attempt-b"),
        NoResourceOutcome.NO_RESOURCE,
    )

    with pytest.raises(RuntimeError, match="positive child proof"):
        registry.accept_absence(
            parent,
            absence,
            attempt_id=ShutdownAttemptId("attempt-b"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )


def test_every_parent_candidate_has_executable_authorized_child_path():
    for parent_kind in ShutdownResourceKind:
        parent_schema = shutdown_registry.shutdown_resource_schema(parent_kind)
        if parent_schema.role.name != "PARENT":
            continue
        paths = getattr(parent_schema, "disposition_paths", None)
        assert paths is not None
        primary_types = {
            item
            for item in parent_schema.candidate_types
            if item is not NoResourceCandidate
        }
        assert {path.candidate_type for path in paths} == primary_types

        for path in paths:
            issuer = object()
            registry = ShutdownObligationRegistry(
                ShutdownEpochId("epoch-a"), issuer=issuer
            )
            parent = register_with_lineage(
                registry,
                parent_kind,
                candidate_type=path.candidate_type,
            )
            activate(registry, parent)
            child = registry.register_child(
                parent,
                path.child_resource_kind,
                ShutdownCreationSite.ATTEMPT_ENTRY,
                path.candidate_type,
            )
            activate(registry, child)
            child_schema = shutdown_registry.shutdown_resource_schema(
                path.child_resource_kind
            )
            parent_rules = [
                mapping
                for mapping in parent_schema.accepted_outcomes
                if (
                    mapping[0] is path.candidate_type
                    and mapping[3].name in {"POSITIVE", "PROGRESS"}
                )
            ]
            parent_rule = next(
                (
                    mapping
                    for mapping in parent_rules
                    if mapping[3].name == "POSITIVE"
                ),
                parent_rules[0],
            )
            rule = next(
                mapping
                for mapping in child_schema.accepted_outcomes
                if (
                    mapping[0] is path.candidate_type
                    and mapping[1] is parent_rule[1]
                )
            )
            attempt = ShutdownAttemptId("attempt-a")
            if (
                path.candidate_type is ProducerCandidate
                and rule[1] is ProducerOutcome.MATERIALIZED
            ):
                materialized_kind = (
                    parent_schema.materialized_resource_kinds[0]
                )
                materialized = registry.register_child(
                    parent,
                    materialized_kind,
                    ShutdownCreationSite.ATTEMPT_ENTRY,
                    primary_candidate_type(
                        shutdown_registry.shutdown_resource_schema(
                            materialized_kind
                        )
                    ),
                )
                activate(registry, materialized)
                candidate = path.candidate_type(
                    child,
                    attempt,
                    rule[1],
                    materialized_resource_identity=materialized,
                )
            else:
                candidate = path.candidate_type(child, attempt, rule[1])
            valid_phase = next(
                phase
                for phase in child_schema.phases
                if shutdown_registry.shutdown_candidate_phase_allowed(
                    child_schema,
                    path.candidate_type,
                    rule[1],
                    phase,
                )
            )
            registry.accept_candidate(
                child,
                candidate,
                attempt_id=attempt,
                phase=valid_phase,
                issuer=issuer,
            )
            if (
                parent_rule[2]
                is ShutdownAcceptedOutcome.MILESTONE_ACCEPTED
            ):
                receipt = registry.accept_parent_milestone(
                    parent,
                    candidate,
                    phase=valid_phase,
                    issuer=issuer,
                )
            else:
                receipt = registry.discharge_parent(
                    parent,
                    candidate,
                    phase=valid_phase,
                    issuer=issuer,
                )

            assert registry.validates_receipt(receipt)
            expected_state = (
                ShutdownLifetimeState.ACTIVE
                if (
                    parent_rule[2]
                    is ShutdownAcceptedOutcome.MILESTONE_ACCEPTED
                )
                else ShutdownLifetimeState.DISCHARGED
            )
            assert registry.state(parent) is expected_state


def test_wrong_phase_cannot_mint_or_validate_work_receipt(proof_kernel):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    work = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    activate(registry, work)
    candidate = close_candidate(work, PhysicalCloseOutcome.CLOSED)

    with pytest.raises(ValueError, match="phase"):
        registry.accept_candidate(
            work,
            candidate,
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.VERIFYING,
            issuer=issuer,
        )

    assert registry.state(work) is ShutdownLifetimeState.ACTIVE
    assert registry.snapshot().accepted_receipt_count == 0


def test_unauthorized_parent_and_child_kind_pair_is_rejected(proof_kernel):
    registry, _ = proof_kernel
    parent = register_with_lineage(
        registry,
        ShutdownResourceKind.OWNER_LEASE,
        candidate_type=shutdown_types.OwnerMilestoneCandidate,
    )

    with pytest.raises(TypeError, match="parent"):
        registry.register_child(
            parent,
            ShutdownResourceKind.CLOSE_WORKER,
            ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
            PhysicalCloseCandidate,
        )

    assert registry.state(parent) is ShutdownLifetimeState.REGISTERED


def test_parent_capacity_saturation_preserves_child_progress_slot(
    proof_kernel,
):
    registry, _ = proof_kernel
    parent_limit = getattr(
        shutdown_registry, "MAX_ACTIVE_PARENT_OBLIGATIONS", 64
    )
    parents = [
        register_with_lineage(
            registry, ShutdownResourceKind.ATTEMPT_CONTROL
        )
        for _ in range(parent_limit)
    ]
    child = registry.register_child(
        parents[0],
        ShutdownResourceKind.RETRY_TIMER,
        ShutdownCreationSite.ATTEMPT_ENTRY,
        shutdown_types.WorkerJoinCandidate,
    )

    assert registry.state(child) is ShutdownLifetimeState.REGISTERED
    snapshot = registry.snapshot()
    assert snapshot.active_parent_count == parent_limit
    assert snapshot.active_execution_count == 1
    assert snapshot.parent_capacity == parent_limit
    with pytest.raises(RuntimeError, match="parent"):
        register_with_lineage(
            registry, ShutdownResourceKind.ATTEMPT_CONTROL
        )


def test_child_capacity_overflow_is_independent_from_parent_capacity(
    proof_kernel,
):
    registry, _ = proof_kernel
    child_limit = getattr(
        shutdown_registry, "MAX_ACTIVE_EXECUTION_OBLIGATIONS", 64
    )
    parent = register_parent(registry)
    children = [
        register_child(registry, parent) for _ in range(child_limit)
    ]

    assert len(children) == child_limit
    snapshot = registry.snapshot()
    parent_count = snapshot.active_parent_count
    assert parent_count >= 1
    assert snapshot.active_execution_count == child_limit
    assert snapshot.execution_capacity == child_limit
    with pytest.raises(RuntimeError, match="execution"):
        register_child(registry, parent)
    second_parent = register_parent(registry)
    assert registry.state(second_parent) is ShutdownLifetimeState.REGISTERED
    assert registry.snapshot().active_parent_count > parent_count


def test_registry_mints_unique_work_ids_under_one_latent_epoch(proof_kernel):
    registry, _ = proof_kernel

    first = register_parent(registry)
    version_after_first = registry.version
    second = register_parent(registry)

    assert first.shutdown_epoch_id == ShutdownEpochId("epoch-a")
    assert second.shutdown_epoch_id == first.shutdown_epoch_id
    assert first.shutdown_work_id != second.shutdown_work_id
    assert registry.version > version_after_first


def test_cross_epoch_parent_registration_is_rejected(proof_kernel):
    registry, _ = proof_kernel
    other = ShutdownObligationRegistry(
        ShutdownEpochId("epoch-b"), issuer=object()
    )
    foreign_parent = register_parent(other)
    before = registry.snapshot()

    with pytest.raises(ValueError, match="epoch"):
        register_child(registry, foreign_parent)

    assert registry.snapshot() == before


def test_lifetime_is_strictly_registered_bound_active_discharged(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    child = register_child(registry, parent)
    assert registry.state(child) is ShutdownLifetimeState.REGISTERED

    registry.bind(child, object())
    assert registry.state(child) is ShutdownLifetimeState.BOUND

    registry.activate(child)
    assert registry.state(child) is ShutdownLifetimeState.ACTIVE

    receipt = registry.accept_candidate(
        child,
        close_candidate(child, PhysicalCloseOutcome.CLOSED),
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    assert registry.state(child) is ShutdownLifetimeState.DISCHARGED
    assert receipt.accepted_outcome is ShutdownAcceptedOutcome.WORK_SETTLED
    assert registry.validates_receipt(receipt)
    assert registry.state(parent) is ShutdownLifetimeState.ACTIVE


def test_resource_obligation_cannot_use_direct_work_acceptance(proof_kernel):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)

    with pytest.raises(RuntimeError, match="child work"):
        registry.accept_candidate(
            parent,
            close_candidate(parent, PhysicalCloseOutcome.CLOSED),
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.state(parent) is ShutdownLifetimeState.ACTIVE
    assert registry.snapshot().accepted_receipt_count == 0


def test_no_resource_is_valid_only_for_never_materialized_parent(proof_kernel):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry,
        ShutdownResourceKind.PHYSICAL_HANDLE,
        candidate_type=PhysicalCloseCandidate,
    )
    attempt = ShutdownAttemptId("attempt-a")
    candidate = NoResourceCandidate(
        parent, attempt, NoResourceOutcome.NO_RESOURCE
    )
    accept_absence = getattr(registry, "accept_absence", None)
    assert accept_absence is not None

    first = accept_absence(
        parent,
        candidate,
        attempt_id=attempt,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    repeated = accept_absence(
        parent,
        candidate,
        attempt_id=attempt,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert first == repeated
    assert first.accepted_outcome is ShutdownAcceptedOutcome.NO_RESOURCE
    assert registry.validates_receipt(first)
    assert registry.validates_receipt(first)
    assert registry.state(parent) is ShutdownLifetimeState.DISCHARGED


@pytest.mark.parametrize("activate_live", [False, True])
def test_no_resource_cannot_launder_bound_or_active_live_object(
    proof_kernel, activate_live
):
    registry, issuer = proof_kernel
    parent = register_with_lineage(
        registry,
        ShutdownResourceKind.PHYSICAL_HANDLE,
        candidate_type=PhysicalCloseCandidate,
    )
    registry.bind(parent, object())
    if activate_live:
        registry.activate(parent)
    attempt = ShutdownAttemptId("attempt-a")
    candidate = NoResourceCandidate(
        parent, attempt, NoResourceOutcome.NO_RESOURCE
    )

    with pytest.raises(RuntimeError, match="never-materialized"):
        registry.accept_absence(
            parent,
            candidate,
            attempt_id=attempt,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.state(parent) is (
        ShutdownLifetimeState.ACTIVE
        if activate_live else ShutdownLifetimeState.BOUND
    )


def test_not_started_child_execution_is_separate_from_parent_absence(
    proof_kernel,
):
    candidate_type = getattr(shutdown_types, "NotStartedCandidate", None)
    outcome_type = getattr(shutdown_types, "NotStartedOutcome", None)
    assert candidate_type is not None
    assert outcome_type is not None
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    child = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    attempt = ShutdownAttemptId("attempt-a")
    candidate = candidate_type(child, attempt, outcome_type.NOT_STARTED)

    receipt = registry.accept_candidate(
        child,
        candidate,
        attempt_id=attempt,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert receipt.accepted_outcome is ShutdownAcceptedOutcome.WORK_SETTLED
    assert registry.state(child) is ShutdownLifetimeState.DISCHARGED
    assert registry.state(parent) is ShutdownLifetimeState.REGISTERED


@pytest.mark.parametrize(
    "operation",
    [
        lambda registry, identity: registry.activate(identity),
        lambda registry, identity: registry.record_observation(
            identity,
            ShutdownAttemptId("attempt-a"),
            ShutdownObservationKind.IN_FLIGHT,
        ),
        lambda registry, identity: registry.accept_candidate(
            identity,
            close_candidate(identity, PhysicalCloseOutcome.CLOSED),
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=None,
        ),
    ],
)
def test_wrong_state_calls_are_rejected_without_transition(
    proof_kernel, operation
):
    registry, _ = proof_kernel
    identity = register_parent(registry)

    with pytest.raises((RuntimeError, PermissionError)):
        operation(registry, identity)

    assert registry.state(identity) is ShutdownLifetimeState.REGISTERED


def test_every_mutation_increments_registry_version(proof_kernel):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    identity = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    versions = [registry.version]
    registry.bind(identity, object())
    versions.append(registry.version)
    registry.activate(identity)
    versions.append(registry.version)
    registry.record_observation(
        identity,
        ShutdownAttemptId("attempt-a"),
        ShutdownObservationKind.IN_FLIGHT,
    )
    versions.append(registry.version)
    registry.accept_candidate(
        identity,
        close_candidate(identity, PhysicalCloseOutcome.CLOSED),
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    versions.append(registry.version)

    assert all(after > before for before, after in zip(versions, versions[1:]))


def test_failed_child_discharges_separately_while_parent_remains_active(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    failed_child = register_child(registry, parent)
    activate(registry, failed_child)

    child_receipt = registry.accept_candidate(
        failed_child,
        close_candidate(
            failed_child, PhysicalCloseOutcome.STILL_OPEN, "attempt-a"
        ),
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert registry.state(failed_child) is ShutdownLifetimeState.DISCHARGED
    assert registry.state(parent) is ShutdownLifetimeState.ACTIVE
    assert child_receipt.identity.shutdown_work_id == (
        failed_child.shutdown_work_id
    )
    assert registry.latest_observation(parent).kind is (
        ShutdownObservationKind.TYPED_NEGATIVE
    )


def test_later_positive_child_mints_distinct_parent_disposition_receipt(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    failed_child = register_child(registry, parent)
    activate(registry, failed_child)
    registry.accept_candidate(
        failed_child,
        close_candidate(failed_child, PhysicalCloseOutcome.STILL_OPEN),
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    retry_child = register_child(registry, parent)
    activate(registry, retry_child)
    positive = close_candidate(
        retry_child, PhysicalCloseOutcome.CLOSED, "attempt-b"
    )

    child_receipt = registry.accept_candidate(
        retry_child,
        positive,
        attempt_id=ShutdownAttemptId("attempt-b"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    parent_receipt = registry.discharge_parent(
        parent,
        positive,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    assert child_receipt.identity.shutdown_work_id != (
        parent_receipt.identity.shutdown_work_id
    )
    assert parent_receipt.identity.shutdown_work_id == parent.shutdown_work_id
    assert registry.state(retry_child) is ShutdownLifetimeState.DISCHARGED
    assert registry.state(parent) is ShutdownLifetimeState.DISCHARGED
    assert registry.validates_receipt(child_receipt)
    assert registry.validates_receipt(parent_receipt)


def test_parent_outcome_is_derived_and_caller_cannot_choose_cross_domain_label(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    child = register_child(registry, parent)
    activate(registry, child)
    positive = close_candidate(child, PhysicalCloseOutcome.CLOSED)
    registry.accept_candidate(
        child,
        positive,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    with pytest.raises(TypeError):
        registry.discharge_parent(
            parent,
            positive,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            accepted_outcome=ShutdownAcceptedOutcome.VERIFIED_CLEAN,
            issuer=issuer,
        )

    receipt = registry.discharge_parent(
        parent,
        positive,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    assert receipt.accepted_outcome is (
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED
    )


def test_cross_domain_candidate_cannot_discharge_physical_handle(
    proof_kernel,
):
    registry, _ = proof_kernel

    with pytest.raises(TypeError, match="candidate"):
        registry.register(
            ShutdownResourceKind.PHYSICAL_HANDLE,
            ShutdownCreationSite.BACKEND_OPEN_INPUT,
            VerificationCandidate,
        )

    assert registry.snapshot().active_obligations == ()


def test_cross_parent_result_cannot_discharge_another_resource(proof_kernel):
    registry, issuer = proof_kernel
    first_parent = register_parent(registry)
    second_parent = register_parent(registry)
    activate(registry, first_parent)
    activate(registry, second_parent)
    child = register_child(registry, first_parent)
    activate(registry, child)
    positive = close_candidate(child, PhysicalCloseOutcome.CLOSED)
    registry.accept_candidate(
        child,
        positive,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )

    with pytest.raises(ValueError, match="parent"):
        registry.discharge_parent(
            second_parent,
            positive,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        )

    assert registry.state(second_parent) is ShutdownLifetimeState.ACTIVE


def test_bound_object_is_not_retained_after_discharge(proof_kernel):
    registry, issuer = proof_kernel

    class Payload:
        pass

    payload = Payload()
    reference = weakref.ref(payload)
    parent = register_parent(registry)
    activate(registry, parent)
    identity = registry.register_child(
        parent,
        ShutdownResourceKind.CLOSE_WORKER,
        ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
        PhysicalCloseCandidate,
    )
    activate(registry, identity, payload)
    registry.accept_candidate(
        identity,
        close_candidate(identity, PhysicalCloseOutcome.CLOSED),
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    del payload
    gc.collect()

    assert reference() is None
    snapshot = registry.snapshot()
    assert all(
        item.identity.shutdown_work_id != identity.shutdown_work_id
        for item in snapshot.active_obligations
    )
    assert snapshot.discharged_count == 1


def test_observation_history_and_snapshot_are_bounded_value_only(
    proof_kernel,
):
    registry, _ = proof_kernel
    identity = register_parent(registry)
    activate(registry, identity)
    for index in range(20):
        registry.record_observation(
            identity,
            ShutdownAttemptId(f"attempt-{index}"),
            ShutdownObservationKind.TIMED_OUT,
            exception_type=ShutdownExceptionCategory.TIMEOUT,
        )

    snapshot = registry.snapshot()
    obligation = next(
        item
        for item in snapshot.active_obligations
        if item.identity.shutdown_work_id == identity.shutdown_work_id
    )

    assert len(obligation.observations) == 8
    assert obligation.bound is True
    assert all(
        item.evidence[0].value is ShutdownExceptionCategory.TIMEOUT
        for item in obligation.observations
    )
    assert not hasattr(obligation, "bound_object")


def test_evidence_limit_includes_derived_exception_before_mutation(
    proof_kernel,
):
    registry, _ = proof_kernel
    identity = register_parent(registry)
    activate(registry, identity)
    eight = tuple(
        ShutdownEvidenceItem("attempt_count", index)
        for index in range(8)
    )
    before = registry.snapshot()

    with pytest.raises(ValueError, match="at most"):
        registry.record_observation(
            identity,
            ShutdownAttemptId("attempt-a"),
            ShutdownObservationKind.EXCEPTION,
            evidence=eight,
            exception_type=ShutdownExceptionCategory.TIMEOUT,
        )

    assert registry.snapshot() == before


def test_direct_oversized_observation_evidence_fails_before_mutation(
    proof_kernel,
):
    registry, _ = proof_kernel
    identity = register_parent(registry)
    activate(registry, identity)
    nine = tuple(
        ShutdownEvidenceItem("attempt_count", index)
        for index in range(9)
    )
    before = registry.snapshot()

    with pytest.raises(ValueError, match="at most"):
        registry.record_observation(
            identity,
            ShutdownAttemptId("attempt-a"),
            ShutdownObservationKind.TIMED_OUT,
            evidence=nine,
        )

    assert registry.snapshot() == before


def test_many_discharges_keep_finite_exact_replay_window(proof_kernel):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    receipts = []
    for index in range(160):
        work = registry.register_child(
            parent,
            ShutdownResourceKind.CLOSE_WORKER,
            ShutdownCreationSite.PHYSICAL_CLOSE_WORKER,
            PhysicalCloseCandidate,
        )
        activate(registry, work)
        attempt = ShutdownAttemptId(f"attempt-{index}")
        receipts.append(
            registry.accept_candidate(
                work,
                close_candidate(
                    work, PhysicalCloseOutcome.STILL_OPEN, attempt.value
                ),
                attempt_id=attempt,
                phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
                issuer=issuer,
            )
        )

    snapshot = registry.snapshot()
    assert snapshot.discharged_count == 160
    assert snapshot.accepted_receipt_count == 160
    assert snapshot.retained_discharged_count <= 64
    assert snapshot.retained_receipt_count <= 64
    assert registry.validates_receipt(receipts[-1])
    assert registry.validates_receipt(receipts[-1])
    assert not registry.validates_receipt(receipts[0])


def test_failed_retry_replay_stays_bounded_while_parent_remains_active(
    proof_kernel,
):
    registry, issuer = proof_kernel
    parent = register_parent(registry)
    activate(registry, parent)
    receipts = []
    for index in range(160):
        child = register_child(registry, parent)
        activate(registry, child)
        attempt = ShutdownAttemptId(f"attempt-{index}")
        receipts.append(registry.accept_candidate(
            child,
            close_candidate(
                child, PhysicalCloseOutcome.STILL_OPEN, attempt.value
            ),
            attempt_id=attempt,
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=issuer,
        ))

    snapshot = registry.snapshot()
    assert registry.state(parent) is ShutdownLifetimeState.ACTIVE
    assert snapshot.retained_discharged_count <= 64
    assert snapshot.retained_receipt_count <= 64
    assert not registry.validates_receipt(receipts[0])
    assert registry.validates_receipt(receipts[-1])


def test_snapshot_redacts_owner_capability_nonce_and_tokens(proof_kernel):
    registry, issuer = proof_kernel
    identity = register_with_lineage(
        registry,
        ShutdownResourceKind.PHYSICAL_HANDLE,
        candidate_type=PhysicalCloseCandidate,
        owner_instance_id=OwnerInstanceId(
            "pool-a",
            "hw-1",
            1,
            "run-1",
            "commit-1",
            "OWNER-CAPABILITY-SECRET",
            "OWNER-NONCE-SECRET",
        ),
    )
    activate(registry, identity)

    rendered = repr(registry.snapshot())

    assert "OWNER-CAPABILITY-SECRET" not in rendered
    assert "OWNER-NONCE-SECRET" not in rendered
    assert "issuer" not in rendered.lower()
    assert "token" not in rendered.lower()

    child = register_child(registry, identity)
    activate(registry, child)
    candidate = close_candidate(child, PhysicalCloseOutcome.CLOSED)
    child_receipt = registry.accept_candidate(
        child,
        candidate,
        attempt_id=ShutdownAttemptId("attempt-a"),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    registry.discharge_parent(
        identity,
        candidate,
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        issuer=issuer,
    )
    replay_state = repr(registry._records) + repr(registry._receipt_keys)

    assert "OWNER-CAPABILITY-SECRET" not in replay_state
    assert "OWNER-NONCE-SECRET" not in replay_state
    assert child_receipt.issuer_proof not in repr(registry._receipt_keys)


def test_only_exact_expected_candidate_and_exact_issuer_can_mint_receipt(
    proof_kernel,
):
    registry, issuer = proof_kernel
    identity = register_parent(registry)
    activate(registry, identity)

    with pytest.raises(PermissionError):
        registry.accept_candidate(
            identity,
            close_candidate(identity, PhysicalCloseOutcome.CLOSED),
            attempt_id=ShutdownAttemptId("attempt-a"),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            issuer=object(),
        )

    assert registry.state(identity) is ShutdownLifetimeState.ACTIVE
    assert registry.snapshot().accepted_receipt_count == 0
