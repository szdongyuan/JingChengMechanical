"""Preallocated single-producer/single-consumer callback ingress."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


MIN_PRODUCTION_INGRESS_BLOCKS = 40
_MAX_SEQUENCE = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class _IngressBlock:
    sequence: int
    samples: np.ndarray
    frame_count: int
    callback_monotonic_ns: int


class _SpscIngress:
    """One callback producer and one drain consumer with no wait primitive."""

    __slots__ = (
        "_accepted_sequence",
        "_callback_monotonic_ns",
        "_capacity_blocks",
        "_capture_channel_indices",
        "_frame_counts",
        "_frames_per_buffer",
        "_heartbeat_monotonic_ns",
        "_input_channel_count",
        "_overflow_count",
        "_read_sequence",
        "_storage",
        "_write_sequence",
    )

    def __init__(
        self,
        *,
        capacity_blocks: int,
        frames_per_buffer: int,
        input_channel_count: int,
        capture_channel_indices: tuple[int, ...],
    ) -> None:
        if type(capacity_blocks) is not int or capacity_blocks < 1:
            raise ValueError("capacity_blocks must be a positive integer")
        if type(frames_per_buffer) is not int or frames_per_buffer < 1:
            raise ValueError("frames_per_buffer must be a positive integer")
        if type(input_channel_count) is not int or input_channel_count < 1:
            raise ValueError("input_channel_count must be a positive integer")
        if (
            type(capture_channel_indices) is not tuple
            or not capture_channel_indices
            or any(
                type(index) is not int
                or index < 0
                or index >= input_channel_count
                for index in capture_channel_indices
            )
            or len(set(capture_channel_indices))
            != len(capture_channel_indices)
        ):
            raise ValueError("capture_channel_indices are invalid")
        self._capacity_blocks = capacity_blocks
        self._frames_per_buffer = frames_per_buffer
        self._input_channel_count = input_channel_count
        self._capture_channel_indices = capture_channel_indices
        self._storage = np.empty(
            (
                capacity_blocks,
                frames_per_buffer,
                len(capture_channel_indices),
            ),
            dtype=np.float32,
        )
        self._frame_counts = np.empty(capacity_blocks, dtype=np.int64)
        self._callback_monotonic_ns = np.empty(
            capacity_blocks,
            dtype=np.int64,
        )
        self._read_sequence = 0
        self._write_sequence = 0
        self._accepted_sequence = 0
        self._heartbeat_monotonic_ns: int | None = None
        self._overflow_count = 0

    @classmethod
    def for_stream(
        cls,
        *,
        sample_rate_hz: int,
        frames_per_buffer: int,
        input_channel_count: int,
        capture_channel_indices: tuple[int, ...],
    ) -> _SpscIngress:
        if type(sample_rate_hz) is not int or sample_rate_hz < 1:
            raise ValueError("sample_rate_hz must be a positive integer")
        if type(frames_per_buffer) is not int or frames_per_buffer < 1:
            raise ValueError("frames_per_buffer must be a positive integer")
        two_seconds = math.ceil(
            2 * sample_rate_hz / frames_per_buffer
        )
        return cls(
            capacity_blocks=max(MIN_PRODUCTION_INGRESS_BLOCKS, two_seconds),
            frames_per_buffer=frames_per_buffer,
            input_channel_count=input_channel_count,
            capture_channel_indices=capture_channel_indices,
        )

    @property
    def capacity_blocks(self) -> int:
        return self._capacity_blocks

    @property
    def accepted_sequence(self) -> int:
        return self._accepted_sequence

    @property
    def heartbeat_monotonic_ns(self) -> int | None:
        return self._heartbeat_monotonic_ns

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    @property
    def has_pending(self) -> bool:
        return self._read_sequence < self._write_sequence

    def try_push(
        self,
        samples: np.ndarray,
        frame_count: int,
        callback_monotonic_ns: int,
        heartbeat_monotonic_ns: int,
    ) -> bool:
        if self._write_sequence - self._read_sequence >= self._capacity_blocks:
            self._overflow_count = min(
                self._overflow_count + 1,
                _MAX_SEQUENCE,
            )
            return False
        slot = self._write_sequence % self._capacity_blocks
        destination = self._storage[slot]
        for destination_channel, source_channel in enumerate(
            self._capture_channel_indices
        ):
            np.copyto(
                destination[:frame_count, destination_channel],
                samples[:frame_count, source_channel],
                casting="no",
            )
        self._frame_counts[slot] = frame_count
        self._callback_monotonic_ns[slot] = callback_monotonic_ns
        next_sequence = self._write_sequence + 1
        self._write_sequence = next_sequence
        self._accepted_sequence = min(next_sequence, _MAX_SEQUENCE)
        self._heartbeat_monotonic_ns = heartbeat_monotonic_ns
        return True

    def peek(self) -> _IngressBlock | None:
        if self._read_sequence >= self._write_sequence:
            return None
        slot = self._read_sequence % self._capacity_blocks
        frame_count = int(self._frame_counts[slot])
        return _IngressBlock(
            sequence=self._read_sequence + 1,
            samples=self._storage[slot, :frame_count],
            frame_count=frame_count,
            callback_monotonic_ns=int(self._callback_monotonic_ns[slot]),
        )

    def release(self, sequence: int) -> None:
        expected = self._read_sequence + 1
        if type(sequence) is not int or sequence != expected:
            raise RuntimeError("ingress consumer sequence is out of order")
        if self._read_sequence >= self._write_sequence:
            raise RuntimeError("ingress consumer has no block to release")
        self._read_sequence += 1


__all__: list[str] = []
