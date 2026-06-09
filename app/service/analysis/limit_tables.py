from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any, Iterable

import numpy as np


LIMIT_KIND_LOWER = "lower"
LIMIT_KIND_UPPER = "upper"
VALID_LIMIT_KINDS = {LIMIT_KIND_LOWER, LIMIT_KIND_UPPER}

_METRIC_FFT = "fft"
_METRIC_OCTAVE = "octave"
_VALID_METRICS = {_METRIC_FFT, _METRIC_OCTAVE}

_GOLDEN_MODE_OFF = "off"
_GOLDEN_MODE_ABSOLUTE = "absolute"
_GOLDEN_MODE_DELTA = "delta"
_VALID_GOLDEN_MODES = {
    _GOLDEN_MODE_OFF,
    _GOLDEN_MODE_ABSOLUTE,
    _GOLDEN_MODE_DELTA,
}


@dataclass(frozen=True)
class LimitPoint:
    freq: float
    limit: float
    source_index: int
    point_index: int


@dataclass(frozen=True)
class LimitTable:
    kind: str
    source_index: int
    points: tuple[LimitPoint, ...]


@dataclass(frozen=True)
class EvaluatedLimits:
    frequencies: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    active: np.ndarray


@dataclass(frozen=True)
class _LineSegment:
    table: LimitTable
    left: LimitPoint
    right: LimitPoint

    @property
    def start(self) -> float:
        return self.left.freq

    @property
    def end(self) -> float:
        return self.right.freq

    @property
    def slope(self) -> float:
        return (self.right.limit - self.left.limit) / (
            self.right.freq - self.left.freq
        )

    @property
    def intercept(self) -> float:
        return self.left.limit - self.slope * self.left.freq


def normalize_limit_tables(
    raw: Any,
    *,
    metric: str | None = None,
    label: str | None = None,
) -> list[LimitTable]:
    resolved_metric, metric_errors = _resolve_metric(metric, label)
    if metric_errors:
        raise ValueError("; ".join(metric_errors))
    tables, errors = _parse_limit_tables(raw, label=_display_label(label, metric))
    if errors:
        raise ValueError("; ".join(errors))
    errors = _validate_canonical_tables(
        tables,
        metric=resolved_metric,
        label=_display_label(label, metric),
    )
    if errors:
        raise ValueError("; ".join(errors))
    return tables


def normalize_limit_tables_for_storage(
    raw: Any,
    *,
    metric: str | None = None,
    label: str | None = None,
) -> list[dict]:
    return [
        {
            "kind": table.kind,
            "points": [
                {"freq": point.freq, "limit": point.limit}
                for point in table.points
            ],
        }
        for table in normalize_limit_tables(raw, metric=metric, label=label)
    ]


def validate_limit_tables(
    raw: Any,
    *,
    metric: str | None = None,
    label: str | None = None,
) -> list[str]:
    display_label = _display_label(label, metric)
    resolved_metric, errors = _resolve_metric(metric, label)
    tables, parse_errors = _parse_limit_tables(raw, label=display_label)
    errors.extend(parse_errors)
    if parse_errors or resolved_metric not in _VALID_METRICS:
        return errors
    errors.extend(
        _validate_canonical_tables(
            tables,
            metric=resolved_metric,
            label=display_label,
        )
    )
    return errors


def evaluate_limit_tables(
    tables: Iterable[LimitTable],
    frequencies: Iterable[float],
    *,
    golden_levels: np.ndarray | None = None,
    mode: str = _GOLDEN_MODE_OFF,
) -> EvaluatedLimits:
    resolved_mode = _resolve_golden_mode(mode)
    normalized_tables = list(tables or [])
    frequency_values = _as_float_array(frequencies)
    golden = _golden_levels_for_evaluation(
        frequency_values,
        golden_levels,
        mode=resolved_mode,
    )
    lower = np.full(frequency_values.shape, np.nan, dtype=np.float64)
    upper = np.full(frequency_values.shape, np.nan, dtype=np.float64)
    active = np.zeros(frequency_values.shape, dtype=np.bool_)

    for index, frequency in np.ndenumerate(frequency_values):
        lower_values: list[float] = []
        upper_values: list[float] = []
        if math.isfinite(float(frequency)):
            for table in normalized_tables:
                value = _table_limit_at(table, float(frequency))
                if value is None:
                    continue
                if resolved_mode == _GOLDEN_MODE_ABSOLUTE:
                    value += float(golden[index])
                if table.kind == LIMIT_KIND_LOWER:
                    lower_values.append(value)
                elif table.kind == LIMIT_KIND_UPPER:
                    upper_values.append(value)
        if lower_values:
            lower[index] = max(lower_values)
        if upper_values:
            upper[index] = min(upper_values)
        active[index] = bool(lower_values or upper_values)

    return EvaluatedLimits(
        frequencies=frequency_values,
        lower=lower,
        upper=upper,
        active=active,
    )


def limit_lines_for_chart(
    tables: Iterable[LimitTable],
    *,
    frequencies: np.ndarray | None = None,
    golden_levels: np.ndarray | None = None,
    mode: str = _GOLDEN_MODE_OFF,
) -> list[dict]:
    resolved_mode = _resolve_golden_mode(mode)
    normalized_tables = list(tables or [])
    has_points = any(table.points for table in normalized_tables)
    golden_axis = None
    if resolved_mode == _GOLDEN_MODE_ABSOLUTE and has_points:
        golden_axis = _golden_axis_for_chart(frequencies, golden_levels)

    return [
        {
            "kind": table.kind,
            "points": [
                {
                    "freq_hz": point.freq,
                    "limit_db": _limit_for_chart_point(
                        point,
                        mode=resolved_mode,
                        golden_axis=golden_axis,
                    ),
                }
                for point in table.points
            ],
        }
        for table in normalized_tables
    ]


def _resolve_golden_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    if normalized in _VALID_GOLDEN_MODES:
        return normalized
    raise ValueError("limit table mode must be off, absolute, or delta")


def _as_float_array(values: Iterable[float] | np.ndarray) -> np.ndarray:
    if isinstance(values, np.ndarray):
        return np.asarray(values, dtype=np.float64)
    return np.asarray(list(values), dtype=np.float64)


def _golden_levels_for_evaluation(
    frequencies: np.ndarray,
    golden_levels: np.ndarray | None,
    *,
    mode: str,
) -> np.ndarray | None:
    if golden_levels is None:
        if mode == _GOLDEN_MODE_ABSOLUTE:
            raise ValueError("golden_levels are required when mode is absolute")
        return None

    golden = np.asarray(golden_levels, dtype=np.float64)
    if golden.shape != frequencies.shape:
        raise ValueError("golden_levels shape must match frequencies")
    return golden


def _golden_axis_for_chart(
    frequencies: np.ndarray | None,
    golden_levels: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    if frequencies is None or golden_levels is None:
        raise ValueError(
            "frequencies and golden_levels are required when mode is absolute"
        )

    frequency_axis = np.asarray(frequencies, dtype=np.float64).reshape(-1)
    golden_axis = np.asarray(golden_levels, dtype=np.float64).reshape(-1)
    if frequency_axis.shape != golden_axis.shape:
        raise ValueError("golden_levels shape must match frequencies")
    if frequency_axis.size == 0:
        raise ValueError("frequencies cannot be empty when mode is absolute")

    order = np.argsort(frequency_axis, kind="stable")
    return frequency_axis[order], golden_axis[order]


def _limit_for_chart_point(
    point: LimitPoint,
    *,
    mode: str,
    golden_axis: tuple[np.ndarray, np.ndarray] | None,
) -> float:
    if mode != _GOLDEN_MODE_ABSOLUTE:
        return point.limit
    if golden_axis is None:
        return point.limit
    frequencies, golden_levels = golden_axis
    return float(np.interp(point.freq, frequencies, golden_levels) + point.limit)


def _resolve_metric(
    metric: str | None,
    label: str | None,
) -> tuple[str, list[str]]:
    if metric is None:
        inferred = _metric_from_label(label)
        if inferred is not None:
            return inferred, []
        return "", ["限制表指标 metric 必须是 fft 或 octave"]

    normalized = str(metric).strip().lower()
    if normalized in _VALID_METRICS:
        return normalized, []
    return normalized, ["限制表指标 metric 必须是 fft 或 octave"]


def _metric_from_label(label: str | None) -> str | None:
    if not label:
        return None
    normalized = str(label).strip().lower()
    if "fft" in normalized:
        return _METRIC_FFT
    if "octave" in normalized or "倍频" in normalized:
        return _METRIC_OCTAVE
    return None


def _display_label(label: str | None, metric: str | None = None) -> str:
    if label:
        return str(label)
    if metric == _METRIC_FFT:
        return "FFT"
    if metric == _METRIC_OCTAVE:
        return "倍频程"
    return "限制表"


def _parse_limit_tables(raw: Any, *, label: str) -> tuple[list[LimitTable], list[str]]:
    if isinstance(raw, LimitTable):
        return _parse_normalized_tables([raw], label=label)
    if not isinstance(raw, list):
        return [], [f"{label}限制表必须是列表"]
    if not raw:
        return [], []

    shapes: list[str] = []
    errors: list[str] = []
    for index, item in enumerate(raw):
        shape, shape_errors = _detect_item_shape(item, index=index, label=label)
        shapes.append(shape)
        errors.extend(shape_errors)

    has_legacy = any(shape == "legacy" for shape in shapes)
    has_grouped = any(shape == "grouped" for shape in shapes)
    if has_legacy and has_grouped:
        errors.append(f"{label}限制表不能混合旧版频点格式和分组格式")
        return [], errors
    if "mixed" in shapes:
        errors.append(f"{label}限制表条目不能混合旧版字段和分组字段")
        return [], errors

    if has_legacy:
        tables, legacy_errors = _parse_legacy_rows(raw, shapes=shapes, label=label)
        errors.extend(legacy_errors)
        return ([] if errors else tables), errors
    if has_grouped:
        tables, grouped_errors = _parse_grouped_tables(raw, shapes=shapes, label=label)
        errors.extend(grouped_errors)
        return ([] if errors else tables), errors
    return [], errors


def _detect_item_shape(item: Any, *, index: int, label: str) -> tuple[str, list[str]]:
    if isinstance(item, LimitTable):
        return "grouped", []
    if not isinstance(item, dict):
        return "unknown", [f"{label}限制表第 {index + 1} 项格式无效"]

    legacy_fields = {"freq", "lower", "upper"}
    grouped_fields = {"kind", "points"}
    has_legacy = bool(legacy_fields & set(item))
    has_grouped = bool(grouped_fields & set(item))
    is_legacy = legacy_fields <= set(item)
    is_grouped = grouped_fields <= set(item)

    if is_legacy and not has_grouped:
        return "legacy", []
    if is_grouped and not has_legacy:
        return "grouped", []
    if has_legacy and has_grouped:
        return "mixed", []
    return "unknown", [f"{label}限制表第 {index + 1} 项格式无效"]


def _parse_legacy_rows(
    raw: list[Any],
    *,
    shapes: list[str],
    label: str,
) -> tuple[list[LimitTable], list[str]]:
    lower_points: list[LimitPoint] = []
    upper_points: list[LimitPoint] = []
    errors: list[str] = []

    for row_index, (item, shape) in enumerate(zip(raw, shapes, strict=False)):
        if shape != "legacy":
            continue
        if not isinstance(item, dict):
            continue
        freq, freq_error = _coerce_number(
            item.get("freq"),
            positive=True,
            field_label=f"{label}限制表第 {row_index + 1} 行频率",
        )
        lower, lower_error = _coerce_number(
            item.get("lower"),
            positive=False,
            field_label=f"{label}限制表第 {row_index + 1} 行下限",
        )
        upper, upper_error = _coerce_number(
            item.get("upper"),
            positive=False,
            field_label=f"{label}限制表第 {row_index + 1} 行上限",
        )
        row_errors = [error for error in (freq_error, lower_error, upper_error) if error]
        errors.extend(row_errors)
        if row_errors:
            continue
        lower_points.append(
            LimitPoint(
                freq=freq,
                limit=lower,
                source_index=row_index,
                point_index=row_index,
            )
        )
        upper_points.append(
            LimitPoint(
                freq=freq,
                limit=upper,
                source_index=row_index,
                point_index=row_index,
            )
        )

    if errors:
        return [], errors
    return [
        LimitTable(
            kind=LIMIT_KIND_LOWER,
            source_index=0,
            points=_sort_points(lower_points),
        ),
        LimitTable(
            kind=LIMIT_KIND_UPPER,
            source_index=0,
            points=_sort_points(upper_points),
        ),
    ], []


def _parse_grouped_tables(
    raw: list[Any],
    *,
    shapes: list[str],
    label: str,
) -> tuple[list[LimitTable], list[str]]:
    tables: list[LimitTable] = []
    errors: list[str] = []

    for source_index, (item, shape) in enumerate(zip(raw, shapes, strict=False)):
        if shape != "grouped":
            continue
        if isinstance(item, LimitTable):
            table, table_errors = _parse_normalized_table(item, label=label)
        else:
            table, table_errors = _parse_grouped_table_dict(
                item,
                source_index=source_index,
                label=label,
            )
        errors.extend(table_errors)
        if table_errors or table is None:
            continue
        tables.append(table)

    return ([] if errors else tables), errors


def _parse_normalized_tables(
    raw: list[LimitTable],
    *,
    label: str,
) -> tuple[list[LimitTable], list[str]]:
    tables: list[LimitTable] = []
    errors: list[str] = []
    for table in raw:
        parsed, table_errors = _parse_normalized_table(table, label=label)
        errors.extend(table_errors)
        if parsed is not None and not table_errors:
            tables.append(parsed)
    return ([] if errors else tables), errors


def _parse_normalized_table(
    table: LimitTable,
    *,
    label: str,
) -> tuple[LimitTable | None, list[str]]:
    errors: list[str] = []
    if isinstance(table.points, tuple) and not table.points:
        return None, []
    if table.kind not in VALID_LIMIT_KINDS:
        errors.append(f"{label}限制表类型必须是 upper 或 lower")

    points: list[LimitPoint] = []
    if not isinstance(table.points, tuple):
        errors.append(f"{label}限制表 points 必须是列表")
    else:
        for index, point in enumerate(table.points):
            if not isinstance(point, LimitPoint):
                errors.append(f"{label}限制表第 {index + 1} 个频点格式无效")
                continue
            freq, freq_error = _coerce_number(
                point.freq,
                positive=True,
                field_label=f"{label}限制表第 {index + 1} 个频点频率",
            )
            limit, limit_error = _coerce_number(
                point.limit,
                positive=False,
                field_label=f"{label}限制表第 {index + 1} 个频点限值",
            )
            errors.extend(error for error in (freq_error, limit_error) if error)
            if freq_error or limit_error:
                continue
            points.append(
                LimitPoint(
                    freq=freq,
                    limit=limit,
                    source_index=point.source_index,
                    point_index=point.point_index,
                )
            )

    if errors:
        return None, errors
    return LimitTable(
        kind=table.kind,
        source_index=table.source_index,
        points=_sort_points(points),
    ), []


def _parse_grouped_table_dict(
    item: dict,
    *,
    source_index: int,
    label: str,
) -> tuple[LimitTable | None, list[str]]:
    errors: list[str] = []
    raw_points = item.get("points")
    if isinstance(raw_points, list) and not raw_points:
        return None, []

    kind = item.get("kind")
    if kind not in VALID_LIMIT_KINDS:
        errors.append(f"{label}限制表第 {source_index + 1} 项类型必须是 upper 或 lower")

    if not isinstance(raw_points, list):
        errors.append(f"{label}限制表第 {source_index + 1} 项 points 必须是列表")
        return None, errors

    points: list[LimitPoint] = []
    for point_index, point in enumerate(raw_points):
        if not isinstance(point, dict):
            errors.append(
                f"{label}限制表第 {source_index + 1} 项第 {point_index + 1} 个频点格式无效"
            )
            continue
        freq, freq_error = _coerce_number(
            point.get("freq"),
            positive=True,
            field_label=(
                f"{label}限制表第 {source_index + 1} 项"
                f"第 {point_index + 1} 个频点频率"
            ),
        )
        limit, limit_error = _coerce_number(
            point.get("limit"),
            positive=False,
            field_label=(
                f"{label}限制表第 {source_index + 1} 项"
                f"第 {point_index + 1} 个频点限值"
            ),
        )
        errors.extend(error for error in (freq_error, limit_error) if error)
        if freq_error or limit_error:
            continue
        points.append(
            LimitPoint(
                freq=freq,
                limit=limit,
                source_index=source_index,
                point_index=point_index,
            )
        )

    if errors:
        return None, errors
    return LimitTable(
        kind=kind,
        source_index=source_index,
        points=_sort_points(points),
    ), []


def _coerce_number(
    value: Any,
    *,
    positive: bool,
    field_label: str,
) -> tuple[float, str | None]:
    if isinstance(value, bool) or not isinstance(value, Real):
        return math.nan, f"{field_label}必须是有限数字"
    coerced = float(value)
    if not math.isfinite(coerced):
        return math.nan, f"{field_label}必须是有限数字"
    if positive and coerced <= 0:
        return math.nan, f"{field_label}必须是正数"
    return coerced, None


def _sort_points(points: Iterable[LimitPoint]) -> tuple[LimitPoint, ...]:
    return tuple(sorted(points, key=lambda point: (point.freq, point.point_index)))


def _validate_canonical_tables(
    tables: list[LimitTable],
    *,
    metric: str,
    label: str,
) -> list[str]:
    errors: list[str] = []
    for index, table in enumerate(tables):
        if table.kind not in VALID_LIMIT_KINDS:
            errors.append(f"{label}限制表第 {index + 1} 项类型必须是 upper 或 lower")
            continue
        if metric in (_METRIC_FFT, _METRIC_OCTAVE):
            distinct_count = len({point.freq for point in table.points})
            if distinct_count < 2:
                errors.append(f"{label}限制表第 {index + 1} 项至少需要两个不同频率")
    if errors:
        return errors
    errors.extend(_validate_lower_upper_crossing(tables, label=label))
    return errors


def _validate_lower_upper_crossing(
    tables: list[LimitTable],
    *,
    label: str,
) -> list[str]:
    if not any(table.kind == LIMIT_KIND_LOWER for table in tables):
        return []
    if not any(table.kind == LIMIT_KIND_UPPER for table in tables):
        return []

    candidates = _crossing_candidate_frequencies(tables)
    if not candidates:
        return []

    evaluated = evaluate_limit_tables(
        tables,
        np.asarray(sorted(candidates), dtype=np.float64),
    )
    for lower, upper in zip(evaluated.lower, evaluated.upper, strict=False):
        if math.isfinite(float(lower)) and math.isfinite(float(upper)):
            if float(lower) >= float(upper):
                return [f"{label}限制表下限必须小于上限"]
    return []


def _crossing_candidate_frequencies(tables: list[LimitTable]) -> set[float]:
    candidates: set[float] = set()
    segments: list[_LineSegment] = []
    for table in tables:
        for point in table.points:
            candidates.add(point.freq)
        table_segments = _table_segments(table)
        segments.extend(table_segments)
        for segment in table_segments:
            candidates.add(segment.start)
            candidates.add(segment.end)

    for left_index, left in enumerate(segments):
        for right in segments[left_index + 1 :]:
            intersection = _segment_intersection_frequency(left, right)
            if intersection is not None:
                candidates.add(intersection)
    return candidates


def _table_segments(table: LimitTable) -> list[_LineSegment]:
    segments: list[_LineSegment] = []
    for left, right in zip(table.points, table.points[1:], strict=False):
        if left.freq == right.freq:
            continue
        segments.append(_LineSegment(table=table, left=left, right=right))
    return segments


def _segment_intersection_frequency(
    first: _LineSegment,
    second: _LineSegment,
) -> float | None:
    overlap_start = max(first.start, second.start)
    overlap_end = min(first.end, second.end)
    if overlap_start > overlap_end:
        return None

    slope_delta = first.slope - second.slope
    if math.isclose(slope_delta, 0.0, abs_tol=1e-12):
        return None

    x = (second.intercept - first.intercept) / slope_delta
    tolerance = 1e-9 * max(1.0, abs(overlap_start), abs(overlap_end))
    if overlap_start - tolerance <= x <= overlap_end + tolerance:
        return min(max(x, overlap_start), overlap_end)
    return None


def _table_limit_at(table: LimitTable, frequency: float) -> float | None:
    exact = [point.limit for point in table.points if point.freq == frequency]
    if exact:
        return max(exact) if table.kind == LIMIT_KIND_LOWER else min(exact)

    for left, right in zip(table.points, table.points[1:], strict=False):
        if left.freq == right.freq:
            continue
        lower_freq, upper_freq = sorted((left.freq, right.freq))
        if lower_freq < frequency < upper_freq:
            ratio = (frequency - left.freq) / (right.freq - left.freq)
            return left.limit + ratio * (right.limit - left.limit)
    return None
