from __future__ import annotations

from PyQt6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QGridLayout, QVBoxLayout, QWidget


class ChannelSelectModule(QWidget):
    """Edit enabled channels inside one managed input device."""

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._selected_hardware_id = None
        self._checks = []
        self.init_ui()
        self.init_fun()
        self.refresh_hardware_options()

    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        form = QFormLayout()
        self.combo_hardware = QComboBox()
        form.addRow("输入硬件", self.combo_hardware)
        root.addLayout(form)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)
        root.addWidget(self._grid_host, 0)

    def init_fun(self):
        self.services.bus.refresh_channels.connect(self.init_channel_select)
        self.services.bus.input_hardware_changed.connect(
            self.refresh_hardware_options
        )
        self.services.bus.input_hardware_selected.connect(self.select_hardware)
        self.combo_hardware.currentIndexChanged.connect(
            self._on_hardware_changed
        )

    def refresh_hardware_options(self):
        preferred = self._selected_hardware_id
        self.combo_hardware.blockSignals(True)
        self.combo_hardware.clear()
        for hardware in self.services.hss_service.list_input_hardware():
            self.combo_hardware.addItem(
                hardware["alias"], int(hardware["hardware_id"])
            )
        self.combo_hardware.blockSignals(False)
        if preferred is not None:
            self.select_hardware(preferred)
        elif self.combo_hardware.count():
            self.combo_hardware.setCurrentIndex(0)
            self._on_hardware_changed()
        else:
            self._selected_hardware_id = None
            self.init_channel_select()

    def select_hardware(self, hardware_id: int):
        for index in range(self.combo_hardware.count()):
            if self.combo_hardware.itemData(index) == int(hardware_id):
                self.combo_hardware.setCurrentIndex(index)
                self._on_hardware_changed()
                return

    def _on_hardware_changed(self, _index=None):
        self._selected_hardware_id = self.combo_hardware.currentData()
        self.init_channel_select()

    def _clear_checks(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._checks.clear()

    def init_channel_select(self):
        self._clear_checks()
        if self._selected_hardware_id is None:
            return
        hardware = self.services.hss_service.get_input_hardware(
            self._selected_hardware_id
        )
        if not hardware:
            return
        mic_cal = hardware.get("mic_cal") or {}
        for index in range(int(hardware["channel_count"])):
            channel_name = f"In{index + 1}"
            checkbox = QCheckBox(channel_name)
            checkbox.setChecked(
                (mic_cal.get(channel_name) or {}).get("is_selected", 1) == 1
            )
            checkbox.setStyleSheet(
                "QCheckBox{padding:0px; margin:0px;} "
                "QCheckBox::indicator{width:12px; height:12px;}"
            )
            checkbox.toggled.connect(
                lambda checked, name=channel_name: self.on_check_changed(
                    name, checked
                )
            )
            self._checks.append(checkbox)
            row, column = divmod(index, 6)
            self._grid.addWidget(checkbox, row, column)

    def on_check_changed(self, channel_name, checked):
        if self._selected_hardware_id is None:
            return
        hardware = self.services.hss_service.get_input_hardware(
            self._selected_hardware_id
        )
        if not hardware:
            return
        mic_cal = hardware.get("mic_cal") or {}
        channel = mic_cal.get(channel_name)
        if not isinstance(channel, dict):
            return
        channel["is_selected"] = 1 if checked else 0
        mic_cal[channel_name] = channel
        saved = self.services.hss_service.save_input_hardware_calibration(
            self._selected_hardware_id, mic_cal
        )
        if saved:
            self.services.bus.status.emit(
                "输入通道设置已保存；新配置立即可见，"
                "当前已准备的生产配置需重启软件后重新加载。"
            )
