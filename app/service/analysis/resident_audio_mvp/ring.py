"""Fixed resident storage and deterministic monotonic-time mapping."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

import numpy as np


@dataclass(frozen=True, slots=True)
class _BlockMetadata:
    start_cursor: int
    end_cursor: int
    frame_count: int
    callback_monotonic_ns: int
    estimated_start_ns: int
    estimated_end_ns: int


class _MappingStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_YET_AVAILABLE = "NOT_YET_AVAILABLE"
    EXPIRED = "EXPIRED"
    TIMELINE_GAP = "TIMELINE_GAP"
    STREAM_FAULT = "STREAM_FAULT"


@dataclass(frozen=True, slots=True)
class _MappingResult:
    status: _MappingStatus
    cursor: int | None = None


class _RingTimelineFault(ValueError):
    """An accepted-stream timeline would become ambiguous or discontinuous."""


class _ResidentRing:
    __slots__ = (
        "_blocks",
        "_capacity_frames",
        "_channel_count",
        "_frames_per_buffer",
        "_logical_frames",
        "_oldest_cursor",
        "_sample_rate_hz",
        "_storage",
        "_write_cursor",
    )

    def __init__(self, capacity_frames: int, channel_count: int) -> None:
        self._capacity_frames = capacity_frames
        self._channel_count = channel_count
        self._storage = np.empty(
            (capacity_frames, channel_count),
            dtype=np.float32,
        )
        self._oldest_cursor = 0
        self._write_cursor = 0
        self._sample_rate_hz: int | None = None
        self._frames_per_buffer: int | None = None
        self._logical_frames: int | None = None
        self._blocks: deque[_BlockMetadata] = deque(maxlen=capacity_frames)

    def configure_timeline(
        self,
        *,
        sample_rate_hz: int,
        frames_per_buffer: int,
    ) -> None:
        if self._write_cursor or self._blocks:
            raise RuntimeError("ring timeline is already active")
        if (
            type(sample_rate_hz) is not int
            or sample_rate_hz < 1
            or type(frames_per_buffer) is not int
            or frames_per_buffer < 1
        ):
            raise ValueError("invalid ring timeline configuration")
        logical_frames = min(math.ceil(sample_rate_hz * 10.0),
                             self._capacity_frames - frames_per_buffer)
        if logical_frames < 1:
            raise ValueError("ring capacity is smaller than its logical window")
        self._sample_rate_hz = sample_rate_hz
        self._frames_per_buffer = frames_per_buffer
        self._logical_frames = logical_frames

    @property
    def capacity_frames(self) -> int:
        return self._capacity_frames

    @property
    def oldest_cursor(self) -> int:
        return self._oldest_cursor

    @property
    def write_cursor(self) -> int:
        return self._write_cursor

    @property
    def retained_frames(self) -> int:
        return self._write_cursor - self._oldest_cursor

    @property
    def block_metadata(self) -> tuple[_BlockMetadata, ...]:
        return tuple(self._blocks)

    def append(
        self,
        samples: np.ndarray,
        *,
        callback_monotonic_ns: int | None = None,
    ) -> None:
        sample_rate_hz = self._sample_rate_hz
        frames_per_buffer = self._frames_per_buffer
        logical_frames = self._logical_frames
        if (
            sample_rate_hz is None
            or frames_per_buffer is None
            or logical_frames is None
        ):
            if callback_monotonic_ns is not None:
                raise RuntimeError("ring timeline is not configured")
            self._append_legacy(samples)
            return
        if (
            not isinstance(samples, np.ndarray)
            or samples.dtype != np.dtype(np.float32)
            or samples.ndim != 2
            or not samples.flags.c_contiguous
            or samples.shape[1] != self._channel_count
        ):
            raise ValueError("invalid ring audio block")
        frame_count = samples.shape[0]
        if frame_count < 1 or frame_count > frames_per_buffer:
            raise ValueError("audio block exceeds callback frame bound")
        if (
            type(callback_monotonic_ns) is not int
            or callback_monotonic_ns < 0
        ):
            raise ValueError("invalid callback monotonic timestamp")

        self.append_ingress(
            samples,
            callback_monotonic_ns=callback_monotonic_ns,
        )

    def append_ingress(
        self,
        samples: np.ndarray,
        *,
        callback_monotonic_ns: int,
    ) -> None:
        """Append one block already normalized by callback ingress."""
        sample_rate_hz = self._sample_rate_hz
        logical_frames = self._logical_frames
        if sample_rate_hz is None or logical_frames is None:
            raise RuntimeError("ring timeline is not configured")
        frame_count = samples.shape[0]

        duration_ns = round(frame_count * 1_000_000_000 / sample_rate_hz)
        estimated_start_ns = callback_monotonic_ns - duration_ns
        if estimated_start_ns < 0:
            raise ValueError("callback anchor precedes the monotonic epoch")
        previous = self._blocks[-1] if self._blocks else None
        if (
            previous is not None
            and callback_monotonic_ns < previous.callback_monotonic_ns
        ):
            raise _RingTimelineFault("callback timestamp rollback")
        if previous is not None:
            if previous.end_cursor != self._write_cursor:
                raise _RingTimelineFault("absolute cursor discontinuity")

        start_cursor = self._write_cursor
        end_cursor = start_cursor + frame_count
        metadata = _BlockMetadata(
            start_cursor=start_cursor,
            end_cursor=end_cursor,
            frame_count=frame_count,
            callback_monotonic_ns=callback_monotonic_ns,
            estimated_start_ns=estimated_start_ns,
            estimated_end_ns=callback_monotonic_ns,
        )

        self._copy_into_storage(start_cursor, samples)
        self._write_cursor = end_cursor
        self._blocks.append(metadata)
        cutoff_cursor = max(0, end_cursor - logical_frames)
        while self._blocks and self._blocks[0].end_cursor <= cutoff_cursor:
            self._blocks.popleft()
        self._oldest_cursor = (
            self._blocks[0].start_cursor if self._blocks else self._write_cursor
        )

    def _append_legacy(self, samples: np.ndarray) -> None:
        frame_count = samples.shape[0]
        if frame_count > self._capacity_frames:
            raise ValueError("audio block exceeds ring capacity")
        self._copy_into_storage(self._write_cursor, samples)
        self._write_cursor += frame_count
        self._oldest_cursor = max(
            self._oldest_cursor,
            self._write_cursor - self._capacity_frames,
        )

    def _copy_into_storage(
        self,
        start_cursor: int,
        samples: np.ndarray,
    ) -> None:
        frame_count = samples.shape[0]
        offset = start_cursor % self._capacity_frames
        first_count = min(frame_count, self._capacity_frames - offset)
        self._storage[offset : offset + first_count] = samples[:first_count]
        remaining = frame_count - first_count
        if remaining:
            self._storage[:remaining] = samples[first_count:]

    def read(self, start_cursor: int, frame_count: int) -> np.ndarray:
        end_cursor = start_cursor + frame_count
        if (
            type(start_cursor) is not int
            or type(frame_count) is not int
            or start_cursor < self._oldest_cursor
            or frame_count < 0
            or end_cursor > self._write_cursor
        ):
            raise ValueError("requested frames are outside the resident ring")
        result = np.empty((frame_count, self._channel_count), dtype=np.float32)
        offset = start_cursor % self._capacity_frames
        first_count = min(frame_count, self._capacity_frames - offset)
        result[:first_count] = self._storage[offset : offset + first_count]
        remaining = frame_count - first_count
        if remaining:
            result[first_count:] = self._storage[:remaining]
        return result

    def copy_into(
        self,
        start_cursor: int,
        destination: np.ndarray,
        destination_start: int,
        frame_count: int,
    ) -> None:
        """Copy an exact retained half-open range into preallocated storage."""
        end_cursor = start_cursor + frame_count
        destination_end = destination_start + frame_count
        if (
            type(start_cursor) is not int
            or type(destination_start) is not int
            or type(frame_count) is not int
            or frame_count < 0
            or start_cursor < self._oldest_cursor
            or end_cursor > self._write_cursor
            or not isinstance(destination, np.ndarray)
            or destination.dtype != np.dtype(np.float32)
            or destination.ndim != 2
            or destination.shape[1] != self._channel_count
            or destination_start < 0
            or destination_end > destination.shape[0]
        ):
            raise ValueError("invalid exact ring copy")
        offset = start_cursor % self._capacity_frames
        first_count = min(frame_count, self._capacity_frames - offset)
        destination[destination_start : destination_start + first_count] = (
            self._storage[offset : offset + first_count]
        )
        remaining = frame_count - first_count
        if remaining:
            destination[destination_start + first_count : destination_end] = (
                self._storage[:remaining]
            )

    def map_trigger(self, trigger_monotonic_ns: int) -> _MappingResult:
        if type(trigger_monotonic_ns) is not int or trigger_monotonic_ns < 0:
            raise ValueError("invalid trigger monotonic timestamp")
        sample_rate_hz = self._sample_rate_hz
        frames_per_buffer = self._frames_per_buffer
        if sample_rate_hz is None or frames_per_buffer is None:
            raise RuntimeError("ring timeline is not configured")
        blocks = tuple(self._blocks)
        if not blocks:
            return _MappingResult(_MappingStatus.NOT_YET_AVAILABLE)
        previous: _BlockMetadata | None = None
        for item in blocks:
            if (
                item.start_cursor < self._oldest_cursor
                or item.end_cursor > self._write_cursor
                or item.end_cursor - item.start_cursor != item.frame_count
                or (
                    previous is not None
                    and item.start_cursor != previous.end_cursor
                )
                or (
                    previous is not None
                    and item.callback_monotonic_ns
                    < previous.callback_monotonic_ns
                )
            ):
                return _MappingResult(_MappingStatus.STREAM_FAULT)
            previous = item

        newest = blocks[-1]
        logical_cutoff_ns = newest.estimated_end_ns - round(
            self._logical_frames * 1_000_000_000 / sample_rate_hz
        )
        if trigger_monotonic_ns < logical_cutoff_ns:
            return _MappingResult(_MappingStatus.EXPIRED)

        candidates: list[int] = []
        for item in blocks:
            if (
                item.estimated_start_ns
                <= trigger_monotonic_ns
                < item.estimated_end_ns
            ):
                delta_ns = trigger_monotonic_ns - item.estimated_start_ns
                offset = (
                    delta_ns * sample_rate_hz + 1_000_000_000 - 1
                ) // 1_000_000_000
                candidates.append(
                    min(item.end_cursor, item.start_cursor + offset)
                )
        if candidates:
            return _MappingResult(_MappingStatus.AVAILABLE, min(candidates))
        if trigger_monotonic_ns >= newest.estimated_end_ns:
            return _MappingResult(_MappingStatus.NOT_YET_AVAILABLE)
        for left, right in zip(blocks, blocks[1:]):
            if (
                left.estimated_end_ns
                <= trigger_monotonic_ns
                < right.estimated_start_ns
            ):
                return _MappingResult(
                    _MappingStatus.AVAILABLE,
                    right.start_cursor,
                )
        return _MappingResult(_MappingStatus.EXPIRED)
