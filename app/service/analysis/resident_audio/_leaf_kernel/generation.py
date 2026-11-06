"""Stream-local generation and nonblocking callback-lane state."""

from __future__ import annotations

from threading import Lock
from typing import Any

import numpy as np

from .ring import (
    RingAppendRejected,
    RingOperationRejectionCode,
    RingReadAvailable,
    RingReadBusy,
    RingReadEvicted,
    RingReadFaulted,
    RingReadUnavailable,
    RingStateFaulted,
    TimestampedBlockRing,
)
from .rows import (
    CallbackLaneRejected,
    CallbackLaneSnapshot,
    CallbackLaneState,
    CallbackQuiescenceOutcome,
    CallbackRejectionCode,
    GenerationState,
)
from .values import (
    CallbackGeneration,
    CallbackSequence,
    CallbackTicket,
    GenerationId,
    MAX_SIGNED_63,
    SampleCursor,
    SeededSlotId,
    _canonical_callback_generation,
    _canonical_callback_sequence,
    _canonical_callback_ticket,
    _canonical_seeded_slot_id,
    _runtime_final,
)


@_runtime_final
class _GenerationIssuerSeal:
    __slots__ = ("_marker",)

    def __init__(self, marker: Any = None) -> None:
        self._marker = marker

    def __repr__(self) -> str:
        return "<generation-issuer-seal>"


@_runtime_final
class _ResidentGeneration:
    __slots__ = (
        "_generation_id",
        "_callback_generation",
        "_ring",
        "_lane_state",
        "_admitted_count",
        "_in_flight_count",
        "_returned_count",
        "_last_ingress_sequence",
        "_owner_count",
        "_callback_guard",
        "_active_ticket_sequence",
        "_append_in_progress",
        "_ingress_faulted",
    )

    def __init__(
        self,
        issuer_seal: Any,
        slot_id: Any,
        capacity_frames: Any,
        channels: Any,
    ) -> None:
        if not _generation_seal_is_valid(issuer_seal):
            raise PermissionError("generation construction is issuer-only")
        canonical_slot = _canonical_seeded_slot_id(
            slot_id, type(self).__name__, "slot_id"
        )
        self._generation_id = GenerationId(canonical_slot, 1)
        self._callback_generation = CallbackGeneration(
            self._generation_id, 1
        )
        self._ring = TimestampedBlockRing(capacity_frames, channels)
        self._lane_state = CallbackLaneState.ADMITTING
        self._admitted_count = 0
        self._in_flight_count = 0
        self._returned_count = 0
        self._last_ingress_sequence = 0
        self._owner_count = 0
        self._callback_guard = Lock()
        self._active_ticket_sequence = 0
        self._append_in_progress = False
        self._ingress_faulted = False

    def __repr__(self) -> str:
        return "<resident-generation>"

    @property
    def generation_id(self) -> GenerationId:
        return GenerationId(
            self._generation_id.slot_id,
            self._generation_id.sequence,
        )

    @property
    def callback_generation(self) -> CallbackGeneration:
        return CallbackGeneration(
            self._callback_generation.generation_id,
            self._callback_generation.sequence,
        )

    @property
    def ring_capacity_frames(self) -> int:
        return self._ring.capacity_frames

    @property
    def ring_retained_frames(self) -> int:
        return self._ring.retained_frames

    @property
    def ring_oldest_cursor(self) -> SampleCursor:
        return self._ring.oldest_cursor

    @property
    def ring_next_cursor(self) -> SampleCursor:
        return self._ring.next_cursor

    def ring_snapshot(
        self,
    ) -> tuple[int, int, int, int] | RingStateFaulted:
        return self._ring.snapshot()

    def read_ring(
        self, start: Any, end: Any
    ) -> (
        RingReadAvailable
        | RingReadEvicted
        | RingReadUnavailable
        | RingReadBusy
        | RingReadFaulted
    ):
        return self._ring.read(start, end)

    @property
    def state(self) -> GenerationState:
        return GenerationState.ACTIVE

    @property
    def owner_count(self) -> int:
        with self._callback_guard:
            return self._owner_count

    def _busy_rejection(self) -> CallbackLaneRejected:
        return CallbackLaneRejected(
            self._callback_generation,
            CallbackRejectionCode.BUSY,
        )

    def callback_snapshot(
        self,
    ) -> CallbackLaneSnapshot | CallbackLaneRejected:
        if not self._callback_guard.acquire(blocking=False):
            return self._busy_rejection()
        try:
            return self._callback_snapshot_locked()
        finally:
            self._callback_guard.release()

    def _callback_snapshot_locked(self) -> CallbackLaneSnapshot:
        return CallbackLaneSnapshot(
            self._callback_generation,
            self._lane_state,
            self._admitted_count,
            self._in_flight_count,
            self._returned_count,
            CallbackSequence(self._admitted_count),
            CallbackSequence(self._last_ingress_sequence),
            CallbackSequence(self._returned_count),
        )

    def _require_callback_generation(self, value: Any) -> None:
        generation = _canonical_callback_generation(
            value, type(self).__name__, "callback_generation"
        )
        if generation != self._callback_generation:
            raise ValueError("stale callback generation")

    def _require_ticket(self, value: Any) -> CallbackTicket:
        ticket = _canonical_callback_ticket(
            value, type(self).__name__, "ticket"
        )
        if ticket.callback_generation != self._callback_generation:
            raise ValueError("stale callback generation")
        return ticket

    def admit_callback(
        self, callback_generation: Any, sequence: Any
    ) -> CallbackTicket | CallbackLaneRejected:
        self._require_callback_generation(callback_generation)
        canonical_sequence = _canonical_callback_sequence(
            sequence, type(self).__name__, "sequence"
        )
        if not self._callback_guard.acquire(blocking=False):
            return self._busy_rejection()
        try:
            if self._lane_state is not CallbackLaneState.ADMITTING:
                raise PermissionError("callback admission is closed")
            if self._in_flight_count != 0:
                return CallbackLaneRejected(
                    self._callback_generation,
                    CallbackRejectionCode.OVERLAP,
                )
            expected = self._admitted_count + 1
            if (
                expected > MAX_SIGNED_63
                or canonical_sequence.value != expected
            ):
                raise ValueError("stale callback sequence")
            self._after_admission_check_before_publish()
            self._admitted_count = expected
            self._in_flight_count = 1
            self._active_ticket_sequence = expected
            return CallbackTicket(
                self._callback_generation, canonical_sequence
            )
        finally:
            self._callback_guard.release()

    def append_callback(
        self, ticket: Any, frames: Any, timestamp_ns: Any
    ) -> SampleCursor | RingAppendRejected | CallbackLaneRejected:
        canonical_ticket = self._require_ticket(ticket)
        if not self._callback_guard.acquire(blocking=False):
            return self._busy_rejection()
        try:
            if (
                self._in_flight_count != 1
                or canonical_ticket.sequence.value
                != self._active_ticket_sequence
                or self._last_ingress_sequence
                == self._active_ticket_sequence
            ):
                raise ValueError("stale callback ingress sequence")
            if self._ingress_faulted:
                return RingAppendRejected(
                    RingOperationRejectionCode.FAULTED,
                    self._ring.next_cursor,
                )
            if self._append_in_progress:
                return self._busy_rejection()
            pre_cursor = self._ring.next_cursor
            expected_cursor = None
            if (
                type(frames) is np.ndarray
                and frames.ndim == 2
                and frames.shape[0] > 0
                and pre_cursor.value
                <= MAX_SIGNED_63 - frames.shape[0]
            ):
                expected_cursor = pre_cursor.value + frames.shape[0]
            self._append_in_progress = True
        finally:
            self._callback_guard.release()

        try:
            result = self._ring.append(frames, timestamp_ns)
            if type(result) is SampleCursor:
                self._last_ingress_sequence = (
                    canonical_ticket.sequence.value
                )
            return result
        except BaseException:
            post_state = self._ring.snapshot()
            if type(post_state) is RingStateFaulted:
                self._ingress_faulted = True
            else:
                post_cursor = post_state[1]
                if post_cursor == pre_cursor.value:
                    pass
                elif (
                    expected_cursor is not None
                    and post_cursor == expected_cursor
                ):
                    self._last_ingress_sequence = (
                        canonical_ticket.sequence.value
                    )
                else:
                    self._ingress_faulted = True
            raise
        finally:
            self._append_in_progress = False

    def return_callback(
        self, ticket: Any
    ) -> None | CallbackLaneRejected:
        canonical_ticket = self._require_ticket(ticket)
        if not self._callback_guard.acquire(blocking=False):
            return self._busy_rejection()
        try:
            if self._append_in_progress:
                return self._busy_rejection()
            if self._ingress_faulted:
                return self._busy_rejection()
            if (
                self._in_flight_count != 1
                or canonical_ticket.sequence.value
                != self._active_ticket_sequence
            ):
                raise ValueError("stale callback return sequence")
            self._after_return_check_before_publish()
            self._returned_count += 1
            self._in_flight_count = 0
            self._active_ticket_sequence = 0
            if (
                self._lane_state
                is CallbackLaneState.ADMISSION_CLOSED
            ):
                self._lane_state = CallbackLaneState.QUIESCENT
            return None
        finally:
            self._callback_guard.release()

    def close_callback_admission(
        self, callback_generation: Any
    ) -> CallbackLaneSnapshot | CallbackLaneRejected:
        self._require_callback_generation(callback_generation)
        if not self._callback_guard.acquire(blocking=False):
            return self._busy_rejection()
        try:
            if self._lane_state is CallbackLaneState.ADMITTING:
                self._after_close_check_before_publish()
                self._lane_state = (
                    CallbackLaneState.QUIESCENT
                    if self._in_flight_count == 0
                    else CallbackLaneState.ADMISSION_CLOSED
                )
            return self._callback_snapshot_locked()
        finally:
            self._callback_guard.release()

    def quiescence_outcome(
        self,
    ) -> (
        CallbackQuiescenceOutcome
        | CallbackLaneRejected
        | None
    ):
        if not self._callback_guard.acquire(blocking=False):
            return self._busy_rejection()
        try:
            if self._lane_state is not CallbackLaneState.QUIESCENT:
                return None
            return CallbackQuiescenceOutcome(
                self._generation_id,
                self._callback_generation,
                self._callback_generation,
                self._in_flight_count,
                CallbackSequence(self._admitted_count),
                CallbackSequence(self._last_ingress_sequence),
                False,
            )
        finally:
            self._callback_guard.release()

    def record_seeded_owner_joined(self) -> None:
        with self._callback_guard:
            if self._owner_count == MAX_SIGNED_63:
                raise OverflowError("seeded owner count exhausted")
            self._owner_count += 1

    def record_seeded_owner_left(self) -> None:
        with self._callback_guard:
            if self._owner_count == 0:
                raise ValueError("no seeded owner is attached")
            self._owner_count -= 1

    def _after_admission_check_before_publish(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_close_check_before_publish(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_return_check_before_publish(self) -> None:
        """Deterministic test hook; production performs no work here."""


def _build_generation_issuer():
    issuer_marker = object()
    issuer_seal = _GenerationIssuerSeal(issuer_marker)

    def generation_seal_is_valid(candidate: Any) -> bool:
        return candidate is issuer_seal

    def create_generation(
        slot_id: SeededSlotId,
        capacity_frames: Any,
        channels: Any,
    ) -> _ResidentGeneration:
        return _ResidentGeneration(
            issuer_seal, slot_id, capacity_frames, channels
        )

    return create_generation, generation_seal_is_valid


_create_seeded_generation, _generation_seal_is_valid = (
    _build_generation_issuer()
)
