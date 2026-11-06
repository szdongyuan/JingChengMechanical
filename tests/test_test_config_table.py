"""Tests for TestConfigTable model defaults."""
from __future__ import annotations

from peewee import BooleanField, ForeignKeyField
import pytest

from model.plc_table import PlcTable
from model.test_config_table import TestConfigTable as ConfigTable


def test_test_config_table_exposes_nullable_plc_id_field():
    assert set(ConfigTable._meta.fields) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "allow_incomplete_recording_analysis",
        "scan_triggers_test",
        "plc",
        "created_time",
        "updated_time",
        "input_hardware_device_id",
        "current_meter_host",
        "current_meter_source",
        "current_meter_port",
        "current_meter_unit_id",
        "current_meter_register_type",
        "current_meter_register_address",
        "current_meter_register_count",
        "current_meter_data_type",
        "current_meter_byte_order",
        "current_meter_word_order",
        "current_meter_scale",
        "current_meter_offset",
        "current_meter_enabled",
        "current_meter_poll_interval_ms",
        "current_meter_timeout_ms",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "fault_types_json",
        "manual_judgment_buttons_visible",
        "vibration_view_channel_index",
        "result_dashboard_layout",
        "plc_device_key",
        "plc_device_label",
        "analysis_window_offset_ms",
        "monitor_channel_index",
        "monitor_gain_db",
    }
    assert "plc_id" in ConfigTable._meta.columns

    plc_field = ConfigTable._meta.columns["plc_id"]
    assert isinstance(plc_field, ForeignKeyField)
    assert plc_field.rel_model is PlcTable
    assert plc_field.null is True
    assert plc_field.column_name == "plc_id"
    assert plc_field.on_delete == "SET NULL"

    config = ConfigTable(config_name="Trigger Cleanup Defaults")

    assert config.plc_id is None
    assert config.input_device_name is None
    assert config.input_hardware_device_id is None
    assert config.selected_channels == "[]"
    assert config.fault_types_json == "[]"
    assert config.manual_judgment_buttons_visible is False
    assert config.vibration_view_channel_index is None
    assert config.result_dashboard_layout is None
    assert config.plc_device_key is None
    assert config.plc_device_label is None
    assert config.analysis_window_offset_ms == 0
    assert config.monitor_channel_index is None
    assert config.monitor_gain_db is None

    fault_types_column = next(
        row
        for row in ConfigTable._meta.database.execute_sql(
            "PRAGMA table_info(test_config_table)"
        ).fetchall()
        if row[1] == "fault_types_json"
    )
    assert fault_types_column[2].upper() == "TEXT"
    assert bool(fault_types_column[3]) is True
    assert fault_types_column[4] == "'[]'"

    visibility_field = ConfigTable.manual_judgment_buttons_visible
    assert isinstance(visibility_field, BooleanField)
    assert visibility_field.default is False

    visibility_column = next(
        row
        for row in ConfigTable._meta.database.execute_sql(
            "PRAGMA table_info(test_config_table)"
        ).fetchall()
        if row[1] == "manual_judgment_buttons_visible"
    )
    assert (
        visibility_column[2].upper(),
        bool(visibility_column[3]),
        visibility_column[4],
        int(visibility_column[5]),
    ) == ("INTEGER", True, "0", 0)


def test_model_defines_allow_incomplete_recording_analysis_with_false_default():
    assert "allow_incomplete_recording_analysis" in ConfigTable._meta.fields

    config = ConfigTable(config_name="Incomplete Stop Defaults")

    assert config.allow_incomplete_recording_analysis is False


def test_model_defines_default_lingli_current_meter_fields():
    config = ConfigTable(config_name="LingLi Current Meter Defaults")

    assert config.current_meter_source == "modbus_tcp"
    assert config.current_meter_register_type == "input"
    assert config.current_meter_register_address == 0
    assert config.current_meter_register_count == 1
    assert config.current_meter_data_type == "uint16"
    assert config.current_meter_byte_order == "big"
    assert config.current_meter_word_order == "big"
    assert config.current_meter_scale == pytest.approx(0.001)
    assert config.current_meter_offset == pytest.approx(0.0)


def test_db_backed_plc_fk_clears_config_binding_on_plc_delete():
    plc = PlcTable.create(plc_name="Shared Fixture PLC")
    config = ConfigTable.create(config_name="Persisted PLC Binding", plc=plc)

    persisted = ConfigTable.get_by_id(config.config_id)
    assert persisted.plc_id == plc.id

    plc.delete_instance()

    reloaded = ConfigTable.get_by_id(config.config_id)
    assert reloaded.plc_id is None
