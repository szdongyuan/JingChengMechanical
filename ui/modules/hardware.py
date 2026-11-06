from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.service.hardwareselect_service import HardwareSelectService
from app.service.log_manager import LogManager


class HardwareModule(QWidget):
    """Manage complete input devices; channels remain children of each device."""

    module_id = "hardware"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._devices: list[dict] = []
        self._hostapis: list[dict] = []
        self._managed: list[dict] = []
        self._selected_hardware_id: int | None = None
        self.logger = LogManager.set_log_handler("输入硬件管理")
        self.init_ui()
        self.init_fun()
        self.refresh_devices()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        self.lb_summary = QLabel("已管理 0 台输入设备")
        self.lb_summary.setObjectName("CurrentDevice")
        toolbar.addWidget(self.lb_summary, 1)
        self.btn_refresh = QPushButton("刷新系统设备")
        toolbar.addWidget(self.btn_refresh)
        layout.addLayout(toolbar)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.combobox_input = QComboBox()
        self.combobox_input.setMinimumWidth(480)
        self.ed_alias = QLineEdit()
        self.ed_alias.setPlaceholderText("例如：产线 A 测试声卡")
        form.addRow("系统输入设备", self.combobox_input)
        form.addRow("设备别名", self.ed_alias)
        layout.addWidget(form_host)

        actions = QHBoxLayout()
        self.btn_add = QPushButton("添加设备")
        self.btn_add.setObjectName("Primary")
        self.btn_rename = QPushButton("保存别名")
        self.btn_remove = QPushButton("移除设备")
        actions.addWidget(self.btn_add)
        actions.addWidget(self.btn_rename)
        actions.addWidget(self.btn_remove)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["ID", "别名", "系统设备", "输入通道", "驱动", "采样率", "状态"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)
        self._apply_table_sizing()

    def init_fun(self):
        self.btn_refresh.clicked.connect(self.force_refresh_devices)
        self.btn_add.clicked.connect(self.add_selected_device)
        self.btn_rename.clicked.connect(self.rename_selected_device)
        self.btn_remove.clicked.connect(self.remove_selected_device)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)

    def force_refresh_devices(self, _checked: bool = False):
        if bool(
            getattr(self.services, "production_selection_locked", False)
        ):
            self.services.bus.log.emit(
                "常驻录音运行中，已跳过音频后端重置；"
                "设备热插拔后请重启软件。"
            )
        else:
            try:
                self.services.hss_service.refresh_available_device()
            except Exception as exc:
                self.services.bus.log.emit(f"刷新音频系统失败: {exc}")
        self.refresh_devices()

    def refresh_devices(self):
        try:
            self._devices, self._hostapis = (
                self.services.hss_service.query_devices_and_hostapis(refresh=False)
            )
        except Exception as exc:
            self._devices, self._hostapis = [], []
            self.services.bus.log.emit(f"读取系统输入设备失败: {exc}")
        self._populate_detected_inputs()
        self.reload_managed_devices(preferred_id=self._selected_hardware_id)
        self.services.bus.refresh_channels.emit()

    def _populate_detected_inputs(self):
        self.combobox_input.clear()
        self.combobox_input.addItem("请选择要添加的输入设备", None)
        first = self.combobox_input.model().item(0)
        if first is not None:
            first.setEnabled(False)
        for device in self._devices:
            if int(device.get("max_input_channels", 0) or 0) <= 0:
                continue
            hostapi_index = int(device.get("hostapi", 0) or 0)
            hostapi = HardwareSelectService._hostapi_name(
                self._hostapis, hostapi_index
            )
            sample_rate = int(float(device.get("default_samplerate", 0) or 0))
            label = (
                f"{device.get('name', '')} ({hostapi}) · "
                f"{device.get('max_input_channels', 0)} 通道 · {sample_rate} Hz"
            )
            self.combobox_input.addItem(label, int(device.get("index")))
        self.combobox_input.setCurrentIndex(0)

    def reload_managed_devices(self, *, preferred_id: int | None = None):
        try:
            self._managed = self.services.hss_service.list_input_hardware()
        except Exception as exc:
            self._managed = []
            self.services.bus.log.emit(f"读取受管输入设备失败: {exc}")
        self.table.setRowCount(len(self._managed))
        selected_row = None
        for row_index, hardware in enumerate(self._managed):
            status = "已连接" if self._is_connected(hardware) else "未连接"
            values = [
                hardware["hardware_id"],
                hardware["alias"],
                hardware["name"],
                hardware["channel_count"],
                hardware["hostapi"],
                f"{int(hardware['samplerate'])} Hz",
                status,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                )
                if column in (1, 2):
                    item.setToolTip(str(value))
                self.table.setItem(row_index, column, item)
            if preferred_id == hardware["hardware_id"]:
                selected_row = row_index
        self.lb_summary.setText(f"已管理 {len(self._managed)} 台输入设备")
        if selected_row is not None:
            self.table.selectRow(selected_row)
        elif self._managed:
            self.table.selectRow(0)
        else:
            self._selected_hardware_id = None
            self.ed_alias.clear()

    def _is_connected(self, hardware: dict) -> bool:
        matched = HardwareSelectService.find_matched_device(
            {
                "name": hardware["name"],
                "type": hardware["hostapi"],
                "samplerate": str(int(hardware["samplerate"])),
                "channel_count": hardware["channel_count"],
            },
            "input",
            self._devices,
            self._hostapis,
        )
        return matched is not None

    def _on_table_selection_changed(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._managed):
            return
        hardware = self._managed[row]
        self._selected_hardware_id = int(hardware["hardware_id"])
        self.ed_alias.setText(hardware["alias"])
        signal = getattr(self.services.bus, "input_hardware_selected", None)
        if signal is not None:
            signal.emit(self._selected_hardware_id)

    def add_selected_device(self):
        device_id = self.combobox_input.currentData()
        if device_id is None:
            QMessageBox.warning(self, "提示", "请先选择一个系统输入设备。")
            return
        try:
            hardware = self.services.hss_service.add_input_hardware(
                int(device_id),
                self.ed_alias.text(),
                devices=self._devices,
                hostapis=self._hostapis,
            )
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))
            return
        self._selected_hardware_id = int(hardware["hardware_id"])
        self._notify_changed()
        self.reload_managed_devices(preferred_id=self._selected_hardware_id)
        QMessageBox.information(self, "添加成功", "输入设备已加入硬件管理。")

    def rename_selected_device(self):
        if self._selected_hardware_id is None:
            QMessageBox.warning(self, "提示", "请先选择一台受管输入设备。")
            return
        try:
            self.services.hss_service.rename_input_hardware(
                self._selected_hardware_id, self.ed_alias.text()
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._notify_changed()
        self.reload_managed_devices(preferred_id=self._selected_hardware_id)

    def remove_selected_device(self):
        if self._selected_hardware_id is None:
            QMessageBox.warning(self, "提示", "请先选择一台受管输入设备。")
            return
        answer = QMessageBox.question(
            self,
            "确认移除",
            "确定移除这台输入设备及其全部通道校准值吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.services.hss_service.delete_input_hardware(
                self._selected_hardware_id
            )
        except Exception as exc:
            QMessageBox.warning(self, "移除失败", str(exc))
            return
        self._selected_hardware_id = None
        self._notify_changed()
        self.reload_managed_devices()

    def _notify_changed(self):
        signal = getattr(self.services.bus, "input_hardware_changed", None)
        if signal is not None:
            signal.emit()
        self.services.bus.refresh_channels.emit()

    def _apply_table_sizing(self):
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
