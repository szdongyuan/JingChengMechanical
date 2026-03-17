# Analysis Kernel — Plan A: DSP Foundation

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure signal-processing foundation: data frames, DSP functions, real-time analysis pipeline, and judgment engine — all fully unit-testable with no hardware dependencies.

**Architecture:** Bottom-up: dataclasses (`frames.py`) → pure DSP functions (`dsp.py`) → per-channel streaming pipeline (`pipeline.py`) → post-recording limit-check + AI judgment engine (`judgment.py`). Each layer depends only on the one below it.

**Tech Stack:** numpy, scipy (signal processing), pytest (testing)

**Spec:** `docs/superpowers/specs/2026-03-17-analysis-kernel-design.md`

---

## File Map

| File | Responsibility |
|------|---------------|
| Create: `app/service/analysis/__init__.py` | Package marker |
| Create: `app/service/analysis/frames.py` | Dataclasses: `StftFrame`, `SplFrame`, `OctaveFrame`, `FftFrame`, `BandResult`, `ChannelJudgment`, `JudgmentResult` |
| Create: `app/service/analysis/dsp.py` | Pure stateless DSP: STFT, A/C-weighting, SPL, octave bands, FFT spectrum, mic calibration |
| Create: `app/service/analysis/pipeline.py` | `AnalysisPipeline`: per-channel streaming analysis over audio chunks |
| Create: `app/service/analysis/judgment.py` | `JudgmentEngine`: post-recording limit checks + AI binding validation → pass/fail (AI execution deferred to TestSession) |
| Create: `tests/test_frames.py` | Tests for frame dataclasses |
| Create: `tests/test_dsp.py` | Tests for all DSP functions |
| Create: `tests/test_pipeline.py` | Tests for AnalysisPipeline chunk processing |
| Create: `tests/test_judgment.py` | Tests for JudgmentEngine limit checks and verdict logic |

---

### Task 1: Package init + Frame dataclasses

**Files:**
- Create: `app/service/analysis/__init__.py`
- Create: `app/service/analysis/frames.py`
- Test: `tests/test_frames.py`

- [ ] **Step 1: Create package and write frame dataclasses**

Create `app/service/analysis/__init__.py` (empty file).

Create `app/service/analysis/frames.py`:

```python
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
```

- [ ] **Step 2: Write tests for frame dataclasses**

Create `tests/test_frames.py`:

```python
"""Tests for analysis frame dataclasses."""
from __future__ import annotations

import numpy as np

from app.service.analysis.frames import (
    BandResult,
    ChannelJudgment,
    FftFrame,
    JudgmentResult,
    OctaveFrame,
    SplFrame,
    StftFrame,
)


class TestStftFrame:
    def test_create(self):
        f = StftFrame(time=0.5, frequencies=np.array([100.0, 200.0]),
                      magnitudes=np.array([60.0, 55.0]))
        assert f.time == 0.5
        assert f.frequencies.shape == (2,)
        assert f.magnitudes.shape == (2,)


class TestSplFrame:
    def test_create(self):
        f = SplFrame(time=1.0, spl_db=72.5, weighting="A")
        assert f.spl_db == 72.5
        assert f.weighting == "A"


class TestOctaveFrame:
    def test_create(self):
        f = OctaveFrame(time=0.1, center_freqs=np.array([1000.0]),
                        levels=np.array([65.0]), weighting="C")
        assert f.weighting == "C"


class TestFftFrame:
    def test_create(self):
        f = FftFrame(time=0.2, frequencies=np.array([50.0]),
                     magnitudes=np.array([70.0]))
        assert f.time == 0.2


class TestBandResult:
    def test_pass(self):
        b = BandResult(freq=1000.0, level=60.0, lower=50.0, upper=70.0, passed=True)
        assert b.passed is True

    def test_fail(self):
        b = BandResult(freq=1000.0, level=80.0, lower=50.0, upper=70.0, passed=False)
        assert b.passed is False


class TestJudgmentResult:
    def test_pass_verdict(self):
        ch = ChannelJudgment(channel_index=0, spl_db=65.0, spl_pass=True)
        r = JudgmentResult(overall="合格", limit_pass=True, ai_pass=True,
                           ai_score=0.95, channel_results=[ch],
                           analysis_window=(2.0, 5.0))
        assert r.overall == "合格"
        assert r.limit_pass is True
        assert r.ai_pass is True

    def test_no_ai(self):
        r = JudgmentResult(overall="合格", limit_pass=True, ai_pass=None,
                           ai_score=None)
        assert r.ai_pass is None

    def test_defaults(self):
        r = JudgmentResult(overall="不合格", limit_pass=False, ai_pass=None,
                           ai_score=None)
        assert r.channel_results == []
        assert r.analysis_window == (0.0, 0.0)
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_frames.py -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add app/service/analysis/__init__.py app/service/analysis/frames.py tests/test_frames.py
git commit -m "feat(analysis): add frame and judgment result dataclasses"
```

---

### Task 2: DSP — A/C-weighting functions

**Files:**
- Create: `app/service/analysis/dsp.py`
- Create: `tests/test_dsp.py`

- [ ] **Step 1: Write failing tests for weighting functions**

Create `tests/test_dsp.py`:

```python
"""Tests for pure DSP functions."""
from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.dsp import (
    a_weighting,
    apply_mic_calibration,
    c_weighting,
    compute_fft_spectrum,
    compute_octave_bands,
    compute_spl,
    compute_stft,
    OCTAVE_CENTER_FREQS,
)


class TestAWeighting:
    def test_1khz_is_zero(self):
        """A-weighting at 1 kHz should be ~0 dB (reference point)."""
        freqs = np.array([1000.0])
        gains = a_weighting(freqs)
        assert abs(gains[0]) < 0.1  # within 0.1 dB of 0

    def test_low_freq_attenuated(self):
        """A-weighting strongly attenuates low frequencies."""
        freqs = np.array([100.0])
        gains = a_weighting(freqs)
        assert gains[0] < -15  # ~-19.1 dB at 100 Hz

    def test_high_freq_attenuated(self):
        """A-weighting attenuates very high frequencies."""
        freqs = np.array([10000.0])
        gains = a_weighting(freqs)
        assert gains[0] < 0  # negative gain above ~6 kHz

    def test_known_values(self):
        """Check against IEC 61672-1 reference values."""
        # Freq (Hz): expected A-weight (dB) ± 0.5 dB tolerance
        test_cases = [
            (31.5, -39.4),
            (63.0, -26.2),
            (125.0, -16.1),
            (250.0, -8.6),
            (500.0, -3.2),
            (1000.0, 0.0),
            (2000.0, 1.2),
            (4000.0, 1.0),
            (8000.0, -1.1),
        ]
        for freq, expected in test_cases:
            gains = a_weighting(np.array([freq]))
            assert abs(gains[0] - expected) < 0.5, (
                f"A-weight at {freq} Hz: got {gains[0]:.1f}, expected {expected}"
            )

    def test_zero_freq_returns_neg_inf(self):
        """DC component (0 Hz) should be -inf or very large negative."""
        freqs = np.array([0.0])
        gains = a_weighting(freqs)
        assert gains[0] < -100


class TestCWeighting:
    def test_1khz_is_zero(self):
        freqs = np.array([1000.0])
        gains = c_weighting(freqs)
        assert abs(gains[0]) < 0.1

    def test_flatter_than_a_at_low_freq(self):
        """C-weighting is flatter than A-weighting at low frequencies."""
        freqs = np.array([100.0])
        a_gain = a_weighting(freqs)[0]
        c_gain = c_weighting(freqs)[0]
        assert c_gain > a_gain  # C is less attenuated

    def test_known_values(self):
        test_cases = [
            (31.5, -3.0),
            (63.0, -0.8),
            (125.0, -0.2),
            (250.0, 0.0),
            (500.0, 0.0),
            (1000.0, 0.0),
            (4000.0, -0.8),
            (8000.0, -3.0),
        ]
        for freq, expected in test_cases:
            gains = c_weighting(np.array([freq]))
            assert abs(gains[0] - expected) < 0.5, (
                f"C-weight at {freq} Hz: got {gains[0]:.1f}, expected {expected}"
            )
```

- [ ] **Step 2: Run tests — expect FAIL (module not found)**

Run: `python -m pytest tests/test_dsp.py::TestAWeighting -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement weighting functions**

Create `app/service/analysis/dsp.py`:

```python
"""Pure stateless DSP functions for the analysis kernel.

All functions operate on numpy arrays and have no side effects.
"""
from __future__ import annotations

import numpy as np
from scipy import signal as scipy_signal


# ---------------------------------------------------------------------------
# Frequency weighting (IEC 61672-1)
# ---------------------------------------------------------------------------

def a_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Return A-weighting gains (dB) for the given frequencies.

    Uses the IEC 61672-1 formula.  DC (0 Hz) returns a large negative value.
    """
    f = np.asarray(frequencies, dtype=np.float64)
    f2 = f ** 2
    # Avoid division by zero at f=0
    with np.errstate(divide="ignore", invalid="ignore"):
        num = 12194.0 ** 2 * f2 ** 2
        den = ((f2 + 20.6 ** 2)
               * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
               * (f2 + 12194.0 ** 2))
        ra = num / den
    # Normalise to 0 dB at 1 kHz
    ra_1k = _a_weight_linear(1000.0)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(ra, 1e-30)) - 20.0 * np.log10(ra_1k)
    return np.where(np.isfinite(db), db, -np.inf)


def _a_weight_linear(f: float) -> float:
    f2 = f ** 2
    num = 12194.0 ** 2 * f2 ** 2
    den = ((f2 + 20.6 ** 2)
           * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
           * (f2 + 12194.0 ** 2))
    return num / den


def c_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Return C-weighting gains (dB) for the given frequencies.

    Uses the IEC 61672-1 formula.
    """
    f = np.asarray(frequencies, dtype=np.float64)
    f2 = f ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        num = 12194.0 ** 2 * f2
        den = (f2 + 20.6 ** 2) * (f2 + 12194.0 ** 2)
        rc = num / den
    rc_1k = _c_weight_linear(1000.0)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(rc, 1e-30)) - 20.0 * np.log10(rc_1k)
    return np.where(np.isfinite(db), db, -np.inf)


def _c_weight_linear(f: float) -> float:
    f2 = f ** 2
    num = 12194.0 ** 2 * f2
    den = (f2 + 20.6 ** 2) * (f2 + 12194.0 ** 2)
    return num / den
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_dsp.py::TestAWeighting tests/test_dsp.py::TestCWeighting -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py
git commit -m "feat(analysis): add A/C frequency weighting (IEC 61672-1)"
```

---

### Task 3: DSP — SPL computation

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`

- [ ] **Step 1: Write failing tests for SPL**

Append to `tests/test_dsp.py`:

```python
class TestComputeSpl:
    def test_sine_wave_z_weight(self):
        """A full-scale sine wave should give ~91 dB SPL (Z-weighted, ref 20 µPa)."""
        sr = 44100.0
        t = np.arange(int(sr * 1.0)) / sr  # 1 second
        # amplitude = 1.0 Pa peak → RMS = 1/√2 Pa → SPL = 20*log10(RMS/20e-6) ≈ 90.97 dB
        sig = np.sin(2 * np.pi * 1000 * t)  # 1 Pa peak sine at 1 kHz
        spl = compute_spl(sig, sr, weighting="Z", time_constant=0.125)
        # With Z-weighting (flat), 1 kHz sine at 1 Pa peak → ~91 dB
        # But signal is in arbitrary units (sounddevice gives float32 -1..1),
        # so the actual dB depends on the reference. We use 20 µPa.
        # Since our signal peaks at 1.0 (not 1 Pa), the SPL will be based on
        # the RMS of the raw signal.
        assert isinstance(spl, float)
        assert np.isfinite(spl)

    def test_z_weighting_is_flat(self):
        """Z-weighted SPL should equal unweighted RMS-based SPL."""
        sr = 44100.0
        t = np.arange(int(sr * 0.5)) / sr
        sig = 0.5 * np.sin(2 * np.pi * 1000 * t)
        spl_z = compute_spl(sig, sr, weighting="Z", time_constant=0.125)
        # Manual RMS: 0.5/√2
        rms = 0.5 / np.sqrt(2)
        expected = 20 * np.log10(rms / 20e-6)
        assert abs(spl_z - expected) < 1.0  # within 1 dB (time constant smoothing)

    def test_a_weighting_attenuates_low_freq(self):
        """A-weighted SPL of a low-freq tone should be less than Z-weighted."""
        sr = 44100.0
        t = np.arange(int(sr * 1.0)) / sr
        sig = 0.5 * np.sin(2 * np.pi * 100 * t)  # 100 Hz
        spl_z = compute_spl(sig, sr, weighting="Z", time_constant=0.125)
        spl_a = compute_spl(sig, sr, weighting="A", time_constant=0.125)
        assert spl_a < spl_z - 10  # A-weight attenuates 100 Hz by ~19 dB

    def test_silence_returns_very_low(self):
        """Silent signal should return a very low SPL."""
        sig = np.zeros(44100)
        spl = compute_spl(sig, 44100.0, weighting="Z", time_constant=0.125)
        assert spl < -50  # essentially silence
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python -m pytest tests/test_dsp.py::TestComputeSpl -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement compute_spl**

Append to `app/service/analysis/dsp.py`:

```python
# ---------------------------------------------------------------------------
# SPL computation
# ---------------------------------------------------------------------------

_REF_PRESSURE = 20e-6  # 20 µPa reference for SPL

def compute_spl(signal: np.ndarray, sample_rate: float,
                weighting: str, time_constant: float) -> float:
    """Compute weighted Sound Pressure Level in dB.

    Parameters
    ----------
    signal : 1-D array of audio samples (float, arbitrary units treated as Pa)
    sample_rate : Hz
    weighting : "A", "C", or "Z" (flat)
    time_constant : exponential averaging time constant in seconds
        (0.125 = Fast, 1.0 = Slow)

    Returns
    -------
    SPL in dB re 20 µPa
    """
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size == 0:
        return -np.inf

    # Apply weighting in frequency domain
    if weighting in ("A", "C"):
        n = sig.size
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        spectrum = np.fft.rfft(sig)
        if weighting == "A":
            gains_db = a_weighting(freqs)
        else:
            gains_db = c_weighting(freqs)
        gains_linear = 10.0 ** (gains_db / 20.0)
        spectrum *= gains_linear
        sig = np.fft.irfft(spectrum, n=n)

    # Exponential time-weighted RMS (simplified: single-pass IIR)
    # For short signals, fall back to simple RMS
    alpha = 1.0 - np.exp(-1.0 / (sample_rate * time_constant))
    sq = sig ** 2
    # IIR low-pass on squared signal
    filtered = np.empty_like(sq)
    filtered[0] = sq[0]
    for i in range(1, len(sq)):
        filtered[i] = alpha * sq[i] + (1.0 - alpha) * filtered[i - 1]
    rms = np.sqrt(filtered[-1])

    if rms <= 0:
        return -np.inf
    return 20.0 * np.log10(rms / _REF_PRESSURE)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `python -m pytest tests/test_dsp.py::TestComputeSpl -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py
git commit -m "feat(analysis): add SPL computation with A/C/Z weighting"
```

---

### Task 4: DSP — STFT computation

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`

- [ ] **Step 1: Write failing tests for STFT**

Append to `tests/test_dsp.py`:

```python
class TestComputeStft:
    def test_output_shapes(self):
        """STFT output arrays should have correct shapes."""
        sr = 44100.0
        n = int(sr * 1.0)  # 1 second
        sig = np.random.randn(n)
        nfft = 1024
        overlap = 0.5
        times, freqs, mag_db = compute_stft(sig, sr, nfft, overlap)
        assert freqs.shape[0] == nfft // 2 + 1
        assert times.ndim == 1
        assert mag_db.shape == (freqs.shape[0], times.shape[0])

    def test_frequency_axis(self):
        """Frequency axis should range from 0 to Nyquist."""
        sr = 44100.0
        sig = np.zeros(int(sr * 0.5))
        times, freqs, mag_db = compute_stft(sig, sr, 1024, 0.5)
        assert freqs[0] == pytest.approx(0.0)
        assert freqs[-1] == pytest.approx(sr / 2)

    def test_sine_peak(self):
        """A 1 kHz sine should produce a peak near 1 kHz in the STFT."""
        sr = 44100.0
        t = np.arange(int(sr * 0.5)) / sr
        sig = np.sin(2 * np.pi * 1000 * t)
        times, freqs, mag_db = compute_stft(sig, sr, 1024, 0.5)
        # Average over time frames, find peak frequency
        avg_mag = mag_db.mean(axis=1)
        peak_freq = freqs[np.argmax(avg_mag)]
        assert abs(peak_freq - 1000.0) < 50  # within one FFT bin
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest tests/test_dsp.py::TestComputeStft -v`

- [ ] **Step 3: Implement compute_stft**

Append to `app/service/analysis/dsp.py`:

```python
# ---------------------------------------------------------------------------
# STFT
# ---------------------------------------------------------------------------

def compute_stft(signal: np.ndarray, sample_rate: float, nfft: int,
                 overlap: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Short-Time Fourier Transform.

    Parameters
    ----------
    signal : 1-D array
    sample_rate : Hz
    nfft : FFT window size (e.g. 1024)
    overlap : fraction 0..1 (e.g. 0.5 = 50 %)

    Returns
    -------
    (times, frequencies, magnitudes_db)
        times : 1-D array of time stamps (seconds)
        frequencies : 1-D array of frequency bins (Hz), length nfft//2+1
        magnitudes_db : 2-D array (n_freq, n_times) in dB
    """
    sig = np.asarray(signal, dtype=np.float64)
    noverlap = int(nfft * overlap)
    freqs, times, zxx = scipy_signal.stft(
        sig, fs=sample_rate, window="hann", nperseg=nfft, noverlap=noverlap,
        return_onesided=True,
    )
    mag = np.abs(zxx)
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-30))
    return times, freqs, mag_db
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_dsp.py::TestComputeStft -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py
git commit -m "feat(analysis): add STFT computation"
```

---

### Task 5: DSP — FFT spectrum

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dsp.py`:

```python
class TestComputeFftSpectrum:
    def test_output_shapes(self):
        sig = np.random.randn(4096)
        freqs, mag_db = compute_fft_spectrum(sig, 44100.0, 4096)
        assert freqs.shape[0] == 4096 // 2 + 1
        assert mag_db.shape == freqs.shape

    def test_sine_peak(self):
        sr = 44100.0
        t = np.arange(4096) / sr
        sig = np.sin(2 * np.pi * 1000 * t)
        freqs, mag_db = compute_fft_spectrum(sig, sr, 4096)
        peak_freq = freqs[np.argmax(mag_db)]
        assert abs(peak_freq - 1000.0) < 20

    def test_frequency_range(self):
        freqs, _ = compute_fft_spectrum(np.zeros(1024), 44100.0, 1024)
        assert freqs[0] == pytest.approx(0.0)
        assert freqs[-1] == pytest.approx(44100.0 / 2)
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

Append to `app/service/analysis/dsp.py`:

```python
# ---------------------------------------------------------------------------
# FFT spectrum (single-shot)
# ---------------------------------------------------------------------------

def compute_fft_spectrum(signal: np.ndarray, sample_rate: float,
                         nfft: int) -> tuple[np.ndarray, np.ndarray]:
    """Compute single-sided FFT magnitude spectrum.

    Returns
    -------
    (frequencies, magnitudes_db)  — both 1-D, length nfft//2+1
    """
    sig = np.asarray(signal, dtype=np.float64)
    # Zero-pad or truncate to nfft
    if sig.size < nfft:
        sig = np.pad(sig, (0, nfft - sig.size))
    else:
        sig = sig[:nfft]
    window = np.hanning(nfft)
    spectrum = np.fft.rfft(sig * window)
    mag = np.abs(spectrum) * 2.0 / nfft  # single-sided scaling
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-30))
    freqs = np.fft.rfftfreq(nfft, d=1.0 / sample_rate)
    return freqs, mag_db
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_dsp.py::TestComputeFftSpectrum -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py
git commit -m "feat(analysis): add FFT spectrum computation"
```

---

### Task 6: DSP — 1/3-octave band analysis

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dsp.py`:

```python
class TestComputeOctaveBands:
    def test_output_shapes(self):
        sr = 44100.0
        sig = np.random.randn(int(sr * 0.5))
        centers, levels = compute_octave_bands(sig, sr, weighting="Z")
        # Should return only bands within Nyquist
        assert centers.ndim == 1
        assert levels.shape == centers.shape
        assert len(centers) > 0
        assert centers[-1] <= sr / 2

    def test_z_weighting_white_noise(self):
        """White noise should have roughly equal levels across bands (Z-weighted)."""
        sr = 44100.0
        rng = np.random.default_rng(42)
        sig = rng.standard_normal(int(sr * 2.0))  # 2 seconds
        centers, levels = compute_octave_bands(sig, sr, weighting="Z")
        # Levels should be within ~10 dB of each other for white noise
        # (excluding very low and very high bands near Nyquist)
        mid_levels = levels[2:-2]  # skip edge bands
        assert mid_levels.max() - mid_levels.min() < 15

    def test_a_weighting_applied(self):
        """A-weighted octave levels at low freq should be less than Z-weighted."""
        sr = 44100.0
        rng = np.random.default_rng(42)
        sig = rng.standard_normal(int(sr * 1.0))
        _, levels_z = compute_octave_bands(sig, sr, weighting="Z")
        _, levels_a = compute_octave_bands(sig, sr, weighting="A")
        # At the lowest band, A-weighting should significantly reduce the level
        assert levels_a[0] < levels_z[0] - 5

    def test_center_freqs_are_standard(self):
        """Center frequencies should follow IEC 61260 standard values."""
        # 1/3-octave: 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, ...
        assert 1000.0 in OCTAVE_CENTER_FREQS
        assert 31.5 in OCTAVE_CENTER_FREQS
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

Append to `app/service/analysis/dsp.py`:

```python
# ---------------------------------------------------------------------------
# 1/3-octave band analysis (IEC 61260)
# ---------------------------------------------------------------------------

# Standard 1/3-octave center frequencies (Hz) — IEC 61260
OCTAVE_CENTER_FREQS = np.array([
    25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
    630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300,
    8000, 10000, 12500, 16000, 20000,
], dtype=np.float64)


def compute_octave_bands(signal: np.ndarray, sample_rate: float,
                         weighting: str) -> tuple[np.ndarray, np.ndarray]:
    """Compute 1/3-octave band levels.

    Parameters
    ----------
    signal : 1-D array
    sample_rate : Hz
    weighting : "A", "C", or "Z"

    Returns
    -------
    (center_frequencies, levels_db)
        Only bands whose upper edge is below Nyquist are returned.
    """
    sig = np.asarray(signal, dtype=np.float64)
    nyquist = sample_rate / 2.0
    n = sig.size
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    spectrum = np.fft.rfft(sig)
    power = (np.abs(spectrum) / n) ** 2  # power spectral density (normalised)

    # Apply weighting
    if weighting == "A":
        w_db = a_weighting(freqs)
    elif weighting == "C":
        w_db = c_weighting(freqs)
    else:
        w_db = np.zeros_like(freqs)
    w_linear = 10.0 ** (w_db / 10.0)
    power = power * w_linear

    # 1/3-octave bandwidth factor
    factor = 2.0 ** (1.0 / 6.0)  # half-bandwidth multiplier for 1/3-octave

    # Filter bands within Nyquist
    valid_mask = OCTAVE_CENTER_FREQS * factor < nyquist
    centers = OCTAVE_CENTER_FREQS[valid_mask]
    levels = np.empty(len(centers))

    for i, fc in enumerate(centers):
        f_lo = fc / factor
        f_hi = fc * factor
        band_mask = (freqs >= f_lo) & (freqs < f_hi)
        band_power = power[band_mask].sum()
        if band_power > 0:
            levels[i] = 10.0 * np.log10(band_power)
        else:
            levels[i] = -np.inf

    return centers, levels
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_dsp.py::TestComputeOctaveBands -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py
git commit -m "feat(analysis): add 1/3-octave band analysis (IEC 61260)"
```

---

### Task 7: DSP — Mic calibration

**Files:**
- Modify: `app/service/analysis/dsp.py`
- Modify: `tests/test_dsp.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_dsp.py`:

```python
class TestApplyMicCalibration:
    def test_scales_signal(self):
        sig = np.array([1.0, 2.0, -1.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=0.5)
        np.testing.assert_array_almost_equal(result, [0.5, 1.0, -0.5])

    def test_zero_sensitivity_returns_raw(self):
        sig = np.array([1.0, 2.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=0.0)
        np.testing.assert_array_equal(result, sig)

    def test_unity_sensitivity(self):
        sig = np.array([3.0, -3.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=1.0)
        np.testing.assert_array_equal(result, sig)
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

Append to `app/service/analysis/dsp.py`:

```python
# ---------------------------------------------------------------------------
# Mic calibration
# ---------------------------------------------------------------------------

def apply_mic_calibration(signal: np.ndarray,
                          sensitivity_pa_per_v: float) -> np.ndarray:
    """Apply microphone calibration sensitivity.

    The existing mic_cal stores deviation_value as a string in Pa/V units.
    This multiplies the raw voltage signal by the sensitivity to get pressure.
    If sensitivity is 0, returns the raw signal unchanged.
    """
    if sensitivity_pa_per_v == 0.0:
        return np.asarray(signal, dtype=np.float64)
    return np.asarray(signal, dtype=np.float64) * sensitivity_pa_per_v
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_dsp.py::TestApplyMicCalibration -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/dsp.py tests/test_dsp.py
git commit -m "feat(analysis): add mic calibration function"
```

---

### Task 8: AnalysisPipeline — streaming per-channel analysis

**Files:**
- Create: `app/service/analysis/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_pipeline.py`:

```python
"""Tests for AnalysisPipeline streaming analysis."""
from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.pipeline import AnalysisPipeline
from app.service.analysis.frames import StftFrame, SplFrame, OctaveFrame, FftFrame


def _make_config(**overrides) -> dict:
    """Create a channel config dict with defaults."""
    cfg = {
        "channel_index": 0,
        "stft_nfft": 1024,
        "stft_overlap": 0.5,
        "spl_weighting": "A",
        "spl_time_constant": 0.125,
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "fft_band_limits": [],
        "oct_weighting": "A",
        "oct_band_limits": [],
    }
    cfg.update(overrides)
    return cfg


class TestAnalysisPipeline:
    def test_init(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        assert p.channel_index == 0
        assert p.sample_rate == 44100.0

    def test_process_chunk_returns_all_frames(self):
        """Processing a chunk large enough for STFT should return all frame types."""
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        # Need at least nfft samples for one STFT frame
        chunk = np.random.randn(2048).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=0.5)
        assert "stft" in result
        assert "spl" in result
        assert "octave" in result
        assert "fft" in result

    def test_spl_frame_type(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        chunk = np.random.randn(2048).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=1.0)
        assert isinstance(result["spl"], SplFrame)
        assert result["spl"].weighting == "A"

    def test_stft_frame_when_enough_samples(self):
        cfg = _make_config(stft_nfft=512, stft_overlap=0.5)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg,
                             sample_rate=44100.0)
        chunk = np.random.randn(1024).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=0.0)
        stft = result["stft"]
        assert isinstance(stft, StftFrame)
        assert stft.frequencies.shape[0] == 512 // 2 + 1

    def test_stft_none_when_too_few_samples(self):
        """If chunk is smaller than nfft and no buffer, stft should be None."""
        cfg = _make_config(stft_nfft=4096)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg,
                             sample_rate=44100.0)
        chunk = np.random.randn(100).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=0.0)
        assert result["stft"] is None

    def test_mic_calibration_applied(self):
        """Non-zero sensitivity should change the SPL value."""
        cfg = _make_config()
        p_raw = AnalysisPipeline(channel_index=0, channel_config=cfg,
                                 sample_rate=44100.0, mic_sensitivity=0.0)
        p_cal = AnalysisPipeline(channel_index=0, channel_config=cfg,
                                 sample_rate=44100.0, mic_sensitivity=2.0)
        chunk = np.random.randn(2048).astype(np.float32)
        r_raw = p_raw.process_chunk(chunk.copy(), elapsed_time=0.0)
        r_cal = p_cal.process_chunk(chunk.copy(), elapsed_time=0.0)
        # Calibrated signal is 2x amplitude → ~6 dB higher SPL
        assert r_cal["spl"].spl_db > r_raw["spl"].spl_db

    def test_reset_clears_buffer(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        chunk = np.random.randn(2048).astype(np.float32)
        p.process_chunk(chunk, elapsed_time=0.0)
        p.reset()
        # After reset, internal buffer should be empty
        assert p._stft_buffer.size == 0 or len(p._stft_buffer) == 0
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest tests/test_pipeline.py -v`

- [ ] **Step 3: Implement AnalysisPipeline**

Create `app/service/analysis/pipeline.py`:

```python
"""Per-channel real-time analysis pipeline.

Receives raw audio chunks from AudioRecorder.  For each chunk, computes:
- STFT (when enough samples have accumulated)
- SPL (every chunk)
- 1/3-octave bands (every chunk)
- FFT spectrum (every chunk)
"""
from __future__ import annotations

import numpy as np

from app.service.analysis.frames import FftFrame, OctaveFrame, SplFrame, StftFrame
from app.service.analysis import dsp


class AnalysisPipeline:
    """Streaming analysis for one channel."""

    def __init__(
        self,
        channel_index: int,
        channel_config: dict,
        sample_rate: float,
        mic_sensitivity: float = 0.0,
    ):
        self.channel_index = channel_index
        self.sample_rate = sample_rate
        self._cfg = channel_config
        self._mic_sensitivity = mic_sensitivity

        # STFT config
        self._nfft: int = channel_config["stft_nfft"]
        self._overlap: float = channel_config["stft_overlap"]

        # SPL config
        self._spl_weighting: str = channel_config["spl_weighting"]
        self._spl_tc: float = channel_config["spl_time_constant"]

        # Octave config
        self._oct_weighting: str = channel_config["oct_weighting"]

        # Internal STFT overlap buffer
        self._stft_buffer = np.array([], dtype=np.float64)

    def process_chunk(self, chunk_mono: np.ndarray,
                      elapsed_time: float) -> dict:
        """Process one audio chunk and return analysis results.

        Parameters
        ----------
        chunk_mono : 1-D float array (single channel)
        elapsed_time : seconds since recording start

        Returns
        -------
        dict with keys: "stft", "spl", "octave", "fft"
            Values are frame dataclasses or None if not enough data.
        """
        sig = np.asarray(chunk_mono, dtype=np.float64)

        # Apply mic calibration
        if self._mic_sensitivity != 0.0:
            sig = dsp.apply_mic_calibration(sig, self._mic_sensitivity)

        # SPL — always computed (even for short chunks)
        spl_db = dsp.compute_spl(sig, self.sample_rate,
                                 self._spl_weighting, self._spl_tc)
        spl_frame = SplFrame(time=elapsed_time, spl_db=spl_db,
                             weighting=self._spl_weighting)

        # FFT spectrum — use latest chunk directly
        if sig.size >= self._nfft:
            freqs, mag_db = dsp.compute_fft_spectrum(sig, self.sample_rate,
                                                     self._nfft)
            fft_frame = FftFrame(time=elapsed_time, frequencies=freqs,
                                 magnitudes=mag_db)
        else:
            fft_frame = None

        # Octave bands — need enough samples for low-freq resolution
        if sig.size >= 256:
            centers, levels = dsp.compute_octave_bands(sig, self.sample_rate,
                                                       self._oct_weighting)
            octave_frame = OctaveFrame(time=elapsed_time, center_freqs=centers,
                                       levels=levels, weighting=self._oct_weighting)
        else:
            octave_frame = None

        # STFT — accumulate in overlap buffer
        self._stft_buffer = np.concatenate([self._stft_buffer, sig])
        if self._stft_buffer.size >= self._nfft:
            # Compute STFT on accumulated buffer, then keep overlap
            times, freqs, mag_db = dsp.compute_stft(
                self._stft_buffer, self.sample_rate, self._nfft, self._overlap,
            )
            # Take the last time frame
            stft_frame = StftFrame(
                time=elapsed_time,
                frequencies=freqs,
                magnitudes=mag_db[:, -1] if mag_db.shape[1] > 0 else mag_db.ravel(),
            )
            # Keep only the overlap portion for next chunk
            keep = int(self._nfft * self._overlap)
            self._stft_buffer = self._stft_buffer[-keep:]
        else:
            stft_frame = None

        return {
            "stft": stft_frame,
            "spl": spl_frame,
            "octave": octave_frame,
            "fft": fft_frame,
        }

    def reset(self):
        """Clear internal state for reuse."""
        self._stft_buffer = np.array([], dtype=np.float64)
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_pipeline.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/service/analysis/pipeline.py tests/test_pipeline.py
git commit -m "feat(analysis): add streaming AnalysisPipeline"
```

---

### Task 9: JudgmentEngine — limit checks

**Files:**
- Create: `app/service/analysis/judgment.py`
- Create: `tests/test_judgment.py`

- [ ] **Step 1: Write failing tests for limit checks**

Create `tests/test_judgment.py`:

```python
"""Tests for JudgmentEngine limit checks and verdict logic."""
from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import MagicMock

from app.service.analysis.judgment import JudgmentEngine
from app.service.analysis.frames import JudgmentResult, ChannelJudgment, BandResult


def _make_config(channels=None, **overrides) -> dict:
    """Minimal config dict for judgment testing."""
    cfg = {
        "config_id": 1,
        "config_name": "Test",
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "selected_channels": [0],
        "channels": channels or [_make_channel_config(0)],
    }
    cfg.update(overrides)
    return cfg


def _make_channel_config(idx, **overrides) -> dict:
    cfg = {
        "channel_index": idx,
        "stft_nfft": 1024,
        "stft_overlap": 0.5,
        "spl_weighting": "Z",
        "spl_time_constant": 0.125,
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "fft_band_limits": [],
        "oct_weighting": "Z",
        "oct_band_limits": [],
    }
    cfg.update(overrides)
    return cfg


class TestLimitChecks:
    def _make_engine(self):
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None  # no AI configured
        return JudgmentEngine(mock_ai)

    def test_spl_within_limits_passes(self):
        engine = self._make_engine()
        # Generate a signal that produces SPL within [40, 85] dB
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        # ~60 dB SPL signal (Z-weighted)
        amplitude = 20e-6 * 10 ** (60 / 20)  # 0.02 Pa
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)  # single channel
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert isinstance(result, JudgmentResult)
        assert len(result.channel_results) == 1
        assert result.channel_results[0].spl_pass is True

    def test_spl_above_upper_limit_fails(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        # Very loud signal → SPL well above 85 dB
        sig = 10.0 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.channel_results[0].spl_pass is False
        assert result.limit_pass is False

    def test_spl_below_lower_limit_fails(self):
        engine = self._make_engine()
        sr = 44100.0
        # Very quiet signal → SPL below 40 dB
        buf = np.full((int(sr * 3), 1), 1e-10)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.channel_results[0].spl_pass is False

    def test_fft_band_limit_pass(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        sig = 0.01 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        ch_cfg = _make_channel_config(0, fft_band_limits=[
            {"freq": 1000.0, "lower": -100.0, "upper": 100.0},
        ])
        config = _make_config(channels=[ch_cfg])
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert len(result.channel_results[0].fft_band_results) == 1
        assert result.channel_results[0].fft_band_results[0].passed is True

    def test_fft_band_limit_fail(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        sig = 10.0 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        ch_cfg = _make_channel_config(0, fft_band_limits=[
            {"freq": 1000.0, "lower": -200.0, "upper": -100.0},  # impossibly low
        ])
        config = _make_config(channels=[ch_cfg])
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.channel_results[0].fft_band_results[0].passed is False

    def test_no_ai_configured(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None

    def test_all_pass_verdict(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        # With no AI and SPL within limits → pass
        assert result.overall == "合格"

    def test_octave_band_limit_fail(self):
        """A signal exceeding an octave band limit should produce a fail verdict."""
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        # Strong 1 kHz tone — should produce high energy in the 1 kHz band
        amplitude = 20e-6 * 10 ** (80 / 20)  # ~80 dB
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        # Set octave band limit: 1 kHz upper = 50 dB (will be exceeded)
        config = _make_config(
            channels=[_make_channel_config(0,
                oct_band_limits=[{"freq": 1000, "lower": -100, "upper": 50}],
            )],
        )
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.limit_pass is False
        assert result.overall == "不合格"
        # Verify the band result details
        ch_result = result.channel_results[0]
        assert any(not br.passed for br in ch_result.oct_band_results)

    def test_fft_band_limit_fail(self):
        """A signal exceeding an FFT band limit should produce a fail verdict."""
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (80 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        # Set FFT band limit: 1 kHz upper = 50 dB (will be exceeded)
        config = _make_config(
            channels=[_make_channel_config(0,
                fft_band_limits=[{"freq": 1000, "lower": -100, "upper": 50}],
            )],
        )
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.limit_pass is False

    def test_analysis_window_extraction(self):
        """Should analyze only the last analysis_duration seconds."""
        engine = self._make_engine()
        sr = 44100.0
        config = _make_config(record_duration=5.0, analysis_duration=3.0)
        # 5 seconds of audio, single channel
        buf = np.random.randn(int(sr * 5), 1) * 0.001
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        # Window should be (2.0, 5.0)
        assert result.analysis_window[0] == pytest.approx(2.0, abs=0.1)
        assert result.analysis_window[1] == pytest.approx(5.0, abs=0.1)

    def test_multi_channel(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        ch0 = amplitude * np.sin(2 * np.pi * 1000 * t)
        ch1 = amplitude * np.sin(2 * np.pi * 500 * t)
        buf = np.column_stack([ch0, ch1])
        config = _make_config(
            selected_channels=[0, 1],
            channels=[_make_channel_config(0), _make_channel_config(1)],
        )
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert len(result.channel_results) == 2


class TestRejudge:
    def test_custom_window(self):
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        buf = np.random.randn(int(sr * 5), 1) * 0.001
        config = _make_config()
        result = engine.rejudge(config, buf, sr, mic_cal={},
                                audio_file_path="/tmp/test.wav",
                                start_sec=1.0, end_sec=4.0)
        assert result.analysis_window == pytest.approx((1.0, 4.0), abs=0.01)

    def test_reversed_window_clamped(self):
        """start_sec > end_sec should be clamped, not crash."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        buf = np.random.randn(int(sr * 5), 1) * 0.001
        config = _make_config()
        result = engine.rejudge(config, buf, sr, mic_cal={},
                                audio_file_path="/tmp/test.wav",
                                start_sec=4.0, end_sec=1.0)
        # end_sec gets clamped to start_sec (empty window), should not crash
        assert isinstance(result, JudgmentResult)

    def test_out_of_bounds_clamped(self):
        """Window beyond buffer length should be clamped."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        buf = np.random.randn(int(sr * 2), 1) * 0.001
        config = _make_config()
        result = engine.rejudge(config, buf, sr, mic_cal={},
                                audio_file_path="/tmp/test.wav",
                                start_sec=0.0, end_sec=100.0)
        assert result.analysis_window[1] == pytest.approx(2.0, abs=0.1)


class TestMissingChannelConfig:
    def test_missing_config_uses_defaults(self):
        """Selected channel with no config should use defaults, not skip."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        # Config says channel 0 selected, but no channel configs provided
        config = _make_config(selected_channels=[0], channels=[])
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        # Should still analyze channel 0 using defaults, not skip it
        assert len(result.channel_results) == 1


class TestRunAiDeferral:
    """_run_ai() intentionally returns (None, None) in JudgmentEngine.

    AI integration requires full context (test_run_id, segments, etc.) that only
    TestSession has. JudgmentEngine delegates AI to TestSession via the deferred
    pattern — _run_ai() validates binding exists but does not call AiService.run_ai_analysis().
    """

    def test_ai_returns_none_when_no_config_id(self):
        mock_ai = MagicMock()
        engine = JudgmentEngine(mock_ai)
        config = _make_config(config_id=None)
        result = engine.judge(config, np.zeros((44100, 1)), 44100.0,
                              mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None

    def test_ai_returns_none_when_binding_disabled(self):
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = {"enabled": False}
        engine = JudgmentEngine(mock_ai)
        config = _make_config()
        result = engine.judge(config, np.zeros((44100 * 3, 1)), 44100.0,
                              mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None
        # Confirm resolve_binding was called (integration validated)
        mock_ai.resolve_binding.assert_called_once_with(config["config_id"])

    def test_ai_returns_none_when_binding_enabled(self):
        """Even with a valid binding, _run_ai defers to TestSession."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = {"enabled": True}
        engine = JudgmentEngine(mock_ai)
        config = _make_config()
        result = engine.judge(config, np.zeros((44100 * 3, 1)), 44100.0,
                              mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None
        # Confirm run_ai_analysis was NOT called (deferred to TestSession)
        mock_ai.run_ai_analysis.assert_not_called()
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest tests/test_judgment.py -v`

- [ ] **Step 3: Implement JudgmentEngine**

Create `app/service/analysis/judgment.py`:

```python
"""Post-recording judgment engine.

Extracts the analysis window, runs per-channel limit checks, validates AI binding,
and produces a pass/fail verdict. AI execution is deferred to TestSession.
"""
from __future__ import annotations

import json

import numpy as np

from app.service.analysis import dsp
from app.service.analysis.frames import (
    BandResult,
    ChannelJudgment,
    JudgmentResult,
)
from app.service.config_service import _CHANNEL_DEFAULTS


def _runtime_channel_defaults(channel_index: int) -> dict:
    """Convert _CHANNEL_DEFAULTS (DB-storage format) to runtime dict.

    _CHANNEL_DEFAULTS stores list/dict fields as JSON strings (for peewee).
    This parses them into Python objects for runtime use.
    """
    cfg = {"channel_index": channel_index}
    for k, v in _CHANNEL_DEFAULTS.items():
        if k in ("fft_band_limits", "oct_band_limits", "ai_config"):
            cfg[k] = json.loads(v) if isinstance(v, str) else v
        else:
            cfg[k] = v
    return cfg


class JudgmentEngine:
    """Compute pass/fail verdict for a completed recording."""

    def __init__(self, ai_service):
        self._ai = ai_service

    def judge(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
    ) -> JudgmentResult:
        """Run judgment on the default analysis window (last N seconds).

        Parameters
        ----------
        config : loaded config dict (from ConfigService.load_config)
        buffer : (total_samples, n_channels) recorded audio
        sample_rate : Hz
        mic_cal : mic calibration dict from DeviceTable (keys like "In1")
        audio_file_path : path to saved WAV for AI
        """
        record_dur = config.get("record_duration", 5.0)
        analysis_dur = config.get("analysis_duration", 3.0)
        total_samples = buffer.shape[0]
        total_sec = total_samples / sample_rate

        # Default window: last analysis_duration seconds
        end_sec = total_sec
        start_sec = max(0.0, end_sec - analysis_dur)
        return self._run(config, buffer, sample_rate, mic_cal,
                         audio_file_path, start_sec, end_sec)

    def rejudge(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        start_sec: float,
        end_sec: float,
    ) -> JudgmentResult:
        """Re-run judgment on a user-selected window."""
        total_sec = buffer.shape[0] / sample_rate
        start_sec = max(0.0, min(start_sec, total_sec))
        end_sec = max(start_sec, min(end_sec, total_sec))
        return self._run(config, buffer, sample_rate, mic_cal,
                         audio_file_path, start_sec, end_sec)

    def _run(
        self,
        config: dict,
        buffer: np.ndarray,
        sample_rate: float,
        mic_cal: dict,
        audio_file_path: str,
        start_sec: float,
        end_sec: float,
    ) -> JudgmentResult:
        start_idx = int(start_sec * sample_rate)
        end_idx = int(end_sec * sample_rate)
        end_idx = min(end_idx, buffer.shape[0])
        window = buffer[start_idx:end_idx]

        selected = config.get("selected_channels", [])
        channels_cfg = {ch["channel_index"]: ch for ch in config.get("channels", [])}

        channel_results: list[ChannelJudgment] = []
        all_pass = True

        for ch_idx in selected:
            ch_cfg = channels_cfg.get(ch_idx)
            if ch_cfg is None:
                # Spec: use defaults when channel config is missing
                ch_cfg = _runtime_channel_defaults(ch_idx)

            # Extract single channel
            if window.ndim == 2 and ch_idx < window.shape[1]:
                sig = window[:, ch_idx].copy()
            elif window.ndim == 1:
                sig = window.copy()
            else:
                continue

            # Apply mic calibration
            sensitivity = self._get_sensitivity(mic_cal, ch_idx)
            if sensitivity != 0.0:
                sig = dsp.apply_mic_calibration(sig, sensitivity)

            # SPL check
            spl_db = dsp.compute_spl(
                sig, sample_rate,
                ch_cfg.get("spl_weighting", "Z"),
                ch_cfg.get("spl_time_constant", 0.125),
            )
            spl_lo = ch_cfg.get("spl_lower_limit", 0.0)
            spl_hi = ch_cfg.get("spl_upper_limit", 100.0)
            spl_pass = spl_lo <= spl_db <= spl_hi

            # FFT band checks
            nfft = ch_cfg.get("stft_nfft", 1024)
            fft_bands = self._check_bands(
                sig, sample_rate, nfft,
                ch_cfg.get("fft_band_limits", []),
            )

            # Octave band checks
            oct_bands = self._check_octave_bands(
                sig, sample_rate,
                ch_cfg.get("oct_weighting", "Z"),
                ch_cfg.get("oct_band_limits", []),
            )

            ch_pass = spl_pass and all(b.passed for b in fft_bands) and all(
                b.passed for b in oct_bands
            )
            if not ch_pass:
                all_pass = False

            channel_results.append(ChannelJudgment(
                channel_index=ch_idx,
                spl_db=spl_db,
                spl_pass=spl_pass,
                fft_band_results=fft_bands,
                oct_band_results=oct_bands,
            ))

        # AI analysis
        ai_pass, ai_score = self._run_ai(config, audio_file_path)

        # Combined verdict
        limit_pass = all_pass
        if ai_pass is None:
            overall = "合格" if limit_pass else "不合格"
        else:
            overall = "合格" if (limit_pass and ai_pass) else "不合格"

        return JudgmentResult(
            overall=overall,
            limit_pass=limit_pass,
            ai_pass=ai_pass,
            ai_score=ai_score,
            channel_results=channel_results,
            analysis_window=(start_sec, end_sec),
        )

    def _check_bands(
        self,
        sig: np.ndarray,
        sample_rate: float,
        nfft: int,
        band_limits: list[dict],
    ) -> list[BandResult]:
        """Check FFT band limits."""
        if not band_limits:
            return []
        freqs, mag_db = dsp.compute_fft_spectrum(sig, sample_rate, nfft)
        results = []
        for bl in band_limits:
            target_freq = bl["freq"]
            # Find nearest frequency bin
            idx = np.argmin(np.abs(freqs - target_freq))
            level = float(mag_db[idx])
            lo = bl.get("lower", -np.inf)
            hi = bl.get("upper", np.inf)
            results.append(BandResult(
                freq=float(freqs[idx]),
                level=level,
                lower=lo,
                upper=hi,
                passed=lo <= level <= hi,
            ))
        return results

    def _check_octave_bands(
        self,
        sig: np.ndarray,
        sample_rate: float,
        weighting: str,
        band_limits: list[dict],
    ) -> list[BandResult]:
        """Check 1/3-octave band limits."""
        if not band_limits:
            return []
        centers, levels = dsp.compute_octave_bands(sig, sample_rate, weighting)
        results = []
        for bl in band_limits:
            target_freq = bl["freq"]
            idx = np.argmin(np.abs(centers - target_freq))
            level = float(levels[idx])
            lo = bl.get("lower", -np.inf)
            hi = bl.get("upper", np.inf)
            results.append(BandResult(
                freq=float(centers[idx]),
                level=level,
                lower=lo,
                upper=hi,
                passed=lo <= level <= hi,
            ))
        return results

    def _run_ai(self, config: dict, audio_file_path: str) -> tuple[bool | None, float | None]:
        """Validate AI binding and return (None, None) — actual call deferred to TestSession.

        JudgmentEngine only checks whether a binding exists/is enabled.
        The actual AiService.run_ai_analysis() call is made by TestSession,
        which owns the test_run_id, segment list, and ai_runs_dir context
        that AiService requires. TestSession patches ai_pass/ai_score into
        the JudgmentResult after calling AiService.
        """
        config_id = config.get("config_id")
        if config_id is None:
            return None, None

        binding = self._ai.resolve_binding(config_id)
        if binding is None or not binding.get("enabled"):
            return None, None

        # AI integration will be called from TestSession which has the full
        # context (test_run_id, segments, etc.). For now, return None to indicate
        # "AI not yet run" — TestSession will fill this in.
        return None, None

    @staticmethod
    def _get_sensitivity(mic_cal: dict, channel_index: int) -> float:
        """Extract mic sensitivity from mic_cal dict.

        mic_cal format: {"In1": {"deviation_value": "0.5", ...}, ...}
        """
        key = f"In{channel_index + 1}"
        entry = mic_cal.get(key, {})
        raw = entry.get("deviation_value", "0")
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m pytest tests/test_judgment.py -v`

- [ ] **Step 5: Run ALL tests to verify no regressions**

Run: `python -m pytest tests/ -v`
Expected: All tests pass (existing + new).

- [ ] **Step 6: Commit**

```bash
git add app/service/analysis/judgment.py tests/test_judgment.py
git commit -m "feat(analysis): add JudgmentEngine with limit checks and verdict"
```

---

### Task 10: Run full test suite and verify

- [ ] **Step 1: Run entire test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests pass.

- [ ] **Step 2: Verify file structure**

```
app/service/analysis/
├── __init__.py
├── frames.py
├── dsp.py
├── pipeline.py
└── judgment.py

tests/
├── test_frames.py
├── test_dsp.py
├── test_pipeline.py
└── test_judgment.py
```

- [ ] **Step 3: Final commit (if any fixes needed)**

---

## Summary

After Plan A, we have:
- **7 dataclasses** for analysis frames and judgment results
- **6 pure DSP functions** (STFT, SPL, A/C-weighting, FFT, octave bands, mic calibration)
- **AnalysisPipeline** for streaming per-channel real-time analysis
- **JudgmentEngine** for post-recording limit checks and verdict
- **~30 unit tests** covering all functionality

Plan B (Orchestration) will build on this foundation: `AudioRecorder`, `AudioMonitor`, `TestSession`, `TestManager`, EventBus wiring, and `ServiceContainer` integration.
