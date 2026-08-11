"""Exact value-contract tests for the shutdown proof kernel."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

from app.service.analysis.resident_audio import shutdown_types
from app.service.analysis.resident_audio.owner_identity import OwnerInstanceId
from app.service.analysis.resident_audio.shutdown_types import (
    ArtifactCleanupCandidate,
    ArtifactCleanupOutcome,
    BackendForceCandidate,
    BackendForceOutcome,
    LogicalBarrierCandidate,
    LogicalBarrierOutcome,
    NoResourceCandidate,
    NoResourceOutcome,
    OwnerMilestoneCandidate,
    OwnerMilestoneOutcome,
    PhysicalCloseCandidate,
    PhysicalCloseOutcome,
    ProducerCandidate,
    ProducerOutcome,
    ShutdownAcceptedOutcome,
    ShutdownAttemptId,
    ShutdownAttemptResult,
    ShutdownAttemptStatus,
    ShutdownCoordinatorSnapshot,
    ShutdownCreationSite,
    ShutdownEpochId,
    ShutdownEvidenceItem,
    ShutdownExceptionCategory,
    ShutdownLifetimeState,
    ShutdownObservationKind,
    ShutdownObligationIdentity,
    ShutdownObligationReceipt,
    ShutdownPhase,
    ShutdownIdentifierKind,
    ShutdownRedactedIdentifier,
    ShutdownResourceKind,
    ShutdownWorkId,
    VerificationCandidate,
    VerificationOutcome,
    WorkerJoinCandidate,
    WorkerJoinOutcome,
)


def epoch(value: str = "epoch-a") -> ShutdownEpochId:
    return ShutdownEpochId(value)


def attempt(value: str = "attempt-a") -> ShutdownAttemptId:
    return ShutdownAttemptId(value)


def work(value: str = "work-a") -> ShutdownWorkId:
    return ShutdownWorkId(value)


def owner_id() -> OwnerInstanceId:
    return OwnerInstanceId(
        "pool-a",
        "hw-1",
        3,
        "run-1",
        "commit-1",
        "owner-capability",
        "owner-instance",
    )


def identity(
    *,
    work_id: ShutdownWorkId | None = None,
    parent_work_id: ShutdownWorkId | None = None,
) -> ShutdownObligationIdentity:
    return ShutdownObligationIdentity(
        shutdown_epoch_id=epoch(),
        shutdown_work_id=work_id or work(),
        resource_kind=ShutdownResourceKind.PHYSICAL_HANDLE,
        creation_site=ShutdownCreationSite.BACKEND_OPEN_INPUT,
        parent_work_id=parent_work_id,
        hardware_id="hw-1",
        stream_generation=3,
        run_id="run-1",
        commit_attempt_id="commit-1",
        owner_instance_id=owner_id(),
        artifact_id="artifact-1",
    )


def evidence() -> tuple[ShutdownEvidenceItem, ...]:
    return (
        ShutdownEvidenceItem(
            "exception_type", ShutdownExceptionCategory.TIMEOUT
        ),
        ShutdownEvidenceItem("worker_alive", False),
    )


def test_opaque_shutdown_ids_are_non_empty_frozen_and_distinct_types():
    epoch_id = ShutdownEpochId.mint()
    attempt_id = ShutdownAttemptId.mint()
    work_id = ShutdownWorkId.mint()

    assert epoch_id.value
    assert attempt_id.value
    assert work_id.value
    assert len({epoch_id.value, attempt_id.value, work_id.value}) == 3
    assert type(epoch_id) is ShutdownEpochId
    assert type(attempt_id) is ShutdownAttemptId
    assert type(work_id) is ShutdownWorkId
    with pytest.raises(FrozenInstanceError):
        epoch_id.value = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "identifier_type",
    [ShutdownEpochId, ShutdownAttemptId, ShutdownWorkId],
)
@pytest.mark.parametrize("value", ["", "x" * 257, 1, True, object()])
def test_opaque_shutdown_ids_reject_empty_oversized_and_non_text(
    identifier_type: type[object], value: object
):
    with pytest.raises(TypeError):
        identifier_type(value)  # type: ignore[call-arg]


def test_obligation_identity_carries_every_exact_value_field():
    value = identity(parent_work_id=work("parent-a"))

    assert value.shutdown_epoch_id == epoch()
    assert value.shutdown_work_id == work()
    assert value.parent_work_id == work("parent-a")
    assert value.resource_kind is ShutdownResourceKind.PHYSICAL_HANDLE
    assert value.creation_site is ShutdownCreationSite.BACKEND_OPEN_INPUT
    assert value.hardware_id == "hw-1"
    assert value.stream_generation == 3
    assert value.run_id == "run-1"
    assert value.commit_attempt_id == "commit-1"
    assert value.owner_instance_id == owner_id()
    assert value.artifact_id == "artifact-1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hardware_id", ""),
        ("hardware_id", "x" * 257),
        ("stream_generation", True),
        ("stream_generation", -1),
        ("run_id", object()),
        ("commit_attempt_id", True),
        ("owner_instance_id", object()),
        ("artifact_id", "x" * 257),
    ],
)
def test_obligation_identity_rejects_unbounded_or_wrong_scalar_fields(
    field: str, value: object
):
    values = {
        item.name: getattr(identity(), item.name)
        for item in fields(ShutdownObligationIdentity)
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        ShutdownObligationIdentity(**values)


def test_required_shutdown_enums_are_exact_non_empty_closed_sets():
    enums = (
        ShutdownResourceKind,
        ShutdownCreationSite,
        ShutdownPhase,
        ShutdownLifetimeState,
        ShutdownObservationKind,
        ShutdownAcceptedOutcome,
        ShutdownAttemptStatus,
        ProducerOutcome,
        LogicalBarrierOutcome,
        PhysicalCloseOutcome,
        WorkerJoinOutcome,
        OwnerMilestoneOutcome,
        ArtifactCleanupOutcome,
        BackendForceOutcome,
        VerificationOutcome,
        NoResourceOutcome,
    )

    assert all(tuple(enum_type) for enum_type in enums)
    assert {
        item.name for item in ShutdownObservationKind
    } == {
        "IN_FLIGHT",
        "TYPED_NEGATIVE",
        "MALFORMED_RESULT",
        "EXCEPTION",
        "TIMED_OUT",
        "POSITIVE_CANDIDATE",
        "ACCEPTED",
    }
    assert [item.value for item in ShutdownPhase] == list(range(1, 8))


def test_every_candidate_category_is_an_exact_frozen_slotted_value():
    exact_identity = identity()
    exact_attempt = attempt()
    materialized_identity = identity(
        work_id=work("materialized-resource"),
        parent_work_id=exact_identity.parent_work_id,
    )
    candidates = (
        ProducerCandidate(
            exact_identity,
            exact_attempt,
            ProducerOutcome.MATERIALIZED,
            materialized_resource_identity=materialized_identity,
        ),
        LogicalBarrierCandidate(
            exact_identity, exact_attempt, LogicalBarrierOutcome.CLEARED
        ),
        PhysicalCloseCandidate(
            exact_identity, exact_attempt, PhysicalCloseOutcome.CLOSED
        ),
        WorkerJoinCandidate(
            exact_identity, exact_attempt, WorkerJoinOutcome.JOINED
        ),
        OwnerMilestoneCandidate(
            exact_identity,
            exact_attempt,
            OwnerMilestoneOutcome.TERMINAL_SETTLED,
        ),
        ArtifactCleanupCandidate(
            exact_identity, exact_attempt, ArtifactCleanupOutcome.DISPOSED
        ),
        BackendForceCandidate(
            exact_identity, exact_attempt, BackendForceOutcome.FORCED
        ),
        VerificationCandidate(
            exact_identity, exact_attempt, VerificationOutcome.CLEAN
        ),
        NoResourceCandidate(
            exact_identity, exact_attempt, NoResourceOutcome.NO_RESOURCE
        ),
    )

    assert len({type(item) for item in candidates}) == len(candidates)
    for candidate in candidates:
        assert is_dataclass(candidate)
        assert hasattr(type(candidate), "__slots__")
        with pytest.raises(FrozenInstanceError):
            candidate.attempt_id = attempt("changed")  # type: ignore[misc]


def test_structural_cleanup_lookalike_is_not_an_exact_candidate():
    class Lookalike:
        deleted_paths = ("session/a.partial",)
        failed_paths = ()

    assert type(Lookalike()) is not ArtifactCleanupCandidate


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("unknown", "value"),
        ("exception_type", ""),
        ("exception_type", "x" * 257),
        ("worker_alive", 1),
        ("attempt_count", True),
        ("attempt_count", -1),
        ("resource_id", object()),
        ("path", "secret/file.wav"),
        ("exception_message", "secret"),
    ],
)
def test_evidence_rejects_unknown_unbounded_and_non_scalar_payloads(
    key: str, value: object
):
    with pytest.raises((TypeError, ValueError)):
        ShutdownEvidenceItem(key, value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("resource_id", "session/private/file.wav"),
        ("hardware_id", r"device\private"),
        ("reason_code", "failure contained an exception message"),
        ("result_code", "secret-token-value"),
        ("exception_type", "ValueError: private message"),
        ("resource_id", "Bearer-private-credential"),
    ],
)
def test_evidence_strings_reject_paths_messages_and_token_shaped_values(
    key: str, value: str
):
    with pytest.raises((TypeError, ValueError)):
        ShutdownEvidenceItem(key, value)


@pytest.mark.parametrize(
    ("key", "semantic_type", "member"),
    [
        ("reason_code", "ShutdownReasonCode", "DEADLINE_EXPIRED"),
        ("result_code", "ShutdownResultCode", "CLOSE_FAILED"),
        ("exception_type", "ShutdownExceptionCategory", "TIMEOUT"),
    ],
)
def test_categorical_evidence_requires_exact_semantic_enum(
    key: str, semantic_type: str, member: str
):
    exact_type = getattr(shutdown_types, semantic_type, None)
    assert exact_type is not None
    value = getattr(exact_type, member)
    assert ShutdownEvidenceItem(key, value).value == value


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "sk-project-abcdefghijklmnopqrstuvwxyz",
        "Q7vK9mP2xR8cN4wL6sT1yH5jF3dB0aZ",
        "timeout while closing private device",
        "captures/private/session.wav",
    ],
)
@pytest.mark.parametrize(
    "key",
    [
        "candidate_kind",
        "exception_type",
        "hardware_id",
        "reason_code",
        "resource_id",
        "result_code",
    ],
)
def test_no_evidence_key_accepts_arbitrary_general_string(
    key: str, value: str
):
    with pytest.raises((TypeError, ValueError)):
        ShutdownEvidenceItem(key, value)


def test_candidate_kind_evidence_is_an_exact_semantic_enum():
    candidate_kind = getattr(
        shutdown_types, "ShutdownCandidateKind", None
    )
    assert candidate_kind is not None

    item = ShutdownEvidenceItem(
        "candidate_kind", candidate_kind.NOT_STARTED
    )

    assert item.value is candidate_kind.NOT_STARTED
    with pytest.raises(TypeError):
        ShutdownEvidenceItem("candidate_kind", "NOT_STARTED")
    with pytest.raises(TypeError):
        ShutdownEvidenceItem(
            "result_code", candidate_kind.NOT_STARTED
        )


def test_materialized_producer_requires_one_exact_resource_identity():
    producer_identity = identity()
    child_identity = identity(
        work_id=work("materialized-child"),
        parent_work_id=producer_identity.parent_work_id,
    )

    exact = ProducerCandidate(
        producer_identity,
        attempt(),
        ProducerOutcome.MATERIALIZED,
        materialized_resource_identity=child_identity,
    )

    assert exact.materialized_resource_identity == child_identity
    with pytest.raises(TypeError, match="materialized_resource_identity"):
        ProducerCandidate(
            producer_identity,
            attempt(),
            ProducerOutcome.MATERIALIZED,
        )
    for outcome in (ProducerOutcome.NO_RESOURCE, ProducerOutcome.PENDING):
        with pytest.raises(ValueError, match="only MATERIALIZED"):
            ProducerCandidate(
                producer_identity,
                attempt(),
                outcome,
                materialized_resource_identity=child_identity,
            )


def test_owner_milestones_are_exact_distinct_phase_progress_values():
    assert set(OwnerMilestoneOutcome) == {
        OwnerMilestoneOutcome.OWNER_SIGNALLED,
        OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        OwnerMilestoneOutcome.TERMINAL_SETTLED,
        OwnerMilestoneOutcome.PENDING,
    }


def test_redacted_identifier_is_dedicated_and_not_cross_key_compatible():
    identifier_type = getattr(
        shutdown_types, "ShutdownRedactedIdentifier", None
    )
    identifier_kind = getattr(
        shutdown_types, "ShutdownIdentifierKind", None
    )
    assert identifier_type is not None
    assert identifier_kind is not None
    identifier = identifier_type(identifier_kind.HARDWARE, 7)

    assert ShutdownEvidenceItem("hardware_id", identifier).value == identifier
    with pytest.raises(TypeError):
        ShutdownEvidenceItem("reason_code", identifier)
    with pytest.raises(TypeError):
        ShutdownEvidenceItem("resource_id", 7)


def test_evidence_implementation_has_no_character_or_marker_deny_lists():
    source = (
        __import__(
            "inspect"
        ).getsource(shutdown_types.ShutdownEvidenceItem)
        + __import__("inspect").getsource(shutdown_types)
    )

    assert "_SECRET_MARKERS" not in source
    assert "_EVIDENCE_TEXT_CHARS" not in source


def test_receipt_is_exact_frozen_and_contains_only_bounded_evidence():
    receipt = ShutdownObligationReceipt(
        identity=identity(),
        attempt_id=attempt(),
        phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        accepted_outcome=ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        evidence=evidence(),
        issuer_proof="issuer-proof",
    )

    assert receipt.evidence == evidence()
    with pytest.raises(FrozenInstanceError):
        receipt.phase = ShutdownPhase.VERIFYING  # type: ignore[misc]
    with pytest.raises(ValueError, match="at most"):
        ShutdownObligationReceipt(
            identity=identity(),
            attempt_id=attempt(),
            phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            accepted_outcome=ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
            evidence=evidence() * 5,
            issuer_proof="issuer-proof",
        )


def test_attempt_result_and_coordinator_snapshot_are_immutable_and_bounded():
    epoch_secret = "epoch-super-secret-value"
    attempt_secret = "attempt-super-secret-value"
    result = ShutdownAttemptResult(
        shutdown_epoch_id=epoch(epoch_secret),
        attempt_id=attempt(attempt_secret),
        status=ShutdownAttemptStatus.FAILED_CLOSED,
        completed_phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        first_incomplete_phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        evidence=evidence(),
        active_obligation_count=1,
        registry_version=7,
    )
    snapshot = ShutdownCoordinatorSnapshot(
        shutdown_epoch_id=ShutdownRedactedIdentifier(
            ShutdownIdentifierKind.EPOCH, 1
        ),
        active_attempt_id=ShutdownRedactedIdentifier(
            ShutdownIdentifierKind.ATTEMPT, 2
        ),
        completed_phase=ShutdownPhase.GLOBAL_GENERATION_BARRIER,
        first_incomplete_phase=ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
        registry_version=7,
        retry_count=1,
        force_invoked=False,
        obligation_counts=(
            (ShutdownResourceKind.PHYSICAL_HANDLE, 1),
        ),
        state_counts=((ShutdownLifetimeState.ACTIVE, 1),),
        accepted_receipt_count=0,
    )

    assert result.status is ShutdownAttemptStatus.FAILED_CLOSED
    assert snapshot.obligation_counts[0][1] == 1
    assert epoch_secret not in repr(snapshot)
    assert attempt_secret not in repr(snapshot)
    assert not isinstance(snapshot.shutdown_epoch_id, ShutdownEpochId)
    assert not isinstance(snapshot.active_attempt_id, ShutdownAttemptId)
    with pytest.raises(FrozenInstanceError):
        result.status = ShutdownAttemptStatus.CLOSED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.retry_count = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "values",
    [
        {
            "status": ShutdownAttemptStatus.CLOSED,
            "completed_phase": ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
            "first_incomplete_phase": None,
            "active_obligation_count": 0,
        },
        {
            "status": ShutdownAttemptStatus.CLOSED,
            "completed_phase": ShutdownPhase.VERIFYING,
            "first_incomplete_phase": ShutdownPhase.VERIFYING,
            "active_obligation_count": 0,
        },
        {
            "status": ShutdownAttemptStatus.CLOSED,
            "completed_phase": ShutdownPhase.VERIFYING,
            "first_incomplete_phase": None,
            "active_obligation_count": 1,
        },
        {
            "status": ShutdownAttemptStatus.FAILED_CLOSED,
            "completed_phase": ShutdownPhase.GLOBAL_GENERATION_BARRIER,
            "first_incomplete_phase": None,
            "active_obligation_count": 1,
        },
        {
            "status": ShutdownAttemptStatus.FAILED_CLOSED,
            "completed_phase": ShutdownPhase.PHYSICAL_CLOSE_ISSUED,
            "first_incomplete_phase": ShutdownPhase.GLOBAL_GENERATION_BARRIER,
            "active_obligation_count": 1,
        },
        {
            "status": ShutdownAttemptStatus.FAILED_CLOSED,
            "completed_phase": ShutdownPhase.ADMISSION_CLOSED,
            "first_incomplete_phase": (
                ShutdownPhase.GLOBAL_GENERATION_BARRIER
            ),
            "active_obligation_count": 1,
        },
    ],
)
def test_attempt_result_rejects_semantically_contradictory_completion(
    values
):
    with pytest.raises(ValueError, match="coherent"):
        ShutdownAttemptResult(
            shutdown_epoch_id=epoch(),
            attempt_id=attempt(),
            evidence=(),
            registry_version=1,
            **values,
        )


def test_attempt_result_accepts_exact_closed_and_adjacent_failed_states():
    closed = ShutdownAttemptResult(
        shutdown_epoch_id=epoch(),
        attempt_id=attempt(),
        status=ShutdownAttemptStatus.CLOSED,
        completed_phase=ShutdownPhase.VERIFYING,
        first_incomplete_phase=None,
        evidence=(),
        active_obligation_count=0,
        registry_version=1,
    )
    failed = ShutdownAttemptResult(
        shutdown_epoch_id=epoch(),
        attempt_id=attempt("attempt-b"),
        status=ShutdownAttemptStatus.FAILED_CLOSED,
        completed_phase=ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
        first_incomplete_phase=ShutdownPhase.VERIFYING,
        evidence=(),
        active_obligation_count=0,
        registry_version=2,
    )

    assert closed.status is ShutdownAttemptStatus.CLOSED
    assert failed.first_incomplete_phase is ShutdownPhase.VERIFYING
