from __future__ import annotations

import os
from datetime import datetime

import sounddevice as sd
from scipy.io import wavfile
from PyQt6.QtCore import QDateTime, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from consts.running_consts import base_dir
from base.utils import format_datetime_display, normalize_path


class AudioDataFilterDialog(QDialog):
    def __init__(
        self,
        *,
        product_models: list[str],
        current_filters: dict[str, object],
        min_time: str | None,
        max_time: str | None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("条件筛选")
        self.resize(360, 220)
        self.setStyleSheet(
            """
            QDialog {
                background: #f8fafc;
            }
            QLabel {
                color: #111827;
            }
            QCheckBox {
                color: #111827;
            }
            QCalendarWidget QWidget {
                background: #ffffff;
                color: #111827;
            }
            QCalendarWidget QToolButton {
                background: #ffffff;
                color: #111827;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QCalendarWidget QAbstractItemView:enabled {
                background: #ffffff;
                color: #111827;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.combo_product_model = QComboBox()
        self.combo_product_model.addItem("ALL")
        for model in product_models:
            self.combo_product_model.addItem(model)
        selected_model = str(current_filters.get("product_model") or "ALL")
        index = self.combo_product_model.findText(selected_model)
        self.combo_product_model.setCurrentIndex(max(0, index))

        self.cb_enable_time_range = QCheckBox("启用时间范围")
        self.dt_start_time = QDateTimeEdit()
        self.dt_end_time = QDateTimeEdit()
        for edit in (self.dt_start_time, self.dt_end_time):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            self._setup_calendar_popup(edit)

        self._init_datetime_range(
            start_value=str(current_filters.get("start_time") or ""),
            end_value=str(current_filters.get("end_time") or ""),
            min_time=min_time,
            max_time=max_time,
        )

        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(8)
        time_row.addWidget(self.cb_enable_time_range)
        time_row.addWidget(self.dt_start_time)
        time_row.addWidget(QLabel("-"))
        time_row.addWidget(self.dt_end_time)

        self.ed_barcode = QLineEdit(str(current_filters.get("barcode") or ""))
        self.ed_barcode.setPlaceholderText("SN码模糊匹配，不写则全部")

        labels = set(current_filters.get("labels") or ["OK", "NG", "not_labeled"])
        self.cb_ok = QCheckBox("OK")
        self.cb_ng = QCheckBox("NG")
        self.cb_not_labeled = QCheckBox("未标记")
        self.cb_ok.setChecked("OK" in labels)
        self.cb_ng.setChecked("NG" in labels)
        self.cb_not_labeled.setChecked("not_labeled" in labels)

        label_row = QHBoxLayout()
        label_row.setContentsMargins(0, 0, 0, 0)
        label_row.setSpacing(16)
        label_row.addWidget(self.cb_ok)
        label_row.addWidget(self.cb_ng)
        label_row.addWidget(self.cb_not_labeled)
        label_row.addStretch(1)

        form.addRow("产品型号", self.combo_product_model)
        form.addRow("时间", time_row)
        form.addRow("SN码", self.ed_barcode)
        form.addRow("标签", label_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_filters)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _init_datetime_range(
        self,
        *,
        start_value: str,
        end_value: str,
        min_time: str | None,
        max_time: str | None,
    ) -> None:
        start_dt = self._parse_datetime(start_value) or self._parse_datetime(min_time) or QDateTime.currentDateTime()
        end_dt = self._parse_datetime(end_value) or self._parse_datetime(max_time) or QDateTime.currentDateTime()
        self.dt_start_time.setDateTime(start_dt)
        self.dt_end_time.setDateTime(end_dt)
        enabled = bool(start_value.strip() and end_value.strip()) if start_value or end_value else False
        self.cb_enable_time_range.setChecked(enabled)
        self.dt_start_time.setEnabled(enabled)
        self.dt_end_time.setEnabled(enabled)
        self.cb_enable_time_range.toggled.connect(self.dt_start_time.setEnabled)
        self.cb_enable_time_range.toggled.connect(self.dt_end_time.setEnabled)

    @staticmethod
    def _parse_datetime(value: str | None) -> QDateTime | None:
        text = str(value or "").strip()
        if not text:
            return None
        dt = QDateTime.fromString(text, "yyyy-MM-dd HH:mm:ss")
        if dt.isValid():
            return dt
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return QDateTime(parsed)

    @staticmethod
    def _setup_calendar_popup(edit: QDateTimeEdit) -> None:
        calendar = edit.calendarWidget()
        if not calendar:
            return
        calendar.setMinimumSize(360, 260)
        calendar.setGridVisible(True)
        calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        calendar.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        calendar.setStyleSheet(
            """
            QCalendarWidget QWidget {
                background: #ffffff;
                color: #111827;
            }
            QCalendarWidget QToolButton {
                min-width: 56px;
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
                min-width: 72px;
                color: #111827;
                background: #ffffff;
                selection-background-color: #dbeafe;
            }
            QCalendarWidget QAbstractItemView:enabled {
                background: #ffffff;
                color: #111827;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            """
        )

    def _accept_filters(self) -> None:
        if self.cb_enable_time_range.isChecked():
            start_dt = self.dt_start_time.dateTime()
            end_dt = self.dt_end_time.dateTime()
            if start_dt > end_dt:
                QMessageBox.warning(self, "提示", "开始时间不能晚于结束时间")
                return
        self.accept()

    def filters(self) -> dict[str, object]:
        labels = []
        if self.cb_ok.isChecked():
            labels.append("OK")
        if self.cb_ng.isChecked():
            labels.append("NG")
        if self.cb_not_labeled.isChecked():
            labels.append("not_labeled")
        return {
            "product_model": self.combo_product_model.currentText(),
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
            "labels": labels,
        }


class AudioDataExportThread(QThread):
    # 后台导出线程：避免大量音频写入 zip 时卡住主界面。
    progress_changed = pyqtSignal(int)
    succeeded = pyqtSignal(str, int)
    failed = pyqtSignal(str)

    def __init__(self, audio_data_service, target_path: str, records: list[dict[str, object]]):
        super().__init__()
        self.audio_data_service = audio_data_service
        self.target_path = target_path
        self.records = list(records)

    def run(self) -> None:
        try:
            # progress_callback 会在服务层每写入一项后触发，用来更新进度条。
            self.audio_data_service.export_zip(
                self.target_path,
                self.records,
                progress_callback=self.progress_changed.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self.target_path, len(self.records))


class DataManagementModule(QWidget):
    module_id = "data_management"
    table_header_labels = {
        "audio_data_id": "audio_id",
        "file_path": "路径",
        "product_model": "产品型号",
        "sample_rate": "采样率",
        "record_date": "创建时间",
        "labels": "标签结果",
        "label_source": "判定来源",
        "barcode": "SN码",
        "analyse_config": "分析配置",
        "config_content": "配置内容",
    }
    header_min_widths = {
        "__select__": 52,
        "audio_data_id": 160,
        "file_path": 360,
        "product_model": 90,
        "sample_rate": 80,
        "record_date": 170,
        "labels": 90,
        "label_source": 90,
        "barcode": 90,
        "analyse_config": 120,
        "config_content": 180,
        "__action__": 60,
        "__delete__": 80,
    }

    def __init__(self, services):
        super().__init__()
        self.services = services
        # 导出文件默认落到项目 data 目录下。
        self.data_dir = os.path.join(base_dir, "data")
        # 列名和记录都直接从 audio_data_table 动态读取，避免手写表头。
        self._column_names: list[str] = []
        self._records: list[dict[str, object]] = []
        self._filters: dict[str, object] = {
            "product_model": "ALL",
            "start_time": "",
            "end_time": "",
            "barcode": "",
            "labels": ["OK", "NG", "not_labeled"],
        }
        self._export_thread = None
        self._export_progress_dialog = None

        self._build_ui()
        self.refresh_table()

    def _build_ui(self) -> None:
        # 顶部工具栏 + 下方数据表。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        self.btn_refresh = QPushButton("刷新")
        self.btn_filter = QPushButton("条件筛选")
        self.btn_select_all = QPushButton("全选")
        self.btn_select_all.setCheckable(True)
        self.btn_export = QPushButton("打包导出")
        self.lb_selected = QLabel("已选 0 条")

        toolbar_layout.addWidget(self.btn_refresh)
        toolbar_layout.addWidget(self.btn_filter)
        toolbar_layout.addWidget(self.btn_select_all)
        toolbar_layout.addWidget(self.btn_export)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self.lb_selected)
        layout.addWidget(toolbar)

        self.table = QTableWidget(0, 0, self)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.table, 1)

        self.btn_refresh.clicked.connect(self.refresh_table)
        self.btn_filter.clicked.connect(self._show_filter_dialog)
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_export.clicked.connect(self._export_selected_rows)
        self.table.itemChanged.connect(lambda _item: self._update_selected_count())

    def refresh_table(self) -> None:
        # 每次刷新都重新读表结构和数据，适配数据库字段变化。
        service = self.services.audio_data_service
        self._column_names = [
            name for name in service.list_column_names() if name != "config_content"
        ]
        self._records = service.list_records(self._filters)
        self._render_table()

    def _show_filter_dialog(self) -> None:
        service = self.services.audio_data_service
        min_time, max_time = service.record_date_bounds()
        dialog = AudioDataFilterDialog(
            product_models=service.list_product_models(),
            current_filters=self._filters,
            min_time=min_time,
            max_time=max_time,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._filters = dialog.filters()
        self.refresh_table()

    def _render_table(self) -> None:
        # 左边一列勾选，右边一列操作，中间字段按中文表头显示。
        headers = [
            "选择",
            *[self.table_header_labels.get(name, name) for name in self._column_names],
            "操作",
            "删除",
        ]
        self.table.blockSignals(True)
        try:
            self.table.clearContents()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(self._records))

            for row_index, record in enumerate(self._records):
                checkbox_item = QTableWidgetItem()
                checkbox_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                checkbox_item.setCheckState(Qt.CheckState.Unchecked)
                self.table.setItem(row_index, 0, checkbox_item)

                for column_index, column_name in enumerate(self._column_names, start=1):
                    value = record.get(column_name)
                    text = self._display_cell_text(column_name, value)
                    item = QTableWidgetItem(text)
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    )
                    if column_name == "file_path" and text:
                        item.setToolTip(text)
                    self.table.setItem(row_index, column_index, item)

                # 每一行单独放一个播放按钮，按该行 file_path 播放。
                play_button = QPushButton()
                play_button.setObjectName("TablePlayButton")
                play_button.setIcon(
                    self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
                )
                play_button.setToolTip("播放")
                play_button.setFixedWidth(36)
                play_button.clicked.connect(
                    lambda _checked=False, path=record.get("file_path"): self._play_file(path)
                )
                self.table.setCellWidget(row_index, len(headers) - 2, play_button)

                delete_button = QPushButton("×")
                delete_button.setObjectName("TableDeleteButton")
                delete_button.setToolTip("删除该条数据和本地音频")
                delete_button.setFixedWidth(36)
                delete_button.clicked.connect(
                    lambda _checked=False, row=record: self._delete_record(row)
                )
                self.table.setCellWidget(row_index, len(headers) - 1, delete_button)
        finally:
            self.table.blockSignals(False)

        # 直接按内容展开列宽，显示不下时让表格自己横向滚动。
        self.table.resizeColumnsToContents()
        for row_index in range(self.table.rowCount()):
            self.table.setRowHeight(row_index, 30)
        self._apply_table_widths()
        self._update_selected_count()

    @staticmethod
    def _display_cell_text(column_name: str, value: object) -> str:
        if column_name == "record_date":
            return format_datetime_display(value)
        if column_name == "label_source":
            text = "" if value is None else str(value).strip()
            if text == "auto":
                return "自动"
            if text == "manual":
                return "人工"
            return text
        return "" if value is None else str(value)

    def _apply_table_widths(self) -> None:
        keys = ["__select__", *self._column_names, "__action__", "__delete__"]
        for column_index, key in enumerate(keys):
            self.table.setColumnWidth(
                column_index,
                max(self.table.columnWidth(column_index), self.header_min_widths.get(key, 80)),
            )

        extra_width = self.table.viewport().width() - sum(
            self.table.columnWidth(column_index) for column_index in range(self.table.columnCount())
        )
        if extra_width <= 0:
            return

        # 右边有空白时，优先补给更适合展开的列。
        grow_columns = []
        for key in ("file_path", "analyse_config", "audio_data_id", "record_date"):
            if key in self._column_names:
                grow_columns.append(self._column_names.index(key) + 1)
        if not grow_columns:
            grow_columns = [self.table.columnCount() - 1]

        share = extra_width // len(grow_columns)
        remainder = extra_width % len(grow_columns)
        for offset, column_index in enumerate(grow_columns):
            width = self.table.columnWidth(column_index) + share
            if offset == len(grow_columns) - 1:
                width += remainder
            self.table.setColumnWidth(column_index, width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_table_widths()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_table_widths()

    def _toggle_select_all(self, checked: bool) -> None:
        # 批量改勾选状态时先断开表格信号，避免重复刷新计数。
        self.table.blockSignals(True)
        try:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for row_index in range(self.table.rowCount()):
                item = self.table.item(row_index, 0)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self.table.blockSignals(False)
        self._update_selected_count()

    def _update_selected_count(self) -> None:
        # 同步右上角已选数量，以及“全选/取消全选”按钮文案。
        selected_count = len(self._selected_records())
        total_count = self.table.rowCount()
        self.lb_selected.setText(f"已选 {selected_count} 条")
        all_selected = total_count > 0 and selected_count == total_count
        self.btn_select_all.blockSignals(True)
        try:
            self.btn_select_all.setChecked(all_selected)
            self.btn_select_all.setText("取消全选" if all_selected else "全选")
        finally:
            self.btn_select_all.blockSignals(False)

    def _selected_records(self) -> list[dict[str, object]]:
        # 以当前表格勾选状态为准，返回选中的原始记录。
        selected: list[dict[str, object]] = []
        for row_index, record in enumerate(self._records):
            item = self.table.item(row_index, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                selected.append(record)
        return selected

    def _export_selected_rows(self) -> None:
        records = self._selected_records()
        if not records:
            QMessageBox.warning(self, "提示", "请先勾选要打包的数据")
            return

        os.makedirs(self.data_dir, exist_ok=True)
        default_name = os.path.join(
            self.data_dir,
            f'audio_data_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
        )
        target_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出选中数据",
            default_name,
            "Zip 文件 (*.zip)",
        )
        if not target_path:
            return
        if not target_path.lower().endswith(".zip"):
            target_path += ".zip"

        # 导出放到后台线程里执行，主界面只负责显示进度。
        self.btn_export.setEnabled(False)
        self._export_progress_dialog = QProgressDialog("正在打包...", None, 0, len(records) + 1, self)
        self._export_progress_dialog.setWindowTitle("打包导出")
        self._export_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._export_progress_dialog.setCancelButton(None)
        self._export_progress_dialog.setValue(0)

        self._export_thread = AudioDataExportThread(
            self.services.audio_data_service,
            target_path,
            records,
        )
        # 线程发出当前进度值后，直接更新进度条。
        self._export_thread.progress_changed.connect(self._export_progress_dialog.setValue)
        self._export_thread.succeeded.connect(self._on_export_succeeded)
        self._export_thread.failed.connect(self._on_export_failed)
        self._export_thread.finished.connect(self._cleanup_export_thread)
        self._export_thread.start()

    def _on_export_succeeded(self, target_path: str, record_count: int) -> None:
        self._finish_export()
        self._emit_log(
            f"[DataManagement] 已导出 {record_count} 条音频数据到 {target_path}"
        )
        QMessageBox.information(
            self,
            "导出成功",
            f"已导出 {record_count} 条数据到\n{target_path}",
        )

    def _on_export_failed(self, error_message: str) -> None:
        self._finish_export()
        QMessageBox.warning(self, "导出失败", f"导出失败: {error_message}")

    def _finish_export(self) -> None:
        self.btn_export.setEnabled(True)
        if self._export_progress_dialog is not None:
            self._export_progress_dialog.close()
            self._export_progress_dialog = None

    def _cleanup_export_thread(self) -> None:
        if self._export_thread is not None:
            self._export_thread.deleteLater()
            self._export_thread = None

    def _play_file(self, file_path: object) -> None:
        normalized_path = normalize_path(file_path)
        if not normalized_path or not os.path.exists(normalized_path):
            QMessageBox.warning(self, "提示", "没找到该文件")
            return
        try:
            # 播放前把 sounddevice 默认输出切到软件当前保存的设备。
            self._apply_current_output_device()
            sample_rate, data = wavfile.read(normalized_path)
            sd.stop()
            # 直接在程序里播放 wav，不调系统外部播放器。
            sd.play(data, int(sample_rate))
            self._emit_log(f"[DataManagement] 播放音频: {normalized_path}")
        except Exception as exc:
            QMessageBox.warning(self, "提示", f"播放失败: {exc}")

    def _delete_record(self, record: dict[str, object]) -> None:
        audio_data_id = record.get("audio_data_id")
        file_path = normalize_path(record.get("file_path"))
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除这条数据和本地音频吗？\n\n{file_path or ''}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            sd.stop()
            self.services.audio_data_service.delete_record(audio_data_id)
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", f"删除失败: {exc}")
            return

        self._emit_log(f"[DataManagement] 已删除音频数据: {audio_data_id}")
        self.refresh_table()

    def _apply_current_output_device(self) -> None:
        # 播放模块不自己记设备，直接复用硬件页当前保存的输出设备配置。
        hss_service = getattr(self.services, "hss_service", None)
        if hss_service is None:
            return
        load_io_devices = getattr(hss_service, "load_io_devices", None)
        query_devices_and_hostapis = getattr(hss_service, "query_devices_and_hostapis", None)
        restore_default_device_from_db = getattr(
            hss_service,
            "restore_default_device_from_db",
            None,
        )
        if not callable(load_io_devices):
            return
        if not callable(query_devices_and_hostapis):
            return
        if not callable(restore_default_device_from_db):
            return
        device_info = load_io_devices()
        if not device_info:
            return
        devices, hostapis = query_devices_and_hostapis(refresh=False)
        restore_default_device_from_db(device_info, devices, hostapis)

    def _emit_log(self, message: str) -> None:
        # 统一走 bus.log，状态栏和日志面板都能看到。
        bus = getattr(self.services, "bus", None)
        log_signal = getattr(bus, "log", None)
        emit = getattr(log_signal, "emit", None)
        if callable(emit):
            emit(message)
