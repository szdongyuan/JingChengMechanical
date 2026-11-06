"""Thin intent routing for every post-owner cleanup path."""

from __future__ import annotations

from .contracts import CaptureErrorCode, LeaseState
from .owner_settlement_types import SettlementIntent


class ResidentPoolCleanupMixin:
    def _rollback_commit(
        self,
        record,
        owner,
        writer,
        stream,
        identity,
        prepared,
        failure_code,
        failure_message,
        *,
        intent=SettlementIntent.PREPARE_ROLLBACK,
    ) -> None:
        current = None
        with self._lock:
            current = self._leases.get(record.run_id)
            if (
                current is not record
                or current.commit_attempt_id != record.commit_attempt_id
            ):
                return
            retry = current.settlement_claim is not None
            resources_published = current.commit_resources_published
            if not retry:
                canceled = (
                    current.cancel_requested
                    or current.shutdown_requested
                    or not self._pool_is_open_locked()
                )
                current.owner = owner
                current.writer = writer
                current.stream = stream
                current.owner_instance_id = identity
                terminal_state = (
                    LeaseState.CANCELED if canceled else LeaseState.FAILED
                )
                terminal_code = (
                    CaptureErrorCode.CAPTURE_CANCELED
                    if canceled
                    else failure_code
                )
                terminal_message = (
                    current.cancel_reason or "capture commit canceled"
                    if canceled
                    else failure_message
                )
                proof = current.non_admission_proof
                current.commit_resources_published = True
        if retry:
            if not resources_published and not (
                self._settlement_adapter.publish_commit_resources(
                    record, owner, writer, stream
                )
            ):
                return
            self._advance_settlement(record.run_id)
            return
        self._submit_settlement(
            record,
            intent,
            terminal_state,
            terminal_code,
            terminal_message,
            non_admission_proof=proof,
        )

    def _cleanup_committed(
        self,
        record,
        owner,
        stream,
        identity,
        *,
        shutdown,
        cleanup_timeout=1.0,
        intent=None,
    ):
        del cleanup_timeout
        with self._lock:
            current = self._leases.get(record.run_id)
            if current is not record:
                return False
            current.settled = False
            existing = current.settlement_claim is not None
            reason = (
                "pool shutdown canceled capture"
                if shutdown
                else current.cancel_reason or "capture canceled"
            )
            code = (
                CaptureErrorCode.POOL_SHUTTING_DOWN
                if shutdown
                else CaptureErrorCode.CAPTURE_CANCELED
            )
        if existing and shutdown:
            with self._lock:
                return not self._lease_cleanup_pending_locked(record)
        if existing:
            result = self._advance_settlement(record.run_id)
        else:
            result = self._submit_settlement(
                record,
                intent
                or (
                    SettlementIntent.POOL_SHUTDOWN
                    if shutdown
                    else SettlementIntent.CLIENT_CANCEL
                ),
                LeaseState.CANCELED,
                code,
                reason,
            )
        return result is not None and result.settled

    def _retry_lease_cleanup(
        self, record, cleanup_timeout=1.0
    ) -> bool:
        del cleanup_timeout
        with self._lock:
            current = self._leases.get(record.run_id)
            if current is not record or current.settlement_claim is None:
                return False
        result = self._advance_settlement(record.run_id)
        return result is not None and result.settled


__all__ = ["ResidentPoolCleanupMixin"]
