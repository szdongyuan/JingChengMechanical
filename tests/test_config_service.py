"""Tests for ConfigService hardware field persistence and validation."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.config_service import ConfigService, _GOLDEN_BINDING_FIELDS
from app.service.golden_sample_service import GoldenSampleService
from model.channel_config_table import ChannelConfigTable
from model.plc_table import PlcTable
from model.test_config_ai_binding_table import (
    TestConfigAiBindingTable as AiBindingTable,
)
from model.test_config_table import TestConfigTable as ConfigTable


class FakeBus:
    config_changed = MagicMock()


def _service(golden_root: Path | None = None) -> ConfigService:
    FakeBus.config_changed.reset_mock()
    golden_service = None
    if golden_root is not None:
        golden_service = GoldenSampleService(root_dir=str(golden_root))
    return ConfigService(FakeBus(), golden_sample_service=golden_service)


def _channel_payload(channel_index: int) -> dict:
    return {
        "channel_index": channel_index,
        "stft_nfft": 1024,
        "stft_overlap": 0.5,
        "spl_weighting": "A",
        "spl_time_constant": 0.125,
        "spl_lower_enabled": False,
        "spl_upper_enabled": False,
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "spl_average_lower_enabled": False,
        "spl_average_lower_limit": 40.0,
        "spl_average_upper_enabled": False,
        "spl_average_upper_limit": 85.0,
        "loudness_lower_enabled": False,
        "loudness_upper_enabled": False,
        "loudness_average_lower_enabled": False,
        "loudness_average_lower_limit": 0.0,
        "loudness_average_upper_enabled": False,
        "loudness_average_upper_limit": 60.0,
        "fft_band_limits": [],
        "oct_weighting": "A",
        "oct_band_limits": [],
        "ai_config": {},
        "golden_sample_path": None,
        "golden_sample_label": None,
        "golden_sample_source_channel": None,
        "golden_sample_start_sample": None,
        "golden_sample_num_samples": None,
        "golden_sample_start_ms": None,
        "golden_sample_duration_ms": None,
        "golden_sample_sample_rate": None,
        "fft_golden_mode": "off",
        "fft_show_golden_overlay": False,
        "oct_golden_mode": "off",
        "oct_show_golden_overlay": False,
    }


def _channel_payload_with_loudness(
    channel_index: int,
    *,
    unit: str = "sone",
    lower: float = 0.0,
    upper: float = 60.0,
) -> dict:
    payload = _channel_payload(channel_index)
    payload.update(
        {
            "loudness_unit": unit,
            "loudness_lower_enabled": True,
            "loudness_upper_enabled": True,
            "loudness_lower_limit": lower,
            "loudness_upper_limit": upper,
        }
    )
    return payload


def _config_payload(
    config_id: int,
    *,
    config_name: str = "Cfg1",
    channels: list[dict] | None = None,
    selected_channels: list[int] | None = None,
    record_duration: float = 5.0,
    analysis_duration: float = 3.0,
    sample_rate: float = 48_000.0,
    golden_editor_state: dict[int, dict] | None = None,
) -> dict:
    channels = channels or []
    if selected_channels is None:
        selected_channels = [ch["channel_index"] for ch in channels]
    payload = {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": record_duration,
        "analysis_duration": analysis_duration,
        "plc_id": None,
        "scan_triggers_test": False,
        "input_device_name": "Mic 1",
        "input_device_hostapi": "ASIO",
        "input_device_samplerate": sample_rate,
        "output_device_name": "Speaker 1",
        "output_device_hostapi": "WASAPI",
        "output_device_samplerate": 44_100.0,
        "selected_channels": selected_channels,
        "channels": channels,
    }
    if golden_editor_state is not None:
        payload["golden_editor_state"] = golden_editor_state
    return payload


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    samples: int = 144_000,
    channels: int = 1,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = np.linspace(-0.9, 0.9, num=samples, dtype=np.float32)
    if channels <= 1:
        data = base
    else:
        data = np.column_stack([base + float(i) for i in range(channels)]).astype(
            np.float32
        )
    wavfile.write(path, sample_rate, data)
    return path


def _golden_binding(
    path: Path | str,
    *,
    label: str = "gold.wav",
    source_channel: int = 1,
    start_sample: int = 24_000,
    num_samples: int = 144_000,
    sample_rate: float = 48_000.0,
) -> dict:
    return {
        "golden_sample_path": str(path),
        "golden_sample_label": label,
        "golden_sample_source_channel": source_channel,
        "golden_sample_start_sample": start_sample,
        "golden_sample_num_samples": num_samples,
        "golden_sample_start_ms": round(start_sample * 1000 / sample_rate),
        "golden_sample_duration_ms": round(num_samples * 1000 / sample_rate),
        "golden_sample_sample_rate": sample_rate,
    }


def _staged_golden_binding(
    path: Path | str,
    *,
    label: str = "gold.wav",
    source_channel: int = 1,
    start_sample: int = 24_000,
    num_samples: int = 144_000,
    sample_rate: float = 48_000.0,
) -> dict:
    return {
        "staged_path": str(path),
        "label": label,
        "source_channel": source_channel,
        "start_sample": start_sample,
        "num_samples": num_samples,
        "start_ms": round(start_sample * 1000 / sample_rate),
        "duration_ms": round(num_samples * 1000 / sample_rate),
        "sample_rate": sample_rate,
    }


def _channel_payload_with_golden(channel_index: int, **updates) -> dict:
    payload = _channel_payload(channel_index)
    payload.update(updates)
    return payload


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
        "allow_incomplete_recording_analysis",
        "scan_triggers_test",
        "plc_id",
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
    assert result["plc_id"] is None
    assert result["allow_incomplete_recording_analysis"] is False
    assert result["selected_channels"] == [0, 2]


def test_load_config_returns_plc_id_and_not_legacy_plc_text_fields():
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")
    cfg = ConfigTable.create(
        config_name="Cfg1",
        plc=plc,
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
        "allow_incomplete_recording_analysis",
        "scan_triggers_test",
        "plc_id",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "channels",
    }
    assert result["plc_id"] == plc.id
    assert "plc_device_key" not in result
    assert "plc_device_label" not in result
    assert result["analysis_window_offset_ms"] == 250
    assert result["allow_incomplete_recording_analysis"] is False


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
        "allow_incomplete_recording_analysis",
        "scan_triggers_test",
        "plc_id",
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
    assert loaded["plc_id"] is None
    assert loaded["allow_incomplete_recording_analysis"] is False
    assert loaded["selected_channels"] == [0, 2]
    assert row.plc_id is None


def test_save_config_persists_plc_id_and_rejects_missing_plc_rows():
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 500,
            "scan_triggers_test": False,
            "plc_id": plc.id,
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
    assert loaded["plc_id"] == plc.id
    assert saved_row.plc_id == plc.id

    with pytest.raises(ValueError, match="PLC"):
        svc.save_config(
            {
                "config_id": config_id,
                "config_name": "Cfg1",
                "record_duration": 5.0,
                "analysis_duration": 3.0,
                "scan_triggers_test": False,
                "plc_id": plc.id + 999,
                "selected_channels": [0],
                "channels": [_channel_payload(0)],
            }
        )


def test_save_config_reports_late_db_level_plc_fk_failure_as_plc_error(monkeypatch):
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    payload = {
        "config_id": config_id,
        "config_name": "Cfg1",
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "scan_triggers_test": False,
        "plc_id": plc.id,
        "selected_channels": [0],
        "channels": [_channel_payload(0)],
    }

    assert svc.validate(payload) == []

    original_collect_replaced_paths = svc._collect_replaced_paths

    def _delete_plc_before_write(config_id_arg, channels):
        PlcTable.delete().where(PlcTable.id == plc.id).execute()
        return original_collect_replaced_paths(config_id_arg, channels)

    monkeypatch.setattr(svc, "_collect_replaced_paths", _delete_plc_before_write)

    with pytest.raises(ValueError, match="PLC 不存在或已被删除"):
        svc.save_config(payload)

    saved_row = ConfigTable.get_by_id(config_id)

    assert saved_row.plc_id is None
    FakeBus.config_changed.emit.assert_not_called()


def test_save_config_does_not_misreport_non_plc_fk_failure_as_plc_error(monkeypatch):
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    payload = {
        "config_id": config_id,
        "config_name": "Cfg1",
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "scan_triggers_test": False,
        "plc_id": plc.id,
        "selected_channels": [0],
        "channels": [_channel_payload(0)],
    }

    assert svc.validate(payload) == []

    original_collect_replaced_paths = svc._collect_replaced_paths

    def _delete_config_before_write(config_id_arg, channels):
        ConfigTable.delete().where(ConfigTable.config_id == config_id).execute()
        return original_collect_replaced_paths(config_id_arg, channels)

    monkeypatch.setattr(svc, "_collect_replaced_paths", _delete_config_before_write)

    with pytest.raises(ValueError) as exc_info:
        svc.save_config(payload)

    assert str(exc_info.value) == "配置不存在或已被删除"
    assert PlcTable.select().where(PlcTable.id == plc.id).exists()
    assert not ConfigTable.select().where(ConfigTable.config_id == config_id).exists()
    FakeBus.config_changed.emit.assert_not_called()


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


def test_save_and_load_config_round_trips_allow_incomplete_recording_analysis():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    payload = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["allow_incomplete_recording_analysis"] = True

    svc.save_config(payload)

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)

    assert loaded["allow_incomplete_recording_analysis"] is True
    assert row.allow_incomplete_recording_analysis is True


def test_reserve_import_config_id_returns_unused_local_id():
    svc = _service()
    ConfigTable.create(config_name="Existing")

    reserved_id = svc.reserve_import_config_id()

    assert isinstance(reserved_id, int)
    assert reserved_id > 0
    assert not ConfigTable.select().where(ConfigTable.config_id == reserved_id).exists()


def test_create_imported_config_uses_reserved_id_and_clears_plc_id():
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source.update(
        {
            "plc_id": plc.id,
            "created_time": "2000-01-01 00:00:00",
            "updated_time": "2000-01-01 00:00:00",
            "input_device_name": "Imported Mic",
            "input_device_hostapi": "Imported API",
            "input_device_samplerate": 96_000.0,
            "output_device_name": "Imported Speaker",
            "output_device_hostapi": "Imported Output API",
            "output_device_samplerate": 48_000.0,
        }
    )
    reserved_id = svc.reserve_import_config_id()

    new_id = svc.create_imported_config(source, reserved_config_id=reserved_id)
    loaded = svc.load_config(new_id)
    row = ConfigTable.get_by_id(new_id)

    assert new_id == reserved_id
    assert new_id != 999
    assert loaded["config_name"] == "Imported"
    assert loaded["plc_id"] is None
    assert row.plc_id is None
    assert row.created_time != "2000-01-01 00:00:00"
    assert row.updated_time != "2000-01-01 00:00:00"
    assert loaded["input_device_name"] == "Imported Mic"
    assert loaded["input_device_hostapi"] == "Imported API"
    assert loaded["input_device_samplerate"] == 96_000.0
    assert loaded["output_device_name"] == "Imported Speaker"
    assert loaded["output_device_hostapi"] == "Imported Output API"
    assert loaded["output_device_samplerate"] == 48_000.0


def test_overwrite_imported_config_keeps_local_id_and_clears_plc_id():
    svc = _service()
    local_plc = PlcTable.create(plc_name="Local PLC")
    source_plc = PlcTable.create(plc_name="Source PLC")
    local_id = ConfigTable.create(
        config_name="Imported",
        plc=local_plc,
        input_device_name="Local Mic",
    ).config_id
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source.update(
        {
            "plc_id": source_plc.id,
            "input_device_name": "Source Mic",
            "input_device_hostapi": "Source API",
            "input_device_samplerate": 44_100.0,
        }
    )

    saved_id = svc.overwrite_imported_config(local_id, source)
    loaded = svc.load_config(local_id)

    assert saved_id == local_id
    assert loaded["config_id"] == local_id
    assert loaded["plc_id"] is None
    assert loaded["input_device_name"] == "Source Mic"
    assert loaded["input_device_hostapi"] == "Source API"
    assert loaded["input_device_samplerate"] == 44_100.0


def test_imported_config_omitted_golden_assets_clear_bindings_and_modes(tmp_path):
    svc = _service(tmp_path / "managed")
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[
            _channel_payload_with_golden(
                0,
                **_golden_binding(tmp_path / "source-machine.wav"),
                fft_golden_mode="absolute",
                fft_show_golden_overlay=True,
                oct_golden_mode="delta",
            )
        ],
        selected_channels=[0],
    )
    reserved_id = svc.reserve_import_config_id()

    new_id = svc.create_imported_config(source, reserved_config_id=reserved_id)
    channel = svc.load_config(new_id)["channels"][0]

    for key in _GOLDEN_BINDING_FIELDS:
        assert channel[key] is None
    assert channel["fft_golden_mode"] == "off"
    assert channel["fft_show_golden_overlay"] is False
    assert channel["oct_golden_mode"] == "off"
    assert channel["oct_show_golden_overlay"] is False


def test_imported_config_omitted_golden_assets_clear_existing_local_managed_binding(
    tmp_path,
):
    svc = _service(tmp_path / "managed")
    existing_local = _write_temp_wav(tmp_path / "managed" / "config_99" / "gold.wav")
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[
            _channel_payload_with_golden(
                0,
                **_golden_binding(existing_local),
                fft_golden_mode="absolute",
                fft_show_golden_overlay=True,
                oct_golden_mode="delta",
                oct_show_golden_overlay=False,
            )
        ],
        selected_channels=[0],
    )
    reserved_id = svc.reserve_import_config_id()

    new_id = svc.create_imported_config(source, reserved_config_id=reserved_id)
    channel = svc.load_config(new_id)["channels"][0]

    for key in _GOLDEN_BINDING_FIELDS:
        assert channel[key] is None
    assert channel["fft_golden_mode"] == "off"
    assert channel["fft_show_golden_overlay"] is False
    assert channel["oct_golden_mode"] == "off"
    assert channel["oct_show_golden_overlay"] is False
    assert existing_local.exists()


def test_create_imported_config_failure_does_not_delete_payload_only_managed_file(
    tmp_path,
):
    svc = _service(tmp_path / "managed")
    existing_local = _write_temp_wav(tmp_path / "managed" / "config_99" / "gold.wav")
    ConfigTable.create(config_name="Imported")
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[
            _channel_payload_with_golden(
                0,
                **_golden_binding(existing_local),
                fft_golden_mode="absolute",
            )
        ],
        selected_channels=[0],
    )
    reserved_id = svc.reserve_import_config_id()

    with pytest.raises(ValueError, match="已存在"):
        svc.create_imported_config(source, reserved_config_id=reserved_id)

    assert existing_local.exists()
    assert list((tmp_path / "managed").rglob("*.wav")) == [existing_local]


def test_create_imported_config_validation_failure_rolls_back_explicit_restored_asset(
    tmp_path,
):
    svc = _service(tmp_path / "managed")
    restored_path = _write_temp_wav(tmp_path / "managed" / "config_42" / "restored.wav")
    source = _config_payload(
        999,
        config_name="   ",
        channels=[
            _channel_payload_with_golden(
                0,
                fft_golden_mode="absolute",
            )
        ],
        selected_channels=[0],
    )
    reserved_id = svc.reserve_import_config_id()

    with pytest.raises(ValueError, match="配置名称不能为空"):
        svc.create_imported_config(
            source,
            reserved_config_id=reserved_id,
            golden_asset_paths_by_channel={
                0: _golden_binding(restored_path),
            },
        )

    assert not restored_path.exists()
    assert list((tmp_path / "managed").rglob("*.wav")) == []


def test_overwrite_imported_config_rollback_preserves_old_golden_binding(
    tmp_path,
    monkeypatch,
):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Imported").config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(old_path)),
    )
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )

    def _fail_create(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ChannelConfigTable, "create", _fail_create)

    with pytest.raises(RuntimeError, match="db exploded"):
        svc.overwrite_imported_config(config_id, source)

    loaded = svc.load_config(config_id)

    assert loaded["channels"][0]["golden_sample_path"] == str(old_path)
    assert old_path.exists()
    assert list((tmp_path / "managed").rglob("*.wav")) == [old_path]


def test_legacy_fallback_infers_selected_channels():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1", selected_channels="[]")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))
    ChannelConfigTable.create(config=cfg, **_channel_payload(2))

    result = svc.load_config(cfg.config_id)

    assert result["selected_channels"] == [0, 2]


def test_load_config_returns_default_loudness_fields_for_channel_rows():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))

    result = svc.load_config(cfg.config_id)

    assert result["channels"][0]["loudness_unit"] == "sone"
    assert result["channels"][0]["loudness_lower_limit"] == 0.0
    assert result["channels"][0]["loudness_upper_limit"] == 60.0


def test_load_config_returns_default_fft_resolution_for_channel_rows():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))

    result = svc.load_config(cfg.config_id)

    assert result["channels"][0]["fft_nfft"] == 4096


def test_save_config_persists_loudness_fields():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [
                _channel_payload_with_loudness(
                    0, unit="phon", lower=12.5, upper=70.0
                )
            ],
        }
    )

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )

    assert loaded["channels"][0]["loudness_unit"] == "phon"
    assert loaded["channels"][0]["loudness_lower_limit"] == 12.5
    assert loaded["channels"][0]["loudness_upper_limit"] == 70.0
    assert row.loudness_unit == "phon"
    assert row.loudness_lower_limit == 12.5
    assert row.loudness_upper_limit == 70.0


def test_save_config_persists_fft_resolution():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_nfft"] = 2048

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )

    assert loaded["channels"][0]["fft_nfft"] == 2048
    assert getattr(row, "fft_nfft", None) == 2048


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


def test_validate_rejects_invalid_loudness_unit():
    svc = _service()

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [
                _channel_payload_with_loudness(
                    0, unit="db", lower=0.0, upper=60.0
                )
            ],
        }
    )

    assert any("响度单位" in err for err in errors)


def test_validate_rejects_invalid_fft_resolution():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_nfft"] = 123

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    assert any("FFT 分辨率" in err for err in errors)


def test_validate_rejects_negative_loudness_limits():
    svc = _service()

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [
                _channel_payload_with_loudness(
                    0, unit="sone", lower=-0.1, upper=60.0
                )
            ],
        }
    )

    assert any("响度下限" in err for err in errors)


def test_validate_rejects_loudness_lower_limit_not_less_than_upper():
    svc = _service()

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [
                _channel_payload_with_loudness(
                    0, unit="phon", lower=60.0, upper=60.0
                )
            ],
        }
    )

    assert any("响度下限必须小于上限" in err for err in errors)


def test_validate_ignores_disabled_invalid_limit_values():
    svc = _service()
    channel = _channel_payload(0)
    channel.update(
        {
            "spl_lower_enabled": False,
            "spl_upper_enabled": False,
            "spl_lower_limit": "not-number",
            "spl_upper_limit": "also-not-number",
            "loudness_lower_enabled": False,
            "loudness_upper_enabled": False,
            "loudness_lower_limit": -10.0,
            "loudness_upper_limit": -1.0,
        }
    )

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    assert errors == []


def test_validate_rejects_non_boolean_limit_enable_flags():
    svc = _service()
    channel = _channel_payload(0)
    channel["spl_lower_enabled"] = "yes"

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    assert any("SPL 下限启用必须为布尔值" in err for err in errors)


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


def test_validate_rejects_non_boolean_allow_incomplete_recording_analysis():
    svc = _service()

    errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "allow_incomplete_recording_analysis": "yes",
            "scan_triggers_test": False,
            "selected_channels": [],
            "channels": [],
        }
    )

    assert any("允许分析不完整录音" in err for err in errors)


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


def test_validate_allows_unbound_plc_id_and_existing_row_only():
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")

    unbound_errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "scan_triggers_test": False,
            "plc_id": None,
            "selected_channels": [],
            "channels": [],
        }
    )
    bound_errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "scan_triggers_test": False,
            "plc_id": plc.id,
            "selected_channels": [],
            "channels": [],
        }
    )
    missing_errors = svc.validate(
        {
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 0,
            "scan_triggers_test": False,
            "plc_id": plc.id + 999,
            "selected_channels": [],
            "channels": [],
        }
    )

    assert unbound_errors == []
    assert bound_errors == []
    assert any("PLC" in err for err in missing_errors)


def test_load_config_round_trips_golden_fields():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    ChannelConfigTable.create(
        config=cfg,
        **_channel_payload_with_golden(
            0,
            **_golden_binding("D:/managed/golden.wav"),
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            oct_golden_mode="delta",
            oct_show_golden_overlay=False,
        ),
    )

    loaded = svc.load_config(cfg.config_id)
    channel = loaded["channels"][0]

    assert channel["golden_sample_path"] == "D:/managed/golden.wav"
    assert channel["golden_sample_label"] == "gold.wav"
    assert channel["golden_sample_source_channel"] == 1
    assert channel["golden_sample_start_sample"] == 24_000
    assert channel["golden_sample_num_samples"] == 144_000
    assert channel["golden_sample_start_ms"] == 500
    assert channel["golden_sample_duration_ms"] == 3000
    assert channel["golden_sample_sample_rate"] == 48_000.0
    assert channel["fft_golden_mode"] == "absolute"
    assert channel["fft_show_golden_overlay"] is True
    assert channel["oct_golden_mode"] == "delta"
    assert channel["oct_show_golden_overlay"] is False


def test_validate_rejects_missing_staged_golden_asset_path(tmp_path):
    svc = _service(tmp_path / "managed")

    errors = svc.validate(
        _config_payload(
            1,
            channels=[
                _channel_payload_with_golden(
                    0,
                    fft_golden_mode="absolute",
                )
            ],
            selected_channels=[0],
            golden_editor_state={
                0: _staged_golden_binding(tmp_path / "missing.wav")
            },
        )
    )

    assert any("暂存" in err for err in errors)


def test_validate_rejects_committed_golden_path_outside_managed_root(tmp_path):
    svc = _service(tmp_path / "managed")
    outside = _write_temp_wav(tmp_path / "outside.wav")

    errors = svc.validate(
        _config_payload(
            1,
            channels=[
                _channel_payload_with_golden(
                    0,
                    **_golden_binding(outside),
                    fft_show_golden_overlay=True,
                )
            ],
            selected_channels=[0],
        )
    )

    assert any("受管目录" in err for err in errors)


def test_validate_rejects_committed_golden_path_under_managed_staged_root(tmp_path):
    svc = _service(tmp_path / "managed")
    staged = _write_temp_wav(tmp_path / "managed" / "staged" / "gold.wav")

    errors = svc.validate(
        _config_payload(
            1,
            channels=[
                _channel_payload_with_golden(
                    0,
                    **_golden_binding(staged),
                    fft_golden_mode="absolute",
                )
            ],
            selected_channels=[0],
        )
    )

    assert any("已提交目录" in err for err in errors)


def test_validate_rejects_committed_stereo_golden_asset(tmp_path):
    svc = _service(tmp_path / "managed")
    managed = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        channels=2,
    )

    errors = svc.validate(
        _config_payload(
            1,
            channels=[
                _channel_payload_with_golden(
                    0,
                    **_golden_binding(managed),
                    fft_golden_mode="absolute",
                )
            ],
            selected_channels=[0],
        )
    )

    assert any("单声道" in err for err in errors)


@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [
        (
            lambda channel: channel.update(golden_sample_start_sample=-1),
            "起始采样",
        ),
        (
            lambda channel: channel.update(golden_sample_source_channel=-1),
            "源通道",
        ),
        (
            lambda channel: channel.update(golden_sample_sample_rate=44_100.0),
            "采样率",
        ),
        (
            lambda channel: channel.update(golden_sample_num_samples=120_000),
            "长度",
        ),
        (
            lambda channel: channel.update(golden_sample_start_ms=499),
            "起始时间",
        ),
        (
            lambda channel: channel.update(golden_sample_duration_ms=2999),
            "时长元数据",
        ),
    ],
)
def test_validate_rejects_invalid_committed_golden_metadata(
    tmp_path,
    mutate,
    expected_fragment,
):
    svc = _service(tmp_path / "managed")
    managed = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
    channel = _channel_payload_with_golden(
        0,
        **_golden_binding(managed),
        fft_golden_mode="absolute",
    )
    mutate(channel)

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert any(expected_fragment in err for err in errors)


def test_validate_rejects_committed_golden_file_length_mismatch(tmp_path):
    svc = _service(tmp_path / "managed")
    managed = _write_temp_wav(
        tmp_path / "managed" / "config_1" / "gold.wav",
        samples=143_999,
    )

    errors = svc.validate(
        _config_payload(
            1,
            channels=[
                _channel_payload_with_golden(
                    0,
                    **_golden_binding(managed),
                    fft_golden_mode="absolute",
                )
            ],
            selected_channels=[0],
        )
    )

    assert any("文件长度" in err for err in errors)


@pytest.mark.parametrize(
    ("updates", "expected_fragment"),
    [
        (
            {"fft_golden_mode": "delta", "fft_show_golden_overlay": True},
            "FFT Delta",
        ),
        (
            {"oct_golden_mode": "delta", "oct_show_golden_overlay": True},
            "倍频程 Delta",
        ),
        (
            {"fft_golden_mode": "absolute"},
            "FFT 黄金模式",
        ),
        (
            {"oct_show_golden_overlay": True},
            "倍频程黄金曲线",
        ),
    ],
)
def test_validate_rejects_invalid_golden_control_combinations(
    tmp_path,
    updates,
    expected_fragment,
):
    svc = _service(tmp_path / "managed")
    channel = _channel_payload_with_golden(0, **updates)

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert any(expected_fragment in err for err in errors)


def test_validate_allows_empty_band_tables_when_golden_modes_are_enabled(tmp_path):
    svc = _service(tmp_path / "managed")
    managed = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")

    errors = svc.validate(
        _config_payload(
            1,
            channels=[
                _channel_payload_with_golden(
                    0,
                    **_golden_binding(managed),
                    fft_golden_mode="absolute",
                    oct_golden_mode="delta",
                )
            ],
            selected_channels=[0],
        )
    )

    assert errors == []


def test_save_config_promotes_staged_golden_asset_and_persists_committed_metadata(
    tmp_path,
):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    staged = _write_temp_wav(tmp_path / "staged.wav")

    saved_id = svc.save_config(
        _config_payload(
            config_id,
            channels=[_channel_payload(0)],
            selected_channels=[0],
            golden_editor_state={0: _staged_golden_binding(staged)},
        )
    )

    loaded = svc.load_config(saved_id)
    channel = loaded["channels"][0]

    assert saved_id == config_id
    assert channel["golden_sample_path"].endswith(".wav")
    assert channel["golden_sample_path"] != str(staged)
    assert Path(channel["golden_sample_path"]).exists()
    assert Path(channel["golden_sample_path"]).resolve().is_relative_to(
        (tmp_path / "managed").resolve()
    )
    assert channel["golden_sample_label"] == "gold.wav"
    assert channel["golden_sample_source_channel"] == 1
    assert channel["golden_sample_start_sample"] == 24_000
    assert channel["golden_sample_num_samples"] == 144_000
    assert channel["golden_sample_start_ms"] == 500
    assert channel["golden_sample_duration_ms"] == 3000
    assert channel["golden_sample_sample_rate"] == 48_000.0
    assert channel["fft_golden_mode"] == "off"


def test_save_config_success_keeps_external_staged_source_file(tmp_path):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    external_staged = _write_temp_wav(tmp_path / "external-staged.wav")

    saved_id = svc.save_config(
        _config_payload(
            config_id,
            channels=[_channel_payload(0)],
            selected_channels=[0],
            golden_editor_state={0: _staged_golden_binding(external_staged)},
        )
    )

    loaded = svc.load_config(saved_id)

    assert loaded["channels"][0]["golden_sample_path"] != str(external_staged)
    assert external_staged.exists()


def test_save_config_deletes_replaced_committed_golden_asset_after_success(tmp_path):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(old_path)),
    )
    staged = _write_temp_wav(tmp_path / "replacement.wav")

    svc.save_config(
        _config_payload(
            config_id,
            channels=[_channel_payload(0)],
            selected_channels=[0],
            golden_editor_state={0: _staged_golden_binding(staged)},
        )
    )

    loaded = svc.load_config(config_id)

    assert loaded["channels"][0]["golden_sample_path"] != str(old_path)
    assert not old_path.exists()


def test_save_config_rolls_back_promoted_golden_asset_when_db_write_fails(
    tmp_path,
    monkeypatch,
):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(old_path)),
    )
    staged = _write_temp_wav(tmp_path / "replacement.wav")

    def _fail_create(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ChannelConfigTable, "create", _fail_create)

    with pytest.raises(RuntimeError, match="db exploded"):
        svc.save_config(
            _config_payload(
                config_id,
                channels=[_channel_payload(0)],
                selected_channels=[0],
                golden_editor_state={0: _staged_golden_binding(staged)},
            )
        )

    loaded = svc.load_config(config_id)
    committed_paths = list((tmp_path / "managed").rglob("*.wav"))

    assert loaded["channels"][0]["golden_sample_path"] == str(old_path)
    assert old_path.exists()
    assert committed_paths == [old_path]


def test_save_config_allocates_unique_committed_paths_per_binding(tmp_path):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    config2_id = ConfigTable.create(config_name="Cfg2").config_id
    shared = _write_temp_wav(tmp_path / "shared.wav")
    shared_again = _write_temp_wav(tmp_path / "shared-again.wav")

    svc.save_config(
        _config_payload(
            config_id,
            channels=[_channel_payload(0), _channel_payload(1)],
            selected_channels=[0, 1],
            golden_editor_state={
                0: _staged_golden_binding(shared, source_channel=0),
                1: _staged_golden_binding(shared, source_channel=1),
            },
        )
    )
    svc.save_config(
        _config_payload(
            config2_id,
            config_name="Cfg2",
            channels=[_channel_payload(0)],
            selected_channels=[0],
            golden_editor_state={
                0: _staged_golden_binding(shared_again, source_channel=0)
            },
        )
    )

    loaded1 = svc.load_config(config_id)
    loaded2 = svc.load_config(config2_id)
    paths = {
        loaded1["channels"][0]["golden_sample_path"],
        loaded1["channels"][1]["golden_sample_path"],
        loaded2["channels"][0]["golden_sample_path"],
    }

    assert len(paths) == 3
    assert all(Path(path).exists() for path in paths)


def test_save_config_rejects_unmatched_staged_entries_before_any_promotion(tmp_path):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    staged_match = _write_temp_wav(tmp_path / "staged-match.wav")
    staged_stray = _write_temp_wav(tmp_path / "staged-stray.wav")

    with pytest.raises(ValueError, match="暂存黄金样本.*缺少对应通道"):
        svc.save_config(
            _config_payload(
                config_id,
                channels=[_channel_payload(0)],
                selected_channels=[0],
                golden_editor_state={
                    0: _staged_golden_binding(staged_match),
                    9: _staged_golden_binding(staged_stray),
                },
            )
        )

    loaded = svc.load_config(config_id)

    assert loaded["channels"] == []
    assert list((tmp_path / "managed").rglob("*.wav")) == []


def test_save_config_replacement_leaves_external_persisted_path_untouched(tmp_path):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    external_old_path = _write_temp_wav(tmp_path / "external-old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(external_old_path)),
    )
    staged = _write_temp_wav(tmp_path / "replacement.wav")

    saved_id = svc.save_config(
        _config_payload(
            config_id,
            channels=[_channel_payload(0)],
            selected_channels=[0],
            golden_editor_state={0: _staged_golden_binding(staged)},
        )
    )

    loaded = svc.load_config(saved_id)

    assert loaded["channels"][0]["golden_sample_path"] != str(external_old_path)
    assert external_old_path.exists()


def test_save_config_logs_warning_when_post_commit_cleanup_fails(
    tmp_path,
    monkeypatch,
    caplog,
):
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    svc = ConfigService(FakeBus(), golden_sample_service=golden_service)
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(old_path)),
    )
    staged = _write_temp_wav(tmp_path / "replacement.wav")
    original_cleanup = golden_service.cleanup_paths

    def _cleanup_with_failure(paths):
        normalized = {str(path) for path in paths}
        if str(old_path) in normalized:
            raise OSError("cleanup failed")
        return original_cleanup(paths)

    monkeypatch.setattr(golden_service, "cleanup_paths", _cleanup_with_failure)

    with caplog.at_level(logging.WARNING):
        saved_id = svc.save_config(
            _config_payload(
                config_id,
                channels=[_channel_payload(0)],
                selected_channels=[0],
                golden_editor_state={0: _staged_golden_binding(staged)},
            )
        )

    loaded = svc.load_config(saved_id)

    assert saved_id == config_id
    assert loaded["channels"][0]["golden_sample_path"] != str(old_path)
    assert old_path.exists()
    assert any("cleanup failed" in message for message in caplog.messages)


def test_delete_config_deletes_managed_committed_golden_assets(tmp_path):
    svc = _service(tmp_path / "managed")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    managed_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(managed_path)),
    )

    deleted = svc.delete_config(config_id)

    assert deleted is True
    assert not ConfigTable.select().where(ConfigTable.config_id == config_id).exists()
    assert (
        ChannelConfigTable.select()
        .where(ChannelConfigTable.config == config_id)
        .count()
        == 0
    )
    assert not managed_path.exists()


def test_delete_config_logs_warning_when_managed_cleanup_fails(
    tmp_path,
    monkeypatch,
    caplog,
):
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    svc = ConfigService(FakeBus(), golden_sample_service=golden_service)
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    managed_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload_with_golden(0, **_golden_binding(managed_path)),
    )

    def _cleanup_with_failure(paths):
        raise OSError(f"cleanup failed for {paths}")

    monkeypatch.setattr(golden_service, "cleanup_paths", _cleanup_with_failure)

    with caplog.at_level(logging.WARNING):
        deleted = svc.delete_config(config_id)

    assert deleted is True
    assert not ConfigTable.select().where(ConfigTable.config_id == config_id).exists()
    assert (
        ChannelConfigTable.select()
        .where(ChannelConfigTable.config == config_id)
        .count()
        == 0
    )
    assert managed_path.exists()
    assert any("cleanup failed" in message for message in caplog.messages)
