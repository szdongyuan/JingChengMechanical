"""Leaf-only fixtures that never model or touch an audio backend."""

from __future__ import annotations

from threading import Event

import numpy as np

from app.service.analysis.resident_audio._leaf_kernel.ring import (
    TimestampedBlockRing,
)
from app.service.analysis.resident_audio._leaf_kernel.values import (
    SampleCursor,
)


class MaliciousLookalike:
    """A structural value whose comparison hooks must never be reached."""

    def __eq__(self, other):
        raise AssertionError("malicious equality executed")

    def __hash__(self):
        raise AssertionError("malicious hash executed")


def float32_frames(start: int, count: int, channels: int = 1) -> np.ndarray:
    values = np.arange(start, start + count * channels, dtype=np.float32)
    return values.reshape(count, channels)


class PausingWriteRing(TimestampedBlockRing):
    """Pauses after buffer overwrite but before state publication."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.pause_writes = False
        self.writer_paused = Event()
        self.allow_writer = Event()
        self.reader_saw_in_progress = Event()

    def _after_buffer_write_before_publish(self) -> None:
        if self.pause_writes:
            self.writer_paused.set()
            assert self.allow_writer.wait(5)

    def _on_read_observed_in_progress(self) -> None:
        self.reader_saw_in_progress.set()


class PausingCommitRing(TimestampedBlockRing):
    """Pauses at the final throwing boundary before commit publication."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.pause_next_commit = False
        self.fail_next_commit = False
        self.commit_paused = Event()
        self.allow_commit = Event()

    def _after_metadata_write_before_commit(self) -> None:
        if self.pause_next_commit:
            self.pause_next_commit = False
            self.commit_paused.set()
            assert self.allow_commit.wait(5)
            if self.fail_next_commit:
                self.fail_next_commit = False
                raise RuntimeError("injected pre-commit failure")


class PausingReadValidationRing(TimestampedBlockRing):
    """Pauses one reader after copying so a writer can invalidate it."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.pause_next_read = False
        self.reader_copied = Event()
        self.allow_reader_validation = Event()

    def _after_read_buffer_copy_before_validate(self) -> None:
        if self.pause_next_read:
            self.pause_next_read = False
            self.reader_copied.set()
            assert self.allow_reader_validation.wait(5)


class FailingPublicationRing(TimestampedBlockRing):
    """Raises at one selected publication stage after a committed seed."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.failure_stage = ""

    def _raise_at(self, stage: str) -> None:
        if self.failure_stage == stage:
            raise RuntimeError(f"injected {stage} failure")

    def _before_buffer_write(self) -> None:
        self._raise_at("before")

    def _after_first_buffer_segment_write(self) -> None:
        self._raise_at("during")

    def _after_buffer_write_before_publish(self) -> None:
        self._raise_at("after")

    def _before_state_publication(self) -> None:
        self._raise_at("publish")

    def _after_metadata_write_before_commit(self) -> None:
        self._raise_at("state")


class InterruptingPublicationRing(FailingPublicationRing):
    """Raises one caller-supplied BaseException at a write stage."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.interruption = None

    def _raise_at(self, stage: str) -> None:
        if self.failure_stage == stage:
            raise self.interruption


class AmbiguousAdvanceRing(TimestampedBlockRing):
    """Advances by more than the callback's exact block before interrupting."""

    def append(self, frames, timestamp_ns):
        first = super().append(frames, timestamp_ns)
        assert type(first) is SampleCursor
        second = super().append(frames, timestamp_ns)
        assert type(second) is SampleCursor
        raise KeyboardInterrupt("ambiguous committed delta")


class DoubleFailurePublicationRing(InterruptingPublicationRing):
    """Also fails at one selected restoration stage."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.rollback_failure_stage = ""
        self.rollback_failure = None

    def _before_rollback_restore(self) -> None:
        if self.rollback_failure_stage == "before":
            raise self.rollback_failure

    def _after_first_rollback_segment(self) -> None:
        if self.rollback_failure_stage == "during":
            raise self.rollback_failure


class PreclaimPausingPublicationRing(DoubleFailurePublicationRing):
    """Pauses one append after its fault precheck but before writer claim."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.pause_next_preclaim = False
        self.preclaim_paused = Event()
        self.allow_preclaim = Event()

    def _after_fault_precheck_before_writer_claim(self) -> None:
        if self.pause_next_preclaim:
            self.pause_next_preclaim = False
            self.preclaim_paused.set()
            assert self.allow_preclaim.wait(5)


class FaultQuarantineReadRing(DoubleFailurePublicationRing):
    """Pauses readers at retry and validated-result quarantine boundaries."""

    def __init__(self, capacity_frames: int, channels: int) -> None:
        super().__init__(capacity_frames, channels)
        self.pause_copy_once = False
        self.copy_paused = Event()
        self.allow_copy = Event()
        self.pause_attempt = -1
        self.attempt_paused = Event()
        self.allow_attempt = Event()
        self.pause_validated_once = False
        self.validated_paused = Event()
        self.allow_validated = Event()

    def _after_read_buffer_copy_before_validate(self) -> None:
        if self.pause_copy_once:
            self.pause_copy_once = False
            self.copy_paused.set()
            assert self.allow_copy.wait(5)

    def _before_read_attempt(self, attempt: int) -> None:
        if attempt == self.pause_attempt:
            self.pause_attempt = -1
            self.attempt_paused.set()
            assert self.allow_attempt.wait(5)

    def _after_read_validation_before_result(self) -> None:
        if self.pause_validated_once:
            self.pause_validated_once = False
            self.validated_paused.set()
            assert self.allow_validated.wait(5)
