from __future__ import annotations

import math
from pathlib import Path
import re

import pyqtgraph as pg
from PyQt6.QtCore import QDateTime, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.service.historical_metric_report_export import (
    export_csv_detail,
    export_html_report,
)
from app.service.historical_metric_report_types import (
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    MetricGroupStats,
    ReportCanceled,
)


class HistoricalReportWorkerThread(QThread):
    progress_changed = pyqtSignal(int, int, str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    canceled = pyqtSignal()

    def __init__(self, report_service, filters):
        super().__init__()
        self._report_service = report_service
        self._filters = dict(filters)

    def run(self):
        try:
            result = self._report_service.generate_report(
                self._filters,
                progress_callback=self.progress_changed.emit,
                cancel_check=self.isInterruptionRequested,
            )
        except ReportCanceled:
            self.canceled.emit()
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class HistoricalMetricReportDialog(QDialog):
    def __init__(self, *, services, parent=None):
        super().__init__(parent)
        self.services = services
        self._current_result: HistoricalMetricReportResult | None = None
        self._worker: HistoricalReportWorkerThread | None = None
        self._deferred_close_requested = False
        self._worker_terminal_signal_received = False
        self._worker_finished_signal_received = False
        self._stats_by_row: list[MetricGroupStats] = []
        self._available_product_models: list[str] = []
        self._selected_product_models: list[str] = []

        self.setWindowTitle("测量结果正态分布统计报告")
        self.resize(1180, 760)
        self.setModal(False)

        self._init_ui()
        self._load_product_models()
        self._set_running_state(False)
        self._render_empty_state()

    def current_filters(self) -> dict[str, object]:
        labels: list[str] = []
        if self.cb_ok.isChecked():
            labels.append("OK")
        if self.cb_ng.isChecked():
            labels.append("NG")
        if self.cb_not_labeled.isChecked():
            labels.append("not_labeled")

        return {
            "product_model": self._legacy_product_model_filter(),
            "product_models": self._product_models_filter(),
            "start_time": (
                self.dt_start_time.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                if self.cb_enable_time_range.isChecked()
                else ""
            ),
            "end_time": (
                self.dt_end_time.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                if self.cb_enable_time_range.isChecked()
                else ""
            ),
            "barcode": self.ed_barcode.text().strip(),
            "barcode_regex": self.cb_barcode_regex.isChecked(),
            "labels": labels,
        }

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        filter_bar = QWidget(self)
        filter_layout = QHBoxLayout(filter_bar)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)

        self.btn_product_models = QPushButton("全部型号", self)
        self.btn_product_models.setObjectName("HistoricalReportProductModelSelector")
        self.btn_product_models.setMinimumWidth(130)
        self.btn_product_models.clicked.connect(self._open_product_model_selector)
        filter_layout.addWidget(QLabel("型号", self))
        filter_layout.addWidget(self.btn_product_models)

        self.cb_enable_time_range = QCheckBox("时间", self)
        filter_layout.addWidget(self.cb_enable_time_range)

        self.dt_start_time = QDateTimeEdit(self)
        self.dt_end_time = QDateTimeEdit(self)
        for edit in (self.dt_start_time, self.dt_end_time):
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            edit.setCalendarPopup(True)
            edit.setDateTime(QDateTime.currentDateTime())
            edit.setEnabled(False)
            edit.setMinimumWidth(170)
            self._setup_calendar_popup(edit)
        self.cb_enable_time_range.toggled.connect(self.dt_start_time.setEnabled)
        self.cb_enable_time_range.toggled.connect(self.dt_end_time.setEnabled)
        filter_layout.addWidget(self.dt_start_time)
        filter_layout.addWidget(QLabel("-", self))
        filter_layout.addWidget(self.dt_end_time)

        self.ed_barcode = QLineEdit(self)
        self.ed_barcode.setPlaceholderText("SN码")
        self.ed_barcode.setMinimumWidth(130)
        filter_layout.addWidget(QLabel("SN码", self))
        filter_layout.addWidget(self.ed_barcode)
        self.cb_barcode_regex = QCheckBox("正则", self)
        self.cb_barcode_regex.setObjectName("HistoricalReportBarcodeRegex")
        self.cb_barcode_regex.setToolTip("勾选后 SN 码按正则表达式匹配")
        filter_layout.addWidget(self.cb_barcode_regex)

        self.cb_ok = QCheckBox("OK", self)
        self.cb_ng = QCheckBox("NG", self)
        self.cb_not_labeled = QCheckBox("未标注", self)
        self.cb_ok.setChecked(True)
        self.cb_ng.setChecked(True)
        self.cb_not_labeled.setChecked(False)
        filter_layout.addWidget(self.cb_ok)
        filter_layout.addWidget(self.cb_ng)
        filter_layout.addWidget(self.cb_not_labeled)
        filter_layout.addStretch(1)

        self.btn_generate = QPushButton("生成", self)
        self.btn_generate.setObjectName("GenerateHistoricalReportButton")
        self.btn_generate.clicked.connect(self._generate_report)
        filter_layout.addWidget(self.btn_generate)

        self.btn_cancel = QPushButton("取消", self)
        self.btn_cancel.setObjectName("CancelHistoricalReportButton")
        self.btn_cancel.clicked.connect(self._cancel_generation)
        filter_layout.addWidget(self.btn_cancel)

        self.btn_export = QPushButton("导出", self)
        self.btn_export.setObjectName("ExportHistoricalReportButton")
        self.btn_export.clicked.connect(self._export_report)
        filter_layout.addWidget(self.btn_export)

        root.addWidget(filter_bar)

        summary = QWidget(self)
        summary_layout = QGridLayout(summary)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setHorizontalSpacing(16)
        summary_layout.setVerticalSpacing(4)

        self.lb_total_records = QLabel("0", self)
        self.lb_total_records.setObjectName("HistoricalReportTotalRecords")
        self.lb_valid_records = QLabel("0", self)
        self.lb_valid_records.setObjectName("HistoricalReportValidRecords")
        self.lb_skipped_records = QLabel("0", self)
        self.lb_skipped_records.setObjectName("HistoricalReportSkippedRecords")
        self.lb_failed_records = QLabel("0", self)
        self.lb_failed_records.setObjectName("HistoricalReportFailedRecords")
        self.lb_label_counts = QLabel("OK 0 / NG 0 / 未标注 0", self)
        self.lb_label_counts.setObjectName("HistoricalReportLabelCounts")
        self.lb_progress = QLabel("就绪", self)
        self.lb_progress.setObjectName("HistoricalReportProgressLabel")

        self._add_summary_value(summary_layout, 0, "匹配记录", self.lb_total_records)
        self._add_summary_value(summary_layout, 1, "有效记录", self.lb_valid_records)
        self._add_summary_value(summary_layout, 2, "跳过记录", self.lb_skipped_records)
        self._add_summary_value(summary_layout, 3, "失败记录", self.lb_failed_records)
        self._add_summary_value(summary_layout, 4, "标签", self.lb_label_counts)
        self._add_summary_value(summary_layout, 5, "进度", self.lb_progress)
        summary_layout.setColumnStretch(12, 1)
        root.addWidget(summary)

        main_splitter = QSplitter(Qt.Orientation.Vertical, self)
        top_splitter = QSplitter(Qt.Orientation.Horizontal, main_splitter)

        selector_host = QWidget(self)
        selector_layout = QVBoxLayout(selector_host)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(6)

        table_title = QLabel("结果选择", self)
        table_title.setObjectName("HistoricalReportStatsTableTitle")
        selector_layout.addWidget(table_title)

        self.stats_table = self._make_table(
            [
                "结果",
                "样本数",
                "均值",
                "标准差",
                "P5",
                "P50",
                "P95",
            ],
            "HistoricalReportStatsTable",
        )
        self.stats_table.itemSelectionChanged.connect(self._on_stats_selection_changed)
        selector_layout.addWidget(self.stats_table, 1)
        top_splitter.addWidget(selector_host)

        chart_host = QWidget(self)
        chart_layout = QVBoxLayout(chart_host)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(6)
        self.chart_state_label = QLabel("无可用统计结果", self)
        self.chart_state_label.setObjectName("HistoricalReportChartStateLabel")
        self.chart_state_label.setWordWrap(True)
        self.plot_widget = pg.PlotWidget(self)
        self.plot_widget.setObjectName("HistoricalReportHistogramPlot")
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.18)
        self.plot_widget.setLabel("bottom", "数值")
        self.plot_widget.setLabel("left", "数量")
        chart_layout.addWidget(self.chart_state_label)
        chart_layout.addWidget(self.plot_widget, 1)
        top_splitter.addWidget(chart_host)
        top_splitter.setSizes([560, 560])

        bottom_tabs = QTabWidget(self)
        self.failures_table = self._make_table(
            ["记录ID", "时间", "型号", "条码", "指标", "状态", "原因"],
            "HistoricalReportFailuresTable",
        )
        self.outliers_table = self._make_table(
            ["记录ID", "时间", "型号", "通道", "指标", "数值", "单位"],
            "HistoricalReportOutliersTable",
        )
        bottom_tabs.addTab(self.failures_table, "失败和跳过")
        bottom_tabs.addTab(self.outliers_table, "离群记录")

        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(bottom_tabs)
        main_splitter.setSizes([470, 230])
        root.addWidget(main_splitter, 1)

    def _load_product_models(self) -> None:
        try:
            product_models = self.services.audio_data_service.list_product_models()
        except Exception:
            product_models = []
        self._available_product_models = [
            str(model).strip() for model in product_models if str(model).strip()
        ]
        self._selected_product_models = list(self._available_product_models)
        self._update_product_model_button()

    def _legacy_product_model_filter(self) -> str:
        if not self._selected_product_models:
            return "ALL"
        if self._all_product_models_selected():
            return "ALL"
        if len(self._selected_product_models) == 1:
            return self._selected_product_models[0]
        return ""

    def _product_models_filter(self) -> list[str]:
        if not self._selected_product_models or self._all_product_models_selected():
            return []
        return list(self._selected_product_models)

    def _all_product_models_selected(self) -> bool:
        return bool(self._available_product_models) and set(
            self._selected_product_models
        ) == set(self._available_product_models)

    def _update_product_model_button(self) -> None:
        selected = list(self._selected_product_models)
        if not self._available_product_models:
            text = "全部型号"
        elif not selected:
            text = "未选择型号"
        elif self._all_product_models_selected():
            text = "已选全部"
        elif len(selected) == 1:
            text = selected[0]
        else:
            text = f"已选 {len(selected)} 个型号"
        self.btn_product_models.setText(text)

    def _open_product_model_selector(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("选择型号")
        dialog.resize(360, 420)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        model_list = QListWidget(dialog)
        model_list.setObjectName("HistoricalReportProductModelList")
        model_list.setAlternatingRowColors(True)
        model_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        selected = set(self._selected_product_models)

        for model in self._available_product_models:
            item = QListWidgetItem(model)
            item.setData(Qt.ItemDataRole.UserRole, model)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                Qt.CheckState.Checked if model in selected else Qt.CheckState.Unchecked
            )
            model_list.addItem(item)

        cb_select_all = QCheckBox("全选", dialog)
        cb_select_all.setObjectName("HistoricalReportSelectAllModels")

        syncing_model_checks = False

        def set_all_model_checks(state: Qt.CheckState) -> None:
            nonlocal syncing_model_checks
            syncing_model_checks = True
            try:
                for item_row in range(model_list.count()):
                    model_list.item(item_row).setCheckState(state)
            finally:
                syncing_model_checks = False

        def sync_select_all_check() -> None:
            nonlocal syncing_model_checks
            if syncing_model_checks:
                return
            syncing_model_checks = True
            try:
                all_checked = bool(model_list.count()) and all(
                    model_list.item(item_row).checkState() == Qt.CheckState.Checked
                    for item_row in range(model_list.count())
                )
                cb_select_all.setChecked(all_checked)
            finally:
                syncing_model_checks = False

        def on_select_all_toggled(checked: bool) -> None:
            if syncing_model_checks:
                return
            set_all_model_checks(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )

        def toggle_model_item(item: QListWidgetItem) -> None:
            item.setCheckState(
                Qt.CheckState.Unchecked
                if item.checkState() == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )

        cb_select_all.setEnabled(model_list.count() > 0)
        cb_select_all.toggled.connect(on_select_all_toggled)
        model_list.itemChanged.connect(lambda _item: sync_select_all_check())
        model_list.itemDoubleClicked.connect(toggle_model_item)
        sync_select_all_check()

        layout.addWidget(cb_select_all)
        layout.addWidget(model_list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        chosen: list[str] = []
        for row in range(model_list.count()):
            item = model_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                chosen.append(str(item.data(Qt.ItemDataRole.UserRole)))
        if not chosen:
            QMessageBox.information(self, "提示", "请至少选择一个型号")
            return
        self._selected_product_models = chosen
        self._update_product_model_button()

    @staticmethod
    def _setup_calendar_popup(edit: QDateTimeEdit) -> None:
        calendar = edit.calendarWidget()
        if not calendar:
            return
        calendar.setMinimumSize(420, 320)
        calendar.setGridVisible(True)
        calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        calendar.setHorizontalHeaderFormat(
            QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames
        )
        calendar.setStyleSheet(
            """
            QCalendarWidget QWidget {
                background: #ffffff;
                color: #111827;
            }
            QCalendarWidget QToolButton {
                min-width: 72px;
                background: #ffffff;
                color: #111827;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QCalendarWidget QMenu {
                background: #ffffff;
                color: #111827;
            }
            QCalendarWidget QSpinBox {
                min-width: 80px;
                color: #111827;
                background: #ffffff;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            QCalendarWidget QAbstractItemView:enabled {
                min-width: 320px;
                min-height: 220px;
                background: #ffffff;
                color: #111827;
                selection-background-color: #dbeafe;
                selection-color: #111827;
                alternate-background-color: #ffffff;
            }
            """
        )

    def _generate_report(self) -> None:
        if self._worker is not None:
            self._maybe_cleanup_worker()
        if self._worker is not None:
            return
        filters = self.current_filters()
        if not self._validate_filters(filters):
            return
        self.lb_progress.setText("正在生成...")
        self._set_running_state(True)
        self._deferred_close_requested = False
        self._worker_terminal_signal_received = False
        self._worker_finished_signal_received = False
        self._worker = HistoricalReportWorkerThread(
            self.services.historical_metric_report_service,
            filters,
        )
        self._worker.progress_changed.connect(self._on_progress_changed)
        self._worker.succeeded.connect(self._on_report_succeeded)
        self._worker.failed.connect(self._on_report_failed)
        self._worker.canceled.connect(self._on_report_canceled)
        if hasattr(self._worker, "finished"):
            self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _validate_filters(self, filters: dict[str, object]) -> bool:
        if not filters.get("barcode_regex"):
            return True
        barcode = str(filters.get("barcode") or "").strip()
        if not barcode:
            return True
        try:
            re.compile(barcode)
        except re.error as exc:
            QMessageBox.warning(self, "筛选条件无效", f"SN 正则表达式无效: {exc}")
            return False
        return True

    def _cancel_generation(self) -> None:
        if self._worker is None:
            self._set_running_state(False)
            return
        if self._worker.isRunning():
            self._worker.requestInterruption()
            self.btn_cancel.setEnabled(False)
            self.lb_progress.setText("正在取消...")
        else:
            self._set_running_state(False)

    def _on_progress_changed(self, done: int, total: int, audio_data_id: str) -> None:
        self.lb_progress.setText(f"{done} / {total}  {audio_data_id}")

    def _on_report_succeeded(self, result: HistoricalMetricReportResult) -> None:
        self._render_result(result)
        self.lb_progress.setText("生成完成")
        self._finish_generation()

    def _on_report_failed(self, message: str) -> None:
        self.lb_progress.setText("生成失败")
        if not self._deferred_close_requested:
            QMessageBox.warning(self, "生成失败", message)
        self._finish_generation()

    def _on_report_canceled(self) -> None:
        self.lb_progress.setText("已取消")
        self._finish_generation()

    def _finish_generation(self) -> None:
        self._worker_terminal_signal_received = True
        self._set_running_state(False)
        self._maybe_cleanup_worker()

    def _on_worker_finished(self) -> None:
        self._worker_finished_signal_received = True
        self._maybe_cleanup_worker()

    def _maybe_cleanup_worker(self) -> None:
        worker = self._worker
        if worker is None:
            return
        has_finished_signal = hasattr(worker, "finished")
        if has_finished_signal and not (
            self._worker_terminal_signal_received
            and self._worker_finished_signal_received
        ):
            return
        if (not has_finished_signal) and not self._worker_terminal_signal_received:
            return
        self._cleanup_worker()

    def _cleanup_worker(self) -> None:
        worker = self._worker
        close_after_cleanup = self._deferred_close_requested
        self._worker = None
        self._deferred_close_requested = False
        self._worker_terminal_signal_received = False
        self._worker_finished_signal_received = False
        if worker is not None and hasattr(worker, "deleteLater"):
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        if close_after_cleanup:
            self.close()

    def _render_result(self, result: HistoricalMetricReportResult) -> None:
        self._current_result = result
        self.lb_total_records.setText(str(result.total_records))
        self.lb_valid_records.setText(str(result.valid_record_count))
        self.lb_skipped_records.setText(str(result.skipped_record_count))
        self.lb_failed_records.setText(str(result.failed_record_count))
        self.lb_label_counts.setText(
            "OK {ok} / NG {ng} / 未标注 {unlabeled}".format(
                ok=result.label_counts.get("OK", 0),
                ng=result.label_counts.get("NG", 0),
                unlabeled=result.label_counts.get("not_labeled", 0),
            )
        )
        self._render_stats_table(result.stats)
        self._render_metric_rows(self.failures_table, self._failure_rows(result))
        self._render_metric_rows(self.outliers_table, result.outliers, outliers=True)
        self.btn_export.setEnabled(True)

        if self._stats_by_row:
            self.stats_table.selectRow(0)
            self._render_plot(self._stats_by_row[0])
        else:
            self._render_empty_chart("无可用统计结果")

    def _render_empty_state(self) -> None:
        empty = HistoricalMetricReportResult(
            filters={},
            generated_at="",
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
        self._render_result(empty)
        self._current_result = None
        self.btn_export.setEnabled(False)
        self.lb_progress.setText("就绪")

    def _render_stats_table(self, stats: list[MetricGroupStats]) -> None:
        self._stats_by_row = list(stats)
        self.stats_table.setSortingEnabled(False)
        self.stats_table.clearSelection()
        self.stats_table.setRowCount(len(self._stats_by_row))
        for row_index, stat in enumerate(self._stats_by_row):
            values = [
                stat.metric_label,
                stat.n,
                self._format_number(stat.mean, digits=3),
                self._format_number(stat.std, digits=3),
                self._format_number(stat.p5, digits=3),
                self._format_number(stat.p50, digits=3),
                self._format_number(stat.p95, digits=3),
            ]
            for col_index, value in enumerate(values):
                self.stats_table.setItem(
                    row_index,
                    col_index,
                    QTableWidgetItem(str(value)),
                )
        self.stats_table.resizeColumnsToContents()

    def _render_metric_rows(
        self,
        table: QTableWidget,
        rows: list[HistoricalMetricValue],
        *,
        outliers: bool = False,
    ) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            if outliers:
                values = [
                    row.audio_data_id,
                    row.record_date,
                    row.product_model,
                    row.channel_label,
                    row.metric_label,
                    self._format_number(row.metric_value, digits=3),
                    row.metric_unit,
                ]
            else:
                values = [
                    row.audio_data_id,
                    row.record_date,
                    row.product_model,
                    row.barcode or "",
                    row.metric_label,
                    row.status,
                    row.error_message or "",
                ]
            for col_index, value in enumerate(values):
                table.setItem(row_index, col_index, QTableWidgetItem(str(value)))
        table.resizeColumnsToContents()

    def _on_stats_selection_changed(self) -> None:
        row = self.stats_table.currentRow()
        if 0 <= row < len(self._stats_by_row):
            self._render_plot(self._stats_by_row[row])

    def _render_plot(self, stat: MetricGroupStats) -> None:
        self.plot_widget.clear()
        self.plot_widget.setLabel("bottom", stat.metric_label, units=stat.metric_unit)
        self.plot_widget.setLabel("left", "数量")

        histogram = self._histogram_points(stat)
        if histogram:
            centers = [(left + right) / 2.0 for left, right, _count in histogram]
            widths = [right - left for left, right, _count in histogram]
            counts = [count for _left, _right, count in histogram]
            self.plot_widget.addItem(
                pg.BarGraphItem(
                    x=centers,
                    height=counts,
                    width=widths,
                    brush=pg.mkBrush("#4c78a8"),
                    pen=pg.mkPen("#345f89"),
                )
            )
        if stat.normal_available and stat.normal_x and stat.normal_y:
            points = self._normal_points(stat)
            if points:
                x_values = [x for x, _y in points]
                y_values = [y for _x, y in points]
                self.plot_widget.plot(
                    x_values,
                    y_values,
                    pen=pg.mkPen(color="#d1495b", width=2),
                )
        self._add_sigma_markers(stat)

        if not histogram:
            self.chart_state_label.setText("无可用直方图数据")
        elif not stat.normal_available:
            self.chart_state_label.setText("样本不足，未显示正态曲线")
        else:
            self.chart_state_label.setText("")
        self.plot_widget.enableAutoRange()

    def _render_empty_chart(self, message: str) -> None:
        self.plot_widget.clear()
        self.chart_state_label.setText(message)

    def _add_sigma_markers(self, stat: MetricGroupStats) -> None:
        markers = self._sigma_markers(stat)
        if not markers:
            return
        y_top = self._sigma_label_y(stat)
        for marker in markers:
            line = pg.InfiniteLine(
                pos=marker["x"],
                angle=90,
                movable=False,
                pen=pg.mkPen(
                    color=marker["color"],
                    width=1,
                    style=Qt.PenStyle.DashLine,
                ),
            )
            self.plot_widget.addItem(line)
            label = pg.TextItem(
                marker["label"],
                color=marker["color"],
                anchor=(0.5, 1.0),
            )
            label.setPos(marker["x"], y_top)
            self.plot_widget.addItem(label)

    def _export_report(self) -> None:
        if self._current_result is None:
            QMessageBox.information(self, "提示", "请先生成报告")
            return
        html_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出统计报告",
            "historical_metric_report.html",
            "HTML 文件 (*.html)",
        )
        if not html_path:
            return
        if not html_path.lower().endswith((".html", ".htm")):
            html_path += ".html"
        csv_path = str(Path(html_path).with_suffix(".csv"))
        try:
            export_html_report(self._current_result, html_path)
            export_csv_detail(self._current_result, csv_path)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", f"导出失败: {exc}")
            return
        QMessageBox.information(
            self,
            "导出完成",
            f"已导出报告:\n{html_path}\n{csv_path}",
        )

    def _set_running_state(self, running: bool) -> None:
        self.btn_generate.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.btn_export.setEnabled((not running) and self._current_result is not None)

    def _add_summary_value(
        self,
        layout: QGridLayout,
        column_pair: int,
        label_text: str,
        value_label: QLabel,
    ) -> None:
        label = QLabel(label_text, self)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_label.setMinimumWidth(54)
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label, 0, column_pair * 2)
        layout.addWidget(value_label, 0, column_pair * 2 + 1)

    def _make_table(self, headers: list[str], object_name: str) -> QTableWidget:
        table = QTableWidget(0, len(headers), self)
        table.setObjectName(object_name)
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        for index in range(len(headers)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
        return table

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._deferred_close_requested = True
            if self._worker_is_running(self._worker):
                self._worker.requestInterruption()
                self.lb_progress.setText("正在取消...")
            self.btn_generate.setEnabled(False)
            self.btn_cancel.setEnabled(False)
            self.btn_export.setEnabled(False)
            event.ignore()
            return
        super().closeEvent(event)

    @staticmethod
    def _worker_is_running(worker) -> bool:
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    @staticmethod
    def _failure_rows(
        result: HistoricalMetricReportResult,
    ) -> list[HistoricalMetricValue]:
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

    @staticmethod
    def _histogram_points(stat: MetricGroupStats) -> list[tuple[float, float, float]]:
        points: list[tuple[float, float, float]] = []
        if len(stat.histogram_bins) < 2:
            return points
        for left, right, count in zip(
            stat.histogram_bins,
            stat.histogram_bins[1:],
            stat.histogram_counts,
        ):
            if not (
                HistoricalMetricReportDialog._is_finite(left)
                and HistoricalMetricReportDialog._is_finite(right)
                and HistoricalMetricReportDialog._is_finite(count)
            ):
                continue
            left_value = float(left)
            right_value = float(right)
            if right_value <= left_value:
                continue
            points.append((left_value, right_value, max(0.0, float(count))))
        return points

    @staticmethod
    def _normal_points(stat: MetricGroupStats) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for x_value, y_value in zip(stat.normal_x, stat.normal_y):
            if HistoricalMetricReportDialog._is_finite(
                x_value
            ) and HistoricalMetricReportDialog._is_finite(y_value):
                points.append((float(x_value), max(0.0, float(y_value))))
        return points

    @staticmethod
    def _sigma_markers(stat: MetricGroupStats) -> list[dict[str, object]]:
        if not (
            HistoricalMetricReportDialog._is_finite(stat.mean)
            and HistoricalMetricReportDialog._is_finite(stat.std)
        ):
            return []
        mean = float(stat.mean)
        std = float(stat.std)
        if std <= 0.0:
            return []
        return [
            {"x": mean, "label": "均值", "color": "#111827"},
            {"x": mean - std, "label": "-1σ", "color": "#2f855a"},
            {"x": mean + std, "label": "+1σ", "color": "#2f855a"},
            {"x": mean - (2.0 * std), "label": "-2σ", "color": "#b7791f"},
            {"x": mean + (2.0 * std), "label": "+2σ", "color": "#b7791f"},
            {"x": mean - (3.0 * std), "label": "-3σ", "color": "#6b46c1"},
            {"x": mean + (3.0 * std), "label": "+3σ", "color": "#6b46c1"},
        ]

    @staticmethod
    def _sigma_label_y(stat: MetricGroupStats) -> float:
        y_values: list[float] = []
        y_values.extend(float(value) for value in stat.histogram_counts if value >= 0)
        y_values.extend(
            float(value)
            for value in stat.normal_y
            if HistoricalMetricReportDialog._is_finite(value) and float(value) >= 0.0
        )
        if not y_values:
            return 1.0
        return max(y_values) * 0.96

    @staticmethod
    def _format_number(value: object, *, digits: int) -> str:
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
        return f"{number:.{digits}f}"

    @staticmethod
    def _is_finite(value: object) -> bool:
        if isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False


__all__ = ["HistoricalMetricReportDialog", "HistoricalReportWorkerThread"]
