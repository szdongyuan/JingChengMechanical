from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from model.plc_field_offset_table import PlcFieldOffsetTable
from model.plc_process_param_offset_table import PlcProcessParamOffsetTable
from model.plc_table import PlcTable
from model.test_config_table import TestConfigTable as ConfigTable
from ui.modules import plc_settings


class _SignalStub:
    def __init__(self):
        self.callbacks = []
        self.emissions = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        self.emissions.append(args)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def plc_tables(setup_test_db):
    tables = [PlcFieldOffsetTable, PlcProcessParamOffsetTable]
    setup_test_db.bind(tables)
    setup_test_db.create_tables(tables)
    yield
    setup_test_db.drop_tables(tables)


@pytest.fixture
def services():
    bus = SimpleNamespace(
        log=_SignalStub(),
        write_test_result_sign=_SignalStub(),
        write_limit_sign=_SignalStub(),
        plc_changed=_SignalStub(),
        plc_limit_values_dict={},
    )
    return SimpleNamespace(bus=bus, _start_from_plc=lambda *_args: None)


def _make_module(monkeypatch, services):
    monkeypatch.setattr(
        plc_settings.PlcSettingsModule,
        "start_process_param_service",
        lambda self: None,
    )
    return plc_settings.PlcSettingsModule(services)


def test_plc_current_offset_is_optional_for_modbus_configs(qapp):
    values = {
        field_name: "0.0"
        for field_name, _label in plc_settings.PLC_FIELD_OFFSETS
    }
    values["current_value_offset"] = None

    dialog = plc_settings.PlcFieldOffsetDialog(1, values)

    assert dialog.validate_values() is True


def test_empty_plc_table_can_create_blank_configuration(
    qapp,
    plc_tables,
    services,
    monkeypatch,
):
    module = _make_module(monkeypatch, services)

    assert PlcTable.select().count() == 0
    assert module.btn_add_plc.isEnabled()
    assert module.btn_process_param_offset.isEnabled()
    assert not module.btn_delete_plc.isEnabled()
    assert not module.btn_save.isEnabled()

    module.add_plc()

    row = PlcTable.get()
    assert row.plc_open_state == 0
    assert PlcFieldOffsetTable.get_by_id(row.id).plc_id == row.id
    assert module.combo_plc.currentData() == row.id
    assert module.ed_ip.isEnabled()
    assert module.btn_save.isEnabled()
    assert module.btn_field_offset.isEnabled()
    assert module.btn_delete_plc.isEnabled()
    assert services.bus.plc_changed.emissions == [()]


def test_delete_plc_cleans_related_data_and_notifies_other_modules(
    qapp,
    plc_tables,
    services,
    monkeypatch,
):
    row = PlcTable.create(plc_name="Line A", plc_open_state=0)
    PlcFieldOffsetTable.create(plc_id=row.id)
    config = ConfigTable.create(config_name="Uses PLC", plc=row)
    services.bus.plc_limit_values_dict[row.id] = {"product_model": "A"}
    module = _make_module(monkeypatch, services)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    module.delete_current_plc()

    assert not PlcTable.select().where(PlcTable.id == row.id).exists()
    assert not PlcFieldOffsetTable.select().where(
        PlcFieldOffsetTable.plc_id == row.id
    ).exists()
    assert ConfigTable.get_by_id(config.config_id).plc_id is None
    assert row.id not in services.bus.plc_limit_values_dict
    assert row.id not in services.plc_state
    assert module.combo_plc.count() == 0
    assert not module.btn_delete_plc.isEnabled()
    assert services.bus.plc_changed.emissions == [()]
    assert services.bus.log.emissions[-1] == (f"PLC {row.id} 已删除",)


def test_delete_plc_cancellation_keeps_current_configuration(
    qapp,
    plc_tables,
    services,
    monkeypatch,
):
    row = PlcTable.create(plc_name="Keep me", plc_open_state=0)
    PlcFieldOffsetTable.create(plc_id=row.id)
    module = _make_module(monkeypatch, services)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )

    module.delete_current_plc()

    assert PlcTable.get_by_id(row.id).plc_name == "Keep me"
    assert PlcFieldOffsetTable.get_by_id(row.id).plc_id == row.id
    assert module.combo_plc.currentData() == row.id
    assert services.bus.plc_changed.emissions == []


def test_process_configuration_opens_when_process_table_is_empty(
    qapp,
    plc_tables,
    services,
    monkeypatch,
):
    module = _make_module(monkeypatch, services)

    class _AcceptedProcessDialog:
        def __init__(self, *_args, **_kwargs):
            self.btn_process_connect = SimpleNamespace(clicked=_SignalStub())

        def set_process_state(self, _state):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def get_values(self):
            return {}

    monkeypatch.setattr(
        plc_settings,
        "PlcProcessParamOffsetDialog",
        _AcceptedProcessDialog,
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    module.open_process_param_offset_dialog()

    assert PlcProcessParamOffsetTable.select().count() == 1
