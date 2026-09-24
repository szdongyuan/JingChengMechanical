"""Per-PLC torque configuration; persistence belongs to application jobs."""
from PyQt6.QtCore import pyqtSlot
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout,
                            QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

from app.service.torque.types import TorqueConfig
from app.service.torque.validation import parse_offset, resolve_addresses, validate_db_number
from consts.style import APP_QSS
from ui.popups.torque_table_dialog import TorqueTableDialog


class TorqueSettingsDialog(QDialog):
    def __init__(self, services, plc_id, inherited_db, parent=None):
        super().__init__(parent)
        self.services, self.plc_id = services, plc_id
        # Resolve again at save time: a connection may have changed while open.
        self.inherited_db = inherited_db
        self._request_id = None
        self._closed = False
        self.table_dialog = None
        self.setWindowTitle(f'扭矩配置 · PLC {plc_id}')
        self.setStyleSheet(APP_QSS)
        self.resize(560, 470)
        self.setMinimumSize(440, 420)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox('启用扭矩功能')
        layout.addWidget(self.enabled)
        self.match_type = QComboBox()
        self.match_type.addItem('型号', 'model')
        self.match_type.addItem('SN', 'sn')
        self.read_db, self.read_offset = QLineEdit(), QLineEdit()
        self.write_db, self.write_offset = QLineEdit(), QLineEdit()
        form = QFormLayout()
        form.addRow('查表依据', self.match_type)
        for title, db, offset in (('读取', self.read_db, self.read_offset),
                                  ('回传', self.write_db, self.write_offset)):
            db.setPlaceholderText('留空：继承 PLC DB')
            offset.setPlaceholderText('字节地址，例如 20 或 20.0')
            form.addRow(f'{title} DB 块号', db)
            form.addRow(f'{title}字节偏移', offset)
        layout.addLayout(form)
        self.explanation = QLabel(
            '固定类型 REAL，每个地址占 4 字节；与 PLC 单位一致，无换算。\n'
            'DB 留空沿用 PLC 当前已应用的 DB；断开时按已保存 DB 校验。\n'
            '读取为 0 时查表回传；非零时采用 PLC 值。保存配置不写入扭矩。')
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        self.saved_status, self.applied_status, self.error_label = QLabel(), QLabel(), QLabel()
        for label in (self.saved_status, self.applied_status, self.error_label):
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addStretch()
        buttons = QHBoxLayout()
        self.manage_button = QPushButton('管理扭矩表')
        self.save_button = QPushButton('保存')
        self.save_button.setObjectName('Primary')
        self.close_button = QPushButton('关闭')
        buttons.addWidget(self.manage_button)
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.save_button.clicked.connect(self.save)
        self.manage_button.clicked.connect(self.open_table)
        self.close_button.clicked.connect(self.reject)
        services.torque_jobs.finished.connect(self._job_finished)
        services.bus.plc_torque_config_saved.connect(self._config_saved)
        services.bus.plc_torque_config_applied.connect(self._config_applied)
        self._load_config()
        self.refresh_status()

    def _load_config(self):
        config = self.services.torque_store.get_config(self.plc_id)
        self.enabled.setChecked(config.enabled)
        self.match_type.setCurrentIndex(self.match_type.findData(config.match_type))
        for widget, value in ((self.read_db, config.read_db_number),
                              (self.read_offset, config.read_byte_offset),
                              (self.write_db, config.write_db_number),
                              (self.write_offset, config.write_byte_offset)):
            widget.setText('' if value is None else str(value))

    def refresh_status(self):
        config = self.services.torque_store.get_config(self.plc_id)
        state = '已启用' if config.enabled else '未启用'
        self.saved_status.setText(f'已保存 · {state} · 版本 {config.revision}'
                                  if config.revision else f'当前配置 · {state}')
        receipt = self.services.torque_config_coordinator.get_applied_receipt(self.plc_id)
        if receipt is None:
            self.applied_status.setText('运行配置：待应用')
        elif receipt.success:
            self.applied_status.setText(f'运行配置：已应用 · 版本 {receipt.config_revision}')
        else:
            self.applied_status.setText(f'运行配置：应用失败 · {receipt.detail}')

    def save(self):
        try:
            config = TorqueConfig(
                self.plc_id, self.enabled.isChecked(), self.match_type.currentData(),
                validate_db_number(self.read_db.text().strip() or None),
                parse_offset(self.read_offset.text()) if self.read_offset.text().strip() else None,
                validate_db_number(self.write_db.text().strip() or None),
                parse_offset(self.write_offset.text()) if self.write_offset.text().strip() else None,
            )
            db = self.inherited_db()
            resolve_addresses(config, db)
            self._request_id = self.services.torque_jobs.submit('save_config', config, db)
        except (ValueError, RuntimeError) as exc:
            self.error_label.setText(str(exc))
            return
        self.error_label.clear()
        self.save_button.setEnabled(False)
        self.saved_status.setText('保存中…')

    @pyqtSlot(object)
    def _job_finished(self, result):
        if result.request_id != self._request_id:
            return
        self._request_id = None
        self.save_button.setEnabled(True)
        if result.error is not None or result.cancelled:
            self.error_label.setText(str(result.error) if result.error else '保存已取消')
        else:
            # Another save may have advanced the store before this queued result.
            self._load_config()
        self.refresh_status()

    @pyqtSlot(object)
    def _config_saved(self, config):
        if config.plc_id == self.plc_id:
            self.refresh_status()

    @pyqtSlot(object)
    def _config_applied(self, receipt):
        if receipt.plc_id == self.plc_id:
            self.refresh_status()

    def open_table(self):
        if self.table_dialog is None or not self.table_dialog.isVisible():
            self.table_dialog = TorqueTableDialog(self.services, self)
        self.table_dialog.show()
        self.table_dialog.raise_()

    def _detach(self):
        if not self._closed:
            self._closed = True
            self.services.torque_jobs.finished.disconnect(self._job_finished)
            self.services.bus.plc_torque_config_saved.disconnect(self._config_saved)
            self.services.bus.plc_torque_config_applied.disconnect(self._config_applied)
            if self.table_dialog is not None:
                self.table_dialog.close()
    def closeEvent(self, event):
        self._detach()
        super().closeEvent(event)

    def done(self, result):
        self._detach()
        super().done(result)
