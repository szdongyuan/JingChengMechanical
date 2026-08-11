"""Deterministic per-hardware reconnect scheduling for resident streams."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from .pool_types import _OpenAction, _RemovalAction
from .stream_types import RetryBackoff


class ThreadingRetryScheduler:
    """Production scheduler whose returned timer is cancellation-capable."""

    def schedule(self, delay: float, callback):
        timer = threading.Timer(float(delay), callback)
        timer.daemon = True
        timer.name = "resident-audio-retry"
        timer.start()
        return timer


@dataclass(frozen=True, slots=True)
class _RetryIdentity:
    hardware_id: str
    desired_revision: int
    resolution_attempt_id: int
    stream_generation: int


@dataclass(slots=True)
class _ScheduledRetry:
    identity: _RetryIdentity
    attempt: int
    delay: float
    handle: object | None = None


class ResidentPoolRetryMixin:
    """Own retry backoff independently of any replaceable stream object."""

    def _schedule_stream_retry(self, attempt) -> None:
        generation = int(attempt.stream_generation or 0)
        self._schedule_hardware_retry(
            _RetryIdentity(
                hardware_id=attempt.hardware_id,
                desired_revision=attempt.desired_revision,
                resolution_attempt_id=attempt.resolution_attempt_id,
                stream_generation=generation,
            )
        )

    def _schedule_hardware_retry(self, identity: _RetryIdentity) -> None:
        with self._lock:
            if not self._retry_identity_is_current_locked(identity):
                return
            self._cancel_retry_locked(identity.hardware_id)
            backoff = self._retry_backoffs.setdefault(
                identity.hardware_id, RetryBackoff()
            )
            delay = backoff.next_delay()
            if delay is None:
                return
            scheduled = _ScheduledRetry(
                identity=identity,
                attempt=backoff.attempt,
                delay=delay,
            )
            self._scheduled_retries[identity.hardware_id] = scheduled
            prior = self._stream_diagnostics.get(identity.hardware_id)
            if prior is not None:
                self._stream_diagnostics[identity.hardware_id] = replace(
                    prior,
                    retry_attempt=scheduled.attempt,
                    retry_delay=scheduled.delay,
                )
            scheduled.handle = self._retry_scheduler.schedule(
                delay, lambda: self._run_scheduled_retry(scheduled)
            )

    def _run_scheduled_retry(self, scheduled: _ScheduledRetry) -> None:
        identity = scheduled.identity
        with self._lock:
            if (
                self._scheduled_retries.get(identity.hardware_id) is not scheduled
                or not self._retry_identity_is_current_locked(identity)
            ):
                return
            self._scheduled_retries.pop(identity.hardware_id, None)
            desired = self._desired[identity.hardware_id]
            record = self._streams.get(identity.hardware_id)
            action = _OpenAction(
                identity.hardware_id, desired, identity.desired_revision
            )
            removal = _RemovalAction(
                hardware_id=identity.hardware_id,
                desired_revision=identity.desired_revision,
                expected_desired=desired,
                expected_record=record,
            )
        if record is not None:
            closed = self._close_removed([removal], owner_free_only=True)
            if not closed:
                self._schedule_hardware_retry(identity)
                return
        with self._lock:
            if (
                not self._pool_is_open_locked()
                or self._desired.get(identity.hardware_id) != desired
                or self._hardware_desired_revisions.get(identity.hardware_id)
                != identity.desired_revision
                or self._resident_capacity_full_locked()
            ):
                return
        self._open_additions([action], supersede=True)

    def _retry_identity_is_current_locked(self, identity: _RetryIdentity) -> bool:
        if (
            not self._pool_is_open_locked()
            or self._hardware_desired_revisions.get(identity.hardware_id)
            != identity.desired_revision
            or identity.hardware_id not in self._desired
        ):
            return False
        opening = self._opening_attempts.get(identity.hardware_id)
        if opening is not None:
            return (
                opening.desired_revision == identity.desired_revision
                and opening.resolution_attempt_id
                == identity.resolution_attempt_id
                and int(opening.stream_generation or 0)
                == identity.stream_generation
            )
        record = self._streams.get(identity.hardware_id)
        if record is not None:
            return (
                record.desired_revision == identity.desired_revision
                and record.resolution_attempt_id
                == identity.resolution_attempt_id
                and record.stream_generation == identity.stream_generation
            )
        return (
            self._resolution_attempt_counters.get(identity.hardware_id, 0)
            == identity.resolution_attempt_id
            and (
                identity.stream_generation == 0
                or self._generation_counters.get(identity.hardware_id, 0)
                == identity.stream_generation
            )
        )

    def _reset_retry_locked(self, hardware_id: str) -> None:
        self._cancel_retry_locked(hardware_id)
        backoff = self._retry_backoffs.get(hardware_id)
        if backoff is not None:
            backoff.reset()

    def _cancel_retry_locked(self, hardware_id: str) -> None:
        scheduled = self._scheduled_retries.pop(hardware_id, None)
        if scheduled is not None and scheduled.handle is not None:
            cancel = getattr(scheduled.handle, "cancel", None)
            if callable(cancel):
                cancel()

    def _retire_hardware_state_locked(self, hardware_id: str) -> None:
        if (
            hardware_id in self._desired
            or hardware_id in self._streams
            or hardware_id in self._opening_attempts
            or hardware_id in self._closing_streams
            or any(
                tracked.hardware_id == hardware_id
                for tracked in self._unpublished_closes.values()
            )
        ):
            return
        self._cancel_retry_locked(hardware_id)
        self._retry_backoffs.pop(hardware_id, None)
        self._stream_diagnostics.pop(hardware_id, None)

    def _cancel_all_retries_locked(self) -> None:
        for hardware_id in tuple(self._scheduled_retries):
            self._cancel_retry_locked(hardware_id)
        for backoff in self._retry_backoffs.values():
            backoff.cancel()


__all__ = ["ResidentPoolRetryMixin", "ThreadingRetryScheduler"]
