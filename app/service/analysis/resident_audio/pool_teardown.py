"""Positive shutdown teardown proof and phased settlement release."""

from __future__ import annotations

import threading

from .contracts import CaptureErrorCode, LeaseState
from .pool_types import PoolState


class ResidentPoolTeardownMixin:
    def _has_retryable_shutdown_resources_locked(self) -> bool:
        return bool(
            not self._shutdown_run_started
            or self._physical_close_gate.active_lease_count
            or self._shutdown_close_resources
            or any(
                resource.worker.is_alive()
                for resource in self._execution_resources.values()
            )
            or any(worker.is_alive() for worker in self._control_threads)
            or (
                self._artifact_cleanup_work is not None
                and (
                    not self._artifact_cleanup_work.settled.is_set()
                    or self._artifact_cleanup_work.worker.is_alive()
                    or not self._artifact_cleanup_work.harvested
                    or self._artifact_cleanup_work.exception_type is not None
                    or bool(self._artifact_cleanup_report.failed_paths)
                    or bool(self._pending_artifact_cleanup_ids)
                )
            )
            or not self._force_termination_complete()
            or self._shutdown_pending_streams
            or self._unpublished_closes
            or self._closing_streams
            or self._shutdown_incomplete_resources.get(
                "unsettled_leases", 0
            )
            or any(
                self._lease_cleanup_pending_locked(record)
                for record in self._leases.values()
            )
        )

    def _harvest_finished_workers_locked(self) -> None:
        for event, resource in tuple(self._execution_resources.items()):
            if event.is_set() and not resource.worker.is_alive():
                self._execution_resources.pop(event, None)
                self._active_control_executions.discard(event)
                self._terminal_executions.discard(event)
        for worker in tuple(self._control_threads):
            if not worker.is_alive():
                self._control_threads.discard(worker)
                self._unregister_control_worker_locked(worker)

    def _join_tracked_workers(self, deadline) -> None:
        with self._lock:
            resources = tuple(self._execution_resources.values())
            controls = tuple(self._control_threads)
        current = threading.current_thread()
        workers = {
            resource.worker for resource in resources
            if resource.worker is not current
        }
        workers.update(worker for worker in controls if worker is not current)
        for worker in workers:
            worker.join(self._shutdown_remaining(deadline))
        with self._lock:
            self._harvest_finished_workers_locked()

    def _join_commit_workers(self, records, deadline) -> None:
        current = threading.current_thread()
        workers = {
            record.commit_worker
            for record in records
            if (
                record.commit_worker is not None
                and record.commit_worker is not current
            )
        }
        for worker in workers:
            worker.join(self._shutdown_remaining(deadline))

    def _stream_teardown_is_proven(self) -> bool:
        live_streams = self._collect_live_shutdown_resources()
        with self._lock:
            self._harvest_finished_workers_locked()
            live_executions = any(
                resource.worker.is_alive()
                for resource in self._execution_resources.values()
                if resource.kind in ("control", "resolver")
            )
            return not (
                live_streams
                or live_executions
                or self._active_control_executions
                or self._opening_attempts
                or self._closing_streams
                or self._control_threads
                or self._shutdown_close_resources
                or self._shutdown_pending_streams
                or self._unpublished_closes
            )

    def _owner_teardown_is_proven(self) -> bool:
        with self._lock:
            records = tuple(self._leases.values())
            claims = tuple(
                record.settlement_claim
                for record in records
                if record.settlement_claim is not None
            )
        for claim in claims:
            if not claim.progress.owner_joined:
                return False
        for record in records:
            worker = record.commit_worker
            if record.state.value == "COMMITTING" and (
                worker is not None and worker.is_alive()
            ):
                return False
            owner = record.owner
            if owner is not None and owner.snapshot().drain_thread_alive:
                return False
        return True

    def _advance_shutdown_teardown(self, deadline) -> bool:
        self._join_tracked_workers(deadline)
        with self._lock:
            committing_records = tuple(
                record
                for record in self._leases.values()
                if record.state is LeaseState.COMMITTING
            )
        self._join_commit_workers(committing_records, deadline)
        stream_complete = self._stream_teardown_is_proven()
        with self._lock:
            self._shutdown_stream_teardown_complete = stream_complete
            self._shutdown_teardown_complete = False
        if not stream_complete:
            return False
        self._run_settlement_work(deadline)
        self._join_tracked_workers(deadline)
        owner_complete = self._owner_teardown_is_proven()
        with self._lock:
            self._shutdown_teardown_complete = owner_complete
        if owner_complete:
            self._run_settlement_work(deadline)
            self._join_tracked_workers(deadline)
        return owner_complete

    def _retry_failed_shutdown(self, deadline):
        self._harvest_force_terminate_work()
        with self._lock:
            self._harvest_finished_workers_locked()
            barrier_busy = any(
                resource.kind == "shutdown-begin"
                and resource.worker.is_alive()
                for resource in self._control_worker_resources.values()
            )
            pending_streams = tuple(
                self._shutdown_pending_streams.values()
            )
        barrier_complete = not pending_streams
        if pending_streams and not barrier_busy:
            barrier_complete = self._run_stream_shutdown_barrier(
                pending_streams, deadline
            )
        authorize_in_progress = []
        with self._lock:
            candidates = [
                tracked.stream
                for tracked in self._shutdown_close_resources.values()
                if tracked.worker is None or not tracked.worker.is_alive()
            ]
            candidates.extend(
                tracked.stream
                for tracked in self._unpublished_closes.values()
                if not tracked.in_progress
            )
            candidates.extend(
                tracked.record.stream
                for tracked in self._closing_streams.values()
                if not tracked.in_progress
            )
            if barrier_complete:
                authorize_in_progress.extend(
                    tracked.record.stream
                    for tracked in self._closing_streams.values()
                    if tracked.in_progress
                )
                authorize_in_progress.extend(
                    tracked.stream
                    for tracked in self._unpublished_closes.values()
                    if (
                        tracked.in_progress
                        and tracked.open_attempt is None
                    )
                )
                in_progress_ids = {
                    id(tracked.stream)
                    for tracked in self._unpublished_closes.values()
                    if tracked.in_progress
                }
                in_progress_ids.update(
                    id(tracked.record.stream)
                    for tracked in self._closing_streams.values()
                    if tracked.in_progress
                )
                candidates.extend(
                    stream
                    for stream in pending_streams
                    if (
                        id(stream) not in self._shutdown_close_resources
                        and id(stream) not in in_progress_ids
                    )
                )
        for stream in authorize_in_progress:
            self._physical_close_gate.authorize(stream)
            stream.control_authorize_shutdown_close()
        unique = tuple({id(stream): stream for stream in candidates}.values())
        if barrier_complete:
            for stream in unique:
                self._physical_close_gate.authorize(stream)
                stream.control_authorize_shutdown_close()
        retries = [
            (
                stream,
                self._schedule_shutdown_close(
                    stream, self._shutdown_remaining(deadline)
                ),
            )
            for stream in unique
        ]
        closed_ids = set()
        for stream, tracked in retries:
            tracked.settled_event.wait(self._shutdown_remaining(deadline))
            if tracked.settled_event.is_set() and tracked.closed:
                closed_ids.add(id(stream))
        with self._lock:
            for hardware_id, tracked in tuple(
                self._closing_streams.items()
            ):
                if id(tracked.record.stream) in closed_ids:
                    self._closing_streams.pop(hardware_id, None)
                    tracked.settled_event.set()
        self._advance_shutdown_teardown(deadline)
        with self._lock:
            self._harvest_finished_workers_locked()
            lease_cleanup_complete = not any(
                self._lease_cleanup_pending_locked(record)
                for record in self._leases.values()
            )
        artifact_cleanup_complete = False
        if lease_cleanup_complete and self._shutdown_teardown_complete:
            artifact_cleanup_complete = self._run_artifact_cleanup(
                deadline, management=True
            )
        with self._lock:
            self._harvest_finished_workers_locked()
            clean = (
                self._shutdown_teardown_complete
                and not self._active_control_executions
                and not self._opening_attempts
                and not self._closing_streams
                and not self._control_threads
                and not self._shutdown_close_resources
                and not self._shutdown_pending_streams
                and not self._unpublished_closes
                and not self._terminal_executions
                and not any(
                    self._lease_cleanup_pending_locked(record)
                    for record in self._leases.values()
                )
                and artifact_cleanup_complete
                and self._force_termination_complete()
            )
            self._state = (
                PoolState.CLOSED if clean else PoolState.FAILED_CLOSED
            )
            self._shutdown_error_code = (
                None
                if clean
                else CaptureErrorCode.SHUTDOWN_INCOMPLETE
            )
            if clean:
                self._shutdown_resources = ()
        return self.snapshot()


__all__ = ["ResidentPoolTeardownMixin"]
