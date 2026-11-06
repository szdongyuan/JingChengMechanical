"""Forced shutdown grace and exact remaining-resource reporting."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .artifact_cleanup import ArtifactCleanupReport
from .contracts import ArtifactState, CaptureErrorCode
from .pool_types import PoolState


@dataclass(slots=True)
class _ShutdownWork:
    kind: str
    worker: threading.Thread
    settled: threading.Event
    result: object | None = None
    exception_type: str | None = None
    harvested: bool = False


class ResidentPoolShutdownMixin:
    def shutdown(self):
        owns_shutdown = False
        retries_shutdown = False
        deadline = None
        with self._lock:
            if self._shutdown_transitioning:
                settled = self._shutdown_settled
            elif self._shutdown_result is not None:
                if (
                    self._state is not PoolState.FAILED_CLOSED
                    or not self._has_retryable_shutdown_resources_locked()
                ):
                    return self._shutdown_result
                deadline = (
                    self._shutdown_clock()
                    + self._shutdown_control_timeout
                )
                self._physical_close_gate.begin_shutdown()
                self._shutdown_transitioning = True
                self._shutdown_settled = self._new_event()
                self._shutdown_result = None
                settled = self._shutdown_settled
                owns_shutdown = True
                retries_shutdown = True
            elif self._state is not PoolState.OPEN:
                settled = self._shutdown_settled
            else:
                deadline = (
                    self._shutdown_clock()
                    + self._shutdown_control_timeout
                )
                self._physical_close_gate.begin_shutdown()
                self._shutdown_transitioning = True
                self._shutdown_settled = self._new_event()
                self._shutdown_result = None
                settled = self._shutdown_settled
                owns_shutdown = True
        if not owns_shutdown:
            settled.wait()
            with self._lock:
                return self._shutdown_result or self.snapshot()

        assert deadline is not None
        drained = self._physical_close_gate.wait_for_drain(
            self._shutdown_remaining(deadline)
        )
        if not drained:
            with self._lock:
                self._shutdown_transitioning = False
                self._state = PoolState.FAILED_CLOSED
                self._shutdown_error_code = (
                    CaptureErrorCode.SHUTDOWN_INCOMPLETE
                )
                self._shutdown_incomplete_resources[
                    "physical_close_leases"
                ] = self._physical_close_gate.active_lease_count
                self._shutdown_result = self.snapshot()
                settled.set()
                return self._shutdown_result

        with self._lock:
            self._shutdown_transitioning = False
            self._state = PoolState.SHUTTING_DOWN
            full_shutdown = (
                not retries_shutdown or not self._shutdown_run_started
            )
            if full_shutdown:
                self._shutdown_run_started = True
        try:
            result = (
                self._run_shutdown(deadline)
                if full_shutdown
                else self._retry_failed_shutdown(deadline)
            )
            result = self._force_shutdown_if_required(result, deadline)
        except BaseException:
            with self._lock:
                self._state = PoolState.FAILED_CLOSED
                self._shutdown_result = self.snapshot()
                settled.set()
                return self._shutdown_result
        with self._lock:
            self._shutdown_result = result
            settled.set()
        return result

    def _run_stream_shutdown_barrier(self, streams, deadline) -> bool:
        work = []
        for stream in streams:
            stream.control_require_shutdown_close_authorization()
        for index, stream in enumerate(streams):
            settled = threading.Event()
            outcome = {"accepted": False}

            def begin_shutdown(
                _stream=stream,
                _settled=settled,
                _outcome=outcome,
            ):
                try:
                    _stream.control_begin_shutdown()
                    _outcome["accepted"] = True
                except Exception:
                    pass
                finally:
                    _settled.set()

            worker = threading.Thread(
                target=begin_shutdown,
                name=f"resident-audio-shutdown-begin-{index}",
                daemon=True,
            )
            identity = self._stream_resource_identity(stream)
            with self._lock:
                worker.start()
                self._control_threads.add(worker)
                self._register_control_worker_locked(
                    worker,
                    "shutdown-begin",
                    (identity,),
                )
            work.append((worker, settled, outcome))
        for _worker, settled, _outcome in work:
            settled.wait(self._shutdown_remaining(deadline))
        for worker, _settled, _outcome in work:
            worker.join(self._shutdown_remaining(deadline))
        with self._lock:
            self._harvest_finished_workers_locked()
        return all(
            settled.is_set()
            and not worker.is_alive()
            and outcome["accepted"]
            for worker, settled, outcome in work
        )

    def _run_pre_teardown_settlement_work(
        self, work, deadline, run_ids
    ) -> bool:
        execution = self._new_event()

        def settle_before_teardown():
            try:
                work()
            finally:
                with self._lock:
                    execution.set()

        worker = threading.Thread(
            target=settle_before_teardown,
            name="resident-audio-shutdown-pre-teardown-settlement",
            daemon=True,
        )
        with self._lock:
            worker.start()
            self._terminal_executions.add(execution)
            self._register_execution_locked(
                execution,
                "terminal",
                worker,
                self._execution_identities_for_run_ids_locked(run_ids),
            )
        execution.wait(self._shutdown_remaining(deadline))
        return execution.is_set()

    def _run_settlement_work(self, deadline) -> bool:
        with self._lock:
            run_ids = tuple(
                record.run_id
                for record in self._leases.values()
                if record.settlement_claim is not None
            )
        execution = self._schedule_settlement_advances(run_ids)
        if execution is None:
            return True
        execution.wait(self._shutdown_remaining(deadline))
        return execution.is_set()

    def _force_shutdown_if_required(self, result, cooperative_deadline):
        resources = self._collect_live_shutdown_resources()
        had_resources = bool(resources)
        if had_resources:
            remaining = self._shutdown_remaining(cooperative_deadline)
            if remaining > 0:
                threading.Event().wait(remaining)
            resources = self._collect_live_shutdown_resources()
            if not resources:
                result = self._retry_failed_shutdown(cooperative_deadline)
        with self._lock:
            self._shutdown_resources = resources
            needs_force = bool(resources) and not self._force_terminate_called
            if needs_force:
                self._force_terminate_called = True
        if not needs_force:
            complete_resources = self._collect_shutdown_resources()
            with self._lock:
                self._shutdown_resources = complete_resources
            return self.snapshot()
        retried = result
        grace_deadline = self._shutdown_clock() + self._shutdown_force_grace
        self._start_force_terminate_work()
        force_work = self._force_terminate_work
        if force_work is not None:
            force_work.settled.wait(self._shutdown_remaining(grace_deadline))
            force_work.worker.join(
                self._shutdown_remaining(grace_deadline)
            )
            self._harvest_force_terminate_work()
        self._wait_for_shutdown_controls(grace_deadline)
        if force_work is None or not force_work.worker.is_alive():
            retried = self._retry_failed_shutdown(grace_deadline)
        resources = self._collect_shutdown_resources()
        with self._lock:
            self._shutdown_resources = resources
            if retried.state is PoolState.CLOSED:
                self._shutdown_error_code = None
            else:
                self._shutdown_error_code = CaptureErrorCode.SHUTDOWN_INCOMPLETE
        return self.snapshot()

    def _start_force_terminate_work(self) -> None:
        existing = self._force_terminate_work
        if existing is not None:
            return
        settled = threading.Event()

        def invoke_force():
            work = self._force_terminate_work
            try:
                force = getattr(self._backend, "force_terminate", None)
                if callable(force):
                    force()
                    if work is not None:
                        work.result = True
                elif work is not None:
                    work.exception_type = "MissingForceTerminate"
            except Exception as error:
                if work is not None:
                    work.exception_type = type(error).__name__[:256]
            finally:
                settled.set()

        worker = threading.Thread(
            target=invoke_force,
            name="resident-audio-backend-force-terminate",
            daemon=True,
        )
        self._force_terminate_work = _ShutdownWork(
            "backend-force-terminate", worker, settled
        )
        worker.start()

    def _run_artifact_cleanup(self, deadline, *, management: bool) -> bool:
        self._harvest_artifact_cleanup_work()
        if self._artifact_cleanup_complete():
            return True
        work = self._artifact_cleanup_work
        if work is None or (
            work.settled.is_set() and not work.worker.is_alive()
        ):
            targets = (
                tuple(sorted(self._pending_artifact_cleanup_ids))
                if management
                else ()
            )
            settled = threading.Event()

            def clean_artifacts():
                current = self._artifact_cleanup_work
                try:
                    if targets:
                        reports = tuple(
                            self._artifact_store.retry_delete_pending_artifact(
                                artifact_id
                            )
                            for artifact_id in targets
                        )
                        result = self._combine_cleanup_reports(reports)
                    else:
                        result = self._artifact_store.begin_closing()
                    if current is not None:
                        current.result = result
                except Exception as error:
                    if current is not None:
                        current.exception_type = type(error).__name__[:256]
                finally:
                    settled.set()

            worker = threading.Thread(
                target=clean_artifacts,
                name="resident-audio-artifact-cleanup",
                daemon=True,
            )
            work = _ShutdownWork("artifact-cleanup", worker, settled)
            self._artifact_cleanup_work = work
            worker.start()
        work.settled.wait(self._shutdown_remaining(deadline))
        work.worker.join(self._shutdown_remaining(deadline))
        self._harvest_artifact_cleanup_work()
        return self._artifact_cleanup_complete()

    def _harvest_force_terminate_work(self) -> None:
        work = self._force_terminate_work
        if (
            work is None
            or not work.settled.is_set()
            or work.worker.is_alive()
            or work.harvested
        ):
            return
        if work.result is not True and work.exception_type is None:
            work.exception_type = "ForceTerminateResultRejected"
        work.harvested = True

    def _force_termination_complete(self) -> bool:
        work = self._force_terminate_work
        return work is None or bool(
            work.settled.is_set()
            and not work.worker.is_alive()
            and work.harvested
            and work.result is True
            and work.exception_type is None
        )

    def _harvest_artifact_cleanup_work(self) -> None:
        work = self._artifact_cleanup_work
        if (
            work is None
            or not work.settled.is_set()
            or work.worker.is_alive()
            or work.harvested
        ):
            return
        report = work.result
        if isinstance(report, ArtifactCleanupReport):
            if self._pending_artifact_cleanup_ids:
                self._artifact_cleanup_report = self._merge_cleanup_retry(
                    self._artifact_cleanup_report, report
                )
            else:
                self._artifact_cleanup_report = report
        elif work.exception_type is None:
            work.exception_type = type(report).__name__[:256]
        known = self._known_artifact_ids()
        pending = set()
        for artifact_id in known:
            try:
                if (
                    self._artifact_store.state(artifact_id)
                    is ArtifactState.DELETE_PENDING
                ):
                    pending.add(artifact_id)
            except Exception as error:
                work.exception_type = type(error).__name__[:256]
                continue
        self._pending_artifact_cleanup_ids = pending
        work.harvested = True

    def _artifact_cleanup_complete(self) -> bool:
        work = self._artifact_cleanup_work
        if (
            work is None
            or not work.settled.is_set()
            or work.worker.is_alive()
        ):
            return False
        if work.exception_type is not None:
            return False
        if not isinstance(work.result, ArtifactCleanupReport):
            return False
        if self._artifact_cleanup_report.failed_paths:
            return False
        for artifact_id in self._known_artifact_ids():
            try:
                state = self._artifact_store.state(artifact_id)
            except Exception as error:
                work.exception_type = type(error).__name__[:256]
                return False
            if state in (
                ArtifactState.PARTIAL,
                ArtifactState.READY_UNCLAIMED,
                ArtifactState.DELETE_PENDING,
            ):
                return False
        return True

    def _known_artifact_ids(self):
        with self._lock:
            values = []
            for record in self._leases.values():
                summary = record.terminal_summary
                values.extend(
                    (
                        record.artifact_id,
                        getattr(record.outcome.artifact, "artifact_id", None),
                        getattr(summary, "artifact_id", None),
                    )
                )
        return tuple(sorted({value for value in values if value}))

    @staticmethod
    def _combine_cleanup_reports(reports):
        return ArtifactCleanupReport(
            deleted_paths=tuple(
                path for report in reports for path in report.deleted_paths
            ),
            failed_paths=tuple(
                path for report in reports for path in report.failed_paths
            ),
            retained_paths=tuple(
                path for report in reports for path in report.retained_paths
            ),
            attempted_paths=tuple(
                path for report in reports for path in report.attempted_paths
            ),
        )

    @staticmethod
    def _merge_cleanup_retry(previous, retry):
        attempted = set(retry.attempted_paths) | set(retry.deleted_paths)
        return ArtifactCleanupReport(
            deleted_paths=tuple(
                dict.fromkeys(previous.deleted_paths + retry.deleted_paths)
            ),
            failed_paths=tuple(
                path
                for path in dict.fromkeys(
                    previous.failed_paths + retry.failed_paths
                )
                if path not in attempted or path in retry.failed_paths
            ),
            retained_paths=previous.retained_paths,
            attempted_paths=tuple(
                dict.fromkeys(previous.attempted_paths + retry.attempted_paths)
            ),
        )

    def _wait_for_shutdown_controls(self, deadline) -> None:
        with self._lock:
            tracked = tuple(self._shutdown_close_resources.values())
        for close in tracked:
            close.settled_event.wait(self._shutdown_remaining(deadline))
        with self._lock:
            workers = tuple(self._control_threads)
        for worker in workers:
            worker.join(self._shutdown_remaining(deadline))
        with self._lock:
            self._harvest_finished_workers_locked()

    def _shutdown_remaining(self, deadline) -> float:
        return max(0.0, float(deadline) - float(self._shutdown_clock()))


__all__ = ["ResidentPoolShutdownMixin"]
