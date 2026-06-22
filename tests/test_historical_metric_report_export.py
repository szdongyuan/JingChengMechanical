import csv
from io import StringIO

from app.service.historical_metric_report_export import (
    export_csv_detail,
    export_html_report,
)
from app.service.historical_metric_report_types import (
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    MetricGroupStats,
)


def _metric_row(
    *,
    audio_data_id="a1",
    status="ok",
    metric_value=60.0,
    error_message=None,
    product_model="M1",
    barcode="SN-1",
):
    return HistoricalMetricValue(
        audio_data_id=audio_data_id,
        record_date="2026-06-18T10:00:00+08:00",
        product_model=product_model,
        barcode=barcode,
        label="OK",
        label_source="auto",
        analyse_config="Cfg",
        channel_index=0,
        channel_label="CH1",
        metric_key="spl_peak_db",
        metric_label="声压级峰值",
        metric_value=metric_value,
        metric_unit="dB",
        status=status,
        error_message=error_message,
    )


def _stats_row():
    return MetricGroupStats(
        group_key="M1|spl_peak_db|CH1",
        product_model="M1",
        metric_key="spl_peak_db",
        metric_label="声压级峰值",
        metric_unit="dB",
        channel_label="CH1",
        n=5,
        mean=60.0,
        std=1.5,
        min=58.0,
        max=62.0,
        p5=58.4,
        p50=60.0,
        p95=61.6,
        histogram_bins=[58.0, 59.0, 60.0, 61.0, 62.0],
        histogram_counts=[1, 1, 1, 1],
        normal_x=[58.0, 60.0, 62.0],
        normal_y=[0.1, 0.3, 0.1],
        normal_available=True,
    )


def _sample_report_result(metric_rows=None, stats=None, failures=None, outliers=None):
    rows = list(metric_rows or [_metric_row()])
    failure_rows = list(
        failures
        if failures is not None
        else [row for row in rows if row.status in {"skipped", "failed"}]
    )
    return HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=2,
        valid_record_count=1,
        skipped_record_count=sum(1 for row in rows if row.status == "skipped"),
        failed_record_count=sum(1 for row in rows if row.status == "failed"),
        label_counts={"OK": 1, "NG": 0, "not_labeled": 0},
        metric_rows=rows,
        stats=list(stats or [_stats_row()]),
        failures=failure_rows,
        outliers=list(outliers or []),
    )


def test_csv_export_uses_utf8_bom_and_includes_all_status_rows(tmp_path):
    result = _sample_report_result(
        metric_rows=[
            _metric_row(status="ok", metric_value=60.0),
            _metric_row(
                audio_data_id="a2",
                status="skipped",
                metric_value=None,
                error_message="无可用指标",
            ),
            _metric_row(
                audio_data_id="a3",
                status="failed",
                metric_value=None,
                error_message="WAV 文件不存在",
            ),
        ]
    )
    target = tmp_path / "report.csv"

    export_csv_detail(result, str(target))

    content = target.read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")
    text = content.decode("utf-8-sig")
    rows = list(csv.DictReader(StringIO(text)))
    assert [row["status"] for row in rows] == ["ok", "skipped", "failed"]
    assert rows[1]["metric_value"] == ""
    assert rows[1]["error_message"] == "无可用指标"
    assert rows[2]["error_message"] == "WAV 文件不存在"


def test_html_export_is_static_and_contains_summary_and_chart_data(tmp_path):
    failed = _metric_row(
        audio_data_id="a2",
        status="failed",
        metric_value=None,
        error_message="WAV 文件不存在",
    )
    result = _sample_report_result(metric_rows=[_metric_row(), failed])
    target = tmp_path / "report.html"

    export_html_report(result, str(target))

    text = target.read_text(encoding="utf-8")
    assert "测量结果正态分布统计报告" in text
    assert "2026-06-18T10:05:00+08:00" in text
    assert "声压级峰值" in text
    assert "失败" in text
    assert "<svg" in text
    assert "<th>结果</th>" in text
    assert "M1 / CH1 / 声压级峰值" not in text
    assert "M1 CH1 声压级峰值 直方图" not in text
    assert "http://" not in text
    assert "https://" not in text


def test_html_export_escapes_report_content(tmp_path):
    failed = _metric_row(
        audio_data_id="a2",
        status="failed",
        metric_value=None,
        error_message="bad <tag> & file",
        product_model="<script>alert(1)</script>",
        barcode="SN&1",
    )
    result = _sample_report_result(metric_rows=[failed])
    target = tmp_path / "report.html"

    export_html_report(result, str(target))

    text = target.read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "bad &lt;tag&gt; &amp; file" in text
    assert "SN&amp;1" in text
    assert "<script>" not in text
