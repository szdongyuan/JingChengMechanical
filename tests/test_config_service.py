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


def test_load_config_returns_hardware_fields():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        trigger_type="hardware",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44100,
        selected_channels='[0, 2]',
    )

    result = svc.load_config(cfg.config_id)

    assert result["trigger_type"] == "hardware"
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
        trigger_type="plc",
        plc_device_key="plc-a",
        plc_device_label="PLC A",
        analysis_window_offset_ms=250,
    )

    result = svc.load_config(cfg.config_id)

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
            "trigger_type": "hardware",
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

    assert loaded["trigger_type"] == "hardware"
    assert loaded["input_device_name"] == "Mic 1"
    assert loaded["input_device_hostapi"] == "ASIO"
    assert loaded["input_device_samplerate"] == 48000
    assert loaded["output_device_name"] == "Speaker 1"
    assert loaded["output_device_hostapi"] == "WASAPI"
    assert loaded["output_device_samplerate"] == 44100
    assert loaded["selected_channels"] == [0, 2]


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
            "trigger_type": "plc",
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

    loaded = svc.load_config(config_id)

    assert loaded["analysis_window_offset_ms"] == 500
    assert loaded["plc_device_key"] == "plc-a"
    assert loaded["plc_device_label"] == "PLC A"


def test_save_config_keeps_existing_ai_binding_analysis_window_offset_in_sync():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    binding = AiBindingTable.create(
        config=config_id,
        enabled=False,
        analysis_window_offset_ms=100,
    )

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 500,
            "scan_triggers_test": False,
            "trigger_type": "plc",
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

    assert updated.binding_id == binding.binding_id
    assert updated.analysis_window_offset_ms == 500


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
            "trigger_type": "manual",
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
            "trigger_type": "manual",
        }
    )

    assert errors == []


def test_validate_factory_runtime_requires_plc_device_key_for_plc_trigger():
    svc = _service()

    errors = svc.validate_factory_runtime(
        {
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "trigger_type": "plc",
            "plc_device_key": "",
        }
    )

    assert any("PLC" in err for err in errors)
