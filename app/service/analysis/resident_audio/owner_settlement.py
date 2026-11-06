"""Retry-safe coordinator for post-owner lease settlement."""

from __future__ import annotations

import threading
from dataclasses import replace

from .owner_identity import MAX_SIGNED_64
from .owner_settlement_types import (
    ArtifactSettlementPort,
    ArtifactSettlementResult,
    CompletionInvocationResult,
    OwnerLifecyclePort,
    SettlementAdvanceResult,
    SettlementClaim,
    SettlementClaimPort,
    SettlementDiagnostics,
    SettlementIntent,
    SettlementPhase,
    SettlementProgress,
    StreamFinalityPort,
    TerminalTransitionPort,
    authoritative_artifact_result,
    authoritative_completion_invocation,
    canonical_settlement_candidate,
    claim_diagnostic_for,
    exact_settlement_ack,
    exact_settlement_removal,
    progress_after_artifact_result,
    settlement_prune_confirms,
    terminal_summary_for,
)


_MAX_DIAGNOSTIC_CLAIMS = 64


class _AdvanceGate:
    __slots__ = ("lock", "owner_thread_id", "users")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.owner_thread_id: int | None = None
        self.users = 0


class SettlementReentrantAdvanceError(RuntimeError):
    """The active external step synchronously re-entered its own lease."""


class OwnerSettlementCoordinator:
    """Arbitrate and monotonically advance one settlement claim per lease."""

    def __init__(
        self,
        *,
        claims: SettlementClaimPort,
        finality: StreamFinalityPort,
        owners: OwnerLifecyclePort,
        artifacts: ArtifactSettlementPort,
        terminal: TerminalTransitionPort,
    ) -> None:
        self._claims = claims
        self._finality = finality
        self._owners = owners
        self._artifacts = artifacts
        self._terminal = terminal
        self._guard = threading.Lock()
        self._advance_locks: dict[str, _AdvanceGate] = {}
        self._retry_counts = {
            "removal": 0,
            "owner_join": 0,
            "artifact_disposition": 0,
            "publication": 0,
        }
        self._backpressure_rejections = 0

    def claim_or_resume(
        self, candidate: SettlementClaim
    ) -> SettlementAdvanceResult:
        canonical_candidate = canonical_settlement_candidate(candidate)
        if canonical_candidate is None:
            return self._result(candidate, claimed=False, progressed=False)
        candidate = canonical_candidate
        retained, created = self._claims.claim_or_resume(candidate)
        if (
            created
            and retained.non_admission_proof is not None
        ):
            with self._guard:
                self._backpressure_rejections = self._increment(
                    self._backpressure_rejections
                )
        return self._result(retained, claimed=created, progressed=created)

    def advance(self, lease_id: str) -> SettlementAdvanceResult:
        gate = self._gate_for(lease_id)
        thread_id = threading.get_ident()
        try:
            with self._guard:
                if gate.owner_thread_id == thread_id:
                    raise SettlementReentrantAdvanceError(
                        "same-lease settlement advance is reentrant"
                    )
            with gate.lock:
                with self._guard:
                    gate.owner_thread_id = thread_id
                try:
                    claim = self._claims.validate_and_load(lease_id)
                    if claim is None:
                        raise KeyError(lease_id)
                    if claim.progress.terminalized:
                        if claim.progress.references_cleared:
                            return self._result(
                                claim, claimed=False, progressed=False
                            )
                        return self._advance_reference_clear(
                            claim, terminal_progressed=False
                        )
                    return self._advance_one(claim)
                finally:
                    with self._guard:
                        gate.owner_thread_id = None
        finally:
            self._release_gate(lease_id, gate)

    def diagnostics(self) -> SettlementDiagnostics:
        retained = self._claims.diagnostic_claims()
        bounded = retained[-_MAX_DIAGNOSTIC_CLAIMS:]
        with self._guard:
            retries = dict(self._retry_counts)
            backpressure_rejections = self._backpressure_rejections
        counts = tuple(
            (
                phase,
                sum(
                    claim.progress.phase is phase
                    for claim in retained
                ),
            )
            for phase in SettlementPhase
        )
        return SettlementDiagnostics(
            claims_by_phase=counts,
            unsettled_leases=sum(
                not self._complete(claim.progress) for claim in retained
            ),
            removal_retries=retries["removal"],
            owner_join_retries=retries["owner_join"],
            artifact_disposition_retries=(
                retries["artifact_disposition"]
            ),
            publication_retries=retries["publication"],
            settlement_backpressure_rejections=backpressure_rejections,
            claims=tuple(claim_diagnostic_for(claim) for claim in bounded),
        )

    def _advance_one(
        self, claim: SettlementClaim
    ) -> SettlementAdvanceResult:
        progress = claim.progress
        if not (
            progress.removal_receipt is not None
            or progress.non_admission_confirmed
        ):
            return self._advance_finality_admission(claim)
        if not progress.owner_signaled:
            return self._confirm_bool(
                claim,
                lambda: self._owners.signal_owner(claim),
                owner_signaled=True,
                phase=SettlementPhase.OWNER_JOIN,
            )
        if not progress.owner_joined:
            self._record_retry(
                "owner_join", progress.owner_join_attempts
            )
            attempted = replace(
                progress,
                owner_join_attempts=self._increment(
                    progress.owner_join_attempts
                ),
            )
            return self._confirm_bool(
                claim,
                lambda: self._owners.join_owner(claim),
                base=attempted,
                owner_joined=True,
                phase=SettlementPhase.ARTIFACT_READINESS,
            )
        if not progress.artifact_ready_checked:
            next_phase = (
                SettlementPhase.ARTIFACT_SETTLEMENT
                if progress.non_admission_confirmed
                else SettlementPhase.LATE_COMMANDS
            )
            return self._confirm_bool(
                claim,
                lambda: self._artifacts.check_artifact_readiness(claim),
                artifact_ready_checked=True,
                phase=next_phase,
            )
        if (
            not progress.late_owner_commands_resolved
            and not progress.late_owner_commands_not_applicable
        ):
            return self._confirm_bool(
                claim,
                lambda: self._finality.late_owner_commands_resolved(
                    claim
                ),
                late_owner_commands_resolved=True,
                phase=SettlementPhase.REMOVE_DISABLE,
            )
        if not progress.remove_retry_disabled:
            updated = replace(
                progress,
                remove_retry_disabled=True,
                phase=SettlementPhase.ACKNOWLEDGEMENT,
            )
            return self._persist_result(claim, updated, progressed=True)
        if (
            not progress.finality_not_applicable
            and not progress.finality_acknowledged
        ):
            return self._advance_acknowledgement(claim)
        if (
            not progress.finality_not_applicable
            and not progress.finality_pruned
        ):
            return self._advance_prune(claim)
        if not (
            progress.publication_resolved
            and progress.artifact_disposition_confirmed
        ):
            return self._advance_artifact_settlement(claim)
        if not progress.quota_outcome_confirmed:
            return self._confirm_bool(
                claim,
                lambda: self._artifacts.confirm_quota_outcome(claim),
                quota_outcome_confirmed=True,
                phase=SettlementPhase.TERMINAL,
            )
        return self._advance_terminal(claim)

    def _advance_finality_admission(
        self, claim: SettlementClaim
    ) -> SettlementAdvanceResult:
        progress = claim.progress
        proof = claim.non_admission_proof
        if proof is not None:
            if not proof.confirms(claim.owner_instance_id):
                return self._result(
                    claim, claimed=False, progressed=False
                )
            updated = replace(
                progress,
                non_admission_confirmed=True,
                late_owner_commands_not_applicable=True,
                remove_retry_disabled=True,
                finality_not_applicable=True,
                phase=SettlementPhase.OWNER_SIGNAL,
            )
            return self._persist_result(claim, updated, progressed=True)
        attempted = replace(
            progress,
            removal_attempts=self._increment(progress.removal_attempts),
            phase=SettlementPhase.REMOVAL,
        )
        self._record_retry("removal", progress.removal_attempts)
        try:
            receipt = self._finality.remove_owner(claim)
        except Exception:
            return self._persist_result(
                claim, attempted, progressed=False
            )
        receipt = exact_settlement_removal(receipt, claim)
        if receipt is None:
            return self._persist_result(
                claim, attempted, progressed=False
            )
        updated = replace(
            attempted,
            removal_receipt=receipt,
            phase=SettlementPhase.OWNER_SIGNAL,
        )
        return self._persist_result(claim, updated, progressed=True)

    def _advance_acknowledgement(
        self, claim: SettlementClaim
    ) -> SettlementAdvanceResult:
        try:
            receipt = self._finality.acknowledge_finality(
                claim, claim.progress
            )
        except Exception:
            return self._result(
                claim, claimed=False, progressed=False
            )
        receipt = exact_settlement_ack(receipt, claim)
        if receipt is None:
            return self._result(
                claim, claimed=False, progressed=False
            )
        updated = replace(
            claim.progress,
            finality_acknowledged=True,
            finality_ack_receipt=receipt,
            phase=SettlementPhase.PRUNE,
        )
        return self._persist_result(claim, updated, progressed=True)

    def _advance_prune(
        self, claim: SettlementClaim
    ) -> SettlementAdvanceResult:
        ack = claim.progress.finality_ack_receipt
        if ack is None:
            return self._result(
                claim, claimed=False, progressed=False
            )
        try:
            receipt = self._finality.prune_finality(claim, ack)
        except Exception:
            return self._result(
                claim, claimed=False, progressed=False
            )
        if not settlement_prune_confirms(receipt, claim):
            return self._result(
                claim, claimed=False, progressed=False
            )
        updated = replace(
            claim.progress,
            finality_pruned=True,
            phase=SettlementPhase.ARTIFACT_SETTLEMENT,
        )
        return self._persist_result(claim, updated, progressed=True)

    def _advance_artifact_settlement(
        self, claim: SettlementClaim
    ) -> SettlementAdvanceResult:
        progress = claim.progress
        if (
            claim.intent is SettlementIntent.OWNER_SUCCESS
            and not progress.handler_invoked
        ):
            started = replace(progress, handler_invoked=True)
            retained = self._claims.compare_and_set_progress(
                claim.lease_id, claim.revision, started
            )
            if retained is None:
                retained = self._claims.validate_and_load(claim.lease_id)
                if retained is None:
                    raise KeyError(claim.lease_id)
                return self._result(
                    retained, claimed=False, progressed=False
                )
            claim = retained
            progress = retained.progress
            try:
                invocation = self._artifacts.invoke_completion_handler(
                    claim
                )
            except Exception:
                return self._result(
                    claim, claimed=False, progressed=True
                )
            if (
                not isinstance(invocation, CompletionInvocationResult)
                or not authoritative_completion_invocation(invocation)
            ):
                return self._result(
                    claim, claimed=False, progressed=True
                )
            completed = replace(
                progress,
                handler_completed=True,
                handler_returned=invocation.handler_returned,
                handler_resolution=invocation.resolution,
                handler_exception_type=invocation.exception_type,
            )
            return self._persist_result(
                claim, completed, progressed=True
            )
        if not progress.publication_resolved:
            self._record_retry(
                "publication", progress.publication_attempts
            )
        if not progress.artifact_disposition_confirmed:
            self._record_retry(
                "artifact_disposition",
                progress.artifact_disposition_attempts,
            )
        attempted = replace(
            progress,
            publication_attempts=(
                progress.publication_attempts
                if progress.publication_resolved
                else self._increment(progress.publication_attempts)
            ),
            artifact_disposition_attempts=(
                progress.artifact_disposition_attempts
                if progress.artifact_disposition_confirmed
                else self._increment(
                    progress.artifact_disposition_attempts
                )
            ),
        )
        try:
            result = self._artifacts.settle_artifact_and_publication(
                claim,
                invoke_handler=False,
            )
        except Exception:
            return self._persist_result(
                claim, attempted, progressed=False
            )
        if not isinstance(result, ArtifactSettlementResult):
            return self._persist_result(
                claim, attempted, progressed=False
            )
        if not authoritative_artifact_result(claim, result):
            return self._persist_result(
                claim, attempted, progressed=False
            )
        updated, changed = progress_after_artifact_result(
            progress,
            attempted,
            result,
            handler_started=False,
        )
        return self._persist_result(claim, updated, progressed=changed)

    def _advance_terminal(
        self, claim: SettlementClaim
    ) -> SettlementAdvanceResult:
        summary = terminal_summary_for(claim)
        updated = replace(
            claim.progress,
            terminalized=True,
            phase=SettlementPhase.REFERENCE_CLEAR,
        )
        try:
            persisted = self._terminal.terminal_cas(
                claim, updated, summary
            )
        except Exception:
            persisted = None
        if persisted is None or not persisted.progress.terminalized:
            return self._result(
                claim, claimed=False, progressed=False
            )
        return self._advance_reference_clear(
            persisted, terminal_progressed=True
        )

    def _advance_reference_clear(
        self,
        claim: SettlementClaim,
        *,
        terminal_progressed: bool,
    ) -> SettlementAdvanceResult:
        summary = claim.terminal_summary or terminal_summary_for(claim)
        updated = replace(
            claim.progress,
            references_cleared=True,
            phase=SettlementPhase.SETTLED,
        )
        try:
            persisted = self._terminal.clear_references(
                claim.lease_id,
                claim.revision,
                updated,
                summary,
            )
        except Exception:
            persisted = None
        if persisted is None or not self._complete(persisted.progress):
            return self._result(
                claim,
                claimed=False,
                progressed=terminal_progressed,
            )
        return self._result(
            persisted, claimed=False, progressed=True
        )

    def _confirm_bool(
        self,
        claim: SettlementClaim,
        action,
        *,
        base: SettlementProgress | None = None,
        phase: SettlementPhase,
        **confirmation: bool,
    ) -> SettlementAdvanceResult:
        progress = base or claim.progress
        try:
            confirmed = action()
        except Exception:
            if base is not None:
                return self._persist_result(
                    claim, base, progressed=False
                )
            return self._result(
                claim, claimed=False, progressed=False
            )
        if confirmed is not True:
            if base is not None:
                return self._persist_result(
                    claim, base, progressed=False
                )
            return self._result(
                claim, claimed=False, progressed=False
            )
        updated = replace(progress, phase=phase, **confirmation)
        return self._persist_result(claim, updated, progressed=True)

    def _persist_result(
        self,
        claim: SettlementClaim,
        progress: SettlementProgress,
        *,
        progressed: bool,
    ) -> SettlementAdvanceResult:
        retained = self._claims.compare_and_set_progress(
            claim.lease_id, claim.revision, progress
        )
        if retained is None:
            retained = self._claims.validate_and_load(claim.lease_id)
            if retained is None:
                raise KeyError(claim.lease_id)
            progressed = False
        return self._result(
            retained, claimed=False, progressed=progressed
        )

    @staticmethod
    def _result(
        claim: SettlementClaim,
        *,
        claimed: bool,
        progressed: bool,
    ) -> SettlementAdvanceResult:
        return SettlementAdvanceResult(
            lease_id=claim.lease_id,
            intent=claim.intent,
            phase=claim.progress.phase,
            progress=claim.progress,
            claimed=claimed,
            progressed=progressed,
            settled=OwnerSettlementCoordinator._complete(
                claim.progress
            ),
        )

    def _gate_for(self, lease_id: str) -> _AdvanceGate:
        with self._guard:
            gate = self._advance_locks.setdefault(
                lease_id, _AdvanceGate()
            )
            gate.users += 1
            return gate

    def _release_gate(
        self, lease_id: str, gate: _AdvanceGate
    ) -> None:
        with self._guard:
            gate.users -= 1
            if (
                gate.users == 0
                and self._advance_locks.get(lease_id) is gate
            ):
                del self._advance_locks[lease_id]

    def _record_retry(self, name: str, prior_attempts: int) -> None:
        if prior_attempts < 1:
            return
        with self._guard:
            self._retry_counts[name] = self._increment(
                self._retry_counts[name]
            )

    @staticmethod
    def _complete(progress: SettlementProgress) -> bool:
        return (
            progress.terminalized
            and progress.references_cleared
        )

    @staticmethod
    def _increment(value: int) -> int:
        return min(value + 1, MAX_SIGNED_64)

__all__ = [
    "OwnerSettlementCoordinator",
    "SettlementReentrantAdvanceError",
]
