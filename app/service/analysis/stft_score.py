"""Pure STFT health score calculations."""
from __future__ import annotations

from collections.abc import Mapping
from numbers import Integral

import numpy as np

from app.service.analysis import dsp


F0_MIN_HZ = 30.0
F0_MAX_HZ = 150.0
F0_WINDOW_SEC = 0.5
F0_MIN_WINDOW_SEC = 0.2
F0_AUTOCORRELATION_THRESHOLD = 0.30
F0_BOUNDARY_LAG_TOLERANCE = 0.05
LOCAL_MODULATION_WINDOW_SEC = 1.0
LOUDNESS_CHANGE_SIDE_WINDOW_SEC = 0.15
LOUDNESS_CHANGE_LOW_PERCENTILE = 10.0
LOUDNESS_CHANGE_HIGH_PERCENTILE = 90.0
LOCAL_F0_WINDOW_SEC = 1.0
SPEED_LOCAL_RIPPLE_SPAN_SEC = 0.25
SPEED_LOCAL_RIPPLE_MIN_PERCENTILE_SAMPLES = 5
SPEED_LOCAL_RIPPLE_LOW_PERCENTILE = 10.0
SPEED_LOCAL_RIPPLE_HIGH_PERCENTILE = 90.0
EPSILON = 1e-9

_SUMMARY_MODES = {"percentile", "mean", "min"}


def default_score_config() -> dict:
    return {
        "stft_score_enabled": True,
        "stft_score_summary_mode": "percentile",
        "stft_score_percentile": 10.0,
        "stft_score_loudness_reference_sone": 10.0,
        "stft_score_modulation_reference": 0.20,
        "stft_score_harmonic_ratio_reference": 0.25,
        "stft_score_frequency_instability_reference": 0.05,
        "stft_score_harmonic_start_order": 2,
        "stft_score_harmonic_end_order": 100,
    }


def normalize_score_config(channel_config: dict | None) -> dict:
    config = default_score_config()
    if not channel_config:
        return config
    if not isinstance(channel_config, Mapping):
        return config

    enabled = channel_config.get("stft_score_enabled", config["stft_score_enabled"])
    if isinstance(enabled, (bool, np.bool_)):
        config["stft_score_enabled"] = bool(enabled)

    mode = str(channel_config.get("stft_score_summary_mode", config["stft_score_summary_mode"]))
    if mode in _SUMMARY_MODES:
        config["stft_score_summary_mode"] = mode

    config["stft_score_percentile"] = _positive_float_in_range(
        channel_config.get("stft_score_percentile"),
        default=config["stft_score_percentile"],
        minimum=0.0,
        maximum=100.0,
        include_minimum=False,
    )
    for key in (
        "stft_score_loudness_reference_sone",
        "stft_score_modulation_reference",
        "stft_score_harmonic_ratio_reference",
        "stft_score_frequency_instability_reference",
    ):
        config[key] = _positive_float_in_range(
            channel_config.get(key),
            default=config[key],
            minimum=0.0,
            include_minimum=False,
        )

    start_order = _integer_at_least(
        channel_config.get("stft_score_harmonic_start_order"),
        default=config["stft_score_harmonic_start_order"],
        minimum=2,
    )
    end_order = _integer_at_least(
        channel_config.get("stft_score_harmonic_end_order"),
        default=config["stft_score_harmonic_end_order"],
        minimum=start_order,
    )
    config["stft_score_harmonic_start_order"] = start_order
    config["stft_score_harmonic_end_order"] = max(end_order, start_order)
    return config


def compute_stft_score_payload(
    *,
    signal,
    sample_rate,
    stft_nfft,
    stft_overlap,
    score_config,
    absolute_start_sec=0.0,
    loudness_context_signal=None,
    loudness_trace=None,
    stft_times=None,
    stft_frequencies=None,
    stft_magnitudes=None,
) -> dict | None:
    config = normalize_score_config(score_config)
    if not config["stft_score_enabled"]:
        return {"enabled": False}

    prepared = _prepare_signal_and_params(signal, sample_rate, stft_nfft, stft_overlap)
    if prepared is None:
        return None
    sig, resolved_sample_rate, resolved_nfft, resolved_overlap = prepared

    if (
        stft_times is not None
        and stft_frequencies is not None
        and stft_magnitudes is not None
    ):
        relative_times = stft_times
        frequencies_hz = stft_frequencies
        magnitudes_db = stft_magnitudes
    else:
        try:
            relative_times, frequencies_hz, magnitudes_db = dsp.compute_stft(
                sig,
                resolved_sample_rate,
                resolved_nfft,
                resolved_overlap,
            )
        except Exception:
            return None

    relative_times = _float_array_or_none(relative_times, reshape=True)
    frequencies_hz = _float_array_or_none(frequencies_hz, reshape=True)
    magnitudes_db = _float_array_or_none(magnitudes_db, reshape=False)
    if relative_times is None or frequencies_hz is None or magnitudes_db is None:
        return None
    if (
        relative_times.size == 0
        or frequencies_hz.size == 0
        or magnitudes_db.shape != (frequencies_hz.size, relative_times.size)
        or not np.all(np.isfinite(relative_times))
        or not np.all(np.isfinite(frequencies_hz))
        or not np.all(np.isfinite(magnitudes_db))
    ):
        return None

    absolute_start = _finite_float(absolute_start_sec, default=0.0)
    window_duration_sec = sig.size / resolved_sample_rate
    cached_loudness = _valid_loudness_trace(
        loudness_trace,
        window_duration_sec=window_duration_sec,
    )
    if cached_loudness is None:
        try:
            if loudness_context_signal is not None and absolute_start > 0.0:
                loudness_times, loudness_values, _max_loudness = (
                    dsp.compute_loudness_trace_for_window(
                        loudness_context_signal,
                        resolved_sample_rate,
                        absolute_start,
                        absolute_start + window_duration_sec,
                    )
                )
            else:
                loudness_times, loudness_values, _max_loudness = dsp.compute_loudness_trace(
                    sig,
                    resolved_sample_rate,
                )
        except Exception:
            return None
    else:
        loudness_times, loudness_values = cached_loudness

    absolute_times = relative_times + absolute_start
    loudness = _interpolate_loudness_to_score_times(
        loudness_times,
        loudness_values,
        absolute_times,
        absolute_start=absolute_start,
    )
    if loudness is None:
        return None

    trace_times_absolute = _float_array_or_none(loudness_times, reshape=True)
    if trace_times_absolute is None:
        return None
    trace_times_absolute = trace_times_absolute + absolute_start
    local_changes = _local_loudness_change_values(
        trace_times=trace_times_absolute,
        trace_values=loudness_values,
        score_times=absolute_times,
    )
    if local_changes is None or local_changes.size != absolute_times.size:
        return None

    scores = []
    change_reference = config["stft_score_modulation_reference"]
    loudness_reference = config["stft_score_loudness_reference_sone"]

    for frame_loudness, change_value in zip(loudness, local_changes, strict=False):
        if not np.isfinite(frame_loudness) or not np.isfinite(change_value):
            return None
        loudness_factor = _clamp01(float(frame_loudness) / loudness_reference)
        change_factor = _clamp01(float(change_value) / change_reference)
        anomaly = loudness_factor * change_factor
        scores.append(float(100.0 * (1.0 - anomaly)))

    score_array = np.asarray(scores, dtype=np.float64)
    if score_array.size == 0 or not np.all(np.isfinite(score_array)):
        return None

    summary = summary_score(
        score_array,
        mode="min",
        percentile=config["stft_score_percentile"],
    )
    if summary is None:
        return None

    return {
        "enabled": True,
        "summary_score": float(summary),
        "times_sec": [float(value) for value in absolute_times],
        "scores": [float(value) for value in score_array],
        "summary_mode": "min",
        "percentile": float(config["stft_score_percentile"]),
    }


def estimate_periodic_f0_hz(signal, *, sample_rate: float) -> float | None:
    try:
        sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return None
    resolved_sample_rate = _finite_float(sample_rate)
    if resolved_sample_rate is None or resolved_sample_rate <= 0.0:
        return None
    if sig.size < int(np.ceil(F0_MIN_WINDOW_SEC * resolved_sample_rate)):
        return None
    if sig.size < 3 or not np.all(np.isfinite(sig)):
        return None

    centered = sig - float(np.mean(sig))
    window = np.hanning(centered.size)
    windowed = centered * window
    energy = float(np.dot(windowed, windowed))
    if energy <= 0.0 or not np.isfinite(energy):
        return None

    min_search_lag = resolved_sample_rate / F0_MAX_HZ
    max_search_lag = resolved_sample_rate / F0_MIN_HZ
    min_lag = max(1, int(np.floor(min_search_lag)))
    max_lag = int(np.ceil(max_search_lag))
    max_lag = min(max_lag, centered.size - 2)
    if max_lag < min_lag:
        return None

    analysis_max_lag = min(max_lag + 1, centered.size - 1)
    autocorrelation = _fft_positive_autocorrelation(windowed, max_lag=analysis_max_lag)
    autocorrelation = autocorrelation / max(float(autocorrelation[0]), EPSILON)

    candidate_peaks = []
    for lag in range(min_lag, max_lag + 1):
        peak = _autocorrelation_peak_in_search_range(
            autocorrelation,
            lag=lag,
            min_search_lag=min_search_lag,
            max_search_lag=max_search_lag,
        )
        if peak is not None:
            candidate_peaks.append(peak)

    reliable_candidates = [
        (lag, float(value))
        for lag, value in candidate_peaks
        if float(value) >= F0_AUTOCORRELATION_THRESHOLD
    ]
    if not reliable_candidates:
        return None

    strongest_lag, _strongest_value = max(reliable_candidates, key=lambda item: item[1])
    return float(resolved_sample_rate / float(strongest_lag))


def _dominant_speed_frequency_hz(signal, sample_rate) -> float | None:
    try:
        sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return None
    resolved_sample_rate = _finite_float(sample_rate)
    if resolved_sample_rate is None or resolved_sample_rate <= 0.0:
        return None
    if sig.size == 0 or not np.all(np.isfinite(sig)):
        return None

    nperseg = min(sig.size, 32768)
    noverlap = nperseg // 2
    try:
        frequencies_hz, spectrum = dsp.scipy_signal.welch(
            sig,
            fs=resolved_sample_rate,
            window="hann",
            nperseg=nperseg,
            noverlap=noverlap,
            detrend="constant",
            scaling="spectrum",
        )
    except Exception:
        return None

    frequencies = _float_array_or_none(frequencies_hz, reshape=True)
    spectrum_values = _float_array_or_none(spectrum, reshape=True)
    if frequencies is None or spectrum_values is None:
        return None
    if frequencies.size == 0 or frequencies.size != spectrum_values.size:
        return None

    with np.errstate(divide="ignore", invalid="ignore"):
        spectrum_db = 10.0 * np.log10(spectrum_values)
    finite_band = (
        (frequencies >= F0_MIN_HZ)
        & (frequencies <= F0_MAX_HZ)
        & np.isfinite(frequencies)
        & np.isfinite(spectrum_db)
    )
    if int(np.count_nonzero(finite_band)) < 3:
        return None

    band_indices = np.flatnonzero(finite_band)
    band_db = spectrum_db[band_indices]
    band_frequencies = frequencies[band_indices]
    selected_band_index = int(np.argmax(band_db))
    peak_db = float(band_db[selected_band_index])
    peak_frequency = float(band_frequencies[selected_band_index])
    if not np.isfinite(peak_db) or not np.isfinite(peak_frequency) or peak_frequency <= 0.0:
        return None

    median_db = float(np.median(band_db))
    if not np.isfinite(median_db) or peak_db - median_db < 10.0:
        return None

    peaks, properties = dsp.scipy_signal.find_peaks(band_db, prominence=0)
    peak_matches = np.flatnonzero(peaks == selected_band_index)
    if peak_matches.size:
        prominences = properties.get("prominences")
        if prominences is None:
            return None
        prominence_db = float(prominences[int(peak_matches[0])])
        if not np.isfinite(prominence_db) or prominence_db < 6.0:
            return None

    selected_index = int(band_indices[selected_band_index])
    if 0 < selected_index < frequencies.size - 1:
        left = float(spectrum_db[selected_index - 1])
        center = float(spectrum_db[selected_index])
        right = float(spectrum_db[selected_index + 1])
        denominator = left - (2.0 * center) + right
        if np.isfinite([left, center, right, denominator]).all() and denominator < 0.0:
            offset = 0.5 * (left - right) / denominator
            bin_width_hz = float(
                (frequencies[selected_index + 1] - frequencies[selected_index - 1]) / 2.0
            )
            interpolated = peak_frequency + (float(offset) * bin_width_hz)
            if (
                np.isfinite(interpolated)
                and F0_MIN_HZ <= interpolated <= F0_MAX_HZ
                and interpolated > 0.0
            ):
                return float(interpolated)

    return peak_frequency


def _estimate_speed_frequency_track(signal, sample_rate, *, score_times_sec) -> np.ndarray:
    score_times = _float_array_or_none(score_times_sec, reshape=True)
    if score_times is None:
        return np.asarray([], dtype=np.float64)
    unavailable = np.full(score_times.size, np.nan, dtype=np.float64)

    try:
        sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return unavailable
    resolved_sample_rate = _finite_float(sample_rate)
    if resolved_sample_rate is None or resolved_sample_rate <= 0.0:
        return unavailable
    if sig.size == 0 or not np.all(np.isfinite(sig)):
        return unavailable

    speed_line_hz = _dominant_speed_frequency_hz(sig, resolved_sample_rate)
    if speed_line_hz is None:
        return unavailable

    band_width_hz = max(float(speed_line_hz) * 0.08, 1.5)
    low_hz = max(F0_MIN_HZ, float(speed_line_hz) - band_width_hz)
    high_hz = min(F0_MAX_HZ, float(speed_line_hz) + band_width_hz)
    if high_hz < low_hz:
        return unavailable

    nperseg = min(sig.size, 8192)
    hop_samples = max(1, int(round(resolved_sample_rate / 25.0)))
    nfft = max(65536, nperseg)
    centered = sig - float(np.mean(sig))
    frequencies = np.fft.rfftfreq(nfft, d=1.0 / resolved_sample_rate)
    if frequencies.size == 0 or not np.all(np.isfinite(frequencies)):
        return unavailable

    band_mask = (frequencies >= low_hz) & (frequencies <= high_hz)
    band_indices = np.flatnonzero(band_mask)
    if band_indices.size == 0:
        return unavailable

    selected_start = max(0, int(band_indices[0]) - 1)
    selected_stop = min(frequencies.size, int(band_indices[-1]) + 2)
    selected_indices = np.arange(selected_start, selected_stop, dtype=np.int64)
    if selected_indices.size == 0:
        return unavailable
    band_positions = band_indices - selected_start

    frame_starts = range(0, sig.size - nperseg + 1, hop_samples)
    frame_times = []
    frame_estimates = []
    window = np.hanning(nperseg)

    for start in frame_starts:
        frame = centered[start : start + nperseg]
        if frame.size != nperseg:
            continue
        spectrum = np.fft.rfft(frame * window, n=nfft)[selected_indices]
        with np.errstate(divide="ignore", invalid="ignore"):
            log_magnitude = np.log(np.maximum(np.abs(spectrum), EPSILON))

        band_log_magnitude = log_magnitude[band_positions]
        finite_band = np.isfinite(band_log_magnitude)
        if not np.any(finite_band):
            frame_times.append((float(start) + (float(nperseg) / 2.0)) / resolved_sample_rate)
            frame_estimates.append(np.nan)
            continue

        selected_position = int(
            np.argmax(np.where(finite_band, band_log_magnitude, -np.inf))
        )
        selected_index = int(band_indices[selected_position])
        estimate_hz = float(frequencies[selected_index])
        selected_log_position = selected_index - selected_start

        if 0 < selected_log_position < log_magnitude.size - 1:
            left = float(log_magnitude[selected_log_position - 1])
            center = float(log_magnitude[selected_log_position])
            right = float(log_magnitude[selected_log_position + 1])
            denominator = left - (2.0 * center) + right
            if np.isfinite([left, center, right, denominator]).all() and denominator < 0.0:
                offset = 0.5 * (left - right) / denominator
                bin_width_hz = float(
                    (frequencies[selected_index + 1] - frequencies[selected_index - 1])
                    / 2.0
                )
                interpolated_hz = estimate_hz + (float(offset) * bin_width_hz)
                if np.isfinite(interpolated_hz) and low_hz <= interpolated_hz <= high_hz:
                    estimate_hz = float(interpolated_hz)

        frame_times.append((float(start) + (float(nperseg) / 2.0)) / resolved_sample_rate)
        if np.isfinite(estimate_hz) and estimate_hz > 0.0:
            frame_estimates.append(estimate_hz)
        else:
            frame_estimates.append(np.nan)

    stft_times = np.asarray(frame_times, dtype=np.float64)
    column_estimates = np.asarray(frame_estimates, dtype=np.float64)
    if stft_times.size == 0:
        return unavailable

    finite_columns = np.isfinite(stft_times) & np.isfinite(column_estimates)
    if not np.any(finite_columns):
        return unavailable

    result = unavailable.copy()
    finite_times = stft_times[finite_columns]
    finite_estimates = column_estimates[finite_columns]
    in_range = (
        np.isfinite(score_times)
        & (score_times >= float(finite_times[0]))
        & (score_times <= float(finite_times[-1]))
    )
    result[in_range] = np.interp(score_times[in_range], finite_times, finite_estimates)
    return result


def _autocorrelation_peak_in_search_range(
    autocorrelation: np.ndarray,
    *,
    lag: int,
    min_search_lag: float,
    max_search_lag: float,
) -> tuple[float, float] | None:
    if lag <= 0 or lag >= autocorrelation.size - 1:
        return None

    left = float(autocorrelation[lag - 1])
    value = float(autocorrelation[lag])
    right = float(autocorrelation[lag + 1])
    if value < left or value < right:
        return None

    peak_lag = _quadratic_peak_lag(left, value, right, center_lag=lag)
    peak_value = value
    if peak_lag is not None:
        peak_value = _quadratic_peak_value(left, value, right)
    else:
        peak_lag = float(lag)

    if peak_lag <= 0.0 or not np.isfinite(peak_lag):
        return None
    if peak_value < 0.0 or not np.isfinite(peak_value):
        return None

    if peak_lag < min_search_lag:
        if min_search_lag - peak_lag > F0_BOUNDARY_LAG_TOLERANCE:
            return None
        peak_lag = min_search_lag
    if peak_lag > max_search_lag:
        if peak_lag - max_search_lag > F0_BOUNDARY_LAG_TOLERANCE:
            return None
        peak_lag = max_search_lag

    return float(peak_lag), float(peak_value)


def _quadratic_peak_lag(
    left: float,
    center: float,
    right: float,
    *,
    center_lag: int,
) -> float | None:
    denominator = left - (2.0 * center) + right
    if denominator >= 0.0:
        return None
    offset = 0.5 * (left - right) / denominator
    return float(center_lag) + float(offset)


def _quadratic_peak_value(left: float, center: float, right: float) -> float:
    denominator = left - (2.0 * center) + right
    offset = 0.5 * (left - right) / denominator
    return float(center - (0.25 * (left - right) * offset))


def _fft_positive_autocorrelation(windowed: np.ndarray, *, max_lag: int) -> np.ndarray:
    fft_size = 1 << (int(windowed.size * 2 - 1).bit_length())
    spectrum = np.fft.rfft(windowed, n=fft_size)
    autocorrelation = np.fft.irfft(spectrum * np.conjugate(spectrum), n=fft_size)
    return autocorrelation[: max_lag + 1]


def harmonic_energy_ratio(
    *,
    frequencies_hz,
    frame_power,
    f0_hz,
    start_order: int,
    end_order: int,
) -> float:
    frequencies = _float_array_or_none(frequencies_hz, reshape=True)
    power = _float_array_or_none(frame_power, reshape=True)
    if frequencies is None or power is None:
        return 0.0
    if frequencies.size == 0 or frequencies.size != power.size:
        return 0.0
    if not np.all(np.isfinite(frequencies)) or not np.all(np.isfinite(power)):
        return 0.0

    f0 = _finite_float(f0_hz)
    if f0 is None or f0 <= 0.0:
        return 0.0
    if not _is_integer_order(start_order) or not _is_integer_order(end_order):
        return 0.0

    start = int(start_order)
    end = int(end_order)
    if start < 2 or end < start:
        return 0.0

    max_frequency = float(np.max(frequencies))
    last_order = min(end, int(np.floor(max_frequency / f0)))
    if last_order < start:
        return 0.0

    usable_power = np.maximum(power, 0.0)
    total_energy = float(np.sum(usable_power))
    if total_energy <= 0.0:
        return 0.0

    positive_diffs = np.diff(np.sort(frequencies))
    positive_diffs = positive_diffs[positive_diffs > 0.0]
    bin_width_hz = float(np.median(positive_diffs)) if positive_diffs.size else 0.0

    harmonic_mask = np.zeros(frequencies.shape, dtype=bool)
    for order in range(start, last_order + 1):
        harmonic_frequency = f0 * float(order)
        if harmonic_frequency > max_frequency:
            continue
        half_bandwidth = max(bin_width_hz, 0.02 * harmonic_frequency)
        harmonic_mask |= np.abs(frequencies - harmonic_frequency) <= half_bandwidth

    if not np.any(harmonic_mask):
        return 0.0
    harmonic_energy = float(np.sum(usable_power[harmonic_mask]))
    return float(np.clip(harmonic_energy / total_energy, 0.0, 1.0))


def summary_score(scores, *, mode: str, percentile: float) -> float | None:
    score_values = _float_array_or_none(scores, reshape=True)
    if score_values is None:
        return None
    score_values = score_values[np.isfinite(score_values)]
    if score_values.size == 0:
        return None

    if mode == "percentile":
        pct = _positive_float_in_range(
            percentile,
            default=10.0,
            minimum=0.0,
            maximum=100.0,
            include_minimum=False,
        )
        return float(np.percentile(score_values, pct))
    if mode == "mean":
        return float(np.mean(score_values))
    if mode == "min":
        return float(np.min(score_values))
    return None


def _prepare_signal_and_params(signal, sample_rate, stft_nfft, stft_overlap):
    try:
        sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return None
    resolved_sample_rate = _finite_float(sample_rate)
    if resolved_sample_rate is None or resolved_sample_rate <= 0.0:
        return None
    if sig.size == 0 or not np.all(np.isfinite(sig)):
        return None
    if not _is_integer_order(stft_nfft):
        return None
    resolved_nfft = int(stft_nfft)
    if resolved_nfft <= 0 or sig.size < resolved_nfft:
        return None
    resolved_overlap = _finite_float(stft_overlap)
    if resolved_overlap is None or resolved_overlap < 0.0 or resolved_overlap >= 1.0:
        return None
    return sig, resolved_sample_rate, resolved_nfft, float(resolved_overlap)


def _valid_loudness_trace(
    loudness_trace,
    *,
    window_duration_sec: float,
) -> tuple[np.ndarray, np.ndarray] | None:
    if not isinstance(loudness_trace, Mapping):
        return None
    if loudness_trace.get("algorithm") != dsp.LOUDNESS_TRACE_ALGORITHM:
        return None
    internal_rate_hz = _finite_float(loudness_trace.get("internal_rate_hz"))
    if internal_rate_hz is None:
        return None
    if not np.isclose(
        internal_rate_hz,
        dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
        rtol=0.0,
        atol=1e-9,
    ):
        return None

    times = _float_array_or_none(loudness_trace.get("times_sec"), reshape=True)
    values = _float_array_or_none(loudness_trace.get("values_sone"), reshape=True)
    if times is None or values is None:
        return None
    if times.size <= 0 or values.size <= 0 or times.size != values.size:
        return None
    if not np.all(np.isfinite(times)) or not np.all(np.isfinite(values)):
        return None
    if np.any(values < 0.0):
        return None
    if not np.isclose(times[0], 0.0, rtol=0.0, atol=1e-9):
        return None

    if times.size > 1:
        diffs = np.diff(times)
        if not np.all(diffs > 0.0):
            return None
        if not np.allclose(
            diffs,
            1.0 / internal_rate_hz,
            rtol=0.0,
            atol=1e-9,
        ):
            return None

    duration = _finite_float(window_duration_sec)
    if duration is None or duration <= 0.0:
        return None
    coverage_tolerance = 1.0 / internal_rate_hz
    if float(times[-1]) + coverage_tolerance < duration:
        return None
    return times, values


def _interpolate_loudness_to_score_times(
    loudness_times,
    loudness_values,
    score_times,
    *,
    absolute_start: float,
) -> np.ndarray | None:
    trace_times = _float_array_or_none(loudness_times, reshape=True)
    trace_values = _float_array_or_none(loudness_values, reshape=True)
    if trace_times is None or trace_values is None:
        return None
    trace_times = trace_times + absolute_start
    if trace_times.size == 0 or trace_times.size != trace_values.size:
        return None
    if not np.all(np.isfinite(trace_times)) or not np.all(np.isfinite(trace_values)):
        return None
    if np.any(trace_values < 0.0):
        return None

    order = np.argsort(trace_times)
    trace_times = trace_times[order]
    trace_values = trace_values[order]
    unique_times, unique_indices = np.unique(trace_times, return_index=True)
    unique_values = trace_values[unique_indices]
    if unique_times.size == 0:
        return None
    return np.interp(score_times, unique_times, unique_values)


def _percentile_span(values) -> float:
    arr = _float_array_or_none(values, reshape=True)
    if arr is None:
        return 0.0
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return 0.0
    high = float(np.percentile(arr, LOUDNESS_CHANGE_HIGH_PERCENTILE))
    low = float(np.percentile(arr, LOUDNESS_CHANGE_LOW_PERCENTILE))
    if not np.isfinite(high) or not np.isfinite(low):
        return 0.0
    return max(0.0, high - low)


def _local_loudness_change_values(
    *,
    trace_times,
    trace_values,
    score_times,
    side_window_sec: float = LOUDNESS_CHANGE_SIDE_WINDOW_SEC,
) -> np.ndarray | None:
    times = _float_array_or_none(trace_times, reshape=True)
    values = _float_array_or_none(trace_values, reshape=True)
    centers = _float_array_or_none(score_times, reshape=True)
    if times is None or values is None or centers is None:
        return None
    if times.size == 0 or times.size != values.size:
        return None
    if not np.all(np.isfinite(times)) or not np.all(np.isfinite(values)):
        return None
    if np.any(values < 0.0):
        return None

    window = _finite_float(side_window_sec)
    if window is None or window <= 0.0:
        return None

    order = np.argsort(times)
    times = times[order]
    values = values[order]
    changes = []
    for center in centers:
        if not np.isfinite(center):
            return None
        left = values[(times >= center - window) & (times <= center)]
        right = values[(times >= center) & (times <= center + window)]
        left_change = _percentile_span(left)
        right_change = _percentile_span(right)
        changes.append((left_change + right_change) / 2.0)
    return np.asarray(changes, dtype=np.float64)


def _modulation_factors(
    score_times: np.ndarray,
    loudness: np.ndarray,
    modulation_reference: float,
) -> list[float]:
    factors = []
    half_window = LOCAL_MODULATION_WINDOW_SEC / 2.0
    for time_sec in score_times:
        mask = np.abs(score_times - time_sec) <= half_window
        local = loudness[mask]
        if local.size <= 1:
            factors.append(0.0)
            continue
        local_mean = max(float(np.mean(local)), EPSILON)
        local_cv = float(np.std(local) / local_mean)
        factors.append(_clamp01(local_cv / modulation_reference))
    return factors


def _estimate_frame_f0_values(
    signal: np.ndarray,
    sample_rate: float,
    *,
    score_times_sec: np.ndarray,
    absolute_start_sec: float,
) -> list[float | None]:
    values: list[float | None] = []
    half_window = F0_WINDOW_SEC / 2.0
    for absolute_time in score_times_sec:
        relative_center = float(absolute_time - absolute_start_sec)
        start = max(0, int(round((relative_center - half_window) * sample_rate)))
        end = min(signal.size, int(round((relative_center + half_window) * sample_rate)))
        if end <= start:
            values.append(None)
            continue
        values.append(estimate_periodic_f0_hz(signal[start:end], sample_rate=sample_rate))
    return values


def _frequency_instability_factors(
    score_times: np.ndarray,
    f0_values: list[float | None],
    reference: float,
) -> list[float]:
    numeric_f0 = np.asarray(
        [np.nan if value is None else float(value) for value in f0_values],
        dtype=np.float64,
    )
    factors = []
    half_window = LOCAL_F0_WINDOW_SEC / 2.0
    for index, time_sec in enumerate(score_times):
        if not np.isfinite(numeric_f0[index]):
            factors.append(0.0)
            continue
        mask = (np.abs(score_times - time_sec) <= half_window) & np.isfinite(numeric_f0)
        local = numeric_f0[mask]
        if local.size < 2:
            factors.append(0.0)
            continue
        local_reference = float(np.median(local))
        if local_reference <= 0.0:
            factors.append(0.0)
            continue
        local_jitter = float(np.median(np.abs(np.diff(local))) / local_reference)
        local_drift = float(
            (np.percentile(local, 90.0) - np.percentile(local, 10.0))
            / local_reference
        )
        local_relative_change = max(local_jitter, local_drift)
        factors.append(_clamp01(local_relative_change / reference))
    return factors


def _speed_stability_factors(
    score_times,
    speed_track_hz,
    *,
    reference,
    absolute_start_sec,
    window_duration_sec,
) -> list[float]:
    try:
        times = np.asarray(score_times, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return []
    zeros = np.zeros(times.size, dtype=np.float64)

    try:
        speed_track = np.asarray(speed_track_hz, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return zeros.tolist()
    if speed_track.size != times.size:
        return zeros.tolist()

    resolved_reference = _finite_float(reference)
    if resolved_reference is None or resolved_reference <= 0.0:
        return zeros.tolist()

    window_duration = _finite_float(window_duration_sec)
    if window_duration is None or window_duration <= 0.0:
        return zeros.tolist()
    absolute_start = _finite_float(absolute_start_sec, default=0.0)

    relative_times = times - absolute_start
    valid = (
        np.isfinite(relative_times)
        & (relative_times >= 0.0)
        & (relative_times <= window_duration)
        & np.isfinite(speed_track)
        & (speed_track > 0.0)
    )
    if int(np.count_nonzero(valid)) < 3:
        return zeros.tolist()

    valid_times = relative_times[valid]
    valid_speeds = speed_track[valid]
    order = np.argsort(valid_times)
    valid_times = valid_times[order]
    valid_speeds = valid_speeds[order]
    if np.any(np.diff(valid_times) == 0.0):
        return zeros.tolist()

    factors = zeros.copy()
    half_span = SPEED_LOCAL_RIPPLE_SPAN_SEC / 2.0
    for index, _current_speed in enumerate(speed_track):
        if not valid[index]:
            continue
        center_time = float(relative_times[index])
        left_time = center_time - half_span
        right_time = center_time + half_span
        valid_start = float(valid_times[0])
        valid_end = float(valid_times[-1])
        if left_time < valid_start - EPSILON or right_time > valid_end + EPSILON:
            continue
        left_time = max(left_time, valid_start)
        right_time = min(right_time, valid_end)

        real_mask = (
            (valid_times >= left_time - EPSILON)
            & (valid_times <= right_time + EPSILON)
        )
        real_sample_count = int(np.count_nonzero(real_mask))
        if real_sample_count < 3:
            continue

        local_times = valid_times[real_mask]
        local_speeds = valid_speeds[real_mask].copy()
        if not np.any(np.isclose(local_times, left_time, rtol=0.0, atol=EPSILON)):
            local_speeds = np.append(
                local_speeds,
                float(np.interp(left_time, valid_times, valid_speeds)),
            )
        if not np.any(np.isclose(local_times, right_time, rtol=0.0, atol=EPSILON)):
            local_speeds = np.append(
                local_speeds,
                float(np.interp(right_time, valid_times, valid_speeds)),
            )

        if real_sample_count >= SPEED_LOCAL_RIPPLE_MIN_PERCENTILE_SAMPLES:
            local_low = float(
                np.percentile(
                    local_speeds,
                    SPEED_LOCAL_RIPPLE_LOW_PERCENTILE,
                    method="linear",
                )
            )
            local_high = float(
                np.percentile(
                    local_speeds,
                    SPEED_LOCAL_RIPPLE_HIGH_PERCENTILE,
                    method="linear",
                )
            )
        else:
            local_low = float(np.min(local_speeds))
            local_high = float(np.max(local_speeds))

        local_reference = float(np.median(local_speeds))
        if not np.isfinite(local_reference) or local_reference <= EPSILON:
            continue

        relative_swing = (local_high - local_low) / local_reference
        if np.isfinite(relative_swing):
            factors[index] = np.clip(relative_swing / resolved_reference, 0.0, 1.0)
    return factors.tolist()


def _positive_float_in_range(
    value,
    *,
    default: float,
    minimum: float,
    maximum: float | None = None,
    include_minimum: bool,
) -> float:
    resolved = _finite_float(value)
    if resolved is None:
        return float(default)
    if include_minimum:
        too_small = resolved < minimum
    else:
        too_small = resolved <= minimum
    if too_small:
        return float(default)
    if maximum is not None and resolved > maximum:
        return float(default)
    return float(resolved)


def _integer_at_least(value, *, default: int, minimum: int) -> int:
    if not _is_integer_order(value):
        return int(default)
    resolved = int(value)
    if resolved < minimum:
        return int(default)
    return resolved


def _is_integer_order(value) -> bool:
    return isinstance(value, Integral) and not isinstance(value, (bool, np.bool_))


def _finite_float(value, *, default=None):
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not np.isfinite(resolved):
        return default
    return resolved


def _float_array_or_none(values, *, reshape: bool) -> np.ndarray | None:
    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        return None
    if reshape:
        return array.reshape(-1)
    return array


def _clamp01(value: float) -> float:
    return float(np.clip(value, 0.0, 1.0))
