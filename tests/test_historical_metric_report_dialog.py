from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QDialog,
    QListWidget,
)

from app.service.historical_metric_report_types import (
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    MetricGroupStats,
    ReportCanceled,
)
from ui.popups.historical_metric_report_dialog import (
    HistoricalMetricReportDialog,
    HistoricalReportWorkerThread,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _SignalStub:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self._callbacks):
            callback(*args)


class _AudioDataServiceStub:
    def __init__(self, product_models=None):
        self._product_models = [] if product_models is None else list(product_models)

    def list_product_models(self):
        return list(self._product_models)


class _ReportServiceStub:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def generate_report(self, filters, progress_callback=None, cancel_check=None):
        self.calls.append(dict(filters))
        if progress_callback is not None:
            progress_callback(1, 1, "stub-audio")
        if self.result is not None:
            return self.result
        return HistoricalMetricReportResult(
            filters=dict(filters),
            generated_at="2026-06-18T10:05:00+08:00",
            total_records=0,
            valid_record_count=0,
            skipped_record_count=0,
            failed_record_count=0,
            label_counts={"OK": 0, "NG": 0, "not_labeled": 0},
            metric_rows=[],
            stats=[],
            failures=[],
            outliers=[],
        )


def _dialog(*, product_models=None, report_service=None):
    return HistoricalMetricReportDialog(
        services=SimpleNamespace(
            audio_data_service=_AudioDataServiceStub(product_models=product_models),
            historical_metric_report_service=report_service or _ReportServiceStub(),
        )
    )


def _failure_row(
    *,
    audio_data_id="failed-1",
    barcode="SN-F",
    label="NG",
    status="failed",
    error_message="WAV 文件不存在",
):
    return HistoricalMetricValue(
        audio_data_id=audio_data_id,
        record_date="2026-06-18T10:00:00+08:00",
        product_model="M1",
        barcode=barcode,
        label=label,
        label_source="manual",
        analyse_config="Cfg",
        channel_index=-1,
        channel_label="记录",
        metric_key="__record__",
        metric_label="记录状态",
        metric_value=None,
        metric_unit="",
        status=status,
        error_message=error_message,
    )


def _report_result_with_stats_and_failure():
    failure = _failure_row()
    return HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=3,
        valid_record_count=2,
        skipped_record_count=0,
        failed_record_count=1,
        label_counts={"OK": 2, "NG": 1, "not_labeled": 0},
        metric_rows=[failure],
        stats=[
            MetricGroupStats(
                group_key="全部型号|CH1|spl_peak_db",
                product_model="全部型号",
                metric_key="spl_peak_db",
                metric_label="声压级峰值",
                metric_unit="dB",
                channel_label="CH1",
                n=5,
                mean=60.0,
                std=1.5,
                min=58.0,
                max=62.0,
                p5=58.2,
                p50=60.0,
                p95=61.8,
                histogram_bins=[58.0, 60.0, 62.0],
                histogram_counts=[2, 3],
                normal_x=[58.0, 60.0, 62.0],
                normal_y=[1.0, 3.0, 1.0],
                normal_available=True,
            )
        ],
        failures=[failure],
        outliers=[],
    )


def _stats_row(
    *,
    product_model="所选型号",
    channel_label="全部通道",
    metric_key="spl_peak_db",
    metric_label="声压级峰值",
    metric_unit="dB",
    mean=60.0,
):
    return MetricGroupStats(
        group_key=f"{product_model}|{channel_label}|{metric_key}",
        product_model=product_model,
        metric_key=metric_key,
        metric_label=metric_label,
        metric_unit=metric_unit,
        channel_label=channel_label,
        n=5,
        mean=mean,
        std=1.5,
        min=58.0,
        max=62.0,
        p5=58.2,
        p50=60.0,
        p95=61.8,
        histogram_bins=[58.0, 60.0, 62.0],
        histogram_counts=[2, 3],
        normal_x=[58.0, 60.0, 62.0],
        normal_y=[1.0, 3.0, 1.0],
        normal_available=True,
    )


def test_report_dialog_defaults_to_all_models_no_time_ok_ng(qapp):
    service = _ReportServiceStub()
    dialog = HistoricalMetricReportDialog(
        services=SimpleNamespace(
            audio_data_service=_AudioDataServiceStub(),
            historical_metric_report_service=service,
        )
    )

    assert dialog._selected_product_models == []
    assert dialog.btn_product_models.text() == "全部型号"
    assert dialog.cb_enable_time_range.isChecked() is False
    assert dialog.cb_ok.isChecked() is True
    assert dialog.cb_ng.isChecked() is True
    assert dialog.cb_not_labeled.isChecked() is False


def test_report_dialog_populates_product_models_from_audio_data_service(qapp):
    dialog = HistoricalMetricReportDialog(
        services=SimpleNamespace(
            audio_data_service=_AudioDataServiceStub(product_models=["M1", "M2"]),
            historical_metric_report_service=_ReportServiceStub(),
        )
    )

    assert dialog._available_product_models == ["M1", "M2"]
    assert dialog._selected_product_models == ["M1", "M2"]
    assert dialog.btn_product_models.text() == "已选全部"


def test_report_dialog_product_model_selector_uses_select_all_control(qapp, monkeypatch):
    seen = {}

    def fake_exec(dialog: QDialog):
        model_list = dialog.findChild(QListWidget, "HistoricalReportProductModelList")
        select_all = dialog.findChild(QCheckBox, "HistoricalReportSelectAllModels")
        seen["select_all_text"] = select_all.text()
        seen["select_all_checked"] = select_all.isChecked()
        seen["items"] = [
            model_list.item(row).text()
            for row in range(model_list.count())
        ]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.QDialog.exec",
        fake_exec,
    )
    dialog = _dialog(product_models=["M1", "M2"])

    dialog._open_product_model_selector()

    assert seen == {
        "select_all_text": "全选",
        "select_all_checked": True,
        "items": ["M1", "M2"],
    }


def test_report_dialog_collects_filters_without_selected_rows(qapp):
    dialog = _dialog()

    filters = dialog.current_filters()

    assert filters["product_model"] == "ALL"
    assert filters["product_models"] == []
    assert filters["start_time"] == ""
    assert filters["end_time"] == ""
    assert filters["barcode"] == ""
    assert filters["barcode_regex"] is False
    assert filters["labels"] == ["OK", "NG"]


def test_report_dialog_collects_barcode_regex_filter(qapp):
    dialog = _dialog()
    dialog.ed_barcode.setText(r"^SN-\\d+$")
    dialog.cb_barcode_regex.setChecked(True)

    filters = dialog.current_filters()

    assert filters["barcode"] == r"^SN-\\d+$"
    assert filters["barcode_regex"] is True


def test_report_dialog_collects_select_all_as_no_model_filter(qapp):
    dialog = _dialog(product_models=["M1", "M2"])

    filters = dialog.current_filters()

    assert filters["product_model"] == "ALL"
    assert filters["product_models"] == []


def test_report_dialog_collects_multiple_product_models_before_generation(qapp):
    dialog = _dialog(product_models=["M1", "M2", "M3"])
    dialog._selected_product_models = ["M1", "M3"]
    dialog._update_product_model_button()

    filters = dialog.current_filters()

    assert dialog.btn_product_models.text() == "已选 2 个型号"
    assert filters["product_model"] == ""
    assert filters["product_models"] == ["M1", "M3"]


def test_report_dialog_serializes_enabled_time_range(qapp):
    dialog = _dialog()
    dialog.cb_enable_time_range.setChecked(True)
    dialog.dt_start_time.setDateTime(
        QDateTime.fromString("2026-06-18 10:00:00", "yyyy-MM-dd HH:mm:ss")
    )
    dialog.dt_end_time.setDateTime(
        QDateTime.fromString("2026-06-18 11:00:00", "yyyy-MM-dd HH:mm:ss")
    )

    filters = dialog.current_filters()

    assert filters["start_time"] == "2026-06-18 10:00:00"
    assert filters["end_time"] == "2026-06-18 11:00:00"


def test_report_dialog_render_result_fills_summary_and_stats_table(qapp):
    dialog = _dialog()

    dialog._render_result(_report_result_with_stats_and_failure())

    assert dialog.lb_total_records.text() == "3"
    assert dialog.lb_valid_records.text() == "2"
    assert dialog.lb_failed_records.text() == "1"
    assert dialog.lb_label_counts.text() == "OK 2 / NG 1 / 未标注 0"
    assert dialog.stats_table.rowCount() == 1
    assert dialog.stats_table.item(0, 0).text() == "声压级峰值"
    assert dialog.stats_table.item(0, 1).text() == "5"
    assert dialog.stats_table.item(0, 2).text() == "60.000"


def test_report_dialog_stats_table_selects_result_plot(qapp):
    first = _stats_row(metric_label="声压级峰值", metric_key="spl_peak_db")
    second = _stats_row(
        metric_key="loudness_average_sone",
        metric_label="平均响度",
        metric_unit="sone",
        mean=2.0,
    )
    result = HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=5,
        valid_record_count=5,
        skipped_record_count=0,
        failed_record_count=0,
        label_counts={"OK": 5, "NG": 0, "not_labeled": 0},
        metric_rows=[],
        stats=[first, second],
        failures=[],
        outliers=[],
    )
    dialog = _dialog()
    rendered = []
    dialog._render_plot = lambda stat: rendered.append(stat.metric_label)

    dialog._render_result(result)

    assert not hasattr(dialog, "chart_list")
    assert dialog.stats_table.rowCount() == 2
    assert dialog.stats_table.item(0, 0).text() == "声压级峰值"
    assert dialog.stats_table.item(1, 0).text() == "平均响度"
    assert rendered[-1] == "声压级峰值"

    dialog.stats_table.selectRow(1)

    assert rendered[-1] == "平均响度"


def test_report_dialog_calendar_popup_keeps_date_numbers_readable(qapp):
    dialog = _dialog()
    calendar = dialog.dt_start_time.calendarWidget()

    assert calendar.minimumWidth() >= 420
    assert calendar.minimumHeight() >= 320
    assert (
        calendar.horizontalHeaderFormat()
        == QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames
    )
    assert "selection-color: #111827" in calendar.styleSheet()


def test_report_dialog_failures_table_shows_reasons(qapp):
    dialog = _dialog()

    dialog._render_result(_report_result_with_stats_and_failure())

    assert dialog.failures_table.rowCount() == 1
    assert dialog.failures_table.item(0, 0).text() == "failed-1"
    assert dialog.failures_table.item(0, 5).text() == "failed"
    assert dialog.failures_table.item(0, 6).text() == "WAV 文件不存在"


def test_report_dialog_export_calls_html_and_csv_exporters(qapp, monkeypatch, tmp_path):
    calls = []
    html_path = tmp_path / "historical-report.html"
    dialog = _dialog()
    result = _report_result_with_stats_and_failure()
    result.stats.append(
        _stats_row(
            metric_key="loudness_average_sone",
            metric_label="平均响度",
            metric_unit="sone",
            mean=2.0,
        )
    )
    dialog._render_result(result)

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(html_path), "HTML"),
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.export_html_report",
        lambda result, path: calls.append(("html", result, path)) or path,
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.export_csv_detail",
        lambda result, path: calls.append(("csv", result, path)) or path,
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.QMessageBox.information",
        lambda *args, **kwargs: None,
    )

    dialog.btn_export.click()

    assert [(kind, path) for kind, _result, path in calls] == [
        ("html", str(html_path)),
        ("csv", str(tmp_path / "historical-report.csv")),
    ]
    assert calls[0][1] is calls[1][1]
    assert [stat.metric_label for stat in calls[0][1].stats] == [
        "声压级峰值",
        "平均响度",
    ]


def test_report_worker_emits_canceled_for_report_canceled(qapp):
    class CancelingService:
        def generate_report(self, *args, **kwargs):
            raise ReportCanceled()

    worker = HistoricalReportWorkerThread(CancelingService(), {"labels": ["OK", "NG"]})
    emissions = []
    worker.canceled.connect(lambda: emissions.append("canceled"))

    worker.run()

    assert emissions == ["canceled"]


def test_report_dialog_generate_starts_worker_and_updates_progress(qapp, monkeypatch):
    starts = []

    class FakeWorker:
        def __init__(self, report_service, filters):
            self.report_service = report_service
            self.filters = filters
            self.progress_changed = _SignalStub()
            self.succeeded = _SignalStub()
            self.failed = _SignalStub()
            self.canceled = _SignalStub()

        def start(self):
            starts.append(self.filters)
            self.progress_changed.emit(1, 3, "audio-1")

        def isRunning(self):
            return True

        def requestInterruption(self):
            pass

        def deleteLater(self):
            pass

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.HistoricalReportWorkerThread",
        FakeWorker,
    )
    dialog = _dialog()

    dialog.btn_generate.click()

    assert starts and starts[0]["labels"] == ["OK", "NG"]
    assert "1 / 3" in dialog.lb_progress.text()
    assert "audio-1" in dialog.lb_progress.text()


def test_report_dialog_rejects_invalid_barcode_regex_before_generation(
    qapp, monkeypatch
):
    starts = []
    warnings = []

    class FakeWorker:
        def __init__(self, report_service, filters):
            self.progress_changed = _SignalStub()
            self.succeeded = _SignalStub()
            self.failed = _SignalStub()
            self.canceled = _SignalStub()

        def start(self):
            starts.append(True)

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.HistoricalReportWorkerThread",
        FakeWorker,
    )
    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )
    dialog = _dialog()
    dialog.ed_barcode.setText("[")
    dialog.cb_barcode_regex.setChecked(True)

    dialog.btn_generate.click()

    assert starts == []
    assert warnings
    assert "SN 正则表达式无效" in warnings[0][2]
    assert dialog._worker is None


def test_report_dialog_cancel_requests_worker_and_restores_controls(qapp, monkeypatch):
    interrupted = []
    workers = []

    class FakeWorker:
        def __init__(self, report_service, filters):
            self.progress_changed = _SignalStub()
            self.succeeded = _SignalStub()
            self.failed = _SignalStub()
            self.canceled = _SignalStub()
            workers.append(self)

        def start(self):
            pass

        def isRunning(self):
            return True

        def requestInterruption(self):
            interrupted.append(True)

        def deleteLater(self):
            pass

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.HistoricalReportWorkerThread",
        FakeWorker,
    )
    dialog = _dialog()

    dialog.btn_generate.click()
    dialog.btn_cancel.click()
    workers[0].canceled.emit()

    assert interrupted == [True]
    assert dialog.btn_generate.isEnabled() is True
    assert dialog.btn_cancel.isEnabled() is False


def test_report_dialog_close_defers_until_running_worker_is_cleaned_up(
    qapp, monkeypatch
):
    workers = []

    class FakeWorker:
        def __init__(self, report_service, filters):
            self.progress_changed = _SignalStub()
            self.succeeded = _SignalStub()
            self.failed = _SignalStub()
            self.canceled = _SignalStub()
            self.finished = _SignalStub()
            self.interruption_requests = 0
            self.deleted = False
            self._running = True
            workers.append(self)

        def start(self):
            pass

        def isRunning(self):
            return self._running

        def requestInterruption(self):
            self.interruption_requests += 1

        def deleteLater(self):
            self.deleted = True

    monkeypatch.setattr(
        "ui.popups.historical_metric_report_dialog.HistoricalReportWorkerThread",
        FakeWorker,
    )
    dialog = _dialog()
    dialog.show()
    qapp.processEvents()

    dialog.btn_generate.click()

    assert dialog.close() is False
    assert dialog.isVisible() is True
    assert workers[0].interruption_requests == 1
    assert dialog.btn_cancel.isEnabled() is False

    workers[0].canceled.emit()
    workers[0]._running = False
    workers[0].finished.emit()
    qapp.processEvents()

    assert dialog._worker is None
    assert workers[0].deleted is True
    assert dialog.isVisible() is False


def test_report_dialog_empty_result_shows_failures_and_allows_export(qapp):
    failure = _failure_row(
        audio_data_id="a1",
        barcode="SN-1",
        label="OK",
        status="failed",
        error_message="WAV 文件不存在",
    )
    result = HistoricalMetricReportResult(
        filters={"product_model": "ALL", "labels": ["OK", "NG"]},
        generated_at="2026-06-18T10:05:00+08:00",
        total_records=1,
        valid_record_count=0,
        skipped_record_count=0,
        failed_record_count=1,
        label_counts={"OK": 1, "NG": 0, "not_labeled": 0},
        metric_rows=[failure],
        stats=[],
        failures=[failure],
        outliers=[],
    )
    dialog = _dialog()

    dialog._render_result(result)

    assert dialog.stats_table.rowCount() == 0
    assert dialog.failures_table.rowCount() == 1
    assert dialog.btn_export.isEnabled() is True
