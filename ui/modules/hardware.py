from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.service.log_manager import LogManager
from consts.style import combo_qss


class HardwareModule(QWidget):
    module_id = "hardware"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.logger = LogManager.set_log_handler("硬件选择")

        self._init_ui()
        self.btn_refresh.clicked.connect(self.refresh_devices)
        self.btn_save.clicked.connect(self.save_selected)
        self.combobox_input.currentIndexChanged.connect(self._update_save_enabled)
        self.combobox_output.currentIndexChanged.connect(self._update_save_enabled)

        self._devices: list[dict] = []
        self._hostapis: list[dict] = []

        self._load_saved()
        self.refresh_devices()

    def _init_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(0, 0, 0, 0)
        bar_lay.setSpacing(8)

        self.lb_current = QLabel("当前保存：—")
        self.lb_current.setObjectName("CurrentDevice")
        bar_lay.addWidget(self.lb_current, 1)

        self.btn_refresh = QPushButton("刷新设备")
        self.btn_save = QPushButton("保存选择")
        self.btn_save.setObjectName("Primary")
        bar_lay.addWidget(self.btn_refresh)
        bar_lay.addWidget(self.btn_save)
        lay.addWidget(bar)

        # 选择区：输入/输出
        sel = QWidget()
        sel_lay = QFormLayout(sel)
        sel_lay.setContentsMargins(0, 0, 0, 0)
        sel_lay.setHorizontalSpacing(10)
        sel_lay.setVerticalSpacing(8)

        self.combobox_input = QComboBox()
        self.combobox_output = QComboBox()
        self.combobox_input.setMinimumWidth(520)
        self.combobox_output.setMinimumWidth(520)
        self.combobox_input.setStyleSheet(combo_qss)
        self.combobox_output.setStyleSheet(combo_qss)
        sel_lay.addRow("输入设备", self.combobox_input)
        sel_lay.addRow("输出设备", self.combobox_output)
        lay.addWidget(sel)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["序号", "名称", "输入通道", "输出通道", "驱动类型", "采样率"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        # self.table.setSortingEnabled(True)
        lay.addWidget(self.table, 1)

    def _load_saved(self):
        # 从“硬件服务”（DB）读取上次保存的输入/输出设备信息，并显示在顶部“当前保存”标签里
        try:
            device_info = self.services.hss_service.load_io_devices()
        except Exception as e:
            # 读取失败时不阻塞页面，其它功能仍可用；给用户一个提示，同时写日志方便排查
            self.lb_current.setText("当前保存：—（数据库未就绪）")
            self.services.bus.log.emit(f"[Hardware] 读取已保存设备失败: {e}")
            return

        in_dev = device_info.get("input")
        out_dev = device_info.get("output")
        if not (in_dev or out_dev):
            # DB 里没有保存过任何选择
            self.lb_current.setText("当前保存：—")
            return

        def fmt(dev: dict) -> str:
            return f"({dev.get('id','-')})：{dev.get('name','-')}"

        self.lb_current.setText(
            f"当前设备：麦克{fmt(in_dev)} | 扬声器{fmt(out_dev)}"
        )

    def refresh_devices(self):
        try:
            self.services.hss_service.refresh_available_device()
        except Exception as e:
            self.services.bus.log.emit(f"[Hardware] 刷新音频系统失败: {e}")
        self.select_devices()
        self.logger.info("刷新设备")

    def select_devices(self):
        try:
            self._devices, self._hostapis = self.services.hss_service.query_devices_and_hostapis(
                refresh=False
            )
        except Exception as e:
            self.services.bus.log.emit(f"[Hardware] 读取设备失败: {e}")
            self.table.setRowCount(0)
            return

        def set_combo(cb: QComboBox, target):
            if target is None:
                cb.setCurrentIndex(0)
                return False
            for i in range(1, cb.count()):
                if cb.itemData(i) == target:
                    cb.setCurrentIndex(i)
                    return True
            cb.setCurrentIndex(0)
            return False

        default_in, default_out = self.services.hss_service.get_default_device_ids()
        self.services.bus.log.emit(f"[Hardware] 默认设备：out{default_out}, in{default_in}")

        self.combobox_output.clear()
        self.combobox_input.clear()
        self.combobox_output.addItem("请选择输出设备", None)
        self.combobox_input.addItem("请选择输入设备", None)
        self.combobox_output.model().item(0).setEnabled(False)
        self.combobox_input.model().item(0).setEnabled(False)
        # self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._devices))
        for index, device in enumerate(self._devices):
            hostapi_name = self._hostapis[device.get("hostapi", 0)].get("name") or "N/A"
            display_name = f"{device.get('name', '')} ({hostapi_name})"
            display_name_with_channels = (
                f"{display_name} [OUT: {device.get('max_output_channels', 0)}, "
                f"IN: {device.get('max_input_channels', 0)}]"
            )

            samplerate = device.get("default_samplerate", "N/A")
            if isinstance(samplerate, float):
                samplerate = f"{int(samplerate)} Hz"
            dev_index = device.get("index", index)

            values = [
                str(dev_index),
                display_name,
                str(device.get("max_input_channels", 0)),
                str(device.get("max_output_channels", 0)),
                hostapi_name,
                str(samplerate),
            ]
            # 表格每一行赋值
            for col, text in enumerate(values):
                it = QTableWidgetItem(text)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
                )
                if col in (1, 4):
                    it.setToolTip(text)
                self.table.setItem(index, col, it)
            # 下拉框赋值
            if device.get("max_output_channels", 0) > 0:
                self.combobox_output.addItem(
                    f"{dev_index}: {display_name_with_channels}", dev_index
                )
            if device.get("max_input_channels", 0) > 0:
                self.combobox_input.addItem(
                    f"{dev_index}: {display_name_with_channels}", dev_index
                )

        self._apply_table_sizing()
        # self.table.setSortingEnabled(True)

        self.services.bus.log.emit(f"[Hardware] 设备列表刷新完成，共 {len(self._devices)} 个")
        self.logger.info(f"设备列表刷新完成，共 {len(self._devices)} 个")

        # 不自动选中任何设备：刷新后保持在“请选择…”
        self.combobox_input.setCurrentIndex(0)
        self.combobox_output.setCurrentIndex(0)

        self._update_save_enabled()

    def _apply_table_sizing(self):
        """让表格更清爽：名称列自适应，其它列固定宽度"""
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)

        # 先设为 Interactive，避免 resizeColumnsToContents 抢占空间
        for c in range(self.table.columnCount()):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)

        self.table.setColumnWidth(0, 50)   # 序号
        self.table.setColumnWidth(2, 70)   # 输入通道
        self.table.setColumnWidth(3, 70)   # 输出通道
        self.table.setColumnWidth(5, 90)   # 采样率（避免右侧太宽）

        # 名称 + 驱动类型一起分担剩余空间（更均衡）
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # 名称
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)  # 驱动类型
        self.table.setColumnWidth(1, 520)  # 初始比例
        self.table.setColumnWidth(4, 220)  # 初始比例
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)

    def _update_save_enabled(self):
        # 按钮保持可点击；未选齐时由 save_selected() 弹窗提示
        missing = (
            self.combobox_input.currentData() is None
            or self.combobox_output.currentData() is None
        )
        self.btn_save.setEnabled(True)
        self.btn_save.setToolTip("请先选择输入设备和输出设备。") if missing else self.btn_save.setToolTip("")

    def save_selected(self):
        in_id = self.combobox_input.currentData()
        out_id = self.combobox_output.currentData()
        if in_id is None or out_id is None:
            QMessageBox.warning(self, "提示", "请先选择输入设备和输出设备。")
            return

        try:
            in_dev, out_dev = self.services.hss_service.save_io_devices(
                int(in_id),
                int(out_id),
                devices=self._devices,
                hostapis=self._hostapis,
                sync_sounddevice_default=True,
            )

            self._load_saved()
            self.services.bus.log.emit(
                f"[Hardware] 已保存输入/输出: IN {in_dev.get('id')} | {in_dev.get('name')} ; "
                f"OUT {out_dev.get('id')} | {out_dev.get('name')}"
            )
            QMessageBox.information(self, "保存成功", "输入/输出设备保存成功！")
            self.services.bus.refresh_channels.emit()
        except Exception as e:
            self.services.bus.log.emit(f"[Hardware] 保存失败: {e}")
            QMessageBox.warning(self, "保存失败", "硬件设备保存失败，请重试！")

