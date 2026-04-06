from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget, QMessageBox,
)

from base.utils import int_or_none, text_or_empty, text_or_none
from model.plc_table import PlcTable


class PlcSettingsModule(QWidget):
    def __init__(self, services):
        super().__init__()
        self.services = services

        self.init_ui()
        self.init_fun()
        self.reload_rows()

    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # 顶部下拉框只显示 PLC 的 id。
        self.combo_plc = QComboBox()
        self.combo_plc.setMinimumWidth(220)
        root.addWidget(self.combo_plc, 0)

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
        self.ed_byte_index = QLineEdit()
        self.ed_bit_index = QLineEdit()
        self.ed_poll_interval = QLineEdit()
        self.ed_reconnect_interval = QLineEdit()
        # 状态只展示当前值，开关通过按钮切换。
        self.lb_state = QLabel("-")

        for edit in (
            self.ed_name,
            self.ed_ip,
            self.ed_rack,
            self.ed_slot,
            self.ed_db_number,
            self.ed_byte_index,
            self.ed_bit_index,
            self.ed_poll_interval,
            self.ed_reconnect_interval,
        ):
            edit.setMinimumHeight(30)
            edit.setObjectName("PlcField")

        form.addRow("PLC名称：", self.ed_name)
        form.addRow("IP：", self.ed_ip)
        form.addRow("机架号：", self.ed_rack)
        form.addRow("槽号：", self.ed_slot)
        form.addRow("数据块号：", self.ed_db_number)
        form.addRow("字节偏移：", self.ed_byte_index)
        form.addRow("位偏移：", self.ed_bit_index)
        form.addRow("轮询时间(ms)：", self.ed_poll_interval)
        form.addRow("断线重连时间(ms)：", self.ed_reconnect_interval)
        form.addRow("当前状态：", self.lb_state)
        root.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)

        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("Primary")
        self.btn_save.setFixedWidth(96)

        self.btn_toggle = QPushButton("开启")
        self.btn_toggle.setObjectName("Primary")
        self.btn_toggle.setFixedWidth(96)

        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_toggle)
        btn_row.addStretch(1)
        root.addLayout(btn_row)
        root.addStretch(1)

    def init_fun(self):
        self.combo_plc.currentIndexChanged.connect(self.on_combo_changed)
        self.btn_save.clicked.connect(self.save_current)
        self.btn_toggle.clicked.connect(self.toggle_state)

    def reload_rows(self):
        # 每次重载都从数据库重新取 PLC 列表，并刷新下拉框。
        try:
            rows = list(PlcTable.select().order_by(PlcTable.id))
        except Exception as exc:
            rows = []
            self.services.bus.log.emit(f"读取 PLC 配置失败: {exc}")
            QMessageBox.warning(self, "提示", f"读取 PLC 配置失败: {exc}")

        self.combo_plc.blockSignals(True)
        self.combo_plc.clear()
        for row in rows:
            self.combo_plc.addItem(str(row.id), row.id)
        self.combo_plc.blockSignals(False)

        has_rows = bool(rows)
        self.combo_plc.setEnabled(has_rows)
        self.btn_save.setEnabled(has_rows)
        self.btn_toggle.setEnabled(has_rows)
        self._set_fields_enabled(has_rows)

        if has_rows:
            self.combo_plc.setCurrentIndex(0)
            self.on_combo_changed()
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
        self.ed_name.setText(text_or_empty(row.plc_name))
        self.ed_ip.setText(text_or_empty(row.plc_ip))
        self.ed_rack.setText(text_or_empty(row.plc_rack))
        self.ed_slot.setText(text_or_empty(row.plc_slot))
        self.ed_db_number.setText(text_or_empty(row.plc_db_number))
        self.ed_byte_index.setText(text_or_empty(row.plc_byte_index))
        self.ed_bit_index.setText(text_or_empty(row.plc_bit_index))
        self.ed_poll_interval.setText(text_or_empty(row.plc_poll_interval_ms))
        self.ed_reconnect_interval.setText(text_or_empty(row.plc_reconnect_interval_ms))

        state = 1 if int(row.plc_state or 0) == 1 else 0
        self.lb_state.setText("开启" if state == 1 else "关闭")
        self.btn_toggle.setText("关闭" if state == 1 else "开启")

    def _render_empty(self):
        # 无数据时清空界面，避免保留上一条 PLC 的显示内容。
        for edit in (
            self.ed_ip,
            self.ed_rack,
            self.ed_slot,
            self.ed_db_number,
            self.ed_byte_index,
            self.ed_bit_index,
            self.ed_poll_interval,
            self.ed_reconnect_interval,
        ):
            edit.setText("")
        self.lb_state.setText("-")
        self.btn_toggle.setText("开启")

    def save_current(self):
        row = self.get_current_row()
        if row is None:
            return
        try:
            # 从界面收集数据，并把需要为整数的字段先转换掉。
            values = {
                "plc_name": text_or_none(self.ed_name.text()),
                "plc_ip": text_or_none(self.ed_ip.text()),
                "plc_rack": int_or_none(self.ed_rack.text(), "机架号"),
                "plc_slot": int_or_none(self.ed_slot.text(), "槽号"),
                "plc_db_number": int_or_none(self.ed_db_number.text(), "数据块号"),
                "plc_byte_index": int_or_none(self.ed_byte_index.text(), "字节偏移"),
                "plc_bit_index": int_or_none(self.ed_bit_index.text(), "位偏移"),
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
            self.services.bus.log.emit(f"PLC {row.id} 保存成功")
            QMessageBox.information(self, "成功", f"PLC {row.id} 保存成功")
        except Exception as exc:
            self.services.bus.log.emit(f"保存 PLC 配置失败: {exc}")
            QMessageBox.warning(self, "提示", f"保存 PLC 配置失败: {exc}")

    def toggle_state(self):
        row = self.get_current_row()
        if row is None:
            return

        # plc_state: 0=关闭, 1=开启
        new_state = 0 if int(row.plc_state or 0) == 1 else 1
        try:
            PlcTable.update(plc_state=new_state).where(PlcTable.id == row.id).execute()
            self.on_combo_changed()
            self.services.bus.log.emit(
                f"PLC {row.id}: {row.plc_name or '-'} 已{'开启' if new_state == 1 else '关闭'}"
            )
            QMessageBox.information(self, "成功",
                                    f"PLC {row.id}: {row.plc_name or '-'} 已{'开启' if new_state == 1 else '关闭'}")
        except Exception as exc:
            self.services.bus.log.emit(f"更新 PLC 状态失败: {exc}")
            QMessageBox.warning(self, "提示", f"更新 PLC 状态失败: {exc}")

    def _set_fields_enabled(self, enabled):
        # 没有 PLC 数据时禁用表单输入。
        for edit in (
            self.ed_name,
            self.ed_ip,
            self.ed_rack,
            self.ed_slot,
            self.ed_db_number,
            self.ed_byte_index,
            self.ed_bit_index,
            self.ed_poll_interval,
            self.ed_reconnect_interval,
        ):
            edit.setEnabled(enabled)
