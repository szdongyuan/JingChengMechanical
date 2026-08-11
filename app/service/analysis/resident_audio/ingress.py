"""Non-waiting callback boundary for one resident stream generation."""
from __future__ import annotations

import math
import time
from collections import deque
from threading import Event
from typing import Callable, Mapping

import numpy as np

from .contracts import (
    BlockTimeQuality,
    CaptureErrorCode,
    CapturedAudioBlock,
    RawCallbackBlock,
)
from .timeline import TimelineUpdate


FOUR_BLOCK_INGRESS_CAPACITY = 4


class GenerationLatch:
    """Pre-existing, one-way callback-visible generation state."""

    def __init__(self) -> None:
        # A single deque token gives fail/deactivate a CPython-atomic,
        # non-waiting winner without putting a lock on the callback path.
        self._alive_token: deque[None] = deque((None,))
        # Readiness and failure race through one CPython-atomic deque token.
        # Neither callback failure nor readiness publication waits on a lock.
        self._readiness_token: deque[None] = deque((None,))
        self._failure_code: CaptureErrorCode | None = None
        self._failure_message: str | None = None
        self._dropped_callbacks = 0

    @property
    def alive(self) -> bool:
        return bool(self._alive_token)

    @property
    def failure_code(self) -> CaptureErrorCode | None:
        return self._failure_code

    @property
    def failure_message(self) -> str | None:
        return self._failure_message

    @property
    def dropped_callbacks(self) -> int:
        return self._dropped_callbacks

    def fail(self, code: CaptureErrorCode, message: str) -> bool:
        if not isinstance(code, CaptureErrorCode):
            raise TypeError("code must be a CaptureErrorCode")
        if not isinstance(message, str) or not message:
            raise ValueError("message must be a non-empty string")
        try:
            self._alive_token.popleft()
        except IndexError:
            return False
        self._discard_readiness()
        self._failure_code = code
        self._failure_message = message
        return True

    def deactivate(self) -> bool:
        try:
            self._alive_token.popleft()
        except IndexError:
            return False
        self._discard_readiness()
        return True

    def try_claim_readiness(self) -> bool:
        """Linearize first readiness against non-waiting failure/deactivate."""

        try:
            self._readiness_token.popleft()
        except IndexError:
            return False
        return bool(self._alive_token)

    def mark_dropped_callback(self) -> None:
        self._dropped_callbacks += 1

    def _discard_readiness(self) -> None:
        try:
            self._readiness_token.popleft()
        except IndexError:
            pass


class SpscBlockIngress:
    """A fixed four-entry CPython deque with one producer and one consumer."""

    def __init__(
        self,
        generation_latch: GenerationLatch,
        *,
        wake_event: Event | None = None,
    ) -> None:
        if not isinstance(generation_latch, GenerationLatch):
            raise TypeError("generation_latch must be a GenerationLatch")
        self._generation_latch = generation_latch
        self._wake_event = wake_event if wake_event is not None else Event()
        self._blocks: deque[RawCallbackBlock] = deque()

    @property
    def generation_latch(self) -> GenerationLatch:
        return self._generation_latch

    @property
    def capacity(self) -> int:
        return FOUR_BLOCK_INGRESS_CAPACITY

    @property
    def size(self) -> int:
        return len(self._blocks)

    @property
    def wake_event(self) -> Event:
        return self._wake_event

    def try_push(self, block: RawCallbackBlock) -> bool:
        if not isinstance(block, RawCallbackBlock):
            raise TypeError("block must be a RawCallbackBlock")
        if not self._generation_latch.alive:
            return False
        if len(self._blocks) >= FOUR_BLOCK_INGRESS_CAPACITY:
            self._generation_latch.fail(
                CaptureErrorCode.INPUT_INGRESS_BACKLOG,
                "four-block input ingress is full",
            )
            self._wake_event.set()
            return False
        self._blocks.append(block)
        self._wake_event.set()
        return True

    def try_pop(self) -> RawCallbackBlock | None:
        try:
            block = self._blocks.popleft()
        except IndexError:
            self._wake_event.clear()
            if self._blocks:
                self._wake_event.set()
            return None
        if not self._blocks:
            self._wake_event.clear()
            if self._blocks:
                self._wake_event.set()
        return block

    def notify_failure(self) -> None:
        self._wake_event.set()


class PortAudioCallbackAdapter:
    """Sounddevice-compatible callback restricted to copy and ingress work."""

    def __init__(
        self,
        *,
        pool_epoch: str,
        hardware_id: str,
        stream_generation: int,
        generation_latch: GenerationLatch,
        ingress: SpscBlockIngress,
        monotonic_clock: Callable[[], float] = time.monotonic,
        fatal_status: Callable[[object], bool] | None = None,
    ) -> None:
        if not isinstance(pool_epoch, str) or not pool_epoch:
            raise ValueError("pool_epoch must be a non-empty string")
        if not isinstance(hardware_id, str) or not hardware_id:
            raise ValueError("hardware_id must be a non-empty string")
        if (
            isinstance(stream_generation, bool)
            or not isinstance(stream_generation, int)
            or stream_generation < 1
        ):
            raise ValueError("stream_generation must be a positive integer")
        if not isinstance(generation_latch, GenerationLatch):
            raise TypeError("generation_latch must be a GenerationLatch")
        if not isinstance(ingress, SpscBlockIngress):
            raise TypeError("ingress must be a SpscBlockIngress")
        if ingress.generation_latch is not generation_latch:
            raise ValueError("ingress and callback must share one generation latch")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        if fatal_status is not None and not callable(fatal_status):
            raise TypeError("fatal_status must be callable")

        self._pool_epoch = pool_epoch
        self._hardware_id = hardware_id
        self._stream_generation = stream_generation
        self._generation = generation_latch
        self._ingress = ingress
        self._monotonic_clock = monotonic_clock
        self._fatal_status = fatal_status or _default_fatal_status
        self._next_sequence = 0

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def __call__(
        self,
        indata: object,
        frames: int,
        time_info: object,
        status: object,
    ) -> None:
        if not self._generation.alive:
            self._generation.mark_dropped_callback()
            return
        try:
            input_buffer_adc_time = _time_value(
                time_info, "inputBufferAdcTime"
            )
            current_time = _time_value(time_info, "currentTime")
            sequence = self._next_sequence
            callback_monotonic = self._monotonic_clock()
            self._next_sequence += 1
            if self._fatal_status(status):
                self._generation.fail(
                    CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                    "fatal PortAudio callback status",
                )
                self._ingress.notify_failure()
                return
            data = np.ascontiguousarray(indata, dtype=np.float32).copy()
            block = RawCallbackBlock(
                pool_epoch=self._pool_epoch,
                hardware_id=self._hardware_id,
                stream_generation=self._stream_generation,
                callback_sequence=sequence,
                callback_monotonic=callback_monotonic,
                input_buffer_adc_time=input_buffer_adc_time,
                current_time=current_time,
                status=status,
                frames=frames,
                data=data,
            )
            self._ingress.try_push(block)
        except Exception:
            self._generation.fail(
                CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                "unrecoverable PortAudio callback boundary error",
            )
            self._ingress.notify_failure()


def captured_block_from_raw(
    raw: RawCallbackBlock,
    update: TimelineUpdate,
    *,
    sample_rate: float,
) -> CapturedAudioBlock:
    """Build the shared captured block without copying callback-owned data."""

    if not isinstance(raw, RawCallbackBlock):
        raise TypeError("raw must be a RawCallbackBlock")
    if not isinstance(update, TimelineUpdate):
        raise TypeError("update must be a TimelineUpdate")
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, (int, float))
        or not math.isfinite(float(sample_rate))
        or float(sample_rate) <= 0
    ):
        raise ValueError("sample_rate must be positive and finite")
    if (
        update.sample_end - update.sample_start != raw.frames
        or update.sample_end < update.sample_start
    ):
        raise ValueError("timeline update range must match raw callback frames")

    if update.block_start_monotonic is None:
        quality = BlockTimeQuality.RECOVERY_UNCONFIRMED
        monotonic_start = None
        monotonic_end = None
    else:
        quality = BlockTimeQuality.CONFIRMED
        monotonic_start = update.block_start_monotonic
        monotonic_end = monotonic_start + raw.frames / float(sample_rate)
    return CapturedAudioBlock(
        pool_epoch=raw.pool_epoch,
        hardware_id=raw.hardware_id,
        stream_generation=raw.stream_generation,
        sample_start=update.sample_start,
        sample_end=update.sample_end,
        data=raw.data,
        time_quality=quality,
        monotonic_start=monotonic_start,
        monotonic_end=monotonic_end,
        status_summary=str(raw.status),
    )


def _time_value(time_info: object, name: str) -> object:
    if isinstance(time_info, Mapping):
        return time_info[name]
    return getattr(time_info, name)


def _default_fatal_status(status: object) -> bool:
    return bool(status)
