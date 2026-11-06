"""Resident stream registry, sole capture lease authority, and shutdown boundary."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Callable

from .artifact_cleanup import ArtifactCleanupReport
from .contracts import CaptureErrorCode, LeaseState
from .pool_cleanup import ResidentPoolCleanupMixin
from .pool_close_gate import PoolPhysicalCloseGate
from .pool_commit import ResidentPoolCommitMixin
from .pool_control import ResidentPoolControlMixin
from .pool_diagnostics import ResidentPoolDiagnosticsMixin
from .pool_reconcile import ResidentPoolReconcileMixin
from .pool_retry import ResidentPoolRetryMixin, ThreadingRetryScheduler
from .pool_resources import ResidentPoolResourceMixin
from .pool_shutdown import ResidentPoolShutdownMixin
from .pool_stream_events import ResidentPoolStreamEventsMixin
from .pool_teardown import ResidentPoolTeardownMixin
from .pool_settlement import (
    PoolSettlementAdapter,
    ResidentPoolSettlementMixin,
)
from .owner_settlement import OwnerSettlementCoordinator
from .owner_settlement_types import SettlementIntent
from .pool_types import (
    DEFAULT_TERMINAL_DIAGNOSTIC_LIMIT,
    DesiredHardware,
    LeaseOutcome,
    PoolState,
    PoolStreamSnapshot,
    ReconcileResult,
    _ACTIVE_LEASE_STATES,
    _LeaseRecord,
)
from .stream import ResidentStream


class ResidentAudioPool(
    ResidentPoolDiagnosticsMixin,
    ResidentPoolCommitMixin,
    ResidentPoolCleanupMixin,
    ResidentPoolControlMixin,
    ResidentPoolReconcileMixin,
    ResidentPoolRetryMixin,
    ResidentPoolResourceMixin,
    ResidentPoolTeardownMixin,
    ResidentPoolShutdownMixin,
    ResidentPoolStreamEventsMixin,
    ResidentPoolSettlementMixin,
):
    """Own all resident streams and the product's only six-capture table."""

    def _pool_is_open_locked(self) -> bool:
        return (
            self._state is PoolState.OPEN
            and not self._shutdown_transitioning
        )

    def __init__(
        self,
        *,
        resolver,
        backend,
        artifact_store,
        monotonic_clock: Callable[[], float] = time.monotonic,
        completion_handler=None,
        stream_factory=ResidentStream,
        terminal_diagnostic_limit: int = DEFAULT_TERMINAL_DIAGNOSTIC_LIMIT,
        reconcile_ready_timeout: float = 2.0,
        retry_scheduler=None,
        shutdown_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(getattr(resolver, "resolve", None)):
            raise TypeError("resolver must provide resolve")
        if not callable(getattr(backend, "open_input", None)):
            raise TypeError("backend must provide open_input")
        if not callable(getattr(artifact_store, "allocate_writer", None)):
            raise TypeError("artifact_store must provide allocate_writer")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        if not callable(shutdown_clock):
            raise TypeError("shutdown_clock must be callable")
        if not callable(stream_factory):
            raise TypeError("stream_factory must be callable")
        retry_scheduler = retry_scheduler or ThreadingRetryScheduler()
        if not callable(getattr(retry_scheduler, "schedule", None)):
            raise TypeError("retry_scheduler must provide schedule")
        if (
            isinstance(terminal_diagnostic_limit, bool)
            or not isinstance(terminal_diagnostic_limit, int)
            or terminal_diagnostic_limit < 1
        ):
            raise ValueError("terminal_diagnostic_limit must be positive")
        if (
            isinstance(reconcile_ready_timeout, bool)
            or not isinstance(reconcile_ready_timeout, (int, float))
            or float(reconcile_ready_timeout) <= 0
        ):
            raise ValueError("reconcile_ready_timeout must be positive")

        self._resolver = resolver
        self._backend = backend
        self._artifact_store = artifact_store
        self._clock = monotonic_clock
        self._stream_factory = stream_factory
        self._completion_handler = completion_handler or (
            lambda completion: completion.decline("no completion handler")
        )
        self._terminal_diagnostic_limit = terminal_diagnostic_limit
        self._reconcile_ready_timeout = float(reconcile_ready_timeout)
        self._retry_scheduler = retry_scheduler
        self._shutdown_clock = shutdown_clock
        self._physical_close_gate = PoolPhysicalCloseGate()

        self._pool_epoch = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._state = PoolState.OPEN
        self._desired: dict[str, DesiredHardware] = {}
        self._desired_revision_clock = 0
        self._hardware_desired_revisions: dict[str, int] = {}
        self._generation_counters: dict[str, int] = {}
        self._resolution_attempt_counters: dict[str, int] = {}
        self._opening_attempts = {}
        self._streams = {}
        self._closing_streams = {}
        self._pending_removals = []
        self._pending_reconcile = None
        self._control_threads = set()
        self._shutdown_close_resources = {}
        self._shutdown_pending_streams = {}
        self._unpublished_closes = {}
        self._shutdown_incomplete_resources = {}
        self._active_control_executions = set()
        self._terminal_executions = set()
        self._execution_resources = {}
        self._control_worker_resources = {}
        self._shutdown_stream_teardown_complete = False
        self._shutdown_control_timeout = 5.0
        self._shutdown_force_grace = 1.0
        self._force_terminate_called = False
        self._shutdown_teardown_complete = False
        self._shutdown_error_code = None
        self._shutdown_resources = ()
        self._artifact_cleanup_report = ArtifactCleanupReport()
        self._artifact_cleanup_work = None
        self._pending_artifact_cleanup_ids = set()
        self._force_terminate_work = None
        self._shutdown_settled = self._new_event()
        self._shutdown_result = None
        self._shutdown_transitioning = False
        self._shutdown_run_started = False
        self._stream_diagnostics: dict[str, PoolStreamSnapshot] = {}
        self._retry_backoffs = {}
        self._scheduled_retries = {}
        self._leases: dict[str, _LeaseRecord] = {}
        self._terminal_order: OrderedDict[str, None] = OrderedDict()
        self._settlement_adapter = PoolSettlementAdapter(self)
        self._settlement_coordinator = OwnerSettlementCoordinator(
            claims=self._settlement_adapter,
            finality=self._settlement_adapter,
            owners=self._settlement_adapter,
            artifacts=self._settlement_adapter,
            terminal=self._settlement_adapter,
        )

    @property
    def pool_epoch(self) -> str:
        return self._pool_epoch

    def set_completion_handler(self, handler) -> None:
        if not callable(handler):
            raise TypeError("completion handler must be callable")
        with self._lock:
            if not self._pool_is_open_locked():
                raise RuntimeError("completion handler cannot change during shutdown")
            self._completion_handler = handler

    def ready_token(self, hardware_id: str):
        with self._lock:
            record = self._streams.get(hardware_id)
            stream = None if record is None else record.stream
        return None if stream is None else stream.current_ready_token

    def synchronize_stream(self, hardware_id: str, timeout=None) -> bool:
        with self._lock:
            record = self._streams.get(hardware_id)
            stream = None if record is None else record.stream
        return bool(stream and stream.synchronize(timeout))

    def stream_for_testing(self, hardware_id: str):
        """Test seam for deterministic command barriers; not a capacity authority."""

        with self._lock:
            record = self._streams.get(hardware_id)
            return None if record is None else record.stream

    def _run_shutdown(self, deadline):
        with self._lock:
            self._shutdown_stream_teardown_complete = False
            self._shutdown_teardown_complete = False
            committing_events = []
            pre_teardown_events = []
            pending_cleanups = []
            for record in tuple(self._leases.values()):
                if record.state is LeaseState.RESERVED:
                    self._ownerless_reserved_terminal_locked(
                        record,
                        LeaseState.CANCELED,
                        CaptureErrorCode.POOL_SHUTTING_DOWN,
                        "pool shutdown canceled reserved capture",
                    )
                elif record.state is LeaseState.COMMITTING:
                    record.cancel_requested = True
                    record.shutdown_requested = True
                    record.cancel_reason = "pool shutdown canceled capture commit"
                    if record.commit_settled is not None:
                        committing_events.append(record.commit_settled)
                    if record.pre_teardown_settled is not None:
                        pre_teardown_events.append(
                            record.pre_teardown_settled
                        )
                    if record.settlement_claim is not None:
                        pending_cleanups.append(record)

        if pending_cleanups:
            self._run_pre_teardown_settlement_work(
                lambda: tuple(
                    self._retry_lease_cleanup(record)
                    for record in pending_cleanups
                ),
                deadline,
                tuple(record.run_id for record in pending_cleanups),
            )
        for event in pre_teardown_events:
            event.wait(self._shutdown_remaining(deadline))

        with self._lock:
            self._cancel_all_retries_locked()
            committed = []
            for record in tuple(self._leases.values()):
                if record.state is LeaseState.COMMITTED:
                    record.cancel_requested = True
                    record.shutdown_requested = True
                    record.cancel_reason = "pool shutdown canceled capture"
                    record.settled = False
                    committed.append(
                        (record, record.owner, record.stream, record.identity)
                    )
        if committed:
            self._run_pre_teardown_settlement_work(
                lambda: tuple(
                    self._cleanup_committed(
                        *cleanup,
                        shutdown=True,
                        intent=SettlementIntent.POOL_SHUTDOWN,
                    )
                    for cleanup in committed
                ),
                deadline,
                tuple(cleanup[0].run_id for cleanup in committed),
            )

        with self._lock:
            self._cancel_pending_removals_locked()
            self._cancel_blocked_reconcile_locked()
            for attempt in self._opening_attempts.values():
                self._cancel_open_attempt_locked(attempt)
            managed_closes = tuple(self._closing_streams.values())
            unpublished_closes = tuple(self._unpublished_closes.values())
            stream_records = tuple(self._streams.values()) + tuple(
                tracked.record for tracked in managed_closes
            )
            attempt_streams = tuple(
                attempt.stream
                for attempt in self._opening_attempts.values()
                if attempt.stream is not None
            )
            self._streams.clear()
            control_executions = tuple(self._active_control_executions)

        unique_streams = tuple(
            {
                id(stream): stream
                for stream in (
                    attempt_streams
                    + tuple(record.stream for record in stream_records)
                    + tuple(
                        tracked.stream for tracked in unpublished_closes
                    )
                )
            }.values()
        )
        with self._lock:
            self._shutdown_pending_streams.update(
                (id(stream), stream) for stream in unique_streams
            )
        shutdown_barrier_complete = self._run_stream_shutdown_barrier(
            unique_streams, deadline
        )

        closed_stream_ids = set()
        close_results = {}
        scheduled_closes = []
        managed_in_progress_ids = {
            id(tracked.record.stream)
            for tracked in managed_closes
            if tracked.in_progress and not tracked.settled_event.is_set()
        }
        managed_in_progress_ids.update(
            id(tracked.stream)
            for tracked in unpublished_closes
            if tracked.in_progress and not tracked.settled_event.is_set()
        )
        if shutdown_barrier_complete:
            for tracked in managed_closes:
                if tracked.in_progress and not tracked.settled_event.is_set():
                    self._physical_close_gate.authorize(
                        tracked.record.stream
                    )
                    tracked.record.stream.control_authorize_shutdown_close()
            for tracked in unpublished_closes:
                if (
                    tracked.in_progress
                    and tracked.open_attempt is None
                    and not tracked.settled_event.is_set()
                ):
                    self._physical_close_gate.authorize(tracked.stream)
                    tracked.stream.control_authorize_shutdown_close()
        for stream in unique_streams if shutdown_barrier_complete else ():
            if (
                id(stream) in closed_stream_ids
                or id(stream) in managed_in_progress_ids
            ):
                continue
            closed_stream_ids.add(id(stream))
            self._physical_close_gate.authorize(stream)
            stream.control_authorize_shutdown_close()
            scheduled_closes.append(
                (
                    stream,
                    self._schedule_shutdown_close(
                        stream,
                        self._shutdown_remaining(deadline),
                    ),
                )
            )
        for stream, tracked in scheduled_closes:
            tracked.settled_event.wait(
                self._shutdown_remaining(deadline)
            )
            closed = tracked.settled_event.is_set() and tracked.closed
            close_results[id(stream)] = closed
        for event in control_executions:
            event.wait(self._shutdown_remaining(deadline))
        with self._lock:
            for tracked in managed_closes:
                current = self._closing_streams.get(
                    tracked.action.hardware_id
                )
                if (
                    current is tracked
                    and close_results.get(id(tracked.record.stream), False)
                ):
                    self._closing_streams.pop(
                        tracked.action.hardware_id, None
                    )
                    tracked.settled_event.set()
            for key, tracked in tuple(self._unpublished_closes.items()):
                if close_results.get(id(tracked.stream), False):
                    self._unpublished_closes.pop(key, None)
                    tracked.closed = True
                    tracked.settled_event.set()
        self._advance_shutdown_teardown(deadline)
        for event in committing_events:
            event.wait(self._shutdown_remaining(deadline))
        with self._lock:
            terminal_events = tuple(
                record.terminal_settled
                for record in self._leases.values()
                if self._lease_cleanup_pending_locked(record)
                and record.terminal_settled is not None
            )
            terminal_executions = tuple(self._terminal_executions)
        for event in terminal_executions:
            event.wait(self._shutdown_remaining(deadline))
        for event in terminal_events:
            event.wait(self._shutdown_remaining(deadline))
        with self._lock:
            self._harvest_finished_workers_locked()
            lease_cleanup_complete = not any(
                self._lease_cleanup_pending_locked(record)
                for record in self._leases.values()
            )
        artifact_cleanup_complete = False
        if lease_cleanup_complete and self._shutdown_teardown_complete:
            artifact_cleanup_complete = self._run_artifact_cleanup(
                deadline, management=False
            )
        with self._lock:
            controls_settled = (
                not self._active_control_executions
                and not self._opening_attempts
                and not self._closing_streams
                and not self._control_threads
                and not self._shutdown_close_resources
                and not self._shutdown_pending_streams
                and not self._unpublished_closes
                and not self._terminal_executions
            )
            leases_settled = not any(
                self._lease_cleanup_pending_locked(record)
                for record in self._leases.values()
            )
            clean = (
                self._shutdown_teardown_complete
                and controls_settled
                and leases_settled
                and artifact_cleanup_complete
            )
            self._shutdown_incomplete_resources = {
                "active_control_executions": len(
                    self._active_control_executions
                ),
                "opening_attempts": len(self._opening_attempts),
                "managed_closes": len(self._closing_streams),
                "control_threads": tuple(
                    (thread.name, thread.is_alive())
                    for thread in self._control_threads
                ),
                "shutdown_closes": len(self._shutdown_close_resources),
                "shutdown_pending_streams": len(
                    self._shutdown_pending_streams
                ),
                "unpublished_closes": len(self._unpublished_closes),
                "terminal_executions": len(self._terminal_executions),
                "active_leases": self._active_lease_count_locked(),
                "unsettled_leases": sum(
                    self._lease_cleanup_pending_locked(record)
                    for record in self._leases.values()
                ),
            }
            if not clean:
                for attempt in self._opening_attempts.values():
                    self._stream_diagnostics[attempt.hardware_id] = (
                        self._failed_open_snapshot(
                            attempt,
                            CaptureErrorCode.SHUTDOWN_INCOMPLETE,
                            "shutdown left an open execution unsettled",
                        )
                    )
            self._state = (
                PoolState.CLOSED
                if clean
                else PoolState.FAILED_CLOSED
            )
            self._shutdown_error_code = (
                None
                if clean
                else CaptureErrorCode.SHUTDOWN_INCOMPLETE
            )
            if clean:
                self._shutdown_resources = ()
        return self.snapshot()

    def _active_lease_count_locked(self) -> int:
        return sum(
            record.state in _ACTIVE_LEASE_STATES
            for record in self._leases.values()
        )

    def _lease_counts_locked(self):
        return {
            state: sum(record.state is state for record in self._leases.values())
            for state in (
                LeaseState.RESERVED,
                LeaseState.COMMITTING,
                LeaseState.COMMITTED,
            )
        }

    def _create_terminal_lease_locked(self, run_id, state, code, message):
        record = _LeaseRecord(
            run_id,
            state,
            LeaseOutcome(run_id, state, code, message),
        )
        self._leases[run_id] = record
        self._remember_terminal_locked(record)
        return record

    def _terminalize_locked(self, record, state, code, message):
        record.previous_state = record.state
        record.state = state
        record.outcome = LeaseOutcome(
            run_id=record.run_id,
            state=state,
            error_code=code,
            message=message,
            artifact_id=record.artifact_id,
        )
        record.settled = True
        self._remember_terminal_locked(record)
        return record.outcome

    def _remember_terminal_locked(self, record) -> None:
        self._terminal_order.pop(record.run_id, None)
        self._terminal_order[record.run_id] = None
        self._prune_terminal_history_locked()

    @staticmethod
    def _unknown_run_outcome(run_id):
        return LeaseOutcome(
            run_id=run_id,
            state=LeaseState.FAILED,
            error_code=CaptureErrorCode.INVALID_CAPTURE_REQUEST,
            message="capture lease does not exist",
        )

    @staticmethod
    def _new_event():
        return threading.Event()

    @staticmethod
    def _unpublished_close_key(attempt, stream):
        return (
            attempt.hardware_id,
            attempt.desired_revision,
            attempt.resolution_attempt_id,
            attempt.stream_generation or 0,
            id(stream),
        )

    @staticmethod
    def _cancel_token_factory():
        return threading.Event()

    def _reconcile_rejected_locked(self, code, message):
        return ReconcileResult(
            False, self._snapshot_cached_locked(), code, message
        )

__all__ = [
    "DesiredHardware",
    "ResidentAudioPool",
]
