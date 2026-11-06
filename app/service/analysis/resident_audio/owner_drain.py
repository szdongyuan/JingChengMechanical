"""Drain-thread mechanics mixed into :class:`CaptureOwner`."""

from __future__ import annotations

import math
import uuid

from .contracts import (
    CaptureErrorCode,
    CaptureOwnerState,
    CapturedAudioBlock,
)
from .owner_types import (
    OWNER_REFERENCE_CREDITS,
    OwnerControlEvent,
    OwnerControlKind,
    TerminalDecision,
)
from .ring import RingReadStatus


class CaptureOwnerDrainMixin:
    """Implements catch-up, live persistence, freeze, and drain cleanup."""

    def _drain_loop(self) -> None:
        while True:
            action, value = self._next_drain_action()
            if action == "EXIT":
                return
            if action == "TERMINAL":
                self._settle_terminal()
                return
            if action == "CONTROL":
                self._send_control(value)
                continue
            if action == "FREEZE":
                self._freeze()
                continue
            if action == "WRITE":
                audio_block = value
                value = None
                self._write_reference(audio_block)

    def _next_drain_action(self) -> tuple[str, object | None]:
        while True:
            with self._condition:
                if (
                    self._decision is not None
                    and self._state is not CaptureOwnerState.COMPLETED
                ):
                    return "TERMINAL", None
                if self._state is CaptureOwnerState.COMPLETED:
                    return "EXIT", None
                if self._state is CaptureOwnerState.FREEZING:
                    return "FREEZE", None
                if self._state is CaptureOwnerState.WAITING_FOR_MAPPING:
                    deadline = self._mapping_deadline
                    try:
                        now = float(self._monotonic_clock())
                    except (TypeError, ValueError, OverflowError):
                        now = math.inf
                    if (
                        deadline is None
                        or not math.isfinite(now)
                        or now >= deadline
                    ):
                        control = self._choose_terminal_locked(
                            CaptureOwnerState.FAILED,
                            CaptureErrorCode.TIMING_UNAVAILABLE,
                            "trigger did not become precisely mappable "
                            "before its deadline",
                        )
                        return "CONTROL", control
                    self._condition.wait(timeout=deadline - now)
                    continue
                if (
                    not self._drain_enabled
                    or self._state is CaptureOwnerState.PREPARED
                ):
                    self._condition.wait()
                    continue
                if self._state is CaptureOwnerState.LIVE:
                    if not self._live_queue:
                        self._condition.wait()
                        continue
                    audio_block = self._live_queue.popleft()
                    self._in_flight = audio_block
                    return "WRITE", audio_block
                if self._state is not CaptureOwnerState.CATCHING_UP:
                    self._condition.wait()
                    continue
                if self._catch_up_batch:
                    audio_block = self._catch_up_batch.popleft()
                    self._in_flight = audio_block
                    return "WRITE", audio_block
                if self._switch_request_pending:
                    self._condition.wait()
                    continue
                available = OWNER_REFERENCE_CREDITS - self._credits_occupied
                cursor = self._next_write_cursor
                self._credits_occupied += available
                self._peak_credits = max(
                    self._peak_credits, self._credits_occupied
                )
            if cursor is None:
                with self._condition:
                    self._credits_occupied -= available
                self.fail(
                    CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                    "catch-up owner lost its persistence cursor",
                )
                continue
            try:
                batch = self._ring.references_from(cursor, available)
            except RuntimeError:
                # The stream is the ring's sole writer, while this drain is its
                # bounded reader. A deque mutation during iteration is retried
                # after returning all credits reserved before the read.
                with self._condition:
                    self._credits_occupied -= available
                continue
            with self._condition:
                if (
                    self._decision is not None
                    or self._state is not CaptureOwnerState.CATCHING_UP
                    or self._next_write_cursor != cursor
                ):
                    self._credits_occupied -= available
                    continue
                if batch.status is RingReadStatus.EXPIRED:
                    self._credits_occupied -= available
                    control = self._choose_terminal_locked(
                        CaptureOwnerState.FAILED,
                        CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                        "mapped catch-up cursor was evicted from the ring",
                    )
                    return "CONTROL", control
                if batch.status is RingReadStatus.NOT_YET_AVAILABLE:
                    self._credits_occupied -= available
                    self._switch_request_pending = True
                    return "CONTROL", self._control_event(
                        OwnerControlKind.REQUEST_LIVE_SWITCH
                    )
                references = batch.references
                self._catch_up_batch.extend(references)
                self._credits_occupied -= available - len(references)

    def _write_reference(self, audio_block: CapturedAudioBlock) -> None:
        try:
            written = self._writer.write(audio_block)
        except Exception as error:
            audio_block = None
            with self._condition:
                self._release_in_flight_locked()
                control = self._choose_terminal_locked(
                    CaptureOwnerState.FAILED,
                    self._error_code(error),
                    f"artifact write failed: {error}",
                )
            self._send_control(control)
            return

        sample_end = audio_block.sample_end
        audio_block = None
        control: OwnerControlEvent | None = None
        with self._condition:
            if self._in_flight is not None:
                capture_end = self._capture_end
                if capture_end is None:
                    raise RuntimeError("mapped owner lost its capture end")
                self._next_write_cursor = min(sample_end, capture_end)
                self._written_frames += written
            self._release_in_flight_locked()
            if (
                self._decision is None
                and self._written_frames == self._target_frames
            ):
                self._clear_pending_references_locked()
                self._transition_state_locked(CaptureOwnerState.FREEZING)
                control = self._detach_once_locked()
            self._condition.notify_all()
        self._send_control(control)

    def _freeze(self) -> None:
        try:
            artifact = self._writer.freeze()
        except Exception as error:
            control = self._choose_terminal(
                CaptureOwnerState.FAILED,
                self._error_code(error),
                f"artifact freeze failed: {error}",
            )
            self._send_control(control)
            return

        with self._condition:
            self._artifact = artifact
            if self._decision is None:
                self._decision = TerminalDecision(
                    CaptureOwnerState.COMPLETED,
                    None,
                    None,
                    uuid.uuid4().hex,
                )
                self._transition_state_locked(CaptureOwnerState.COMPLETED)
            decision = self._decision
        self._emit_terminal(decision)

    def _settle_terminal(self) -> None:
        with self._condition:
            self._release_in_flight_locked()
            self._clear_pending_references_locked()
            decision = self._decision
            artifact = self._artifact
        if artifact is None:
            try:
                self._writer.discard()
            except Exception:
                pass
        self._emit_terminal(decision)


__all__ = ["CaptureOwnerDrainMixin"]
