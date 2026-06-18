from __future__ import annotations

from collections import OrderedDict
import math

import numpy as np

from app.service.historical_metric_report_types import (
    HistoricalMetricValue,
    MetricGroupStats,
)


BLANK_MODEL_LABEL = "未填写型号"
ALL_MODELS_LABEL = "全部型号"


def compute_statistics(rows: list[HistoricalMetricValue]) -> list[MetricGroupStats]:
    ok_rows = [
        row
        for row in rows
        if row.status == "ok" and _finite(row.metric_value)
    ]
    groups = _group_metric_rows(ok_rows, include_all_models=True)
    return [
        _compute_group_stats(group_key, group_rows)
        for group_key, group_rows in groups.items()
    ]


def compute_outliers(rows: list[HistoricalMetricValue]) -> list[HistoricalMetricValue]:
    ok_rows = [
        row
        for row in rows
        if row.status == "ok" and _finite(row.metric_value)
    ]
    groups = _group_metric_rows(ok_rows, include_all_models=False)
    outliers: list[HistoricalMetricValue] = []
    seen: set[tuple[str, int, str]] = set()

    for group_rows in groups.values():
        values = _row_values(group_rows)
        n = len(values)
        if n == 0:
            continue

        mean = float(np.mean(values))
        std = float(np.std(values, ddof=1)) if n >= 2 else None
        has_std_rule = std is not None and math.isfinite(std)
        p5 = p95 = None
        if n >= 20:
            p5, _p50, p95 = _linear_percentiles(values)

        for row in group_rows:
            value = float(row.metric_value)
            is_outlier = False
            if has_std_rule:
                is_outlier = value < mean - (3.0 * std) or value > mean + (3.0 * std)
            if n >= 20 and p5 is not None and p95 is not None:
                is_outlier = is_outlier or value < p5 or value > p95
            if not is_outlier:
                continue

            dedupe_key = (row.audio_data_id, row.channel_index, row.metric_key)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            outliers.append(row)

    return outliers


def _group_metric_rows(
    rows: list[HistoricalMetricValue],
    *,
    include_all_models: bool,
) -> OrderedDict[str, list[HistoricalMetricValue]]:
    groups: OrderedDict[str, list[HistoricalMetricValue]] = OrderedDict()
    for row in rows:
        _append_group(groups, _group_key(row, _product_model_label(row.product_model)), row)
        if include_all_models:
            _append_group(groups, _group_key(row, ALL_MODELS_LABEL), row)
    return groups


def _append_group(
    groups: OrderedDict[str, list[HistoricalMetricValue]],
    group_key: str,
    row: HistoricalMetricValue,
) -> None:
    groups.setdefault(group_key, []).append(row)


def _group_key(row: HistoricalMetricValue, product_model: str) -> str:
    return "|".join(
        (
            product_model,
            row.metric_key,
            row.channel_label,
        )
    )


def _compute_group_stats(
    group_key: str,
    rows: list[HistoricalMetricValue],
) -> MetricGroupStats:
    values = _row_values(rows)
    first = rows[0]
    n = len(values)
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1)) if n >= 2 else None
    if std is not None and not math.isfinite(std):
        std = None
    p5, p50, p95 = _linear_percentiles(values)
    histogram_counts, histogram_bins = _histogram(values)
    normal_x, normal_y = _normal_curve(
        values,
        mean,
        std,
        histogram_bins,
    )

    return MetricGroupStats(
        group_key=group_key,
        product_model=group_key.split("|", 1)[0],
        metric_key=first.metric_key,
        metric_label=first.metric_label,
        metric_unit=first.metric_unit,
        channel_label=first.channel_label,
        n=n,
        mean=mean,
        std=std,
        min=float(np.min(values)),
        max=float(np.max(values)),
        p5=p5,
        p50=p50,
        p95=p95,
        histogram_bins=histogram_bins,
        histogram_counts=histogram_counts,
        normal_x=normal_x,
        normal_y=normal_y,
        normal_available=bool(normal_x and normal_y),
    )


def _normal_curve(
    values: list[float],
    mean: float,
    std: float | None,
    histogram_bins: list[float],
) -> tuple[list[float], list[float]]:
    n = len(values)
    if n < 5 or std is None or not math.isfinite(std) or std <= 0.0:
        return [], []
    if len(histogram_bins) < 2:
        return [], []

    bin_width = _first_positive_bin_width(histogram_bins)
    if bin_width is None:
        return [], []

    low = min(min(values), mean - (4.0 * std))
    high = max(max(values), mean + (4.0 * std))
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        return [], []

    x = np.linspace(low, high, 101)
    coefficient = 1.0 / (std * math.sqrt(2.0 * math.pi))
    density = coefficient * np.exp(-0.5 * ((x - mean) / std) ** 2)
    y = density * bin_width * n
    return [float(item) for item in x], [float(item) for item in y]


def _histogram(values: list[float]) -> tuple[list[int], list[float]]:
    counts, bins = np.histogram(values, bins="auto")
    return [int(item) for item in counts], [float(item) for item in bins]


def _first_positive_bin_width(bins: list[float]) -> float | None:
    for left, right in zip(bins, bins[1:]):
        width = float(right) - float(left)
        if math.isfinite(width) and width > 0.0:
            return width
    return None


def _linear_percentiles(values: list[float]) -> tuple[float, float, float]:
    try:
        percentiles = np.percentile(values, [5, 50, 95], method="linear")
    except TypeError:
        percentiles = np.percentile(values, [5, 50, 95], interpolation="linear")
    p5, p50, p95 = percentiles
    return float(p5), float(p50), float(p95)


def _row_values(rows: list[HistoricalMetricValue]) -> list[float]:
    return [float(row.metric_value) for row in rows]


def _product_model_label(value: object) -> str:
    label = "" if value is None else str(value).strip()
    return label or BLANK_MODEL_LABEL


def _finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)
