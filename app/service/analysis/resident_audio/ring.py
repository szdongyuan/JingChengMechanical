"""Generation-scoped rolling references for resident audio blocks."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .contracts import (
    CaptureErrorCode,
    CaptureLayerError,
    CapturedAudioBlock,
)


class RingReadStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    EXPIRED = "EXPIRED"
    NOT_YET_AVAILABLE = "NOT_YET_AVAILABLE"


@dataclass(frozen=True, slots=True)
class RingReferenceBatch:
    status: RingReadStatus
    references: tuple[CapturedAudioBlock, ...] = ()
    next_cursor: int | None = None


class _TimelineTrimmer(Protocol):
    def trim_before(self, sample_cursor: int) -> None: ...


class TimestampedBlockRing:
    """A single-writer, whole-block ring for one stream generation."""

    def __init__(
        self,
        *,
        pool_epoch: str,
        hardware_id: str,
        stream_generation: int,
        sample_rate: float,
        timeline: _TimelineTrimmer | None = None,
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
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, (int, float))
            or not math.isfinite(float(sample_rate))
            or float(sample_rate) <= 0
        ):
            raise ValueError("sample_rate must be positive and finite")
        self._pool_epoch = pool_epoch
        self._hardware_id = hardware_id
        self._stream_generation = stream_generation
        self._minimum_frames = math.ceil(float(sample_rate) * 2.0)
        self._timeline = timeline
        self._blocks: deque[CapturedAudioBlock] = deque()
        self._retained_frames = 0
        self._earliest_cursor: int | None = None
        self._latest_cursor: int | None = None

    @property
    def minimum_frames(self) -> int:
        return self._minimum_frames

    @property
    def retained_frames(self) -> int:
        return self._retained_frames

    @property
    def block_count(self) -> int:
        return len(self._blocks)

    @property
    def earliest_cursor(self) -> int | None:
        return self._earliest_cursor

    @property
    def latest_cursor(self) -> int | None:
        return self._latest_cursor

    @property
    def earliest_monotonic(self) -> float | None:
        return None if not self._blocks else self._blocks[0].monotonic_start

    @property
    def latest_monotonic(self) -> float | None:
        return None if not self._blocks else self._blocks[-1].monotonic_end

    def append(self, block: CapturedAudioBlock) -> None:
        if not isinstance(block, CapturedAudioBlock):
            raise TypeError("block must be a CapturedAudioBlock")
        if (
            block.pool_epoch != self._pool_epoch
            or block.hardware_id != self._hardware_id
            or block.stream_generation != self._stream_generation
        ):
            raise CaptureLayerError(
                CaptureErrorCode.STREAM_GENERATION_CHANGED,
                "audio block does not belong to this ring generation",
                {
                    "hardware_id": block.hardware_id,
                    "pool_epoch": block.pool_epoch,
                    "stream_generation": block.stream_generation,
                },
            )
        if (
            self._latest_cursor is not None
            and block.sample_start != self._latest_cursor
        ):
            raise CaptureLayerError(
                CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                "audio block sample range is discontinuous",
                {
                    "expected_sample_start": self._latest_cursor,
                    "actual_sample_start": block.sample_start,
                },
            )

        self._blocks.append(block)
        self._retained_frames += block.sample_end - block.sample_start
        self._latest_cursor = block.sample_end
        if self._earliest_cursor is None:
            self._earliest_cursor = block.sample_start

        evicted = False
        while (
            len(self._blocks) > 1
            and self._retained_frames
            - (self._blocks[0].sample_end - self._blocks[0].sample_start)
            >= self._minimum_frames
        ):
            oldest = self._blocks.popleft()
            self._retained_frames -= oldest.sample_end - oldest.sample_start
            self._earliest_cursor = self._blocks[0].sample_start
            evicted = True

        if evicted and self._timeline is not None:
            self._timeline.trim_before(self._earliest_cursor)

    def references_from(self, cursor: int, limit: int) -> RingReferenceBatch:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if self._earliest_cursor is None or self._latest_cursor is None:
            return RingReferenceBatch(RingReadStatus.NOT_YET_AVAILABLE)
        if cursor < self._earliest_cursor:
            return RingReferenceBatch(RingReadStatus.EXPIRED)
        if cursor >= self._latest_cursor:
            return RingReferenceBatch(RingReadStatus.NOT_YET_AVAILABLE)

        references: list[CapturedAudioBlock] = []
        for block in self._blocks:
            if block.sample_end <= cursor:
                continue
            references.append(block)
            if len(references) == limit:
                break
        if not references:
            return RingReferenceBatch(RingReadStatus.NOT_YET_AVAILABLE)
        return RingReferenceBatch(
            RingReadStatus.AVAILABLE,
            tuple(references),
            references[-1].sample_end,
        )
