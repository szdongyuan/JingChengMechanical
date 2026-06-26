from __future__ import annotations

import csv
import html
import math
from pathlib import Path
from typing import Iterable

from app.service.historical_metric_report_types import (
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    MetricGroupStats,
)


CSV_HEADERS = [
    "audio_data_id",
    "record_date",
    "product_model",
    "barcode",
    "label",
    "label_source",
    "analyse_config",
    "channel_label",
    "metric_key",
    "metric_label",
    "metric_value",
    "metric_unit",
    "status",
    "error_message",
]

_FILTER_LABELS = {
    "product_model": "型号",
    "product_models": "型号",
    "start_time": "开始时间",
    "end_time": "结束时间",
    "barcode": "SN码",
    "barcode_regex": "SN码匹配方式",
    "labels": "状态标签",
}

_FILTER_VALUE_LABELS = {
    "ALL": "全部型号",
    "OK": "OK",
    "NG": "NG",
    "not_labeled": "未标注",
}

_STATUS_LABELS = {
    "ok": "正常",
    "skipped": "跳过",
    "failed": "失败",
}


def export_csv_detail(result: HistoricalMetricReportResult, target_path: str) -> str:
    with open(target_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in result.metric_rows:
            writer.writerow(_metric_row_to_csv_dict(row))
    return target_path


def export_html_report(result: HistoricalMetricReportResult, target_path: str) -> str:
    html_text = render_html_report(result)
    Path(target_path).write_text(html_text, encoding="utf-8")
    return target_path


def render_html_report(result: HistoricalMetricReportResult) -> str:
    body_parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>测量结果正态分布统计报告</title>",
        "<style>",
        _css(),
        "</style>",
        "</head>",
        "<body>",
        "<main>",
        "<h1>测量结果正态分布统计报告</h1>",
        _render_summary(result),
        _render_filters(result.filters),
        _render_stats(result.stats),
        _render_charts(result.stats),
        _render_failures(_failure_rows(result)),
        _render_outliers(result.outliers),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(body_parts)


def _metric_row_to_csv_dict(row: HistoricalMetricValue) -> dict[str, object]:
    return {
        "audio_data_id": _csv_value(row.audio_data_id),
        "record_date": _csv_value(row.record_date),
        "product_model": _csv_value(row.product_model),
        "barcode": _csv_value(row.barcode),
        "label": _csv_value(row.label),
        "label_source": _csv_value(row.label_source),
        "analyse_config": _csv_value(row.analyse_config),
        "channel_label": _csv_value(row.channel_label),
        "metric_key": _csv_value(row.metric_key),
        "metric_label": _csv_value(row.metric_label),
        "metric_value": _csv_value(row.metric_value),
        "metric_unit": _csv_value(row.metric_unit),
        "status": _csv_value(row.status),
        "error_message": _csv_value(row.error_message),
    }


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    return value


def _render_summary(result: HistoricalMetricReportResult) -> str:
    rows = [
        ("生成时间", result.generated_at),
        ("匹配记录数", result.total_records),
        ("有效记录数", result.valid_record_count),
        ("跳过记录数", result.skipped_record_count),
        ("失败记录数", result.failed_record_count),
        ("OK 数量", result.label_counts.get("OK", 0)),
        ("NG 数量", result.label_counts.get("NG", 0)),
        ("未标注数量", result.label_counts.get("not_labeled", 0)),
    ]
    return (
        '<section aria-labelledby="summary-title">'
        '<h2 id="summary-title">数据覆盖摘要</h2>'
        '<table class="summary-table"><tbody>'
        + "".join(
            f"<tr><th>{_escape(label)}</th><td>{_escape(value)}</td></tr>"
            for label, value in rows
        )
        + "</tbody></table></section>"
    )


def _render_filters(filters: dict[str, object]) -> str:
    rows = []
    for key in sorted(filters):
        rows.append((_filter_label(key), _filter_value(key, filters[key])))
    if not rows:
        rows.append(("筛选条件", "全部记录"))
    return (
        '<section aria-labelledby="filters-title">'
        '<h2 id="filters-title">筛选条件</h2>'
        "<table><thead><tr><th>字段</th><th>值</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_escape(key)}</td><td>{_escape(value)}</td></tr>"
            for key, value in rows
        )
        + "</tbody></table></section>"
    )


def _filter_label(key: str) -> str:
    return _FILTER_LABELS.get(key, str(key))


def _filter_value(key: str, value: object) -> str:
    if key == "barcode_regex":
        return "正则匹配" if bool(value) else "普通匹配"
    if isinstance(value, (list, tuple, set)):
        if not value:
            return "全部"
        return "，".join(_filter_scalar_value(item) for item in value)
    if value is None or value == "":
        return "全部" if key in {"product_model", "product_models", "labels"} else ""
    return _filter_scalar_value(value)


def _filter_scalar_value(value: object) -> str:
    text = str(value)
    return _FILTER_VALUE_LABELS.get(text, text)


def _render_stats(stats: list[MetricGroupStats]) -> str:
    headers = [
        "结果",
        "样本数",
        "均值",
        "标准差",
        "最小值",
        "最大值",
        "P5",
        "P50",
        "P95",
    ]
    rows = [
        [
            stat.metric_label,
            stat.n,
            _format_number(stat.mean),
            _format_number(stat.std),
            _format_number(stat.min),
            _format_number(stat.max),
            _format_number(stat.p5),
            _format_number(stat.p50),
            _format_number(stat.p95),
        ]
        for stat in stats
    ]
    return _render_table_section(
        title="统计结果",
        headers=headers,
        rows=rows,
        empty_text="无可用统计结果",
    )


def _render_charts(stats: list[MetricGroupStats]) -> str:
    if not stats:
        return (
            '<section aria-labelledby="charts-title">'
            '<h2 id="charts-title">直方图与正态拟合</h2>'
            '<p class="empty">无可用图表</p></section>'
        )

    charts = []
    for stat in stats:
        title = stat.metric_label
        charts.append(
            '<article class="chart-block">'
            f"<h3>{_escape(title)}</h3>"
            f"{_render_svg_chart(stat)}"
            "</article>"
        )
    return (
        '<section aria-labelledby="charts-title">'
        '<h2 id="charts-title">直方图与正态拟合</h2>'
        + "".join(charts)
        + "</section>"
    )


def _render_svg_chart(stat: MetricGroupStats) -> str:
    width = 720
    height = 260
    margin_left = 48
    margin_right = 20
    margin_top = 20
    margin_bottom = 42
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    histogram_points = _histogram_points(stat)
    normal_points = _normal_points(stat)
    x_values = [item for left, right, _count in histogram_points for item in (left, right)]
    x_values.extend(x for x, _y in normal_points)
    sigma_markers = _sigma_markers(stat)
    x_values.extend(marker[0] for marker in sigma_markers)
    y_values = [count for _left, _right, count in histogram_points]
    y_values.extend(y for _x, y in normal_points)

    if not x_values or not y_values:
        return _empty_svg(
            width,
            height,
            "无可用直方图数据",
            stat,
        )

    x_min = min(x_values)
    x_max = max(x_values)
    y_max = max(y_values)
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5
    if y_max <= 0.0:
        y_max = 1.0
    x_ticks = _nice_ticks(x_min, x_max, 6)
    y_ticks = _nice_ticks(0.0, y_max, 5)
    if len(x_ticks) >= 2:
        x_min = x_ticks[0]
        x_max = x_ticks[-1]
    if len(y_ticks) >= 2:
        y_max = y_ticks[-1]

    def scale_x(value: float) -> float:
        return margin_left + ((value - x_min) / (x_max - x_min) * plot_width)

    def scale_y(value: float) -> float:
        return margin_top + plot_height - (value / y_max * plot_height)

    bars = []
    for left, right, count in histogram_points:
        x = scale_x(left)
        bar_width = max(1.0, scale_x(right) - x - 2.0)
        y = scale_y(count)
        bar_height = margin_top + plot_height - y
        bars.append(
            '<rect class="histogram-bar" '
            f'x="{_svg_number(x)}" y="{_svg_number(y)}" '
            f'width="{_svg_number(bar_width)}" '
            f'height="{_svg_number(bar_height)}" />'
        )

    normal_polyline = ""
    if stat.normal_available and normal_points:
        points = " ".join(
            f"{_svg_number(scale_x(x))},{_svg_number(scale_y(y))}"
            for x, y in normal_points
        )
        normal_polyline = (
            '<polyline class="normal-line" fill="none" '
            f'points="{points}" />'
        )
    elif not stat.normal_available:
        normal_polyline = (
            f'<text class="note" x="{margin_left}" y="{height - 12}">'
            "样本不足，未显示正态曲线</text>"
        )

    x_axis_y = margin_top + plot_height
    tick_elements = []
    for tick in x_ticks:
        x = scale_x(tick)
        tick_elements.append(
            f'<line class="grid-line vertical-grid" '
            f'x1="{_svg_number(x)}" y1="{margin_top}" '
            f'x2="{_svg_number(x)}" y2="{x_axis_y}" />'
            f'<line class="tick-mark" '
            f'x1="{_svg_number(x)}" y1="{x_axis_y}" '
            f'x2="{_svg_number(x)}" y2="{x_axis_y + 4}" />'
            f'<text class="x-tick-label" '
            f'x="{_svg_number(x)}" y="{height - 22}" '
            f'text-anchor="middle">{_escape(_format_number(tick))}</text>'
        )
    for tick in y_ticks:
        y = scale_y(tick)
        tick_elements.append(
            f'<line class="grid-line horizontal-grid" '
            f'x1="{margin_left}" y1="{_svg_number(y)}" '
            f'x2="{width - margin_right}" y2="{_svg_number(y)}" />'
            f'<line class="tick-mark" '
            f'x1="{margin_left - 4}" y1="{_svg_number(y)}" '
            f'x2="{margin_left}" y2="{_svg_number(y)}" />'
            f'<text class="y-tick-label" '
            f'x="{margin_left - 8}" y="{_svg_number(y + 4)}" '
            f'text-anchor="end">{_escape(_format_number(tick))}</text>'
        )

    sigma_lines = []
    sigma_label_y = margin_top + 14
    for x_value, label, css_class in sigma_markers:
        x = scale_x(x_value)
        sigma_lines.append(
            f'<line class="sigma-line {css_class}" '
            f'x1="{_svg_number(x)}" y1="{margin_top}" '
            f'x2="{_svg_number(x)}" y2="{x_axis_y}" />'
            f'<text class="sigma-label {css_class}" '
            f'x="{_svg_number(x)}" y="{sigma_label_y}" '
            f'text-anchor="middle">{_escape(label)}</text>'
        )

    chart_title = f"{stat.metric_label} 直方图"
    return "".join(
        [
        f'<svg class="chart" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{_escape(chart_title)}">',
            f"<title>{_escape(chart_title)}</title>",
            f"<desc>{_escape(_chart_description(stat))}</desc>",
        f'<line class="axis" x1="{margin_left}" y1="{x_axis_y}" '
            f'x2="{width - margin_right}" y2="{x_axis_y}" />',
            f'<line class="axis" x1="{margin_left}" y1="{margin_top}" '
            f'x2="{margin_left}" y2="{x_axis_y}" />',
            "".join(tick_elements),
            "".join(bars),
            normal_polyline,
            "".join(sigma_lines),
            "</svg>",
        ]
    )


def _render_failures(rows: list[HistoricalMetricValue]) -> str:
    table_rows = [
        [
            row.audio_data_id,
            row.record_date,
            row.product_model,
            row.barcode,
            row.metric_label,
            _status_label(row.status),
            row.error_message,
        ]
        for row in rows
    ]
    return _render_table_section(
        title="失败和跳过记录",
        headers=["记录ID", "时间", "型号", "条码", "指标", "状态", "原因"],
        rows=table_rows,
        empty_text="无失败或跳过记录",
    )


def _render_outliers(rows: list[HistoricalMetricValue]) -> str:
    table_rows = [
        [
            row.audio_data_id,
            row.record_date,
            row.product_model,
            row.channel_label,
            row.metric_label,
            _format_number(row.metric_value),
            row.metric_unit,
        ]
        for row in rows
    ]
    return _render_table_section(
        title="离群记录",
        headers=["记录ID", "时间", "型号", "通道", "指标", "数值", "单位"],
        rows=table_rows,
        empty_text="无离群记录",
    )


def _render_table_section(
    *,
    title: str,
    headers: list[str],
    rows: list[list[object]],
    empty_text: str,
) -> str:
    section_id = f"section-{abs(hash(title))}"
    if not rows:
        return (
            f'<section aria-labelledby="{section_id}">'
            f'<h2 id="{section_id}">{_escape(title)}</h2>'
            f'<p class="empty">{_escape(empty_text)}</p></section>'
        )
    return (
        f'<section aria-labelledby="{section_id}">'
        f'<h2 id="{section_id}">{_escape(title)}</h2>'
        "<table><thead><tr>"
        + "".join(f"<th>{_escape(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>"
            + "".join(f"<td>{_escape(cell)}</td>" for cell in row)
            + "</tr>"
            for row in rows
        )
        + "</tbody></table></section>"
    )


def _failure_rows(result: HistoricalMetricReportResult) -> list[HistoricalMetricValue]:
    rows: list[HistoricalMetricValue] = []
    seen: set[tuple[str, int, str, str, str | None]] = set()
    candidates = list(result.failures) + [
        row for row in result.metric_rows if row.status in {"skipped", "failed"}
    ]
    for row in candidates:
        key = (
            row.audio_data_id,
            row.channel_index,
            row.metric_key,
            row.status,
            row.error_message,
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _histogram_points(stat: MetricGroupStats) -> list[tuple[float, float, float]]:
    if len(stat.histogram_bins) < 2:
        return []
    points = []
    for left, right, count in zip(
        stat.histogram_bins,
        stat.histogram_bins[1:],
        stat.histogram_counts,
    ):
        if not (_is_finite(left) and _is_finite(right) and _is_finite(count)):
            continue
        if float(right) <= float(left):
            continue
        points.append((float(left), float(right), max(0.0, float(count))))
    return points


def _normal_points(stat: MetricGroupStats) -> list[tuple[float, float]]:
    points = []
    for x, y in zip(stat.normal_x, stat.normal_y):
        if _is_finite(x) and _is_finite(y):
            points.append((float(x), max(0.0, float(y))))
    return points


def _sigma_markers(stat: MetricGroupStats) -> list[tuple[float, str, str]]:
    if not (_is_finite(stat.mean) and _is_finite(stat.std)):
        return []
    mean = float(stat.mean)
    std = float(stat.std)
    if std <= 0.0:
        return []
    return [
        (mean, "均值", "sigma-mean"),
        (mean - std, "-1σ", "sigma-1"),
        (mean + std, "+1σ", "sigma-1"),
        (mean - (2.0 * std), "-2σ", "sigma-2"),
        (mean + (2.0 * std), "+2σ", "sigma-2"),
        (mean - (3.0 * std), "-3σ", "sigma-3"),
        (mean + (3.0 * std), "+3σ", "sigma-3"),
    ]


def _nice_ticks(
    min_value: float,
    max_value: float,
    target_count: int,
) -> list[float]:
    if not (_is_finite(min_value) and _is_finite(max_value)):
        return []
    min_value = float(min_value)
    max_value = float(max_value)
    target_count = max(2, int(target_count))
    if min_value > max_value:
        min_value, max_value = max_value, min_value
    if min_value == max_value:
        padding = 0.5 if min_value == 0.0 else abs(min_value) * 0.1
        min_value -= padding
        max_value += padding

    span = max_value - min_value
    step = _nice_step(span / float(target_count - 1))
    if step <= 0.0:
        return [min_value, max_value]

    first = math.floor(min_value / step) * step
    last = math.ceil(max_value / step) * step
    ticks = []
    current = first
    epsilon = step * 1e-9
    while current <= last + epsilon and len(ticks) < 64:
        ticks.append(_clean_tick(current, step))
        current += step
    if len(ticks) < 2:
        return [min_value, max_value]
    return ticks


def _nice_step(raw_step: float) -> float:
    if not _is_finite(raw_step) or raw_step <= 0.0:
        return 0.0
    exponent = math.floor(math.log10(raw_step))
    scale = 10.0 ** exponent
    fraction = raw_step / scale
    if fraction <= 1.0:
        nice_fraction = 1.0
    elif fraction <= 2.0:
        nice_fraction = 2.0
    elif fraction <= 2.5:
        nice_fraction = 2.5
    elif fraction <= 5.0:
        nice_fraction = 5.0
    else:
        nice_fraction = 10.0
    return nice_fraction * scale


def _clean_tick(value: float, step: float) -> float:
    if abs(value) < abs(step) * 1e-9:
        return 0.0
    return round(value, 12)


def _empty_svg(
    width: int,
    height: int,
    message: str,
    stat: MetricGroupStats,
) -> str:
    chart_title = f"{stat.metric_label} 直方图"
    return (
        f'<svg class="chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_escape(chart_title)}">'
        f"<title>{_escape(chart_title)}</title>"
        f'<text class="note" x="24" y="{height // 2}">'
        f"{_escape(message)}</text></svg>"
    )


def _chart_description(stat: MetricGroupStats) -> str:
    return (
        "histogram_bins="
        f"{_number_list_text(stat.histogram_bins)}; "
        "histogram_counts="
        f"{_number_list_text(stat.histogram_counts)}; "
        "normal_x="
        f"{_number_list_text(stat.normal_x)}; "
        "normal_y="
        f"{_number_list_text(stat.normal_y)}"
    )


def _number_list_text(values: Iterable[object]) -> str:
    return "[" + ", ".join(_format_number(value) for value in values) + "]"


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _format_number(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return ""
    return f"{number:.6g}"


def _svg_number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _is_finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _escape(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _css() -> str:
    return """
body {
  margin: 0;
  color: #1f2933;
  background: #f7f8fa;
  font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
  line-height: 1.5;
}
main {
  max-width: 1180px;
  margin: 0 auto;
  padding: 24px;
}
h1 {
  margin: 0 0 18px;
  font-size: 26px;
}
h2 {
  margin: 0 0 12px;
  font-size: 18px;
}
h3 {
  margin: 0 0 8px;
  font-size: 15px;
}
section {
  margin: 0 0 18px;
  padding: 16px;
  background: #ffffff;
  border: 1px solid #d9dee7;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  padding: 7px 8px;
  border: 1px solid #d9dee7;
  text-align: left;
  vertical-align: top;
}
th {
  background: #edf1f5;
  font-weight: 600;
}
.summary-table th {
  width: 160px;
}
.empty,
.note {
  color: #667085;
}
.chart-block {
  margin-top: 14px;
}
.chart {
  display: block;
  width: 100%;
  max-width: 720px;
  height: auto;
  background: #ffffff;
  border: 1px solid #d9dee7;
}
.axis {
  stroke: #73808f;
  stroke-width: 1;
}
.grid-line {
  stroke: #e4e8ef;
  stroke-width: 1;
}
.tick-mark {
  stroke: #73808f;
  stroke-width: 1;
}
.x-tick-label,
.y-tick-label,
.note {
  font-size: 12px;
  fill: #667085;
}
.histogram-bar {
  fill: #4c78a8;
  opacity: 0.72;
}
.normal-line {
  stroke: #d1495b;
  stroke-width: 2;
}
.sigma-line {
  stroke-width: 1;
  stroke-dasharray: 5 4;
}
.sigma-label {
  font-size: 11px;
  font-weight: 600;
}
.sigma-mean {
  stroke: #111827;
  fill: #111827;
}
.sigma-1 {
  stroke: #2f855a;
  fill: #2f855a;
}
.sigma-2 {
  stroke: #b7791f;
  fill: #b7791f;
}
.sigma-3 {
  stroke: #6b46c1;
  fill: #6b46c1;
}
""".strip()
