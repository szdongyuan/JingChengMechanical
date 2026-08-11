"""Closed, explicit resource policy for resident-audio shutdown."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .shutdown_types import (
    ArtifactCleanupCandidate,
    ArtifactCleanupOutcome,
    BackendForceCandidate,
    BackendForceOutcome,
    LogicalBarrierCandidate,
    LogicalBarrierOutcome,
    NoResourceCandidate,
    NoResourceOutcome,
    NotStartedCandidate,
    NotStartedOutcome,
    OwnerMilestoneCandidate,
    OwnerMilestoneOutcome,
    PhysicalCloseCandidate,
    PhysicalCloseOutcome,
    ProducerCandidate,
    ProducerOutcome,
    ShutdownAcceptedOutcome,
    ShutdownCandidateKind,
    ShutdownPhase,
    ShutdownResourceKind,
    VerificationCandidate,
    VerificationOutcome,
    WorkerJoinCandidate,
    WorkerJoinOutcome,
)


class ShutdownResourceRole(str, Enum):
    PARENT = "PARENT"
    WORK = "WORK"


class ShutdownCandidateDisposition(str, Enum):
    POSITIVE = "POSITIVE"
    PROGRESS = "PROGRESS"
    NEGATIVE = "NEGATIVE"


OutcomeMapping = tuple[
    type,
    Enum,
    ShutdownAcceptedOutcome,
    ShutdownCandidateDisposition,
    ShutdownCandidateKind,
]


@dataclass(frozen=True, slots=True)
class ShutdownDispositionPath:
    child_resource_kind: ShutdownResourceKind
    candidate_type: type


@dataclass(frozen=True, slots=True)
class ShutdownResourceSchema:
    candidate_types: tuple[type, ...]
    phases: tuple[ShutdownPhase, ...]
    accepted_outcomes: tuple[OutcomeMapping, ...]
    allowed_parent_kinds: tuple[ShutdownResourceKind, ...]
    role: ShutdownResourceRole
    root_allowed: bool
    disposition_paths: tuple[ShutdownDispositionPath, ...] = ()
    materialized_resource_kinds: tuple[ShutdownResourceKind, ...] = ()
    outcome_phase_constraints: tuple[
        tuple[type, Enum, tuple[ShutdownPhase, ...]], ...
    ] = ()
    seals_resource_subtree: bool = False

_ABSENT_OUTCOME: OutcomeMapping = (
    NoResourceCandidate,
    NoResourceOutcome.NO_RESOURCE,
    ShutdownAcceptedOutcome.NO_RESOURCE,
    ShutdownCandidateDisposition.POSITIVE,
    ShutdownCandidateKind.NO_RESOURCE,
)
_NOT_STARTED_OUTCOME: OutcomeMapping = (
    NotStartedCandidate,
    NotStartedOutcome.NOT_STARTED,
    ShutdownAcceptedOutcome.WORK_SETTLED,
    ShutdownCandidateDisposition.NEGATIVE,
    ShutdownCandidateKind.NOT_STARTED,
)
_WORKER_JOIN_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        WorkerJoinCandidate,
        WorkerJoinOutcome.JOINED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.WORKER_JOIN,
    ),
    (
        WorkerJoinCandidate,
        WorkerJoinOutcome.STILL_ALIVE,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.WORKER_JOIN,
    ),
    _NOT_STARTED_OUTCOME,
)
_PRODUCER_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        ProducerCandidate,
        ProducerOutcome.MATERIALIZED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.PRODUCER,
    ),
    (
        ProducerCandidate,
        ProducerOutcome.NO_RESOURCE,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.PRODUCER,
    ),
    (
        ProducerCandidate,
        ProducerOutcome.PENDING,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.PRODUCER,
    ),
    _NOT_STARTED_OUTCOME,
)
_OWNER_MILESTONE_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.OWNER_SIGNALLED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.PROGRESS,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.PROGRESS,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.TERMINAL_SETTLED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.PENDING,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    _NOT_STARTED_OUTCOME,
)
_LOGICAL_BARRIER_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        LogicalBarrierCandidate,
        LogicalBarrierOutcome.CLEARED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.PROGRESS,
        ShutdownCandidateKind.LOGICAL_BARRIER,
    ),
    (
        LogicalBarrierCandidate,
        LogicalBarrierOutcome.NOT_CLEARED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.LOGICAL_BARRIER,
    ),
    _NOT_STARTED_OUTCOME,
)
_PHYSICAL_CLOSE_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        PhysicalCloseCandidate,
        PhysicalCloseOutcome.CLOSED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.PHYSICAL_CLOSE,
    ),
    (
        PhysicalCloseCandidate,
        PhysicalCloseOutcome.STILL_OPEN,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.PHYSICAL_CLOSE,
    ),
    _NOT_STARTED_OUTCOME,
)
_ARTIFACT_CLEANUP_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        ArtifactCleanupCandidate,
        ArtifactCleanupOutcome.DISPOSED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.ARTIFACT_CLEANUP,
    ),
    (
        ArtifactCleanupCandidate,
        ArtifactCleanupOutcome.RETAINED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.ARTIFACT_CLEANUP,
    ),
    (
        ArtifactCleanupCandidate,
        ArtifactCleanupOutcome.FAILED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.ARTIFACT_CLEANUP,
    ),
    _NOT_STARTED_OUTCOME,
)
_BACKEND_FORCE_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        BackendForceCandidate,
        BackendForceOutcome.FORCED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.BACKEND_FORCE,
    ),
    (
        BackendForceCandidate,
        BackendForceOutcome.NOT_REQUIRED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.BACKEND_FORCE,
    ),
    (
        BackendForceCandidate,
        BackendForceOutcome.FAILED,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.BACKEND_FORCE,
    ),
    _NOT_STARTED_OUTCOME,
)
_VERIFICATION_WORK_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        VerificationCandidate,
        VerificationOutcome.CLEAN,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.VERIFICATION,
    ),
    (
        VerificationCandidate,
        VerificationOutcome.LIVE_RESOURCES,
        ShutdownAcceptedOutcome.WORK_SETTLED,
        ShutdownCandidateDisposition.NEGATIVE,
        ShutdownCandidateKind.VERIFICATION,
    ),
    _NOT_STARTED_OUTCOME,
)
_VERIFICATION_PARENT_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        VerificationCandidate,
        VerificationOutcome.CLEAN,
        ShutdownAcceptedOutcome.VERIFIED_CLEAN,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.VERIFICATION,
    ),
    _ABSENT_OUTCOME,
)
_PRODUCER_PARENT_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        ProducerCandidate,
        ProducerOutcome.MATERIALIZED,
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.PRODUCER,
    ),
    (
        ProducerCandidate,
        ProducerOutcome.NO_RESOURCE,
        ShutdownAcceptedOutcome.NO_RESOURCE,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.PRODUCER,
    ),
    _ABSENT_OUTCOME,
)
_LOGICAL_BARRIER_PARENT_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        LogicalBarrierCandidate,
        LogicalBarrierOutcome.CLEARED,
        ShutdownAcceptedOutcome.MILESTONE_ACCEPTED,
        ShutdownCandidateDisposition.PROGRESS,
        ShutdownCandidateKind.LOGICAL_BARRIER,
    ),
    _ABSENT_OUTCOME,
)
_PHYSICAL_CLOSE_PARENT_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        PhysicalCloseCandidate,
        PhysicalCloseOutcome.CLOSED,
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.PHYSICAL_CLOSE,
    ),
    _ABSENT_OUTCOME,
)
_OWNER_MILESTONE_PARENT_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.OWNER_SIGNALLED,
        ShutdownAcceptedOutcome.MILESTONE_ACCEPTED,
        ShutdownCandidateDisposition.PROGRESS,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
        ShutdownAcceptedOutcome.MILESTONE_ACCEPTED,
        ShutdownCandidateDisposition.PROGRESS,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    (
        OwnerMilestoneCandidate,
        OwnerMilestoneOutcome.TERMINAL_SETTLED,
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.OWNER_MILESTONE,
    ),
    _ABSENT_OUTCOME,
)
_ARTIFACT_CLEANUP_PARENT_OUTCOMES: tuple[OutcomeMapping, ...] = (
    (
        ArtifactCleanupCandidate,
        ArtifactCleanupOutcome.DISPOSED,
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.ARTIFACT_CLEANUP,
    ),
    (
        ArtifactCleanupCandidate,
        ArtifactCleanupOutcome.RETAINED,
        ShutdownAcceptedOutcome.RESOURCE_DISPOSED,
        ShutdownCandidateDisposition.POSITIVE,
        ShutdownCandidateKind.ARTIFACT_CLEANUP,
    ),
    _ABSENT_OUTCOME,
)


_RESOURCE_SCHEMAS = {
    ShutdownResourceKind.ATTEMPT_CONTROL: ShutdownResourceSchema(
        candidate_types=(
            VerificationCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.VERIFYING,),
        accepted_outcomes=_VERIFICATION_PARENT_OUTCOMES,
        allowed_parent_kinds=(),
        role=ShutdownResourceRole.PARENT,
        root_allowed=True,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.VERIFICATION_WORKER,
            VerificationCandidate),),
    ),
    ShutdownResourceKind.CONTROL_WORKER: ShutdownResourceSchema(
        candidate_types=(
            ProducerCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.GLOBAL_GENERATION_BARRIER,),
        accepted_outcomes=_PRODUCER_WORK_OUTCOMES,
        allowed_parent_kinds=(
            ShutdownResourceKind.RESOLVER_PRODUCER,
            ShutdownResourceKind.OPEN_PRODUCER,
        ),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.RETRY_TIMER: ShutdownResourceSchema(
        candidate_types=(
            WorkerJoinCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,),
        accepted_outcomes=_WORKER_JOIN_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.ATTEMPT_CONTROL,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.RESOLVER_PRODUCER: ShutdownResourceSchema(
        candidate_types=(
            ProducerCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.GLOBAL_GENERATION_BARRIER,),
        accepted_outcomes=_PRODUCER_PARENT_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.ATTEMPT_CONTROL,),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.CONTROL_WORKER,
            ProducerCandidate),),
        materialized_resource_kinds=(
            ShutdownResourceKind.OPEN_PRODUCER,
        ),
    ),
    ShutdownResourceKind.OPEN_PRODUCER: ShutdownResourceSchema(
        candidate_types=(
            ProducerCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.GLOBAL_GENERATION_BARRIER,),
        accepted_outcomes=_PRODUCER_PARENT_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.RESOLVER_PRODUCER,),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.CONTROL_WORKER,
            ProducerCandidate),),
        materialized_resource_kinds=(
            ShutdownResourceKind.STREAM_GENERATION,
        ),
    ),
    ShutdownResourceKind.STREAM_GENERATION: ShutdownResourceSchema(
        candidate_types=(
            LogicalBarrierCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.GLOBAL_GENERATION_BARRIER,),
        accepted_outcomes=_LOGICAL_BARRIER_PARENT_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.OPEN_PRODUCER,),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.BARRIER_WORKER,
            LogicalBarrierCandidate),),
        seals_resource_subtree=True,
    ),
    ShutdownResourceKind.PHYSICAL_HANDLE: ShutdownResourceSchema(
        candidate_types=(
            PhysicalCloseCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.PHYSICAL_CLOSE_ISSUED,),
        accepted_outcomes=_PHYSICAL_CLOSE_PARENT_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.STREAM_GENERATION,),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.CLOSE_WORKER,
            PhysicalCloseCandidate),),
    ),
    ShutdownResourceKind.INGEST_WORKER: ShutdownResourceSchema(
        candidate_types=(
            WorkerJoinCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,),
        accepted_outcomes=_WORKER_JOIN_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.STREAM_GENERATION,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.OWNER_LEASE: ShutdownResourceSchema(
        candidate_types=(
            OwnerMilestoneCandidate, NoResourceCandidate),
        phases=(
            ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
            ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
            ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
        ),
        accepted_outcomes=_OWNER_MILESTONE_PARENT_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.STREAM_GENERATION,),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.OWNER_DRAIN_WORKER,
            OwnerMilestoneCandidate),),
        outcome_phase_constraints=(
            (
                OwnerMilestoneCandidate,
                OwnerMilestoneOutcome.OWNER_SIGNALLED,
                (ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,),
            ),
            (
                OwnerMilestoneCandidate,
                OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
                (ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,),
            ),
            (
                OwnerMilestoneCandidate,
                OwnerMilestoneOutcome.TERMINAL_SETTLED,
                (ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
            ),
            (
                NoResourceCandidate,
                NoResourceOutcome.NO_RESOURCE,
                (ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
            ),
        ),
    ),
    ShutdownResourceKind.ARTIFACT_WRITER: ShutdownResourceSchema(
        candidate_types=(
            ArtifactCleanupCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
        accepted_outcomes=_ARTIFACT_CLEANUP_PARENT_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.OWNER_LEASE,),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.CLEANUP_WORKER,
            ArtifactCleanupCandidate),),
    ),
    ShutdownResourceKind.COMMIT_WORKER: ShutdownResourceSchema(
        candidate_types=(
            WorkerJoinCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
        accepted_outcomes=_WORKER_JOIN_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.OWNER_LEASE,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.TERMINAL_WORKER: ShutdownResourceSchema(
        candidate_types=(
            WorkerJoinCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
        accepted_outcomes=_WORKER_JOIN_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.OWNER_LEASE,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.OWNER_DRAIN_WORKER: ShutdownResourceSchema(
        candidate_types=(
            OwnerMilestoneCandidate, NotStartedCandidate),
        phases=(
            ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,
            ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,
            ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,
        ),
        accepted_outcomes=_OWNER_MILESTONE_WORK_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.OWNER_LEASE,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
        outcome_phase_constraints=(
            (
                OwnerMilestoneCandidate,
                OwnerMilestoneOutcome.OWNER_SIGNALLED,
                (ShutdownPhase.ROLLBACK_AND_LOGICAL_CANCEL,),
            ),
            (
                OwnerMilestoneCandidate,
                OwnerMilestoneOutcome.PRE_TERMINAL_FINALITY,
                (ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,),
            ),
            (
                OwnerMilestoneCandidate,
                OwnerMilestoneOutcome.TERMINAL_SETTLED,
                (ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
            ),
        ),
    ),
    ShutdownResourceKind.SETTLEMENT_WORKER: ShutdownResourceSchema(
        candidate_types=(
            WorkerJoinCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
        accepted_outcomes=_WORKER_JOIN_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.OWNER_LEASE,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.ARTIFACT_TARGET: ShutdownResourceSchema(
        candidate_types=(
            ArtifactCleanupCandidate, NoResourceCandidate),
        phases=(ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
        accepted_outcomes=_ARTIFACT_CLEANUP_PARENT_OUTCOMES,
        allowed_parent_kinds=(
            ShutdownResourceKind.OWNER_LEASE,
            ShutdownResourceKind.ARTIFACT_WRITER,
        ),
        role=ShutdownResourceRole.PARENT,
        root_allowed=False,
        disposition_paths=(ShutdownDispositionPath(
            ShutdownResourceKind.CLEANUP_WORKER,
            ArtifactCleanupCandidate),),
    ),
    ShutdownResourceKind.BARRIER_WORKER: ShutdownResourceSchema(
        candidate_types=(
            LogicalBarrierCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.GLOBAL_GENERATION_BARRIER,),
        accepted_outcomes=_LOGICAL_BARRIER_WORK_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.STREAM_GENERATION,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.CLOSE_WORKER: ShutdownResourceSchema(
        candidate_types=(
            PhysicalCloseCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.PHYSICAL_CLOSE_ISSUED,),
        accepted_outcomes=_PHYSICAL_CLOSE_WORK_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.PHYSICAL_HANDLE,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.CLEANUP_WORKER: ShutdownResourceSchema(
        candidate_types=(
            ArtifactCleanupCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.TERMINAL_AND_ARTIFACT_SETTLED,),
        accepted_outcomes=_ARTIFACT_CLEANUP_WORK_OUTCOMES,
        allowed_parent_kinds=(
            ShutdownResourceKind.ARTIFACT_TARGET,
            ShutdownResourceKind.ARTIFACT_WRITER,
        ),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.FORCE_WORKER: ShutdownResourceSchema(
        candidate_types=(
            BackendForceCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.COOPERATIVE_JOIN_AND_FORCE_SETTLED,),
        accepted_outcomes=_BACKEND_FORCE_WORK_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.ATTEMPT_CONTROL,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
    ShutdownResourceKind.VERIFICATION_WORKER: ShutdownResourceSchema(
        candidate_types=(
            VerificationCandidate, NotStartedCandidate),
        phases=(ShutdownPhase.VERIFYING,),
        accepted_outcomes=_VERIFICATION_WORK_OUTCOMES,
        allowed_parent_kinds=(ShutdownResourceKind.ATTEMPT_CONTROL,),
        role=ShutdownResourceRole.WORK,
        root_allowed=False,
    ),
}

def _validate_resource_schemas() -> None:
    if set(_RESOURCE_SCHEMAS) != set(ShutdownResourceKind):
        raise RuntimeError("shutdown resource schema is not total")

    for kind, schema in _RESOURCE_SCHEMAS.items():
        if type(kind) is not ShutdownResourceKind:
            raise RuntimeError("shutdown resource schema has an invalid key")
        if not schema.candidate_types or not schema.phases:
            raise RuntimeError("shutdown resource schema is incomplete")
        if len(set(schema.candidate_types)) != len(schema.candidate_types):
            raise RuntimeError("shutdown resource schema repeats a candidate")
        if len(set(schema.phases)) != len(schema.phases):
            raise RuntimeError("shutdown resource schema repeats a phase")
        if any(type(phase) is not ShutdownPhase for phase in schema.phases):
            raise RuntimeError("shutdown resource schema has an invalid phase")
        if any(
            type(parent_kind) is not ShutdownResourceKind
            for parent_kind in schema.allowed_parent_kinds
        ):
            raise RuntimeError("shutdown resource schema has an invalid parent")
        if schema.root_allowed is not (
            kind is ShutdownResourceKind.ATTEMPT_CONTROL
        ):
            raise RuntimeError("shutdown resource schema has invalid root policy")
        for rule in schema.accepted_outcomes:
            candidate_type, outcome, accepted, disposition, candidate_kind = rule
            if candidate_type not in schema.candidate_types:
                raise RuntimeError("shutdown outcome names an invalid candidate")
            if not isinstance(outcome, Enum):
                raise RuntimeError("shutdown schema outcome must be an enum")
            if type(accepted) is not ShutdownAcceptedOutcome:
                raise RuntimeError("shutdown accepted outcome must be exact")
            if type(disposition) is not ShutdownCandidateDisposition:
                raise RuntimeError("shutdown disposition must be exact")
            if type(candidate_kind) is not ShutdownCandidateKind:
                raise RuntimeError("shutdown candidate kind must be exact")
        for candidate_type, outcome, phases in (
            schema.outcome_phase_constraints
        ):
            if not any(
                mapping[0] is candidate_type and mapping[1] is outcome
                for mapping in schema.accepted_outcomes
            ):
                raise RuntimeError(
                    "shutdown phase constraint names an invalid outcome"
                )
            if (
                not phases
                or any(phase not in schema.phases for phase in phases)
            ):
                raise RuntimeError(
                    "shutdown outcome phase constraint is invalid"
                )

    for kind, schema in _RESOURCE_SCHEMAS.items():
        for parent_kind in schema.allowed_parent_kinds:
            parent = _RESOURCE_SCHEMAS[parent_kind]
            if parent.role is not ShutdownResourceRole.PARENT:
                raise RuntimeError("shutdown child names a work item as parent")
        if (schema.role is ShutdownResourceRole.PARENT) is not bool(
            schema.disposition_paths
        ):
            raise RuntimeError("shutdown parent disposition path is incomplete")
        for path in schema.disposition_paths:
            child = _RESOURCE_SCHEMAS[path.child_resource_kind]
            if (
                child.role is not ShutdownResourceRole.WORK
                or kind not in child.allowed_parent_kinds
                or path.candidate_type not in child.candidate_types
                or path.candidate_type not in schema.candidate_types
            ):
                raise RuntimeError("shutdown disposition path is unreachable")
        if schema.materialized_resource_kinds and kind not in (
            ShutdownResourceKind.RESOLVER_PRODUCER,
            ShutdownResourceKind.OPEN_PRODUCER,
        ):
            raise RuntimeError(
                "only producer resources may declare materialized children"
            )
        for child_kind in schema.materialized_resource_kinds:
            child = _RESOURCE_SCHEMAS[child_kind]
            if (
                child.role is not ShutdownResourceRole.PARENT
                or kind not in child.allowed_parent_kinds
            ):
                raise RuntimeError(
                    "materialized producer child path is unreachable"
                )
        if schema.seals_resource_subtree is not (
            kind is ShutdownResourceKind.STREAM_GENERATION
        ):
            raise RuntimeError("shutdown resource subtree seal is invalid")


_validate_resource_schemas()


def shutdown_resource_schema(kind: ShutdownResourceKind) -> ShutdownResourceSchema:
    if type(kind) is not ShutdownResourceKind or not any(
        kind is member for member in ShutdownResourceKind
    ):
        raise TypeError("resource_kind must be an exact ShutdownResourceKind")
    return _RESOURCE_SCHEMAS[kind]


def shutdown_candidate_phase_allowed(
    schema: ShutdownResourceSchema,
    candidate_type: type,
    outcome: Enum,
    phase: ShutdownPhase,
) -> bool:
    for constrained_type, constrained_outcome, phases in (
        schema.outcome_phase_constraints
    ):
        if (
            candidate_type is constrained_type
            and outcome is constrained_outcome
        ):
            return any(phase is item for item in phases)
    return any(phase is item for item in schema.phases)
