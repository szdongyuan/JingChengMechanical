"""Exact, bounded value contracts for resident-audio shutdown proof."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import ClassVar, Self, TypeAlias

from .owner_finality_canonical import canonical_owner_instance_id
from .owner_identity import MAX_SIGNED_64, OwnerInstanceId

MAX_SHUTDOWN_TEXT_LENGTH = 256
MAX_SHUTDOWN_EVIDENCE_ITEMS = 8
MAX_SHUTDOWN_SNAPSHOT_ITEMS = 64

def _field(value: object, name: str, owner: str) -> object:
    try:
        return object.__getattribute__(value, name)
    except AttributeError as exc:
        raise TypeError(f"{owner}.{name} is required") from exc

def _text(value: object, owner: str, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_SHUTDOWN_TEXT_LENGTH
    ):
        raise TypeError(f"{owner}.{field} must be a bounded non-empty string")
    return value

def _optional_text(value: object, owner: str, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, owner, field)

def _integer(
    value: object,
    owner: str,
    field: str,
    *,
    minimum: int = 0,
) -> int:
    if type(value) is not int:
        raise TypeError(f"{owner}.{field} must be an integer")
    if not minimum <= value <= MAX_SIGNED_64:
        raise ValueError(
            f"{owner}.{field} must be in {minimum}..{MAX_SIGNED_64}"
        )
    return value

def _optional_scalar_id(value: object, owner: str, field: str) -> str | int | None:
    if value is None:
        return None
    if type(value) is str:
        return _text(value, owner, field)
    if type(value) is int:
        return _integer(value, owner, field)
    raise TypeError(f"{owner}.{field} must be a string or integer")

def _exact_enum(value: object, enum_type: type[Enum], owner: str, field: str) -> Enum:
    if type(value) is not enum_type or not any(
        value is member for member in enum_type
    ):
        raise TypeError(
            f"{owner}.{field} must be an exact {enum_type.__name__}"
        )
    return value

@dataclass(frozen=True, slots=True)
class _ShutdownTextId:
    value: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _text(_field(self, "value", owner), owner, "value")

    @classmethod
    def mint(cls) -> Self:
        return cls(uuid.uuid4().hex)

class ShutdownEpochId(_ShutdownTextId):
    __slots__ = ()

class ShutdownAttemptId(_ShutdownTextId):
    __slots__ = ()

class ShutdownWorkId(_ShutdownTextId):
    __slots__ = ()

class ShutdownResourceKind(str, Enum):
    ATTEMPT_CONTROL = "ATTEMPT_CONTROL"
    CONTROL_WORKER = "CONTROL_WORKER"
    RETRY_TIMER = "RETRY_TIMER"
    RESOLVER_PRODUCER = "RESOLVER_PRODUCER"
    OPEN_PRODUCER = "OPEN_PRODUCER"
    STREAM_GENERATION = "STREAM_GENERATION"
    PHYSICAL_HANDLE = "PHYSICAL_HANDLE"
    INGEST_WORKER = "INGEST_WORKER"
    OWNER_LEASE = "OWNER_LEASE"
    ARTIFACT_WRITER = "ARTIFACT_WRITER"
    COMMIT_WORKER = "COMMIT_WORKER"
    TERMINAL_WORKER = "TERMINAL_WORKER"
    OWNER_DRAIN_WORKER = "OWNER_DRAIN_WORKER"
    SETTLEMENT_WORKER = "SETTLEMENT_WORKER"
    ARTIFACT_TARGET = "ARTIFACT_TARGET"
    BARRIER_WORKER = "BARRIER_WORKER"
    CLOSE_WORKER = "CLOSE_WORKER"
    CLEANUP_WORKER = "CLEANUP_WORKER"
    FORCE_WORKER = "FORCE_WORKER"
    VERIFICATION_WORKER = "VERIFICATION_WORKER"

class ShutdownCreationSite(str, Enum):
    ATTEMPT_ENTRY = "ATTEMPT_ENTRY"
    RECONCILE_EXECUTION = "RECONCILE_EXECUTION"
    RECONNECT_EXECUTION = "RECONNECT_EXECUTION"
    RETRY_TIMER = "RETRY_TIMER"
    RETRY_CALLBACK = "RETRY_CALLBACK"
    PENDING_REMOVAL_WORKER = "PENDING_REMOVAL_WORKER"
    REPLACEMENT_WORKER = "REPLACEMENT_WORKER"
    RESOLVER_EXECUTION = "RESOLVER_EXECUTION"
    OPEN_ATTEMPT = "OPEN_ATTEMPT"
    STREAM_FACTORY = "STREAM_FACTORY"
    STREAM_PUBLICATION = "STREAM_PUBLICATION"
    BACKEND_OPEN_INPUT = "BACKEND_OPEN_INPUT"
    BACKEND_HANDLE_START = "BACKEND_HANDLE_START"
    INGEST_WORKER = "INGEST_WORKER"
    ARTIFACT_WRITER = "ARTIFACT_WRITER"
    OWNER_FACTORY = "OWNER_FACTORY"
    OWNER_DRAIN_WORKER = "OWNER_DRAIN_WORKER"
    COMMIT_WORKER = "COMMIT_WORKER"
    ROLLBACK_WORKER = "ROLLBACK_WORKER"
    TERMINAL_WORKER = "TERMINAL_WORKER"
    SETTLEMENT_WORKER = "SETTLEMENT_WORKER"
    STREAM_FAILURE_WORKER = "STREAM_FAILURE_WORKER"
    LOGICAL_BARRIER_WORKER = "LOGICAL_BARRIER_WORKER"
    PHYSICAL_CLOSE_WORKER = "PHYSICAL_CLOSE_WORKER"
    ARTIFACT_CLEANUP_TARGET = "ARTIFACT_CLEANUP_TARGET"
    ARTIFACT_CLEANUP_WORKER = "ARTIFACT_CLEANUP_WORKER"
    BACKEND_FORCE_WORKER = "BACKEND_FORCE_WORKER"
    VERIFICATION_WORKER = "VERIFICATION_WORKER"

class ShutdownPhase(IntEnum):
    ADMISSION_CLOSED = 1
    ROLLBACK_AND_LOGICAL_CANCEL = 2
    GLOBAL_GENERATION_BARRIER = 3
    PHYSICAL_CLOSE_ISSUED = 4
    COOPERATIVE_JOIN_AND_FORCE_SETTLED = 5
    TERMINAL_AND_ARTIFACT_SETTLED = 6
    VERIFYING = 7

class ShutdownLifetimeState(str, Enum):
    REGISTERED = "REGISTERED"
    BOUND = "BOUND"
    ACTIVE = "ACTIVE"
    DISCHARGED = "DISCHARGED"
class ShutdownObservationKind(str, Enum):
    IN_FLIGHT = "IN_FLIGHT"
    TYPED_NEGATIVE = "TYPED_NEGATIVE"
    MALFORMED_RESULT = "MALFORMED_RESULT"
    EXCEPTION = "EXCEPTION"
    TIMED_OUT = "TIMED_OUT"
    POSITIVE_CANDIDATE = "POSITIVE_CANDIDATE"
    ACCEPTED = "ACCEPTED"
class ShutdownAcceptedOutcome(str, Enum):
    MILESTONE_ACCEPTED = "MILESTONE_ACCEPTED"
    WORK_SETTLED = "WORK_SETTLED"
    RESOURCE_DISPOSED = "RESOURCE_DISPOSED"
    NO_RESOURCE = "NO_RESOURCE"
    VERIFIED_CLEAN = "VERIFIED_CLEAN"
class ShutdownAttemptStatus(str, Enum):
    FAILED_CLOSED = "FAILED_CLOSED"
    CLOSED = "CLOSED"
class ProducerOutcome(str, Enum):
    MATERIALIZED = "MATERIALIZED"
    NO_RESOURCE = "NO_RESOURCE"
    PENDING = "PENDING"
class LogicalBarrierOutcome(str, Enum):
    CLEARED = "CLEARED"
    NOT_CLEARED = "NOT_CLEARED"
class PhysicalCloseOutcome(str, Enum):
    CLOSED = "CLOSED"
    STILL_OPEN = "STILL_OPEN"
class WorkerJoinOutcome(str, Enum):
    JOINED = "JOINED"
    STILL_ALIVE = "STILL_ALIVE"
class OwnerMilestoneOutcome(str, Enum):
    OWNER_SIGNALLED = "OWNER_SIGNALLED"
    PRE_TERMINAL_FINALITY = "PRE_TERMINAL_FINALITY"
    TERMINAL_SETTLED = "TERMINAL_SETTLED"
    PENDING = "PENDING"
class ArtifactCleanupOutcome(str, Enum):
    DISPOSED = "DISPOSED"
    RETAINED = "RETAINED"
    FAILED = "FAILED"
class BackendForceOutcome(str, Enum):
    FORCED = "FORCED"
    NOT_REQUIRED = "NOT_REQUIRED"
    FAILED = "FAILED"
class VerificationOutcome(str, Enum):
    CLEAN = "CLEAN"
    LIVE_RESOURCES = "LIVE_RESOURCES"
class NoResourceOutcome(str, Enum):
    NO_RESOURCE = "NO_RESOURCE"
class NotStartedOutcome(str, Enum):
    NOT_STARTED = "NOT_STARTED"


class ShutdownCandidateKind(str, Enum):
    PRODUCER = "PRODUCER"
    LOGICAL_BARRIER = "LOGICAL_BARRIER"
    PHYSICAL_CLOSE = "PHYSICAL_CLOSE"
    WORKER_JOIN = "WORKER_JOIN"
    OWNER_MILESTONE = "OWNER_MILESTONE"
    ARTIFACT_CLEANUP = "ARTIFACT_CLEANUP"
    BACKEND_FORCE = "BACKEND_FORCE"
    VERIFICATION = "VERIFICATION"
    NO_RESOURCE = "NO_RESOURCE"
    NOT_STARTED = "NOT_STARTED"


def _canonical_id(value: object, exact_type: type, owner: str, field: str):
    if type(value) is not exact_type:
        raise TypeError(f"{owner}.{field} must be an exact {exact_type.__name__}")
    raw = _field(value, "value", exact_type.__name__)
    return exact_type(_text(raw, exact_type.__name__, "value"))

def canonical_shutdown_identity(value: object) -> ShutdownObligationIdentity:
    owner = ShutdownObligationIdentity.__name__
    if type(value) is not ShutdownObligationIdentity:
        raise TypeError(f"{owner} must be exact")
    return ShutdownObligationIdentity(
        shutdown_epoch_id=_field(value, "shutdown_epoch_id", owner),
        shutdown_work_id=_field(value, "shutdown_work_id", owner),
        resource_kind=_field(value, "resource_kind", owner),
        creation_site=_field(value, "creation_site", owner),
        parent_work_id=_field(value, "parent_work_id", owner),
        hardware_id=_field(value, "hardware_id", owner),
        stream_generation=_field(value, "stream_generation", owner),
        run_id=_field(value, "run_id", owner),
        commit_attempt_id=_field(value, "commit_attempt_id", owner),
        owner_instance_id=_field(value, "owner_instance_id", owner),
        artifact_id=_field(value, "artifact_id", owner),
    )

@dataclass(frozen=True, slots=True)
class ShutdownObligationIdentity:
    shutdown_epoch_id: ShutdownEpochId
    shutdown_work_id: ShutdownWorkId
    resource_kind: ShutdownResourceKind
    creation_site: ShutdownCreationSite
    parent_work_id: ShutdownWorkId | None = None
    hardware_id: str | None = None
    stream_generation: int | None = None
    run_id: str | None = None
    commit_attempt_id: str | int | None = None
    owner_instance_id: OwnerInstanceId | None = None
    artifact_id: str | None = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        epoch = _canonical_id(_field(self, "shutdown_epoch_id", owner),
                              ShutdownEpochId, owner, "shutdown_epoch_id")
        work = _canonical_id(_field(self, "shutdown_work_id", owner),
                             ShutdownWorkId, owner, "shutdown_work_id")
        parent_value = _field(self, "parent_work_id", owner)
        parent = (
            None
            if parent_value is None
            else _canonical_id(parent_value, ShutdownWorkId, owner,
                               "parent_work_id")
        )
        if parent is not None and parent.value == work.value:
            raise ValueError(f"{owner}.parent_work_id cannot name itself")
        resource_kind = _exact_enum(_field(self, "resource_kind", owner),
                                    ShutdownResourceKind, owner,
                                    "resource_kind")
        creation_site = _exact_enum(_field(self, "creation_site", owner),
                                    ShutdownCreationSite, owner,
                                    "creation_site")
        hardware_id = _optional_text(_field(self, "hardware_id", owner),
                                     owner, "hardware_id")
        generation_value = _field(self, "stream_generation", owner)
        generation = (
            None
            if generation_value is None
            else _integer(generation_value, owner, "stream_generation")
        )
        run_id = _optional_text(_field(self, "run_id", owner),
                                owner, "run_id")
        commit_attempt_id = _optional_scalar_id(
            _field(self, "commit_attempt_id", owner), owner,
            "commit_attempt_id")
        owner_value = _field(self, "owner_instance_id", owner)
        if owner_value is not None:
            if type(owner_value) is not OwnerInstanceId:
                raise TypeError(
                    f"{owner}.owner_instance_id must be an exact OwnerInstanceId"
                )
            owner_value = canonical_owner_instance_id(owner_value)
        artifact_id = _optional_text(_field(self, "artifact_id", owner),
                                     owner, "artifact_id")
        object.__setattr__(self, "shutdown_epoch_id", epoch)
        object.__setattr__(self, "shutdown_work_id", work)
        object.__setattr__(self, "parent_work_id", parent)
        object.__setattr__(self, "resource_kind", resource_kind)
        object.__setattr__(self, "creation_site", creation_site)
        object.__setattr__(self, "hardware_id", hardware_id)
        object.__setattr__(self, "stream_generation", generation)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "commit_attempt_id", commit_attempt_id)
        object.__setattr__(self, "owner_instance_id", owner_value)
        object.__setattr__(self, "artifact_id", artifact_id)

class ShutdownReasonCode(str, Enum):
    DEADLINE_EXPIRED = "DEADLINE_EXPIRED"
    EXCEPTION = "EXCEPTION"
    MALFORMED_RESULT = "MALFORMED_RESULT"
    NO_RESOURCE = "NO_RESOURCE"
    NOT_STARTED = "NOT_STARTED"
    TIMED_OUT = "TIMED_OUT"
class ShutdownResultCode(str, Enum):
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CLOSE_FAILED = "CLOSE_FAILED"
    FORCE_FAILED = "FORCE_FAILED"
    OPEN_FAILED = "OPEN_FAILED"
    SETTLED = "SETTLED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
class ShutdownExceptionCategory(str, Enum):
    OS_ERROR = "OS_ERROR"
    OTHER_EXCEPTION = "OTHER_EXCEPTION"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TIMEOUT = "TIMEOUT"
    TYPE_ERROR = "TYPE_ERROR"
    VALUE_ERROR = "VALUE_ERROR"
class ShutdownIdentifierKind(str, Enum):
    ATTEMPT = "ATTEMPT"
    EPOCH = "EPOCH"
    HARDWARE = "HARDWARE"
    RESOURCE = "RESOURCE"
@dataclass(frozen=True, slots=True)
class ShutdownRedactedIdentifier:
    kind: ShutdownIdentifierKind
    ordinal: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _exact_enum(_field(self, "kind", owner), ShutdownIdentifierKind,
                    owner, "kind")
        _integer(_field(self, "ordinal", owner), owner, "ordinal")
ShutdownEvidenceScalar: TypeAlias = (
    ShutdownCandidateKind | ShutdownReasonCode | ShutdownResultCode
    | ShutdownExceptionCategory
    | ShutdownObservationKind | ShutdownRedactedIdentifier | int | bool
)
_EVIDENCE_KEY_TYPES = {
    "attempt_count": int,
    "candidate_kind": ShutdownCandidateKind,
    "exception_type": ShutdownExceptionCategory,
    "hardware_id": ShutdownRedactedIdentifier,
    "observation": ShutdownObservationKind,
    "reason_code": ShutdownReasonCode,
    "resource_id": ShutdownRedactedIdentifier,
    "result_code": ShutdownResultCode,
    "stream_generation": int,
    "target_count": int,
    "worker_alive": bool,
}
@dataclass(frozen=True, slots=True)
class ShutdownEvidenceItem:
    key: str
    value: ShutdownEvidenceScalar

    def __post_init__(self) -> None:
        owner = type(self).__name__
        key = _text(_field(self, "key", owner), owner, "key")
        if key not in _EVIDENCE_KEY_TYPES:
            raise ValueError(f"{owner}.key is not allowlisted")
        value = _field(self, "value", owner)
        exact_type = _EVIDENCE_KEY_TYPES[key]
        if type(value) is not exact_type:
            raise TypeError(f"{owner}.value has the wrong type for {key}")
        if type(value) is int:
            _integer(value, owner, "value")
        elif type(value) is ShutdownRedactedIdentifier:
            value.__post_init__()
            required = (
                ShutdownIdentifierKind.HARDWARE if key == "hardware_id"
                else ShutdownIdentifierKind.RESOURCE)
            if value.kind is not required:
                raise TypeError(f"{owner}.value has wrong identifier kind")

def _canonical_evidence(value: object, owner: str) -> tuple[ShutdownEvidenceItem, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{owner}.evidence must be a tuple")
    if len(value) > MAX_SHUTDOWN_EVIDENCE_ITEMS:
        raise ValueError(
            f"{owner}.evidence accepts at most "
            f"{MAX_SHUTDOWN_EVIDENCE_ITEMS} items"
        )
    copied: list[ShutdownEvidenceItem] = []
    for item in value:
        if type(item) is not ShutdownEvidenceItem:
            raise TypeError(f"{owner}.evidence items must be exact")
        copied.append(
            ShutdownEvidenceItem(
                _field(item, "key", ShutdownEvidenceItem.__name__),
                _field(item, "value", ShutdownEvidenceItem.__name__),
            )
        )
    return tuple(copied)

def _candidate_post_init(candidate: object, outcome_type: type[Enum]) -> None:
    owner = type(candidate).__name__
    identity = canonical_shutdown_identity(
        _field(candidate, "identity", owner)
    )
    attempt = _canonical_id(
        _field(candidate, "attempt_id", owner),
        ShutdownAttemptId,
        owner,
        "attempt_id",
    )
    outcome = _exact_enum(
        _field(candidate, "outcome", owner),
        outcome_type,
        owner,
        "outcome",
    )
    candidate_evidence = _canonical_evidence(
        _field(candidate, "evidence", owner), owner
    )
    object.__setattr__(candidate, "identity", identity)
    object.__setattr__(candidate, "attempt_id", attempt)
    object.__setattr__(candidate, "outcome", outcome)
    object.__setattr__(candidate, "evidence", candidate_evidence)

@dataclass(frozen=True, slots=True)
class _Candidate:
    identity: ShutdownObligationIdentity
    attempt_id: ShutdownAttemptId
    outcome: Enum
    evidence: tuple[ShutdownEvidenceItem, ...] = ()
    _outcome_type: ClassVar[type[Enum]]

    def __post_init__(self) -> None:
        _candidate_post_init(self, self._outcome_type)

@dataclass(frozen=True, slots=True)
class ProducerCandidate(_Candidate):
    materialized_resource_identity: ShutdownObligationIdentity | None = None
    _outcome_type = ProducerOutcome

    def __post_init__(self) -> None:
        _candidate_post_init(self, self._outcome_type)
        owner = type(self).__name__
        value = _field(self, "materialized_resource_identity", owner)
        outcome = _field(self, "outcome", owner)
        if outcome is ProducerOutcome.MATERIALIZED:
            if value is None:
                raise TypeError(
                    f"{owner}.materialized_resource_identity is required"
                )
            value = canonical_shutdown_identity(value)
        elif value is not None:
            raise ValueError(
                f"{owner}.materialized_resource_identity: only MATERIALIZED "
                "may name a resource"
            )
        object.__setattr__(self, "materialized_resource_identity", value)
@dataclass(frozen=True, slots=True)
class LogicalBarrierCandidate(_Candidate):
    _outcome_type = LogicalBarrierOutcome
@dataclass(frozen=True, slots=True)
class PhysicalCloseCandidate(_Candidate):
    _outcome_type = PhysicalCloseOutcome
@dataclass(frozen=True, slots=True)
class WorkerJoinCandidate(_Candidate):
    _outcome_type = WorkerJoinOutcome
@dataclass(frozen=True, slots=True)
class OwnerMilestoneCandidate(_Candidate):
    _outcome_type = OwnerMilestoneOutcome
@dataclass(frozen=True, slots=True)
class ArtifactCleanupCandidate(_Candidate):
    _outcome_type = ArtifactCleanupOutcome
@dataclass(frozen=True, slots=True)
class BackendForceCandidate(_Candidate):
    _outcome_type = BackendForceOutcome
@dataclass(frozen=True, slots=True)
class VerificationCandidate(_Candidate):
    _outcome_type = VerificationOutcome
@dataclass(frozen=True, slots=True)
class NoResourceCandidate(_Candidate):
    _outcome_type = NoResourceOutcome
@dataclass(frozen=True, slots=True)
class NotStartedCandidate(_Candidate):
    _outcome_type = NotStartedOutcome

@dataclass(frozen=True, slots=True)
class ShutdownObligationReceipt:
    identity: ShutdownObligationIdentity
    attempt_id: ShutdownAttemptId
    phase: ShutdownPhase
    accepted_outcome: ShutdownAcceptedOutcome
    evidence: tuple[ShutdownEvidenceItem, ...]
    issuer_proof: str

    def __post_init__(self) -> None:
        owner = type(self).__name__
        exact_identity = canonical_shutdown_identity(
            _field(self, "identity", owner))
        exact_attempt = _canonical_id(_field(self, "attempt_id", owner),
                                      ShutdownAttemptId, owner, "attempt_id")
        exact_phase = _exact_enum(_field(self, "phase", owner),
                                  ShutdownPhase, owner, "phase")
        exact_outcome = _exact_enum(_field(self, "accepted_outcome", owner),
                                    ShutdownAcceptedOutcome, owner,
                                    "accepted_outcome")
        exact_evidence = _canonical_evidence(
            _field(self, "evidence", owner), owner)
        _text(_field(self, "issuer_proof", owner), owner, "issuer_proof")
        object.__setattr__(self, "identity", exact_identity)
        object.__setattr__(self, "attempt_id", exact_attempt)
        object.__setattr__(self, "phase", exact_phase)
        object.__setattr__(self, "accepted_outcome", exact_outcome)
        object.__setattr__(self, "evidence", exact_evidence)

def _validated_completion_epoch(value: object, owner: str) -> ShutdownEpochId:
    epoch = _canonical_id(
        _field(value, "shutdown_epoch_id", owner),
        ShutdownEpochId, owner, "shutdown_epoch_id")
    _exact_enum(
        _field(value, "completed_phase", owner),
        ShutdownPhase, owner, "completed_phase")
    incomplete = _field(value, "first_incomplete_phase", owner)
    if incomplete is not None:
        _exact_enum(
            incomplete, ShutdownPhase, owner, "first_incomplete_phase")
    return epoch

@dataclass(frozen=True, slots=True)
class ShutdownAttemptResult:
    shutdown_epoch_id: ShutdownEpochId
    attempt_id: ShutdownAttemptId
    status: ShutdownAttemptStatus
    completed_phase: ShutdownPhase
    first_incomplete_phase: ShutdownPhase | None
    evidence: tuple[ShutdownEvidenceItem, ...]
    active_obligation_count: int
    registry_version: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        epoch = _validated_completion_epoch(self, owner)
        attempt = _canonical_id(
            _field(self, "attempt_id", owner),
            ShutdownAttemptId, owner, "attempt_id")
        status = _exact_enum(
            _field(self, "status", owner),
            ShutdownAttemptStatus, owner, "status")
        evidence = _canonical_evidence(
            _field(self, "evidence", owner), owner)
        active_count = _integer(
            _field(self, "active_obligation_count", owner),
            owner, "active_obligation_count")
        _integer(
            _field(self, "registry_version", owner),
            owner, "registry_version")
        completed = _field(self, "completed_phase", owner)
        incomplete = _field(self, "first_incomplete_phase", owner)
        coherent_closed = (
            status is ShutdownAttemptStatus.CLOSED
            and completed is ShutdownPhase.VERIFYING
            and incomplete is None
            and active_count == 0
        )
        coherent_failed = (
            status is ShutdownAttemptStatus.FAILED_CLOSED
            and incomplete is not None
            and completed is not ShutdownPhase.VERIFYING
            and incomplete.value == completed.value + 1
        )
        if not coherent_closed and not coherent_failed:
            raise ValueError(
                f"{owner} must describe a coherent completion state"
            )
        object.__setattr__(self, "shutdown_epoch_id", epoch)
        object.__setattr__(self, "attempt_id", attempt)
        object.__setattr__(self, "evidence", evidence)

@dataclass(frozen=True, slots=True)
class ShutdownCoordinatorSnapshot:
    shutdown_epoch_id: ShutdownRedactedIdentifier
    active_attempt_id: ShutdownRedactedIdentifier | None
    completed_phase: ShutdownPhase
    first_incomplete_phase: ShutdownPhase | None
    registry_version: int
    retry_count: int
    force_invoked: bool
    obligation_counts: tuple[tuple[ShutdownResourceKind, int], ...]
    state_counts: tuple[tuple[ShutdownLifetimeState, int], ...]
    accepted_receipt_count: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        epoch = _canonical_redacted_identifier(
            _field(self, "shutdown_epoch_id", owner),
            ShutdownIdentifierKind.EPOCH,
            owner,
            "shutdown_epoch_id",
        )
        _exact_enum(
            _field(self, "completed_phase", owner),
            ShutdownPhase,
            owner,
            "completed_phase",
        )
        incomplete = _field(self, "first_incomplete_phase", owner)
        if incomplete is not None:
            _exact_enum(
                incomplete,
                ShutdownPhase,
                owner,
                "first_incomplete_phase",
            )
        attempt_value = _field(self, "active_attempt_id", owner)
        attempt = None
        if attempt_value is not None:
            attempt = _canonical_redacted_identifier(
                attempt_value,
                ShutdownIdentifierKind.ATTEMPT,
                owner,
                "active_attempt_id",
            )
        for field_name in (
            "registry_version", "retry_count", "accepted_receipt_count"
        ):
            _integer(_field(self, field_name, owner), owner, field_name)
        if type(_field(self, "force_invoked", owner)) is not bool:
            raise TypeError(f"{owner}.force_invoked must be boolean")
        _validate_counts(
            _field(self, "obligation_counts", owner),
            ShutdownResourceKind, owner, "obligation_counts")
        _validate_counts(
            _field(self, "state_counts", owner),
            ShutdownLifetimeState, owner, "state_counts")
        object.__setattr__(self, "shutdown_epoch_id", epoch)
        object.__setattr__(self, "active_attempt_id", attempt)


def _canonical_redacted_identifier(
    value: object,
    required_kind: ShutdownIdentifierKind,
    owner: str,
    field: str,
) -> ShutdownRedactedIdentifier:
    if type(value) is not ShutdownRedactedIdentifier:
        raise TypeError(
            f"{owner}.{field} must be an exact ShutdownRedactedIdentifier"
        )
    kind = _field(value, "kind", ShutdownRedactedIdentifier.__name__)
    ordinal = _field(value, "ordinal", ShutdownRedactedIdentifier.__name__)
    if kind is not required_kind:
        raise TypeError(f"{owner}.{field} has the wrong identifier kind")
    return ShutdownRedactedIdentifier(kind, ordinal)

def _validate_counts(
    value: object, enum_type: type[Enum], owner: str, field: str
) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{owner}.{field} must be a tuple")
    if len(value) > MAX_SHUTDOWN_SNAPSHOT_ITEMS:
        raise ValueError(f"{owner}.{field} is unbounded")
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError(f"{owner}.{field} items must be pairs")
        _exact_enum(item[0], enum_type, owner, field)
        _integer(item[1], owner, field)
