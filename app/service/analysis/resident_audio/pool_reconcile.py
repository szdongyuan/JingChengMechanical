"""Desired-set reconciliation and persistent generation allocation."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from .contracts import (
    CaptureErrorCode,
    CaptureLayerError,
    StreamState,
)
from .owner_settlement_types import SettlementIntent
from .pool_types import (
    MAX_RESIDENT_STREAMS,
    DesiredHardware,
    PoolStreamSnapshot,
    ReconcileResult,
    _OpenAction,
    _OpeningAttempt,
    _RemovalAction,
    _StreamRecord,
)
from .stream import ResidentStream


class ResidentPoolReconcileMixin:
    """Pool control-path operations; resolver and backend calls stay lock-free."""

    def reconcile(
        self, desired_hardware: Iterable[DesiredHardware]
    ) -> ReconcileResult:
        execution = self._new_event()
        tracked = False
        with self._lock:
            if self._pool_is_open_locked():
                self._active_control_executions.add(execution)
                self._register_execution_locked(
                    execution,
                    "control",
                    threading.current_thread(),
                )
                tracked = True
        try:
            return self._reconcile(desired_hardware)
        finally:
            if tracked:
                with self._lock:
                    self._active_control_executions.discard(execution)
                    self._unregister_execution_locked(execution)
                    execution.set()

    def _reconcile(
        self, desired_hardware: Iterable[DesiredHardware]
    ) -> ReconcileResult:
        desired, error = self._normalize_desired(desired_hardware)
        if error is not None:
            return ReconcileResult(False, self.snapshot(), error.code, error.message)

        with self._lock:
            if not self._pool_is_open_locked():
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.POOL_SHUTTING_DOWN,
                    "resident audio pool is shutting down",
                )
            if self._active_lease_count_locked():
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.INVALID_CAPTURE_REQUEST,
                    "desired hardware cannot change while capture leases are active",
                )
            if desired == self._desired:
                pending = self._pending_reconcile
                if pending is None:
                    return ReconcileResult(True, self._snapshot_cached_locked())
            else:
                pending = None
        if pending is not None:
            self._resume_blocked_reconcile(pending)
            return ReconcileResult(True, self.snapshot())

        with self._lock:
            if not self._pool_is_open_locked():
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.POOL_SHUTTING_DOWN,
                    "resident audio pool is shutting down",
                )
            if self._active_lease_count_locked():
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.INVALID_CAPTURE_REQUEST,
                    "desired hardware cannot change while capture leases are active",
                )
            old_desired = self._desired
            if desired == old_desired:
                return ReconcileResult(True, self._snapshot_cached_locked())

            self._cancel_pending_removals_locked()
            self._cancel_blocked_reconcile_locked()
            old_ids = set(old_desired)
            new_ids = set(desired)
            changed_ids = {
                hardware_id
                for hardware_id in old_ids & new_ids
                if old_desired[hardware_id] != desired[hardware_id]
            }
            changed_ids.update(set(self._closing_streams) & new_ids)
            changed = sorted(changed_ids)
            additions = sorted((new_ids - old_ids) | changed_ids)
            removals = sorted(
                (old_ids - new_ids)
                | (set(self._streams) - new_ids)
                | changed_ids
            )
            self._desired_revision_clock += 1
            desired_revision = self._desired_revision_clock
            self._desired = desired
            for hardware_id in new_ids:
                if (
                    hardware_id not in old_desired
                    or old_desired[hardware_id] != desired[hardware_id]
                ):
                    self._hardware_desired_revisions[hardware_id] = desired_revision
            for hardware_id in removals:
                self._cancel_retry_locked(hardware_id)
                self._hardware_desired_revisions[hardware_id] = desired_revision
                attempt = self._opening_attempts.get(hardware_id)
                if attempt is not None:
                    self._cancel_open_attempt_locked(attempt)
                self._retire_hardware_state_locked(hardware_id)
            for hardware_id in changed:
                self._cancel_retry_locked(hardware_id)
                attempt = self._opening_attempts.get(hardware_id)
                if attempt is not None:
                    self._cancel_open_attempt_locked(attempt)

            reusable = []
            for hardware_id in additions:
                record = self._streams.get(hardware_id)
                if record is not None and record.desired == desired[hardware_id]:
                    record.desired_revision = desired_revision
                    reusable.append(hardware_id)
            additions = [
                hardware_id for hardware_id in additions if hardware_id not in reusable
            ]

            removal_actions = {
                hardware_id: _RemovalAction(
                    hardware_id=hardware_id,
                    desired_revision=desired_revision,
                    expected_desired=desired.get(hardware_id),
                    expected_record=(
                        self._streams.get(hardware_id)
                        or (
                            self._closing_streams[hardware_id].record
                            if hardware_id in self._closing_streams
                            else None
                        )
                    ),
                )
                for hardware_id in removals
            }
            open_actions = {
                hardware_id: _OpenAction(
                    hardware_id,
                    desired[hardware_id],
                    self._hardware_desired_revisions[hardware_id],
                )
                for hardware_id in additions
            }
            changed_set = set(changed)
            distinct_additions = [
                open_actions[item] for item in additions if item not in changed_set
            ]
            distinct_removals = [
                removal_actions[item] for item in removals if item not in changed_set
            ]
            changed_opens = [
                open_actions[item] for item in changed if item in open_actions
            ]
            changed_removals = [
                removal_actions[item] for item in changed
                if item in removal_actions
            ]
            current_published = len(self._streams) - sum(
                action.expected_record is not None for action in changed_removals
            )
            opening_elsewhere = sum(
                1
                for hardware_id in self._opening_attempts
                if hardware_id not in removals
            )
            add_before_remove = (
                current_published + opening_elsewhere + len(distinct_additions)
                <= MAX_RESIDENT_STREAMS
            )

        if changed_removals:
            closed = self._close_removed(changed_removals, owner_free_only=True)
            if closed:
                self._open_additions(changed_opens, require_ready=True)
            else:
                self._register_blocked_reconcile(
                    desired_revision,
                    changed_opens,
                    changed_removals,
                    require_ready=True,
                )
        if add_before_remove:
            additions_ready = self._open_additions(
                distinct_additions,
                require_ready=bool(distinct_removals),
            )
            if additions_ready:
                removed = self._close_removed(distinct_removals)
                if not removed:
                    self._register_blocked_reconcile(
                        desired_revision,
                        [],
                        distinct_removals,
                    )
            else:
                if any(
                    action.expected_record is not None
                    for action in distinct_removals
                ):
                    self._register_pending_removal(
                        desired_revision,
                        distinct_additions,
                        distinct_removals,
                    )
                elif self._unpublished_closes:
                    self._register_blocked_reconcile(
                        desired_revision,
                        distinct_additions,
                        [],
                    )
        else:
            closed = self._close_removed(
                distinct_removals,
                owner_free_only=True,
            )
            if closed:
                self._open_additions(distinct_additions)
            else:
                self._register_blocked_reconcile(
                    desired_revision,
                    distinct_additions,
                    distinct_removals,
                )
        return ReconcileResult(True, self.snapshot())

    def reconnect(self, hardware_id: str) -> ReconcileResult:
        execution = self._new_event()
        tracked = False
        with self._lock:
            if self._pool_is_open_locked():
                self._active_control_executions.add(execution)
                self._register_execution_locked(
                    execution,
                    "control",
                    threading.current_thread(),
                )
                tracked = True
        try:
            return self._reconnect(hardware_id)
        finally:
            if tracked:
                with self._lock:
                    self._active_control_executions.discard(execution)
                    self._unregister_execution_locked(execution)
                    execution.set()

    def _reconnect(self, hardware_id: str) -> ReconcileResult:
        with self._lock:
            if not self._pool_is_open_locked():
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.POOL_SHUTTING_DOWN,
                    "resident audio pool is shutting down",
                )
            if self._active_lease_count_locked():
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.INVALID_CAPTURE_REQUEST,
                    "hardware cannot reconnect while capture leases are active",
                )
            if hardware_id not in self._desired:
                return self._reconcile_rejected_locked(
                    CaptureErrorCode.STREAM_NOT_READY,
                    "hardware is not in the desired set",
                )
            old_attempt = self._opening_attempts.get(hardware_id)
            if old_attempt is not None:
                self._cancel_open_attempt_locked(old_attempt)
            action = _OpenAction(
                hardware_id,
                self._desired[hardware_id],
                self._hardware_desired_revisions[hardware_id],
            )
            record = self._streams.get(hardware_id)
            removal = _RemovalAction(
                hardware_id=hardware_id,
                desired_revision=self._hardware_desired_revisions[hardware_id],
                expected_desired=self._desired[hardware_id],
                expected_record=record,
            )
        closed = self._close_removed([removal], owner_free_only=True)
        if closed:
            with self._lock:
                capacity_blocked = self._resident_capacity_full_locked()
            if capacity_blocked:
                self._register_blocked_reconcile(
                    action.desired_revision, [action], []
                )
            else:
                self._open_additions([action], supersede=True)
        else:
            self._register_blocked_reconcile(
                action.desired_revision,
                [action],
                [removal],
            )
        return ReconcileResult(True, self.snapshot())

    def _normalize_desired(self, values):
        normalized: dict[str, DesiredHardware] = {}
        try:
            items = tuple(values)
        except TypeError as error:
            return {}, CaptureLayerError(
                CaptureErrorCode.HARDWARE_RESOLUTION_CONFLICT,
                f"desired hardware must be iterable: {error}",
            )
        for item in items:
            if not isinstance(item, DesiredHardware):
                return {}, CaptureLayerError(
                    CaptureErrorCode.HARDWARE_RESOLUTION_CONFLICT,
                    "desired entries must be immutable DesiredHardware values",
                )
            existing = normalized.get(item.hardware_id)
            if existing is not None and existing != item:
                return {}, CaptureLayerError(
                    CaptureErrorCode.HARDWARE_RESOLUTION_CONFLICT,
                    f"conflicting desired formats for {item.hardware_id}",
                )
            normalized[item.hardware_id] = item
        if len(normalized) > MAX_RESIDENT_STREAMS:
            return {}, CaptureLayerError(
                CaptureErrorCode.TOO_MANY_REQUIRED_HARDWARE,
                "desired hardware set exceeds the six-stream limit",
            )
        return dict(sorted(normalized.items())), None

    def _open_additions(
        self, actions, *, supersede=False, require_ready=False
    ) -> bool:
        self._retry_unpublished_closes()
        all_ready = True
        for action in actions:
            hardware_id = action.hardware_id
            with self._lock:
                closing = self._closing_streams.get(hardware_id)
            if closing is not None:
                closing.settled_event.wait(self._reconcile_ready_timeout)
            with self._lock:
                if hardware_id in self._closing_streams:
                    all_ready = False
                    continue
                if not self._pool_is_open_locked():
                    return False
                if (
                    self._desired.get(hardware_id) != action.desired
                    or self._hardware_desired_revisions.get(hardware_id)
                    != action.desired_revision
                ):
                    all_ready = False
                    continue
                existing_record = self._streams.get(hardware_id)
                if not supersede and existing_record is not None:
                    if existing_record.desired == action.desired:
                        existing_record.desired_revision = action.desired_revision
                        continue
                    all_ready = False
                    continue
                existing_attempt = self._opening_attempts.get(hardware_id)
                if (
                    not supersede
                    and existing_attempt is not None
                    and not existing_attempt.cancel_token.is_set()
                ):
                    continue
                if self._resident_capacity_full_locked():
                    all_ready = False
                    continue
                attempt = self._new_attempt_locked(action)
            published = self._execute_open_attempt(attempt)
            if not published:
                all_ready = False
                continue
            if require_ready:
                attempt.ready_event.wait(self._reconcile_ready_timeout)
                with self._lock:
                    record = self._streams.get(hardware_id)
                    diagnostic = self._stream_diagnostics.get(hardware_id)
                    ready = (
                        record is not None
                        and record.stream is attempt.stream
                        and diagnostic is not None
                        and diagnostic.state is StreamState.READY
                    )
                all_ready = ready and all_ready
        return all_ready

    def _new_attempt_locked(self, action: _OpenAction) -> _OpeningAttempt:
        hardware_id = action.hardware_id
        prior = self._opening_attempts.get(hardware_id)
        if prior is not None:
            self._cancel_open_attempt_locked(prior)
        self._resolution_attempt_counters[hardware_id] = (
            self._resolution_attempt_counters.get(hardware_id, 0) + 1
        )
        attempt = _OpeningAttempt(
            hardware_id=hardware_id,
            desired=action.desired,
            desired_revision=action.desired_revision,
            resolution_attempt_id=self._resolution_attempt_counters[hardware_id],
            cancel_token=self._cancel_token_factory(),
        )
        self._opening_attempts[hardware_id] = attempt
        self._identify_current_execution_locked(
            kind="resolver", hardware_id=hardware_id
        )
        self._stream_diagnostics[hardware_id] = self._opening_snapshot(attempt)
        return attempt

    def _execute_open_attempt(self, attempt: _OpeningAttempt) -> bool:
        try:
            resolved = self._resolver.resolve(
                attempt.hardware_id,
                attempt.desired_revision,
                attempt.resolution_attempt_id,
                attempt.cancel_token,
            )
            if (
                resolved.hardware_id != attempt.hardware_id
                or resolved.sample_rate != attempt.desired.sample_rate
                or resolved.channels != attempt.desired.channels
            ):
                raise CaptureLayerError(
                    CaptureErrorCode.HARDWARE_RESOLUTION_CONFLICT,
                    "resolved identity or format differs from desired hardware",
            )
            if not self._attempt_is_current(attempt):
                self._retire_stale_attempt(attempt)
                return False
            with self._lock:
                if not self._attempt_is_current_locked(attempt):
                    return False
                self._generation_counters[attempt.hardware_id] = (
                    self._generation_counters.get(attempt.hardware_id, 0) + 1
                )
                attempt.stream_generation = self._generation_counters[
                    attempt.hardware_id
                ]
                self._identify_current_execution_locked(
                    kind="resolver",
                    hardware_id=attempt.hardware_id,
                    stream_generation=attempt.stream_generation,
                )
                self._stream_diagnostics[attempt.hardware_id] = (
                    self._opening_snapshot(attempt)
                )
            stream = self._stream_factory(
                resolved_device=resolved,
                backend=self._backend,
                pool_epoch=self._pool_epoch,
                stream_generation=attempt.stream_generation,
                monotonic_clock=self._clock,
                physical_close_gate=self._physical_close_gate,
                ready_sink=lambda token: self._stream_ready(attempt, token),
                failure_sink=lambda event: self._stream_failed(attempt, event),
            )
            attempt.stream = stream
            if not self._attempt_is_current(attempt):
                self._close_stale_stream(stream, attempt)
                self._retire_stale_attempt(attempt)
                return False
            stream.start()
            snapshot = stream.snapshot()
            if snapshot.state is StreamState.FAILED:
                self._record_attempt_failure(
                    attempt,
                    snapshot.failure_code or CaptureErrorCode.STREAM_OPEN_FAILED,
                    "resident stream failed during open/start",
                )
                return False
            if not self._attempt_is_current(attempt):
                self._close_stale_stream(stream, attempt)
                self._retire_stale_attempt(attempt)
                return False
            with self._lock:
                if not self._attempt_is_current_locked(attempt):
                    stale = True
                else:
                    stale = False
                    self._streams[attempt.hardware_id] = _StreamRecord(
                        stream,
                        attempt.desired,
                        attempt.desired_revision,
                        attempt.resolution_attempt_id,
                        attempt.stream_generation,
                    )
                    self._opening_attempts.pop(attempt.hardware_id, None)
                    self._stream_diagnostics[attempt.hardware_id] = (
                        self._pool_stream_snapshot_from_record(
                            attempt.hardware_id,
                            self._streams[attempt.hardware_id],
                            snapshot,
                        )
                    )
            if stale:
                self._close_stale_stream(stream, attempt)
                self._retire_stale_attempt(attempt)
                return False
            if snapshot.state is StreamState.READY:
                attempt.ready_event.set()
            return True
        except Exception as error:
            code = (
                error.code
                if isinstance(error, CaptureLayerError)
                else CaptureErrorCode.STREAM_OPEN_FAILED
            )
            self._record_attempt_failure(
                attempt,
                code,
                "resident stream resolution/open failed",
                exception_type=type(error).__name__,
            )
            return False

    def _attempt_is_current(self, attempt) -> bool:
        with self._lock:
            return self._attempt_is_current_locked(attempt)

    def _attempt_is_current_locked(self, attempt) -> bool:
        return (
            self._pool_is_open_locked()
            and not attempt.cancel_token.is_set()
            and self._opening_attempts.get(attempt.hardware_id) is attempt
            and self._desired.get(attempt.hardware_id) == attempt.desired
            and self._hardware_desired_revisions.get(attempt.hardware_id)
            == attempt.desired_revision
        )

    def _stream_failed(self, attempt, event) -> None:
        with self._lock:
            record = self._streams.get(attempt.hardware_id)
            current = record is not None and record.stream is attempt.stream
            if current:
                prior = self._stream_diagnostics.get(attempt.hardware_id)
                self._stream_diagnostics[attempt.hardware_id] = PoolStreamSnapshot(
                    hardware_id=attempt.hardware_id,
                    desired_revision=record.desired_revision,
                    resolution_attempt_id=record.resolution_attempt_id,
                    stream_generation=record.stream_generation,
                    state=StreamState.FAILED,
                    owner_count=0 if prior is None else prior.owner_count,
                    ready=False,
                    published=True,
                    error_code=event.error_code,
                    message="resident stream generation failed",
                    exception_type=event.exception_type,
                    previous_state=None if prior is None else prior.state,
                )
                self._replacement_failed_locked(attempt)
        self._claim_stream_failure_settlements(
            attempt.stream, event, SettlementIntent.STREAM_FAILURE
        )
        if current:
            self._schedule_stream_retry(attempt)
        attempt.ready_event.set()

__all__ = ["ResidentPoolReconcileMixin"]
