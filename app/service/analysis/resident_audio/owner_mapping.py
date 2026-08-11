"""Precise trigger mapping and one-shot artifact binding for capture owners."""

from __future__ import annotations

import math

from .contracts import (
    BlockTimeQuality,
    CaptureErrorCode,
    CaptureOwnerState,
)
from .owner_types import (
    MAPPING_TIMEOUT_SECONDS,
    OwnerControlEvent,
    TimelineMapper,
)
from .timeline import MappingStatus, TimelineMapping


class CaptureOwnerMappingMixin:
    """Implements activation deadlines and mapping-to-artifact binding."""

    def activate(
        self,
        timeline: TimelineMapper,
        *,
        commit_monotonic: float,
        start_drain: bool = True,
    ) -> CaptureOwnerState:
        if not math.isfinite(float(commit_monotonic)):
            raise ValueError("commit_monotonic must be finite")
        with self._condition:
            if not self._commit_token.is_open:
                raise RuntimeError("commit token is not open")
            if self._state is not CaptureOwnerState.PREPARED:
                raise RuntimeError("owner is not prepared")
            self._mapping_deadline = (
                max(float(commit_monotonic), self._request.trigger_monotonic)
                + MAPPING_TIMEOUT_SECONDS
            )
            self._drain_enabled = start_drain
            deadline = self._mapping_deadline

        if self._mapping_deadline_expired(deadline, None):
            self._fail_mapping_deadline()
            return self.snapshot().state
        mapping = timeline.map_trigger(self._request.trigger_monotonic)
        if self._mapping_deadline_expired(deadline, None):
            self._fail_mapping_deadline()
            return self.snapshot().state
        control = self._apply_mapping(mapping)
        self._send_control(control)
        return self.snapshot().state

    def advance_pending_mapping(
        self,
        timeline: TimelineMapper,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        with self._condition:
            if self._state is not CaptureOwnerState.WAITING_FOR_MAPPING:
                return False
            deadline = self._mapping_deadline
        if self._mapping_deadline_expired(deadline, now_monotonic):
            self._fail_mapping_deadline()
            return False

        mapping = timeline.map_trigger(self._request.trigger_monotonic)
        if self._mapping_deadline_expired(deadline, now_monotonic):
            self._fail_mapping_deadline()
            return False
        if mapping.status is not MappingStatus.AFTER_CONFIRMED_RANGE:
            control = self._apply_mapping(mapping)
            self._send_control(control)
            return (
                mapping.status is MappingStatus.MAPPED
                and self.snapshot().state is CaptureOwnerState.CATCHING_UP
            )
        return False

    def _apply_mapping(
        self, mapping: TimelineMapping
    ) -> OwnerControlEvent | None:
        if not isinstance(mapping, TimelineMapping):
            raise TypeError("timeline mapping must be a TimelineMapping")
        with self._condition:
            if self._decision is not None:
                return None
            if mapping.status is MappingStatus.AFTER_CONFIRMED_RANGE:
                self._transition_state_locked(
                    CaptureOwnerState.WAITING_FOR_MAPPING
                )
                self._condition.notify_all()
                return None
            if mapping.status is MappingStatus.EXPIRED:
                return self._choose_terminal_locked(
                    CaptureOwnerState.FAILED,
                    CaptureErrorCode.TRIGGER_AUDIO_EXPIRED,
                    "trigger audio is older than the mappable timeline",
                )
            start = mapping.sample_index
            mapped_time = mapping.mapped_sample_monotonic
            if start is None or mapped_time is None:
                raise ValueError(
                    "mapped timeline result requires sample coordinates"
                )
            earliest = self._ring.earliest_cursor
            if earliest is not None and start < earliest:
                return self._choose_terminal_locked(
                    CaptureOwnerState.FAILED,
                    CaptureErrorCode.TRIGGER_AUDIO_EXPIRED,
                    "trigger audio was evicted before owner activation",
                )

        try:
            self._writer.bind_capture_start(
                sample_start=start,
                capture_start_monotonic=mapped_time,
                time_quality=BlockTimeQuality.CONFIRMED.value,
            )
        except Exception as error:
            return self._choose_terminal(
                CaptureOwnerState.FAILED,
                self._error_code(error),
                f"artifact capture-start bind failed: {error}",
            )

        with self._condition:
            if self._decision is not None:
                return None
            self._capture_start = start
            self._capture_end = start + self._target_frames
            self._next_write_cursor = start
            self._accepted_cursor = start
            self._transition_state_locked(CaptureOwnerState.CATCHING_UP)
            self._condition.notify_all()
            return None

    def _mapping_deadline_expired(
        self, deadline: float | None, supplied_now: float | None
    ) -> bool:
        now = (
            float(self._monotonic_clock())
            if supplied_now is None
            else float(supplied_now)
        )
        if not math.isfinite(now):
            raise ValueError("now_monotonic must be finite")
        return deadline is not None and now >= deadline

    def _fail_mapping_deadline(self) -> None:
        control = self._choose_terminal(
            CaptureOwnerState.FAILED,
            CaptureErrorCode.TIMING_UNAVAILABLE,
            "trigger did not become precisely mappable before its deadline",
        )
        self._send_control(control)


__all__ = ["CaptureOwnerMappingMixin"]
