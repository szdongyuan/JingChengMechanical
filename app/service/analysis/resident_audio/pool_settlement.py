"""Resident-pool ports and intent adapter for owner settlement."""

from __future__ import annotations

from dataclasses import replace

from .artifact_settlement import ArtifactSettlementFacade
from .contracts import ArtifactState, CaptureErrorCode, LeaseState
from .owner_finality import FinalityDurableProgress
from .owner_settlement_types import (
    ArtifactSettlementResult,
    SettlementClaim,
    SettlementIntent,
    SettlementProgress,
    terminal_summary_for,
)
from .pool_types import CaptureCompletion, LeaseOutcome, PoolState
from .pool_settlement_control import ResidentPoolSettlementControlMixin


class PoolSettlementAdapter:
    """Implement every coordinator port against one resident pool."""

    def __init__(self, pool) -> None:
        self._pool = pool
        self._claims: dict[str, SettlementClaim] = {}
        self._artifacts = ArtifactSettlementFacade(pool._artifact_store)

    def claim_or_resume(self, candidate):
        with self._pool._lock:
            retained = self._claims.get(candidate.lease_id)
            if retained is not None:
                record = self._pool._leases.get(candidate.lease_id)
                if (
                    record is not None
                    and record.settlement_claim is retained
                    and record.owner_instance_id
                    == retained.owner_instance_id
                    == candidate.owner_instance_id
                ):
                    return retained, False
                return candidate, False
            record = self._pool._leases.get(candidate.lease_id)
            if (
                record is None
                or record.owner_instance_id != candidate.owner_instance_id
                or record.state not in (
                    LeaseState.COMMITTING,
                    LeaseState.COMMITTED,
                )
            ):
                return candidate, False
            self._claims[candidate.lease_id] = candidate
            record.settlement_claim = candidate
            record.settled = False
            return candidate, True

    def validate_and_load(self, lease_id):
        with self._pool._lock:
            claim = self._claims.get(lease_id)
            record = self._pool._leases.get(lease_id)
            if (
                claim is None
                or record is None
                or record.owner_instance_id != claim.owner_instance_id
                or record.state not in (
                    LeaseState.COMMITTING,
                    LeaseState.COMMITTED,
                    LeaseState.RELEASED,
                    LeaseState.FAILED,
                    LeaseState.CANCELED,
                )
                or (
                    record.state is LeaseState.COMMITTING
                    and not record.commit_resources_published
                )
            ):
                return None
            return claim

    def publish_commit_resources(self, record, owner, writer, stream):
        with self._pool._lock:
            claim = self._claims.get(record.run_id)
            if (
                self._pool._leases.get(record.run_id) is not record
                or record.state is not LeaseState.COMMITTING
                or record.settlement_claim is not claim
                or claim is None
                or claim.revision != 0
                or claim.progress != SettlementProgress()
                or record.commit_resources_published
            ):
                return False
            retained = replace(
                claim,
                owner=owner,
                writer=writer,
                stream=stream,
            )
            self._claims[record.run_id] = retained
            record.owner = owner
            record.writer = writer
            record.stream = stream
            record.settlement_claim = retained
            record.commit_resources_published = True
            return True

    def compare_and_set_progress(
        self, lease_id, expected_revision, progress
    ):
        with self._pool._lock:
            claim = self._claims.get(lease_id)
            if claim is None or claim.revision != expected_revision:
                return None
            retained = replace(
                claim, progress=progress, revision=claim.revision + 1
            )
            self._claims[lease_id] = retained
            record = self._pool._leases.get(lease_id)
            if record is not None:
                record.settlement_claim = retained
            return retained

    def diagnostic_claims(self):
        with self._pool._lock:
            return tuple(self._claims.values())

    def remove_owner(self, claim):
        return getattr(claim.stream, "remove_owner")(
            claim.owner_instance_id, timeout=1.0
        )

    @staticmethod
    def late_owner_commands_resolved(claim):
        # An exact positive ledger receipt cannot be issued with a queued or
        # claimed prepare. The acknowledgement repeats this invariant.
        return claim.progress.removal_receipt is not None

    def acknowledge_finality(self, claim, progress):
        durable = FinalityDurableProgress(
            removal_receipt=progress.removal_receipt,
            owner_signaled=progress.owner_signaled,
            owner_joined=progress.owner_joined,
            late_owner_commands_resolved=(
                progress.late_owner_commands_resolved
            ),
            remove_retry_disabled=progress.remove_retry_disabled,
        )
        return getattr(claim.stream, "acknowledge_finality")(
            claim.owner_instance_id,
            progress.removal_receipt,
            durable,
            timeout=1.0,
        )

    @staticmethod
    def prune_finality(claim, receipt):
        return getattr(claim.stream, "prune_finality")(
            claim.owner_instance_id, receipt, timeout=1.0
        )

    @staticmethod
    def signal_owner(claim):
        owner = claim.owner
        if owner is None:
            return True
        if claim.intent in (
            SettlementIntent.OWNER_SUCCESS,
            SettlementIntent.OWNER_FAILURE,
        ):
            return True
        if claim.intent is SettlementIntent.STREAM_FAILURE:
            owner.fail(
                CaptureErrorCode(claim.error_code),
                claim.failure_reason or "resident stream failed",
            )
            return True
        if claim.intent is SettlementIntent.POOL_SHUTDOWN:
            owner.shutdown()
        elif claim.terminal_state == LeaseState.CANCELED.value:
            owner.cancel(claim.failure_reason or "capture canceled")
        else:
            owner.fail(
                CaptureErrorCode(claim.error_code),
                claim.failure_reason or "capture failed",
            )
        return True

    def join_owner(self, claim):
        with self._pool._lock:
            if (
                self._pool._state
                in (PoolState.SHUTTING_DOWN, PoolState.FAILED_CLOSED)
                and not self._pool._shutdown_stream_teardown_complete
            ):
                return False
        return claim.owner is None or claim.owner.join(1.0) is True

    def check_artifact_readiness(self, claim):
        if claim.intent is not SettlementIntent.OWNER_SUCCESS:
            return True
        artifact_id = getattr(claim.artifact, "artifact_id", None)
        if artifact_id is None:
            return False
        state = self._pool._artifact_store.state(artifact_id)
        return state in (
            ArtifactState.READY_UNCLAIMED,
            ArtifactState.CLAIMED,
            ArtifactState.TRANSFERRED,
            ArtifactState.DELETED,
            ArtifactState.RELEASED,
        )

    def settle_artifact_and_publication(
        self, claim, *, invoke_handler=False
    ):
        del invoke_handler
        if claim.intent is SettlementIntent.OWNER_SUCCESS:
            return self._settle_success(claim)
        artifact_id = (
            getattr(claim.artifact, "artifact_id", None)
            or getattr(claim.writer, "artifact_id", None)
        )
        if artifact_id is None:
            return ArtifactSettlementResult(True, True)
        writer = claim.writer
        if (
            writer is not None
            and self._pool._artifact_store.state(artifact_id)
            is ArtifactState.PARTIAL
        ):
            if writer.discard() is not True:
                return self._result_from_receipt(
                    self._artifacts.inspect(artifact_id)
                )
        token = (
            getattr(claim.artifact, "publication_token", None)
        )
        receipt = self._artifacts.settle_non_success(
            artifact_id, token
        )
        return self._result_from_receipt(receipt)

    def invoke_completion_handler(self, claim):
        artifact = claim.artifact
        if artifact is None:
            return self._artifacts.unavailable_completion_result()
        completion = CaptureCompletion(
            artifact,
            self._pool._artifact_store,
            claimed=lambda consumer_id: self._record_artifact_consumer(
                claim.lease_id, claim.owner_instance_id, consumer_id
            ),
        )
        return self._artifacts.invoke_completion_handler(
            claim.completion_handler,
            completion,
        )

    def _record_artifact_consumer(
        self, lease_id, owner_instance_id, consumer_id
    ):
        with self._pool._lock:
            record = self._pool._leases.get(lease_id)
            if record is not None and record.owner_instance_id == owner_instance_id:
                record.artifact_consumer_id = consumer_id

    def _settle_success(self, claim):
        artifact = claim.artifact
        if artifact is None:
            return ArtifactSettlementResult(False, False)
        if claim.progress.terminal_state_override is None:
            outcome = self._artifacts.classify_completion(artifact,
                handler_returned=claim.progress.handler_returned,
                exception_type=claim.progress.handler_exception_type,
            )
        else:
            outcome = self._artifacts.settle_completion(artifact,
                prior_terminal_state=claim.progress.terminal_state_override,
                prior_error_code=claim.progress.error_code_override,
                prior_failure_reason=claim.progress.failure_reason_override,
                prior_exception_type=claim.progress.handler_exception_type,
            )
        return ArtifactSettlementResult(
            publication_resolved=outcome.artifact.publication_resolved,
            artifact_disposition_confirmed=outcome.artifact.disposition_confirmed,
            receipt=outcome.artifact,
            terminal_state=outcome.terminal_state,
            error_code=outcome.error_code,
            failure_reason=outcome.failure_reason,
            exception_type=outcome.exception_type,
        )

    def confirm_quota_outcome(self, claim):
        artifact_id = (
            getattr(claim.artifact, "artifact_id", None)
            or getattr(claim.writer, "artifact_id", None)
        )
        if artifact_id is None:
            return True
        return self._artifacts.inspect(artifact_id).quota_outcome_confirmed

    @staticmethod
    def _result_from_receipt(receipt):
        return ArtifactSettlementResult(
            publication_resolved=receipt.publication_resolved,
            artifact_disposition_confirmed=(
                receipt.disposition_confirmed
            ),
            receipt=receipt,
        )

    def terminal_cas(self, claim, progress, summary):
        with self._pool._lock:
            retained = self._claims.get(claim.lease_id)
            record = self._pool._leases.get(claim.lease_id)
            if (
                retained is not claim
                or record is None
                or record.owner_instance_id != claim.owner_instance_id
                or record.state not in (
                    LeaseState.COMMITTING,
                    LeaseState.COMMITTED,
                )
            ):
                return None
            state = LeaseState(
                progress.terminal_state_override or claim.terminal_state
            )
            error_code = (
                progress.error_code_override
                if progress.terminal_state_override is not None
                else claim.error_code
            )
            failure_reason = (
                progress.failure_reason_override
                if progress.failure_reason_override is not None
                else claim.failure_reason
            )
            record.previous_state = record.state
            record.state = state
            record.outcome = LeaseOutcome(
                run_id=record.run_id,
                state=state,
                error_code=(
                    None
                    if error_code is None
                    else CaptureErrorCode(error_code)
                ),
                message=failure_reason,
                artifact_id=record.artifact_id,
                artifact=claim.artifact
                if state is LeaseState.RELEASED
                else None,
                diagnostic_context=record.diagnostic_context,
            )
            record.settled = True
            record.terminal_summary = summary
            self._pool._remember_terminal_locked(record)
            updated = replace(
                claim,
                progress=progress,
                revision=claim.revision + 1,
                terminal_summary=summary,
            )
            self._claims[claim.lease_id] = updated
            record.settlement_claim = updated
            if record.commit_settled is not None:
                record.commit_settled.set()
            if record.terminal_settled is not None:
                record.terminal_settled.set()
            return updated

    def clear_references(
        self, lease_id, expected_revision, progress, summary
    ):
        with self._pool._lock:
            claim = self._claims.get(lease_id)
            record = self._pool._leases.get(lease_id)
            if (
                claim is None
                or claim.revision != expected_revision
                or record is None
                or record.settlement_claim is not claim
                or record.owner_instance_id != claim.owner_instance_id
            ):
                return None
            cleared = replace(
                claim,
                owner=None,
                writer=None,
                stream=None,
                artifact=None,
                ring=None,
                callback=None,
                completion_handler=None,
                progress=progress,
                revision=claim.revision + 1,
                terminal_summary=summary,
            )
            record.owner = record.writer = record.stream = record.artifact = None
            record.owner_capability = record.publication_token = None
            record.identity = record.owner_instance_id = None
            record.non_admission_proof = record.commit_token = None
            record.settlement_claim = None
            self._claims.pop(lease_id, None)
            self._pool._prune_terminal_history_locked()
            return cleared


class ResidentPoolSettlementMixin(ResidentPoolSettlementControlMixin):
    def _prune_terminal_history_locked(self):
        while len(self._terminal_order) > self._terminal_diagnostic_limit:
            evictable = None
            for run_id in self._terminal_order:
                old = self._leases.get(run_id)
                if old is None or (
                    old.state
                    not in (LeaseState.COMMITTING, LeaseState.COMMITTED)
                    and old.settlement_claim is None
                    and all(
                        value is None
                        for value in (
                            old.owner,
                            old.writer,
                            old.stream,
                            old.artifact,
                        )
                    )
                ):
                    evictable = run_id
                    break
            if evictable is None:
                break
            run_id = evictable
            old = self._leases.get(run_id)
            self._terminal_order.pop(run_id, None)
            if old is not None:
                self._leases.pop(run_id, None)

    def _ownerless_reserved_terminal_locked(
        self, record, state, code, message
    ):
        assert record.owner_instance_id is None
        assert record.owner is None
        assert record.writer is None
        assert record.stream is None
        assert record.artifact is None
        assert record.settlement_claim is None
        return self._terminalize_locked(record, state, code, message)


__all__ = ["PoolSettlementAdapter", "ResidentPoolSettlementMixin"]
