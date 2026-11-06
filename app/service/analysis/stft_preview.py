"""Bounded peak aggregation for the optional live spectrogram preview."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np

from app.service.analysis.frames import StftFrame


@dataclass(frozen=True)
class CoarseStftFrame:
    """StftFrame-compatible preview with authoritative observed time support.

    Frequency coordinates are band midpoints; magnitudes are band/time peaks.
    This ephemeral payload is never used for final analysis or saved PCM.
    """

    time: float
    frequencies: np.ndarray
    magnitudes: np.ndarray
    time_bounds: tuple[float, float]
    source_frame_count: int

    def __post_init__(self):
        # Immutable byte backing also prevents callers re-enabling array writes.
        for name in ("frequencies", "magnitudes"):
            array = np.asarray(getattr(self, name), dtype=np.float64)
            object.__setattr__(self, name, np.frombuffer(array.tobytes(), dtype=np.float64))


class StftPreviewReducer:
    """Retain two <=256-element vectors and scalar metadata for one group."""

    def __init__(self, *, interval_sec: float):
        if not np.isfinite(interval_sec) or interval_sec <= 0:
            raise ValueError("preview interval must be finite and positive")
        self.interval_sec = float(interval_sec)
        self._frequencies: np.ndarray | None = None
        self._peaks: np.ndarray | None = None
        self._axis_digest: bytes | None = None
        self._start = self._end = self._hop = 0.0
        self._count = 0

    @property
    def pending(self) -> bool:
        return self._count > 0

    def push(self, frame: StftFrame, *, hop_sec: float) -> list[CoarseStftFrame]:
        """Accept one column; split before gaps, changed axes or changed hops."""
        frequencies = np.asarray(frame.frequencies, dtype=np.float64)
        magnitudes = np.asarray(frame.magnitudes, dtype=np.float64)
        center = float(frame.time)
        if (frequencies.ndim != 1 or not frequencies.size
                or magnitudes.shape != frequencies.shape
                or not np.all(np.isfinite(frequencies))
                or not np.all(np.diff(frequencies) > 0)
                or not np.all(np.isfinite(magnitudes))
                or not np.isfinite(center)
                or not np.isfinite(hop_sec) or hop_sec <= 0):
            raise ValueError("invalid STFT preview column or hop")

        start, end = center - hop_sec / 2, center + hop_sec / 2
        digest = hashlib.sha256(frequencies.tobytes()).digest()
        output = []
        tolerance = max(1e-12, hop_sec * 1e-7)
        if self.pending and (digest != self._axis_digest
                             or hop_sec != self._hop
                             or abs(start - self._end) > tolerance):
            output.extend(self.flush())

        edges = np.linspace(0, frequencies.size, min(256, frequencies.size) + 1, dtype=int)
        peaks = np.maximum.reduceat(magnitudes, edges[:-1])
        if not self.pending:
            self._frequencies = (frequencies[edges[:-1]] + frequencies[edges[1:] - 1]) / 2
            self._peaks = peaks
            self._axis_digest = digest
            self._start = start
            self._hop = hop_sec
        else:
            np.maximum(self._peaks, peaks, out=self._peaks)
        self._end = end
        self._count += 1
        if self._end - self._start >= self.interval_sec - tolerance:
            output.extend(self.flush())
        return output

    def flush(self) -> list[CoarseStftFrame]:
        if not self.pending:
            return []
        frame = CoarseStftFrame(
            time=(self._start + self._end) / 2,
            frequencies=self._frequencies,
            magnitudes=self._peaks,
            time_bounds=(self._start, self._end),
            source_frame_count=self._count,
        )
        self._count = 0
        self._frequencies = self._peaks = None
        self._axis_digest = None
        return [frame]
