"""Value state and narrow ports for retry-safe owner settlement."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol

from . import owner_finality_canonical as canonical
from .artifact_settlement import (
    ArtifactSettlementReceipt,
    CompletionInvocationResult,
)
from .owner_identity import (
    FinalityAckReceipt,
    FinalityPruneReceipt,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerRemovalReceipt,
    StreamInstanceId,
)


class SettlementIntent(str, Enum):
    PREPARE_ROLLBACK = "PREPARE_ROLLBACK"
    CLIENT_CANCEL = "CLIENT_CANCEL"
    OWNER_SUCCESS = "OWNER_SUCCESS"
    OWNER_FAILURE = "OWNER_FAILURE"
    STREAM_FAILURE = "STREAM_FAILURE"
    POOL_SHUTDOWN = "POOL_SHUTDOWN"


class SettlementPhase(str, Enum):
    CLAIMED = "CLAIMED"
    REMOVAL = "REMOVAL"
    OWNER_SIGNAL = "OWNER_SIGNAL"
    OWNER_JOIN = "OWNER_JOIN"
    ARTIFACT_READINESS = "ARTIFACT_READINESS"
    LATE_COMMANDS = "LATE_COMMANDS"
    REMOVE_DISABLE = "REMOVE_DISABLE"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    PRUNE = "PRUNE"
    ARTIFACT_SETTLEMENT = "ARTIFACT_SETTLEMENT"
    QUOTA = "QUOTA"
    TERMINAL = "TERMINAL"
    REFERENCE_CLEAR = "REFERENCE_CLEAR"
    SETTLED = "SETTLED"


@dataclass(frozen=True, slots=True)
class ArtifactSettlementResult:
    publication_resolved: bool
    artifact_disposition_confirmed: bool
    receipt: ArtifactSettlementReceipt | None = None
    terminal_state: str | None = None
    error_code: str | None = None
    failure_reason: str | None = None
    exception_type: str | None = None


@dataclass(frozen=True, slots=True)
class SettlementProgress:
    removal_receipt: OwnerRemovalReceipt | None = None
    non_admission_confirmed: bool = False
    owner_signaled: bool = False
    owner_joined: bool = False
    artifact_ready_checked: bool = False
    late_owner_commands_resolved: bool = False
    late_owner_commands_not_applicable: bool = False
    remove_retry_disabled: bool = False
    finality_not_applicable: bool = False
    finality_acknowledged: bool = False
    finality_ack_receipt: FinalityAckReceipt | None = None
    finality_pruned: bool = False
    publication_resolved: bool = False
    artifact_disposition_confirmed: bool = False
    quota_outcome_confirmed: bool = False
    handler_invoked: bool = False
    handler_completed: bool = False
    handler_returned: bool = False
    handler_resolution: str | None = None
    terminal_state_override: str | None = None
    error_code_override: str | None = None
    failure_reason_override: str | None = None
    handler_exception_type: str | None = None
    terminalized: bool = False
    references_cleared: bool = False
    phase: SettlementPhase = SettlementPhase.CLAIMED
    removal_attempts: int = 0
    owner_join_attempts: int = 0
    artifact_disposition_attempts: int = 0
    publication_attempts: int = 0


@dataclass(frozen=True, slots=True)
class SettlementOwnerDiagnosticId:
    pool_epoch: str | int
    hardware_id: str
    stream_generation: int
    run_id: str
    commit_attempt_id: str | int
    instance_nonce: str


@dataclass(frozen=True, slots=True)
class SettlementTerminalSummary:
    owner_instance_id: SettlementOwnerDiagnosticId
    artifact_id: str | None
    intent: SettlementIntent
    terminal_state: str
    error_code: str | None
    failure_reason: str | None
    exception_type: str | None
    removal_outcome: str | None
    non_admission_confirmed: bool
    finality_pruned: bool


@dataclass(frozen=True, slots=True)
class SettlementClaimDiagnostic:
    pool_epoch: str | int
    hardware_id: str
    stream_generation: int
    run_id: str
    commit_attempt_id: str | int
    instance_nonce: str
    intent: SettlementIntent
    phase: SettlementPhase
    terminal_state: str
    error_code: str | None
    removal_outcome: str | None
    settled: bool


@dataclass(frozen=True, slots=True)
class SettlementDiagnostics:
    claims_by_phase: tuple[tuple[SettlementPhase, int], ...]
    unsettled_leases: int
    removal_retries: int
    owner_join_retries: int
    artifact_disposition_retries: int
    publication_retries: int
    settlement_backpressure_rejections: int
    claims: tuple[SettlementClaimDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class SettlementClaim:
    lease_id: str
    intent: SettlementIntent
    owner_instance_id: OwnerInstanceId
    stream_instance_id: StreamInstanceId
    terminal_state: str
    error_code: str | None = None
    failure_reason: str | None = None
    non_admission_proof: OwnerNonAdmissionProof | None = None
    progress: SettlementProgress = field(default_factory=SettlementProgress)
    revision: int = 0
    owner: object | None = None
    writer: object | None = None
    stream: object | None = None
    artifact: object | None = None
    ring: object | None = None
    callback: object | None = None
    completion_handler: object | None = None
    terminal_summary: SettlementTerminalSummary | None = None


@dataclass(frozen=True, slots=True)
class SettlementAdvanceResult:
    lease_id: str
    intent: SettlementIntent
    phase: SettlementPhase
    progress: SettlementProgress
    claimed: bool
    progressed: bool
    settled: bool


def canonical_settlement_candidate(
    candidate: object,
) -> SettlementClaim | None:
    """Validate and detach all value identity accepted at claim admission."""

    if not isinstance(candidate, SettlementClaim):
        return None
    if (
        type(candidate.lease_id) is not str
        or not candidate.lease_id
        or len(candidate.lease_id) > 256
        or not isinstance(candidate.intent, SettlementIntent)
        or candidate.terminal_state
        not in ("RELEASED", "FAILED", "CANCELED")
        or candidate.revision != 0
        or candidate.progress != SettlementProgress()
    ):
        return None
    try:
        owner_id = canonical.canonical_owner_instance_id(
            candidate.owner_instance_id
        )
        stream_id = canonical.canonical_stream_instance_id(
            candidate.stream_instance_id
        )
    except (TypeError, ValueError):
        return None
    if (
        owner_id.pool_epoch != stream_id.pool_epoch
        or owner_id.hardware_id != stream_id.hardware_id
        or owner_id.stream_generation != stream_id.stream_generation
    ):
        return None
    for label in (candidate.error_code, candidate.failure_reason):
        if label is not None and (
            type(label) is not str or not label or len(label) > 256
        ):
            return None
    proof = candidate.non_admission_proof
    if proof is not None and not proof.confirms(owner_id):
        return None
    return replace(
        candidate,
        owner_instance_id=owner_id,
        stream_instance_id=stream_id,
        non_admission_proof=(
            None
            if proof is None
            else canonical.canonical_non_admission_proof(proof)
        ),
    )


def exact_settlement_removal(
    receipt: object, claim: SettlementClaim
) -> OwnerRemovalReceipt | None:
    try:
        receipt = canonical.canonical_owner_removal_receipt(receipt)
    except (TypeError, ValueError):
        return None
    return receipt if receipt.confirms(claim.owner_instance_id) else None


def exact_settlement_ack(
    receipt: object, claim: SettlementClaim
) -> FinalityAckReceipt | None:
    try:
        receipt = canonical.canonical_finality_ack_receipt(receipt)
    except (TypeError, ValueError):
        return None
    if (
        receipt.owner_instance_id != claim.owner_instance_id
        or receipt.stream_instance_id != claim.stream_instance_id
    ):
        return None
    return receipt


def settlement_prune_confirms(
    receipt: object, claim: SettlementClaim
) -> bool:
    try:
        receipt = canonical.canonical_finality_prune_receipt(receipt)
    except (TypeError, ValueError):
        return False
    return receipt.confirms(claim.owner_instance_id)


def authoritative_artifact_result(
    claim: SettlementClaim,
    result: ArtifactSettlementResult,
) -> bool:
    if (
        type(result.publication_resolved) is not bool
        or type(result.artifact_disposition_confirmed) is not bool
    ):
        return False
    receipt = result.receipt
    artifact_id = (
        getattr(claim.artifact, "artifact_id", None)
        or getattr(claim.writer, "artifact_id", None)
    )
    if artifact_id is not None and receipt is None:
        return False
    if receipt is not None:
        if (
            receipt.artifact_id != artifact_id
            or receipt.publication_resolved
            is not result.publication_resolved
            or receipt.disposition_confirmed
            is not result.artifact_disposition_confirmed
        ):
            return False
    if result.terminal_state is not None and result.terminal_state not in (
        "RELEASED",
        "FAILED",
        "CANCELED",
    ):
        return False
    return all(
        label is None
        or (
            type(label) is str
            and bool(label)
            and len(label) <= 256
        )
        for label in (
            result.error_code,
            result.failure_reason,
            result.exception_type,
        )
    )


def authoritative_completion_invocation(
    result: CompletionInvocationResult,
) -> bool:
    if (
        type(result.handler_returned) is not bool
        or result.resolution not in (None, "CLAIMED", "DECLINED")
    ):
        return False
    if result.handler_returned:
        if result.exception_type is not None:
            return False
    elif result.exception_type is None:
        return False
    return (
        result.exception_type is None
        or (
            type(result.exception_type) is str
            and bool(result.exception_type)
            and len(result.exception_type) <= 256
        )
    )


def progress_after_artifact_result(
    progress: SettlementProgress,
    attempted: SettlementProgress,
    result: ArtifactSettlementResult,
    *,
    handler_started: bool,
) -> tuple[SettlementProgress, bool]:
    publication = (
        progress.publication_resolved
        or result.publication_resolved is True
    )
    disposition = (
        progress.artifact_disposition_confirmed
        or result.artifact_disposition_confirmed is True
    )
    updated = replace(
        attempted,
        publication_resolved=publication,
        artifact_disposition_confirmed=disposition,
        terminal_state_override=(
            result.terminal_state or progress.terminal_state_override
        ),
        error_code_override=(
            result.error_code
            if result.terminal_state is not None
            else progress.error_code_override
        ),
        failure_reason_override=(
            result.failure_reason or progress.failure_reason_override
        ),
        handler_exception_type=(
            result.exception_type or progress.handler_exception_type
        ),
        phase=(
            SettlementPhase.QUOTA
            if publication and disposition
            else SettlementPhase.ARTIFACT_SETTLEMENT
        ),
    )
    changed = handler_started or any(
        (
            publication != progress.publication_resolved,
            disposition != progress.artifact_disposition_confirmed,
            result.terminal_state != progress.terminal_state_override,
            result.error_code != progress.error_code_override,
            result.failure_reason != progress.failure_reason_override,
            result.exception_type != progress.handler_exception_type,
        )
    )
    return updated, changed


def terminal_summary_for(
    claim: SettlementClaim,
) -> SettlementTerminalSummary:
    """Build the value-only record retained after references are cleared."""

    receipt = claim.progress.removal_receipt
    identity = claim.owner_instance_id
    terminal_state, error_code = effective_terminal_state_and_error(claim)
    artifact_id = getattr(claim.artifact, "artifact_id", None)
    if artifact_id is None:
        artifact_id = getattr(claim.writer, "artifact_id", None)
    return SettlementTerminalSummary(
        owner_instance_id=SettlementOwnerDiagnosticId(
            pool_epoch=identity.pool_epoch,
            hardware_id=identity.hardware_id,
            stream_generation=identity.stream_generation,
            run_id=identity.run_id,
            commit_attempt_id=identity.commit_attempt_id,
            instance_nonce=identity.instance_nonce,
        ),
        artifact_id=artifact_id,
        intent=claim.intent,
        terminal_state=terminal_state,
        error_code=error_code,
        failure_reason=(
            claim.progress.failure_reason_override
            if claim.progress.failure_reason_override is not None
            else claim.failure_reason
        ),
        exception_type=claim.progress.handler_exception_type,
        removal_outcome=None if receipt is None else receipt.outcome.value,
        non_admission_confirmed=claim.progress.non_admission_confirmed,
        finality_pruned=claim.progress.finality_pruned,
    )


def effective_terminal_state_and_error(
    claim: SettlementClaim,
) -> tuple[str, str | None]:
    override = claim.progress.terminal_state_override
    return (
        override or claim.terminal_state,
        (
            claim.progress.error_code_override
            if override is not None
            else claim.error_code
        ),
    )


def claim_diagnostic_for(
    claim: SettlementClaim,
) -> SettlementClaimDiagnostic:
    """Select only bounded, non-capability fields for public diagnostics."""

    identity = claim.owner_instance_id
    receipt = claim.progress.removal_receipt
    terminal_state, error_code = effective_terminal_state_and_error(claim)
    return SettlementClaimDiagnostic(
        pool_epoch=identity.pool_epoch,
        hardware_id=identity.hardware_id,
        stream_generation=identity.stream_generation,
        run_id=identity.run_id,
        commit_attempt_id=identity.commit_attempt_id,
        instance_nonce=identity.instance_nonce,
        intent=claim.intent,
        phase=claim.progress.phase,
        terminal_state=terminal_state,
        error_code=error_code,
        removal_outcome=None if receipt is None else receipt.outcome.value,
        settled=(
            claim.progress.terminalized
            and claim.progress.references_cleared
        ),
    )


class SettlementClaimPort(Protocol):
    def claim_or_resume(
        self, candidate: SettlementClaim
    ) -> tuple[SettlementClaim, bool]: ...

    def validate_and_load(self, lease_id: str) -> SettlementClaim | None: ...

    def compare_and_set_progress(
        self,
        lease_id: str,
        expected_revision: int,
        progress: SettlementProgress,
    ) -> SettlementClaim | None: ...

    def diagnostic_claims(self) -> tuple[SettlementClaim, ...]: ...


class StreamFinalityPort(Protocol):
    def remove_owner(self, claim: SettlementClaim) -> OwnerRemovalReceipt: ...

    def late_owner_commands_resolved(
        self, claim: SettlementClaim
    ) -> bool: ...

    def acknowledge_finality(
        self, claim: SettlementClaim, progress: SettlementProgress
    ) -> FinalityAckReceipt | None: ...

    def prune_finality(
        self, claim: SettlementClaim, receipt: FinalityAckReceipt
    ) -> FinalityPruneReceipt: ...


class OwnerLifecyclePort(Protocol):
    def signal_owner(self, claim: SettlementClaim) -> bool: ...

    def join_owner(self, claim: SettlementClaim) -> bool: ...


class ArtifactSettlementPort(Protocol):
    def check_artifact_readiness(self, claim: SettlementClaim) -> bool: ...

    def invoke_completion_handler(
        self, claim: SettlementClaim
    ) -> CompletionInvocationResult: ...

    def settle_artifact_and_publication(
        self,
        claim: SettlementClaim,
        *,
        invoke_handler: bool = False,
    ) -> ArtifactSettlementResult: ...

    def confirm_quota_outcome(self, claim: SettlementClaim) -> bool: ...


class TerminalTransitionPort(Protocol):
    def terminal_cas(
        self,
        claim: SettlementClaim,
        progress: SettlementProgress,
        summary: SettlementTerminalSummary,
    ) -> SettlementClaim | None: ...

    def clear_references(
        self,
        lease_id: str,
        expected_revision: int,
        progress: SettlementProgress,
        summary: SettlementTerminalSummary,
    ) -> SettlementClaim | None: ...


__all__ = [
    "ArtifactSettlementPort",
    "ArtifactSettlementResult",
    "CompletionInvocationResult",
    "OwnerLifecyclePort",
    "SettlementAdvanceResult",
    "SettlementClaim",
    "SettlementClaimDiagnostic",
    "SettlementClaimPort",
    "SettlementDiagnostics",
    "SettlementIntent",
    "SettlementOwnerDiagnosticId",
    "SettlementPhase",
    "SettlementProgress",
    "SettlementTerminalSummary",
    "StreamFinalityPort",
    "TerminalTransitionPort",
    "canonical_settlement_candidate",
    "authoritative_artifact_result",
    "authoritative_completion_invocation",
    "claim_diagnostic_for",
    "exact_settlement_ack",
    "exact_settlement_removal",
    "effective_terminal_state_and_error",
    "progress_after_artifact_result",
    "settlement_prune_confirms",
    "terminal_summary_for",
]
