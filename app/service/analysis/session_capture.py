"""Pure sample-window contracts for one real-time test session.

The shared recorder deals only in absolute sample positions.  A
``SessionCaptureWindow`` (defined below) is responsible for projecting one
session's retained range onto a zero-based timeline.  This module deliberately
has no Qt or DSP dependencies so its state transitions can be tested directly.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np


SESSION_WARMUP_SECONDS = 0.05


def frames_for_duration(sample_rate: float, duration_sec: float) -> int:
    """Return the smallest whole-frame count that covers ``duration_sec``."""

    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be positive and finite")
    if not math.isfinite(duration_sec) or duration_sec < 0:
        raise ValueError("duration_sec must be non-negative and finite")
    return int(math.ceil(sample_rate * duration_sec))


class CaptureNotAcceptedError(RuntimeError):
    """Raised when a recorder has atomically stopped accepting capture owners."""


@dataclass(frozen=True)
class CapturedAudioBlock:
    """One recorder block on its absolute sample and monotonic timelines."""

    data: np.ndarray
    sample_start: int
    sample_end: int
    monotonic_start: float | None
    monotonic_end: float | None

    def __post_init__(self):
        if not isinstance(self.data, np.ndarray):
            raise TypeError("data must be a numpy array")
        if self.data.ndim < 1:
            raise ValueError("data must be at least one-dimensional")
        if self.sample_start < 0 or self.sample_end < self.sample_start:
            raise ValueError("sample span must be non-negative")
        if self.data.shape[0] != self.sample_end - self.sample_start:
            raise ValueError("data frame count must match the sample span")


@dataclass(frozen=True)
class ResolvedCaptureBounds:
    """The exact recorder boundary assigned to a capture request."""

    attach_sample: int
    attach_monotonic: float
    warmup_samples: int
    target_samples: int

    @property
    def retained_start(self) -> int:
        return self.attach_sample + self.warmup_samples

    @property
    def retained_end(self) -> int:
        return self.retained_start + self.target_samples


@dataclass(frozen=True)
class RetainedAudioBlock:
    """A retained data slice on a session-relative sample timeline."""

    data: np.ndarray
    sample_start: int
    sample_end: int


class RecorderAttachment:
    """Thread-safe capture-owner handle published by an audio recorder.

    Request metadata never changes.  The recorder publishes at most one exact
    resolution and eventually marks the owner released.  Consumers only use
    the read APIs; mutation methods are package-internal recorder operations.
    """

    def __init__(
        self,
        *,
        request_monotonic: float,
        warmup_samples: int,
        target_samples: int,
    ):
        if not math.isfinite(request_monotonic):
            raise ValueError("request_monotonic must be finite")
        if warmup_samples < 0:
            raise ValueError("warmup_samples must be non-negative")
        if target_samples < 0:
            raise ValueError("target_samples must be non-negative")

        self._request_monotonic = request_monotonic
        self._warmup_samples = warmup_samples
        self._target_samples = target_samples
        self._resolution: ResolvedCaptureBounds | None = None
        self._released = False
        self._lock = threading.RLock()

    @property
    def request_monotonic(self) -> float:
        return self._request_monotonic

    @property
    def warmup_samples(self) -> int:
        return self._warmup_samples

    @property
    def target_samples(self) -> int:
        return self._target_samples

    @property
    def resolution(self) -> ResolvedCaptureBounds | None:
        with self._lock:
            return self._resolution

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def current_epoch(self) -> float | None:
        resolved = self.resolution
        if resolved is None:
            return None
        return resolved.attach_monotonic + SESSION_WARMUP_SECONDS

    def _resolve_once(self, resolution: ResolvedCaptureBounds):
        """Atomically publish ``resolution`` or verify an idempotent repeat."""

        if not isinstance(resolution, ResolvedCaptureBounds):
            raise TypeError("resolution must be ResolvedCaptureBounds")
        if resolution.warmup_samples != self._warmup_samples:
            raise ValueError("resolution warmup_samples do not match the request")
        if resolution.target_samples != self._target_samples:
            raise ValueError("resolution target_samples do not match the request")

        with self._lock:
            if self._resolution is not None:
                if self._resolution != resolution:
                    raise ValueError("attachment is already resolved differently")
                return
            if self._released:
                raise RuntimeError("cannot resolve a released attachment")
            self._resolution = resolution

    def _mark_released(self):
        """Atomically mark recorder ownership released; repeated calls are safe."""

        with self._lock:
            self._released = True


class SessionCaptureWindow:
    """Clip one recorder stream to a session's post-warmup sample range.

    ``accept`` and ``freeze`` are linearized by the same re-entrant lock.  Each
    returned block is a view into its input block, rebased to the session's
    zero-based timeline.  The window retains counts and boundaries only; it
    never assembles a session buffer.
    """

    def __init__(
        self,
        *,
        attachment: RecorderAttachment,
        sample_rate: float,
        record_duration: float,
    ):
        if not isinstance(attachment, RecorderAttachment):
            raise TypeError("attachment must be RecorderAttachment")

        warmup_samples = frames_for_duration(sample_rate, SESSION_WARMUP_SECONDS)
        target_samples = frames_for_duration(sample_rate, record_duration)
        if attachment.warmup_samples != warmup_samples:
            raise ValueError(
                "attachment warmup_samples do not match the session sample rate"
            )
        if attachment.target_samples != target_samples:
            raise ValueError(
                "attachment target_samples do not match the session duration"
            )

        self._attachment = attachment
        self._sample_rate = sample_rate
        self._target_samples = target_samples
        self._expected_warmup_samples = warmup_samples
        self._retained_frames = 0
        self._frozen = False
        self._lock = threading.RLock()

        # Fail immediately when a handle was already resolved inconsistently.
        with self._lock:
            self._resolved_bounds_locked()

    def accept(self, block: CapturedAudioBlock) -> RetainedAudioBlock | None:
        """Accept the not-yet-emitted portion of ``block`` inside this window."""

        if not isinstance(block, CapturedAudioBlock):
            raise TypeError("block must be CapturedAudioBlock")

        with self._lock:
            if self._frozen or self._retained_frames >= self._target_samples:
                return None

            bounds = self._resolved_bounds_locked()
            if bounds is None:
                return None

            expected_absolute = bounds.retained_start + self._retained_frames

            # This covers warmup-only blocks, exact-boundary endings, and
            # deliveries already consumed through overlap/repetition.
            if block.sample_end <= expected_absolute:
                return None

            if block.sample_start > expected_absolute:
                raise ValueError(
                    "gap in retained samples: expected absolute sample "
                    f"{expected_absolute}, got {block.sample_start}"
                )

            accepted_absolute_end = min(block.sample_end, bounds.retained_end)
            if accepted_absolute_end <= expected_absolute:
                return None

            data_start = expected_absolute - block.sample_start
            data_end = accepted_absolute_end - block.sample_start
            retained_start = expected_absolute - bounds.retained_start
            retained_end = accepted_absolute_end - bounds.retained_start
            retained = RetainedAudioBlock(
                data=block.data[data_start:data_end],
                sample_start=retained_start,
                sample_end=retained_end,
            )
            self._retained_frames = retained_end
            return retained

    def freeze(self) -> int:
        """Close the window and return its stable retained-frame count."""

        with self._lock:
            self._frozen = True
            return self._retained_frames

    @property
    def retained_frames(self) -> int:
        with self._lock:
            return self._retained_frames

    @property
    def target_samples(self) -> int:
        with self._lock:
            return self._target_samples

    @property
    def complete(self) -> bool:
        with self._lock:
            return self._retained_frames >= self._target_samples

    @property
    def absolute_retained_end(self) -> int | None:
        with self._lock:
            bounds = self._resolved_bounds_locked()
            if bounds is None:
                return None
            return bounds.retained_start + self._retained_frames

    @property
    def current_epoch(self) -> float | None:
        with self._lock:
            bounds = self._resolved_bounds_locked()
            if bounds is None:
                return None
            return bounds.attach_monotonic + SESSION_WARMUP_SECONDS

    def _resolved_bounds_locked(self) -> ResolvedCaptureBounds | None:
        """Read and validate the attachment's atomically published resolution."""

        bounds = self._attachment.resolution
        if bounds is None:
            return None
        if bounds.warmup_samples != self._expected_warmup_samples:
            raise ValueError(
                "resolved warmup_samples do not match the session sample rate"
            )
        if bounds.target_samples != self._target_samples:
            raise ValueError(
                "resolved target_samples do not match the session duration"
            )
        return bounds
