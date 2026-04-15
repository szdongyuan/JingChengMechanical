from __future__ import annotations

from PyQt6.QtCore import QMetaObject, QThread, Qt
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

from app.service.log_manager import LogManager
from base.utils import int_or_none, text_or_empty, text_or_none
from app.service.plc_service import PlcService
from model.plc_table import PlcTable


class PlcSettingsModule(QWidget):
    _SAVE_BLOCKED_STATES = {"连接中", "重连中", "已连接"}

    def __init__(self, services):
        super().__init__()
        self.logger = LogManager.set_log_handler("core")
        self.services = services
        # plc_services 是挂在ServiceContainer 容器对象上的一个属性
        self.services.plc_services = {}
        self.services.plc_threads = {}
        self.services.plc_state = {}

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
        # 状态只展示当前值，开关和连接通过按钮触发。
        self.lb_state = QLabel("-")
        self.lb_connect_state = QLabel("-")

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
            edit.setMinimumHeight(25)
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
        form.addRow("开机启动：", self.lb_state)
        form.addRow("连接状态：", self.lb_connect_state)
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

        self.btn_connect = QPushButton("连接")
        self.btn_connect.setObjectName("Primary")
        self.btn_connect.setFixedWidth(96)

        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_toggle)
        btn_row.addWidget(self.btn_connect)
        btn_row.addStretch(1)
        root.addLayout(btn_row)
        root.addStretch(1)

    def init_fun(self):
        self.combo_plc.currentIndexChanged.connect(self.on_combo_changed)
        self.btn_save.clicked.connect(self.save_current)
        self.btn_toggle.clicked.connect(self.toggle_state)
        self.btn_connect.clicked.connect(self.connect_plc)

    def reload_rows(self):
        # 每次重载都从数据库重新取 PLC 列表，并刷新下拉框。
        try:
            rows = list(PlcTable.select().order_by(PlcTable.id))
        except Exception as exc:
            rows = []
            self.services.bus.log.emit(f"读取 PLC 配置失败: {exc}")
            QMessageBox.warning(self, "提示", f"读取 PLC 配置失败: {exc}")

        for row in rows:
            self.services.plc_state[row.id] = "未连接"

        self.combo_plc.blockSignals(True)
        self.combo_plc.clear()
        for row in rows:
            self.combo_plc.addItem(str(row.id), row.id)
        self.combo_plc.blockSignals(False)

        has_rows = bool(rows)
        self.combo_plc.setEnabled(has_rows)
        self.btn_save.setEnabled(has_rows)
        self.btn_toggle.setEnabled(has_rows)
        self.btn_connect.setEnabled(has_rows)
        self._set_fields_enabled(has_rows)

        if has_rows:
            # 默认先显示第一条 PLC，并把开机自动连接的项拉起来。
            self.combo_plc.setCurrentIndex(0)
            self.on_combo_changed()
            for row in rows:
                if self._is_open_state_enabled(row.plc_open_state):
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
        self.ed_name.setText(text_or_empty(row.plc_name))
        self.ed_ip.setText(text_or_empty(row.plc_ip))
        self.ed_rack.setText(text_or_empty(row.plc_rack))
        self.ed_slot.setText(text_or_empty(row.plc_slot))
        self.ed_db_number.setText(text_or_empty(row.plc_db_number))
        self.ed_byte_index.setText(text_or_empty(row.plc_byte_index))
        self.ed_bit_index.setText(text_or_empty(row.plc_bit_index))
        self.ed_poll_interval.setText(text_or_empty(row.plc_poll_interval_ms))
        self.ed_reconnect_interval.setText(text_or_empty(row.plc_reconnect_interval_ms))

        is_open = self._is_open_state_enabled(row.plc_open_state)  # 是否已开启开机自启
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
            self.ed_byte_index,
            self.ed_bit_index,
            self.ed_poll_interval,
            self.ed_reconnect_interval,
        ):
            edit.setText("")
        self.lb_state.setText("-")
        self.set_connect_label("-")
        self.btn_toggle.setText("开启")
        self.btn_connect.setText("连接")
        self.btn_connect.setEnabled(False)

    def save_current(self):
        row = self.get_current_row()
        if row is None:
            self.services.bus.log.emit("plc_table表无此数据")
            return
        if self._should_block_save(row.id):
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

    def _should_block_save(self, row_id):
        state = self.get_plc_state(row_id)
        if state not in self._SAVE_BLOCKED_STATES:
            return False

        self.set_connect_label(self.set_lb_text(row_id))
        self.apply_connect_button_state(row_id)
        message = "PLC 正在连接或已连接，请先断开后再保存配置"
        self.services.bus.log.emit(message)
        QMessageBox.warning(self, "提示", message)
        return True

    def toggle_state(self):
        row = self.get_current_row()
        if row is None:
            self.services.bus.log.emit("plc_table表无此数据")
            return

        # plc_open_state: 0=关闭, 1=开启
        new_state = 0 if self._is_open_state_enabled(row.plc_open_state) else 1
        try:
            PlcTable.update(plc_open_state=new_state).where(PlcTable.id == row.id).execute()
            self.on_combo_changed()
            self.services.bus.log.emit(
                f"PLC {row.id}: {row.plc_name or '-'} 开机自启动 已{'开启' if new_state == 1 else '关闭'}"
            )
            QMessageBox.information(self, "成功",
                                    f"PLC {row.id}: {row.plc_name or '-'} 开机自启动 已{'开启' if new_state == 1 else '关闭'}")
        except Exception as exc:
            self.services.bus.log.emit(f"更新 PLC 状态失败: {exc}")
            QMessageBox.warning(self, "提示", f"更新 PLC 状态失败: {exc}")

    def connect_plc(self):
        self.logger.info(f"点击连接按钮")
        row = self.get_current_row()
        if row is None:
            self.services.bus.log.emit("plc_table表无此数据")
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
        if self.get_plc_state(row.id) != "未连接":
            return
        old_service = self.services.plc_services.get(row.id)
        old_thread = self.services.plc_threads.get(row.id)
        if old_service is not None or old_thread is not None:
            self.services.bus.log.emit(f"PLC {row.id} 连接线程仍在运行，不重复创建")
            self.logger.info(f"PLC {row.id} 连接线程仍在运行，不重复创建")
            return

        _, config_error = PlcService.validate_connection_config(
            ip=row.plc_ip,
            rack=row.plc_rack,
            slot=row.plc_slot,
            db_number=row.plc_db_number,
            byte_index=row.plc_byte_index,
            bit_index=row.plc_bit_index,
            poll_interval_ms=row.plc_poll_interval_ms,
            reconnect_interval_ms=row.plc_reconnect_interval_ms,
        )
        if config_error is not None:
            failure_message = f"PLC {row.id} 连接失败: {config_error}"
            self.logger.warning(failure_message)
            self.services.bus.log.emit(failure_message)
            if self.combo_plc.currentData() == row.id:
                self.set_connect_label(f"连接失败: {config_error}")
                self.apply_connect_button_state(row.id)
                QMessageBox.warning(self, "提示", failure_message)
            return

        plc_service = PlcService(
            bus=self.services.bus,
            on_start_requested=self.services._start_from_plc,
            ip=row.plc_ip,
            rack=row.plc_rack,
            slot=row.plc_slot,
            db_number=row.plc_db_number,
            byte_index=row.plc_byte_index,
            bit_index=row.plc_bit_index,
            poll_interval_ms=row.plc_poll_interval_ms,
            reconnect_interval_ms=row.plc_reconnect_interval_ms,
            auto_reset_bit=True,
        )

        connect_thread = QThread(self)
        plc_service.moveToThread(connect_thread)
        connect_thread.started.connect(plc_service.start_plc_service)
        connect_thread.finished.connect(plc_service.deleteLater)

        if self.combo_plc.currentData() == row.id:
            self.set_connect_label("连接中...")
            self.btn_connect.setEnabled(False)
        self.services.plc_threads[row.id] = connect_thread

        def on_connect_finished(success, message):
            self._on_connect_finished(row.id, plc_service, connect_thread, success, message)

        def on_connect_thread_stopped():
            self._on_connect_thread_stopped(row.id)

        def on_state_changed(state, plc_id=row.id):
            self.on_plc_state_changed(plc_id, state)

        # 连接结束后，把这次对应的 plc_id 和 plc_service 一起传回处理函数。
        plc_service.connect_result.connect(on_connect_finished)
        plc_service.state_changed.connect(on_state_changed)
        # 线程彻底结束后，把这条 PLC 对应的线程记录清掉。
        connect_thread.finished.connect(on_connect_thread_stopped)
        # 启动后台连接线程，避免界面卡住。
        connect_thread.start()

    def _on_connect_finished(self, plc_id, plc_service, connect_thread, success, message):
        row = self.get_current_row()

        # 连接成功后记录 service；具体状态由 PlcService 的状态信号回传。
        if success and plc_service is not None:
            self.services.plc_services[plc_id] = plc_service
            # 保留最近一次成功连接的服务，兼容原来单实例用法。
            self.services.plc = plc_service
            self.services.bus.log.emit(f"连接成功: {plc_id}")
        elif plc_service is not None:
            connect_thread.quit()
            connect_thread.wait(2000)

        if row is not None and row.id == plc_id:
            if not success:
                self.set_connect_label(f"连接失败: {message}" if message else "连接失败")

    def _on_connect_thread_stopped(self, plc_id):
        self.logger.info(f"plc {plc_id} 线程已停止")
        self.services.plc_threads.pop(plc_id, None)
        self.services.plc_services.pop(plc_id, None)
        self.logger.info(f"plc {plc_id} 后台plc_services：{getattr(self.services, "plc_services", {}).get(plc_id)}, "
                         f"后台线程{getattr(self.services, "plc_threads", {}).get(plc_id)}")

    def disconnect_row(self, row_id):
        # 断开时先停 service，再退出对应线程，最后把状态改回未连接。
        plc_service = getattr(self.services, "plc_services", {}).get(row_id)
        plc_thread = getattr(self.services, "plc_threads", {}).get(row_id)
        self.logger.info(f"断开前正在运行的plc_services:{plc_service} , 线程{plc_thread}")
        if plc_service:
            try:
                QMetaObject.invokeMethod(
                    plc_service,
                    "stop_plc_service",
                    Qt.ConnectionType.BlockingQueuedConnection,
                )
                self.services.plc_services.pop(row_id, None)
            except Exception as exc:
                self.services.bus.log.emit(f"plc{row_id}断开失败: {exc}")
                self.logger.error(f"plc{row_id}断开失败: {exc}")

        if plc_thread is not None:
            plc_thread.quit()
            plc_thread.wait(2000)

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

    def _is_open_state_enabled(self, value):
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return False

    def get_plc_state(self, row_id):
        return self.services.plc_state.get(
            row_id, "未连接"
        )

    def set_plc_state(self, row_id, state):
        self.services.plc_state[row_id] = state

    def on_plc_state_changed(self, plc_id, state):
        self.set_plc_state(plc_id, state)
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
            self.ed_byte_index,
            self.ed_bit_index,
            self.ed_poll_interval,
            self.ed_reconnect_interval,
        ):
            edit.setEnabled(enabled)
