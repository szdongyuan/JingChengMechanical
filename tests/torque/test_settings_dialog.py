from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QLineEdit, QPushButton, QWidget

from app.service.torque.jobs import TorqueJobResult
from app.service.torque.types import RuleSnapshot, TorqueConfig, TorqueConfigApplied
from ui.popups.torque_settings_dialog import TorqueSettingsDialog


class FakeJobs(QObject):
    finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.requests = []
        self.cancelled = []

    def submit(self, operation, *args):
        request_id = str(len(self.requests) + 1)
        self.requests.append((request_id, operation, args))
        return request_id

    def cancel(self, request_id):
        self.cancelled.append(request_id)

    def finish(self, value=None, error=None, index=-1):
        request_id, operation, _ = self.requests[index]
        self.finished.emit(TorqueJobResult(request_id, operation, value, error))


class FakeBus(QObject):
    plc_torque_config_saved = pyqtSignal(object)
    plc_torque_config_applied = pyqtSignal(object)


class FakeStore:
    def __init__(self):
        self.config = TorqueConfig(7)
        self.rules = RuleSnapshot(0, {})

    def get_config(self, plc_id):
        return self.config if plc_id == self.config.plc_id else TorqueConfig(plc_id)

    def snapshot(self):
        return self.rules


@pytest.fixture
def services(qapp):
    coordinator = SimpleNamespace(receipt=None)
    coordinator.get_applied_receipt = lambda plc_id: coordinator.receipt
    return SimpleNamespace(torque_jobs=FakeJobs(), torque_store=FakeStore(),
                           bus=FakeBus(), torque_config_coordinator=coordinator)


def test_defaults_and_disabled_empty_config_save_are_background_only(services):
    dialog = TorqueSettingsDialog(services, 7, lambda: 3)
    assert not dialog.enabled.isChecked()
    assert dialog.match_type.currentData() == 'model'
    assert len(dialog.findChildren(QComboBox)) == 1
    assert len(dialog.findChildren(QLineEdit)) == 4
    assert 'REAL' in dialog.explanation.text()
    assert '与 PLC 单位一致' in dialog.explanation.text()
    dialog.save_button.click()
    _, operation, (config, db) = services.torque_jobs.requests[-1]
    assert operation == 'save_config'
    assert config == TorqueConfig(7)
    assert db == 3
    assert '保存中' in dialog.saved_status.text()
    dialog.close()


@pytest.mark.parametrize('read,write,message', [('', '', '完整'), ('20', '22', '重叠'), ('20.1', '24', '位偏移')])
def test_invalid_enabled_addresses_never_submit(services, read, write, message):
    dialog = TorqueSettingsDialog(services, 7, lambda: 3)
    dialog.enabled.setChecked(True)
    dialog.read_offset.setText(read)
    dialog.write_offset.setText(write)
    dialog.save_button.click()
    assert not services.torque_jobs.requests
    assert message in dialog.error_label.text()
    dialog.close()


def test_config_fields_normalize_and_current_db_is_resolved_at_save(services):
    current_db = [1]
    dialog = TorqueSettingsDialog(services, 7, lambda: current_db[0])
    dialog.enabled.setChecked(True)
    dialog.match_type.setCurrentIndex(1)
    dialog.read_offset.setText('20.0')
    dialog.write_db.setText('1')
    dialog.write_offset.setText('22')
    current_db[0] = 2
    dialog.save_button.click()
    _, _, (config, db) = services.torque_jobs.requests[-1]
    assert config == TorqueConfig(7, True, 'sn', None, 20, 1, 22)
    assert db == 2
    services.torque_store.config = replace(config, revision=1)
    services.torque_jobs.finish(services.torque_store.config)
    assert '已保存' in dialog.saved_status.text()
    assert '待应用' in dialog.applied_status.text()
    receipt = TorqueConfigApplied(7, 'live', 1, True)
    services.torque_config_coordinator.receipt = receipt
    services.bus.plc_torque_config_applied.emit(receipt)
    assert '已应用' in dialog.applied_status.text()
    dialog.close()


def test_opening_uses_current_receipt_and_stale_save_cannot_regress_form(services):
    services.torque_store.config = TorqueConfig(7, revision=3)
    services.torque_config_coordinator.receipt = TorqueConfigApplied(7, 'live', 3, True)
    dialog = TorqueSettingsDialog(services, 7, lambda: 3)
    assert '已应用' in dialog.applied_status.text()
    dialog.save_button.click()
    services.torque_store.config = TorqueConfig(7, match_type='sn', revision=5)
    services.torque_config_coordinator.receipt = None
    services.torque_jobs.finish(TorqueConfig(7, revision=4))
    assert dialog.match_type.currentData() == 'sn'
    assert '5' in dialog.saved_status.text()
    assert '待应用' in dialog.applied_status.text()
    dialog.close()


def test_enabled_config_reloaded_at_revision_zero_is_not_called_disabled(services):
    services.torque_store.config = TorqueConfig(7, True, 'model', None, 20, None, 24)
    dialog = TorqueSettingsDialog(services, 7, lambda: 3)
    assert dialog.enabled.isChecked()
    assert '已启用' in dialog.saved_status.text()
    assert '默认关闭' not in dialog.saved_status.text()
    dialog.close()


def test_failed_apply_and_save_and_unrelated_receipts(services):
    dialog = TorqueSettingsDialog(services, 7, lambda: 3)
    dialog.save_button.click()
    services.torque_jobs.finish(error=ValueError('保存失败'))
    assert dialog.save_button.isEnabled()
    assert '保存失败' in dialog.error_label.text()
    receipt = TorqueConfigApplied(7, 'live', 0, False, '地址重叠')
    services.torque_config_coordinator.receipt = receipt
    services.bus.plc_torque_config_applied.emit(TorqueConfigApplied(8, 'other', 0, True))
    assert '待应用' in dialog.applied_status.text()
    services.bus.plc_torque_config_applied.emit(receipt)
    assert '地址重叠' in dialog.applied_status.text()
    dialog.close()


def test_manage_entry_and_close_leave_application_jobs_alive(services):
    dialog = TorqueSettingsDialog(services, 7, lambda: 3)
    dialog.show()
    dialog.manage_button.click()
    assert dialog.table_dialog.isVisible()
    dialog.save_button.click()
    dialog.close()
    assert not services.torque_jobs.cancelled
    services.torque_jobs.finish(TorqueConfig(7, revision=1))
    assert services.torque_jobs.submit('write_template', 'template.xlsx')


def test_hardware_torque_entry_uses_applied_db_and_permission_boundary(services, monkeypatch, qapp):
    from ui.modules.plc_settings import PlcSettingsModule
    from model.plc_table import PlcTable
    monkeypatch.setattr(PlcSettingsModule, 'reload_rows', lambda self: None)
    monkeypatch.setattr(PlcSettingsModule, 'start_process_param_service', lambda self: None)
    module = PlcSettingsModule(services)
    row = PlcTable.create(plc_db_number=8, plc_open_state=0)
    module.combo_plc.addItem('PLC', row.id)
    module.ed_db_number.setText('99')
    services.plc_services[row.id] = SimpleNamespace(_db_number=3)
    services.plc_state[row.id] = '已连接'
    module.btn_torque.click()
    assert module.torque_dialog.inherited_db() == 3
    assert services.plc_services[row.id]._db_number == 3
    receipt = TorqueConfigApplied(row.id, 'live', 0, True)
    services.torque_config_coordinator.receipt = receipt
    services.bus.plc_torque_config_applied.emit(receipt)
    assert '已应用' in module.torque_dialog.applied_status.text()
    services.plc_services.pop(row.id)
    services.torque_config_coordinator.receipt = None
    module.on_plc_state_changed(row.id, '未连接')
    assert '待应用' in module.torque_dialog.applied_status.text()
    assert module.torque_dialog.inherited_db() == 8
    module.resize(480, 650)
    module.show()
    qapp.processEvents()
    assert module.width() == 480
    assert module.btn_torque.isVisible()
    module.set_permission_enabled(False)
    assert not module.btn_torque.isEnabled()
    module.torque_dialog.close()
    module.close()
