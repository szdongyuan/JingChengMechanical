from __future__ import annotations

import logging
import math

import numpy as np

from app.service.analysis import dsp, stft_score
from app.service.analysis.golden_reference import GoldenReferenceCache
from app.service.analysis.judgment import _runtime_channel_defaults
from app.service.analysis.vibration_payloads import build_vibration_payload

_SUMMARY_ROW_DEFS = (
    ("spl", "SPL"),
    ("spl_average", "平均SPL"),
    ("loudness", "Loudness"),
    ("loudness_average", "平均Loudness"),
    ("fft", "FFT"),
    ("octave", "倍频程"),
    ("ai", "AI分析"),
)
_NO_RESULT_FALLBACK_MESSAGE = "分析结果不可用"
_NO_RESULT_STATUS = "无结果"
_REF_PRESSURE = 20e-6
_SPL_CHUNK_DURATION_SEC = 0.05
logger = logging.getLogger(__name__)


def canonical_channel_label(channel_index: int | None) -> str:
    if isinstance(channel_index, int) and channel_index >= 0:
        return f"CH{channel_index + 1}"
    return "--"


def build_summary_payload(
    *,
    config: dict,
    judgment_payload: dict | None,
    ai_review: dict | None = None,
    error_message: str | None = None,
) -> dict:
    if not isinstance(judgment_payload, dict):
        return {
            "overall_status": "no_result",
            "overall_label": _NO_RESULT_STATUS,
            "error_message": _normalize_error_message(error_message),
            "rows": [_build_full_no_result_row(item_key, label) for item_key, label in _SUMMARY_ROW_DEFS],
        }

    selected_channels = _selected_channels(config, judgment_payload)
    channel_configs = _channel_config_by_index(config)
    channel_results = _channel_result_by_index(judgment_payload)
    ai_row, ai_error_message = _build_ai_row(
        selected_channels=selected_channels,
        ai_review=ai_review,
    )
    spl_row, spl_error_message = _build_spl_row(
        selected_channels=selected_channels,
        channel_configs=channel_configs,
        channel_results=channel_results,
    )
    spl_average_row, spl_average_error_message = _build_spl_average_row(
        selected_channels=selected_channels,
        channel_configs=channel_configs,
        channel_results=channel_results,
    )
    loudness_row, loudness_error_message = _build_loudness_row(
        selected_channels=selected_channels,
        channel_configs=channel_configs,
        channel_results=channel_results,
    )
    loudness_average_row, loudness_average_error_message = _build_loudness_average_row(
        selected_channels=selected_channels,
        channel_configs=channel_configs,
        channel_results=channel_results,
    )
    fft_row, fft_error_message = _build_band_row(
        item_key="fft",
        label="FFT",
        config_key="fft_band_limits",
        result_key="fft_band_results",
        available_key="fft_available",
        error_key="fft_error_message",
        selected_channels=selected_channels,
        channel_configs=channel_configs,
        channel_results=channel_results,
    )
    octave_row, octave_error_message = _build_band_row(
        item_key="octave",
        label="倍频程",
        config_key="oct_band_limits",
        result_key="oct_band_results",
        available_key="octave_available",
        error_key="octave_error_message",
        selected_channels=selected_channels,
        channel_configs=channel_configs,
        channel_results=channel_results,
    )
    rows = [
        spl_row,
        spl_average_row,
        loudness_row,
        loudness_average_row,
        fft_row,
        octave_row,
        ai_row,
    ]
    overall_status = _overall_status(rows)
    return {
        "overall_status": overall_status,
        "overall_label": _overall_label(overall_status),
        "error_message": (
            _normalize_error_message(
                error_message,
                spl_error_message,
                spl_average_error_message,
                loudness_error_message,
                loudness_average_error_message,
                fft_error_message,
                octave_error_message,
                ai_error_message,
            )
            if overall_status == "no_result"
            else None
        ),
        "rows": rows,
    }


def build_chart_payload(
    *,
    buffer: np.ndarray | None,
    config: dict,
    sample_rate: float,
    mic_cal: dict,
    analysis_window: tuple[float, float] | list[float] | None,
    judgment_payload: dict | None = None,
    channel_column_map: dict[int, int] | None = None,
    loudness_trace_cache: dict[int, dict] | None = None,
    allow_golden_runtime_resample: bool = False,
) -> dict[int, dict]:
    if not isinstance(buffer, np.ndarray) or buffer.size <= 0:
        return {}

    try:
        sample_rate = float(sample_rate)
    except (TypeError, ValueError):
        return {}
    if sample_rate <= 0:
        return {}

    window, window_start_sec = _slice_analysis_window_with_bounds(
        buffer,
        sample_rate,
        analysis_window,
    )
    if window.size <= 0:
        return {}

    channel_results = (
        _channel_result_by_index(judgment_payload)
        if isinstance(judgment_payload, dict)
        else {}
    )
    golden_cache = GoldenReferenceCache()
    configured_input_sample_rate = config.get("input_device_samplerate")
    payload: dict[int, dict] = {}
    vibration_channel_index = _configured_vibration_view_channel(config)
    for channel_index in _selected_chart_channels(config, buffer):
        signal = _extract_channel(
            window,
            channel_index,
            channel_column_map=channel_column_map,
        )
        if signal is None or signal.size <= 0:
            continue
        channel_config = _runtime_channel_config(config, channel_index)
        mic_sensitivity = _mic_sensitivity(mic_cal, channel_index)
        calibrated = dsp.apply_mic_calibration(signal, mic_sensitivity)
        full_signal = _extract_channel(
            buffer,
            channel_index,
            channel_column_map=channel_column_map,
        )
        calibrated_full = (
            dsp.apply_mic_calibration(full_signal, mic_sensitivity)
            if full_signal is not None and full_signal.size > 0
            else None
        )
        channel_result = channel_results.get(channel_index)
        golden_bundle_loader = _build_golden_bundle_loader(
            channel_index=channel_index,
            channel_config=channel_config,
            configured_input_sample_rate=configured_input_sample_rate,
            runtime_sample_rate=sample_rate,
            mic_sensitivity=mic_sensitivity,
            golden_cache=golden_cache,
            allow_golden_runtime_resample=allow_golden_runtime_resample,
        )
        channel_payload = {
            "spl": _build_spl_chart(
                signal=calibrated,
                sample_rate=sample_rate,
                channel_config=channel_config,
                window_start_sec=window_start_sec,
            ),
        }
        fft_payload = _build_fft_chart(
            channel_index=channel_index,
            signal=calibrated,
            sample_rate=sample_rate,
            channel_config=channel_config,
            channel_result=channel_result,
            golden_cache=golden_cache,
            golden_bundle_loader=golden_bundle_loader,
        )
        if isinstance(fft_payload, dict):
            channel_payload["fft"] = fft_payload
        octave_payload = _build_octave_chart(
            channel_index=channel_index,
            signal=calibrated,
            sample_rate=sample_rate,
            channel_config=channel_config,
            channel_result=channel_result,
            golden_cache=golden_cache,
            golden_bundle_loader=golden_bundle_loader,
        )
        if isinstance(octave_payload, dict):
            channel_payload["octave"] = octave_payload
        channel_result = channel_results.get(channel_index)
        window_duration_sec = float(np.asarray(calibrated).size) / float(sample_rate)
        cached_loudness_trace = _valid_loudness_trace_cache_entry(
            loudness_trace_cache,
            channel_index,
            window_duration_sec=window_duration_sec,
        )
        if not isinstance(channel_result, dict) or channel_result.get("loudness_available") is True:
            try:
                channel_payload["loudness"] = _build_loudness_chart(
                    channel_index=channel_index,
                    signal=calibrated,
                    sample_rate=sample_rate,
                    channel_config=channel_config,
                    loudness_trace_cache=loudness_trace_cache,
                    context_signal=calibrated_full,
                    window_start_sec=window_start_sec,
                    cached_trace=cached_loudness_trace,
                )
            except Exception:
                pass
        score_payload = _build_stft_score_payload(
            channel_index=channel_index,
            signal=calibrated,
            sample_rate=sample_rate,
            channel_config=channel_config,
            window_start_sec=window_start_sec,
            loudness_context_signal=calibrated_full,
            loudness_trace=cached_loudness_trace,
        )
        if isinstance(score_payload, dict):
            channel_payload["stft_score"] = score_payload
        if channel_index == vibration_channel_index:
            try:
                vibration_payload = build_vibration_payload(
                    channel_index=channel_index,
                    signal=calibrated,
                    sample_rate=sample_rate,
                    calibration_value=mic_sensitivity,
                )
            except Exception as exc:
                _log_chart_warning(
                    "vibration analysis payload omitted",
                    channel_index=channel_index,
                    error=exc,
                )
                vibration_payload = None
            if isinstance(vibration_payload, dict):
                vibration_payload["times_sec"] = _offset_chart_times(
                    vibration_payload.get("times_sec"),
                    window_start_sec,
                )
                vibration_payload["frequency_times_sec"] = _offset_chart_times(
                    vibration_payload.get("frequency_times_sec"),
                    window_start_sec,
                )
                channel_payload["vibration"] = vibration_payload
        payload[channel_index] = channel_payload
    return payload


def _configured_vibration_view_channel(config: dict) -> int | None:
    value = config.get("vibration_view_channel_index")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    selected = {
        int(channel)
        for channel in config.get("selected_channels", [])
        if isinstance(channel, int) and not isinstance(channel, bool) and channel >= 0
    }
    return value if value in selected else None


def _selected_channels(config: dict, judgment_payload: dict) -> tuple[int, ...]:
    selected = tuple(
        channel_index
        for channel_index in config.get("selected_channels", [])
        if isinstance(channel_index, int) and not isinstance(channel_index, bool) and channel_index >= 0
    )
    if selected:
        return selected
    return tuple(sorted(_channel_result_by_index(judgment_payload)))


def _channel_config_by_index(config: dict) -> dict[int, dict]:
    return {
        channel["channel_index"]: channel
        for channel in config.get("channels", [])
        if isinstance(channel, dict)
        and isinstance(channel.get("channel_index"), int)
        and not isinstance(channel.get("channel_index"), bool)
    }


def _channel_result_by_index(judgment_payload: dict) -> dict[int, dict]:
    return {
        channel_result["channel_index"]: channel_result
        for channel_result in judgment_payload.get("channel_results", [])
        if isinstance(channel_result, dict)
        and isinstance(channel_result.get("channel_index"), int)
        and not isinstance(channel_result.get("channel_index"), bool)
    }


def _build_spl_row(
    *,
    selected_channels: tuple[int, ...],
    channel_configs: dict[int, dict],
    channel_results: dict[int, dict],
) -> tuple[dict, str | None]:
    candidates = []
    for channel_index in selected_channels:
        channel_config = _effective_channel_config(channel_configs, channel_index)
        lower, upper = _enabled_bounds(
            channel_config,
            lower_enabled_key="spl_lower_enabled",
            lower_value_key="spl_lower_limit",
            upper_enabled_key="spl_upper_enabled",
            upper_value_key="spl_upper_limit",
        )
        if not _has_constraint(lower, upper):
            continue
        channel_result = channel_results.get(channel_index)
        if not isinstance(channel_result, dict):
            return _build_constrained_no_result_row("spl", "SPL", channel_index=channel_index), None
        if channel_result.get("spl_trace_available") is not True:
            return (
                _build_constrained_no_result_row("spl", "SPL", channel_index=channel_index),
                _normalize_error_message(channel_result.get("spl_trace_error_message")),
            )
        if not isinstance(channel_result.get("spl_pass"), bool):
            return (
                _build_constrained_no_result_row("spl", "SPL", channel_index=channel_index),
                _normalize_error_message(channel_result.get("spl_trace_error_message")),
            )
        actual = _coerce_number(channel_result.get("spl_db"))
        if actual is None:
            return (
                _build_constrained_no_result_row("spl", "SPL", channel_index=channel_index),
                _normalize_error_message(channel_result.get("spl_trace_error_message")),
            )
        lower_actual = _coerce_measurement(channel_result.get("spl_min_db"))
        upper_actual = _coerce_measurement(channel_result.get("spl_max_db"))
        if lower_actual is None:
            lower_actual = actual
        if upper_actual is None:
            upper_actual = actual
        deviation = _compute_range_deviation(
            lower_actual=lower_actual,
            upper_actual=upper_actual,
            lower=lower,
            upper=upper,
        )
        candidates.append(
            {
                "deviation_value": deviation,
                "channel_index": channel_index,
                "passed": channel_result["spl_pass"],
            }
        )
    if not candidates:
        return _build_unconstrained_row("spl", "SPL"), None
    return (
        _build_pass_row_from_candidate(
            item_key="spl",
            label="SPL",
            candidate=_worst_candidate(candidates),
            unit="dB",
            constrained=True,
        ),
        None,
    )


def _build_spl_average_row(
    *,
    selected_channels: tuple[int, ...],
    channel_configs: dict[int, dict],
    channel_results: dict[int, dict],
) -> tuple[dict, str | None]:
    candidates = []
    for channel_index in selected_channels:
        channel_config = _effective_channel_config(channel_configs, channel_index)
        lower, upper = _enabled_bounds(
            channel_config,
            lower_enabled_key="spl_average_lower_enabled",
            lower_value_key="spl_average_lower_limit",
            upper_enabled_key="spl_average_upper_enabled",
            upper_value_key="spl_average_upper_limit",
        )
        if not _has_constraint(lower, upper):
            continue
        channel_result = channel_results.get(channel_index)
        if not isinstance(channel_result, dict):
            return _build_constrained_no_result_row("spl_average", "平均SPL", channel_index=channel_index), None
        if channel_result.get("spl_average_available") is not True:
            return (
                _build_constrained_no_result_row("spl_average", "平均SPL", channel_index=channel_index),
                _normalize_error_message(channel_result.get("spl_average_error_message")),
            )
        if not isinstance(channel_result.get("spl_average_pass"), bool):
            return (
                _build_constrained_no_result_row("spl_average", "平均SPL", channel_index=channel_index),
                _normalize_error_message(channel_result.get("spl_average_error_message")),
            )
        actual = _coerce_number(channel_result.get("spl_average_db"))
        deviation = _compute_deviation(actual=actual, lower=lower, upper=upper)
        if deviation is None:
            return (
                _build_constrained_no_result_row("spl_average", "平均SPL", channel_index=channel_index),
                _normalize_error_message(channel_result.get("spl_average_error_message")),
            )
        candidates.append(
            {
                "deviation_value": deviation,
                "channel_index": channel_index,
                "passed": channel_result["spl_average_pass"],
            }
        )
    if not candidates:
        return _build_unconstrained_row("spl_average", "平均SPL"), None
    return (
        _build_pass_row_from_candidate(
            item_key="spl_average",
            label="平均SPL",
            candidate=_worst_candidate(candidates),
            unit="dB",
            constrained=True,
        ),
        None,
    )


def _build_band_row(
    *,
    item_key: str,
    label: str,
    config_key: str,
    result_key: str,
    available_key: str,
    error_key: str,
    selected_channels: tuple[int, ...],
    channel_configs: dict[int, dict],
    channel_results: dict[int, dict],
) -> tuple[dict, str | None]:
    configured_any = False
    candidates = []
    for channel_index in selected_channels:
        channel_config = _effective_channel_config(channel_configs, channel_index)
        configured_bands = channel_config.get(config_key, [])
        if isinstance(configured_bands, list) and configured_bands:
            configured_any = True
        if not isinstance(configured_bands, list) or not configured_bands:
            continue
        channel_result = channel_results.get(channel_index)
        if not isinstance(channel_result, dict):
            return (
                _build_constrained_no_result_row(item_key, label),
                None,
            )
        if channel_result.get(available_key) is False:
            return (
                _build_constrained_no_result_row(item_key, label, channel_index=channel_index),
                _normalize_error_message(channel_result.get(error_key)),
            )
        if item_key == "fft":
            channel_candidates = _build_fft_envelope_candidates(
                band_results=channel_result.get(result_key),
                channel_index=channel_index,
            )
        else:
            channel_candidates = _build_configured_band_candidates(
                configured_bands=configured_bands,
                band_results=channel_result.get(result_key),
                channel_index=channel_index,
            )
        if channel_candidates is None:
            return (
                _build_constrained_no_result_row(item_key, label),
                _normalize_error_message(channel_result.get(error_key)),
            )
        candidates.extend(channel_candidates)
    if not configured_any:
        return _build_unconstrained_row(item_key, label), None
    if not candidates:
        if item_key == "fft":
            return (
                _build_row(
                    item_key=item_key,
                    label=label,
                    deviation_value=None,
                    channel_index=None,
                    status="OK",
                    constrained=True,
                    unit="dB",
                ),
                None,
            )
        return _build_constrained_no_result_row(item_key, label), None
    return (
        _build_row_from_candidate(
            item_key=item_key,
            label=label,
            candidate=_worst_candidate(candidates),
            unit="dB",
            constrained=True,
        ),
        None,
    )


def _build_configured_band_candidates(
    *,
    configured_bands,
    band_results,
    channel_index: int,
) -> list[dict] | None:
    if not isinstance(band_results, list):
        return None

    candidates = []
    for band_index, configured_band in enumerate(configured_bands):
        if not isinstance(configured_band, dict):
            return None
        if band_index >= len(band_results):
            return None
        band_result = band_results[band_index]
        if not isinstance(band_result, dict):
            return None
        actual = _coerce_number(band_result.get("level"))
        lower = _coerce_bound(band_result.get("lower"))
        upper = _coerce_bound(band_result.get("upper"))
        deviation = _compute_deviation(actual=actual, lower=lower, upper=upper)
        if deviation is None:
            return None
        candidates.append(
            {
                "deviation_value": deviation,
                "channel_index": channel_index,
            }
        )
    return candidates


def _build_fft_envelope_candidates(
    *,
    band_results,
    channel_index: int,
) -> list[dict] | None:
    if not isinstance(band_results, list):
        return None

    candidates = []
    for band_result in band_results:
        if not isinstance(band_result, dict):
            return None
        actual = _coerce_number(band_result.get("level"))
        lower = _coerce_bound(band_result.get("lower"))
        upper = _coerce_bound(band_result.get("upper"))
        deviation = _compute_deviation(actual=actual, lower=lower, upper=upper)
        if deviation is None:
            return None
        candidates.append(
            {
                "deviation_value": deviation,
                "channel_index": channel_index,
            }
        )
    return candidates


def _build_loudness_row(
    *,
    selected_channels: tuple[int, ...],
    channel_configs: dict[int, dict],
    channel_results: dict[int, dict],
) -> tuple[dict, str | None]:
    if not selected_channels:
        return _build_unconstrained_row("loudness", "Loudness"), None

    candidates = []
    for channel_index in selected_channels:
        channel_config = _effective_channel_config(channel_configs, channel_index)
        unit = _normalize_loudness_unit(channel_config.get("loudness_unit"))
        lower_limit, upper_limit = _enabled_bounds(
            channel_config,
            lower_enabled_key="loudness_lower_enabled",
            lower_value_key="loudness_lower_limit",
            upper_enabled_key="loudness_upper_enabled",
            upper_value_key="loudness_upper_limit",
        )
        if not _has_constraint(lower_limit, upper_limit):
            continue
        channel_result = channel_results.get(channel_index)
        if not isinstance(channel_result, dict):
            return _build_constrained_no_result_row("loudness", "Loudness", channel_index=channel_index), None

        if channel_result.get("loudness_available") is not True:
            return (
                _build_constrained_no_result_row("loudness", "Loudness", channel_index=channel_index),
                _normalize_error_message(channel_result.get("loudness_error_message")),
            )
        if not isinstance(channel_result.get("loudness_pass"), bool):
            return (
                _build_constrained_no_result_row("loudness", "Loudness", channel_index=channel_index),
                _normalize_error_message(channel_result.get("loudness_error_message")),
            )

        actual_max_sone = _coerce_number(channel_result.get("loudness_max_sone"))
        if actual_max_sone is None:
            return (
                _build_constrained_no_result_row("loudness", "Loudness", channel_index=channel_index),
                _normalize_error_message(channel_result.get("loudness_error_message")),
            )
        actual_min_sone = _coerce_number(channel_result.get("loudness_min_sone"))
        if actual_min_sone is None:
            actual_min_sone = actual_max_sone

        lower_sone, upper_sone = _loudness_limits_sone(lower_limit, upper_limit, unit)
        deviation_sone = _compute_range_deviation_raw(
            lower_actual=actual_min_sone,
            upper_actual=actual_max_sone,
            lower=lower_sone,
            upper=upper_sone,
        )
        min_display = _loudness_value_in_unit(actual_min_sone, unit)
        max_display = _loudness_value_in_unit(actual_max_sone, unit)
        display_deviation = _compute_range_deviation_raw(
            lower_actual=min_display,
            upper_actual=max_display,
            lower=lower_limit,
            upper=upper_limit,
        )
        if deviation_sone is None or display_deviation is None:
            return _build_constrained_no_result_row("loudness", "Loudness", channel_index=channel_index), None

        candidates.append(
            {
                "ranking_deviation_value": deviation_sone,
                "display_deviation_value": display_deviation,
                "channel_index": channel_index,
                "unit": unit,
                "passed": channel_result["loudness_pass"],
            }
        )

    if not candidates:
        return _build_unconstrained_row("loudness", "Loudness"), None

    candidate = _worst_loudness_candidate(candidates)
    return (
        _build_row(
            item_key="loudness",
            label="Loudness",
            deviation_value=_normalize_deviation(candidate["display_deviation_value"]),
            channel_index=candidate["channel_index"],
            status="OK" if candidate["passed"] else "NG",
            constrained=True,
            unit=candidate["unit"],
        ),
        None,
    )


def _build_loudness_average_row(
    *,
    selected_channels: tuple[int, ...],
    channel_configs: dict[int, dict],
    channel_results: dict[int, dict],
) -> tuple[dict, str | None]:
    if not selected_channels:
        return _build_unconstrained_row("loudness_average", "平均Loudness"), None

    candidates = []
    for channel_index in selected_channels:
        channel_config = _effective_channel_config(channel_configs, channel_index)
        unit = _normalize_loudness_unit(channel_config.get("loudness_unit"))
        lower_limit, upper_limit = _enabled_bounds(
            channel_config,
            lower_enabled_key="loudness_average_lower_enabled",
            lower_value_key="loudness_average_lower_limit",
            upper_enabled_key="loudness_average_upper_enabled",
            upper_value_key="loudness_average_upper_limit",
        )
        if not _has_constraint(lower_limit, upper_limit):
            continue
        channel_result = channel_results.get(channel_index)
        if not isinstance(channel_result, dict):
            return _build_constrained_no_result_row(
                "loudness_average",
                "平均Loudness",
                channel_index=channel_index,
            ), None
        if channel_result.get("loudness_available") is not True:
            return (
                _build_constrained_no_result_row(
                    "loudness_average",
                    "平均Loudness",
                    channel_index=channel_index,
                ),
                _normalize_error_message(channel_result.get("loudness_error_message")),
            )
        if not isinstance(channel_result.get("loudness_average_pass"), bool):
            return (
                _build_constrained_no_result_row(
                    "loudness_average",
                    "平均Loudness",
                    channel_index=channel_index,
                ),
                _normalize_error_message(channel_result.get("loudness_error_message")),
            )

        actual_sone = _coerce_number(channel_result.get("loudness_average_sone"))
        if actual_sone is None:
            return (
                _build_constrained_no_result_row(
                    "loudness_average",
                    "平均Loudness",
                    channel_index=channel_index,
                ),
                _normalize_error_message(channel_result.get("loudness_error_message")),
            )
        lower_sone, upper_sone = _loudness_limits_sone(lower_limit, upper_limit, unit)
        deviation_sone = _compute_deviation_raw(
            actual=actual_sone,
            lower=lower_sone,
            upper=upper_sone,
        )
        actual_display = _loudness_value_in_unit(actual_sone, unit)
        display_deviation = _compute_deviation_raw(
            actual=actual_display,
            lower=lower_limit,
            upper=upper_limit,
        )
        if deviation_sone is None or display_deviation is None:
            return _build_constrained_no_result_row(
                "loudness_average",
                "平均Loudness",
                channel_index=channel_index,
            ), None

        candidates.append(
            {
                "ranking_deviation_value": deviation_sone,
                "display_deviation_value": display_deviation,
                "channel_index": channel_index,
                "unit": unit,
                "passed": channel_result["loudness_average_pass"],
            }
        )

    if not candidates:
        return _build_unconstrained_row("loudness_average", "平均Loudness"), None

    candidate = _worst_loudness_candidate(candidates)
    return (
        _build_row(
            item_key="loudness_average",
            label="平均Loudness",
            deviation_value=_normalize_deviation(candidate["display_deviation_value"]),
            channel_index=candidate["channel_index"],
            status=_status_from_deviation(candidate["display_deviation_value"]),
            constrained=True,
            unit=candidate["unit"],
        ),
        None,
    )


def _build_ai_row(
    *,
    selected_channels: tuple[int, ...],
    ai_review: dict | None,
) -> tuple[dict, str | None]:
    if not isinstance(ai_review, dict):
        return _build_unconstrained_row("ai", "AI分析"), None

    status = str(ai_review.get("status") or "").strip().lower()
    threshold = _coerce_number(ai_review.get("threshold"))
    score = _coerce_number(ai_review.get("score"))
    channel_index = _normalize_ai_channel_index(
        ai_review.get("channel_index"),
        selected_channels=selected_channels,
    )

    if status in {"disabled", "not_enabled", "not_configured", "skipped"}:
        return _build_unconstrained_row("ai", "AI分析"), None
    if status == "" and threshold is None:
        return _build_unconstrained_row("ai", "AI分析"), None
    if status == "success" and score is not None and threshold is None:
        return (
            _build_constrained_no_result_row("ai", "AI分析", channel_index=channel_index),
            "AI decision threshold unavailable",
        )
    if status == "success" and score is not None and threshold is not None:
        deviation = _compute_deviation(actual=score, lower=threshold, upper=None)
        return (
            _build_row(
                item_key="ai",
                label="AI分析",
                deviation_value=deviation,
                channel_index=channel_index,
                status=_status_from_deviation(deviation),
                constrained=True,
                unit=None,
            ),
            None,
        )
    return (
        _build_row(
            item_key="ai",
            label="AI分析",
            deviation_value=None,
            channel_index=channel_index,
            status=_NO_RESULT_STATUS,
            constrained=True,
            unit=None,
        ),
        _normalize_error_message(ai_review.get("error_message")),
    )


def _build_row_from_candidate(
    *,
    item_key: str,
    label: str,
    candidate: dict,
    unit: str | None,
    constrained: bool,
) -> dict:
    return _build_row(
        item_key=item_key,
        label=label,
        deviation_value=candidate["deviation_value"],
        channel_index=candidate["channel_index"],
        status=_status_from_deviation(candidate["deviation_value"]),
        constrained=constrained,
        unit=unit,
    )


def _build_pass_row_from_candidate(
    *,
    item_key: str,
    label: str,
    candidate: dict,
    unit: str | None,
    constrained: bool,
) -> dict:
    return _build_row(
        item_key=item_key,
        label=label,
        deviation_value=candidate["deviation_value"],
        channel_index=candidate["channel_index"],
        status="OK" if candidate["passed"] else "NG",
        constrained=constrained,
        unit=unit,
    )


def _build_unconstrained_row(item_key: str, label: str) -> dict:
    return _build_row(
        item_key=item_key,
        label=label,
        deviation_value=None,
        channel_index=None,
        status="OK",
        constrained=False,
        unit=None,
    )


def _build_full_no_result_row(item_key: str, label: str) -> dict:
    return _build_no_result_row(item_key, label, constrained=False)


def _build_constrained_no_result_row(
    item_key: str,
    label: str,
    *,
    channel_index: int | None = None,
) -> dict:
    return _build_no_result_row(item_key, label, constrained=True, channel_index=channel_index)


def _build_no_result_row(
    item_key: str,
    label: str,
    *,
    constrained: bool,
    channel_index: int | None = None,
) -> dict:
    return _build_row(
        item_key=item_key,
        label=label,
        deviation_value=None,
        channel_index=channel_index,
        status=_NO_RESULT_STATUS,
        constrained=constrained,
        unit=None,
    )


def _build_row(
    *,
    item_key: str,
    label: str,
    deviation_value: float | None,
    channel_index: int | None,
    status: str,
    constrained: bool,
    unit: str | None,
) -> dict:
    return {
        "item_key": item_key,
        "label": label,
        "deviation_value": deviation_value,
        "deviation_display": _format_deviation(deviation_value, unit=unit),
        "channel_index": channel_index,
        "channel_label": (
            canonical_channel_label(channel_index) if channel_index is not None else "--"
        ),
        "status": status,
        "constrained": constrained,
    }


def _worst_candidate(candidates: list[dict]) -> dict:
    return max(candidates, key=lambda candidate: candidate["deviation_value"])


def _worst_loudness_candidate(candidates: list[dict]) -> dict:
    failing_candidates = [
        candidate for candidate in candidates
        if candidate["passed"] is False
    ]
    ranked_candidates = failing_candidates or candidates
    return max(ranked_candidates, key=lambda candidate: candidate["ranking_deviation_value"])


def _overall_status(rows: list[dict]) -> str:
    if any(row["status"] == _NO_RESULT_STATUS for row in rows):
        return "no_result"
    if any(row["status"] == "NG" for row in rows):
        return "fail"
    return "pass"


def _overall_label(overall_status: str) -> str:
    if overall_status == "pass":
        return "PASS"
    if overall_status == "fail":
        return "FAIL"
    return _NO_RESULT_STATUS


def _status_from_deviation(deviation_value: float | None) -> str:
    if deviation_value is None:
        return "OK"
    return "NG" if deviation_value > 0 else "OK"


def _compute_deviation(
    *,
    actual: float | None,
    lower: float | None,
    upper: float | None,
) -> float | None:
    deviation = _compute_range_deviation_raw(
        lower_actual=actual,
        upper_actual=actual,
        lower=lower,
        upper=upper,
    )
    if deviation is None:
        return None
    return _normalize_deviation(deviation)


def _compute_range_deviation(
    *,
    lower_actual: float | None,
    upper_actual: float | None,
    lower: float | None,
    upper: float | None,
) -> float | None:
    deviation = _compute_range_deviation_raw(
        lower_actual=lower_actual,
        upper_actual=upper_actual,
        lower=lower,
        upper=upper,
    )
    if deviation is None:
        return None
    return _normalize_deviation(deviation)


def _compute_range_deviation_raw(
    *,
    lower_actual: float | None,
    upper_actual: float | None,
    lower: float | None,
    upper: float | None,
) -> float | None:
    lower_deviation = _compute_deviation_raw(
        actual=lower_actual,
        lower=lower,
        upper=None,
    )
    upper_deviation = _compute_deviation_raw(
        actual=upper_actual,
        lower=None,
        upper=upper,
    )
    deviations = [
        deviation
        for deviation in (lower_deviation, upper_deviation)
        if deviation is not None
    ]
    if not deviations:
        return None
    return max(deviations)


def _compute_deviation_raw(
    *,
    actual: float | None,
    lower: float | None,
    upper: float | None,
) -> float | None:
    if actual is None:
        return None
    if _is_finite(lower) and _is_finite(upper):
        if actual < lower:
            return lower - actual
        if actual > upper:
            return actual - upper
        return -min(actual - lower, upper - actual)
    if _is_finite(lower):
        if actual < lower:
            return lower - actual
        return -(actual - lower)
    if _is_finite(upper):
        if actual > upper:
            return actual - upper
        return -(upper - actual)
    return None


def _format_deviation(value: float | None, *, unit: str | None) -> str:
    if value is None:
        return "--"
    if math.isinf(value):
        formatted = "∞" if value > 0 else "-∞"
    else:
        formatted = f"{value:g}"
    if unit:
        return f"{formatted} {unit}"
    return formatted


def _normalize_deviation(value: float) -> float:
    normalized = round(float(value), 6)
    if abs(normalized) < 1e-9:
        return 0.0
    return normalized


def _normalize_ai_channel_index(
    raw_channel_index,
    *,
    selected_channels: tuple[int, ...],
) -> int | None:
    if not isinstance(raw_channel_index, int) or isinstance(raw_channel_index, bool):
        return None
    if raw_channel_index not in selected_channels:
        return None
    return raw_channel_index


def _coerce_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        if math.isfinite(value):
            return float(value)
        return None
    return None


def _coerce_measurement(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        numeric = float(value)
        if not math.isnan(numeric):
            return numeric
    return None


def _coerce_bound(value) -> float | None:
    coerced = _coerce_number(value)
    if coerced is None:
        return None
    return coerced


def _enabled_bounds(
    channel_config: dict,
    *,
    lower_enabled_key: str,
    lower_value_key: str,
    upper_enabled_key: str,
    upper_value_key: str,
) -> tuple[float | None, float | None]:
    lower = _coerce_bound(channel_config.get(lower_value_key)) if channel_config.get(lower_enabled_key) is True else None
    upper = _coerce_bound(channel_config.get(upper_value_key)) if channel_config.get(upper_enabled_key) is True else None
    return lower, upper


def _has_constraint(lower: float | None, upper: float | None) -> bool:
    return _is_finite(lower) or _is_finite(upper)


def _is_finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _normalize_error_message(*messages) -> str:
    for message in messages:
        if isinstance(message, str):
            normalized = message.strip()
            if normalized:
                return normalized
    return _NO_RESULT_FALLBACK_MESSAGE


def _slice_analysis_window(
    buffer: np.ndarray,
    sample_rate: float,
    analysis_window: tuple[float, float] | list[float] | None,
) -> np.ndarray:
    window, _start_sec = _slice_analysis_window_with_bounds(
        buffer,
        sample_rate,
        analysis_window,
    )
    return window


def _slice_analysis_window_with_bounds(
    buffer: np.ndarray,
    sample_rate: float,
    analysis_window: tuple[float, float] | list[float] | None,
) -> tuple[np.ndarray, float]:
    total_samples = int(buffer.shape[0])
    if (
        not isinstance(analysis_window, (list, tuple))
        or len(analysis_window) != 2
    ):
        return buffer, 0.0
    try:
        start_sec = float(analysis_window[0])
        end_sec = float(analysis_window[1])
    except (TypeError, ValueError):
        return buffer, 0.0
    if not math.isfinite(start_sec) or not math.isfinite(end_sec):
        return buffer, 0.0
    total_sec = float(total_samples) / float(sample_rate) if sample_rate > 0 else 0.0
    start_sec = max(0.0, min(total_sec, start_sec))
    end_sec = max(start_sec, min(total_sec, end_sec))
    start_idx = max(0, min(total_samples, int(start_sec * sample_rate)))
    end_idx = max(start_idx, min(total_samples, int(end_sec * sample_rate)))
    return buffer[start_idx:end_idx], start_sec


def _selected_chart_channels(config: dict, buffer: np.ndarray) -> tuple[int, ...]:
    selected = tuple(
        channel_index
        for channel_index in config.get("selected_channels", [])
        if isinstance(channel_index, int)
        and not isinstance(channel_index, bool)
        and channel_index >= 0
    )
    if selected:
        return selected
    if buffer.ndim == 1:
        return (0,)
    return tuple(range(int(buffer.shape[1])))


def _runtime_channel_config(config: dict, channel_index: int) -> dict:
    return _effective_channel_config(_channel_config_by_index(config), channel_index)


def _effective_channel_config(channel_configs: dict[int, dict], channel_index: int) -> dict:
    channel_config = _runtime_channel_defaults(channel_index)
    if isinstance(channel_configs.get(channel_index), dict):
        channel_config.update(channel_configs[channel_index])
    return channel_config


def _extract_channel(
    buffer: np.ndarray,
    channel_index: int,
    *,
    channel_column_map: dict[int, int] | None = None,
) -> np.ndarray | None:
    column_index = _buffer_column_for_channel(channel_index, channel_column_map)
    if buffer.ndim == 1:
        return np.asarray(buffer, dtype=np.float64)
    if column_index < 0 or column_index >= buffer.shape[1]:
        return None
    return np.asarray(buffer[:, column_index], dtype=np.float64)


def _buffer_column_for_channel(
    channel_index: int,
    channel_column_map: dict[int, int] | None,
) -> int:
    if isinstance(channel_column_map, dict) and channel_index in channel_column_map:
        try:
            return int(channel_column_map[channel_index])
        except (TypeError, ValueError):
            return channel_index
    return channel_index


def _mic_sensitivity(mic_cal: dict, channel_index: int) -> float:
    if not isinstance(mic_cal, dict):
        return 0.0
    entry = mic_cal.get(f"In{channel_index + 1}", {})
    raw = entry.get("deviation_value", "0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _build_golden_bundle_loader(
    *,
    channel_index: int,
    channel_config: dict,
    configured_input_sample_rate,
    runtime_sample_rate: float,
    mic_sensitivity: float,
    golden_cache: GoldenReferenceCache,
    allow_golden_runtime_resample: bool = False,
):
    state: dict[str, object] = {"loaded": False, "bundle": None, "error": None}

    def _load():
        if state["loaded"] is not True:
            try:
                state["bundle"] = golden_cache.bundle_for_channel(
                    channel_index=channel_index,
                    channel_config=channel_config,
                    configured_input_sample_rate=configured_input_sample_rate,
                    runtime_sample_rate=runtime_sample_rate,
                    mic_sensitivity=mic_sensitivity,
                    allow_runtime_resample=allow_golden_runtime_resample,
                )
            except Exception as exc:  # pragma: no cover - exercised through callers
                state["error"] = exc
            state["loaded"] = True
        if state["error"] is not None:
            raise state["error"]  # type: ignore[misc]
        return state["bundle"]

    return _load


def _build_spl_chart(
    *,
    signal: np.ndarray,
    sample_rate: float,
    channel_config: dict,
    window_start_sec: float = 0.0,
) -> dict:
    weighting = str(channel_config.get("spl_weighting", "Z") or "Z")
    try:
        time_constant = float(channel_config.get("spl_time_constant", 0.125) or 0.125)
    except (TypeError, ValueError):
        time_constant = 0.125

    raw_times_sec, raw_values_db = dsp.compute_spl_trace(
        signal,
        sample_rate,
        weighting,
        time_constant,
        chunk_duration=_SPL_CHUNK_DURATION_SEC,
    )
    times_sec = []
    values_db = []
    for end_time, value in zip(raw_times_sec, raw_values_db):
        normalized_time = _normalize_chart_number(float(end_time) + float(window_start_sec))
        normalized_value = _normalize_chart_number(value)
        if normalized_time is None or normalized_value is None:
            continue
        times_sec.append(normalized_time)
        values_db.append(normalized_value)

    return {
        "times_sec": times_sec,
        "values_db": values_db,
        "lower_limit_db": (
            _normalize_chart_number(channel_config.get("spl_lower_limit"))
            if channel_config.get("spl_lower_enabled") is True
            else None
        ),
        "upper_limit_db": (
            _normalize_chart_number(channel_config.get("spl_upper_limit"))
            if channel_config.get("spl_upper_enabled") is True
            else None
        ),
    }


def _build_loudness_chart(
    *,
    channel_index: int,
    signal: np.ndarray,
    sample_rate: float,
    channel_config: dict,
    loudness_trace_cache: dict[int, dict] | None,
    context_signal: np.ndarray | None = None,
    window_start_sec: float = 0.0,
    cached_trace: dict | None = None,
) -> dict:
    window_duration_sec = float(np.asarray(signal).size) / float(sample_rate)
    if cached_trace is None:
        cached_trace = _valid_loudness_trace_cache_entry(
            loudness_trace_cache,
            channel_index,
            window_duration_sec=window_duration_sec,
        )
    if cached_trace is None:
        if context_signal is not None and window_start_sec > 0.0:
            times_sec, values_sone, _max_sone = dsp.compute_loudness_trace_for_window(
                context_signal,
                sample_rate,
                window_start_sec,
                window_start_sec + window_duration_sec,
            )
        else:
            times_sec, values_sone, _max_sone = dsp.compute_loudness_trace(signal, sample_rate)
    else:
        times_sec = cached_trace["times_sec"]
        values_sone = cached_trace["values_sone"]
    display_times_sec, display_values_sone = dsp.downsample_loudness_trace_for_display(
        times_sec,
        values_sone,
    )
    unit = _normalize_loudness_unit(channel_config.get("loudness_unit"))
    values = np.asarray(
        [_loudness_value_in_unit(value_sone, unit) for value_sone in display_values_sone],
        dtype=np.float64,
    )
    normalized_times, normalized_values = _normalize_chart_pairs(display_times_sec, values)
    if normalized_times.size <= 0 or normalized_values.size <= 0:
        raise RuntimeError("loudness chart payload is empty")

    return {
        "times_sec": _offset_chart_times(normalized_times, window_start_sec),
        "values": _normalize_chart_list(normalized_values),
        "unit": unit,
        "lower_limit": (
            _normalize_chart_number(channel_config.get("loudness_lower_limit"))
            if channel_config.get("loudness_lower_enabled") is True
            else None
        ),
        "upper_limit": (
            _normalize_chart_number(channel_config.get("loudness_upper_limit"))
            if channel_config.get("loudness_upper_enabled") is True
            else None
        ),
    }


def _build_stft_score_payload(
    *,
    channel_index: int,
    signal: np.ndarray,
    sample_rate: float,
    channel_config: dict,
    window_start_sec: float,
    loudness_context_signal: np.ndarray | None = None,
    loudness_trace: dict | None = None,
) -> dict | None:
    try:
        defaults = _runtime_channel_defaults(channel_index)
        score_payload = stft_score.compute_stft_score_payload(
            signal=signal,
            sample_rate=sample_rate,
            stft_nfft=channel_config.get("stft_nfft", defaults.get("stft_nfft")),
            stft_overlap=channel_config.get("stft_overlap", defaults.get("stft_overlap")),
            score_config=stft_score.normalize_score_config(channel_config),
            absolute_start_sec=window_start_sec,
            loudness_context_signal=loudness_context_signal,
            loudness_trace=loudness_trace,
        )
    except Exception as exc:
        _log_chart_warning(
            "STFT score payload omitted",
            channel_index=channel_index,
            error=exc,
        )
        return None
    return score_payload if isinstance(score_payload, dict) else None


def _valid_loudness_trace_cache_entry(
    loudness_trace_cache: dict[int, dict] | None,
    channel_index: int,
    *,
    window_duration_sec: float | None = None,
) -> dict | None:
    if not isinstance(loudness_trace_cache, dict):
        return None
    entry = loudness_trace_cache.get(channel_index)
    if not isinstance(entry, dict):
        return None
    if entry.get("algorithm") != dsp.LOUDNESS_TRACE_ALGORITHM:
        return None
    try:
        internal_rate_hz = float(entry.get("internal_rate_hz"))
    except (TypeError, ValueError):
        return None
    if not np.isfinite(internal_rate_hz):
        return None
    if not np.isclose(
        internal_rate_hz,
        dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
        rtol=0.0,
        atol=1e-9,
    ):
        return None

    try:
        times = np.asarray(entry.get("times_sec"), dtype=np.float64).reshape(-1)
        values = np.asarray(entry.get("values_sone"), dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
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
            1.0 / dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
            rtol=0.0,
            atol=1e-9,
        ):
            return None

    if window_duration_sec is not None:
        try:
            duration = float(window_duration_sec)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(duration) or duration <= 0.0:
            return None
        coverage_tolerance = 1.0 / internal_rate_hz
        if float(times[-1]) + coverage_tolerance < duration:
            return None

    try:
        max_sone = float(entry.get("max_sone"))
    except (TypeError, ValueError):
        max_sone = float(np.max(values))
    if not np.isfinite(max_sone) or max_sone < 0.0:
        max_sone = float(np.max(values))

    return {
        "times_sec": times.tolist(),
        "values_sone": values.tolist(),
        "max_sone": max_sone,
        "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
        "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
    }


def _build_fft_chart(
    *,
    channel_index: int,
    signal: np.ndarray,
    sample_rate: float,
    channel_config: dict,
    channel_result: dict | None,
    golden_cache: GoldenReferenceCache,
    golden_bundle_loader,
) -> dict | None:
    mode = _golden_mode(channel_config.get("fft_golden_mode"))
    overlay_requested = bool(channel_config.get("fft_show_golden_overlay")) and mode != "delta"
    nfft = channel_config.get(
        "fft_nfft",
        _runtime_channel_defaults(channel_index).get("fft_nfft"),
    )
    if isinstance(channel_result, dict) and channel_result.get("fft_available") is False:
        _log_chart_warning(
            "FFT chart payload omitted",
            channel_index=channel_index,
            error=channel_result.get("fft_error_message"),
        )
        return None

    try:
        frequencies_hz, measured_levels_db = _measure_fft_levels(
            signal,
            sample_rate,
            nfft=nfft,
        )
    except Exception as exc:
        _log_chart_warning("FFT chart payload omitted", channel_index=channel_index, error=exc)
        return None

    configured_fft_bands = channel_config.get("fft_band_limits", [])
    x_axis_scale = _fft_x_axis_scale(channel_config.get("fft_x_axis_scale"))
    if mode == "off":
        chart = {
            "frequencies_hz": _normalize_chart_list(frequencies_hz),
            "levels_db": _normalize_chart_list(measured_levels_db),
            "golden_mode": "off",
            "x_axis_scale": x_axis_scale,
            "limit_bands": _limit_bands_for_chart(
                channel_result=channel_result,
                result_key="fft_band_results",
                frequencies=frequencies_hz,
                levels_db=measured_levels_db,
                configured_bands=configured_fft_bands,
                mode=mode,
            ),
            "limit_envelope": _fft_limit_envelope_for_chart(
                configured_bands=configured_fft_bands,
                channel_result=channel_result,
                frequencies=frequencies_hz,
                mode=mode,
            ),
        }
        if overlay_requested:
            try:
                golden_bundle = golden_bundle_loader()
                golden_freqs_hz, golden_levels_db = golden_cache.ensure_fft(
                    golden_bundle,
                    nfft=nfft,
                )
                _validate_matching_axis(frequencies_hz, golden_freqs_hz, "FFT")
            except Exception as exc:
                logger.warning("黄金样本加载失败，FFT 对比曲线已忽略: %s", exc)
            else:
                chart["golden_overlay_levels_db"] = _normalize_chart_list(golden_levels_db)
        return chart

    try:
        golden_bundle = golden_bundle_loader()
        golden_freqs_hz, golden_levels_db = golden_cache.ensure_fft(
            golden_bundle,
            nfft=nfft,
        )
        _validate_matching_axis(frequencies_hz, golden_freqs_hz, "FFT")
    except Exception as exc:
        _log_chart_warning("FFT chart payload omitted", channel_index=channel_index, error=exc)
        return None

    plotted_levels_db = (
        measured_levels_db if mode == "absolute" else measured_levels_db - golden_levels_db
    )
    chart = {
        "frequencies_hz": _normalize_chart_list(frequencies_hz),
        "levels_db": _normalize_chart_list(plotted_levels_db),
        "golden_mode": mode,
        "x_axis_scale": x_axis_scale,
        "limit_bands": _limit_bands_for_chart(
            channel_result=channel_result,
            result_key="fft_band_results",
            frequencies=frequencies_hz,
            levels_db=measured_levels_db,
            configured_bands=configured_fft_bands,
            golden_levels_db=golden_levels_db,
            mode=mode,
        ),
        "limit_envelope": _fft_limit_envelope_for_chart(
            configured_bands=configured_fft_bands,
            channel_result=channel_result,
            frequencies=frequencies_hz,
            golden_levels_db=golden_levels_db,
            mode=mode,
        ),
    }
    if mode == "delta":
        chart["show_zero_reference"] = True
    elif overlay_requested:
        chart["golden_overlay_levels_db"] = _normalize_chart_list(golden_levels_db)
    return chart


def _build_octave_chart(
    *,
    channel_index: int,
    signal: np.ndarray,
    sample_rate: float,
    channel_config: dict,
    channel_result: dict | None,
    golden_cache: GoldenReferenceCache,
    golden_bundle_loader,
) -> dict | None:
    weighting = str(channel_config.get("oct_weighting", "Z") or "Z")
    mode = _golden_mode(channel_config.get("oct_golden_mode"))
    overlay_requested = bool(channel_config.get("oct_show_golden_overlay")) and mode != "delta"
    if mode != "off" and isinstance(channel_result, dict) and channel_result.get("octave_available") is False:
        _log_chart_warning(
            "octave chart payload omitted",
            channel_index=channel_index,
            error=channel_result.get("octave_error_message"),
        )
        return None

    try:
        center_freqs_hz, measured_levels_db = _measure_octave_levels(signal, sample_rate, weighting)
    except Exception:
        if mode != "off":
            logger.warning(
                "octave chart payload omitted for CH%s: measured recompute failed",
                channel_index + 1,
                exc_info=True,
            )
            return None
        raise

    if mode == "off":
        chart_center_freqs_hz, chart_levels_db = _normalize_chart_pairs(
            center_freqs_hz,
            measured_levels_db,
        )
        chart = {
            "center_freqs_hz": _normalize_chart_list(chart_center_freqs_hz),
            "levels_db": _normalize_chart_list(chart_levels_db),
            "golden_mode": "off",
            "limit_bands": _limit_bands_for_chart(
                channel_result=channel_result,
                result_key="oct_band_results",
                frequencies=center_freqs_hz,
                levels_db=measured_levels_db,
                configured_bands=channel_config.get("oct_band_limits", []),
                require_exact_frequency=True,
                mode=mode,
            ),
        }
        if overlay_requested:
            try:
                golden_bundle = golden_bundle_loader()
                golden_center_freqs_hz, golden_levels_db = golden_cache.ensure_octave(
                    golden_bundle,
                    weighting=weighting,
                )
                _validate_matching_axis(center_freqs_hz, golden_center_freqs_hz, "倍频程")
            except Exception as exc:
                logger.warning("黄金样本加载失败，倍频程对比曲线已忽略: %s", exc)
            else:
                chart["golden_overlay_levels_db"] = _normalize_chart_list(golden_levels_db)
        return chart

    try:
        golden_bundle = golden_bundle_loader()
        golden_center_freqs_hz, golden_levels_db = golden_cache.ensure_octave(
            golden_bundle,
            weighting=weighting,
        )
        _validate_matching_axis(center_freqs_hz, golden_center_freqs_hz, "倍频程")
    except Exception as exc:
        _log_chart_warning("octave chart payload omitted", channel_index=channel_index, error=exc)
        return None

    plotted_levels_db = (
        measured_levels_db if mode == "absolute" else measured_levels_db - golden_levels_db
    )
    chart_center_freqs_hz, chart_levels_db = _normalize_chart_pairs(
        center_freqs_hz,
        plotted_levels_db,
    )
    chart = {
        "center_freqs_hz": _normalize_chart_list(chart_center_freqs_hz),
        "levels_db": _normalize_chart_list(chart_levels_db),
        "golden_mode": mode,
        "limit_bands": _limit_bands_for_chart(
            channel_result=channel_result,
            result_key="oct_band_results",
            frequencies=center_freqs_hz,
            levels_db=measured_levels_db,
            configured_bands=channel_config.get("oct_band_limits", []),
            golden_levels_db=golden_levels_db,
            require_exact_frequency=True,
            mode=mode,
        ),
    }
    if mode == "delta":
        chart["show_zero_reference"] = True
    elif overlay_requested:
        chart["golden_overlay_levels_db"] = _normalize_chart_list(golden_levels_db)
    return chart


def _measure_fft_levels(
    signal: np.ndarray,
    sample_rate: float,
    *,
    nfft: int,
) -> tuple[np.ndarray, np.ndarray]:
    frequencies_hz, magnitudes_db = dsp.compute_fft_spectrum(signal, sample_rate, nfft)
    return np.asarray(frequencies_hz, dtype=np.float64), _absolute_levels_db(magnitudes_db)


def _measure_octave_levels(
    signal: np.ndarray,
    sample_rate: float,
    weighting: str,
) -> tuple[np.ndarray, np.ndarray]:
    center_freqs_hz, levels_db = dsp.compute_octave_bands(signal, sample_rate, weighting)
    return np.asarray(center_freqs_hz, dtype=np.float64), _absolute_levels_db(levels_db)


def _absolute_levels_db(levels_db: np.ndarray | list[float]) -> np.ndarray:
    levels = np.asarray(levels_db, dtype=np.float64)
    return levels - 20.0 * math.log10(_REF_PRESSURE)


def _golden_mode(value) -> str:
    mode = str(value or "off").strip().lower()
    if mode in {"absolute", "delta"}:
        return mode
    return "off"


def _fft_x_axis_scale(value) -> str:
    return "log" if str(value or "").strip().lower() == "log" else "linear"


def _validate_matching_axis(
    measured_axis: np.ndarray,
    golden_axis: np.ndarray,
    label: str,
) -> None:
    if measured_axis.shape != golden_axis.shape:
        raise RuntimeError(f"黄金样本{label}频轴不匹配")
    if not np.allclose(measured_axis, golden_axis):
        raise RuntimeError(f"黄金样本{label}频轴不匹配")


def _log_chart_warning(message: str, *, channel_index: int, error) -> None:
    logger.warning("%s for CH%s: %s", message, channel_index + 1, error)


def _fft_limit_envelope_for_chart(
    *,
    configured_bands,
    channel_result,
    frequencies,
    golden_levels_db=None,
    mode: str = "off",
) -> dict:
    envelope = {
        "control_points": _sorted_fft_control_points_in_chart_space(
            configured_bands,
            frequencies=frequencies,
            golden_levels_db=golden_levels_db,
            mode=mode,
        ),
        "failed_points": _failed_fft_points_from_results(
            channel_result.get("fft_band_results")
            if isinstance(channel_result, dict)
            else None
        ),
    }
    line_points = _fft_limit_line_points_in_chart_space(
        configured_bands,
        frequencies=frequencies,
        golden_levels_db=golden_levels_db,
        mode=mode,
    )
    if line_points:
        envelope["line_points"] = line_points
    return envelope


def _sorted_fft_control_points_in_chart_space(
    configured_bands,
    *,
    frequencies,
    golden_levels_db=None,
    mode: str = "off",
) -> list[dict]:
    control_points: list[tuple[float, int, dict]] = []
    for source_index, band in enumerate(configured_bands or []):
        if not isinstance(band, dict):
            continue
        freq_hz = _normalize_chart_number(band.get("freq"))
        lower_db = _coerce_bound(band.get("lower"))
        upper_db = _coerce_bound(band.get("upper"))
        if freq_hz is None or lower_db is None or upper_db is None:
            continue
        if mode == "absolute":
            golden_level = _interpolated_golden_level_at_frequency(
                freq_hz,
                frequencies=frequencies,
                golden_levels_db=golden_levels_db,
            )
            if golden_level is None:
                continue
            lower_db += golden_level
            upper_db += golden_level
        lower_db = _normalize_chart_number(lower_db)
        upper_db = _normalize_chart_number(upper_db)
        if lower_db is None or upper_db is None:
            continue
        control_points.append(
            (
                freq_hz,
                source_index,
                {
                    "freq_hz": freq_hz,
                    "lower_db": lower_db,
                    "upper_db": upper_db,
                },
            )
        )

    control_points.sort(key=lambda item: (item[0], item[1]))
    return [point for _, _, point in control_points]


def _interpolated_golden_level_at_frequency(
    freq_hz: float,
    *,
    frequencies,
    golden_levels_db,
) -> float | None:
    if golden_levels_db is None:
        return None
    try:
        frequency_axis = np.asarray(frequencies, dtype=np.float64).reshape(-1)
        golden_axis = np.asarray(golden_levels_db, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return None

    length = min(int(frequency_axis.size), int(golden_axis.size))
    if length <= 0:
        return None
    frequency_axis = frequency_axis[:length]
    golden_axis = golden_axis[:length]
    finite_mask = np.isfinite(frequency_axis) & np.isfinite(golden_axis)
    if not np.any(finite_mask):
        return None
    frequency_axis = frequency_axis[finite_mask]
    golden_axis = golden_axis[finite_mask]
    order = np.argsort(frequency_axis, kind="stable")
    return float(np.interp(freq_hz, frequency_axis[order], golden_axis[order]))


def _fft_limit_line_points_in_chart_space(
    configured_bands,
    *,
    frequencies,
    golden_levels_db=None,
    mode: str = "off",
) -> list[dict]:
    points = _normalized_fft_limit_offset_points(configured_bands)
    env_freqs, env_lower, env_upper = _collapse_fft_limit_offsets(points)
    if env_freqs.size < 2 or np.any(env_lower >= env_upper):
        return []

    if mode != "absolute":
        return [
            point
            for freq_hz, lower_db, upper_db in zip(
                env_freqs,
                env_lower,
                env_upper,
                strict=False,
            )
            if (
                point := _chart_limit_envelope_point(
                    freq_hz=freq_hz,
                    lower_db=lower_db,
                    upper_db=upper_db,
                )
            )
            is not None
        ]

    if golden_levels_db is None:
        return []

    try:
        frequency_axis = np.asarray(frequencies, dtype=np.float64).reshape(-1)
        golden_axis = np.asarray(golden_levels_db, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        return []

    length = min(int(frequency_axis.size), int(golden_axis.size))
    if length <= 0:
        return []
    frequency_axis = frequency_axis[:length]
    golden_axis = golden_axis[:length]
    finite_mask = np.isfinite(frequency_axis) & np.isfinite(golden_axis)
    if not np.any(finite_mask):
        return []
    frequency_axis = frequency_axis[finite_mask]
    golden_axis = golden_axis[finite_mask]
    order = np.argsort(frequency_axis, kind="stable")
    frequency_axis = frequency_axis[order]
    golden_axis = golden_axis[order]

    interior_mask = (frequency_axis > env_freqs[0]) & (frequency_axis < env_freqs[-1])
    eval_freqs = np.unique(np.concatenate((env_freqs, frequency_axis[interior_mask])))
    if eval_freqs.size == 0:
        return []
    eval_golden = np.interp(eval_freqs, frequency_axis, golden_axis)
    lower = eval_golden + np.interp(eval_freqs, env_freqs, env_lower)
    upper = eval_golden + np.interp(eval_freqs, env_freqs, env_upper)

    line_points: list[dict] = []
    for freq_hz, lower_db, upper_db in zip(
        eval_freqs,
        lower,
        upper,
        strict=False,
    ):
        rendered_point = _chart_limit_envelope_point(
            freq_hz=freq_hz,
            lower_db=lower_db,
            upper_db=upper_db,
        )
        if rendered_point is not None:
            line_points.append(rendered_point)
    return line_points


def _chart_limit_envelope_point(
    *,
    freq_hz,
    lower_db,
    upper_db,
) -> dict | None:
    freq_value = _normalize_chart_number(freq_hz)
    lower_value = _normalize_chart_number(lower_db)
    upper_value = _normalize_chart_number(upper_db)
    if freq_value is None or lower_value is None or upper_value is None:
        return None
    return {
        "freq_hz": freq_value,
        "lower_db": lower_value,
        "upper_db": upper_value,
    }


def _normalized_fft_limit_offset_points(configured_bands) -> list[dict]:
    points: list[tuple[float, int, dict]] = []
    for source_index, band in enumerate(configured_bands or []):
        if not isinstance(band, dict):
            continue
        freq_hz = _coerce_number(band.get("freq"))
        lower_db = _coerce_bound(band.get("lower"))
        upper_db = _coerce_bound(band.get("upper"))
        if (
            freq_hz is None
            or freq_hz <= 0.0
            or lower_db is None
            or upper_db is None
        ):
            continue
        points.append(
            (
                freq_hz,
                source_index,
                {
                    "freq": freq_hz,
                    "lower": lower_db,
                    "upper": upper_db,
                },
            )
        )
    points.sort(key=lambda item: (item[0], item[1]))
    return [point for _freq, _source_index, point in points]


def _collapse_fft_limit_offsets(
    points: list[dict],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    distinct_freqs: list[float] = []
    lower_values: list[float] = []
    upper_values: list[float] = []
    index = 0
    while index < len(points):
        freq = float(points[index]["freq"])
        lowers: list[float] = []
        uppers: list[float] = []
        while index < len(points) and float(points[index]["freq"]) == freq:
            lowers.append(float(points[index]["lower"]))
            uppers.append(float(points[index]["upper"]))
            index += 1
        distinct_freqs.append(freq)
        lower_values.append(max(lowers))
        upper_values.append(min(uppers))
    return (
        np.asarray(distinct_freqs, dtype=np.float64),
        np.asarray(lower_values, dtype=np.float64),
        np.asarray(upper_values, dtype=np.float64),
    )


def _failed_fft_points_from_results(band_results) -> list[dict]:
    failed_points: list[dict] = []
    if not isinstance(band_results, list):
        return failed_points
    for result in band_results:
        if not isinstance(result, dict):
            continue
        freq_hz = _normalize_chart_number(result.get("freq"))
        actual_db = _normalize_chart_number(result.get("level"))
        lower_db = _normalize_chart_number(result.get("lower"))
        upper_db = _normalize_chart_number(result.get("upper"))
        passed = result.get("passed")
        is_failed = passed is False or (
            isinstance(actual_db, (int, float))
            and not _passes_limit(float(actual_db), lower_db, upper_db)
        )
        if (
            not is_failed
            or freq_hz is None
            or actual_db is None
            or lower_db is None
            or upper_db is None
        ):
            continue
        failed_points.append(
            {
                "freq_hz": freq_hz,
                "actual_db": actual_db,
                "lower_db": lower_db,
                "upper_db": upper_db,
            }
        )
    return failed_points


def _limit_bands_for_chart(
    *,
    channel_result: dict | None,
    result_key: str,
    frequencies: np.ndarray | list[float],
    levels_db: np.ndarray | list[float],
    configured_bands,
    golden_levels_db: np.ndarray | list[float] | None = None,
    require_exact_frequency: bool = False,
    mode: str = "off",
) -> list[dict]:
    serialized = _build_limit_band_payloads_from_serialized_results(
        channel_result.get(result_key) if isinstance(channel_result, dict) else None
    )
    if serialized is not None:
        return serialized
    return _build_limit_band_payloads(
        frequencies=frequencies,
        levels_db=levels_db,
        configured_bands=configured_bands,
        golden_levels_db=golden_levels_db,
        require_exact_frequency=require_exact_frequency,
        mode=mode,
    )


def _build_limit_band_payloads_from_serialized_results(band_results) -> list[dict] | None:
    if not isinstance(band_results, list):
        return None

    payload: list[dict] = []
    for band_result in band_results:
        if not isinstance(band_result, dict):
            continue
        freq_hz = _normalize_chart_number(band_result.get("freq"))
        actual_db = _normalize_chart_number(band_result.get("level"))
        lower_db = _normalize_chart_number(band_result.get("lower"))
        upper_db = _normalize_chart_number(band_result.get("upper"))
        if freq_hz is None or actual_db is None:
            continue
        passed = band_result.get("passed")
        payload.append(
            {
                "freq_hz": freq_hz,
                "lower_db": lower_db,
                "upper_db": upper_db,
                "actual_db": actual_db,
                "passed": passed if isinstance(passed, bool) else _passes_limit(actual_db, lower_db, upper_db),
            }
        )
    return payload


def _build_limit_band_payloads(
    *,
    frequencies: np.ndarray | list[float],
    levels_db: np.ndarray | list[float],
    configured_bands,
    golden_levels_db: np.ndarray | list[float] | None = None,
    require_exact_frequency: bool = False,
    mode: str = "off",
) -> list[dict]:
    frequency_axis = np.asarray(frequencies, dtype=np.float64)
    level_axis = np.asarray(levels_db, dtype=np.float64)
    golden_axis = (
        np.asarray(golden_levels_db, dtype=np.float64)
        if golden_levels_db is not None
        else None
    )
    if frequency_axis.size == 0 or level_axis.size == 0:
        return []

    payload: list[dict] = []
    for band in configured_bands or []:
        if not isinstance(band, dict):
            continue
        target_freq = _coerce_number(band.get("freq"))
        if target_freq is None:
            continue
        if require_exact_frequency:
            matches = np.flatnonzero(np.isclose(frequency_axis, target_freq))
            if matches.size == 0:
                continue
            idx = int(matches[0])
        else:
            idx = int(np.argmin(np.abs(frequency_axis - target_freq)))
        golden_level = (
            float(golden_axis[idx])
            if golden_axis is not None and idx < int(golden_axis.size)
            else None
        )
        actual_db, lower_db, upper_db = _chart_band_space(
            actual_level=float(level_axis[idx]),
            golden_level=golden_level,
            lower=band.get("lower"),
            upper=band.get("upper"),
            mode=mode,
        )
        actual_db = _normalize_chart_number(actual_db)
        lower_db = _normalize_chart_number(lower_db)
        upper_db = _normalize_chart_number(upper_db)
        if actual_db is None:
            continue
        payload.append(
            {
                "freq_hz": _normalize_chart_number(frequency_axis[idx]),
                "lower_db": lower_db,
                "upper_db": upper_db,
                "actual_db": actual_db,
                "passed": _passes_limit(actual_db, lower_db, upper_db),
            }
        )
    return payload


def _chart_band_space(
    *,
    actual_level: float,
    golden_level: float | None,
    lower,
    upper,
    mode: str,
) -> tuple[float, float | None, float | None]:
    lower_value = _coerce_bound(lower)
    upper_value = _coerce_bound(upper)
    if mode == "absolute" and golden_level is not None:
        if lower_value is not None:
            lower_value = golden_level + lower_value
        if upper_value is not None:
            upper_value = golden_level + upper_value
        return actual_level, lower_value, upper_value
    if mode == "delta" and golden_level is not None:
        return actual_level - golden_level, lower_value, upper_value
    return actual_level, lower_value, upper_value


def _passes_limit(actual_db: float, lower_db: float | None, upper_db: float | None) -> bool:
    if lower_db is not None and actual_db < lower_db:
        return False
    if upper_db is not None and actual_db > upper_db:
        return False
    return True


def _normalize_chart_list(values: np.ndarray | list[float]) -> list[float]:
    normalized: list[float] = []
    for value in values:
        normalized_value = _normalize_chart_number(value)
        if normalized_value is not None:
            normalized.append(normalized_value)
    return normalized


def _offset_chart_times(values, offset_sec: float) -> list[float]:
    if values is None:
        return []
    offset = _normalize_chart_number(offset_sec)
    if offset is None or abs(offset) <= 1e-12:
        return _normalize_chart_list(values)
    shifted: list[float] = []
    for value in values:
        normalized = _normalize_chart_number(value)
        if normalized is None:
            continue
        shifted_value = _normalize_chart_number(normalized + offset)
        if shifted_value is not None:
            shifted.append(shifted_value)
    return shifted


def _normalize_chart_pairs(
    x_values: np.ndarray | list[float],
    y_values: np.ndarray | list[float],
) -> tuple[np.ndarray, np.ndarray]:
    x_axis = np.asarray(x_values, dtype=np.float64)
    y_axis = np.asarray(y_values, dtype=np.float64)
    if x_axis.size <= 0 or y_axis.size <= 0:
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)

    length = min(int(x_axis.size), int(y_axis.size))
    x_axis = x_axis[:length]
    y_axis = y_axis[:length]
    finite_mask = np.isfinite(x_axis) & np.isfinite(y_axis)
    return x_axis[finite_mask], y_axis[finite_mask]


def _normalize_chart_number(value) -> float | None:
    normalized = _coerce_number(value)
    if normalized is None:
        return None
    normalized = round(normalized, 6)
    if abs(normalized) < 1e-9:
        return 0.0
    return normalized


def _normalize_loudness_unit(value) -> str:
    return "phon" if str(value or "").strip().lower() == "phon" else "sone"


def _loudness_value_in_unit(value_sone: float, unit: str) -> float:
    if unit == "phon":
        return dsp.sone_to_phon(value_sone)
    return max(float(value_sone), 0.0)


def _loudness_limits_sone(
    lower_limit: float | None,
    upper_limit: float | None,
    unit: str,
) -> tuple[float | None, float | None]:
    if unit == "phon":
        lower = dsp.phon_to_sone(lower_limit) if _is_finite(lower_limit) else None
        upper = dsp.phon_to_sone(upper_limit) if _is_finite(upper_limit) else None
        return lower, upper

    lower = max(float(lower_limit), 0.0) if _is_finite(lower_limit) else None
    upper = max(float(upper_limit), 0.0) if _is_finite(upper_limit) else None
    return lower, upper


__all__ = ["build_chart_payload", "build_summary_payload", "canonical_channel_label"]
