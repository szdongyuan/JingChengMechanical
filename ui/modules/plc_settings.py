from __future__ import annotations

import re
from uuid import uuid4

from PyQt6.QtCore import QMetaObject, QThread, Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget, QMessageBox,
)

from app.service.log_manager import LogManager
from base.utils import int_or_none, text_or_empty, text_or_none
from consts.style import APP_QSS
from app.service.plc_process_param_service import PlcProcessParamService
from app.service.plc_service import PlcService
from model.plc_field_offset_table import PlcFieldOffsetTable
from model.plc_process_param_offset_table import PlcProcessParamOffsetTable
from model.plc_table import PlcTable
from ui.popups.torque_settings_dialog import TorqueSettingsDialog

PLC_FIELD_OFFSETS = [
    ("start_signal_offset", "启动信号 StartSignal"),
    ("current_value_offset", "电流 CurrentValue (REAL/A)"),
    ("rotation_speed_offset", "转速值 RotationSpeed"),
    ("res_testing_signal_offset", "返回测试中 ResTestingSignal"),
    ("res_finish_signal_offset", "返回结束信号 ResFinishSignal"),
    ("res_error_code_offset", "返回异常代码 ResErrorCode"),
    ("res_result_offset", "返回测试结果 ResResult"),
    ("res_score_offset", "返回评分 ResScore"),
    ("res_spl_offset", "返回平均声压级 ResSPL"),
    ("res_loudness_offset", "返回响度 ResLoudness"),
    ("res_vibrate_data_offset", "返回中场振动 ResVibrateData"),
    ("res_ai_score_offset", "返回AI评分 ResAiScore"),
    ("res_spl_limit_up_offset", "返回声压值上限 ResSplLimitUp"),
    ("res_spl_limit_down_offset", "返回声压值下限 ResSplLimitDown"),
    ("res_loudness_limit_up_offset", "返回响度值上限 ResLoudnessLimitUp"),
    ("res_loudness_limit_down_offset", "返回响度值下限 ResLoudnessLimitDown"),
    ("res_vibrate_limit_up_offset", "返回振动值上限 ResVibrateLimitUp"),
    ("res_vibrate_limit_down_offset", "返回振动值下限 ResVibrateLimitDown"),
    ("res_ai_score_limit_down_offset", "返回AI评分下限 ResAiScoreLimitDown"),
    ("sn_code_offset", "SN码 SnCode"),
    ("product_model_offset", "产品型号 ProductModel"),
    ("res_sn_code_offset", "返回SN码 ResSnCode"),
    ("res_product_model_offset", "返回机种 ResProductModel"),
]

PLC_PROCESS_PARAM_OFFSETS = [
    ("load_cw_scan_offset", "加载Cw扫描 LoadCwScan"),
    ("load_cw_load_offset", "加载CwLoad LoadCwLoad"),
    ("load_pause_offset", "加载停顿 LoadPause"),
    ("load_ccw_scan_offset", "加载Ccw扫描 LoadCcwScan"),
    ("load_ccw_load_offset", "加载CcwLoad LoadCcwLoad"),
    ("load_to_noload_waiting_time_offset", "负载到空载等待时间"),
    ("switch_sound_cw_1_offset", "切换音Cw1 SwitchSoundCw_1"),
    ("switch_sound_ccw_1_offset", "切换音Ccw1 SwitchSoundCcw_1"),
    ("switch_sound_cw_2_offset", "切换音Cw2 SwitchSoundCw_2"),
    ("switch_sound_ccw_2_offset", "切换音Ccw2 SwitchSoundCcw_2"),
    ("noload_cw_offset", "空载Cw NoloadCw"),
    ("noload_pause_offset", "空载停顿 NoloadPause"),
    ("noload_ccw_offset", "空载Ccw NoloadCcw"),
]


class PlcFieldOffsetDialog(QDialog):
    def __init__(self, plc_id, values=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"字段偏移量配置 - PLC {plc_id}")
        self.resize(520, 620)
        self.setStyleSheet(APP_QSS + "\nQDialog { background: white; }")
        self.edits = {}

        form = QFormLayout()
        values = values or {}
        for key, label in PLC_FIELD_OFFSETS:
            edit = QLineEdit(text_or_empty(values.get(key)))
            edit.setMinimumHeight(34)
            self.edits[key] = edit
            form.addRow(f"{label}：", edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def get_values(self):
        return {
            key: text_or_none(edit.text())
            for key, edit in self.edits.items()
        }

    def validate_values(self):
        for key, label in PLC_FIELD_OFFSETS:
            value = self.edits[key].text().strip()
            if not value:
                continue
            if not re.fullmatch(r"\d+\.\d+", value):
                QMessageBox.warning(self, "提示", f"{label} 偏移格式错误，请输入类似 0.0 的格式")
                self.edits[key].setFocus()
                return False
        return True

    def accept(self):
        if not self.validate_values():
            return
        super().accept()


class PlcProcessParamOffsetDialog(QDialog):
    def __init__(self, process_state="未连接", parent=None):
        super().__init__(parent)
        self.setWindowTitle("工艺参数偏移配置")
        self.resize(560, 660)
        self.setStyleSheet(APP_QSS + "\nQDialog { background: white; }")

        form = QFormLayout()
        row = PlcProcessParamOffsetTable.select().first()

        # 工艺 PLC 连接参数保存在同一张工艺偏移表里，弹窗打开时自己读取数据库第一条。
        self.ed_process_ip = QLineEdit(text_or_empty(row.process_ip if row else None))
        self.ed_process_db_number = QLineEdit(text_or_empty(row.process_db_number if row else None))
        self.ed_process_reconnect_interval = QLineEdit(
            text_or_empty(row.process_reconnect_interval_ms if row else None)
        )
        self.lb_process_state = QLabel(process_state)
        self.btn_process_connect = QPushButton("连接")
        self.btn_process_connect.setObjectName("Primary")
        self.btn_process_connect.setFixedWidth(72)
        for edit in (
                self.ed_process_ip,
                self.ed_process_db_number,
                self.ed_process_reconnect_interval,
        ):
            edit.setMinimumHeight(34)

        form.addRow("IP：", self.ed_process_ip)
        form.addRow("数据块号：", self.ed_process_db_number)
        form.addRow("断线重连时间(ms)：", self.ed_process_reconnect_interval)
        form.addRow("连接状态：", self.lb_process_state)

        # 下面这些是工艺参数在 PLC DB 里的偏移地址。
        self.ed_load_cw_scan_offset = QLineEdit(text_or_empty(row.load_cw_scan_offset if row else None))
        self.ed_load_cw_load_offset = QLineEdit(text_or_empty(row.load_cw_load_offset if row else None))
        self.ed_load_pause_offset = QLineEdit(text_or_empty(row.load_pause_offset if row else None))
        self.ed_load_ccw_scan_offset = QLineEdit(text_or_empty(row.load_ccw_scan_offset if row else None))
        self.ed_load_ccw_load_offset = QLineEdit(text_or_empty(row.load_ccw_load_offset if row else None))
        self.ed_load_to_noload_waiting_time_offset = QLineEdit(
            text_or_empty(row.load_to_noload_waiting_time_offset if row else None)
        )
        self.ed_switch_sound_cw_1_offset = QLineEdit(text_or_empty(row.switch_sound_cw_1_offset if row else None))
        self.ed_switch_sound_ccw_1_offset = QLineEdit(text_or_empty(row.switch_sound_ccw_1_offset if row else None))
        self.ed_switch_sound_cw_2_offset = QLineEdit(text_or_empty(row.switch_sound_cw_2_offset if row else None))
        self.ed_switch_sound_ccw_2_offset = QLineEdit(text_or_empty(row.switch_sound_ccw_2_offset if row else None))
        self.ed_noload_cw_offset = QLineEdit(text_or_empty(row.noload_cw_offset if row else None))
        self.ed_noload_pause_offset = QLineEdit(text_or_empty(row.noload_pause_offset if row else None))
        self.ed_noload_ccw_offset = QLineEdit(text_or_empty(row.noload_ccw_offset if row else None))

        process_offset_edits = (
            self.ed_load_cw_scan_offset,
            self.ed_load_cw_load_offset,
            self.ed_load_pause_offset,
            self.ed_load_ccw_scan_offset,
            self.ed_load_ccw_load_offset,
            self.ed_load_to_noload_waiting_time_offset,
            self.ed_switch_sound_cw_1_offset,
            self.ed_switch_sound_ccw_1_offset,
            self.ed_switch_sound_cw_2_offset,
            self.ed_switch_sound_ccw_2_offset,
            self.ed_noload_cw_offset,
            self.ed_noload_pause_offset,
            self.ed_noload_ccw_offset,
        )
        for edit in process_offset_edits:
            edit.setMinimumHeight(34)

        form.addRow("加载Cw扫描 LoadCwScan：", self.ed_load_cw_scan_offset)
        form.addRow("加载CwLoad LoadCwLoad：", self.ed_load_cw_load_offset)
        form.addRow("加载停顿 LoadPause：", self.ed_load_pause_offset)
        form.addRow("加载Ccw扫描 LoadCcwScan：", self.ed_load_ccw_scan_offset)
        form.addRow("加载CcwLoad LoadCcwLoad：", self.ed_load_ccw_load_offset)
        form.addRow("负载到空载等待时间：", self.ed_load_to_noload_waiting_time_offset)
        form.addRow("切换音Cw1 SwitchSoundCw_1：", self.ed_switch_sound_cw_1_offset)
        form.addRow("切换音Ccw1 SwitchSoundCcw_1：", self.ed_switch_sound_ccw_1_offset)
        form.addRow("切换音Cw2 SwitchSoundCw_2：", self.ed_switch_sound_cw_2_offset)
        form.addRow("切换音Ccw2 SwitchSoundCcw_2：", self.ed_switch_sound_ccw_2_offset)
        form.addRow("空载Cw NoloadCw：", self.ed_noload_cw_offset)
        form.addRow("空载停顿 NoloadPause：", self.ed_noload_pause_offset)
        form.addRow("空载Ccw NoloadCcw：", self.ed_noload_ccw_offset)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.btn_process_connect)
        button_row.addWidget(buttons)
        layout.addLayout(button_row)

    def get_values(self):
        values = {}
        values["process_ip"] = text_or_none(self.ed_process_ip.text())
        values["process_db_number"] = text_or_none(self.ed_process_db_number.text())
        values["process_reconnect_interval_ms"] = text_or_none(self.ed_process_reconnect_interval.text())
        values["load_cw_scan_offset"] = text_or_none(self.ed_load_cw_scan_offset.text())
        values["load_cw_load_offset"] = text_or_none(self.ed_load_cw_load_offset.text())
        values["load_pause_offset"] = text_or_none(self.ed_load_pause_offset.text())
        values["load_ccw_scan_offset"] = text_or_none(self.ed_load_ccw_scan_offset.text())
        values["load_ccw_load_offset"] = text_or_none(self.ed_load_ccw_load_offset.text())
        values["load_to_noload_waiting_time_offset"] = text_or_none(
            self.ed_load_to_noload_waiting_time_offset.text()
        )
        values["switch_sound_cw_1_offset"] = text_or_none(self.ed_switch_sound_cw_1_offset.text())
        values["switch_sound_ccw_1_offset"] = text_or_none(self.ed_switch_sound_ccw_1_offset.text())
        values["switch_sound_cw_2_offset"] = text_or_none(self.ed_switch_sound_cw_2_offset.text())
        values["switch_sound_ccw_2_offset"] = text_or_none(self.ed_switch_sound_ccw_2_offset.text())
        values["noload_cw_offset"] = text_or_none(self.ed_noload_cw_offset.text())
        values["noload_pause_offset"] = text_or_none(self.ed_noload_pause_offset.text())
        values["noload_ccw_offset"] = text_or_none(self.ed_noload_ccw_offset.text())
        return values

    def process_params_validate(self):
        """
        工艺参数校验格式
        """
        # 连接参数必须可用，否则后台服务无法自动连接工艺 PLC。
        if not self.ed_process_ip.text().strip():
            QMessageBox.warning(self, "提示", "请填写工艺 PLC IP")
            self.ed_process_ip.setFocus()
            return False
        try:
            if int_or_none(self.ed_process_db_number.text(), "数据块号") is None:
                self.ed_process_db_number.setFocus()
                return False
            if int_or_none(self.ed_process_reconnect_interval.text(), "断线重连时间") is None:
                self.ed_process_reconnect_interval.setFocus()
                return False
        except ValueError as exc:
            QMessageBox.warning(self, "提示", str(exc))
            return False
        validate_items = (
            ("加载Cw扫描 LoadCwScan", self.ed_load_cw_scan_offset),
            ("加载CwLoad LoadCwLoad", self.ed_load_cw_load_offset),
            ("加载停顿 LoadPause", self.ed_load_pause_offset),
            ("加载Ccw扫描 LoadCcwScan", self.ed_load_ccw_scan_offset),
            ("加载CcwLoad LoadCcwLoad", self.ed_load_ccw_load_offset),
            ("负载到空载等待时间", self.ed_load_to_noload_waiting_time_offset),
            ("切换音Cw1 SwitchSoundCw_1", self.ed_switch_sound_cw_1_offset),
            ("切换音Ccw1 SwitchSoundCcw_1", self.ed_switch_sound_ccw_1_offset),
            ("切换音Cw2 SwitchSoundCw_2", self.ed_switch_sound_cw_2_offset),
            ("切换音Ccw2 SwitchSoundCcw_2", self.ed_switch_sound_ccw_2_offset),
            ("空载Cw NoloadCw", self.ed_noload_cw_offset),
            ("空载停顿 NoloadPause", self.ed_noload_pause_offset),
            ("空载Ccw NoloadCcw", self.ed_noload_ccw_offset),
        )
        for label, edit in validate_items:
            value = edit.text().strip()
            if not value:
                continue
            if not re.fullmatch(r"\d+\.\d+", value):
                QMessageBox.warning(self, "提示", f"{label} 偏移格式错误，请输入类似 0.0 的格式")
                edit.setFocus()
                return False
        return True

    def set_process_state(self, state):
        # 后台服务状态变化时，弹窗如果正在打开，就同步刷新这里的显示。
        self.lb_process_state.setText(state)
        if state == "已连接":
            self.lb_process_state.setStyleSheet("color: #16a34a;")
            self.btn_process_connect.setText("断开")
        elif state in ("连接中", "重连中"):
            self.lb_process_state.setStyleSheet("color: #d97706;")
            self.btn_process_connect.setText("断开")
        else:
            self.lb_process_state.setStyleSheet("color: #dc2626;")
            self.btn_process_connect.setText("连接")

    def accept(self):
        if not self.process_params_validate():
            return
        super().accept()


class PlcSettingsModule(QWidget):
    def __init__(self, services):
        super().__init__()
        self.logger = LogManager.set_log_handler("core")
        self.services = services
        self._torque_delete_requests = {}
        self._disconnecting_threads = set()
        if getattr(services, 'torque_jobs', None) is not None:
            services.torque_jobs.finished.connect(self._on_torque_job_finished)
        # plc_services 是挂在ServiceContainer 容器对象上的一个属性
        self.services.plc_services = {}
        self.services.plc_threads = {}
        self.services.plc_state = {}
        self.services.process_plc_service = None
        self.services.process_plc_thread = None
        self.services.process_plc_state = "未连接"
        self.process_param_dialog = None
        self.torque_dialog = None

        self.init_ui()
        self.init_fun()
        self.reload_rows()
        self.start_process_param_service()

    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # 顶部下拉框只显示 PLC 的 id。
        self.combo_plc = QComboBox()
        self.combo_plc.setMinimumWidth(160)
        # 新增/删除 PLC：只处理数据库里的 PLC 配置和对应偏移配置。
        self.btn_add_plc = QPushButton("新增 PLC")
        self.btn_add_plc.setObjectName("Primary")
        self.btn_add_plc.setFixedWidth(96)
        self.btn_delete_plc = QPushButton("删除 PLC")
        self.btn_delete_plc.setObjectName("Danger")
        self.btn_delete_plc.setFixedWidth(96)

        plc_row = QHBoxLayout()
        plc_row.setContentsMargins(0, 0, 0, 0)
        plc_row.setSpacing(10)
        plc_row.addWidget(self.combo_plc, 1)
        plc_row.addWidget(self.btn_add_plc)
        plc_row.addWidget(self.btn_delete_plc)
        root.addLayout(plc_row)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        # 这些输入框分别对应 plc_table 里的可编辑字段。
        self.ed_name = QLineEdit()
        self.ed_ip = QLineEdit()
        self.ed_rack = QLineEdit()
        self.ed_slot = QLineEdit()
        self.ed_db_number = QLineEdit()
        self.ed_poll_interval = QLineEdit()
        self.ed_reconnect_interval = QLineEdit()
        # 状态只展示当前值，开关和连接通过按钮触发。
        self.lb_state = QLabel("-")
        self.lb_connect_state = QLabel("-")

        for edit in (
                self.ed_name,
                self.ed_ip,
                self.ed_rack,
                self.ed_slot,
                self.ed_db_number,
                self.ed_poll_interval,
                self.ed_reconnect_interval,
        ):
            edit.setMinimumHeight(25)
            edit.setObjectName("PlcField")

        form.addRow("PLC名称：", self.ed_name)
        form.addRow("IP：", self.ed_ip)
        form.addRow("机架号：", self.ed_rack)
        form.addRow("槽号：", self.ed_slot)
        form.addRow("数据块号：", self.ed_db_number)
        form.addRow("轮询时间(ms)：", self.ed_poll_interval)
        form.addRow("断线重连时间(ms)：", self.ed_reconnect_interval)
        form.addRow("开机启动：", self.lb_state)
        form.addRow("连接状态：", self.lb_connect_state)
        root.addLayout(form)

        btn_row = QGridLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(10)

        self.btn_field_offset = QPushButton("偏移设置")
        self.btn_field_offset.setObjectName("Primary")

        self.btn_process_param_offset = QPushButton("工艺")
        self.btn_process_param_offset.setObjectName("Primary")

        self.btn_torque = QPushButton("扭矩")
        self.btn_torque.setObjectName("Primary")

        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("Primary")

        self.btn_toggle = QPushButton("开启")
        self.btn_toggle.setObjectName("Primary")

        self.btn_connect = QPushButton("连接")
        self.btn_connect.setObjectName("Primary")

        for index, button in enumerate((self.btn_field_offset, self.btn_process_param_offset,
                                        self.btn_torque, self.btn_save, self.btn_toggle,
                                        self.btn_connect)):
            btn_row.addWidget(button, index // 3, index % 3)
        root.addLayout(btn_row)
        root.addStretch(1)

    def init_fun(self):
        self.combo_plc.currentIndexChanged.connect(self.on_combo_changed)
        # 顶部新增/删除按钮。
        self.btn_add_plc.clicked.connect(self.add_plc)
        self.btn_delete_plc.clicked.connect(self.delete_current_plc)
        self.btn_save.clicked.connect(self.save_current)
        self.btn_toggle.clicked.connect(self.toggle_state)
        self.btn_connect.clicked.connect(self.connect_plc)
        self.btn_field_offset.clicked.connect(self.open_field_offset_dialog)
        self.btn_process_param_offset.clicked.connect(self.open_process_param_offset_dialog)
        self.btn_torque.clicked.connect(self.open_torque_dialog)
        for edit in (
                self.ed_name,
                self.ed_ip,
                self.ed_rack,
                self.ed_slot,
                self.ed_db_number,
                self.ed_poll_interval,
                self.ed_reconnect_interval,
        ):
            edit.textChanged.connect(lambda _text: self.btn_save.setText("保存*"))

    def _emit_plc_changed(self):
        signal = getattr(self.services.bus, "plc_changed", None)
        emit = getattr(signal, "emit", None)
        if callable(emit):
            emit()

    def reload_rows(self):
        # 每次重载都从数据库重新取 PLC 列表，并刷新下拉框。
        try:
            rows = list(PlcTable.select().order_by(PlcTable.id))
        except Exception as exc:
            rows = []
            self.logger.error(f"读取 PLC 配置失败: {exc}")
            self.services.bus.log.emit(f"读取 PLC 配置失败: {exc}")
            QMessageBox.warning(self, "提示", f"读取 PLC 配置失败: {exc}")

        for row in rows:
            if row.id not in self.services.plc_state:
                self.services.plc_state[row.id] = "未连接"

        self.combo_plc.blockSignals(True)
        self.combo_plc.clear()
        for row in rows:
            self.combo_plc.addItem(str(row.id), row.id)
        self.combo_plc.blockSignals(False)

        has_rows = bool(rows)
        self.combo_plc.setEnabled(has_rows)
        self.btn_add_plc.setEnabled(True)
        self.btn_delete_plc.setEnabled(has_rows)
        self.btn_save.setEnabled(has_rows)
        self.btn_toggle.setEnabled(has_rows)
        self.btn_connect.setEnabled(has_rows)
        self.btn_field_offset.setEnabled(has_rows)
        self.btn_process_param_offset.setEnabled(has_rows)
        self.btn_torque.setEnabled(has_rows)
        self._set_fields_enabled(has_rows)

        if has_rows:
            # 默认先显示第一条 PLC，并把开机自动连接的项拉起来。
            self.combo_plc.setCurrentIndex(0)
            self.on_combo_changed()
            for row in rows:
                if int(row.plc_open_state) == 1:
                    self.start_connect(row)
        else:
            self._render_empty()

    def get_current_row(self):
        # 当前下拉框选中的那条 PLC 记录，每次都直接查数据库。
        plc_id = self.combo_plc.currentData()
        if plc_id is None:
            return None
        try:
            return PlcTable.get_by_id(plc_id)
        except Exception:
            return None

    def on_combo_changed(self):
        row = self.get_current_row()
        if row is None:
            self._render_empty()
            return
        # 把当前 PLC 记录填充到界面控件中。
        edits = (
            self.ed_name,
            self.ed_ip,
            self.ed_rack,
            self.ed_slot,
            self.ed_db_number,
            self.ed_poll_interval,
            self.ed_reconnect_interval,
        )
        for edit in edits:
            edit.blockSignals(True)
        self.ed_name.setText(text_or_empty(row.plc_name))
        self.ed_ip.setText(text_or_empty(row.plc_ip))
        self.ed_rack.setText(text_or_empty(row.plc_rack))
        self.ed_slot.setText(text_or_empty(row.plc_slot))
        self.ed_db_number.setText(text_or_empty(row.plc_db_number))
        self.ed_poll_interval.setText(text_or_empty(row.plc_poll_interval_ms))
        self.ed_reconnect_interval.setText(text_or_empty(row.plc_reconnect_interval_ms))
        for edit in edits:
            edit.blockSignals(False)
        self.btn_save.setText("保存")

        is_open = (int(row.plc_open_state) == 1)  # 是否已开启开机自启
        if is_open:
            self.lb_state.setText("开启")
            self.btn_toggle.setText("关闭")
        else:
            self.lb_state.setText("关闭")
            self.btn_toggle.setText("开启")

        self.set_connect_label(self.set_lb_text(row.id))
        self.apply_connect_button_state(row.id)

    def _render_empty(self):
        # 无数据时清空界面，避免保留上一条 PLC 的显示内容。
        for edit in (
                self.ed_name,
                self.ed_ip,
                self.ed_rack,
                self.ed_slot,
                self.ed_db_number,
                self.ed_poll_interval,
                self.ed_reconnect_interval,
        ):
            edit.setText("")
        self.btn_save.setText("保存")
        self.lb_state.setText("-")
        self.set_connect_label("-")
        self.btn_toggle.setText("开启")
        self.btn_connect.setText("连接")
        self.btn_connect.setEnabled(False)
        self.btn_delete_plc.setEnabled(False)
        self.btn_field_offset.setEnabled(False)
        self.btn_process_param_offset.setEnabled(False)
        self.btn_torque.setEnabled(False)

    def add_plc(self):
        try:
            # 先新增 PLC 基础配置，再给它补一条空的偏移配置。
            row = PlcTable.create(
                plc_name=None,
                plc_ip=None,
                plc_rack=0,
                plc_slot=1,
                plc_db_number=1,
                plc_poll_interval_ms=100,
                plc_reconnect_interval_ms=1000,
                plc_open_state=0,
            )
            PlcFieldOffsetTable.create(
                plc_id=row.id,
                **{field_name: None for field_name, _label in PLC_FIELD_OFFSETS},
            )
            # 重新加载下拉框，并切到刚新增的 PLC。
            self.reload_rows()
            index = self.combo_plc.findData(row.id)
            if index >= 0:
                self.combo_plc.setCurrentIndex(index)
            self._emit_plc_changed()
            QMessageBox.information(self, "成功", f"PLC {row.id} 新增成功!")
        except Exception as exc:
            QMessageBox.warning(self, "提示", f"新增 PLC 失败: {exc}")

    def delete_current_plc(self):
        row = self.get_current_row()
        if row is None:
            QMessageBox.warning(self, "提示", "请先选择 PLC")
            return

        reply = QMessageBox.question(
            self,
            "提示",
            f"确认删除 PLC {row.id}：{row.plc_name or '-'}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        row_id = row.id
        try:
            self.services.torque_jobs.cancel_pending_config(row_id)
            # 删除前先断开当前 PLC，避免后台线程还在读写这条配置。
            self.disconnect_row(row_id)
            thread = self.services.plc_threads.get(row_id)
            if (self.services.plc_services.get(row_id) is not None
                    or (thread is not None and thread.isRunning())):
                raise RuntimeError('PLC 尚未完成断开，请稍后重试')
            request_id = self.services.torque_jobs.submit('delete_plc', row_id)
            self._torque_delete_requests[request_id] = row_id
            self.btn_delete_plc.setEnabled(False)
        except Exception as exc:
            QMessageBox.warning(self, "提示", f"删除 PLC 失败: {exc}")

    @pyqtSlot(object)
    def _on_torque_job_finished(self, result):
        row_id = self._torque_delete_requests.pop(result.request_id, None)
        if row_id is None:
            return
        if result.cancelled:
            self.reload_rows()
            return
        if result.error is not None:
            self.reload_rows()
            QMessageBox.warning(self, "提示", f"删除 PLC 失败: {result.error}")
            return
        self.services.plc_state.pop(row_id, None)
        self.reload_rows()
        self._emit_plc_changed()
        refresh_plc_labels = getattr(self.window(), "_refresh_plc_state_labels", None)
        if callable(refresh_plc_labels):
            refresh_plc_labels()
        QMessageBox.information(self, "成功", f"PLC {row_id} 删除成功!")

    def open_field_offset_dialog(self):
        row = self.get_current_row()
        if row is None:
            QMessageBox.warning(self, "提示", "请先选择 PLC")
            return

        offset_row = (
            PlcFieldOffsetTable
            .select()
            .where(PlcFieldOffsetTable.plc_id == row.id)
            .first()
        )
        if offset_row is None:
            QMessageBox.warning(self, "提示", f"PLC {row.id} 未配置偏移数据")
            return

        values = {
            field_name: getattr(offset_row, field_name)
            for field_name, _label in PLC_FIELD_OFFSETS
        }

        dialog = PlcFieldOffsetDialog(row.id, values, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            PlcFieldOffsetTable.update(**dialog.get_values()).where(
                PlcFieldOffsetTable.plc_id == row.id
            ).execute()
            self.logger.info(f"PLC {row.id} 偏移设置保存成功")
            self.services.bus.log.emit(f"PLC {row.id} 偏移设置保存成功")
            QMessageBox.information(self, "成功", f"PLC {row.id} 偏移设置保存成功!")
        except Exception as exc:
            self.logger.error(f"PLC {row.id} 偏移设置保存失败: {exc}")
            self.services.bus.log.emit(f"PLC {row.id} 偏移设置保存失败: {exc}")
            QMessageBox.warning(self, "提示", f"偏移设置保存失败: {exc}")

    def open_process_param_offset_dialog(self):
        # 工艺配置只有一条记录：既保存 PLC 连接参数，也保存每个工艺字段的偏移。
        row = PlcProcessParamOffsetTable.select().first()
        if row is None:
            QMessageBox.warning(self, "提示", "未配置工艺参数偏移数据")
            return
        dialog = PlcProcessParamOffsetDialog(
            self.services.process_plc_state,
            self,
        )
        self.process_param_dialog = dialog
        # 弹窗里的连接按钮只控制后台工艺 PLC 服务，不影响普通测试 PLC。
        dialog.btn_process_connect.clicked.connect(self.toggle_process_param_connect)
        dialog.set_process_state(self.services.process_plc_state)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.process_param_dialog = None
            return

        try:
            PlcProcessParamOffsetTable.update(**dialog.get_values()).execute()
            self.services.bus.log.emit("工艺参数偏移保存成功")
            QMessageBox.information(self, "成功", "工艺参数偏移保存成功!")
        except Exception as exc:
            self.services.bus.log.emit(f"工艺参数偏移保存失败: {exc}")
            QMessageBox.warning(self, "提示", f"工艺参数偏移保存失败: {exc}")
        finally:
            self.process_param_dialog = None

    def start_process_param_service(self):
        # 看下后台有没有挂着工艺线程process_plc_thread 和工艺对象, 没有才启动
        if getattr(self.services, "process_plc_service", None) is not None:
            return
        if getattr(self.services, "process_plc_thread", None) is not None:
            return

        # 从工艺配置表读取连接参数；没有 IP 时不启动。
        row = PlcProcessParamOffsetTable.select().first()
        if row is None:
            return

        # 工艺 PLC 单独用一个服务和线程，避免和普通测试 PLC 的 StartSignal 逻辑混在一起。
        process_service = PlcProcessParamService(
            bus=self.services.bus,
            ip=row.process_ip,
            db_number=row.process_db_number,
            reconnect_interval_ms=row.process_reconnect_interval_ms,
            rack=0,
            slot=1,
        )
        process_thread = QThread(self)
        process_service.moveToThread(process_thread)
        process_thread.started.connect(process_service.start_service)
        process_thread.finished.connect(process_service.deleteLater)
        process_thread.finished.connect(self._on_process_param_thread_stopped)
        process_service.state_changed.connect(self.on_process_param_state_changed)
        process_service.connect_result.connect(self.on_process_param_connect_result)

        self.services.process_plc_service = process_service
        self.services.process_plc_thread = process_thread
        self.services.process_plc_state = "连接中"
        process_thread.start()

    def stop_process_param_service(self):
        # 主动断开工艺 PLC：先让服务在线程里停止，再退出线程。
        process_service = getattr(self.services, "process_plc_service", None)
        process_thread = getattr(self.services, "process_plc_thread", None)
        if process_service is not None:
            try:
                QMetaObject.invokeMethod(
                    process_service,
                    "stop_service",
                    Qt.ConnectionType.BlockingQueuedConnection,
                )
            except Exception as exc:
                self.logger.error(f"工艺 PLC 断开失败: {exc}")
                self.services.bus.log.emit(f"工艺 PLC 断开失败: {exc}")
        if process_thread is not None:
            process_thread.quit()
            process_thread.wait(2000)
        self.services.process_plc_service = None
        self.services.process_plc_thread = None
        self.services.process_plc_state = "未连接"

    def toggle_process_param_connect(self):
        # 弹窗按钮：已连接/重连中则断开，未连接则启动后台自动重连。
        state = getattr(self.services, "process_plc_state", "未连接")
        if state in ("连接中", "重连中", "已连接"):
            self.stop_process_param_service()
        else:
            self.start_process_param_service()
        if self.process_param_dialog is not None:
            self.process_param_dialog.set_process_state(self.services.process_plc_state)

    def on_process_param_state_changed(self, state):
        # 后台服务发来的连接状态，保存起来；弹窗打开时也同步刷新。
        self.services.process_plc_state = state
        if self.process_param_dialog is not None:
            self.process_param_dialog.set_process_state(self.services.process_plc_state)

    def on_process_param_connect_result(self, success, message):
        if success:
            self.logger.info("工艺 PLC 连接成功")
            self.services.bus.log.emit("工艺 PLC 连接成功")
        elif message:
            self.logger.error(f"工艺 PLC 连接失败: {message}")
            self.services.bus.log.emit(f"工艺 PLC 连接失败: {message}")

    def _on_process_param_thread_stopped(self):
        # 线程退出后清理引用，避免下次无法重新启动。
        self.services.process_plc_thread = None
        self.services.process_plc_service = None
        self.services.process_plc_state = "未连接"
        if self.process_param_dialog is not None:
            self.process_param_dialog.set_process_state(self.services.process_plc_state)

    def get_process_param_values(self):
        """
        外部读取工艺参数用。
        已连接时返回布尔状态和 PLC 里读到的工艺参数；未连接时 values 返回 None。
        """
        state = self.services.process_plc_state
        result = {"state": False, "values": None}
        if state != "已连接":
            return result

        process_service = self.services.process_plc_service
        if process_service is None:
            return result

        row = PlcProcessParamOffsetTable.select().first()
        if row is None:
            return result

        offset_dict = {}
        for field_name, _label in PLC_PROCESS_PARAM_OFFSETS:
            offset_dict[field_name] = getattr(row, field_name)

        try:
            result["state"] = True
            result["values"] = process_service.read_process_params(offset_dict)
        except Exception as exc:
            result["state"] = False
            result["values"] = None
            self.logger.error(f"读取工艺参数失败: {exc}")
            self.services.bus.log.emit(f"读取工艺参数失败: {exc}")
        return result

    def _torque_inherited_db(self, plc_id):
        service = self.services.plc_services.get(plc_id)
        if service is not None and self.get_plc_state(plc_id) == '已连接':
            return service._db_number
        row = PlcTable.get_or_none(PlcTable.id == plc_id)
        if row is None:
            raise ValueError('PLC 已删除或不存在')
        return row.plc_db_number

    def open_torque_dialog(self):
        row = self.get_current_row()
        if row is None:
            return
        if self.torque_dialog is not None:
            self.torque_dialog.close()
        self.torque_dialog = TorqueSettingsDialog(
            self.services, row.id, lambda plc_id=row.id: self._torque_inherited_db(plc_id), self)
        self.torque_dialog.show()

    def get_field_offsets(self, plc_id):
        offset_row = (
            PlcFieldOffsetTable
            .select()
            .where(PlcFieldOffsetTable.plc_id == plc_id)
            .first()
        )
        if offset_row is None:
            QMessageBox.warning(self, "提示", f"PLC {plc_id} 未配置偏移数据")
            return None

        return {
            field_name: getattr(offset_row, field_name)
            for field_name, _label in PLC_FIELD_OFFSETS
        }

    def save_current(self):
        row = self.get_current_row()
        if row is None:
            self.logger.error("plc_table表无此数据")
            self.services.bus.log.emit("plc_table表无此数据")
            QMessageBox.warning(self, "提示", "plc_table表无此数据")
            return
        try:
            # 从界面收集数据，并把需要为整数的字段先转换掉。
            values = {
                "plc_name": text_or_none(self.ed_name.text()),
                "plc_ip": text_or_none(self.ed_ip.text()),
                "plc_rack": int_or_none(self.ed_rack.text(), "机架号"),
                "plc_slot": int_or_none(self.ed_slot.text(), "槽号"),
                "plc_db_number": int_or_none(self.ed_db_number.text(), "数据块号"),
                "plc_poll_interval_ms": int_or_none(self.ed_poll_interval.text(), "轮询时间"),
                "plc_reconnect_interval_ms": int_or_none(
                    self.ed_reconnect_interval.text(), "断线重连时间"
                ),
            }
        except ValueError as exc:
            QMessageBox.warning(self, "提示", f"{str(exc)}")
            return

        try:
            # 保存后重新按当前下拉框选中的 id 查询一次，确保界面显示数据库最新值。
            PlcTable.update(**values).where(PlcTable.id == row.id).execute()
            self.on_combo_changed()
            self.btn_save.setText("保存")
            self.logger.info(f"PLC {row.id} 保存成功")
            self.services.bus.log.emit(f"PLC {row.id} 保存成功")
            self._emit_plc_changed()
            QMessageBox.information(self, "成功", f"PLC {row.id}      配置保存成功!")
        except Exception as exc:
            self.logger.error(f"保存 PLC 配置失败: {exc}")
            self.services.bus.log.emit(f"保存 PLC 配置失败: {exc}")
            QMessageBox.warning(self, "提示", f"保存 PLC 配置失败: {exc}")

    def toggle_state(self):
        row = self.get_current_row()
        if row is None:
            self.logger.error("plc_table表无此数据")
            self.services.bus.log.emit("plc_table表无此数据")
            QMessageBox.warning(self, "提示", "plc_table表无此数据")
            return

        # plc_open_state: 0=关闭, 1=开启
        new_state = 0 if int(row.plc_open_state) == 1 else 1
        try:
            PlcTable.update(plc_open_state=new_state).where(PlcTable.id == row.id).execute()
            self.on_combo_changed()
            self.logger.info(f"PLC {row.id}: {row.plc_name or '-'} 开机自启动 已{'开启' if new_state == 1 else '关闭'}")
            self.services.bus.log.emit(
                f"PLC {row.id}: {row.plc_name or '-'} 开机自启动 已{'开启' if new_state == 1 else '关闭'}"
            )
            QMessageBox.information(self, "成功",
                                    f"PLC {row.id}: {row.plc_name or '-'} 开机自启动 已{'开启' if new_state == 1 else '关闭'}")
        except Exception as exc:
            self.logger.error(f"更新 PLC 状态失败: {exc}")
            self.services.bus.log.emit(f"更新 PLC 状态失败: {exc}")
            QMessageBox.warning(self, "提示", f"更新 PLC 状态失败: {exc}")

    def connect_plc(self):
        self.logger.info(f"点击连接按钮")
        row = self.get_current_row()
        if row is None:
            self.logger.error("plc_table表无此数据")
            self.services.bus.log.emit("plc_table表无此数据")
            QMessageBox.warning(self, "提示", "plc_table表无此数据")
            return

        # 连接按钮四态：未连接=连接，连接中=禁用，重连中=断开，已连接=断开
        state = self.get_plc_state(row.id)
        self.logger.info(f"当前{row.id}的状态：{state}")
        if state == "已连接":
            self.disconnect_row(row.id)
            return
        if state == "重连中":
            self.set_connect_label("重连中...")
            self.disconnect_row(row.id)
            return
        if state == "连接中":
            # 当前这条 PLC 还在连接中，不重复发起连接
            self.set_connect_label("连接中...")
            self.apply_connect_button_state(row.id)
            return
        self.start_connect(row)

    def start_connect(self, row):
        if row.id in getattr(self, '_torque_delete_requests', {}).values():
            return
        if self.get_plc_state(row.id) != "未连接":
            return
        old_service = self.services.plc_services.get(row.id)
        old_thread = self.services.plc_threads.get(row.id)
        if old_service is not None or old_thread is not None:
            self.services.bus.log.emit(f"PLC {row.id} 连接线程仍在运行，不重复创建")
            self.logger.info(f"PLC {row.id} 连接线程仍在运行，不重复创建")
            return

        field_offsets_dict = self.get_field_offsets(row.id)
        if field_offsets_dict is None:
            return
        plc_service = PlcService(
            bus=self.services.bus,
            on_start_requested=self.services._start_from_plc,
            ip=row.plc_ip,
            rack=row.plc_rack,
            slot=row.plc_slot,
            db_number=row.plc_db_number,
            poll_interval_ms=row.plc_poll_interval_ms,
            reconnect_interval_ms=row.plc_reconnect_interval_ms,
            auto_reset_bit=True,
            field_offsets_dict=field_offsets_dict,
            plc_id=row.id,
            torque_store=getattr(self.services, 'torque_store', None),
            torque_config=(self.services.torque_store.get_config(row.id)
                           if getattr(self.services, 'torque_store', None) is not None else None),
            service_generation=uuid4().hex,
        )

        connect_thread = QThread(self)
        plc_service.moveToThread(connect_thread)
        connect_thread.started.connect(plc_service.start_plc_service)
        connect_thread.finished.connect(plc_service.deleteLater)

        if self.combo_plc.currentData() == row.id:
            self.set_connect_label("连接中...")
            self.btn_connect.setEnabled(False)
        self.services.plc_services[row.id] = plc_service
        self.services.plc_threads[row.id] = connect_thread
        coordinator = getattr(self.services, 'torque_config_coordinator', None)
        if coordinator is not None:
            coordinator.bind_service(row.id, plc_service)

        def on_connect_finished(success, message):
            self._on_connect_finished(row.id, plc_service, success)

        def on_connect_thread_stopped():
            self._on_connect_thread_stopped(row.id, connect_thread)

        def on_state_changed(state, plc_id=row.id):
            if self.services.plc_services.get(plc_id) is plc_service:
                self.on_plc_state_changed(plc_id, state)

        # 连接结束后，把这次对应的 plc_id 和 plc_service 一起传回处理函数。
        plc_service.connect_result.connect(on_connect_finished)
        plc_service.state_changed.connect(on_state_changed)
        self.services.bus.write_test_result_sign.connect(plc_service.last_write_test_result_to_plc)
        self.services.bus.write_limit_sign.connect(plc_service.last_write_limit_to_plc)
        # 线程彻底结束后，把这条 PLC 对应的线程记录清掉。
        connect_thread.finished.connect(on_connect_thread_stopped)
        # 启动后台连接线程，避免界面卡住。
        connect_thread.start()

    def _on_connect_finished(self, plc_id, plc_service, success):
        if self.services.plc_services.get(plc_id) is not plc_service:
            return
        row = self.get_current_row()
        # 连接成功后保留最近一次成功连接的服务；具体状态由 PlcService 的状态信号回传。
        if success and plc_service is not None:
            self.services.plc = plc_service
            self.services.bus.log.emit(f"连接成功: {plc_id}")

    def _on_connect_thread_stopped(self, plc_id, stopped_thread=None):
        if stopped_thread is not None and self.services.plc_threads.get(plc_id) is not stopped_thread:
            return
        current_thread = self.services.plc_threads.get(plc_id)
        if current_thread is not None and current_thread.isRunning():
            return
        self.logger.info(f"plc {plc_id} 线程已停止")
        self.services.plc_threads.pop(plc_id, None)
        self.services.plc_services.pop(plc_id, None)
        self._disconnecting_threads.discard(current_thread)
        # A blocking join can finish before the queued service state signal is
        # delivered. Publish the confirmed owner's final state here on the UI
        # thread; stale callbacks above must not change a replacement's state.
        self.on_plc_state_changed(plc_id, "未连接")
        self.logger.info(f"plc {plc_id} 后台plc_services：{getattr(self.services, "plc_services", {}).get(plc_id)}, "
                         f"后台线程{getattr(self.services, "plc_threads", {}).get(plc_id)}")

    def disconnect_row(self, row_id):
        # 断开时先停 service，再退出对应线程，最后把状态改回未连接。
        plc_service = getattr(self.services, "plc_services", {}).get(row_id)
        plc_thread = getattr(self.services, "plc_threads", {}).get(row_id)
        self.logger.info(f"断开前正在运行的plc_services:{plc_service} , 线程{plc_thread}")
        if plc_thread is not None and not plc_thread.isRunning():
            self._on_connect_thread_stopped(row_id, plc_thread)
            return
        if plc_service and plc_thread not in self._disconnecting_threads:
            try:
                QMetaObject.invokeMethod(
                    plc_service,
                    "stop_plc_service",
                    Qt.ConnectionType.BlockingQueuedConnection,
                )
            except Exception as exc:
                self.services.bus.log.emit(f"plc{row_id}断开失败: {exc}")
                self.logger.error(f"plc{row_id}断开失败: {exc}")
                return
        if plc_thread is not None:
            self._disconnecting_threads.add(plc_thread)
            plc_thread.quit()
            if plc_thread.wait(2000):
                self._on_connect_thread_stopped(row_id, plc_thread)
            else:
                self.services.bus.log.emit(f"plc{row_id}线程尚未停止，请稍后重试")

    def set_lb_text(self, row_id):
        state = self.get_plc_state(row_id)
        if state == "连接中":
            return "连接中..."
        if state == "重连中":
            return "重连中..."
        if state == "已连接":
            return "连接成功"
        return "未连接"

    def set_connect_label(self, text):
        self.lb_connect_state.setText(text)
        if text == "连接成功":
            self.lb_connect_state.setStyleSheet("color: #16a34a;")
        elif text.startswith("连接失败"):
            self.lb_connect_state.setStyleSheet("color: #dc2626;")
        else:
            self.lb_connect_state.setStyleSheet("")

    def get_plc_state(self, row_id):
        return self.services.plc_state.get(
            row_id, "未连接"
        )

    def set_plc_state(self, row_id, state):
        self.services.plc_state[row_id] = state

    def on_plc_state_changed(self, plc_id, state):
        self.set_plc_state(plc_id, state)
        dialog = getattr(self, 'torque_dialog', None)
        if dialog is not None and dialog.plc_id == plc_id:
            dialog.refresh_status()
        row = self.get_current_row()
        if row is None or row.id != plc_id:
            return
        if state == "已连接":
            self.set_connect_label("连接成功")
        elif state == "连接中":
            self.set_connect_label("连接中...")
        elif state == "重连中":
            self.set_connect_label("重连中...")
        else:
            self.set_connect_label("未连接")
        self.apply_connect_button_state(plc_id)

    def apply_connect_button_state(self, row_id):
        # 根据状态刷新按钮文案和可点击状态。
        state = self.get_plc_state(row_id)
        if state == "连接中":
            self.btn_connect.setText("连接中")
            self.btn_connect.setEnabled(False)
        elif state == "重连中":
            self.btn_connect.setText("断开")
            self.btn_connect.setEnabled(True)
        elif state == "已连接":
            self.btn_connect.setText("断开")
            self.btn_connect.setEnabled(True)
        else:
            self.btn_connect.setText("连接")
            self.btn_connect.setEnabled(True)

    def _set_fields_enabled(self, enabled):
        # 没有 PLC 数据时禁用表单输入。
        for edit in (
                self.ed_name,
                self.ed_ip,
                self.ed_rack,
                self.ed_slot,
                self.ed_db_number,
                self.ed_poll_interval,
                self.ed_reconnect_interval,
        ):
            edit.setEnabled(enabled)

    def set_permission_enabled(self, allowed: bool) -> None:
        """
        设置权限
        """
        for widget in (
                self.ed_name,
                self.ed_ip,
                self.ed_rack,
                self.ed_slot,
                self.ed_db_number,
                self.ed_poll_interval,
                self.ed_reconnect_interval,
                self.btn_add_plc,
                self.btn_delete_plc,
                self.btn_save,
                self.btn_toggle,
                self.btn_connect,
                self.btn_field_offset,
                self.btn_process_param_offset,
                self.btn_torque,
        ):
            widget.setEnabled(allowed)
        self.combo_plc.setEnabled(True)
