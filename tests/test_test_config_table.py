"""Tests for TestConfigTable model defaults."""
from __future__ import annotations

from model.test_config_table import TestConfigTable as ConfigTable


def test_model_defines_hardware_trigger_fields_with_defaults():
    for field_name in (
        "trigger_type",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
    ):
        assert field_name in ConfigTable._meta.fields

    config = ConfigTable.create(config_name="Hardware Defaults")

    assert config.trigger_type == "manual"
    assert config.input_device_name is None
    assert config.selected_channels == "[]"
