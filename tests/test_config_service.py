"""Tests for ConfigService hardware field persistence and validation."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.service.config_service import ConfigService
from model.channel_config_table import ChannelConfigTable
from model.test_config_ai_binding_table import (
    TestConfigAiBindingTable as AiBindingTable,
)
from model.test_config_table import TestConfigTable as ConfigTable


class FakeBus:
    config_changed = MagicMock()


def _service() -> ConfigService:
    FakeBus.config_changed.reset_mock()
    return ConfigService(FakeBus())


def _channel_payload(channel_index: int) -> dict:
    return {
        "channel_index": channel_index,
        "stft_nfft": 1024,
        "stft_overlap": 0.5,
        "spl_weighting": "A",
        "spl_time_constant": 0.125,
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "fft_band_limits": [],
        "oct_weighting": "A",
        "oct_band_limits": [],
        "ai_config": {},
    }


def test_create_config_no_channel_rows():
    svc = _service()

    config_id = svc.create_config("Cfg1")

    assert ChannelConfigTable.select().where(ChannelConfigTable.config == config_id).count() == 0


def test_load_config_returns_hardware_fields_with_cleaned_trigger_model():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44100,
        selected_channels='[0, 2]',
    )

    result = svc.load_config(cfg.config_id)

    assert set(result) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "analysis_window_offset_ms",
        "scan_triggers_test",
        "plc_device_key",
        "plc_device_label",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "channels",
    }
    assert result["input_device_name"] == "Mic 1"
    assert result["input_device_hostapi"] == "ASIO"
    assert result["input_device_samplerate"] == 48000
    assert result["output_device_name"] == "Speaker 1"
    assert result["output_device_hostapi"] == "WASAPI"
    assert result["output_device_samplerate"] == 44100
    assert result["selected_channels"] == [0, 2]


def test_load_config_returns_plc_binding_and_analysis_offset_fields():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        plc_device_key="  plc-a  ",
        plc_device_label="  PLC A  ",
        analysis_window_offset_ms=250,
    )

    result = svc.load_config(cfg.config_id)

    assert set(result) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "analysis_window_offset_ms",
        "scan_triggers_test",
        "plc_device_key",
        "plc_device_label",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "channels",
    }
    assert result["plc_device_key"] == "plc-a"
    assert result["plc_device_label"] == "PLC A"
    assert result["analysis_window_offset_ms"] == 250


def test_save_config_persists_hardware_fields():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 44100,
            "selected_channels": [0, 2],
            "channels": [_channel_payload(0), _channel_payload(2)],
        }
    )

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)

    assert set(loaded) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "analysis_window_offset_ms",
        "scan_triggers_test",
        "plc_device_key",
        "plc_device_label",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "channels",
    }
    assert loaded["input_device_name"] == "Mic 1"
    assert loaded["input_device_hostapi"] == "ASIO"
    assert loaded["input_device_samplerate"] == 48000
    assert loaded["output_device_name"] == "Speaker 1"
    assert loaded["output_device_hostapi"] == "WASAPI"
    assert loaded["output_device_samplerate"] == 44100
    assert loaded["selected_channels"] == [0, 2]
    assert row.plc_device_key is None
    assert row.plc_device_label is None


def test_save_config_blank_plc_metadata_persists_none():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "plc_device_key": "   ",
            "plc_device_label": "   ",
            "selected_channels": [],
            "channels": [],
        }
    )

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)

    assert loaded["plc_device_key"] is None
    assert loaded["plc_device_label"] is None
    assert row.plc_device_key is None
    assert row.plc_device_label is None


def test_load_config_normalizes_direct_row_plc_metadata():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        plc_device_key="  PLC-A  ",
        plc_device_label="   ",
    )

    loaded = svc.load_config(cfg.config_id)

    assert loaded["plc_device_key"] == "PLC-A"
    assert loaded["plc_device_label"] is None


def test_save_config_persists_analysis_window_offset_ms_and_plc_fields():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 500,
            "scan_triggers_test": False,
            "plc_device_key": "  plc-a  ",
            "plc_device_label": "  PLC A  ",
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 44100,
            "selected_channels": [0],
            "channels": [_channel_payload(0)],
        }
    )

    loaded = svc.load_config(config_id)
    saved_row = ConfigTable.get_by_id(config_id)

    assert loaded["analysis_window_offset_ms"] == 500
    assert loaded["plc_device_key"] == "plc-a"
    assert loaded["plc_device_label"] == "PLC A"
    assert saved_row.plc_device_key == "plc-a"
    assert saved_row.plc_device_label == "PLC A"


def test_save_config_blank_plc_metadata_persists_none():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 500,
            "scan_triggers_test": False,
            "plc_device_key": "   ",
            "plc_device_label": "\t",
            "selected_channels": [0],
            "channels": [_channel_payload(0)],
        }
    )

    loaded = svc.load_config(config_id)
    saved_row = ConfigTable.get_by_id(config_id)

    assert loaded["plc_device_key"] is None
    assert loaded["plc_device_label"] is None
    assert saved_row.plc_device_key is None
    assert saved_row.plc_device_label is None


def test_save_config_keeps_existing_ai_binding_row_while_offset_stays_config_owned():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    binding = AiBindingTable.create(
        config=config_id,
        enabled=True,
        timeout_ms=1200,
        params_json='{"legacy": true}',
    )

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 500,
            "scan_triggers_test": False,
            "plc_device_key": "plc-a",
            "plc_device_label": "PLC A",
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 44100,
            "selected_channels": [0],
            "channels": [_channel_payload(0)],
        }
    )

    updated = AiBindingTable.get_by_id(binding.binding_id)
    loaded = svc.load_config(config_id)

    assert loaded["analysis_window_offset_ms"] == 500
    assert updated.binding_id == binding.binding_id
    assert updated.enabled is True
    assert updated.timeout_ms == 1200
    assert updated.params_json == '{"legacy": true}'
    assert not hasattr(updated, "analysis_window_offset_ms")


def test_legacy_fallback_infers_selected_channels():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1", selected_channels="[]")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))
    ChannelConfigTable.create(config=cfg, **_channel_payload(2))

    result = svc.load_config(cfg.config_id)

    assert result["selected_channels"] == [0, 2]


def test_validate_device_without_channels_error():
    svc = _service()

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "input_device_name": "Mic 1",
            "selected_channels": [],
            "channels": [],
        }
    )

    assert "已选择输入设备但未选择任何通道" in errors


def test_validate_rejects_negative_analysis_window_offset():
    svc = _service()

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": -1,
            "scan_triggers_test": False,
            "selected_channels": [],
            "channels": [],
        }
    )

    assert any("偏移" in err for err in errors)


def test_validate_factory_runtime_rejects_analysis_window_offset_plus_duration_beyond_record_duration():
    svc = _service()

    errors = svc.validate_factory_runtime(
        {
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 2500,
        }
    )

    assert any("分析" in err for err in errors)


def test_validate_factory_runtime_allows_exact_fit_analysis_window_boundary():
    svc = _service()

    errors = svc.validate_factory_runtime(
        {
            "record_duration": 0.3,
            "analysis_duration": 0.1,
            "analysis_window_offset_ms": 200,
        }
    )

    assert errors == []


def test_validate_factory_runtime_does_not_require_plc_device_key_when_unbound():
    svc = _service()

    errors = svc.validate_factory_runtime(
        {
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "plc_device_key": "",
        }
    )

    assert errors == []
