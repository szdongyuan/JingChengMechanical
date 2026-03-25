"""Tests for TestConfigTable model defaults."""
from __future__ import annotations

from model.test_config_table import TestConfigTable as ConfigTable


def test_model_defines_trigger_cleanup_fields_with_defaults():
    assert set(ConfigTable._meta.fields) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "scan_triggers_test",
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

    config = ConfigTable.create(config_name="Trigger Cleanup Defaults")

    assert config.input_device_name is None
    assert config.selected_channels == "[]"
    assert config.plc_device_key is None
    assert config.plc_device_label is None
    assert config.analysis_window_offset_ms == 0
