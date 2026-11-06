"""Bounded persistence owner for one resident audio capture."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from typing import Callable

from .contracts import (
    CaptureErrorCode,
    CaptureLayerError,
    CaptureOwnerState,
    CaptureRequest,
    CapturedAudioBlock,
)
from .ring import TimestampedBlockRing
from .owner_drain import CaptureOwnerDrainMixin
from .owner_mapping import CaptureOwnerMappingMixin
from .owner_types import (
    MAPPING_TIMEOUT_SECONDS,
    OWNER_REFERENCE_CREDITS,
    ArtifactWriter,
    CaptureOwnerSnapshot,
    CommitToken,
    DequeLiveReferenceQueue,
    LiveReferenceQueue,
    OwnerControlEvent,
    OwnerControlKind,
    OwnerTerminalEvent,
    TerminalDecision,
)


class CaptureOwner(CaptureOwnerMappingMixin, CaptureOwnerDrainMixin):
    """One independently drained capture with a strict eight-reference bound."""

    def __init__(
        self,
        *,
        request: CaptureRequest,
        target_frames: int,
        commit_attempt_id: str,
        owner_capability: str,
        ring: TimestampedBlockRing,
        writer: ArtifactWriter,
        terminal_sink: Callable[[OwnerTerminalEvent], None],
        control_sink: Callable[[OwnerControlEvent], None],
        monotonic_clock: Callable[[], float] = time.monotonic,
        live_queue: LiveReferenceQueue | None = None,
        commit_token: CommitToken | None = None,
    ) -> None:
        if not isinstance(request, CaptureRequest):
            raise TypeError("request must be a CaptureRequest")
        if (
            isinstance(target_frames, bool)
            or not isinstance(target_frames, int)
            or target_frames < 1
        ):
            raise ValueError("target_frames must be a positive integer")
        expected_frames = math.ceil(
            request.record_duration * request.stream_token.sample_rate
        )
        if target_frames != expected_frames:
            raise ValueError(
                "target_frames must equal ceil(duration * actual sample rate)"
            )
        for value, name in (
            (commit_attempt_id, "commit_attempt_id"),
            (owner_capability, "owner_capability"),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(ring, TimestampedBlockRing):
            raise TypeError("ring must be a TimestampedBlockRing")
        if not callable(terminal_sink) or not callable(control_sink):
            raise TypeError("owner sinks must be callable")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        if commit_token is not None and not isinstance(commit_token, CommitToken):
            raise TypeError("commit_token must be a CommitToken")
        if live_queue is not None:
            for method_name in ("try_push", "popleft", "clear", "__len__"):
                if not callable(getattr(live_queue, method_name, None)):
                    raise TypeError(
                        "live_queue must provide nonblocking queue operations"
                    )

        self._request = request
        self._target_frames = target_frames
        self._commit_attempt_id = commit_attempt_id
        self._owner_capability = owner_capability
        self._ring = ring
        self._writer = writer
        self._terminal_sink = terminal_sink
        self._control_sink = control_sink
        self._monotonic_clock = monotonic_clock
        self._commit_token = commit_token or CommitToken()

        self._condition = threading.Condition()
        self._state = CaptureOwnerState.PREPARED
        self._previous_state: CaptureOwnerState | None = None
        self._mapping_deadline: float | None = None
        self._capture_start: int | None = None
        self._capture_end: int | None = None
        self._next_write_cursor: int | None = None
        self._accepted_cursor: int | None = None
        self._written_frames = 0

        self._catch_up_batch: deque[CapturedAudioBlock] = deque()
        self._live_queue = (
            DequeLiveReferenceQueue() if live_queue is None else live_queue
        )
        self._in_flight: CapturedAudioBlock | None = None
        self._credits_occupied = 0
        self._peak_credits = 0
        self._switch_request_pending = False
        self._drain_enabled = True
        self._detached = False

        self._decision: TerminalDecision | None = None
        self._artifact = None
        self._terminal_emitted = False
        self._thread = threading.Thread(
            target=self._drain_loop,
            name=f"resident-capture-{request.run_id}",
            daemon=True,
        )
        self._thread.start()

    @property
    def run_id(self) -> str:
        return self._request.run_id

    @property
    def commit_attempt_id(self) -> str:
        return self._commit_attempt_id

    @property
    def owner_capability(self) -> str:
        return self._owner_capability

    @property
    def pool_epoch(self) -> str:
        return self._request.stream_token.pool_epoch

    @property
    def stream_generation(self) -> int:
        return self._request.stream_token.stream_generation

    def snapshot(self) -> CaptureOwnerSnapshot:
        with self._condition:
            return CaptureOwnerSnapshot(
                state=self._state,
                previous_state=self._previous_state,
                commit_open=self._commit_token.is_open,
                original_trigger_monotonic=self._request.trigger_monotonic,
                mapping_deadline=self._mapping_deadline,
                capture_start_cursor=self._capture_start,
                capture_end_cursor=self._capture_end,
                next_write_cursor=self._next_write_cursor,
                written_frames=self._written_frames,
                credits_occupied=self._credits_occupied,
                peak_credits=self._peak_credits,
                catch_up_references=len(self._catch_up_batch),
                live_queued=len(self._live_queue),
                in_flight=self._in_flight is not None,
                terminal_attempt_id=(
                    None if self._decision is None else self._decision.attempt_id
                ),
                drain_thread_name=(
                    None if self._thread is None else self._thread.name
                ),
                drain_thread_alive=bool(
                    self._thread is not None and self._thread.is_alive()
                ),
            )

    def open_commit_token(self) -> bool:
        with self._condition:
            if self._decision is not None:
                return False
            if self._state is not CaptureOwnerState.PREPARED:
                return self._commit_token.is_open
            return self._commit_token.open()

    def start_drain(self) -> None:
        with self._condition:
            self._drain_enabled = True
            self._condition.notify_all()

    def notify_ring_extended(self) -> None:
        with self._condition:
            if self._state is CaptureOwnerState.CATCHING_UP:
                self._switch_request_pending = False
                self._condition.notify_all()

    def switch_to_live(self, stream_cursor: int) -> bool:
        if (
            isinstance(stream_cursor, bool)
            or not isinstance(stream_cursor, int)
            or stream_cursor < 0
        ):
            raise ValueError("stream_cursor must be a non-negative integer")
        with self._condition:
            if self._state is not CaptureOwnerState.CATCHING_UP:
                return self._state is CaptureOwnerState.LIVE
            self._switch_request_pending = False
            empty = (
                self._credits_occupied == 0
                and not self._catch_up_batch
                and not self._live_queue
                and self._in_flight is None
            )
            if empty and self._next_write_cursor == stream_cursor:
                self._transition_state_locked(CaptureOwnerState.LIVE)
                self._accepted_cursor = stream_cursor
                self._condition.notify_all()
                return True
            self._condition.notify_all()
            return False

    def offer_live(self, audio_block: CapturedAudioBlock) -> bool:
        if not isinstance(audio_block, CapturedAudioBlock):
            raise TypeError("audio_block must be a CapturedAudioBlock")
        control: OwnerControlEvent | None = None
        with self._condition:
            if self._state is not CaptureOwnerState.LIVE:
                return False
            if not self._block_identity_matches(audio_block):
                control = self._choose_terminal_locked(
                    CaptureOwnerState.FAILED,
                    CaptureErrorCode.STREAM_GENERATION_CHANGED,
                    "live audio block belongs to a different stream generation",
                )
            else:
                accepted = self._accepted_cursor
                capture_end = self._capture_end
                if accepted is None or capture_end is None:
                    raise RuntimeError("live owner has no mapped capture range")
                if accepted >= capture_end:
                    return True
                if audio_block.sample_start != accepted:
                    control = self._choose_terminal_locked(
                        CaptureOwnerState.FAILED,
                        CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                        "live audio references are discontinuous",
                    )
                elif self._credits_occupied == OWNER_REFERENCE_CREDITS:
                    control = self._choose_terminal_locked(
                        CaptureOwnerState.FAILED,
                        CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                        "owner exhausted all eight unwritten-reference credits",
                    )
                else:
                    queued_before_push = len(self._live_queue)
                    self._credits_occupied += 1
                    self._peak_credits = max(
                        self._peak_credits, self._credits_occupied
                    )
                    try:
                        pushed = self._live_queue.try_push(audio_block)
                    except Exception:
                        pushed = False
                    if not pushed:
                        self._live_queue.clear()
                        self._credits_occupied -= 1 + queued_before_push
                        if self._credits_occupied < 0:
                            raise RuntimeError(
                                "owner reference credit underflow"
                            )
                        control = self._choose_terminal_locked(
                            CaptureOwnerState.FAILED,
                            CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                            "owner live reference queue rejected a credited block",
                        )
                    else:
                        self._accepted_cursor = min(
                            audio_block.sample_end, capture_end
                        )
                        self._condition.notify_all()
                        return True
        self._send_control(control)
        return False

    def cancel(self, reason: str = "capture canceled") -> bool:
        with self._condition:
            if self._decision is not None:
                return False
            control = self._choose_terminal_locked(
                CaptureOwnerState.CANCELED,
                CaptureErrorCode.CAPTURE_CANCELED,
                reason,
            )
        self._send_control(control)
        return True

    def shutdown(self) -> bool:
        with self._condition:
            if self._decision is not None:
                return False
            control = self._choose_terminal_locked(
                CaptureOwnerState.CANCELED,
                CaptureErrorCode.POOL_SHUTTING_DOWN,
                "capture owner stopped during pool shutdown",
            )
        self._send_control(control)
        return True

    def fail(self, code: CaptureErrorCode, message: str) -> bool:
        if not isinstance(code, CaptureErrorCode):
            raise TypeError("code must be a CaptureErrorCode")
        with self._condition:
            if self._decision is not None:
                return False
            control = self._choose_terminal_locked(
                CaptureOwnerState.FAILED, code, message
            )
        self._send_control(control)
        return True

    def stream_generation_changed(self) -> bool:
        return self.stream_failed(
            CaptureErrorCode.STREAM_GENERATION_CHANGED,
            "resident stream generation changed",
        )

    def stream_failed(self, code: CaptureErrorCode, message: str) -> bool:
        if not isinstance(code, CaptureErrorCode):
            raise TypeError("code must be a CaptureErrorCode")
        with self._condition:
            if self._state in (
                CaptureOwnerState.FREEZING,
                CaptureOwnerState.COMPLETED,
            ):
                return False
        return self.fail(code, message)

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _emit_terminal(self, decision: TerminalDecision | None) -> None:
        if decision is None:
            return
        with self._condition:
            if self._terminal_emitted:
                return
            self._terminal_emitted = True
            artifact = self._artifact
            event = OwnerTerminalEvent(
                terminal_attempt_id=decision.attempt_id,
                run_id=self.run_id,
                commit_attempt_id=self._commit_attempt_id,
                owner_capability=self._owner_capability,
                pool_epoch=self.pool_epoch,
                stream_generation=self.stream_generation,
                state=decision.state,
                artifact_id=self._writer.artifact_id,
                publication_token=(
                    None if artifact is None else artifact.publication_token
                ),
                artifact=artifact,
                error_code=decision.error_code,
                message=decision.message,
            )
        try:
            self._terminal_sink(event)
        except Exception:
            # The terminal attempt remains one-shot. Pool integration uses a
            # non-throwing command queue sink.
            pass

    def _choose_terminal(
        self,
        state: CaptureOwnerState,
        code: CaptureErrorCode,
        message: str,
    ) -> OwnerControlEvent | None:
        with self._condition:
            return self._choose_terminal_locked(state, code, message)

    def _choose_terminal_locked(
        self,
        state: CaptureOwnerState,
        code: CaptureErrorCode,
        message: str,
    ) -> OwnerControlEvent | None:
        if self._decision is not None:
            return None
        self._decision = TerminalDecision(
            state=state,
            error_code=code,
            message=str(message),
            attempt_id=uuid.uuid4().hex,
        )
        self._transition_state_locked(state)
        control = self._detach_once_locked()
        self._condition.notify_all()
        return control

    def _transition_state_locked(self, state: CaptureOwnerState) -> None:
        if state is self._state:
            return
        self._previous_state = self._state
        self._state = state

    def _detach_once_locked(self) -> OwnerControlEvent | None:
        if self._detached:
            return None
        self._detached = True
        return self._control_event(OwnerControlKind.DETACH)

    def _release_in_flight_locked(self) -> None:
        if self._in_flight is not None:
            self._in_flight = None
            self._credits_occupied -= 1
            if self._credits_occupied < 0:
                raise RuntimeError("owner reference credit underflow")

    def _clear_pending_references_locked(self) -> None:
        released = len(self._catch_up_batch) + len(self._live_queue)
        self._catch_up_batch.clear()
        self._live_queue.clear()
        self._credits_occupied -= released
        if self._credits_occupied < 0:
            raise RuntimeError("owner reference credit underflow")

    def _control_event(self, kind: OwnerControlKind) -> OwnerControlEvent:
        return OwnerControlEvent(
            kind=kind,
            owner=self,
            run_id=self.run_id,
            commit_attempt_id=self._commit_attempt_id,
            owner_capability=self._owner_capability,
            pool_epoch=self.pool_epoch,
            stream_generation=self.stream_generation,
        )

    def _send_control(self, event: object | None) -> None:
        if isinstance(event, OwnerControlEvent):
            try:
                self._control_sink(event)
            except Exception as error:
                if event.kind is OwnerControlKind.REQUEST_LIVE_SWITCH:
                    self.fail(
                        CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG,
                        f"live switch command failed: {error}",
                    )

    def _block_identity_matches(self, block: CapturedAudioBlock) -> bool:
        return (
            block.pool_epoch == self.pool_epoch
            and block.hardware_id == self._request.stream_token.hardware_id
            and block.stream_generation == self.stream_generation
        )

    @staticmethod
    def _error_code(error: Exception) -> CaptureErrorCode:
        if isinstance(error, CaptureLayerError):
            return error.code
        return CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED


__all__ = [
    "CaptureOwner",
    "CaptureOwnerSnapshot",
    "MAPPING_TIMEOUT_SECONDS",
    "OWNER_REFERENCE_CREDITS",
    "OwnerControlEvent",
    "OwnerControlKind",
    "OwnerTerminalEvent",
]
