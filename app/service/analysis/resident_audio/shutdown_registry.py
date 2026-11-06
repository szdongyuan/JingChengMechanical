"""Closed-world obligation registry for resident-audio shutdown."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import OrderedDict, deque
from dataclasses import dataclass

from .shutdown_schema import (
    ShutdownCandidateDisposition,
    ShutdownResourceRole,
    shutdown_candidate_phase_allowed,
    shutdown_resource_schema,
)
from .shutdown_types import (
    NoResourceCandidate,
    NotStartedCandidate,
    ProducerCandidate,
    ProducerOutcome,
    ShutdownAcceptedOutcome, ShutdownAttemptId, ShutdownCreationSite,
    ShutdownCandidateKind, ShutdownEpochId, ShutdownEvidenceItem,
    ShutdownExceptionCategory,
    ShutdownLifetimeState,
    ShutdownObservationKind, ShutdownObligationIdentity,
    ShutdownObligationReceipt, ShutdownPhase, ShutdownResourceKind,
    ShutdownWorkId,
    _canonical_evidence as _contract_evidence,
    _canonical_id as _contract_id,
    canonical_shutdown_identity,
)

MAX_ACTIVE_PARENT_OBLIGATIONS = 64
MAX_ACTIVE_EXECUTION_OBLIGATIONS = 64
MAX_RETAINED_DISCHARGED_OBLIGATIONS = 64
MAX_RETAINED_RECEIPTS = 64
MAX_RETAINED_OBSERVATIONS = 8

@dataclass(frozen=True, slots=True)
class ShutdownObservation:
    attempt_id: ShutdownAttemptId
    kind: ShutdownObservationKind
    evidence: tuple[ShutdownEvidenceItem, ...] = ()
@dataclass(frozen=True, slots=True)
class ShutdownDiagnosticIdentity:
    shutdown_epoch_id: ShutdownEpochId
    shutdown_work_id: ShutdownWorkId
    resource_kind: ShutdownResourceKind
    creation_site: ShutdownCreationSite
    parent_work_id: ShutdownWorkId | None
    hardware_id: str | None
    stream_generation: int | None
    run_id: str | None
    commit_attempt_id: str | int | None
    owner_coordinates: tuple[str | int, ...] | None
    artifact_id: str | None
@dataclass(frozen=True, slots=True)
class ShutdownObligationSnapshot:
    identity: ShutdownDiagnosticIdentity
    state: ShutdownLifetimeState
    expected_candidate_type: str
    bound: bool
    observations: tuple[ShutdownObservation, ...]
@dataclass(frozen=True, slots=True)
class ShutdownRegistrySnapshot:
    shutdown_epoch_id: ShutdownEpochId
    version: int
    active_obligations: tuple[ShutdownObligationSnapshot, ...]
    discharged_count: int
    accepted_receipt_count: int
    retained_discharged_count: int
    retained_receipt_count: int
    active_parent_count: int
    active_execution_count: int
    parent_capacity: int
    execution_capacity: int
@dataclass(slots=True)
class _Obligation:
    identity: ShutdownObligationIdentity
    expected_type: type
    state: ShutdownLifetimeState = ShutdownLifetimeState.REGISTERED
    bound_object: object | None = None
    observations: deque[ShutdownObservation] | None = None

    def __post_init__(self) -> None:
        self.observations = deque(maxlen=MAX_RETAINED_OBSERVATIONS)
@dataclass(frozen=True, slots=True)
class _DischargedProof:
    identity_digest: str
    work_id: str
    parent_work_id: str | None
    candidate_type: type
    candidate_outcome: object
    positive: bool
    progress: bool
    parent_disposition_proof: bool
    surviving_lineage_work_id: str | None = None
    surviving_lineage_identity_digest: str | None = None
def _exact_enum(value: object, enum_type: type, field: str):
    if type(value) is not enum_type or not any(
        value is member for member in enum_type
    ):
        raise TypeError(f"{field} must be an exact {enum_type.__name__}")
    return value
def _canonical_id(value: object, exact_type: type, field: str):
    return _contract_id(value, exact_type, "ShutdownRegistry", field)
def _canonical_evidence(value: object) -> tuple[ShutdownEvidenceItem, ...]:
    return _contract_evidence(value, "ShutdownRegistry")
def _authenticated_candidate(
    candidate: object, allowed_types: tuple[type, ...], schema,
    phase: ShutdownPhase, attempt: ShutdownAttemptId | None,
    identity: ShutdownObligationIdentity | None):
    candidate_type = type(candidate)
    if not any(candidate_type is item for item in allowed_types):
        raise TypeError("candidate type is not allowed")
    try:
        values = (
            object.__getattribute__(candidate, "identity"),
            object.__getattribute__(candidate, "attempt_id"),
            object.__getattribute__(candidate, "outcome"),
            object.__getattribute__(candidate, "evidence"),
        )
        if candidate_type is ProducerCandidate:
            exact = candidate_type(
                *values,
                materialized_resource_identity=object.__getattribute__(
                    candidate, "materialized_resource_identity"
                ),
            )
        else:
            exact = candidate_type(*values)
    except AttributeError as exc:
        raise TypeError("candidate is missing a required field") from exc
    exact_phase = _exact_enum(phase, ShutdownPhase, "phase")
    if identity is not None and attempt is not None and (
        exact.attempt_id.value != attempt.value
        or _identity_digest(exact.identity) != _identity_digest(identity)
    ):
        raise ValueError("candidate attempt or identity mismatch")
    for rule in schema.accepted_outcomes:
        candidate_type, outcome, accepted, disposition, candidate_kind = rule
        if type(exact) is candidate_type and exact.outcome is outcome:
            break
    else:
        raise ValueError("candidate outcome is not accepted by resource schema")
    if not shutdown_candidate_phase_allowed(
        schema, candidate_type, exact.outcome, exact_phase
    ):
        raise ValueError("phase is not allowed for resource kind")
    if any(item.key == "candidate_kind" for item in exact.evidence):
        raise ValueError("candidate_kind evidence is registry-owned")
    evidence = _canonical_evidence(exact.evidence + (
        ShutdownEvidenceItem("candidate_kind", candidate_kind),))
    return exact, exact_phase, accepted, disposition, evidence
def _identity_key(identity: ShutdownObligationIdentity) -> tuple:
    owner = identity.owner_instance_id
    owner_key = None if owner is None else (
        owner.pool_epoch, owner.hardware_id, owner.stream_generation,
        owner.run_id, owner.commit_attempt_id, owner.owner_capability,
        owner.instance_nonce)
    return (
        identity.shutdown_epoch_id.value, identity.shutdown_work_id.value,
        identity.resource_kind.value, identity.creation_site.value,
        None if identity.parent_work_id is None
        else identity.parent_work_id.value,
        identity.hardware_id, identity.stream_generation, identity.run_id,
        identity.commit_attempt_id, owner_key, identity.artifact_id)
def _identity_digest(identity: ShutdownObligationIdentity) -> str:
    encoded = repr(_identity_key(identity)).encode("utf-8", "strict")
    return hashlib.sha256(encoded).hexdigest()
def _receipt_key(receipt: ShutdownObligationReceipt) -> tuple:
    return (
        _identity_digest(receipt.identity), receipt.attempt_id.value,
        receipt.phase.value, receipt.accepted_outcome.value,
        tuple((item.key, item.value) for item in receipt.evidence))
def _diagnostic_identity(identity: ShutdownObligationIdentity) -> ShutdownDiagnosticIdentity:
    owner = identity.owner_instance_id
    coordinates = None if owner is None else (
        owner.pool_epoch, owner.hardware_id, owner.stream_generation,
        owner.run_id, owner.commit_attempt_id)
    return ShutdownDiagnosticIdentity(
        ShutdownEpochId(identity.shutdown_epoch_id.value),
        ShutdownWorkId(identity.shutdown_work_id.value),
        identity.resource_kind, identity.creation_site,
        None if identity.parent_work_id is None
        else ShutdownWorkId(identity.parent_work_id.value),
        identity.hardware_id, identity.stream_generation, identity.run_id,
        identity.commit_attempt_id, coordinates, identity.artifact_id)
class ShutdownObligationRegistry:
    """One epoch's monotonic obligation and receipt authority."""

    def __init__(self, epoch_id: ShutdownEpochId, *, issuer: object) -> None:
        self._epoch_id = _canonical_id(epoch_id, ShutdownEpochId, "epoch_id")
        if issuer is None:
            raise TypeError("issuer must be a coordinator-owned capability")
        self._issuer = issuer
        self._issuer_proof = secrets.token_urlsafe(32)
        self._records: dict[str, _Obligation | _DischargedProof] = {}
        self._discharged_order: OrderedDict[str, None] = OrderedDict()
        self._receipt_keys: OrderedDict[
            tuple, tuple[str | None, bool]] = OrderedDict()
        self._version = 0
        self._discharged_count = 0
        self._accepted_receipt_count = 0
        self._sealed_resource_subtrees: set[str] = set()
    @property
    def epoch_id(self) -> ShutdownEpochId:
        return ShutdownEpochId(self._epoch_id.value)
    @property
    def version(self) -> int:
        return self._version
    def _changed(self) -> None:
        self._version += 1
    def _record_for(self, identity: object):
        exact = canonical_shutdown_identity(identity)
        if exact.shutdown_epoch_id.value != self._epoch_id.value:
            raise ValueError("obligation belongs to another shutdown epoch")
        record = self._records.get(exact.shutdown_work_id.value)
        if record is None:
            raise ValueError("unknown or expired shutdown work ID")
        expected_digest = (
            _identity_digest(record.identity)
            if type(record) is _Obligation else record.identity_digest)
        if _identity_digest(exact) != expected_digest:
            raise ValueError("shutdown obligation identity mismatch")
        return exact, record
    def _active_for(self, identity: object) -> tuple[ShutdownObligationIdentity, _Obligation]:
        exact, record = self._record_for(identity)
        if type(record) is not _Obligation:
            raise RuntimeError("shutdown obligation is discharged")
        return exact, record
    def _require_issuer(self, issuer: object) -> None:
        if issuer is not self._issuer:
            raise PermissionError("receipt issuer capability rejected")
    def _parent_has_positive(self, work_id: str) -> bool:
        return any(
            type(item) is _DischargedProof and item.positive
            and item.parent_disposition_proof
            and item.parent_work_id == work_id
            for item in self._records.values())
    def _positive_proof_for_parent(
        self, work_id: str
    ) -> _DischargedProof | None:
        direct = self._records.get(work_id)
        if type(direct) is _DischargedProof and direct.positive:
            return direct
        for item in self._records.values():
            if (
                type(item) is _DischargedProof
                and item.positive
                and item.parent_disposition_proof
                and item.parent_work_id == work_id
            ):
                return item
        return None
    def _is_same_or_descendant(self, work_id: str, ancestor_id: str) -> bool:
        current_id: str | None = work_id
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            if current_id == ancestor_id:
                return True
            seen.add(current_id)
            record = self._records.get(current_id)
            if type(record) is _Obligation:
                parent = record.identity.parent_work_id
                current_id = None if parent is None else parent.value
            elif type(record) is _DischargedProof:
                current_id = record.parent_work_id
            else:
                current_id = None
        return False
    def _has_sealed_resource_ancestor(
        self, identity: ShutdownObligationIdentity
    ) -> bool:
        current = identity.parent_work_id
        current_id = None if current is None else current.value
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            if current_id in self._sealed_resource_subtrees:
                return True
            seen.add(current_id)
            record = self._records.get(current_id)
            if type(record) is _Obligation:
                parent = record.identity.parent_work_id
                current_id = None if parent is None else parent.value
            elif type(record) is _DischargedProof:
                current_id = record.parent_work_id
            else:
                current_id = None
        return False
    def _positive_proof_blocks_mutation(
        self, identity: ShutdownObligationIdentity
    ) -> bool:
        current_id: str | None = identity.shutdown_work_id.value
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            proof = self._positive_proof_for_parent(current_id)
            if proof is not None:
                survivor = proof.surviving_lineage_work_id
                if (
                    survivor is None
                    or not self._is_same_or_descendant(
                        identity.shutdown_work_id.value, survivor
                    )
                ):
                    return True
            record = self._records.get(current_id)
            if type(record) is _Obligation:
                parent = record.identity.parent_work_id
                current_id = None if parent is None else parent.value
            elif type(record) is _DischargedProof:
                current_id = record.parent_work_id
            else:
                current_id = None
        return False
    def _has_positive_ancestor_or_self(
        self, identity: ShutdownObligationIdentity
    ) -> bool:
        current_id: str | None = identity.shutdown_work_id.value
        seen: set[str] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            if self._positive_proof_for_parent(current_id) is not None:
                return True
            record = self._records.get(current_id)
            if type(record) is _Obligation:
                parent = record.identity.parent_work_id
                current_id = None if parent is None else parent.value
            elif type(record) is _DischargedProof:
                current_id = record.parent_work_id
            else:
                current_id = None
        return False
    def _has_live_descendant(
        self, work_id: str, *, permitted_lineage: str | None = None
    ) -> bool:
        for record in self._records.values():
            if type(record) is not _Obligation:
                continue
            if (
                permitted_lineage is not None
                and self._is_same_or_descendant(
                    record.identity.shutdown_work_id.value,
                    permitted_lineage,
                )
            ):
                continue
            parent = record.identity.parent_work_id
            ancestor_id = None if parent is None else parent.value
            while ancestor_id is not None:
                if ancestor_id == work_id:
                    return True
                record = self._records.get(ancestor_id)
                if type(record) is _Obligation:
                    parent = record.identity.parent_work_id
                    ancestor_id = None if parent is None else parent.value
                elif type(record) is _DischargedProof:
                    ancestor_id = record.parent_work_id
                else:
                    ancestor_id = None
        return False
    def _retains_positive(self, parent_id: str | None, positive: bool) -> bool:
        return positive and type(self._records.get(parent_id)) is _Obligation
    def _compact(self) -> None:
        for work_id in tuple(self._discharged_order):
            if len(self._discharged_order) <= (
                MAX_RETAINED_DISCHARGED_OBLIGATIONS
            ):
                break
            proof = self._records.get(work_id)
            if (type(proof) is _DischargedProof
                    and not self._retains_positive(
                        proof.parent_work_id,
                        proof.positive
                        and proof.parent_disposition_proof)):
                self._discharged_order.pop(work_id, None)
                self._records.pop(work_id, None)
        for key, proof_context in tuple(self._receipt_keys.items()):
            if len(self._receipt_keys) <= MAX_RETAINED_RECEIPTS:
                break
            parent_work_id, positive = proof_context
            if not self._retains_positive(parent_work_id, positive):
                self._receipt_keys.pop(key, None)
    def register(
        self, resource_kind: ShutdownResourceKind,
        creation_site: ShutdownCreationSite, expected_candidate_type: type, *,
        parent_identity: ShutdownObligationIdentity | None = None,
        hardware_id: str | None = None,
        stream_generation: int | None = None,
        run_id: str | None = None,
        commit_attempt_id: str | int | None = None,
        owner_instance_id=None,
        artifact_id: str | None = None,
    ) -> ShutdownObligationIdentity:
        schema = shutdown_resource_schema(resource_kind)
        _exact_enum(creation_site, ShutdownCreationSite, "creation_site")
        exceptional = (
            expected_candidate_type is NoResourceCandidate
            or expected_candidate_type is NotStartedCandidate)
        compatible = any(
            expected_candidate_type is item
            for item in schema.candidate_types)
        if exceptional or not compatible:
            raise TypeError("candidate is incompatible with resource kind")
        active = sum(
            type(item) is _Obligation
            and shutdown_resource_schema(item.identity.resource_kind).role
            is schema.role
            for item in self._records.values())
        parent_role = schema.role is ShutdownResourceRole.PARENT
        limit = (MAX_ACTIVE_PARENT_OBLIGATIONS if parent_role
                 else MAX_ACTIVE_EXECUTION_OBLIGATIONS)
        if active >= limit:
            label = "parent" if parent_role else "execution"
            raise RuntimeError(f"active {label} obligation limit reached")
        parent_work_id = None
        if parent_identity is not None:
            parent, _ = self._active_for(parent_identity)
            if (
                schema.role is ShutdownResourceRole.PARENT
                and (
                    parent.shutdown_work_id.value
                    in self._sealed_resource_subtrees
                    or self._has_sealed_resource_ancestor(parent)
                )
            ):
                raise RuntimeError(
                    "sealed resource subtree prevents late registration"
                )
            if (
                self._positive_proof_for_parent(
                    parent.shutdown_work_id.value
                ) is not None
                or self._has_positive_ancestor_or_self(parent)
            ):
                raise RuntimeError(
                    "positive child proof prevents descendant registration"
                )
            if not any(parent.resource_kind is item
                       for item in schema.allowed_parent_kinds):
                raise TypeError("unauthorized parent and child resource kinds")
            parent_work_id = parent.shutdown_work_id
        elif not schema.root_allowed:
            raise TypeError("resource kind requires an exact parent")
        while True:
            work_id = ShutdownWorkId.mint()
            if work_id.value not in self._records:
                break
        identity = ShutdownObligationIdentity(
            self._epoch_id, work_id, resource_kind, creation_site,
            parent_work_id, hardware_id, stream_generation, run_id,
            commit_attempt_id, owner_instance_id, artifact_id)
        retained = canonical_shutdown_identity(identity)
        self._records[work_id.value] = _Obligation(retained,
                                                    expected_candidate_type)
        self._changed()
        return canonical_shutdown_identity(retained)
    def register_child(
        self, parent_identity: ShutdownObligationIdentity,
        resource_kind: ShutdownResourceKind,
        creation_site: ShutdownCreationSite, expected_candidate_type: type,
        **identity_fields) -> ShutdownObligationIdentity:
        return self.register(
            resource_kind, creation_site, expected_candidate_type,
            parent_identity=parent_identity, **identity_fields)
    def state(self, identity: object) -> ShutdownLifetimeState:
        record = self._record_for(identity)[1]
        return (
            record.state if type(record) is _Obligation
            else ShutdownLifetimeState.DISCHARGED)
    def bind(self, identity: object, bound_object: object) -> None:
        exact, record = self._active_for(identity)
        if (
            shutdown_resource_schema(exact.resource_kind).role
            is ShutdownResourceRole.PARENT
            and self._has_sealed_resource_ancestor(exact)
        ):
            raise RuntimeError(
                "sealed resource subtree prevents late binding"
            )
        if self._positive_proof_blocks_mutation(exact):
            raise RuntimeError("positive child proof prevents descendant binding")
        if record.state is not ShutdownLifetimeState.REGISTERED:
            raise RuntimeError("only a registered obligation may be bound")
        if bound_object is None:
            raise TypeError("bound object cannot be None")
        record.bound_object = bound_object
        record.state = ShutdownLifetimeState.BOUND
        self._changed()
    def activate(self, identity: object) -> None:
        exact, record = self._active_for(identity)
        if (
            shutdown_resource_schema(exact.resource_kind).role
            is ShutdownResourceRole.PARENT
            and self._has_sealed_resource_ancestor(exact)
        ):
            raise RuntimeError(
                "sealed resource subtree prevents late activation"
            )
        if self._positive_proof_blocks_mutation(exact):
            raise RuntimeError(
                "positive child proof prevents descendant activation"
            )
        if record.state is not ShutdownLifetimeState.BOUND:
            raise RuntimeError("only a bound obligation may become active")
        record.state = ShutdownLifetimeState.ACTIVE
        self._changed()
    def _append_observation(
        self, record: _Obligation, attempt_id: ShutdownAttemptId,
        kind: ShutdownObservationKind,
        evidence: tuple[ShutdownEvidenceItem, ...] = (),
    ) -> None:
        assert record.observations is not None
        record.observations.append(ShutdownObservation(
            _canonical_id(attempt_id, ShutdownAttemptId, "attempt_id"),
            _exact_enum(kind, ShutdownObservationKind, "kind"),
            _canonical_evidence(evidence)))
        self._changed()
    def record_observation(
        self, identity: object, attempt_id: ShutdownAttemptId,
        kind: ShutdownObservationKind, *,
        evidence: tuple[ShutdownEvidenceItem, ...] = (),
        exception_type: ShutdownExceptionCategory | None = None,
    ) -> None:
        _, record = self._active_for(identity)
        if record.state is not ShutdownLifetimeState.ACTIVE:
            raise RuntimeError("observations require an active obligation")
        exact_attempt = _canonical_id(attempt_id, ShutdownAttemptId,
                                      "attempt_id")
        exact_evidence = _canonical_evidence(evidence)
        if exception_type is not None:
            exact_evidence = _canonical_evidence(exact_evidence + (
                ShutdownEvidenceItem("exception_type", exception_type),))
        self._append_observation(record, exact_attempt, kind, exact_evidence)
    def latest_observation(self, identity: object) -> ShutdownObservation | None:
        record = self._active_for(identity)[1]
        assert record.observations is not None
        if not record.observations:
            return None
        item = record.observations[-1]
        return ShutdownObservation(
            ShutdownAttemptId(item.attempt_id.value), item.kind,
            tuple(ShutdownEvidenceItem(value.key, value.value)
                  for value in item.evidence))
    def _mint_receipt(
        self, record: _Obligation, attempt_id: ShutdownAttemptId,
        phase: ShutdownPhase, accepted_outcome: ShutdownAcceptedOutcome,
        evidence: tuple[ShutdownEvidenceItem, ...],
        candidate_type: type, *, positive: bool,
        candidate_outcome: object,
        progress: bool = False,
        parent_disposition_proof: bool = False,
        surviving_lineage_work_id: str | None = None,
        surviving_lineage_identity_digest: str | None = None,
    ) -> ShutdownObligationReceipt:
        receipt = ShutdownObligationReceipt(
            record.identity, attempt_id, phase, accepted_outcome,
            evidence, self._issuer_proof)
        key = _receipt_key(receipt)
        parent_id = (None if record.identity.parent_work_id is None
                     else record.identity.parent_work_id.value)
        self._receipt_keys[key] = (
            parent_id, positive and parent_disposition_proof
        )
        proof = _DischargedProof(
            _identity_digest(record.identity), record.identity.shutdown_work_id.value,
            parent_id,
            candidate_type, candidate_outcome, positive, progress,
            parent_disposition_proof, surviving_lineage_work_id,
            surviving_lineage_identity_digest)
        self._records[proof.work_id] = proof
        self._discharged_order[proof.work_id] = None
        self._discharged_count += 1
        self._accepted_receipt_count += 1
        self._changed()
        self._compact()
        return receipt
    def accept_candidate(
        self, identity: object, candidate: object, *,
        attempt_id: ShutdownAttemptId, phase: ShutdownPhase,
        issuer: object) -> ShutdownObligationReceipt:
        self._require_issuer(issuer)
        trusted_attempt = _canonical_id(attempt_id, ShutdownAttemptId,
                                        "attempt_id")
        _, record = self._active_for(identity)
        schema = shutdown_resource_schema(record.identity.resource_kind)
        if schema.role is not ShutdownResourceRole.WORK:
            raise RuntimeError("resource requires distinct child work receipt")
        try:
            exact, exact_phase, accepted, disposition, retained_evidence = (
                _authenticated_candidate(
                    candidate,
                    (record.expected_type, NotStartedCandidate),
                    schema, phase, trusted_attempt, record.identity)
            )
        except (TypeError, ValueError):
            self._append_observation(record, trusted_attempt,
                                     ShutdownObservationKind.MALFORMED_RESULT)
            raise
        not_started = type(exact) is NotStartedCandidate
        required_state = (ShutdownLifetimeState.REGISTERED if not_started
                          else ShutdownLifetimeState.ACTIVE)
        if record.state is not required_state:
            raise RuntimeError(
                "not-started requires never-bound work"
                if not_started else "candidate requires active work")
        positive = disposition is ShutdownCandidateDisposition.POSITIVE
        progress = disposition is ShutdownCandidateDisposition.PROGRESS
        surviving_lineage_work_id = None
        surviving_lineage_identity_digest = None
        if record.identity.parent_work_id is not None:
            parent_id = record.identity.parent_work_id.value
            parent = self._records.get(parent_id)
            if type(parent) is not _Obligation:
                raise RuntimeError("registered parent is unavailable")
            if (
                type(exact) is ProducerCandidate
                and exact.outcome is ProducerOutcome.MATERIALIZED
            ):
                materialized = exact.materialized_resource_identity
                assert materialized is not None
                try:
                    materialized_exact, materialized_record = (
                        self._active_for(materialized)
                    )
                except (TypeError, ValueError, RuntimeError) as exc:
                    raise ValueError(
                        "materialized resource is not registered and active"
                    ) from exc
                parent_schema = shutdown_resource_schema(
                    parent.identity.resource_kind
                )
                if (
                    materialized_exact.parent_work_id is None
                    or materialized_exact.parent_work_id.value != parent_id
                    or materialized_exact.resource_kind
                    not in parent_schema.materialized_resource_kinds
                ):
                    raise ValueError(
                        "materialized resource has the wrong producer lineage"
                    )
                if (
                    materialized_record.state
                    is not ShutdownLifetimeState.ACTIVE
                ):
                    raise RuntimeError(
                        "materialized resource must be ACTIVE"
                    )
                surviving_lineage_work_id = (
                    materialized_exact.shutdown_work_id.value
                )
                surviving_lineage_identity_digest = _identity_digest(
                    materialized_exact
                )
            if (
                (positive or progress)
                and parent.state is not ShutdownLifetimeState.ACTIVE
            ):
                raise RuntimeError(
                    "positive child requires exact ACTIVE parent"
                )
            if (
                positive and self._parent_has_positive(parent_id)
            ):
                raise RuntimeError("parent already has positive child proof")
            kind = (ShutdownObservationKind.POSITIVE_CANDIDATE
                    if positive or progress
                    else ShutdownObservationKind.TYPED_NEGATIVE)
            self._append_observation(parent, trusted_attempt, kind,
                                     retained_evidence)
        self._append_observation(record, trusted_attempt,
                                 ShutdownObservationKind.ACCEPTED,
                                 retained_evidence)
        return self._mint_receipt(
            record, trusted_attempt, exact_phase, accepted, retained_evidence,
            type(exact), positive=positive,
            candidate_outcome=exact.outcome,
            progress=progress,
            parent_disposition_proof=positive,
            surviving_lineage_work_id=surviving_lineage_work_id,
            surviving_lineage_identity_digest=(
                surviving_lineage_identity_digest
            ))
    def accept_absence(
        self, identity: object, candidate: object, *,
        attempt_id: ShutdownAttemptId, phase: ShutdownPhase,
        issuer: object) -> ShutdownObligationReceipt:
        self._require_issuer(issuer)
        trusted_attempt = _canonical_id(attempt_id, ShutdownAttemptId,
                                        "attempt_id")
        exact_identity, record = self._record_for(identity)
        schema = shutdown_resource_schema(exact_identity.resource_kind)
        if schema.role is not ShutdownResourceRole.PARENT:
            raise RuntimeError("absence is only valid for parent resources")
        exact, exact_phase, accepted, _, retained_evidence = (
            _authenticated_candidate(
                candidate, (NoResourceCandidate,), schema, phase,
                trusted_attempt, exact_identity)
        )
        if type(record) is _DischargedProof:
            receipt = ShutdownObligationReceipt(
                exact_identity, trusted_attempt, exact_phase, accepted,
                retained_evidence, self._issuer_proof)
            if _receipt_key(receipt) not in self._receipt_keys:
                raise RuntimeError("absence receipt is outside proof window")
            return receipt
        work_id = exact_identity.shutdown_work_id.value
        if self._parent_has_positive(work_id):
            raise RuntimeError("absence contradicts positive child proof")
        if self._has_live_descendant(work_id):
            raise RuntimeError("absence requires no live descendant")
        if (record.state is not ShutdownLifetimeState.REGISTERED
                or record.bound_object is not None):
            raise RuntimeError(
                "absence requires never-materialized registered resource")
        self._append_observation(record, trusted_attempt,
                                 ShutdownObservationKind.ACCEPTED,
                                 retained_evidence)
        return self._mint_receipt(
            record, trusted_attempt, exact_phase, accepted, retained_evidence,
            type(exact), positive=True, candidate_outcome=exact.outcome)

    def accept_parent_milestone(
        self, parent_identity: object, candidate: object, *,
        phase: ShutdownPhase, issuer: object,
    ) -> ShutdownObligationReceipt:
        self._require_issuer(issuer)
        _, parent = self._active_for(parent_identity)
        if parent.state is not ShutdownLifetimeState.ACTIVE:
            raise RuntimeError("parent milestone requires active resource")
        schema = shutdown_resource_schema(parent.identity.resource_kind)
        if schema.role is not ShutdownResourceRole.PARENT:
            raise RuntimeError("work obligations have no parent milestone")
        exact, exact_phase, outcome, disposition, retained_evidence = (
            _authenticated_candidate(
                candidate, (parent.expected_type,), schema, phase, None, None
            )
        )
        if (
            outcome is not ShutdownAcceptedOutcome.MILESTONE_ACCEPTED
            or disposition is not ShutdownCandidateDisposition.PROGRESS
        ):
            raise ValueError("candidate is not a parent milestone")
        if not any(
            path.child_resource_kind is exact.identity.resource_kind
            and path.candidate_type is type(exact)
            for path in schema.disposition_paths
        ):
            raise ValueError("milestone uses unauthorized disposition path")
        child = self._records.get(exact.identity.shutdown_work_id.value)
        if type(child) is not _DischargedProof:
            raise ValueError("milestone child work is not accepted")
        parent_id = parent.identity.shutdown_work_id.value
        if child.parent_work_id != parent_id:
            raise ValueError("milestone belongs to another parent")
        if (
            child.candidate_type is not type(exact)
            or child.candidate_outcome is not exact.outcome
            or child.identity_digest != _identity_digest(exact.identity)
            or not child.progress
        ):
            raise ValueError("candidate is not exact milestone proof")
        child_key = (
            _identity_digest(exact.identity), exact.attempt_id.value,
            exact_phase.value, ShutdownAcceptedOutcome.WORK_SETTLED.value,
            tuple((item.key, item.value) for item in retained_evidence))
        if child_key not in self._receipt_keys:
            raise ValueError("milestone child receipt is outside proof window")
        receipt = ShutdownObligationReceipt(
            parent.identity, exact.attempt_id, exact_phase, outcome,
            retained_evidence, self._issuer_proof)
        key = _receipt_key(receipt)
        if key not in self._receipt_keys:
            parent_parent = parent.identity.parent_work_id
            self._receipt_keys[key] = (
                None if parent_parent is None else parent_parent.value,
                False,
            )
            self._accepted_receipt_count += 1
            if schema.seals_resource_subtree:
                self._sealed_resource_subtrees.add(parent_id)
            self._append_observation(
                parent, exact.attempt_id,
                ShutdownObservationKind.ACCEPTED, retained_evidence)
            self._compact()
        return receipt
    def discharge_parent(
        self, parent_identity: object, candidate: object, *,
        phase: ShutdownPhase, issuer: object) -> ShutdownObligationReceipt:
        self._require_issuer(issuer)
        _, parent = self._active_for(parent_identity)
        if parent.state is not ShutdownLifetimeState.ACTIVE:
            raise RuntimeError("parent disposition requires active resource")
        schema = shutdown_resource_schema(parent.identity.resource_kind)
        if schema.role is not ShutdownResourceRole.PARENT:
            raise RuntimeError("work obligations use accept_candidate")
        exact, exact_phase, outcome, disposition, retained_evidence = (
            _authenticated_candidate(
                candidate, (parent.expected_type,), schema, phase,
                None, None)
        )
        if (
            outcome is ShutdownAcceptedOutcome.MILESTONE_ACCEPTED
            or disposition is ShutdownCandidateDisposition.PROGRESS
        ):
            raise RuntimeError(
                "milestone proof cannot discharge parent resource"
            )
        if not any(
            path.child_resource_kind is exact.identity.resource_kind
            and path.candidate_type is type(exact)
            for path in schema.disposition_paths
        ):
            raise ValueError("candidate uses unauthorized disposition path")
        child = self._records.get(exact.identity.shutdown_work_id.value)
        if type(child) is not _DischargedProof:
            raise ValueError("candidate child work is not accepted")
        parent_id = parent.identity.shutdown_work_id.value
        if child.parent_work_id != parent_id:
            raise ValueError("candidate belongs to another parent")
        if (child.candidate_type is not type(exact)
                or child.candidate_outcome is not exact.outcome
                or child.identity_digest != _identity_digest(exact.identity)
                or not child.positive):
            raise ValueError("candidate is not exact positive child proof")
        if type(exact) is ProducerCandidate:
            materialized = exact.materialized_resource_identity
            materialized_digest = (
                None if materialized is None
                else _identity_digest(materialized)
            )
            if (
                child.surviving_lineage_work_id != (
                    None if materialized is None
                    else materialized.shutdown_work_id.value
                )
                or child.surviving_lineage_identity_digest
                != materialized_digest
            ):
                raise ValueError(
                    "candidate altered the authenticated materialized resource"
                )
        permitted_lineage = child.surviving_lineage_work_id
        if self._has_live_descendant(
            parent_id, permitted_lineage=permitted_lineage
        ):
            raise RuntimeError("parent disposition requires no live descendant")
        child_key = (
            _identity_digest(exact.identity), exact.attempt_id.value,
            exact_phase.value, ShutdownAcceptedOutcome.WORK_SETTLED.value,
            tuple((item.key, item.value) for item in retained_evidence))
        if child_key not in self._receipt_keys:
            raise ValueError("candidate child receipt is outside proof window")
        self._append_observation(parent, exact.attempt_id,
                                 ShutdownObservationKind.ACCEPTED,
                                 retained_evidence)
        return self._mint_receipt(
            parent, exact.attempt_id, exact_phase, outcome, retained_evidence,
            type(exact), positive=True,
            candidate_outcome=exact.outcome,
            surviving_lineage_work_id=permitted_lineage)
    def validates_receipt(self, receipt: object) -> bool:
        if type(receipt) is not ShutdownObligationReceipt:
            return False
        try:
            exact = ShutdownObligationReceipt(
                object.__getattribute__(receipt, "identity"),
                object.__getattribute__(receipt, "attempt_id"),
                object.__getattribute__(receipt, "phase"),
                object.__getattribute__(receipt, "accepted_outcome"),
                object.__getattribute__(receipt, "evidence"),
                object.__getattribute__(receipt, "issuer_proof"))
        except (AttributeError, TypeError, ValueError):
            return False
        schema = shutdown_resource_schema(exact.identity.resource_kind)
        if not any(exact.phase is phase for phase in schema.phases):
            return False
        candidate_kinds = tuple(
            item.value for item in exact.evidence
            if item.key == "candidate_kind")
        if len(candidate_kinds) != 1:
            return False
        if not any(
            exact.accepted_outcome is mapping[2]
            and candidate_kinds[0] is mapping[4]
            for mapping in schema.accepted_outcomes
        ):
            return False
        if not hmac.compare_digest(exact.issuer_proof, self._issuer_proof):
            return False
        return _receipt_key(exact) in self._receipt_keys
    def snapshot(self) -> ShutdownRegistrySnapshot:
        active = []
        parent_count = execution_count = 0
        for record in self._records.values():
            if type(record) is not _Obligation:
                continue
            if shutdown_resource_schema(
                record.identity.resource_kind
            ).role is ShutdownResourceRole.PARENT:
                parent_count += 1
            else:
                execution_count += 1
            assert record.observations is not None
            active.append(ShutdownObligationSnapshot(
                _diagnostic_identity(record.identity), record.state,
                record.expected_type.__name__, record.bound_object is not None,
                tuple(ShutdownObservation(
                    ShutdownAttemptId(item.attempt_id.value), item.kind,
                    tuple(ShutdownEvidenceItem(value.key, value.value)
                          for value in item.evidence))
                      for item in record.observations)))
        return ShutdownRegistrySnapshot(
            ShutdownEpochId(self._epoch_id.value), self._version,
            tuple(active), self._discharged_count,
            self._accepted_receipt_count, len(self._discharged_order),
            len(self._receipt_keys), parent_count, execution_count,
            MAX_ACTIVE_PARENT_OBLIGATIONS,
            MAX_ACTIVE_EXECUTION_OBLIGATIONS)
