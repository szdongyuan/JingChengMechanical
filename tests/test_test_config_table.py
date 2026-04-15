"""Tests for TestConfigTable model defaults."""
from __future__ import annotations

from peewee import ForeignKeyField

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
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "plc_device_key",
        "plc_device_label",
        "analysis_window_offset_ms",
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
    assert config.selected_channels == "[]"
    assert config.plc_device_key is None
    assert config.plc_device_label is None
    assert config.analysis_window_offset_ms == 0


def test_model_defines_allow_incomplete_recording_analysis_with_false_default():
    assert "allow_incomplete_recording_analysis" in ConfigTable._meta.fields

    config = ConfigTable(config_name="Incomplete Stop Defaults")

    assert config.allow_incomplete_recording_analysis is False
