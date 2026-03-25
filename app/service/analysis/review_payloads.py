from __future__ import annotations

import math

_SUMMARY_ROW_DEFS = (
    ("spl", "SPL"),
    ("fft", "FFT"),
    ("octave", "倍频程"),
    ("ai", "AI分析"),
)
_NO_RESULT_FALLBACK_MESSAGE = "分析结果不可用"
_NO_RESULT_STATUS = "无结果"


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
    rows = [
        _build_spl_row(
            selected_channels=selected_channels,
            channel_configs=channel_configs,
            channel_results=channel_results,
        ),
        _build_band_row(
            item_key="fft",
            label="FFT",
            config_key="fft_band_limits",
            result_key="fft_band_results",
            selected_channels=selected_channels,
            channel_configs=channel_configs,
            channel_results=channel_results,
        ),
        _build_band_row(
            item_key="octave",
            label="倍频程",
            config_key="oct_band_limits",
            result_key="oct_band_results",
            selected_channels=selected_channels,
            channel_configs=channel_configs,
            channel_results=channel_results,
        ),
        ai_row,
    ]
    overall_status = _overall_status(rows)
    return {
        "overall_status": overall_status,
        "overall_label": _overall_label(overall_status),
        "error_message": (
            _normalize_error_message(error_message, ai_error_message)
            if overall_status == "no_result"
            else None
        ),
        "rows": rows,
    }


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
) -> dict:
    candidates = []
    for channel_index in selected_channels:
        channel_config = channel_configs.get(channel_index, {})
        lower = _coerce_bound(channel_config.get("spl_lower_limit"))
        upper = _coerce_bound(channel_config.get("spl_upper_limit"))
        if not _has_constraint(lower, upper):
            continue
        channel_result = channel_results.get(channel_index)
        if not isinstance(channel_result, dict):
            continue
        actual = _coerce_number(channel_result.get("spl_db"))
        if actual is None:
            continue
        deviation = _compute_deviation(actual=actual, lower=lower, upper=upper)
        candidates.append(
            {
                "deviation_value": deviation,
                "channel_index": channel_index,
            }
        )
    if not candidates:
        return _build_unconstrained_row("spl", "SPL")
    return _build_row_from_candidate(
        item_key="spl",
        label="SPL",
        candidate=_worst_candidate(candidates),
        unit="dB",
        constrained=True,
    )


def _build_band_row(
    *,
    item_key: str,
    label: str,
    config_key: str,
    result_key: str,
    selected_channels: tuple[int, ...],
    channel_configs: dict[int, dict],
    channel_results: dict[int, dict],
) -> dict:
    configured_any = False
    candidates = []
    for channel_index in selected_channels:
        channel_config = channel_configs.get(channel_index, {})
        configured_bands = channel_config.get(config_key, [])
        if isinstance(configured_bands, list) and configured_bands:
            configured_any = True
        channel_result = channel_results.get(channel_index, {})
        for band_result in channel_result.get(result_key, []):
            if not isinstance(band_result, dict):
                continue
            actual = _coerce_number(band_result.get("level"))
            lower = _coerce_bound(band_result.get("lower"))
            upper = _coerce_bound(band_result.get("upper"))
            deviation = _compute_deviation(actual=actual, lower=lower, upper=upper)
            if deviation is None:
                continue
            candidates.append(
                {
                    "deviation_value": deviation,
                    "channel_index": channel_index,
                }
            )
    if not configured_any:
        return _build_unconstrained_row(item_key, label)
    if not candidates:
        return _build_unconstrained_row(item_key, label)
    return _build_row_from_candidate(
        item_key=item_key,
        label=label,
        candidate=_worst_candidate(candidates),
        unit="dB",
        constrained=True,
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

    if status in {"", "disabled", "not_enabled", "not_configured", "skipped"} and threshold is None:
        return _build_unconstrained_row("ai", "AI分析"), None
    if status == "success" and threshold is None:
        return _build_unconstrained_row("ai", "AI分析"), None
    if status == "success" and score is not None:
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
    return _build_row(
        item_key=item_key,
        label=label,
        deviation_value=None,
        channel_index=None,
        status=_NO_RESULT_STATUS,
        constrained=False,
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
    if actual is None:
        return None
    if _is_finite(lower) and _is_finite(upper):
        if actual < lower:
            return _normalize_deviation(lower - actual)
        if actual > upper:
            return _normalize_deviation(actual - upper)
        return _normalize_deviation(-min(actual - lower, upper - actual))
    if _is_finite(lower):
        if actual < lower:
            return _normalize_deviation(lower - actual)
        return _normalize_deviation(-(actual - lower))
    if _is_finite(upper):
        if actual > upper:
            return _normalize_deviation(actual - upper)
        return _normalize_deviation(-(upper - actual))
    return None


def _format_deviation(value: float | None, *, unit: str | None) -> str:
    if value is None:
        return "--"
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
    if isinstance(value, (int, float)):
        if math.isfinite(value):
            return float(value)
        return None
    return None


def _coerce_bound(value) -> float | None:
    coerced = _coerce_number(value)
    if coerced is None:
        return None
    return coerced


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


__all__ = ["build_summary_payload", "canonical_channel_label"]
