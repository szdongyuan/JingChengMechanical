from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QVBoxLayout,
    QWidget,
)


class ChannelSelectModule(QWidget):
    """
    通道选择（多选）：
    - 从数据库读取输入通道数量
    - 初始化 In1..InN
    """

    def __init__(self, services):
        super().__init__()
        self.services = services

        self.init_ui()
        self.init_fun()
        self.init_channel_select()

    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)
        root.addWidget(self._grid_host, 0)

        self._checks = []

    def init_fun(self):
        self.services.bus.refresh_channels.connect(self.init_channel_select)


    def init_channel_select(self):
        device_info = self.services.hss_service.load_io_devices()
        input_info = device_info.get("input", {})
        raw = input_info.get("channel_count")
        if not raw:
            return
        channel_count = int(raw)
        if channel_count <= 0:
            channel_count = 1

        mic_cal = device_info.get("mic_cal")

        # 清空旧的 checkbox
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._checks.clear()

        # 生成新的 checkbox（每行最多放6个复选框）
        cols = 6
        for idx in range(channel_count):
            ch_name = f"In{idx + 1}"
            cb = QCheckBox(ch_name)
            cb.setChecked((mic_cal.get(ch_name) or {}).get("is_selected") == 1)
            cb.setStyleSheet(
                "QCheckBox{padding:0px; margin:0px;} QCheckBox::indicator{width:12px; height:12px;}"
            )
            cb.toggled.connect(
                lambda state, name=ch_name: self.on_check_changed(name, state)
            )
            self._checks.append(cb)
            r, c = divmod(idx, cols)
            self._grid.addWidget(cb, r, c)

    def on_check_changed(self, ch_name, checked):
        device_info = self.services.hss_service.load_io_devices()
        mic_cal = device_info.get("mic_cal")
        cfg = mic_cal.get(ch_name)
        cfg["is_selected"] = 1 if checked else 0
        mic_cal[ch_name] = cfg
        self.services.hss_service.save_mic_cal(mic_cal)


