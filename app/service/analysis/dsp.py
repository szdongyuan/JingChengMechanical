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
        sig,
        fs=sample_rate,
        window="hann",
        nperseg=nfft,
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
