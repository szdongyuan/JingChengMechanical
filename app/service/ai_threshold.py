"""Shared validation and resolution for optional software AI thresholds."""
import math


def coerce_probability(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        probability = float(value)
    except (OverflowError, ValueError):
        return None
    return probability if math.isfinite(probability) and 0 <= probability <= 1 else None


def resolve_effective_decision_threshold(ai_result: dict, request_params: dict) -> tuple[float, str]:
    configured = request_params.get('decision_threshold')
    if configured is not None:
        value = coerce_probability(configured)
        if value is None:
            raise ValueError('decision_threshold 必须为 0 到 1 之间的有限数值')
        return value, 'request_params'
    extra = ai_result.get('extra')
    value = coerce_probability(extra.get('decision_threshold')) if isinstance(extra, dict) else None
    return (value, 'response_extra') if value is not None else (0.5, 'default')
