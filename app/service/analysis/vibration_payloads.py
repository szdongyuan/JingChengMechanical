from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np

_LOW_FREQUENCY_CUTOFF_HZ = 200.0
_FUNDAMENTAL_MIN_HZ = 50.0
_FUNDAMENTAL_MAX_HZ = 150.0
_MAX_FUNDAMENTAL_HARMONICS = 6


def build_vibration_payload(
    *,
    channel_index: int,
    signal: Iterable[float] | np.ndarray,
    sample_rate: float,
    calibration_value: float | None,
) -> dict | None:
    try:
        sample_rate = float(sample_rate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        return None

    values = np.asarray(signal, dtype=np.float64).reshape(-1)
    values = values[np.isfinite(values)]
    if values.size <= 0:
        return None

    overall_rms = _rms(values)
    overall_peak = float(np.max(np.abs(values))) if values.size else 0.0
    overall_crest = _crest_factor(overall_peak, overall_rms)
    times_sec, rms_values, peak_values, crest_factors = _trend(values, sample_rate)
    (
        frequency_times_sec,
        fundamental_frequency_values_hz,
        dominant_frequency_values_hz,
        spectral_centroid_values_hz,
        frequency_bandwidth_values_hz,
    ) = _frequency_trend(values, sample_rate)

    return {
        "channel_index": int(channel_index),
        "sample_rate": sample_rate,
        "calibration_value": _calibration_value(calibration_value),
        "overall_rms": overall_rms,
        "overall_peak": overall_peak,
        "overall_crest_factor": overall_crest,
        "low_frequency_energy_share": _low_frequency_energy_share(values, sample_rate),
        "dominant_frequency_hz": _dominant_frequency_hz(values, sample_rate),
        "times_sec": times_sec,
        "rms_values": rms_values,
        "peak_values": peak_values,
        "crest_factors": crest_factors,
        "frequency_times_sec": frequency_times_sec,
        "fundamental_frequency_values_hz": fundamental_frequency_values_hz,
        "dominant_frequency_values_hz": dominant_frequency_values_hz,
        "spectral_centroid_values_hz": spectral_centroid_values_hz,
        "frequency_bandwidth_values_hz": frequency_bandwidth_values_hz,
    }


def _calibration_value(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _rms(values: np.ndarray) -> float:
    if values.size <= 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(values))))


def _crest_factor(peak: float, rms: float) -> float:
    if rms <= 0:
        return 0.0
    return float(peak / rms)


def _dominant_frequency_hz(values: np.ndarray, sample_rate: float) -> float | None:
    freqs, energy = _frequency_energy(values, sample_rate)
    if freqs.size <= 1 or energy.size <= 1:
        return None
    energy = energy.copy()
    energy[0] = 0.0
    peak_index = int(np.argmax(energy))
    if energy[peak_index] <= 0.0:
        return None
    return float(freqs[peak_index])


def _low_frequency_energy_share(values: np.ndarray, sample_rate: float) -> float:
    freqs, energy = _frequency_energy(values, sample_rate)
    if freqs.size <= 1 or energy.size <= 1:
        return 0.0
    energy = energy.copy()
    energy[0] = 0.0
    total_energy = float(np.sum(energy))
    if total_energy <= 0.0:
        return 0.0
    low_cutoff_hz = min(_LOW_FREQUENCY_CUTOFF_HZ, sample_rate / 2.0)
    low_energy = float(np.sum(energy[freqs <= low_cutoff_hz]))
    share = max(0.0, min(1.0, low_energy / total_energy))
    return float(share)


def _frequency_energy(values: np.ndarray, sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    if values.size < 2:
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    centered = values - float(np.mean(values))
    if not np.any(centered):
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    window = np.hanning(centered.size) if centered.size > 2 else np.ones(centered.size)
    windowed = centered * window
    nfft = max(centered.size, _next_power_of_two(centered.size * 4))
    spectrum = np.fft.rfft(windowed, n=nfft)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / sample_rate)
    energy = np.square(np.abs(spectrum))
    return freqs, energy


def _next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (int(value) - 1).bit_length()


def _window_and_hop(values: np.ndarray, sample_rate: float) -> tuple[int, int]:
    window = max(1, int(round(sample_rate * 0.1)))
    hop = max(1, window // 2)
    if values.size < window:
        window = values.size
    return window, hop


def _trend(values: np.ndarray, sample_rate: float) -> tuple[list[float], list[float], list[float], list[float]]:
    window, hop = _window_and_hop(values, sample_rate)
    times: list[float] = []
    rms_values: list[float] = []
    peak_values: list[float] = []
    crest_factors: list[float] = []
    for start in range(0, max(values.size - window + 1, 1), hop):
        segment = values[start : start + window]
        if segment.size <= 0:
            continue
        rms = _rms(segment)
        peak = float(np.max(np.abs(segment)))
        times.append(float((start + segment.size / 2.0) / sample_rate))
        rms_values.append(rms)
        peak_values.append(peak)
        crest_factors.append(_crest_factor(peak, rms))
        if start + window >= values.size:
            break
    return times, rms_values, peak_values, crest_factors


def _frequency_trend(
    values: np.ndarray,
    sample_rate: float,
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    window, hop = _window_and_hop(values, sample_rate)
    times: list[float] = []
    fundamental_values: list[float] = []
    dominant_values: list[float] = []
    centroid_values: list[float] = []
    bandwidth_values: list[float] = []
    for start in range(0, max(values.size - window + 1, 1), hop):
        segment = values[start : start + window]
        if segment.size <= 1:
            continue
        features = _segment_frequency_features(segment, sample_rate)
        if features is None:
            if start + window >= values.size:
                break
            continue
        fundamental, dominant, centroid, bandwidth = features
        times.append(float((start + segment.size / 2.0) / sample_rate))
        fundamental_values.append(fundamental)
        dominant_values.append(dominant)
        centroid_values.append(centroid)
        bandwidth_values.append(bandwidth)
        if start + window >= values.size:
            break
    return times, fundamental_values, dominant_values, centroid_values, bandwidth_values


def _segment_frequency_features(
    values: np.ndarray,
    sample_rate: float,
) -> tuple[float, float, float, float] | None:
    freqs, energy = _frequency_energy(values, sample_rate)
    if freqs.size <= 1 or energy.size <= 1:
        return None
    energy = energy.copy()
    energy[0] = 0.0
    total_energy = float(np.sum(energy))
    if total_energy <= 0.0:
        return None

    peak_index = int(np.argmax(energy))
    dominant = float(freqs[peak_index])
    fundamental = _estimate_fundamental_hz(freqs, energy)
    if fundamental is None:
        return None
    centroid = float(np.sum(freqs * energy) / total_energy)
    bandwidth = float(np.sqrt(np.sum(np.square(freqs - centroid) * energy) / total_energy))
    return fundamental, dominant, centroid, bandwidth


def _estimate_fundamental_hz(
    freqs: np.ndarray,
    energy: np.ndarray,
) -> float | None:
    if freqs.size <= 1 or energy.size <= 1:
        return None
    nyquist = float(freqs[-1])
    lower = _FUNDAMENTAL_MIN_HZ
    upper = min(_FUNDAMENTAL_MAX_HZ, nyquist)
    if upper < lower:
        return None

    freq_step = float(freqs[1] - freqs[0]) if freqs.size > 1 else 1.0
    candidate_step = max(freq_step / 2.0, 0.5)
    tolerance = max(freq_step * 1.5, 1.0)
    total_energy = float(np.sum(energy))
    if total_energy <= 0.0:
        return None

    hit_threshold = total_energy * 0.005
    required_score = total_energy * 0.01
    best_frequency: float | None = None
    best_score = 0.0

    for candidate in np.arange(lower, upper + candidate_step * 0.5, candidate_step):
        max_harmonic = min(
            _MAX_FUNDAMENTAL_HARMONICS,
            int(math.floor(nyquist / float(candidate))),
        )
        if max_harmonic <= 0:
            continue

        weighted_energy = 0.0
        hit_count = 0
        first_harmonic_energy = 0.0
        for harmonic in range(1, max_harmonic + 1):
            target = float(candidate) * harmonic
            harmonic_energy = _local_peak_energy(freqs, energy, target, tolerance)
            if harmonic == 1:
                first_harmonic_energy = harmonic_energy
            if harmonic_energy >= hit_threshold:
                hit_count += 1
            weighted_energy += harmonic_energy / math.sqrt(harmonic)

        if weighted_energy <= 0.0:
            continue
        support = 1.0 + (0.45 * max(0, hit_count - 1))
        if first_harmonic_energy >= hit_threshold:
            support += 0.20
        score = weighted_energy * support
        if score > best_score:
            best_score = score
            best_frequency = float(candidate)

    if best_frequency is None or best_score < required_score:
        return None
    return best_frequency


def _local_peak_energy(
    freqs: np.ndarray,
    energy: np.ndarray,
    target_hz: float,
    tolerance_hz: float,
) -> float:
    mask = np.abs(freqs - target_hz) <= tolerance_hz
    if not np.any(mask):
        return 0.0
    return float(np.max(energy[mask]))


__all__ = ["build_vibration_payload"]
