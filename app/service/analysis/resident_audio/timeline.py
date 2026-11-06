"""Strict monotonic-time to absolute-sample mapping for one stream generation."""
from __future__ import annotations

import bisect
import math
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import Enum
from typing import Callable

from .contracts import CaptureErrorCode, CaptureLayerError, RawCallbackBlock


class TimelineState(str, Enum):
    AWAITING_FIRST_ANCHOR = "AWAITING_FIRST_ANCHOR"
    READY = "READY"
    TIMING_RECOVERY = "TIMING_RECOVERY"
    FAILED = "FAILED"


class MappingStatus(str, Enum):
    MAPPED = "MAPPED"
    AFTER_CONFIRMED_RANGE = "AFTER_CONFIRMED_RANGE"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class TimelineMapping:
    status: MappingStatus
    sample_index: int | None = None
    mapped_sample_monotonic: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MappingStatus):
            raise TypeError("status must be a MappingStatus")
        if self.status is MappingStatus.MAPPED:
            if (
                isinstance(self.sample_index, bool)
                or not isinstance(self.sample_index, int)
                or self.sample_index < 0
            ):
                raise ValueError(
                    "mapped timeline result requires a non-negative sample index"
                )
            if (
                isinstance(self.mapped_sample_monotonic, bool)
                or not isinstance(self.mapped_sample_monotonic, (int, float))
                or not math.isfinite(float(self.mapped_sample_monotonic))
            ):
                raise ValueError(
                    "mapped timeline result requires a finite sample time"
                )
            object.__setattr__(
                self,
                "mapped_sample_monotonic",
                float(self.mapped_sample_monotonic),
            )
        elif (
            self.sample_index is not None
            or self.mapped_sample_monotonic is not None
        ):
            raise ValueError(
                "non-mapped timeline result cannot carry sample coordinates"
            )


@dataclass(frozen=True, slots=True)
class TimelineUpdate:
    state: TimelineState
    sample_start: int
    sample_end: int
    block_start_monotonic: float | None


@dataclass(frozen=True, slots=True)
class _Anchor:
    sample_index: int
    monotonic: float
    callback_frames: int


class StreamTimeline:
    """Single-writer strict timeline owned by a stream ingest worker."""

    def __init__(
        self,
        *,
        sample_rate: float,
        input_latency: float = 0.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        initial_cursor: int = 0,
    ) -> None:
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, (int, float))
            or not math.isfinite(float(sample_rate))
            or float(sample_rate) <= 0
        ):
            raise ValueError("sample_rate must be positive and finite")
        if isinstance(initial_cursor, bool) or not isinstance(initial_cursor, int):
            raise ValueError("initial_cursor must be a non-negative integer")
        if initial_cursor < 0 or initial_cursor > sys.maxsize:
            raise ValueError("initial_cursor must fit the platform integer range")
        latency = float(input_latency)
        self._sample_rate = float(sample_rate)
        self._input_latency = (
            latency if math.isfinite(latency) and latency >= 0 else 0.0
        )
        self._monotonic_clock = monotonic_clock
        # Deliberately retained only as an injected dependency boundary. Mapping
        # never reads wall time.
        self._wall_clock = wall_clock
        self._cursor = initial_cursor
        self._state = TimelineState.AWAITING_FIRST_ANCHOR
        self._anchors: list[_Anchor] = []
        self._confirmed_sample_end: int | None = None
        self._earliest_sample: int | None = None
        self._previous_sequence: int | None = None
        self._failure_code: CaptureErrorCode | None = None
        self._failure_message: str | None = None

    @property
    def state(self) -> TimelineState:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state is TimelineState.READY

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def earliest_sample(self) -> int | None:
        return self._earliest_sample

    @property
    def latest_sample(self) -> int | None:
        return self._confirmed_sample_end

    @property
    def anchor_count(self) -> int:
        return len(self._anchors)

    def ingest(
        self,
        block: RawCallbackBlock,
        *,
        input_overflow_or_gap: bool = False,
    ) -> TimelineUpdate:
        if self._state is TimelineState.FAILED:
            self._raise_latched_failure()
        if not isinstance(block, RawCallbackBlock):
            raise TypeError("block must be a RawCallbackBlock")
        if input_overflow_or_gap:
            self._fail(
                CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                "input overflow or sampling gap",
            )
        if (
            self._previous_sequence is not None
            and block.callback_sequence != self._previous_sequence + 1
        ):
            self._fail(
                CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                "callback sequence is discontinuous",
            )
        if self._cursor > sys.maxsize - block.frames:
            self._fail(
                CaptureErrorCode.INPUT_OVERFLOW_OR_GAP,
                "absolute sample cursor exceeds platform integer range",
            )

        sample_start = self._cursor
        sample_end = sample_start + block.frames
        block_start = self._validated_block_start(block)

        if block_start is None:
            if self._state is TimelineState.TIMING_RECOVERY:
                self._fail(
                    CaptureErrorCode.TIMING_UNAVAILABLE,
                    "two consecutive callbacks lack valid timing",
                )
            if self._anchors:
                self._state = TimelineState.TIMING_RECOVERY
            self._cursor = sample_end
            self._previous_sequence = block.callback_sequence
            return TimelineUpdate(
                state=self._state,
                sample_start=sample_start,
                sample_end=sample_end,
                block_start_monotonic=None,
            )

        new_anchor = _Anchor(sample_start, block_start, block.frames)
        if self._anchors:
            previous = self._anchors[-1]
            expected = previous.monotonic + (
                (new_anchor.sample_index - previous.sample_index)
                / self._sample_rate
            )
            tolerance = (
                max(previous.callback_frames, new_anchor.callback_frames)
                / self._sample_rate
            )
            if (
                new_anchor.monotonic <= previous.monotonic
                or abs(new_anchor.monotonic - expected) > tolerance
            ):
                self._fail(
                    CaptureErrorCode.TIMING_UNAVAILABLE,
                    "callback timing anchor is backwards or discontinuous",
                )

        self._anchors.append(new_anchor)
        self._cursor = sample_end
        self._confirmed_sample_end = sample_end
        if self._earliest_sample is None:
            self._earliest_sample = sample_start
        self._previous_sequence = block.callback_sequence
        self._state = TimelineState.READY
        return TimelineUpdate(
            state=self._state,
            sample_start=sample_start,
            sample_end=sample_end,
            block_start_monotonic=block_start,
        )

    def map_trigger(self, trigger_monotonic: float) -> TimelineMapping:
        if self._state is TimelineState.FAILED:
            self._raise_latched_failure()
        if not math.isfinite(trigger_monotonic):
            raise ValueError("trigger_monotonic must be finite")
        if (
            not self._anchors
            or self._earliest_sample is None
            or self._confirmed_sample_end is None
        ):
            return TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)

        earliest_time = self._time_for_sample(self._earliest_sample)
        latest_time = self._time_for_sample(self._confirmed_sample_end)
        if trigger_monotonic < earliest_time:
            return TimelineMapping(MappingStatus.EXPIRED)
        if trigger_monotonic > latest_time:
            return TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)

        anchor_times = [anchor.monotonic for anchor in self._anchors]
        next_position = bisect.bisect_right(anchor_times, trigger_monotonic)
        if next_position == 0:
            mapped = self._earliest_sample
        elif next_position < len(self._anchors):
            left = self._anchors[next_position - 1]
            right = self._anchors[next_position]
            relative_samples = _decimal_ceil(
                (
                    (Decimal(str(trigger_monotonic)) - Decimal(str(left.monotonic)))
                    * Decimal(right.sample_index - left.sample_index)
                )
                / (
                    Decimal(str(right.monotonic))
                    - Decimal(str(left.monotonic))
                )
            )
            mapped = left.sample_index + relative_samples
        else:
            left = self._anchors[-1]
            mapped = left.sample_index + _decimal_ceil(
                (
                    Decimal(str(trigger_monotonic))
                    - Decimal(str(left.monotonic))
                )
                * Decimal(str(self._sample_rate))
            )
        mapped = min(
            self._confirmed_sample_end,
            max(self._earliest_sample, mapped),
        )
        mapped_time = self._time_for_sample(mapped)
        if not math.isfinite(mapped_time):
            self._fail(
                CaptureErrorCode.TIMING_UNAVAILABLE,
                "mapped sample time is not finite",
            )
        return TimelineMapping(MappingStatus.MAPPED, mapped, mapped_time)

    def trim_before(self, sample_cursor: int) -> None:
        if isinstance(sample_cursor, bool) or not isinstance(sample_cursor, int):
            raise ValueError("sample_cursor must be a non-negative integer")
        if sample_cursor < 0:
            raise ValueError("sample_cursor must be a non-negative integer")
        if not self._anchors or self._confirmed_sample_end is None:
            return
        bounded_cursor = min(sample_cursor, self._confirmed_sample_end)
        self._earliest_sample = max(self._earliest_sample or 0, bounded_cursor)

        samples = [anchor.sample_index for anchor in self._anchors]
        boundary_position = bisect.bisect_right(samples, bounded_cursor) - 1
        retain_from = max(0, boundary_position)
        if retain_from:
            self._anchors = self._anchors[retain_from:]

    def _validated_block_start(self, block: RawCallbackBlock) -> float | None:
        adc = float(block.input_buffer_adc_time)
        current = float(block.current_time)
        if (
            not math.isfinite(adc)
            or not math.isfinite(current)
            or adc <= 0
            or current <= 0
        ):
            return None
        age = current - adc
        block_duration = block.frames / self._sample_rate
        max_adc_age = max(
            0.5,
            self._input_latency + 4.0 * block_duration,
        )
        if age < 0 or age > max_adc_age:
            return None
        block_start = block.callback_monotonic + (adc - current)
        future_tolerance = 1.0 / self._sample_rate
        if (
            not math.isfinite(block_start)
            or block_start > block.callback_monotonic + future_tolerance
        ):
            return None
        return block_start

    def _time_for_sample(self, sample_index: int) -> float:
        samples = [anchor.sample_index for anchor in self._anchors]
        next_position = bisect.bisect_right(samples, sample_index)
        if next_position == 0:
            first = self._anchors[0]
            return first.monotonic + (
                (sample_index - first.sample_index) / self._sample_rate
            )
        if next_position < len(self._anchors):
            left = self._anchors[next_position - 1]
            right = self._anchors[next_position]
            fraction = (
                (sample_index - left.sample_index)
                / (right.sample_index - left.sample_index)
            )
            return left.monotonic + fraction * (
                right.monotonic - left.monotonic
            )
        last = self._anchors[-1]
        return last.monotonic + (
            (sample_index - last.sample_index) / self._sample_rate
        )

    def _fail(self, code: CaptureErrorCode, message: str) -> None:
        self._state = TimelineState.FAILED
        self._failure_code = code
        self._failure_message = message
        raise CaptureLayerError(code, message)

    def _raise_latched_failure(self) -> None:
        raise CaptureLayerError(
            self._failure_code or CaptureErrorCode.TIMING_UNAVAILABLE,
            self._failure_message or "timeline generation has failed",
        )


def _decimal_ceil(value: Decimal) -> int:
    """Ceil user-visible float values without binary boundary overshoot."""

    return int(value.to_integral_value(rounding=ROUND_CEILING))
