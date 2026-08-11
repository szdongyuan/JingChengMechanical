"""Lock-safe public lease diagnostics for the resident pool."""

from __future__ import annotations

from .contracts import LeaseState, StreamState
from .pool_types import (
    LeaseSnapshot,
    PoolStreamSnapshot,
    ResidentAudioPoolSnapshot,
)


class ResidentPoolDiagnosticsMixin:
    def _snapshot_cached_locked(self):
        counts = self._lease_counts_locked()
        return ResidentAudioPoolSnapshot(
            pool_epoch=self._pool_epoch,
            state=self._state,
            desired_hardware_ids=tuple(sorted(self._desired)),
            published_hardware_ids=tuple(sorted(self._streams)),
            streams=tuple(
                self._stream_diagnostics[key]
                for key in sorted(self._stream_diagnostics)
                if key in self._desired
            ),
            reserved_leases=counts[LeaseState.RESERVED],
            committing_leases=counts[LeaseState.COMMITTING],
            committed_leases=counts[LeaseState.COMMITTED],
            terminal_diagnostic_count=len(self._terminal_order),
            unsettled_transactions=sum(
                self._lease_cleanup_pending_locked(lease)
                for lease in self._leases.values()
            ),
            shutdown_error_code=self._shutdown_error_code,
            shutdown_resources=self._shutdown_resources,
            cleanup_deleted_paths=(
                self._artifact_cleanup_report.deleted_paths
            ),
            cleanup_failed_paths=self._artifact_cleanup_report.failed_paths,
            cleanup_retained_paths=(
                self._artifact_cleanup_report.retained_paths
            ),
            cleanup_exception_type=(
                None
                if self._artifact_cleanup_work is None
                else self._artifact_cleanup_work.exception_type
            ),
            settlement_diagnostics=(
                self._settlement_coordinator.diagnostics()
            ),
        )

    @staticmethod
    def _opening_snapshot(attempt):
        return PoolStreamSnapshot(
            attempt.hardware_id,
            attempt.desired_revision,
            attempt.resolution_attempt_id,
            attempt.stream_generation or 0,
            StreamState.OPENING,
            0,
            False,
            False,
        )

    @staticmethod
    def _failed_open_snapshot(attempt, code, message):
        return PoolStreamSnapshot(
            attempt.hardware_id,
            attempt.desired_revision,
            attempt.resolution_attempt_id,
            attempt.stream_generation or 0,
            StreamState.FAILED,
            0,
            False,
            False,
            code,
            message,
        )

    @staticmethod
    def _pool_stream_snapshot_from_record(
        hardware_id, record, stream_snapshot, *, error_code=None, message=None
    ):
        return PoolStreamSnapshot(
            hardware_id,
            record.desired_revision,
            record.resolution_attempt_id,
            record.stream_generation,
            stream_snapshot.state,
            stream_snapshot.owner_count,
            stream_snapshot.state is StreamState.READY,
            True,
            error_code or stream_snapshot.failure_code,
            message,
        )

    def snapshot(self) -> ResidentAudioPoolSnapshot:
        with self._lock:
            pool_state = self._state
            desired_ids = tuple(sorted(self._desired))
            published_ids = tuple(sorted(self._streams))
            records = tuple(self._streams.items())
            diagnostics = dict(self._stream_diagnostics)
            counts = self._lease_counts_locked()
            terminal_count = len(self._terminal_order)
            unsettled = sum(
                self._lease_cleanup_pending_locked(lease)
                for lease in self._leases.values()
            )
        for hardware_id, record in records:
            stream_snapshot = record.stream.snapshot()
            prior = diagnostics.get(hardware_id)
            diagnostics[hardware_id] = PoolStreamSnapshot(
                hardware_id=hardware_id,
                desired_revision=record.desired_revision,
                resolution_attempt_id=record.resolution_attempt_id,
                stream_generation=record.stream_generation,
                state=stream_snapshot.state,
                owner_count=stream_snapshot.owner_count,
                ready=stream_snapshot.state is StreamState.READY,
                published=True,
                error_code=stream_snapshot.failure_code,
                message=None,
                retry_attempt=0 if prior is None else prior.retry_attempt,
                retry_delay=None if prior is None else prior.retry_delay,
                exception_type=None if prior is None else prior.exception_type,
                sample_rate=stream_snapshot.sample_rate,
                channels=stream_snapshot.channels,
                callback_frames=stream_snapshot.callback_frames,
                ring_earliest_sample=stream_snapshot.ring_earliest_sample,
                ring_latest_sample=stream_snapshot.ring_latest_sample,
                ring_earliest_monotonic=(
                    stream_snapshot.ring_earliest_monotonic
                ),
                ring_latest_monotonic=(
                    stream_snapshot.ring_latest_monotonic
                ),
                ingress_depth=stream_snapshot.ingress_depth,
                command_depth=stream_snapshot.command_depth,
                old_generation_drops=stream_snapshot.old_generation_drops,
                overflow_count=stream_snapshot.overflow_count,
                recovery_count=stream_snapshot.recovery_count,
                backlog_count=stream_snapshot.backlog_count,
                finality_entry_count=stream_snapshot.finality_entry_count,
                previous_state=(
                    None
                    if prior is None
                    else (
                        prior.previous_state
                        if prior.state is stream_snapshot.state
                        else prior.state
                    )
                ),
            )
        streams = tuple(
            diagnostics[hardware_id]
            for hardware_id in sorted(diagnostics)
            if hardware_id in desired_ids
        )
        settlement_diagnostics = self._settlement_coordinator.diagnostics()
        return ResidentAudioPoolSnapshot(
            pool_epoch=self._pool_epoch,
            state=pool_state,
            desired_hardware_ids=desired_ids,
            published_hardware_ids=published_ids,
            streams=streams,
            reserved_leases=counts[LeaseState.RESERVED],
            committing_leases=counts[LeaseState.COMMITTING],
            committed_leases=counts[LeaseState.COMMITTED],
            terminal_diagnostic_count=terminal_count,
            unsettled_transactions=unsettled,
            shutdown_error_code=self._shutdown_error_code,
            shutdown_resources=self._shutdown_resources,
            cleanup_deleted_paths=(
                self._artifact_cleanup_report.deleted_paths
            ),
            cleanup_failed_paths=self._artifact_cleanup_report.failed_paths,
            cleanup_retained_paths=(
                self._artifact_cleanup_report.retained_paths
            ),
            cleanup_exception_type=(
                None
                if self._artifact_cleanup_work is None
                else self._artifact_cleanup_work.exception_type
            ),
            settlement_diagnostics=settlement_diagnostics,
        )

    def lease_snapshot(self, run_id: str) -> LeaseSnapshot | None:
        run_id = self._validated_run_id(run_id)
        with self._lock:
            record = self._leases.get(run_id)
            if record is None:
                return None
            values = (
                record.run_id,
                record.state,
                record.commit_attempt_id,
                record.artifact_id,
                record.cancel_requested,
                record.settled,
                record.previous_state,
                getattr(record.owner_instance_id, "hardware_id", None),
                record.stream_generation,
                record.artifact_consumer_id,
                record.outcome.error_code,
            )
            writer = record.writer
            artifact = record.outcome.artifact
            owner = record.owner
        owner_snapshot = None if owner is None else owner.snapshot()
        artifact_id = values[3]
        artifact_state = (
            None
            if artifact_id is None
            else self._artifact_store.state(artifact_id)
        )
        path = getattr(writer, "partial_path", None)
        if artifact is not None:
            path = artifact.path
        return LeaseSnapshot(
            run_id=values[0],
            state=values[1],
            commit_attempt_id=values[2],
            artifact_id=artifact_id,
            cancel_requested=values[4],
            settled=values[5],
            artifact_state=artifact_state,
            artifact_path=None if path is None else str(path),
            previous_state=values[6],
            hardware_id=values[7],
            stream_generation=values[8],
            owner_snapshot=owner_snapshot,
            artifact_consumer_id=values[9],
            error_code=values[10],
        )


__all__ = ["ResidentPoolDiagnosticsMixin"]
