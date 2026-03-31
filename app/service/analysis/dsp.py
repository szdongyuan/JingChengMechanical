"""Pure stateless DSP functions for the analysis kernel.

All functions operate on numpy arrays and have no side effects.
"""
from __future__ import annotations

from functools import lru_cache
from math import log2

import numpy as np
from scipy import signal as scipy_signal


# ---------------------------------------------------------------------------
# Frequency weighting (IEC 61672-1)
# ---------------------------------------------------------------------------


def a_weighting(frequencies: np.ndarray) -> np.ndarray:
    """Return A-weighting gains (dB) for the given frequencies.

    Uses the IEC 61672-1 formula. DC (0 Hz) returns a large negative value.
    """
    f = np.asarray(frequencies, dtype=np.float64)
    f2 = f ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        num = 12194.0 ** 2 * f2 ** 2
        den = (
            (f2 + 20.6 ** 2)
            * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
            * (f2 + 12194.0 ** 2)
        )
        ra = num / den
    ra_1k = _a_weight_linear(1000.0)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(ra, 1e-30)) - 20.0 * np.log10(ra_1k)
    return np.where(np.isfinite(db), db, -np.inf)



def _a_weight_linear(f: float) -> float:
    f2 = f ** 2
    num = 12194.0 ** 2 * f2 ** 2
    den = (
        (f2 + 20.6 ** 2)
        * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
        * (f2 + 12194.0 ** 2)
    )
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


# ---------------------------------------------------------------------------
# SPL computation
# ---------------------------------------------------------------------------


_REF_PRESSURE = 20e-6
_LOUDNESS_MIN_SAMPLE_RATE = 48000.0


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

    alpha = 1.0 - np.exp(-1.0 / (sample_rate * time_constant))
    sq = sig ** 2
    filtered = np.empty_like(sq)
    filtered[0] = sq[0]
    for i in range(1, len(sq)):
        filtered[i] = alpha * sq[i] + (1.0 - alpha) * filtered[i - 1]
    rms = np.sqrt(filtered[-1])

    if rms <= 0:
        return -np.inf
    return 20.0 * np.log10(rms / _REF_PRESSURE)


@lru_cache(maxsize=1)
def _load_loudness_backend():
    try:
        from mosqito.sq_metrics import loudness_zwtv
    except ImportError as exc:
        raise RuntimeError("MoSQITo loudness backend is unavailable") from exc
    return loudness_zwtv


def _compute_loudness_zwtv(signal: np.ndarray, sample_rate: float):
    backend = _load_loudness_backend()
    return backend(signal, int(round(sample_rate)))


def compute_loudness_trace(signal: np.ndarray, sample_rate: float) -> tuple[list[float], list[float], float]:
    """Compute an ISO 532-1 loudness trace and its maximum in canonical sones."""
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size == 0:
        raise ValueError("Loudness signal is empty")
    if sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")

    backend_sample_rate = float(sample_rate)
    if backend_sample_rate < _LOUDNESS_MIN_SAMPLE_RATE:
        target_size = max(1, int(round(sig.size * _LOUDNESS_MIN_SAMPLE_RATE / backend_sample_rate)))
        sig = scipy_signal.resample(sig, target_size)
        backend_sample_rate = _LOUDNESS_MIN_SAMPLE_RATE

    values_sone, _, _, times = _compute_loudness_zwtv(sig, backend_sample_rate)
    values = np.maximum(np.asarray(values_sone, dtype=np.float64).reshape(-1), 0.0)
    time_axis = np.asarray(times, dtype=np.float64).reshape(-1)

    if values.size == 0 or time_axis.size == 0 or values.size != time_axis.size:
        raise RuntimeError("MoSQITo loudness backend returned unusable output")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(time_axis)):
        raise RuntimeError("MoSQITo loudness backend returned non-finite output")

    max_sone = float(np.max(values))
    return time_axis.tolist(), values.tolist(), max_sone


def sone_to_phon(value_sone: float) -> float:
    """Convert loudness from sones to phons using the project contract."""
    sone = max(float(value_sone), 0.0)
    if sone >= 1.0:
        return float(40.0 + 10.0 * log2(sone))
    return float(40.0 * (sone + 0.0005) ** 0.35)


def phon_to_sone(value_phon: float) -> float:
    """Convert loudness from phons to sones using the project contract."""
    phon = max(float(value_phon), 0.0)
    if phon >= 40.0:
        return float(2.0 ** ((phon - 40.0) / 10.0))

    sone = (phon / 40.0) ** (1.0 / 0.35) - 0.0005
    return float(max(sone, 0.0))


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
    actual_nperseg = min(nfft, len(sig))
    noverlap = int(actual_nperseg * overlap)
    freqs, times, zxx = scipy_signal.stft(
        sig,
        fs=sample_rate,
        window="hann",
        nperseg=actual_nperseg,
        noverlap=noverlap,
        return_onesided=True,
    )
    mag = np.abs(zxx)
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-30))
    return times, freqs, mag_db


# ---------------------------------------------------------------------------
# FFT spectrum (single-shot)
# ---------------------------------------------------------------------------


def compute_fft_spectrum(signal: np.ndarray, sample_rate: float,
                         nfft: int) -> tuple[np.ndarray, np.ndarray]:
    """Compute single-sided FFT magnitude spectrum.

    Returns
    -------
    (frequencies, magnitudes_db) -- both 1-D, length nfft//2+1
    """
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size < nfft:
        sig = np.pad(sig, (0, nfft - sig.size))
    else:
        sig = sig[:nfft]
    window = np.hanning(nfft)
    spectrum = np.fft.rfft(sig * window)
    mag = np.abs(spectrum) * 2.0 / nfft
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-30))
    freqs = np.fft.rfftfreq(nfft, d=1.0 / sample_rate)
    return freqs, mag_db


# ---------------------------------------------------------------------------
# 1/3-octave band analysis (IEC 61260)
# ---------------------------------------------------------------------------


OCTAVE_CENTER_FREQS = np.array([
    25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
    630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300,
    8000, 10000, 12500, 16000, 20000,
], dtype=np.float64)


@lru_cache(maxsize=None)
def _octave_filter_bank(sample_rate: float) -> tuple[np.ndarray, tuple[np.ndarray, ...], int]:
    """Return valid octave centers, precomputed SOS filters, and default padlen."""
    nyquist = sample_rate / 2.0
    factor = 2.0 ** (1.0 / 6.0)
    valid_mask = OCTAVE_CENTER_FREQS * factor < nyquist
    centers = OCTAVE_CENTER_FREQS[valid_mask]

    sos_filters: list[np.ndarray] = []
    default_padlen: int | None = None
    for fc in centers:
        f_lo = fc / factor
        f_hi = fc * factor
        sos = scipy_signal.butter(
            4,
            [f_lo, f_hi],
            btype="bandpass",
            fs=sample_rate,
            output="sos",
        )
        if default_padlen is None:
            n_sections = sos.shape[0]
            ntaps = 2 * n_sections + 1
            ntaps -= min((sos[:, 2] == 0).sum(), (sos[:, 5] == 0).sum())
            default_padlen = int(ntaps * 3)
        sos_filters.append(sos)

    return centers, tuple(sos_filters), default_padlen or 0


def compute_octave_bands(signal: np.ndarray, sample_rate: float,
                         weighting: str) -> tuple[np.ndarray, np.ndarray]:
    """Compute 1/3-octave band levels with an IEC-style filter bank.

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
    if sig.size == 0:
        return np.array([]), np.array([])

    centers, sos_filters, default_padlen = _octave_filter_bank(float(sample_rate))
    levels = np.empty(len(centers), dtype=np.float64)
    padlen = min(default_padlen, sig.size - 1) if sig.size > 1 else 0

    for i, (fc, sos) in enumerate(zip(centers, sos_filters, strict=False)):
        filtered = scipy_signal.sosfiltfilt(sos, sig, padlen=padlen)
        rms = np.sqrt(np.mean(filtered ** 2))
        if rms <= 0.0:
            levels[i] = -np.inf
            continue

        level_db = 20.0 * np.log10(rms)
        if weighting == "A":
            level_db += float(a_weighting(np.array([fc]))[0])
        elif weighting == "C":
            level_db += float(c_weighting(np.array([fc]))[0])
        levels[i] = level_db

    return centers, levels


# ---------------------------------------------------------------------------
# Mic calibration
# ---------------------------------------------------------------------------


def apply_mic_calibration(signal: np.ndarray,
                          sensitivity_pa_per_v: float | None) -> np.ndarray:
    """Apply microphone calibration sensitivity.

    The existing mic_cal stores deviation_value as a string in Pa/V units.
    This multiplies the raw voltage signal by the sensitivity to get pressure.
    If sensitivity is None or 0, returns the raw signal unchanged.
    """
    calibrated = np.asarray(signal, dtype=np.float64)
    if sensitivity_pa_per_v is None or sensitivity_pa_per_v == 0.0:
        return calibrated
    return calibrated * sensitivity_pa_per_v
