"""Fixed-allocation timestamped block ring for one Resident generation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Any

import numpy as np

from .values import (
    MAX_SIGNED_63,
    SampleCursor,
    _canonical_sample_cursor,
    _exact_enum,
    _required_field,
    _runtime_final,
    _signed_63,
)

_MAX_CHANNELS = 64
_MAX_ALLOCATABLE_FRAMES = (1 << 31) - 1
_MAX_RING_ALLOCATION_BYTES = 256 * 1024 * 1024
_MAX_READ_ATTEMPTS = 3


def _exact_positive_int(
    value: Any, owner: str, field: str, maximum: int
) -> int:
    parsed = _signed_63(value, owner, field)
    if parsed == 0 or parsed > maximum:
        raise ValueError(
            f"{owner}.{field} must be in 1..{maximum}"
        )
    return parsed


def _exact_frames(
    value: Any, owner: str, channels: int
) -> np.ndarray:
    if type(value) is not np.ndarray:
        raise TypeError(f"{owner}.frames must be an exact numpy.ndarray")
    if value.dtype != np.dtype(np.float32):
        raise TypeError(f"{owner}.frames must have dtype float32")
    if value.ndim != 2 or value.shape[1] != channels:
        raise ValueError(
            f"{owner}.frames must have shape (frames, {channels})"
        )
    if value.shape[0] == 0:
        raise ValueError(f"{owner}.frames must be non-empty")
    if not value.flags.c_contiguous:
        raise ValueError(f"{owner}.frames must be C-contiguous")
    return value


def _validate_ring_dimensions(
    capacity_frames: Any, channels: Any
) -> tuple[int, int]:
    owner = TimestampedBlockRing.__name__
    capacity = _exact_positive_int(
        capacity_frames,
        owner,
        "capacity_frames",
        _MAX_ALLOCATABLE_FRAMES,
    )
    channel_count = _exact_positive_int(
        channels, owner, "channels", _MAX_CHANNELS
    )
    allocated_bytes = capacity * (channel_count * 9 + 32)
    if allocated_bytes > _MAX_RING_ALLOCATION_BYTES:
        raise ValueError(
            f"{owner} fixed allocation exceeds the safe byte bound"
        )
    return capacity, channel_count


class RingOperationRejectionCode(str, Enum):
    BUSY = "BUSY"
    FAILED = "FAILED"
    FAULTED = "FAULTED"


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RingAppendRejected:
    code: RingOperationRejectionCode
    committed_cursor: SampleCursor

    def __post_init__(self) -> None:
        owner = type(self).__name__
        code = _exact_enum(
            _required_field(self, "code", owner),
            RingOperationRejectionCode,
            owner,
            "code",
        )
        cursor = _canonical_sample_cursor(
            _required_field(self, "committed_cursor", owner),
            owner,
            "committed_cursor",
        )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "committed_cursor", cursor)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RingReadBusy:
    start: SampleCursor
    end: SampleCursor

    def __post_init__(self) -> None:
        owner = type(self).__name__
        start = _canonical_sample_cursor(
            _required_field(self, "start", owner), owner, "start"
        )
        end = _canonical_sample_cursor(
            _required_field(self, "end", owner), owner, "end"
        )
        if start.value >= end.value:
            raise ValueError(f"{owner} requires a non-empty range")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RingReadFaulted:
    start: SampleCursor
    end: SampleCursor

    def __post_init__(self) -> None:
        owner = type(self).__name__
        start = _canonical_sample_cursor(
            _required_field(self, "start", owner), owner, "start"
        )
        end = _canonical_sample_cursor(
            _required_field(self, "end", owner), owner, "end"
        )
        if start.value >= end.value:
            raise ValueError(f"{owner} requires a non-empty range")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RingStateFaulted:
    committed_cursor: SampleCursor

    def __post_init__(self) -> None:
        owner = type(self).__name__
        cursor = _canonical_sample_cursor(
            _required_field(self, "committed_cursor", owner),
            owner,
            "committed_cursor",
        )
        object.__setattr__(self, "committed_cursor", cursor)


@_runtime_final
@dataclass(
    frozen=True, slots=True, repr=False, weakref_slot=True, eq=False
)
class RingReadAvailable:
    __hash__ = None

    start: SampleCursor
    end: SampleCursor
    frames: np.ndarray
    timestamps_ns: np.ndarray

    def __post_init__(self) -> None:
        owner = type(self).__name__
        start = _canonical_sample_cursor(
            _required_field(self, "start", owner), owner, "start"
        )
        end = _canonical_sample_cursor(
            _required_field(self, "end", owner), owner, "end"
        )
        if start.value >= end.value:
            raise ValueError(f"{owner} requires a non-empty half-open range")
        raw_frames = _required_field(self, "frames", owner)
        if type(raw_frames) is not np.ndarray:
            raise TypeError(f"{owner}.frames must be an exact numpy.ndarray")
        if raw_frames.dtype != np.dtype(np.float32) or raw_frames.ndim != 2:
            raise TypeError(f"{owner}.frames must be a two-dimensional float32 array")
        raw_timestamps = _required_field(self, "timestamps_ns", owner)
        if (
            type(raw_timestamps) is not np.ndarray
            or raw_timestamps.dtype != np.dtype(np.int64)
            or raw_timestamps.ndim != 1
        ):
            raise TypeError(
                f"{owner}.timestamps_ns must be a one-dimensional int64 array"
            )
        expected = end.value - start.value
        if raw_frames.shape[0] != expected or raw_timestamps.shape[0] != expected:
            raise ValueError(f"{owner} payload length must match its cursor range")
        if not np.isfinite(raw_frames).all():
            raise ValueError(f"{owner}.frames must contain finite values")
        if np.any(raw_timestamps < 0):
            raise ValueError(f"{owner}.timestamps_ns cannot be negative")
        frame_bytes = np.ascontiguousarray(raw_frames).tobytes()
        timestamp_bytes = np.ascontiguousarray(raw_timestamps).tobytes()
        frames = np.frombuffer(
            frame_bytes, dtype=np.float32
        ).reshape(raw_frames.shape)
        timestamps = np.frombuffer(timestamp_bytes, dtype=np.int64)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "timestamps_ns", timestamps)

    def __repr__(self) -> str:
        return (
            "RingReadAvailable("
            f"frames={self.frames.shape[0]}, channels={self.frames.shape[1]})"
        )

    def __eq__(self, other: Any) -> bool:
        return (
            type(other) is RingReadAvailable
            and RingReadAvailable._is_constructor_issued(other)
            and self.start == other.start
            and self.end == other.end
            and np.array_equal(self.frames, other.frames)
            and np.array_equal(self.timestamps_ns, other.timestamps_ns)
        )


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RingReadEvicted:
    start: SampleCursor
    end: SampleCursor
    oldest_available: SampleCursor

    def __post_init__(self) -> None:
        owner = type(self).__name__
        start = _canonical_sample_cursor(
            _required_field(self, "start", owner), owner, "start"
        )
        end = _canonical_sample_cursor(
            _required_field(self, "end", owner), owner, "end"
        )
        oldest = _canonical_sample_cursor(
            _required_field(self, "oldest_available", owner),
            owner,
            "oldest_available",
        )
        if start.value >= end.value or start.value >= oldest.value:
            raise ValueError(f"{owner} requires an actually evicted range")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "oldest_available", oldest)


@_runtime_final
@dataclass(frozen=True, slots=True, weakref_slot=True)
class RingReadUnavailable:
    start: SampleCursor
    end: SampleCursor
    next_available: SampleCursor

    def __post_init__(self) -> None:
        owner = type(self).__name__
        start = _canonical_sample_cursor(
            _required_field(self, "start", owner), owner, "start"
        )
        end = _canonical_sample_cursor(
            _required_field(self, "end", owner), owner, "end"
        )
        next_available = _canonical_sample_cursor(
            _required_field(self, "next_available", owner),
            owner,
            "next_available",
        )
        if start.value >= end.value or end.value <= next_available.value:
            raise ValueError(
                f"{owner} requires a range beyond the next cursor"
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "next_available", next_available)


class TimestampedBlockRing:
    """A frame-bounded ring that evicts complete oldest append blocks."""

    __slots__ = (
        "_capacity_frames",
        "_channels",
        "_frames",
        "_timestamps_ns",
        "_block_starts",
        "_block_lengths",
        "_journal_frames",
        "_journal_timestamps_ns",
        "_finite_scratch",
        "_publication",
        "_writer_claim_lock",
        "_write_in_progress",
        "_faulted",
    )

    def __init__(self, capacity_frames: Any, channels: Any) -> None:
        capacity, channel_count = _validate_ring_dimensions(
            capacity_frames, channels
        )
        self._capacity_frames = capacity
        self._channels = channel_count
        self._frames = np.empty(
            (capacity, channel_count), dtype=np.float32
        )
        self._timestamps_ns = np.empty((capacity,), dtype=np.int64)
        self._block_starts = np.empty((capacity,), dtype=np.int64)
        self._block_lengths = np.empty((capacity,), dtype=np.int64)
        self._journal_frames = np.empty(
            (capacity, channel_count), dtype=np.float32
        )
        self._journal_timestamps_ns = np.empty(
            (capacity,), dtype=np.int64
        )
        self._finite_scratch = np.empty(
            (capacity, channel_count), dtype=np.bool_
        )
        self._publication = (0, (0, 0, 0, 0))
        self._writer_claim_lock = Lock()
        self._write_in_progress = False
        self._faulted = False

    def __repr__(self) -> str:
        return (
            "<TimestampedBlockRing "
            f"capacity={self._capacity_frames} channels={self._channels}>"
        )

    @property
    def capacity_frames(self) -> int:
        return self._capacity_frames

    @property
    def retained_frames(self) -> int:
        return self._stable_state()[2]

    @property
    def oldest_cursor(self) -> SampleCursor:
        oldest, _, _, _ = self._stable_state()
        return SampleCursor(oldest)

    @property
    def next_cursor(self) -> SampleCursor:
        _, next_cursor, _, _ = self._stable_state()
        return SampleCursor(next_cursor)

    def _allocation_fingerprint(self) -> tuple[int, ...]:
        arrays = (
            self._frames,
            self._timestamps_ns,
            self._block_starts,
            self._block_lengths,
            self._journal_frames,
            self._journal_timestamps_ns,
            self._finite_scratch,
        )
        return tuple(
            int(array.__array_interface__["data"][0]) for array in arrays
        ) + tuple(array.nbytes for array in arrays)

    def snapshot(
        self,
    ) -> tuple[int, int, int, int] | RingStateFaulted:
        publication = self._publication
        if self._faulted:
            return RingStateFaulted(
                SampleCursor(publication[1][3])
            )
        return self._stable_state(publication)

    def _after_buffer_write_before_publish(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _before_buffer_write(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_first_buffer_segment_write(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _before_state_publication(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_metadata_write_before_commit(self) -> None:
        """Deterministic final pre-commit hook; production does no work."""

    def _before_rollback_restore(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_first_rollback_segment(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_fault_precheck_before_writer_claim(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _on_read_observed_in_progress(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_read_buffer_copy_before_validate(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _before_read_attempt(self, attempt: int) -> None:
        """Deterministic test hook; production performs no work here."""

    def _after_read_validation_before_result(self) -> None:
        """Deterministic test hook; production performs no work here."""

    def _stable_state(
        self,
        publication: (
            tuple[int, tuple[int, int, int, int]] | None
        ) = None,
    ) -> tuple[int, int, int, int]:
        captured = (
            self._publication if publication is None else publication
        )
        _, block_count, retained, next_cursor = captured[1]
        return (
            next_cursor - retained,
            next_cursor,
            retained,
            block_count,
        )

    def append(
        self, frames: Any, timestamp_ns: Any
    ) -> SampleCursor | RingAppendRejected:
        if self._faulted:
            publication = self._publication
            return RingAppendRejected(
                RingOperationRejectionCode.FAULTED,
                SampleCursor(publication[1][3]),
            )
        self._after_fault_precheck_before_writer_claim()
        if not self._writer_claim_lock.acquire(blocking=False):
            publication = self._publication
            return RingAppendRejected(
                RingOperationRejectionCode.BUSY,
                SampleCursor(publication[1][3]),
            )
        try:
            if self._faulted:
                publication = self._publication
                return RingAppendRejected(
                    RingOperationRejectionCode.FAULTED,
                    SampleCursor(publication[1][3]),
                )
            if self._write_in_progress:
                publication = self._publication
                return RingAppendRejected(
                    RingOperationRejectionCode.BUSY,
                    SampleCursor(publication[1][3]),
                )
            if self._faulted:
                publication = self._publication
                return RingAppendRejected(
                    RingOperationRejectionCode.FAULTED,
                    SampleCursor(publication[1][3]),
                )
            self._write_in_progress = True
        finally:
            self._writer_claim_lock.release()

        try:
            frame_block = _exact_frames(
                frames, type(self).__name__, self._channels
            )
            frame_count = frame_block.shape[0]
            if frame_count > self._capacity_frames:
                raise ValueError(
                    "append block exceeds ring frame capacity"
                )
            np.isfinite(
                frame_block,
                out=self._finite_scratch[:frame_count],
            )
            if not self._finite_scratch[:frame_count].all():
                raise ValueError(
                    f"{type(self).__name__}.frames "
                    "must contain finite values"
                )
            timestamp = _signed_63(
                timestamp_ns, type(self).__name__, "timestamp_ns"
            )
            previous_publication = self._publication
            (
                block_head,
                block_count,
                retained_frames,
                next_cursor,
            ) = previous_publication[1]
            if next_cursor > MAX_SIGNED_63 - frame_count:
                raise OverflowError("sample cursor exhausted")

            new_head = block_head
            new_count = block_count
            new_retained = retained_frames
            while (
                new_retained + frame_count
                > self._capacity_frames
            ):
                removed = int(self._block_lengths[new_head])
                new_head = (
                    new_head + 1
                ) % self._capacity_frames
                new_count -= 1
                new_retained -= removed

            absolute_start = next_cursor
            physical_start = (
                absolute_start % self._capacity_frames
            )
            first_count = min(
                frame_count,
                self._capacity_frames - physical_start,
            )
            self._journal_frames[:first_count] = self._frames[
                physical_start : physical_start + first_count
            ]
            self._journal_timestamps_ns[
                :first_count
            ] = self._timestamps_ns[
                physical_start : physical_start + first_count
            ]
            remaining = frame_count - first_count
            if remaining:
                self._journal_frames[
                    first_count:frame_count
                ] = self._frames[:remaining]
                self._journal_timestamps_ns[
                    first_count:frame_count
                ] = self._timestamps_ns[:remaining]

            block_tail = (
                new_head + new_count
            ) % self._capacity_frames
            previous_block_start = int(self._block_starts[block_tail])
            previous_block_length = int(self._block_lengths[block_tail])
            committed_cursor = absolute_start + frame_count
            next_committed_state = (
                new_head,
                new_count + 1,
                new_retained + frame_count,
                committed_cursor,
            )
            committed_result = SampleCursor(committed_cursor)
            odd_publication = (
                previous_publication[0] + 1,
                previous_publication[1],
            )
            rolled_back_publication = (
                previous_publication[0] + 2,
                previous_publication[1],
            )
            committed_publication = (
                previous_publication[0] + 2,
                next_committed_state,
            )
            try:
                self._publication = odd_publication
                self._before_buffer_write()
                self._frames[
                    physical_start : physical_start + first_count
                ] = frame_block[:first_count]
                self._timestamps_ns[
                    physical_start : physical_start + first_count
                ] = timestamp
                self._after_first_buffer_segment_write()
                if remaining:
                    self._frames[:remaining] = frame_block[first_count:]
                    self._timestamps_ns[:remaining] = timestamp
                self._after_buffer_write_before_publish()
                self._before_state_publication()
                self._block_starts[block_tail] = absolute_start
                self._block_lengths[block_tail] = frame_count
                self._after_metadata_write_before_commit()
            except BaseException as publication_error:
                rollback_error: BaseException | None = None
                try:
                    self._before_rollback_restore()
                    self._frames[
                        physical_start : physical_start + first_count
                    ] = self._journal_frames[:first_count]
                    self._timestamps_ns[
                        physical_start : physical_start + first_count
                    ] = self._journal_timestamps_ns[:first_count]
                    self._after_first_rollback_segment()
                    if remaining:
                        self._frames[:remaining] = self._journal_frames[
                            first_count:frame_count
                        ]
                        self._timestamps_ns[
                            :remaining
                        ] = self._journal_timestamps_ns[
                            first_count:frame_count
                        ]
                except BaseException as caught_rollback_error:
                    rollback_error = caught_rollback_error
                try:
                    self._block_starts[block_tail] = previous_block_start
                except BaseException as caught_scalar_error:
                    if rollback_error is None:
                        rollback_error = caught_scalar_error
                try:
                    self._block_lengths[block_tail] = previous_block_length
                except BaseException as caught_scalar_error:
                    if rollback_error is None:
                        rollback_error = caught_scalar_error
                if rollback_error is not None:
                    self._faulted = True
                self._publication = rolled_back_publication
                if not isinstance(publication_error, Exception):
                    raise
                if (
                    rollback_error is not None
                    and not isinstance(rollback_error, Exception)
                ):
                    raise rollback_error
                return RingAppendRejected(
                    (
                        RingOperationRejectionCode.FAULTED
                        if rollback_error is not None
                        else RingOperationRejectionCode.FAILED
                    ),
                    SampleCursor(next_cursor),
                )
            self._publication = committed_publication
            return committed_result
        finally:
            self._write_in_progress = False

    def read(
        self, start: Any, end: Any
    ) -> (
        RingReadAvailable
        | RingReadEvicted
        | RingReadUnavailable
        | RingReadBusy
        | RingReadFaulted
    ):
        canonical_start = _canonical_sample_cursor(
            start, type(self).__name__, "start"
        )
        canonical_end = _canonical_sample_cursor(
            end, type(self).__name__, "end"
        )
        if canonical_start.value >= canonical_end.value:
            raise ValueError("read requires a non-empty half-open range")
        if self._faulted:
            return RingReadFaulted(canonical_start, canonical_end)
        for attempt in range(_MAX_READ_ATTEMPTS):
            self._before_read_attempt(attempt)
            if self._faulted:
                return RingReadFaulted(
                    canonical_start, canonical_end
                )
            publication_before = self._publication
            version_before, committed_state = publication_before
            if version_before & 1:
                self._on_read_observed_in_progress()
                continue
            _, _, retained, next_cursor = committed_state
            oldest = next_cursor - retained
            if canonical_start.value < oldest:
                if (
                    publication_before is self._publication
                    and not version_before & 1
                ):
                    self._after_read_validation_before_result()
                    if self._faulted:
                        return RingReadFaulted(
                            canonical_start, canonical_end
                        )
                    result = RingReadEvicted(
                        canonical_start,
                        canonical_end,
                        SampleCursor(oldest),
                    )
                    if self._faulted:
                        return RingReadFaulted(
                            canonical_start, canonical_end
                        )
                    return result
                continue
            if canonical_end.value > next_cursor:
                if (
                    publication_before is self._publication
                    and not version_before & 1
                ):
                    self._after_read_validation_before_result()
                    if self._faulted:
                        return RingReadFaulted(
                            canonical_start, canonical_end
                        )
                    result = RingReadUnavailable(
                        canonical_start,
                        canonical_end,
                        SampleCursor(next_cursor),
                    )
                    if self._faulted:
                        return RingReadFaulted(
                            canonical_start, canonical_end
                        )
                    return result
                continue

            frame_count = (
                canonical_end.value - canonical_start.value
            )
            physical_start = (
                canonical_start.value % self._capacity_frames
            )
            first_count = min(
                frame_count,
                self._capacity_frames - physical_start,
            )
            frames = np.empty(
                (frame_count, self._channels), dtype=np.float32
            )
            timestamps = np.empty((frame_count,), dtype=np.int64)
            frames[:first_count] = self._frames[
                physical_start : physical_start + first_count
            ]
            timestamps[:first_count] = self._timestamps_ns[
                physical_start : physical_start + first_count
            ]
            remaining = frame_count - first_count
            if remaining:
                frames[first_count:] = self._frames[:remaining]
                timestamps[first_count:] = self._timestamps_ns[:remaining]
            self._after_read_buffer_copy_before_validate()
            if (
                publication_before is self._publication
                and not version_before & 1
            ):
                self._after_read_validation_before_result()
                if self._faulted:
                    return RingReadFaulted(
                        canonical_start, canonical_end
                    )
                result = RingReadAvailable(
                    canonical_start,
                    canonical_end,
                    frames,
                    timestamps,
                )
                if self._faulted:
                    return RingReadFaulted(
                        canonical_start, canonical_end
                    )
                return result
        if self._faulted:
            return RingReadFaulted(canonical_start, canonical_end)
        result = RingReadBusy(canonical_start, canonical_end)
        if self._faulted:
            return RingReadFaulted(canonical_start, canonical_end)
        return result
