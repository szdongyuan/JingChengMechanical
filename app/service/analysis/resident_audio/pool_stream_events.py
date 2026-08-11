"""Ready/failure publication for identity-current resident streams."""

from __future__ import annotations

from dataclasses import replace

from .contracts import StreamState
from .pool_types import PoolStreamSnapshot


class ResidentPoolStreamEventsMixin:
    def _record_attempt_failure(
        self, attempt, code, message, *, exception_type=None
    ) -> None:
        stream = attempt.stream
        if stream is not None:
            self._close_stale_stream(stream, attempt)
        attempt.ready_event.set()
        with self._lock:
            if self._opening_attempts.get(attempt.hardware_id) is attempt:
                self._opening_attempts.pop(attempt.hardware_id, None)
                current = (
                    self._pool_is_open_locked()
                    and attempt.hardware_id in self._desired
                    and not attempt.cancel_token.is_set()
                )
                if current:
                    failed = self._failed_open_snapshot(attempt, code, message)
                    self._stream_diagnostics[attempt.hardware_id] = replace(
                        failed, exception_type=exception_type
                    )
            else:
                current = False
        if current:
            self._schedule_stream_retry(attempt)

    def _retire_stale_attempt(self, attempt) -> None:
        with self._lock:
            if self._opening_attempts.get(attempt.hardware_id) is attempt:
                self._opening_attempts.pop(attempt.hardware_id, None)

    def _stream_ready(self, attempt, token) -> None:
        with self._lock:
            record = self._streams.get(attempt.hardware_id)
            if (
                record is None
                or record.stream is not attempt.stream
                or record.stream_generation != token.stream_generation
            ):
                attempt.ready_event.set()
                return
            prior = self._stream_diagnostics.get(attempt.hardware_id)
            self._stream_diagnostics[attempt.hardware_id] = PoolStreamSnapshot(
                hardware_id=attempt.hardware_id,
                desired_revision=record.desired_revision,
                resolution_attempt_id=record.resolution_attempt_id,
                stream_generation=record.stream_generation,
                state=StreamState.READY,
                owner_count=0 if prior is None else prior.owner_count,
                ready=True,
                published=True,
                previous_state=None if prior is None else prior.state,
            )
            self._reset_retry_locked(attempt.hardware_id)
            self._replacement_ready_locked(attempt)
        attempt.ready_event.set()

__all__ = ["ResidentPoolStreamEventsMixin"]
