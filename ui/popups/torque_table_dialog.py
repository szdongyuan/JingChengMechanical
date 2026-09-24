"""Shared rules, background Excel work, and revision-bound import previews."""
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
                            QFormLayout, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
                            QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from app.service.torque.store import TorquePreviewConflict
from app.service.torque.validation import normalize_key, validate_torque
from consts.style import APP_QSS


def format_torque(value):
    """Shortest readable decimal that still encodes the same validated REAL."""
    for precision in range(1, 10):
        text = format(value, f'.{precision}g')
        try:
            if validate_torque(text) == value:
                return text
        except ValueError:
            continue
    return str(value)


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _set_row(table, row_number, values):
    for column, value in enumerate(values):
        item = QTableWidgetItem(str(value))
        item.setToolTip(str(value))
        table.setItem(row_number, column, item)


class TorqueRuleDialog(QDialog):
    def __init__(self, owner, identity=None, value=None):
        super().__init__(owner)
        self.owner, self.identity = owner, identity
        self.setWindowTitle('编辑扭矩记录' if identity else '新增扭矩记录')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(440, 240)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.match_type = QComboBox()
        self.match_type.addItem('型号', 'model')
        self.match_type.addItem('SN', 'sn')
        self.match_key, self.torque = QLineEdit(), QLineEdit()
        if identity:
            self.match_type.setCurrentIndex(self.match_type.findData(identity[0]))
            self.match_key.setText(identity[1])
            self.torque.setText(format_torque(value))
        form.addRow('匹配类型', self.match_type)
        form.addRow('型号或 SN（文本）', self.match_key)
        form.addRow('扭矩（与 PLC 单位一致）', self.torque)
        layout.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        self.save_button, cancel = QPushButton('保存'), QPushButton('取消')
        self.save_button.setObjectName('Primary')
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.save_button.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)

    def save(self):
        try:
            args = (self.match_type.currentData(), normalize_key(self.match_key.text()),
                    validate_torque(self.torque.text()))
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        operation = 'update_rule' if self.identity else 'create_rule'
        if self.identity:
            args = (*self.identity, *args)
        if self.owner.submit(operation, *args):
            self.save_button.setEnabled(False)


class TorqueImportPreviewDialog(QDialog):
    def __init__(self, owner, preview):
        super().__init__(owner)
        self.owner, self.preview = owner, preview
        self.setWindowTitle('Excel 导入预览')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(860, 520)
        self.setMinimumSize(600, 360)
        layout = QVBoxLayout(self)
        self.status = QLabel(f'新增 {len(preview.additions)} 条，覆盖 {len(preview.overwrites)} 条，错误 {len(preview.errors)} 条。与 PLC 单位一致。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = _table(['Excel 行', '匹配类型', '型号或 SN', '旧值', '新值', '动作 / 错误'])
        self.table.setRowCount(len(preview.additions) + len(preview.overwrites) + len(preview.errors))
        rows = []
        for row in preview.additions:
            rows.append((row.row_number, '型号' if row.match_type == 'model' else 'SN', row.match_key, '—', format_torque(row.torque_value), '新增'))
        for change in preview.overwrites:
            row = change.row
            rows.append((row.row_number, '型号' if row.match_type == 'model' else 'SN', row.match_key, format_torque(change.old_value), format_torque(row.torque_value), '覆盖'))
        rows.extend((error.row_number, '—', '—', '—', '—', error.message) for error in preview.errors)
        for index, values in enumerate(sorted(rows, key=lambda row: row[0])):
            _set_row(self.table, index, values)
        for column, width in enumerate((75, 80, 160, 95, 95)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.confirm_button, self.cancel_button = QPushButton('确认导入'), QPushButton('取消')
        self.confirm_button.setObjectName('Primary')
        self.confirm_button.setEnabled(not preview.errors and bool(preview.rows))
        buttons.addStretch()
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.confirm_button.clicked.connect(self.confirm)
        self.cancel_button.clicked.connect(self.reject)
        self.check_revision()

    def invalidate(self):
        self.confirm_button.setEnabled(False)
        self.status.setText('扭矩表已变化，请关闭预览并重新导入文件以生成预览。')

    def check_revision(self):
        if self.preview.revision != self.owner.services.torque_store.snapshot().revision:
            self.invalidate()
            return False
        return True

    def confirm(self):
        if self.preview.errors or not self.preview.rows or not self.check_revision():
            return
        if self.owner.submit('commit_import', self.preview):
            self.confirm_button.setEnabled(False)
            self.status.setText('正在提交导入…关闭窗口不影响已开始的事务。')


class TorqueTableDialog(QDialog):
    MUTATIONS = {'create_rule', 'upsert_rule', 'update_rule', 'delete_rule', 'commit_import'}

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        self._request_id = None
        self._operation = None
        self._closed = False
        self.preview_dialog = self.editor_dialog = None
        self.setWindowTitle('共享扭矩表')
        self.setStyleSheet(APP_QSS)
        self.resize(760, 560)
        self.setMinimumSize(520, 380)
        layout = QVBoxLayout(self)
        note = QLabel('全软件共享 · 型号 / SN 精确匹配 · 与 PLC 单位一致')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索型号或 SN')
        layout.addWidget(self.search)
        self.table = _table(['匹配类型', '型号或 SN', '扭矩'])
        self.table.setColumnWidth(1, 280)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.add_button, self.edit_button, self.delete_button = QPushButton('新增'), QPushButton('编辑'), QPushButton('删除')
        self.import_button, self.template_button = QPushButton('Excel 导入'), QPushButton('下载模板')
        self.import_button.setObjectName('Primary')
        for button in (self.add_button, self.edit_button, self.delete_button, self.import_button, self.template_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.search.textChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.add_button.clicked.connect(self.add_rule)
        self.edit_button.clicked.connect(self.edit_rule)
        self.delete_button.clicked.connect(self.delete_rule)
        self.import_button.clicked.connect(self.import_file)
        self.template_button.clicked.connect(self.download_template)
        services.torque_jobs.finished.connect(self._job_finished)
        self.refresh()

    def refresh(self):
        search = self.search.text().strip().casefold()
        values = self.services.torque_store.snapshot().values
        rows = [(key, value) for key, value in sorted(values.items()) if search in key[1].casefold()]
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for index, ((kind, key), value) in enumerate(rows):
            _set_row(self.table, index, ('型号' if kind == 'model' else 'SN', key, format_torque(value)))
            self.table.item(index, 0).setData(Qt.ItemDataRole.UserRole, (kind, key))
        self._update_buttons()

    def _selected(self):
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        return tuple(self.table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    def _update_buttons(self):
        idle = self._request_id is None
        for button in (self.add_button, self.import_button, self.template_button):
            button.setEnabled(idle)
        for button in (self.edit_button, self.delete_button):
            button.setEnabled(idle and self._selected() is not None)

    def submit(self, operation, *args):
        if self._request_id is not None:
            return False
        try:
            self._request_id = self.services.torque_jobs.submit(operation, *args)
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))
            return False
        self._operation = operation
        self.status.setText('后台处理中…')
        self._update_buttons()
        return True

    def add_rule(self):
        self.editor_dialog = TorqueRuleDialog(self)
        self.editor_dialog.show()

    def edit_rule(self):
        identity = self._selected()
        if identity is not None:
            snapshot = self.services.torque_store.snapshot()
            if identity not in snapshot.values:
                # The worker can publish before its queued UI refresh arrives.
                self.refresh()
                self.status.setText('记录已变更或删除，请重新选择。')
                return
            self.editor_dialog = TorqueRuleDialog(self, identity, snapshot.values[identity])
            self.editor_dialog.show()

    def delete_rule(self):
        identity = self._selected()
        if identity is not None:
            self.submit('delete_rule', *identity)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入扭矩表', '', 'Excel 工作簿 (*.xlsx)')
        if path:
            self.submit('parse_import', path)

    def download_template(self):
        path, _ = QFileDialog.getSaveFileName(self, '保存扭矩表模板', '扭矩表模板.xlsx', 'Excel 工作簿 (*.xlsx)')
        if path:
            if not path.lower().endswith('.xlsx'):
                path += '.xlsx'
            self.submit('write_template', path)

    @pyqtSlot(object)
    def _job_finished(self, result):
        if result.operation in self.MUTATIONS and result.error is None and not result.cancelled:
            self.refresh()
            if self.preview_dialog is not None:
                self.preview_dialog.check_revision()
        if result.request_id != self._request_id:
            return
        self._request_id = self._operation = None
        self._update_buttons()
        if result.error is not None or result.cancelled:
            message = str(result.error) if result.error else '任务已取消'
            self.status.setText(message)
            if result.operation in {'create_rule', 'update_rule'} and self.editor_dialog is not None:
                self.editor_dialog.error_label.setText(message)
                self.editor_dialog.save_button.setEnabled(True)
            if result.operation == 'commit_import' and self.preview_dialog is not None:
                if isinstance(result.error, TorquePreviewConflict):
                    self.preview_dialog.invalidate()
                else:
                    self.preview_dialog.status.setText(f'导入失败：{message}。请重新导入生成预览。')
            return
        if result.operation == 'parse_import':
            self.preview_dialog = TorqueImportPreviewDialog(self, result.value)
            self.preview_dialog.show()
            self.status.setText('解析完成，请检查导入预览。')
        elif result.operation == 'commit_import':
            self.preview_dialog.accept()
            self.status.setText('导入成功，之后的测试将使用最新扭矩表。')
        elif result.operation == 'write_template':
            self.status.setText('模板已保存。型号或 SN 请保持文本格式。')
        else:
            if result.operation in {'create_rule', 'update_rule'} and self.editor_dialog is not None:
                self.editor_dialog.accept()
            self.status.setText('扭矩表已保存。')

    def _detach(self):
        if not self._closed:
            self._closed = True
            if self._operation == 'parse_import':
                self.services.torque_jobs.cancel(self._request_id)
            self.services.torque_jobs.finished.disconnect(self._job_finished)
            for dialog in (self.editor_dialog, self.preview_dialog):
                if dialog is not None:
                    dialog.close()
    def closeEvent(self, event):
        self._detach()
        super().closeEvent(event)

    def done(self, result):
        self._detach()
        super().done(result)
