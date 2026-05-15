"""DSP functions for the analysis kernel.

Most functions operate on numpy arrays and have no side effects.
"""
from __future__ import annotations

from functools import lru_cache
from math import log2
from numbers import Integral

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
LOUDNESS_BACKEND_SAMPLE_RATE = 48000.0
LOUDNESS_WINDOW_SAMPLES = 8192
_LOUDNESS_MIN_SAMPLE_RATE = LOUDNESS_BACKEND_SAMPLE_RATE
_LOUDNESS_WINDOW_SAMPLES = LOUDNESS_WINDOW_SAMPLES
_LOUDNESS_HOP_SEC = 0.25


def loudness_window_duration_sec() -> float:
    return LOUDNESS_WINDOW_SAMPLES / LOUDNESS_BACKEND_SAMPLE_RATE


class SplDetector:
    """Stateful exponential pressure-squared SPL detector."""

    def __init__(self, sample_rate: float, weighting: str, time_constant: float):
        self.sample_rate = float(sample_rate)
        if self.sample_rate <= 0.0:
            raise ValueError("sample_rate must be positive")
        self.weighting = str(weighting or "Z")
        self.time_constant = float(time_constant)
        if self.time_constant <= 0.0:
            raise ValueError("time_constant must be positive")
        self._alpha = 1.0 - np.exp(-1.0 / (self.sample_rate * self.time_constant))
        self._mean_square_state: float | None = None

    def process(self, signal: np.ndarray) -> float:
        sig = np.asarray(signal, dtype=np.float64).reshape(-1)
        if sig.size == 0:
            return -np.inf

        weighted = _apply_spl_frequency_weighting(
            sig,
            self.sample_rate,
            self.weighting,
        ).reshape(-1)
        squared = weighted ** 2
        if self._mean_square_state is None:
            self._mean_square_state = float(np.mean(squared))

        state = float(self._mean_square_state)
        alpha = self._alpha
        for sample_square in squared:
            state = float(alpha * sample_square + (1.0 - alpha) * state)
        self._mean_square_state = state

        if state <= 0.0:
            return -np.inf
        return float(10.0 * np.log10(state / (_REF_PRESSURE ** 2)))

    def reset(self) -> None:
        self._mean_square_state = None


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
    detector = SplDetector(sample_rate, weighting, time_constant)
    return detector.process(signal)


def compute_spl_leq(signal: np.ndarray, sample_rate: float, weighting: str) -> float:
    """Compute equivalent continuous SPL (Leq) over a full signal."""
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size == 0:
        return -np.inf
    sample_rate = float(sample_rate)
    if sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")
    sig = _apply_spl_frequency_weighting(sig, sample_rate, weighting)
    mean_square = float(np.mean(sig ** 2))
    if mean_square <= 0.0:
        return -np.inf
    return float(10.0 * np.log10(mean_square / (_REF_PRESSURE ** 2)))


def compute_spl_trace(
    signal: np.ndarray,
    sample_rate: float,
    weighting: str,
    time_constant: float,
    *,
    chunk_duration: float = 0.05,
) -> tuple[list[float], list[float]]:
    """Compute chunked weighted SPL values across a signal."""
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size == 0:
        return [], []
    sample_rate = float(sample_rate)
    if sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")
    chunk_duration = float(chunk_duration)
    if chunk_duration <= 0.0:
        raise ValueError("chunk_duration must be positive")

    chunk_samples = max(1, int(round(sample_rate * chunk_duration)))
    detector = SplDetector(sample_rate, weighting, time_constant)
    times_sec: list[float] = []
    values_db: list[float] = []

    if sig.size < chunk_samples:
        times_sec.append(float(sig.size / sample_rate))
        values_db.append(float(detector.process(sig)))
        return times_sec, values_db

    for start in range(0, int(sig.size) - chunk_samples + 1, chunk_samples):
        chunk = sig[start : start + chunk_samples]
        times_sec.append(float((start + chunk.size) / sample_rate))
        values_db.append(float(detector.process(chunk)))

    return times_sec, values_db


def _apply_spl_frequency_weighting(
    signal: np.ndarray,
    sample_rate: float,
    weighting: str,
) -> np.ndarray:
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size == 0 or weighting not in ("A", "C"):
        return sig

    n = sig.size
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    spectrum = np.fft.rfft(sig)
    if weighting == "A":
        gains_db = a_weighting(freqs)
    else:
        gains_db = c_weighting(freqs)
    gains_linear = 10.0 ** (gains_db / 20.0)
    spectrum *= gains_linear
    return np.fft.irfft(spectrum, n=n)


@lru_cache(maxsize=1)
def _load_loudness_backend():
    try:
        from mosqito.sq_metrics import loudness_zwtv
    except ImportError as exc:
        raise RuntimeError("MoSQITo loudness backend is unavailable") from exc
    return loudness_zwtv


def _compute_loudness_zwtv(signal: np.ndarray, sample_rate: float):
    backend = _load_loudness_backend()
    return backend(signal, int(round(sample_rate)), field_type="free")


class _UnsupportedOptimizedLoudnessInput(Exception):
    """Signal that the cached frequency path should fall back to MoSQITo."""


@lru_cache(maxsize=1)
def _load_loudness_frequency_backend():
    try:
        from mosqito.sq_metrics import loudness_zwst_freq
    except ImportError as exc:
        raise RuntimeError("MoSQITo loudness backend is unavailable") from exc
    return _make_cached_loudness_frequency_backend(loudness_zwst_freq)


def _make_cached_loudness_frequency_backend(public_backend):
    def backend(spectrum, freqs, *, field_type="free"):
        try:
            return _compute_loudness_zwst_freq_cached(spectrum, freqs, field_type=field_type)
        except _UnsupportedOptimizedLoudnessInput:
            return public_backend(spectrum, freqs, field_type=field_type)

    return backend


@lru_cache(maxsize=1)
def _load_loudness_frequency_components():
    try:
        from mosqito.sq_metrics.loudness.loudness_zwst._calc_slopes import _calc_slopes
        from mosqito.sq_metrics.loudness.loudness_zwst._main_loudness import _main_loudness
        from mosqito.utils import amp2db
    except ImportError as exc:
        raise _UnsupportedOptimizedLoudnessInput from exc
    return _main_loudness, _calc_slopes, amp2db


def _compute_loudness_zwst_freq_cached(spectrum, freqs, *, field_type="free"):
    spectrum_array = np.asarray(spectrum)
    freqs_array = np.asarray(freqs, dtype=np.float64)
    one_dimensional_input = spectrum_array.ndim == 1
    if one_dimensional_input:
        spectrum_matrix = spectrum_array.reshape(-1, 1)
    elif spectrum_array.ndim == 2:
        spectrum_matrix = spectrum_array
    else:
        raise _UnsupportedOptimizedLoudnessInput

    if freqs_array.ndim != 1 or freqs_array.size < 2:
        raise _UnsupportedOptimizedLoudnessInput
    if spectrum_matrix.shape[0] != freqs_array.size:
        raise _UnsupportedOptimizedLoudnessInput
    if not np.all(np.isfinite(freqs_array)):
        raise _UnsupportedOptimizedLoudnessInput
    if freqs_array.max() < 24000.0 or freqs_array.min() > 24.0:
        raise _UnsupportedOptimizedLoudnessInput

    sample_rate = float(freqs_array.max() * 2.0)
    if round(sample_rate) != int(_LOUDNESS_MIN_SAMPLE_RATE):
        raise _UnsupportedOptimizedLoudnessInput

    weights = _loudness_third_octave_synthesis_weights(
        int(spectrum_matrix.shape[0]),
        int(round(sample_rate)),
    )
    spec_third = np.sqrt(weights @ (np.abs(spectrum_matrix) ** 2))
    _main_loudness, _calc_slopes, amp2db = _load_loudness_frequency_components()
    spec_third_db = amp2db(spec_third.copy(), ref=2e-5)
    main_loudness = _main_loudness(spec_third_db, field_type)
    loudness, specific_loudness = _calc_slopes(main_loudness)
    bark_axis = np.linspace(0.1, 24, int(24 / 0.1))
    return loudness, specific_loudness, bark_axis


@lru_cache(maxsize=8)
def _loudness_third_octave_synthesis_weights(
    freq_count: int,
    sample_rate: int,
) -> np.ndarray:
    if int(sample_rate) != int(_LOUDNESS_MIN_SAMPLE_RATE) or int(freq_count) <= 0:
        raise _UnsupportedOptimizedLoudnessInput
    try:
        from mosqito.sound_level_meter.noct_spectrum._center_freq import _center_freq
        from mosqito.sound_level_meter.noct_spectrum._filter_bandwidth import _filter_bandwidth
    except ImportError as exc:
        raise _UnsupportedOptimizedLoudnessInput from exc

    fc_vec, _fpref = _center_freq(fmin=24, fmax=12600, n=3, G=10, fr=1000)
    alpha_vec, _f_low, f_high = _filter_bandwidth(fc_vec, n=3)
    invalid_high = np.asarray(np.where(f_high > sample_rate / 2.0))
    if any(invalid_high[0]):
        fc_vec = np.delete(fc_vec, invalid_high)

    weights = []
    for fc, alpha in zip(fc_vec, alpha_vec, strict=False):
        w1 = fc / (sample_rate / 2.0) / alpha
        w2 = fc / (sample_rate / 2.0) * alpha
        sos = scipy_signal.butter(3, [w1, w2], "bandpass", analog=False, output="sos")
        _w, response = scipy_signal.sosfreqz(sos, worN=freq_count)
        weights.append(np.abs(response) ** 2)

    return np.asarray(weights, dtype=np.float64)


def _loudness_window_samples(sample_rate: float) -> int:
    return _LOUDNESS_WINDOW_SAMPLES


def _loudness_hop_samples(sample_rate: float) -> int:
    return max(1, int(round(float(sample_rate) * _LOUDNESS_HOP_SEC)))


def _loudness_window_starts(
    signal_size: int,
    window_samples: int,
    hop_samples: int,
) -> np.ndarray:
    if signal_size < window_samples:
        return np.array([0], dtype=np.int64)
    return np.arange(0, signal_size - window_samples + 1, hop_samples, dtype=np.int64)


def _loudness_windows(
    signal: np.ndarray,
    starts: np.ndarray,
    window_samples: int,
) -> np.ndarray:
    windows = np.zeros((window_samples, int(starts.size)), dtype=np.float64)
    for column, start in enumerate(starts):
        segment = signal[int(start) : int(start) + window_samples]
        windows[: segment.size, column] = segment
    return windows


def _one_sided_rms_spectrum_matrix(
    windows: np.ndarray,
    sample_rate: float,
) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.asarray(windows, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    if matrix.ndim != 2 or matrix.shape[0] <= 0 or matrix.shape[1] <= 0:
        raise RuntimeError("Loudness spectrum input is empty")

    window_size = int(matrix.shape[0])
    window = np.hanning(window_size).astype(np.float64)
    window_rms = float(np.sqrt(np.mean(window ** 2)))
    if window_rms > 0.0 and np.isfinite(window_rms):
        matrix = matrix * (window / window_rms)[:, None]

    coeff = np.fft.rfft(matrix, axis=0)
    mag = np.abs(coeff) / window_size
    rms_mag = mag.copy()
    if window_size % 2 == 0:
        rms_mag[1:-1, :] *= np.sqrt(2.0)
    else:
        rms_mag[1:, :] *= np.sqrt(2.0)

    spectrum = rms_mag[1:, :]
    freqs = np.fft.rfftfreq(window_size, d=1.0 / float(sample_rate))[1:]
    return spectrum, freqs


def _prepare_loudness_signal(
    signal: np.ndarray,
    sample_rate: float,
) -> tuple[np.ndarray, float]:
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size == 0:
        raise ValueError("Loudness signal is empty")
    try:
        backend_sample_rate = float(sample_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_rate must be positive") from exc
    if not np.isfinite(backend_sample_rate) or backend_sample_rate <= 0.0:
        raise ValueError("sample_rate must be positive")

    if backend_sample_rate != _LOUDNESS_MIN_SAMPLE_RATE:
        target_size = max(1, int(round(sig.size * _LOUDNESS_MIN_SAMPLE_RATE / backend_sample_rate)))
        sig = scipy_signal.resample(sig, target_size)
        backend_sample_rate = _LOUDNESS_MIN_SAMPLE_RATE
    return sig, backend_sample_rate


def _validate_loudness_values(
    backend_output,
    expected_count: int,
) -> np.ndarray:
    raw_values = backend_output[0] if isinstance(backend_output, tuple) else backend_output
    values = np.asarray(raw_values, dtype=np.float64).reshape(-1)
    if values.size == 0 or values.size != expected_count:
        raise RuntimeError("MoSQITo loudness backend returned unusable output")
    if not np.all(np.isfinite(values)):
        raise RuntimeError("MoSQITo loudness backend returned non-finite output")
    if np.any(values < 0.0):
        raise RuntimeError("MoSQITo loudness backend returned values that must be non-negative")
    return values


def _compute_loudness_values_from_windows(
    windows: np.ndarray,
    sample_rate: float,
) -> np.ndarray:
    spectrum, freqs = _one_sided_rms_spectrum_matrix(windows, sample_rate)
    backend = _load_loudness_frequency_backend()
    backend_output = backend(spectrum, freqs, field_type="free")
    return _validate_loudness_values(backend_output, int(spectrum.shape[1]))


def compute_loudness_trace(signal: np.ndarray, sample_rate: float) -> tuple[list[float], list[float], float]:
    """Compute a rolling frequency-domain loudness trace in canonical sones."""
    sig, backend_sample_rate = _prepare_loudness_signal(signal, sample_rate)
    window_samples = _loudness_window_samples(backend_sample_rate)
    hop_samples = _loudness_hop_samples(backend_sample_rate)
    starts = _loudness_window_starts(int(sig.size), window_samples, hop_samples)
    windows = _loudness_windows(sig, starts, window_samples)

    values = _compute_loudness_values_from_windows(windows, backend_sample_rate)
    if sig.size < window_samples:
        time_axis = np.array([sig.size / (2.0 * backend_sample_rate)], dtype=np.float64)
    else:
        time_axis = (starts.astype(np.float64) + window_samples / 2.0) / backend_sample_rate

    if not np.all(np.isfinite(time_axis)):
        raise RuntimeError("MoSQITo loudness backend returned non-finite output")
    if time_axis.size > 1 and not np.all(np.diff(time_axis) > 0.0):
        raise RuntimeError("MoSQITo loudness backend returned unusable output")

    max_sone = float(np.max(values))
    return time_axis.tolist(), values.tolist(), max_sone


def compute_loudness_window_sone(signal: np.ndarray, sample_rate: float) -> float:
    """Compute one short-window frequency-domain loudness estimate in sones."""
    sig, backend_sample_rate = _prepare_loudness_signal(signal, sample_rate)
    window_samples = _loudness_window_samples(backend_sample_rate)
    if sig.size > window_samples:
        sig = sig[-window_samples:]
    windows = np.zeros((window_samples, 1), dtype=np.float64)
    windows[: sig.size, 0] = sig

    values = _compute_loudness_values_from_windows(windows, backend_sample_rate)
    return float(values[0])


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
        boundary=None,
        padded=False,
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
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size <= 0:
        raise ValueError("FFT input is empty")
    try:
        resolved_sample_rate = float(sample_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_rate must be a finite positive number") from exc
    if not np.isfinite(resolved_sample_rate) or resolved_sample_rate <= 0.0:
        raise ValueError("sample_rate must be a finite positive number")
    if isinstance(nfft, (bool, np.bool_)) or not isinstance(nfft, Integral):
        raise ValueError("nfft must be a positive integer")
    resolved_nfft = int(nfft)
    if resolved_nfft <= 0:
        raise ValueError("nfft must be a positive integer")

    if sig.size <= resolved_nfft:
        if sig.size < resolved_nfft:
            sig = np.pad(sig, (0, resolved_nfft - sig.size))
        window = np.hanning(resolved_nfft)
        spectrum = np.fft.rfft(sig * window)
        mag = np.abs(spectrum) * 2.0 / resolved_nfft
    else:
        window = np.hanning(resolved_nfft)
        mag_sum = np.zeros(resolved_nfft // 2 + 1, dtype=np.float64)
        window_count = 0
        for start in _iter_fft_window_starts(sig.size, resolved_nfft):
            windowed = sig[start:start + resolved_nfft] * window
            spectrum = np.fft.rfft(windowed)
            mag_sum += np.abs(spectrum) * 2.0 / resolved_nfft
            window_count += 1
        mag = mag_sum / max(window_count, 1)
    mag_db = 20.0 * np.log10(np.maximum(mag, 1e-30))
    freqs = np.fft.rfftfreq(resolved_nfft, d=1.0 / resolved_sample_rate)
    return freqs, mag_db


def _iter_fft_window_starts(signal_size: int, nfft: int):
    hop = max(1, nfft // 2)
    start = 0
    last_start = None
    while start + nfft <= signal_size:
        yield start
        last_start = start
        start += hop

    tail_start = signal_size - nfft
    if last_start != tail_start:
        yield tail_start


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
