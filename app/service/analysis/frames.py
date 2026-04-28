from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class StftFrame:
    """One STFT analysis frame for a single channel."""
    time: float                # timestamp in seconds from recording start
    frequencies: np.ndarray    # freq axis (Hz), shape (n_freq,)
    magnitudes: np.ndarray     # magnitude spectrum (dB), shape (n_freq,)


@dataclass
class SplFrame:
    """One SPL measurement for a single channel."""
    time: float
    spl_db: float              # weighted SPL value in dB
    weighting: str             # "A", "C", or "Z"


@dataclass
class LoudnessFrame:
    """One live loudness measurement for a single channel."""
    time: float
    loudness_sone: float


@dataclass
class OctaveFrame:
    """One 1/3-octave band analysis frame for a single channel."""
    time: float
    center_freqs: np.ndarray   # octave band center frequencies (Hz)
    levels: np.ndarray         # dB levels per band
    weighting: str             # "A", "C", or "Z"


@dataclass
class FftFrame:
    """One FFT spectrum frame for a single channel."""
    time: float
    frequencies: np.ndarray    # freq axis (Hz)
    magnitudes: np.ndarray     # magnitude spectrum (dB)


@dataclass
class BandResult:
    """Pass/fail result for a single frequency band limit check."""
    freq: float
    level: float
    lower: float
    upper: float
    passed: bool


@dataclass
class ChannelJudgment:
    """Judgment result for a single channel."""
    channel_index: int
    spl_db: float
    spl_pass: bool
    loudness_max_sone: float | None = None
    loudness_pass: bool | None = None
    loudness_available: bool = False
    loudness_error_message: str | None = None
    fft_available: bool = True
    fft_error_message: str | None = None
    octave_available: bool = True
    octave_error_message: str | None = None
    fft_band_results: list[BandResult] = field(default_factory=list)
    oct_band_results: list[BandResult] = field(default_factory=list)


@dataclass
class JudgmentResult:
    """Combined pass/fail verdict for one test run."""
    overall: str                           # "合格" / "不合格"
    limit_pass: bool                       # all limit checks passed
    ai_pass: bool | None                   # None if AI not configured
    ai_score: float | None
    channel_results: list[ChannelJudgment] = field(default_factory=list)
    analysis_window: tuple[float, float] = (0.0, 0.0)  # (start_sec, end_sec)
