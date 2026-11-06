"""Deferred pool control work scheduled by stream callbacks."""

from __future__ import annotations

import threading

from .contracts import CaptureErrorCode, StreamState
from .pool_resources import _ExecutionIdentity
from .pool_types import (
    MAX_RESIDENT_STREAMS,
    PoolStreamSnapshot,
    _ManagedClose,
    _OpenAction,
    _PendingRemoval,
    _PendingReconcile,
    _RemovalAction,
    _ReplacementIdentity,
    _ShutdownClose,
    _UnpublishedClose,
)


class ResidentPoolControlMixin:
    """Identity-guarded pending removals executed outside ingest threads."""

    def _resident_capacity_full_locked(self) -> bool:
        in_flight = sum(
            not attempt.cancel_token.is_set()
            for attempt in self._opening_attempts.values()
        )
        return (
            len(self._streams)
            + len(self._closing_streams)
            + len(self._unpublished_closes)
            + in_flight
            >= MAX_RESIDENT_STREAMS
        )

    def _register_pending_removal(
        self,
        desired_revision: int,
        replacement_actions: list[_OpenAction],
        removal_actions: list[_RemovalAction],
    ) -> None:
        if not replacement_actions or not removal_actions:
            return
        with self._lock:
            replacements = []
            for action in replacement_actions:
                record = self._streams.get(action.hardware_id)
                diagnostic = self._stream_diagnostics.get(action.hardware_id)
                if (
                    record is None
                    or record.desired != action.desired
                    or record.desired_revision != action.desired_revision
                    or diagnostic is None
                    or diagnostic.state is StreamState.FAILED
                ):
                    return
                replacements.append(
                    _ReplacementIdentity(
                        hardware_id=action.hardware_id,
                        desired_revision=record.desired_revision,
                        resolution_attempt_id=record.resolution_attempt_id,
                        stream_generation=record.stream_generation,
                        stream=record.stream,
                    )
                )
            pending = _PendingRemoval(
                desired_revision=desired_revision,
                replacements=tuple(replacements),
                removals=tuple(removal_actions),
                settled_event=self._new_event(),
            )
            if not self._pending_removal_is_current_locked(pending):
                pending.settled_event.set()
                return
            self._pending_removals.append(pending)
            self._schedule_pending_if_ready_locked(pending)

    def _register_blocked_reconcile(
        self,
        desired_revision: int,
        open_actions: list[_OpenAction],
        removal_actions: list[_RemovalAction],
        *,
        require_ready: bool = False,
    ) -> None:
        with self._lock:
            if (
                not self._pool_is_open_locked()
                or self._desired_revision_clock != desired_revision
            ):
                return
            pending = _PendingReconcile(
                desired_revision=desired_revision,
                opens=tuple(open_actions),
                removals=tuple(removal_actions),
                require_ready=require_ready,
            )
            self._pending_reconcile = pending
            for action in open_actions:
                self._stream_diagnostics[action.hardware_id] = PoolStreamSnapshot(
                    hardware_id=action.hardware_id,
                    desired_revision=action.desired_revision,
                    resolution_attempt_id=self._resolution_attempt_counters.get(
                        action.hardware_id, 0
                    ),
                    stream_generation=self._generation_counters.get(
                        action.hardware_id, 0
                    ),
                    state=StreamState.FAILED,
                    owner_count=0,
                    ready=False,
                    published=False,
                    error_code=CaptureErrorCode.STREAM_OPEN_FAILED,
                    message="replacement blocked by incomplete physical close",
                )

    def _cancel_blocked_reconcile_locked(self) -> None:
        self._pending_reconcile = None

    def _schedule_shutdown_close(self, stream, timeout):
        tracked = _ShutdownClose(
            stream=stream,
            settled_event=self._new_event(),
        )

        def close_on_control_path():
            worker = threading.current_thread()
            close_lease = self._physical_close_gate.acquire(
                stream, control_path=True
            )
            try:
                closed = bool(
                    close_lease is not None
                    and stream.close(
                        timeout=timeout,
                        _control_path=True,
                        _close_lease=close_lease,
                    )
                )
            except Exception:
                closed = False
            finally:
                try:
                    if not closed:
                        self._physical_close_gate.revoke(stream)
                        stream.control_revoke_shutdown_close_authorization()
                    with self._lock:
                        tracked.closed = tracked.closed or closed
                        if tracked.closed:
                            self._shutdown_close_resources.pop(
                                id(stream), None
                            )
                            self._shutdown_pending_streams.pop(
                                id(stream), None
                            )
                            for key, unpublished in tuple(
                                self._unpublished_closes.items()
                            ):
                                if unpublished.stream is stream:
                                    self._unpublished_closes.pop(key, None)
                                    unpublished.closed = True
                                    unpublished.in_progress = False
                                    unpublished.settled_event.set()
                finally:
                    tracked.settled_event.set()
                    if close_lease is not None:
                        close_lease.release()

        worker = threading.Thread(
            target=close_on_control_path,
            name="resident-audio-shutdown-control",
            daemon=True,
        )
        identity = self._stream_resource_identity(stream)
        with self._lock:
            worker.start()
            self._control_threads.add(worker)
            self._register_control_worker_locked(
                worker,
                "control",
                (identity,),
            )
            tracked.worker = worker
            self._shutdown_close_resources[id(stream)] = tracked
        return tracked

    def _cancel_open_attempt_locked(self, attempt) -> None:
        attempt.cancel_token.set()
        stream = attempt.stream
        if stream is None:
            return
        key = self._unpublished_close_key(attempt, stream)
        if key not in self._unpublished_closes:
            self._unpublished_closes[key] = _UnpublishedClose(
                hardware_id=attempt.hardware_id,
                desired_revision=attempt.desired_revision,
                resolution_attempt_id=attempt.resolution_attempt_id,
                stream_generation=attempt.stream_generation or 0,
                stream=stream,
                settled_event=self._new_event(),
                open_attempt=attempt,
            )

    def _close_stale_stream(self, stream, attempt) -> bool:
        key = self._unpublished_close_key(attempt, stream)
        with self._lock:
            tracked = self._unpublished_closes.get(key)
            if tracked is None:
                tracked = _UnpublishedClose(
                    hardware_id=attempt.hardware_id,
                    desired_revision=attempt.desired_revision,
                    resolution_attempt_id=attempt.resolution_attempt_id,
                    stream_generation=attempt.stream_generation or 0,
                    stream=stream,
                    settled_event=self._new_event(),
                )
                self._unpublished_closes[key] = tracked
            elif tracked.open_attempt is attempt:
                tracked.open_attempt = None
            elif tracked.in_progress:
                return False
            else:
                tracked.in_progress = True
        close_lease = self._physical_close_gate.acquire(
            stream, control_path=True
        )
        try:
            try:
                closed = bool(
                    close_lease is not None
                    and stream.close(
                        _control_path=True,
                        _close_lease=close_lease,
                    )
                )
            except Exception:
                closed = False
            with self._lock:
                current = self._unpublished_closes.get(key)
                if current is tracked:
                    tracked.in_progress = False
                    tracked.closed = closed
                    if closed:
                        self._shutdown_pending_streams.pop(
                            id(stream), None
                        )
                        self._unpublished_closes.pop(key, None)
                        tracked.settled_event.set()
                        shutdown_close = (
                            self._shutdown_close_resources.get(id(stream))
                        )
                        if shutdown_close is not None:
                            shutdown_close.closed = True
                            if shutdown_close.settled_event.is_set():
                                self._shutdown_close_resources.pop(
                                    id(stream), None
                                )
            return closed
        finally:
            if close_lease is not None:
                close_lease.release()

    def _retry_unpublished_closes(self) -> bool:
        with self._lock:
            pending = tuple(
                (key, tracked)
                for key, tracked in self._unpublished_closes.items()
                if not tracked.in_progress
            )
            for _, tracked in pending:
                tracked.in_progress = True
        all_closed = True
        for key, tracked in pending:
            close_lease = self._physical_close_gate.acquire(
                tracked.stream, control_path=True
            )
            try:
                try:
                    closed = bool(
                        close_lease is not None
                        and tracked.stream.close(
                            _control_path=True,
                            _close_lease=close_lease,
                        )
                    )
                except Exception:
                    closed = False
                with self._lock:
                    current = self._unpublished_closes.get(key)
                    if current is tracked:
                        tracked.in_progress = False
                        tracked.closed = closed
                        if closed:
                            self._shutdown_pending_streams.pop(
                                id(tracked.stream), None
                            )
                            self._unpublished_closes.pop(key, None)
                            tracked.settled_event.set()
            finally:
                if close_lease is not None:
                    close_lease.release()
            all_closed = closed and all_closed
        return all_closed

    def _resume_blocked_reconcile(self, pending) -> None:
        with self._lock:
            if (
                pending is not self._pending_reconcile
                or pending.in_progress
                or not self._pool_is_open_locked()
                or self._desired_revision_clock != pending.desired_revision
            ):
                return
            pending.in_progress = True
        closed = self._close_removed(
            pending.removals,
            owner_free_only=True,
        )
        opened = False
        if closed:
            opened = self._open_additions(
                pending.opens,
                require_ready=pending.require_ready,
            )
        with self._lock:
            if pending is not self._pending_reconcile:
                return
            if closed and opened:
                self._pending_reconcile = None
            else:
                pending.in_progress = False

    def _close_removed(self, actions, *, owner_free_only=False) -> bool:
        managed = []
        all_closed = True
        for action in actions:
            with self._lock:
                if (
                    self._hardware_desired_revisions.get(action.hardware_id)
                    != action.desired_revision
                    or self._desired.get(action.hardware_id)
                    != action.expected_desired
                ):
                    all_closed = False
                    continue
                tracked = self._closing_streams.get(action.hardware_id)
                record = self._streams.get(action.hardware_id)
            if tracked is not None:
                wait_event = None
                with self._lock:
                    current = self._closing_streams.get(action.hardware_id)
                    if (
                        current is not tracked
                        or current.record is not action.expected_record
                    ):
                        all_closed = False
                        continue
                    if current.in_progress:
                        wait_event = current.settled_event
                    else:
                        current.action = action
                        current.in_progress = True
                        managed.append(current)
                if wait_event is not None:
                    wait_event.wait(self._reconcile_ready_timeout)
                    with self._lock:
                        current = self._closing_streams.get(
                            action.hardware_id
                        )
                        if current is None:
                            continue
                        if (
                            current is not tracked
                            or current.record is not action.expected_record
                            or current.in_progress
                        ):
                            all_closed = False
                            continue
                        current.action = action
                        current.in_progress = True
                        managed.append(current)
                continue
            if action.expected_record is None:
                continue
            if record is not action.expected_record:
                continue
            if (
                owner_free_only
                and record.stream.has_active_registrations_for_reconcile()
            ):
                all_closed = False
                continue
            with self._lock:
                if (
                    self._streams.get(action.hardware_id) is not record
                    or self._hardware_desired_revisions.get(action.hardware_id)
                    != action.desired_revision
                    or self._desired.get(action.hardware_id)
                    != action.expected_desired
                ):
                    all_closed = False
                    continue
                self._streams.pop(action.hardware_id, None)
                tracked = _ManagedClose(
                    action=action,
                    record=record,
                    settled_event=self._new_event(),
                )
                self._closing_streams[action.hardware_id] = tracked
                managed.append(tracked)
                if action.hardware_id not in self._desired:
                    self._stream_diagnostics.pop(action.hardware_id, None)
        for tracked in managed:
            closed = False
            message = None
            close_lease = self._physical_close_gate.acquire(
                tracked.record.stream, control_path=True
            )
            try:
                try:
                    closed = bool(
                        close_lease is not None
                        and tracked.record.stream.close(
                            _control_path=True,
                            _close_lease=close_lease,
                        )
                    )
                    if not closed:
                        message = (
                            "resident stream physical close did not settle"
                        )
                except Exception as error:
                    message = str(error)
                with self._lock:
                    current = self._closing_streams.get(
                        tracked.action.hardware_id
                    )
                    if current is tracked:
                        if closed:
                            self._shutdown_pending_streams.pop(
                                id(tracked.record.stream), None
                            )
                            self._closing_streams.pop(
                                tracked.action.hardware_id, None
                            )
                            self._retire_hardware_state_locked(
                                tracked.action.hardware_id
                            )
                            tracked.settled_event.set()
                        else:
                            tracked.in_progress = False
                            tracked.failed = True
                            tracked.message = message
            finally:
                if close_lease is not None:
                    close_lease.release()
            all_closed = closed and all_closed
        with self._lock:
            pending = self._pending_reconcile
        if pending is not None and not pending.in_progress:
            self._resume_blocked_reconcile(pending)
        return all_closed

    def _pending_removal_is_current_locked(self, pending) -> bool:
        if (
            not self._pool_is_open_locked()
            or self._desired_revision_clock != pending.desired_revision
        ):
            return False
        for replacement in pending.replacements:
            record = self._streams.get(replacement.hardware_id)
            if (
                record is None
                or record.stream is not replacement.stream
                or record.desired_revision != replacement.desired_revision
                or record.resolution_attempt_id
                != replacement.resolution_attempt_id
                or record.stream_generation != replacement.stream_generation
            ):
                return False
        for removal in pending.removals:
            if (
                removal.expected_record is None
                or self._streams.get(removal.hardware_id)
                is not removal.expected_record
                or self._hardware_desired_revisions.get(removal.hardware_id)
                != removal.desired_revision
                or self._desired.get(removal.hardware_id)
                != removal.expected_desired
            ):
                return False
        return True

    def _pending_replacements_ready_locked(self, pending) -> bool:
        for replacement in pending.replacements:
            diagnostic = self._stream_diagnostics.get(replacement.hardware_id)
            if (
                diagnostic is None
                or diagnostic.desired_revision != replacement.desired_revision
                or diagnostic.resolution_attempt_id
                != replacement.resolution_attempt_id
                or diagnostic.stream_generation != replacement.stream_generation
                or diagnostic.state is not StreamState.READY
            ):
                return False
        return True

    def _schedule_pending_if_ready_locked(self, pending) -> None:
        if (
            pending.scheduled
            or pending not in self._pending_removals
            or not self._pending_removal_is_current_locked(pending)
            or not self._pending_replacements_ready_locked(pending)
        ):
            return
        pending.scheduled = True
        worker = threading.Thread(
            target=self._run_pending_removal,
            args=(pending,),
            name="resident-audio-pool-control",
            daemon=True,
        )
        self._control_threads.add(worker)
        self._register_control_worker_locked(
            worker,
            "control",
            tuple(
                _ExecutionIdentity(
                    action.hardware_id,
                    None
                    if action.expected_record is None
                    else action.expected_record.stream_generation,
                )
                for action in pending.removals
            ),
        )
        worker.start()

    def _replacement_ready_locked(self, attempt) -> None:
        for pending in tuple(self._pending_removals):
            if any(
                replacement.stream is attempt.stream
                and replacement.resolution_attempt_id
                == attempt.resolution_attempt_id
                for replacement in pending.replacements
            ):
                self._schedule_pending_if_ready_locked(pending)

    def _replacement_failed_locked(self, attempt) -> None:
        for pending in tuple(self._pending_removals):
            if any(
                replacement.stream is attempt.stream
                and replacement.resolution_attempt_id
                == attempt.resolution_attempt_id
                for replacement in pending.replacements
            ):
                self._settle_pending_locked(pending)

    def _cancel_pending_removals_locked(self) -> None:
        for pending in tuple(self._pending_removals):
            self._settle_pending_locked(pending)

    def _settle_pending_locked(self, pending) -> None:
        if pending in self._pending_removals:
            self._pending_removals.remove(pending)
        pending.settled_event.set()

    def _run_pending_removal(self, pending) -> None:
        worker = threading.current_thread()
        try:
            with self._lock:
                current = (
                    pending in self._pending_removals
                    and self._pending_removal_is_current_locked(pending)
                    and self._pending_replacements_ready_locked(pending)
                )
                actions = pending.removals if current else ()
            if actions:
                closed = self._close_removed(
                    actions,
                    owner_free_only=True,
                )
                if not closed:
                    self._register_blocked_reconcile(
                        pending.desired_revision,
                        [],
                        list(actions),
                    )
        finally:
            with self._lock:
                self._settle_pending_locked(pending)


__all__ = ["ResidentPoolControlMixin"]
