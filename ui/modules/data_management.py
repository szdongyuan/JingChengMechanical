from __future__ import annotations

import os
from datetime import datetime

import sounddevice as sd
from scipy.io import wavfile
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from consts.running_consts import base_dir
from base.utils import format_datetime_display, normalize_path


class DataManagementModule(QWidget):
    module_id = "data_management"
    table_header_labels = {
        "audio_data_id": "audio_id",
        "file_path": "路径",
        "product_model": "产品型号",
        "sample_rate": "采样率",
        "record_date": "创建时间",
        "labels": "标签结果",
        "barcode": "SN码",
        "analyse_config": "分析配置",
    }
    header_min_widths = {
        "__select__": 52,
        "audio_data_id": 160,
        "file_path": 360,
        "product_model": 90,
        "sample_rate": 80,
        "record_date": 170,
        "labels": 90,
        "barcode": 90,
        "analyse_config": 120,
        "__action__": 60,
    }

    def __init__(self, services):
        super().__init__()
        self.services = services
        # 导出文件默认落到项目 data 目录下。
        self.data_dir = os.path.join(base_dir, "data")
        # 列名和记录都直接从 audio_data_table 动态读取，避免手写表头。
        self._column_names: list[str] = []
        self._records: list[dict[str, object]] = []

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
        self.btn_select_all = QPushButton("全选")
        self.btn_select_all.setCheckable(True)
        self.btn_export = QPushButton("打包导出")
        self.lb_selected = QLabel("已选 0 条")

        toolbar_layout.addWidget(self.btn_refresh)
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
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_export.clicked.connect(self._export_selected_rows)
        self.table.itemChanged.connect(lambda _item: self._update_selected_count())

    def refresh_table(self) -> None:
        # 每次刷新都重新读表结构和数据，适配数据库字段变化。
        service = self.services.audio_data_service
        self._column_names = service.list_column_names()
        self._records = service.list_records()
        self._render_table()

    def _render_table(self) -> None:
        # 左边一列勾选，右边一列操作，中间字段按中文表头显示。
        headers = [
            "选择",
            *[self.table_header_labels.get(name, name) for name in self._column_names],
            "操作",
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
                    if column_name == "record_date":
                        text = format_datetime_display(value)
                    else:
                        text = "" if value is None else str(value)
                    item = QTableWidgetItem(text)
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    )
                    if column_name == "file_path" and text:
                        item.setToolTip(text)
                    self.table.setItem(row_index, column_index, item)

                # 每一行单独放一个播放按钮，按该行 file_path 播放。
                play_button = QPushButton()
                play_button.setIcon(
                    self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
                )
                play_button.setToolTip("播放")
                play_button.setFixedWidth(36)
                play_button.clicked.connect(
                    lambda _checked=False, path=record.get("file_path"): self._play_file(path)
                )
                self.table.setCellWidget(row_index, len(headers) - 1, play_button)
        finally:
            self.table.blockSignals(False)

        # 直接按内容展开列宽，显示不下时让表格自己横向滚动。
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()
        self._apply_table_widths()
        self._update_selected_count()

    def _apply_table_widths(self) -> None:
        keys = ["__select__", *self._column_names, "__action__"]
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

        try:
            self.services.audio_data_service.export_zip(target_path, records)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", f"导出失败: {exc}")
            return

        # 导出完成后只提示 zip 路径；zip 内会包含 db 和分类后的音频。
        self._emit_log(
            f"[DataManagement] 已导出 {len(records)} 条音频数据到 {target_path}"
        )
        QMessageBox.information(
            self,
            "导出成功",
            f"已导出 {len(records)} 条数据到\n{target_path}",
        )

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
