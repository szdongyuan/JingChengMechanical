"""Pool settlement claim routing and shutdown phase gates."""

from __future__ import annotations

import threading

from .contracts import LeaseState
from .owner_settlement_types import SettlementClaim, SettlementIntent
from .pool_types import PoolState


class ResidentPoolSettlementControlMixin:
    @staticmethod
    def _ledger_backpressure_diagnostic(stream):
        snapshot = stream.finality_snapshot()
        return (
            ("capacity_domain", "owner_finality_ledger"),
            ("hardware_id", snapshot.hardware_id),
            ("ledger_limit", snapshot.ledger_limit),
            *(
                (f"ledger_{state.value.lower()}_count", count)
                for state, count in snapshot.state_counts
            ),
            (
                "backpressure_rejection_count",
                snapshot.backpressure_rejection_count,
            ),
        )

    @staticmethod
    def _bounded_settlement_reason(intent, error_code, failure_reason):
        if failure_reason is None:
            return None
        code = "unspecified" if error_code is None else error_code.value
        return f"resident audio {intent.value.lower()} settlement ({code})"

    def _claim_settlement(
        self,
        record,
        intent,
        terminal_state,
        error_code,
        failure_reason,
        *,
        non_admission_proof=None,
        artifact=None,
    ):
        failure_reason = self._bounded_settlement_reason(
            intent, error_code, failure_reason
        )
        candidate = SettlementClaim(
            lease_id=record.run_id,
            intent=intent,
            owner_instance_id=record.owner_instance_id,
            stream_instance_id=record.stream.stream_instance_id,
            terminal_state=terminal_state.value,
            error_code=None if error_code is None else error_code.value,
            failure_reason=failure_reason,
            non_admission_proof=non_admission_proof,
            owner=record.owner,
            writer=record.writer,
            stream=record.stream,
            artifact=artifact or record.artifact,
            completion_handler=(
                self._completion_handler
                if intent is SettlementIntent.OWNER_SUCCESS
                else None
            ),
        )
        return self._settlement_coordinator.claim_or_resume(candidate)

    def _submit_settlement(
        self,
        record,
        intent,
        terminal_state,
        error_code,
        failure_reason,
        *,
        non_admission_proof=None,
        artifact=None,
    ):
        claim_result = self._claim_settlement(
            record,
            intent,
            terminal_state,
            error_code,
            failure_reason,
            non_admission_proof=non_admission_proof,
            artifact=artifact,
        )
        with self._lock:
            current = self._leases.get(record.run_id)
            claim = None if current is None else current.settlement_claim
            admitted = bool(
                current is record
                and claim is not None
                and claim.owner_instance_id == record.owner_instance_id
            )
        if not admitted:
            return claim_result
        return self._advance_settlement(record.run_id)

    def _schedule_settlement_advances(self, run_ids):
        pending = tuple(dict.fromkeys(run_ids))
        if not pending:
            return
        execution = self._new_event()

        def advance_claims():
            try:
                for run_id in pending:
                    self._advance_settlement(run_id)
            finally:
                with self._lock:
                    execution.set()

        worker = threading.Thread(
            target=advance_claims,
            name="resident-audio-stream-failure-settlement",
            daemon=True,
        )
        with self._lock:
            worker.start()
            self._terminal_executions.add(execution)
            self._register_execution_locked(
                execution,
                "terminal",
                worker,
                self._execution_identities_for_run_ids_locked(pending),
            )
        return execution

    def _claim_stream_failure_settlements(
        self, stream, event, intent
    ):
        with self._lock:
            affected = [
                record
                for record in self._leases.values()
                if (
                    record.stream is stream
                    and record.state
                    in (LeaseState.COMMITTING, LeaseState.COMMITTED)
                    and record.owner_instance_id is not None
                    and record.settlement_claim is None
                )
            ]
        for record in affected:
            self._claim_settlement(
                record,
                intent,
                LeaseState.FAILED,
                event.error_code,
                event.message,
            )
        self._schedule_settlement_advances(
            record.run_id for record in affected
        )

    def _advance_settlement(self, run_id):
        result = None
        for _ in range(32):
            with self._lock:
                record = self._leases.get(run_id)
                claim = (
                    None if record is None else record.settlement_claim
                )
                stream_gate = bool(
                    self._state
                    in (PoolState.SHUTTING_DOWN, PoolState.FAILED_CLOSED)
                    and claim is not None
                    and claim.progress.owner_signaled
                    and not self._shutdown_stream_teardown_complete
                )
                full_gate = bool(
                    self._state
                    in (PoolState.SHUTTING_DOWN, PoolState.FAILED_CLOSED)
                    and claim is not None
                    and claim.progress.owner_joined
                    and not self._shutdown_teardown_complete
                )
            if stream_gate or full_gate:
                with self._lock:
                    record = self._leases.get(run_id)
                    if (
                        record is not None
                        and record.pre_teardown_settled is not None
                        and stream_gate
                    ):
                        record.pre_teardown_settled.set()
                break
            try:
                result = self._settlement_coordinator.advance(run_id)
            except KeyError:
                break
            if result.settled or not result.progressed:
                break
        return result

    def retry_settlements(self):
        deadline = (
            self._shutdown_clock() + self._shutdown_control_timeout
        )
        current_worker = threading.current_thread()
        with self._lock:
            self._harvest_finished_workers_locked()
            terminal_executions = tuple(
                execution
                for execution in self._terminal_executions
                if (
                    self._execution_resources.get(execution) is None
                    or self._execution_resources[execution].worker
                    is not current_worker
                )
            )
        for execution in terminal_executions:
            execution.wait(self._shutdown_remaining(deadline))
        with self._lock:
            self._harvest_finished_workers_locked()
            run_ids = tuple(
                record.run_id
                for record in self._leases.values()
                if record.settlement_claim is not None
            )
        for run_id in run_ids:
            self._advance_settlement(run_id)
        return self.snapshot()

    @staticmethod
    def _lease_cleanup_pending_locked(record):
        return not record.settled or record.settlement_claim is not None


__all__ = ["ResidentPoolSettlementControlMixin"]
