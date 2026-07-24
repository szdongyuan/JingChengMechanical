from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from app.service.plc_protocol import (
    PLC_FIELD_SPECS,
    PLC_NEW_RECORD_DEFAULT_OFFSETS,
)
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
        write_plc_testing_state_sign=_SignalStub(),
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


@pytest.mark.parametrize(
    "optional_key",
    [spec.key for spec in PLC_FIELD_SPECS if not spec.required],
)
def test_every_non_start_plc_offset_can_be_blank(
    qapp,
    monkeypatch,
    optional_key,
):
    values = {spec.key: "0.0" for spec in PLC_FIELD_SPECS}
    values[optional_key] = None
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: None)

    dialog = plc_settings.PlcFieldOffsetDialog(1, values)

    assert dialog.validate_values() is True


def test_blank_start_offset_fails_validation(qapp, monkeypatch):
    values = {spec.key: "0.0" for spec in PLC_FIELD_SPECS}
    values["start_signal_offset"] = None
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: None)

    dialog = plc_settings.PlcFieldOffsetDialog(1, values)

    assert dialog.validate_values() is False
    assert dialog.focusWidget() is dialog.edits["start_signal_offset"]


def test_bool_bit_eight_fails_validation(qapp, monkeypatch):
    values = {spec.key: "0.0" for spec in PLC_FIELD_SPECS}
    values["res_testing_signal_offset"] = "8.8"
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: None)

    dialog = plc_settings.PlcFieldOffsetDialog(1, values)

    assert dialog.validate_values() is False
    assert dialog.focusWidget() is dialog.edits["res_testing_signal_offset"]


@pytest.mark.parametrize(
    "field_key",
    [
        "rotation_speed_offset",
        "current_value_offset",
        "sn_code_offset",
    ],
)
def test_byte_aligned_fields_reject_nonzero_bit(
    qapp,
    monkeypatch,
    field_key,
):
    values = {spec.key: "0.0" for spec in PLC_FIELD_SPECS}
    values[field_key] = "10.1"
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: None)

    dialog = plc_settings.PlcFieldOffsetDialog(1, values)

    assert dialog.validate_values() is False
    assert dialog.focusWidget() is dialog.edits[field_key]


def test_new_protocol_fields_are_present_and_blank_values_return_none(qapp):
    values = {spec.key: "0.0" for spec in PLC_FIELD_SPECS}
    values["rotation_speed_offset"] = None
    values["res_testing_signal_offset"] = None

    dialog = plc_settings.PlcFieldOffsetDialog(1, values)

    assert "rotation_speed_offset" in dialog.edits
    assert "res_testing_signal_offset" in dialog.edits
    assert dialog.validate_values() is True
    assert dialog.get_values()["rotation_speed_offset"] is None
    assert dialog.get_values()["res_testing_signal_offset"] is None


def test_empty_plc_table_creates_approved_default_offsets(
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
    offset_row = PlcFieldOffsetTable.get_by_id(row.id)
    actual_offsets = {
        spec.key: getattr(offset_row, spec.key)
        for spec in PLC_FIELD_SPECS
    }
    assert actual_offsets == {
        "start_signal_offset": "0.0",
        "current_value_offset": "2.0",
        "rotation_speed_offset": "6.0",
        "res_testing_signal_offset": "8.0",
        "res_finish_signal_offset": "8.1",
        "res_error_code_offset": "10.0",
        "res_result_offset": "12.0",
        "res_score_offset": "14.0",
        "res_spl_offset": "18.0",
        "res_loudness_offset": "22.0",
        "res_vibrate_data_offset": "26.0",
        "res_ai_score_offset": "30.0",
        "res_spl_limit_up_offset": "34.0",
        "res_spl_limit_down_offset": "38.0",
        "res_loudness_limit_up_offset": "42.0",
        "res_loudness_limit_down_offset": "46.0",
        "res_vibrate_limit_up_offset": "50.0",
        "res_vibrate_limit_down_offset": "54.0",
        "res_ai_score_limit_down_offset": "58.0",
        "sn_code_offset": "62.0",
        "product_model_offset": "80.0",
        "res_sn_code_offset": None,
        "res_product_model_offset": None,
    }
    assert actual_offsets == PLC_NEW_RECORD_DEFAULT_OFFSETS
    assert set(PLC_NEW_RECORD_DEFAULT_OFFSETS) == {
        spec.key for spec in PLC_FIELD_SPECS
    }
    assert module.combo_plc.currentData() == row.id
    assert module.ed_ip.isEnabled()
    assert module.btn_save.isEnabled()
    assert module.btn_field_offset.isEnabled()
    assert module.btn_delete_plc.isEnabled()
    assert services.bus.plc_changed.emissions == [()]


def test_add_plc_preserves_existing_row_offsets(
    qapp,
    plc_tables,
    services,
    monkeypatch,
):
    existing = PlcTable.create(plc_name="Existing", plc_open_state=0)
    PlcFieldOffsetTable.create(plc_id=existing.id)
    module = _make_module(monkeypatch, services)

    module.add_plc()

    existing_offsets = PlcFieldOffsetTable.get_by_id(existing.id)
    assert all(
        getattr(existing_offsets, spec.key) is None
        for spec in PLC_FIELD_SPECS
    )
    new_plc = (
        PlcTable.select()
        .where(PlcTable.id != existing.id)
        .get()
    )
    new_offsets = PlcFieldOffsetTable.get_by_id(new_plc.id)
    assert {
        spec.key: getattr(new_offsets, spec.key)
        for spec in PLC_FIELD_SPECS
    } == PLC_NEW_RECORD_DEFAULT_OFFSETS


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


def test_start_connect_wires_testing_state_only_to_ordinary_plc_service(
    qapp,
    plc_tables,
    services,
    monkeypatch,
):
    row = PlcTable.create(plc_name="Line A", plc_open_state=0)
    PlcFieldOffsetTable.create(plc_id=row.id, start_signal_offset="0.0")
    module = _make_module(monkeypatch, services)

    class _ServiceStub:
        def __init__(self, **_kwargs):
            self.connect_result = _SignalStub()
            self.state_changed = _SignalStub()

        def moveToThread(self, _thread):
            pass

        def start_plc_service(self):
            pass

        def deleteLater(self):
            pass

        def last_write_test_result_to_plc(self, *_args):
            pass

        def last_write_limit_to_plc(self, *_args):
            pass

        def write_testing_state(self, *_args):
            pass

    class _ThreadStub:
        def __init__(self, _parent):
            self.started = _SignalStub()
            self.finished = _SignalStub()

        def start(self):
            pass

    monkeypatch.setattr(plc_settings, "PlcService", _ServiceStub)
    monkeypatch.setattr(plc_settings, "QThread", _ThreadStub)

    module.start_connect(row)

    plc_service = services.plc_services[row.id]
    assert services.bus.write_plc_testing_state_sign.callbacks == [
        plc_service.write_testing_state
    ]
    assert services.process_plc_service is None
