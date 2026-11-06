"""Tests for ConfigService hardware field persistence and validation."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.config_service import (
    MONITOR_GAIN_DEFAULT_DB,
    ConfigService,
    _GOLDEN_BINDING_FIELDS,
)
from app.service.golden_sample_service import GoldenSampleService
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from model.input_hardware_table import InputHardwareTable
from model.plc_table import PlcTable
from model.test_config_ai_binding_table import (
    TestConfigAiBindingTable as AiBindingTable,
)
from model.test_config_table import TestConfigTable as ConfigTable


class FakeBus:
    config_changed = MagicMock()


_INVALID_MANAGED_INPUT_ERROR = (
    "\u5fc5\u987b\u9009\u62e9\u6709\u6548\u7684\u8f93\u5165\u786c\u4ef6\u8bbe\u5907"
)
_UNAVAILABLE_MANAGED_INPUT_ERROR = (
    "\u6240\u9009\u8f93\u5165\u786c\u4ef6\u8bbe\u5907\u4e0d\u53ef\u7528"
)
_MONITOR_CHANNEL_RANGE_ERROR = (
    "\u76d1\u542c\u901a\u9053\u5fc5\u987b\u5c0f\u4e8e\u8f93\u5165\u901a\u9053\u6570"
)
_VIBRATION_CHANNEL_RANGE_ERROR = (
    "\u632f\u52a8\u89c6\u56fe\u901a\u9053\u5fc5\u987b\u5c0f\u4e8e\u8f93\u5165\u901a\u9053\u6570"
)
_MISSING = object()


@pytest.fixture(autouse=True)
def valid_managed_input():
    return InputHardwareTable.create(
        hardware_id=1,
        alias="Managed input",
        device_name="Managed microphone",
        hostapi="ASIO",
        samplerate=48_000,
        channel_count=4,
    )


def _service(golden_root: Path | None = None) -> ConfigService:
    FakeBus.config_changed.reset_mock()
    golden_service = None
    if golden_root is not None:
        golden_service = GoldenSampleService(root_dir=str(golden_root))
    return ConfigService(FakeBus(), golden_sample_service=golden_service)


def _recreate_legacy_config_schema_without_monitor_columns() -> None:
    database = ConfigTable._meta.database
    database.execute_sql("DROP TABLE IF EXISTS channel_config_table")
    database.execute_sql("DROP TABLE IF EXISTS test_config_ai_binding_table")
    database.execute_sql("DROP TABLE IF EXISTS test_config_table")
    database.execute_sql(
        """
        CREATE TABLE test_config_table (
            config_id INTEGER NOT NULL PRIMARY KEY,
            config_name TEXT NOT NULL UNIQUE,
            record_duration REAL NOT NULL DEFAULT 5.0,
            analysis_duration REAL NOT NULL DEFAULT 3.0,
            allow_incomplete_recording_analysis INTEGER NOT NULL DEFAULT 0,
            scan_triggers_test INTEGER NOT NULL DEFAULT 0,
            plc_id INTEGER,
            plc_device_key TEXT,
            plc_device_label TEXT,
            created_time TEXT NOT NULL DEFAULT 'legacy-created',
            updated_time TEXT NOT NULL DEFAULT 'legacy-updated',
            input_device_name TEXT,
            input_device_hostapi TEXT,
            input_device_samplerate REAL,
            output_device_name TEXT,
            output_device_hostapi TEXT,
            output_device_samplerate REAL,
            selected_channels TEXT NOT NULL DEFAULT '[]',
            fault_types_json TEXT NOT NULL DEFAULT '[]',
            analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (plc_id) REFERENCES plc_table (id) ON DELETE SET NULL
        )
        """
    )
    ChannelConfigTable.create_table(safe=True)


def _legacy_config_columns() -> set[str]:
    cursor = ConfigTable._meta.database.execute_sql(
        "PRAGMA table_info(test_config_table)"
    )
    return {row[1] for row in cursor.fetchall()}


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
        "fft_x_axis_scale": "linear",
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


_STFT_SCORE_DEFAULTS = {
    "stft_score_enabled": True,
    "stft_score_summary_mode": "percentile",
    "stft_score_percentile": 10.0,
    "stft_score_loudness_reference_sone": 10.0,
    "stft_score_modulation_reference": 0.20,
    "stft_score_harmonic_ratio_reference": 0.25,
    "stft_score_frequency_instability_reference": 0.05,
    "stft_score_harmonic_start_order": 2,
    "stft_score_harmonic_end_order": 100,
}

_STFT_SCORE_CUSTOM = {
    "stft_score_enabled": False,
    "stft_score_summary_mode": "mean",
    "stft_score_percentile": 25.0,
    "stft_score_loudness_reference_sone": 12.5,
    "stft_score_modulation_reference": 0.35,
    "stft_score_harmonic_ratio_reference": 0.45,
    "stft_score_frequency_instability_reference": 0.08,
    "stft_score_harmonic_start_order": 3,
    "stft_score_harmonic_end_order": 80,
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
    monitor_channel_index: int | None = None,
    monitor_gain_db: float | None = 0.0,
    vibration_view_channel_index: int | None = None,
    fault_types: list[str] | None = None,
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
        "input_hardware_device_id": 1,
        "fault_types": list(fault_types or []),
        "selected_channels": selected_channels,
        "monitor_channel_index": monitor_channel_index,
        "monitor_gain_db": monitor_gain_db,
        "vibration_view_channel_index": vibration_view_channel_index,
        "channels": channels,
    }
    if golden_editor_state is not None:
        payload["golden_editor_state"] = golden_editor_state
    return payload


def _chart_layout(*, columns: int = 1) -> dict:
    return {
        "version": 2,
        "columns": columns,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "primary_acoustic",
            }
        ],
    }


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


def test_fault_types_column_has_exact_orm_definition() -> None:
    row = next(
        row
        for row in ConfigTable._meta.database.execute_sql(
            "PRAGMA table_info(test_config_table)"
        ).fetchall()
        if row[1] == "fault_types_json"
    )

    assert row[2].upper() == "TEXT"
    assert bool(row[3]) is True
    assert row[4] == "'[]'"


def test_create_config_explicitly_stores_empty_fault_types() -> None:
    svc = _service()

    config_id = svc.create_config("Fault defaults")

    stored = ConfigTable.get_by_id(config_id)
    assert stored.fault_types_json == "[]"
    assert svc.load_config(config_id)["fault_types"] == []


def test_create_config_defaults_manual_judgment_buttons_visible_to_false() -> None:
    svc = _service()

    config_id = svc.create_config("Manual judgment default")

    stored = ConfigTable.get_by_id(config_id)
    loaded = svc.load_config(config_id)
    assert stored.manual_judgment_buttons_visible is False
    assert loaded["manual_judgment_buttons_visible"] is False
    assert type(loaded["manual_judgment_buttons_visible"]) is bool


def test_manual_judgment_buttons_visible_save_load_and_copy_round_trip() -> None:
    svc = _service()
    source_id = svc.create_config("Source")
    source_payload = _config_payload(
        source_id,
        config_name="Source",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source_payload["manual_judgment_buttons_visible"] = True

    svc.save_config(source_payload)

    source_loaded = svc.load_config(source_id)
    raw_value = ConfigTable._meta.database.execute_sql(
        "SELECT manual_judgment_buttons_visible FROM test_config_table WHERE config_id = ?",
        (source_id,),
    ).fetchone()[0]
    assert source_loaded["manual_judgment_buttons_visible"] is True
    assert raw_value == 1

    copied_payload = dict(source_loaded)
    copied_payload["config_name"] = "Copied"
    copied_id = svc.create_imported_config(
        copied_payload,
        reserved_config_id=svc.reserve_import_config_id(),
    )

    assert svc.load_config(copied_id)["manual_judgment_buttons_visible"] is True


def test_save_legacy_input_missing_manual_judgment_buttons_visible_defaults_false() -> None:
    svc = _service()
    config_id = svc.create_config("Legacy input")
    ConfigTable.update(manual_judgment_buttons_visible=True).where(
        ConfigTable.config_id == config_id
    ).execute()
    legacy_payload = _config_payload(
        config_id,
        config_name="Legacy input",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )

    svc.save_config(legacy_payload)

    assert svc.load_config(config_id)["manual_judgment_buttons_visible"] is False


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_save_rejects_non_bool_manual_judgment_buttons_visible(value) -> None:
    svc = _service()
    config_id = svc.create_config("Invalid manual judgment visibility")
    payload = _config_payload(
        config_id,
        config_name="Invalid manual judgment visibility",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["manual_judgment_buttons_visible"] = value

    with pytest.raises(
        ValueError,
        match="manual_judgment_buttons_visible must be a boolean",
    ):
        svc.save_config(payload)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_prepare_import_rejects_non_bool_manual_judgment_buttons_visible(value) -> None:
    svc = _service()
    payload = _config_payload(999, config_name="Imported")
    payload["manual_judgment_buttons_visible"] = value

    with pytest.raises(
        ValueError,
        match="manual_judgment_buttons_visible must be a boolean",
    ):
        svc.prepare_import_payload(payload)


def test_fault_types_save_load_dashboard_and_fresh_list_round_trip() -> None:
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    payload = _config_payload(
        cfg.config_id,
        fault_types=[" 异响 ", "漏装"],
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )

    svc.save_config(payload)

    assert ConfigTable.get_by_id(cfg.config_id).fault_types_json == '["异响","漏装"]'
    first = svc.load_config(cfg.config_id)
    dashboard = svc.load_config_for_result_dashboard(cfg.config_id)
    assert first["fault_types"] == ["异响", "漏装"]
    assert dashboard["fault_types"] == ["异响", "漏装"]
    assert first["fault_types"] is not dashboard["fault_types"]
    first["fault_types"].append("mutated")
    assert svc.load_config(cfg.config_id)["fault_types"] == ["异响", "漏装"]


@pytest.mark.parametrize(
    "stored",
    [
        '["异响", "漏装"]',
        '[" 异响 "]',
        '{"fault_types":[]}',
        "not-json",
    ],
)
def test_load_config_rejects_malformed_or_noncanonical_fault_types(stored: str) -> None:
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    ConfigTable.update(fault_types_json=stored).where(
        ConfigTable.config_id == cfg.config_id
    ).execute()

    with pytest.raises(ValueError):
        svc.load_config(cfg.config_id)


def test_save_rejects_missing_fault_types_before_opening_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    payload = _config_payload(cfg.config_id)
    payload.pop("fault_types")

    def _unexpected_atomic():
        raise AssertionError("invalid semantic payload opened a transaction")

    monkeypatch.setattr(ConfigTable._meta.database, "atomic", _unexpected_atomic)

    with pytest.raises(ValueError):
        svc.save_config(payload)


def test_load_config_defaults_missing_result_dashboard_layout():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1", selected_channels="[0]")

    loaded = svc.load_config(cfg.config_id)

    assert loaded["result_dashboard_layout"]["columns"] == 3
    assert loaded["result_dashboard_layout"]["panels"][0]["type"] == "stft"


def test_save_config_persists_result_dashboard_layout():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1", selected_channels="[0]")
    payload = _config_payload(
        cfg.config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["result_dashboard_layout"] = _chart_layout()

    svc.save_config(payload)

    loaded = svc.load_config(cfg.config_id)
    assert loaded["result_dashboard_layout"]["columns"] == 1


def test_prepare_import_payload_preserves_result_dashboard_layout():
    svc = _service()
    source = _config_payload(
        999,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source["result_dashboard_layout"] = _chart_layout()

    assert svc.prepare_import_payload(source)["result_dashboard_layout"]["columns"] == 1


def test_create_config_writes_default_result_dashboard_layout():
    svc = _service()

    config_id = svc.create_config("NewCfg")
    loaded = svc.load_config(config_id)

    assert loaded["result_dashboard_layout"]["columns"] == 3
    assert loaded["result_dashboard_layout"]["panels"][0]["type"] == "stft"


def test_load_config_preserves_malformed_layout_so_editor_can_repair_it():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        selected_channels="[0]",
        result_dashboard_layout="{bad json",
    )

    loaded = svc.load_config(cfg.config_id)

    assert loaded["result_dashboard_layout"] == "{bad json"
    assert "结果展示布局" in loaded["_result_dashboard_layout_error"]


def test_load_config_for_result_dashboard_falls_back_for_malformed_layout():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        selected_channels="[0]",
        result_dashboard_layout="{bad json",
    )

    loaded = svc.load_config_for_result_dashboard(cfg.config_id)

    assert loaded["result_dashboard_layout"]["columns"] == 3
    assert loaded["result_dashboard_layout"]["panels"][0]["id"] == "stft_acoustic"


def test_load_config_for_result_dashboard_falls_back_when_layout_binding_exceeds_selected_channels():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        selected_channels="[0]",
        result_dashboard_layout=json.dumps(
            {
                "version": 2,
                "columns": 1,
                "rows": 1,
                "panels": [
                    {
                        "id": "spectrum",
                        "type": "spectrum",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "selected_2",
                    },
                ],
            }
        ),
    )

    loaded = svc.load_config_for_result_dashboard(cfg.config_id)

    assert loaded["result_dashboard_layout"]["columns"] == 3
    assert loaded["result_dashboard_layout"]["panels"][0]["id"] == "stft_acoustic"


def test_prepare_import_payload_rejects_malformed_result_dashboard_layout():
    svc = _service()
    source = _config_payload(
        999,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source["result_dashboard_layout"] = "{bad json"

    with pytest.raises(ValueError, match="结果展示布局|JSON|invalid"):
        svc.prepare_import_payload(source)


def test_prepare_import_payload_rejects_unknown_layout_panel_type():
    svc = _service()
    source = _config_payload(
        999,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source["result_dashboard_layout"] = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "custom",
                "type": "custom_widget",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "primary_acoustic",
            },
        ],
    }

    with pytest.raises(ValueError, match="结果展示布局|面板|panel|type"):
        svc.prepare_import_payload(source)


def test_prepare_import_payload_rejects_invalid_layout_channel_key():
    svc = _service()
    source = _config_payload(
        999,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    source["result_dashboard_layout"] = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "bad_channel",
            },
        ],
    }

    with pytest.raises(ValueError, match="结果展示布局|通道|channel"):
        svc.prepare_import_payload(source)


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
        "fault_types",
        "manual_judgment_buttons_visible",
        "monitor_channel_index",
        "monitor_gain_db",
        "vibration_view_channel_index",
        "result_dashboard_layout",
        "current_meter",
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
    assert result["monitor_channel_index"] == 0
    assert result["monitor_gain_db"] == 0.0


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
        "fault_types",
        "manual_judgment_buttons_visible",
        "monitor_channel_index",
        "monitor_gain_db",
        "vibration_view_channel_index",
        "result_dashboard_layout",
        "current_meter",
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
            "fault_types": [],
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 44100,
            "input_hardware_device_id": 1,
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
            "input_hardware_device_id",
            "selected_channels",
            "fault_types",
            "manual_judgment_buttons_visible",
        "monitor_channel_index",
        "monitor_gain_db",
        "vibration_view_channel_index",
        "result_dashboard_layout",
        "current_meter",
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
    assert loaded["monitor_channel_index"] == 0
    assert loaded["monitor_gain_db"] == 0.0
    assert row.plc_id is None


def test_load_config_defaults_null_monitor_fields_to_selected_channel_and_gain_zero():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        selected_channels="[2, 0]",
        monitor_channel_index=None,
        monitor_gain_db=None,
    )

    result = svc.load_config(cfg.config_id)

    assert result["monitor_channel_index"] == 2
    assert result["monitor_gain_db"] == 0.0


def test_load_config_does_not_default_null_monitor_from_legacy_input_channels():
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="4")
    cfg = ConfigTable.create(
        config_name="Cfg1",
        selected_channels="[]",
        monitor_channel_index=None,
        monitor_gain_db=None,
    )

    result = svc.load_config(cfg.config_id)

    assert result["monitor_channel_index"] is None
    assert result["monitor_gain_db"] == 0.0


def test_load_config_defaults_null_monitor_to_zero_for_bound_managed_input():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=1,
        selected_channels="[]",
        monitor_channel_index=None,
    )

    result = svc.load_config(cfg.config_id)

    assert result["monitor_channel_index"] == 0


def test_load_config_preserves_valid_stored_monitor_without_managed_binding():
    svc = _service()
    cfg = ConfigTable.create(
        config_name="Cfg1",
        selected_channels="[]",
        monitor_channel_index=3,
    )

    result = svc.load_config(cfg.config_id)

    assert result["monitor_channel_index"] == 3


@pytest.mark.parametrize(
    ("hardware_id", "channel_count"),
    [
        (999, 4),
        (1, None),
        (1, True),
        (1, 4.0),
        (1, "4"),
        (1, 0),
        (1, -1),
    ],
)
def test_load_config_does_not_default_monitor_for_unavailable_managed_input(
    monkeypatch,
    hardware_id,
    channel_count,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="4")
    cfg = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=hardware_id,
        selected_channels="[]",
        monitor_channel_index=None,
    )
    if hardware_id == 1:
        monkeypatch.setattr(
            InputHardwareTable,
            "get_by_id",
            lambda _hardware_id: SimpleNamespace(channel_count=channel_count),
        )

    result = svc.load_config(cfg.config_id)

    assert result["monitor_channel_index"] is None


def test_load_config_does_not_default_monitor_when_managed_lookup_raises(
    monkeypatch,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="4")
    cfg = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=1,
        selected_channels="[]",
        monitor_channel_index=None,
    )

    def raise_lookup_error(_hardware_id):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(InputHardwareTable, "get_by_id", raise_lookup_error)

    result = svc.load_config(cfg.config_id)

    assert result["monitor_channel_index"] is None


def test_save_config_persists_monitor_fields_independent_of_selected_channels():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        _config_payload(
            config_id,
            channels=[_channel_payload(0)],
            selected_channels=[0],
            monitor_channel_index=2,
            monitor_gain_db=-12.5,
        )
    )

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)

    assert loaded["monitor_channel_index"] == 2
    assert loaded["monitor_gain_db"] == -12.5
    assert row.monitor_channel_index == 2
    assert row.monitor_gain_db == -12.5


def test_save_config_persists_vibration_view_channel():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "fault_types": [],
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 44100,
            "input_hardware_device_id": 1,
            "selected_channels": [0, 2],
            "vibration_view_channel_index": 2,
            "channels": [_channel_payload(0), _channel_payload(2)],
        }
    )

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)

    assert loaded["vibration_view_channel_index"] == 2
    assert row.vibration_view_channel_index == 2


def test_save_config_persists_current_meter_config():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    payload = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["current_meter"] = {
        "enabled": True,
        "source": "modbus_tcp",
        "host": "192.168.1.20",
        "port": 1502,
        "unit_id": 3,
        "register_type": "input",
        "register_address": 120,
        "register_count": 2,
        "data_type": "float32",
        "byte_order": "little",
        "word_order": "big",
        "scale": 0.1,
        "offset": 1.5,
        "poll_interval_ms": 250,
        "timeout_ms": 1200,
    }

    svc.save_config(payload)

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)
    assert loaded["current_meter"] == payload["current_meter"]
    assert row.current_meter_enabled is True
    assert row.current_meter_source == "modbus_tcp"
    assert row.current_meter_host == "192.168.1.20"
    assert row.current_meter_register_type == "input"
    assert row.current_meter_scale == pytest.approx(0.1)


def test_plc_current_source_requires_valid_plc_and_does_not_require_modbus_host():
    svc = _service()
    plc = PlcTable.create(plc_name="PLC A")
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    payload = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["plc_id"] = plc.id
    payload["current_meter"] = {
        "enabled": True,
        "source": "plc",
        "host": "",
    }

    assert svc.validate(payload) == []
    svc.save_config(payload)

    loaded = svc.load_config(config_id)
    row = ConfigTable.get_by_id(config_id)
    assert loaded["current_meter"]["enabled"] is True
    assert loaded["current_meter"]["source"] == "plc"
    assert loaded["current_meter"]["host"] == ""
    assert row.current_meter_source == "plc"

    payload["plc_id"] = None
    assert any("PLC" in error for error in svc.validate(payload))


def test_current_meter_source_defaults_to_modbus_tcp_for_legacy_payloads():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    payload = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    payload["current_meter"] = {"enabled": False}

    svc.save_config(payload)

    assert svc.load_config(config_id)["current_meter"]["source"] == "modbus_tcp"
    assert ConfigTable.get_by_id(config_id).current_meter_source == "modbus_tcp"


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
            "fault_types": [],
            "input_hardware_device_id": 1,
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
                "input_hardware_device_id": 1,
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
        "fault_types": [],
        "input_hardware_device_id": 1,
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
        "fault_types": [],
        "input_hardware_device_id": 1,
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
            "fault_types": [],
            "input_hardware_device_id": 1,
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


def test_create_imported_config_normalizes_legacy_and_grouped_limit_tables():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"freq": 1000.0, "lower": 40.0, "upper": 100.0},
        {"freq": 2000.0, "lower": 50.0, "upper": 90.0},
    ]
    channel["oct_band_limits"] = [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 20.0},
                {"freq": 250.0, "limit": 25.0},
            ],
        }
    ]
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[channel],
        selected_channels=[0],
    )
    reserved_id = svc.reserve_import_config_id()

    new_id = svc.create_imported_config(source, reserved_config_id=reserved_id)
    loaded_channel = svc.load_config(new_id)["channels"][0]

    assert loaded_channel["fft_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 40.0},
                {"freq": 2000.0, "limit": 50.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        },
    ]
    assert loaded_channel["oct_band_limits"] == channel["oct_band_limits"]


def test_create_imported_config_rejects_malformed_grouped_limits_with_ui_message():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {
            "kind": "ceiling",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        }
    ]
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[channel],
        selected_channels=[0],
    )
    reserved_id = svc.reserve_import_config_id()

    with pytest.raises(ValueError) as excinfo:
        svc.create_imported_config(source, reserved_config_id=reserved_id)

    message = str(excinfo.value)
    assert "通道 1: FFT限制表第 1 项类型必须是上限或下限" in message
    assert "upper 或 lower" not in message


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


def test_prepare_import_payload_preserves_monitor_fields_and_defaults_absent_gain():
    svc = _service()
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
        monitor_channel_index=3,
        monitor_gain_db=6.5,
    )
    legacy_source = dict(source)
    legacy_source.pop("monitor_channel_index")
    legacy_source.pop("monitor_gain_db")

    prepared = svc.prepare_import_payload(source)
    legacy_prepared = svc.prepare_import_payload(legacy_source)

    assert prepared["monitor_channel_index"] == 3
    assert prepared["monitor_gain_db"] == 6.5
    assert legacy_prepared["monitor_channel_index"] is None
    assert legacy_prepared["monitor_gain_db"] == 0.0


def test_prepare_import_payload_preserves_vibration_view_channel():
    svc = _service()
    source = _config_payload(
        1,
        channels=[_channel_payload(0), _channel_payload(2)],
        selected_channels=[0, 2],
    )
    source["vibration_view_channel_index"] = 2

    prepared = svc.prepare_import_payload(source)

    assert prepared["vibration_view_channel_index"] == 2


def test_prepare_import_payload_defaults_missing_fft_x_axis_scale_to_linear():
    svc = _service()
    channel = _channel_payload(0)
    channel.pop("fft_x_axis_scale", None)
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[channel],
        selected_channels=[0],
    )

    prepared = svc.prepare_import_payload(source)

    assert prepared["channels"][0]["fft_x_axis_scale"] == "linear"


def test_prepare_import_payload_normalizes_legacy_limits_and_preserves_grouped_limits():
    svc = _service()
    legacy_channel = _channel_payload(0)
    legacy_channel["fft_band_limits"] = [
        {"freq": 1000.0, "lower": 40.0, "upper": 100.0},
        {"freq": 2000.0, "lower": 50.0, "upper": 90.0},
    ]
    legacy_channel["oct_band_limits"] = [
        {"freq": 125.0, "lower": 15.0, "upper": 60.0},
        {"freq": 250.0, "lower": 20.0, "upper": 55.0},
    ]
    grouped_channel = _channel_payload(1)
    grouped_channel["fft_band_limits"] = [
        {
            "kind": "upper",
            "points": [
                {"freq": 3000.0, "limit": 80.0},
                {"freq": 4000.0, "limit": 75.0},
            ],
        }
    ]
    grouped_channel["oct_band_limits"] = [
        {
            "kind": "lower",
            "points": [
                {"freq": 500.0, "limit": 30.0},
                {"freq": 1000.0, "limit": 35.0},
            ],
        }
    ]
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[legacy_channel, grouped_channel],
        selected_channels=[0, 1],
    )

    prepared = svc.prepare_import_payload(source)

    assert prepared["channels"][0]["fft_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 40.0},
                {"freq": 2000.0, "limit": 50.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        },
    ]
    assert prepared["channels"][0]["oct_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 15.0},
                {"freq": 250.0, "limit": 20.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 125.0, "limit": 60.0},
                {"freq": 250.0, "limit": 55.0},
            ],
        },
    ]
    assert prepared["channels"][1]["fft_band_limits"] == grouped_channel[
        "fft_band_limits"
    ]
    assert prepared["channels"][1]["oct_band_limits"] == grouped_channel[
        "oct_band_limits"
    ]


def test_prepare_import_payload_malformed_grouped_limits_validate_with_ui_message():
    svc = _service()
    channel = _channel_payload(0)
    channel["oct_band_limits"] = [{"kind": "upper", "points": "bad"}]
    source = _config_payload(
        999,
        config_name="Imported",
        channels=[channel],
        selected_channels=[0],
    )

    prepared = svc.prepare_import_payload(source)
    errors = svc.validate(prepared)

    assert "通道 1: 倍频程限制表第 1 项频点列表必须是列表" in errors
    assert not any("points" in error for error in errors)


def test_prepare_import_payload_defaults_missing_stft_color_scale_fields():
    service = _service()
    payload = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    channel = payload["channels"][0]
    channel.pop("stft_color_scale_enabled", None)
    channel.pop("stft_color_scale_min_db", None)
    channel.pop("stft_color_scale_max_db", None)

    prepared = service.prepare_import_payload(payload)

    channel = prepared["channels"][0]
    assert channel["stft_color_scale_enabled"] is False
    assert channel["stft_color_scale_min_db"] == -120.0
    assert channel["stft_color_scale_max_db"] == 0.0


def test_prepare_import_payload_defaults_missing_stft_score_fields():
    service = _service()
    payload = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    for field in _STFT_SCORE_DEFAULTS:
        payload["channels"][0].pop(field, None)

    prepared = service.prepare_import_payload(payload)

    channel = prepared["channels"][0]
    for field, expected in _STFT_SCORE_DEFAULTS.items():
        assert channel[field] == expected


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


def test_legacy_schema_list_and_load_defaults_monitor_fields_without_columns():
    svc = _service()
    _recreate_legacy_config_schema_without_monitor_columns()
    ConfigTable._meta.database.execute_sql(
        """
        INSERT INTO test_config_table (
            config_id,
            config_name,
            record_duration,
            analysis_duration,
            scan_triggers_test,
            selected_channels,
            input_device_name
        )
        VALUES (1, 'Legacy', 5.0, 3.0, 0, '[2, 0]', 'Legacy Mic')
        """
    )

    assert "monitor_channel_index" not in _legacy_config_columns()
    assert "monitor_gain_db" not in _legacy_config_columns()
    assert svc.list_configs() == [{"config_id": 1, "config_name": "Legacy"}]

    result = svc.load_config(1)

    assert result["config_name"] == "Legacy"
    assert result["input_device_name"] == "Legacy Mic"
    assert result["selected_channels"] == [2, 0]
    assert result["monitor_channel_index"] == 2
    assert result["monitor_gain_db"] == 0.0
    assert result["current_meter"]["source"] == "modbus_tcp"


def test_legacy_schema_save_and_import_omit_monitor_writes_and_preserve_fields():
    svc = _service()
    _recreate_legacy_config_schema_without_monitor_columns()
    ConfigTable._meta.database.execute_sql(
        """
        INSERT INTO test_config_table (
            config_id,
            config_name,
            record_duration,
            analysis_duration,
            scan_triggers_test,
            selected_channels,
            input_device_name
        )
        VALUES (1, 'Legacy Save', 5.0, 3.0, 0, '[0]', 'Before Mic')
        """
    )

    save_payload = _config_payload(
        1,
        config_name="Legacy Save",
        channels=[_channel_payload(1)],
        selected_channels=[1],
        monitor_channel_index=0,
        monitor_gain_db=12.0,
    )
    save_payload["input_device_name"] = "After Save Mic"
    save_payload["analysis_window_offset_ms"] = 250
    svc.save_config(save_payload)
    saved = svc.load_config(1)

    assert "monitor_channel_index" not in _legacy_config_columns()
    assert "monitor_gain_db" not in _legacy_config_columns()
    assert saved["input_device_name"] == "After Save Mic"
    assert saved["analysis_window_offset_ms"] == 250
    assert saved["selected_channels"] == [1]
    assert saved["monitor_channel_index"] == 1
    assert saved["monitor_gain_db"] == 0.0

    import_payload = _config_payload(
        999,
        config_name="Imported Legacy",
        channels=[_channel_payload(2)],
        selected_channels=[2],
        monitor_channel_index=0,
        monitor_gain_db=6.0,
    )
    import_payload["input_device_name"] = "Imported Mic"
    imported_id = svc.create_imported_config(
        import_payload,
        reserved_config_id=svc.reserve_import_config_id(),
    )
    imported = svc.load_config(imported_id)

    assert imported["config_name"] == "Imported Legacy"
    assert imported["input_device_name"] == "Imported Mic"
    assert imported["selected_channels"] == [2]
    assert imported["monitor_channel_index"] == 2
    assert imported["monitor_gain_db"] == 0.0

    overwrite_payload = _config_payload(
        999,
        config_name="Imported Legacy",
        channels=[_channel_payload(3)],
        selected_channels=[3],
        monitor_channel_index=0,
        monitor_gain_db=-18.0,
    )
    overwrite_payload["input_device_name"] = "Overwrite Mic"
    svc.overwrite_imported_config(imported_id, overwrite_payload)
    overwritten = svc.load_config(imported_id)

    assert overwritten["config_name"] == "Imported Legacy"
    assert overwritten["input_device_name"] == "Overwrite Mic"
    assert overwritten["selected_channels"] == [3]
    assert overwritten["monitor_channel_index"] == 3
    assert overwritten["monitor_gain_db"] == 0.0


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


def test_load_config_returns_default_fft_x_axis_scale_for_channel_rows():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))

    result = svc.load_config(cfg.config_id)

    assert result["channels"][0]["fft_x_axis_scale"] == "linear"


def test_load_config_defaults_stft_color_scale_fields_for_channel_rows():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1", selected_channels="[0]")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))

    result = svc.load_config(cfg.config_id)

    channel = result["channels"][0]
    assert channel["stft_color_scale_enabled"] is False
    assert channel["stft_color_scale_min_db"] == -120.0
    assert channel["stft_color_scale_max_db"] == 0.0


def test_load_config_defaults_stft_score_fields_for_channel_rows():
    svc = _service()
    cfg = ConfigTable.create(config_name="Cfg1", selected_channels="[0]")
    ChannelConfigTable.create(config=cfg, **_channel_payload(0))

    result = svc.load_config(cfg.config_id)

    channel = result["channels"][0]
    for field, expected in _STFT_SCORE_DEFAULTS.items():
        assert channel[field] == expected


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
            "fault_types": [],
            "input_hardware_device_id": 1,
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
            "fault_types": [],
            "input_hardware_device_id": 1,
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


def test_save_config_persists_fft_x_axis_scale():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_x_axis_scale"] = "log"

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "fault_types": [],
            "input_hardware_device_id": 1,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )

    assert loaded["channels"][0]["fft_x_axis_scale"] == "log"
    assert getattr(row, "fft_x_axis_scale", None) == "log"


def test_save_config_persists_fft_weighting():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_weighting"] = "C"

    svc.save_config(
        {
            "config_id": config_id,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": False,
            "fault_types": [],
            "input_hardware_device_id": 1,
            "selected_channels": [0],
            "channels": [channel],
        }
    )

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )

    assert loaded["channels"][0]["fft_weighting"] == "C"
    assert row.fft_weighting == "C"


def test_save_config_preserves_grouped_fft_and_octave_limits():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 80.0},
            ],
        }
    ]
    channel["oct_band_limits"] = [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 20.0},
                {"freq": 250.0, "limit": 25.0},
            ],
        }
    ]

    svc.save_config(
        _config_payload(config_id, channels=[channel], selected_channels=[0])
    )

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )
    assert loaded["channels"][0]["fft_band_limits"] == channel["fft_band_limits"]
    assert loaded["channels"][0]["oct_band_limits"] == channel["oct_band_limits"]
    assert json.loads(row.fft_band_limits) == channel["fft_band_limits"]
    assert json.loads(row.oct_band_limits) == channel["oct_band_limits"]


def test_save_config_converts_legacy_paired_fft_and_octave_limits_to_grouped_storage():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"freq": 1000.0, "lower": 40.0, "upper": 100.0},
        {"freq": 2000.0, "lower": 50.0, "upper": 90.0},
        {"freq": 2000.0, "lower": 55.0, "upper": 85.0},
    ]
    channel["oct_band_limits"] = [
        {"freq": 125.0, "lower": 15.0, "upper": 60.0},
        {"freq": 250.0, "lower": 20.0, "upper": 55.0},
    ]

    svc.save_config(
        _config_payload(config_id, channels=[channel], selected_channels=[0])
    )

    expected_fft = [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 40.0},
                {"freq": 2000.0, "limit": 50.0},
                {"freq": 2000.0, "limit": 55.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
                {"freq": 2000.0, "limit": 85.0},
            ],
        },
    ]
    expected_octave = [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 15.0},
                {"freq": 250.0, "limit": 20.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 125.0, "limit": 60.0},
                {"freq": 250.0, "limit": 55.0},
            ],
        },
    ]

    loaded = svc.load_config(config_id)
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )
    assert loaded["channels"][0]["fft_band_limits"] == expected_fft
    assert loaded["channels"][0]["oct_band_limits"] == expected_octave
    assert json.loads(row.fft_band_limits) == expected_fft
    assert json.loads(row.oct_band_limits) == expected_octave


def test_save_config_drops_empty_grouped_limit_tables_without_validation_error():
    svc = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"kind": "upper", "points": []},
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 40.0},
                {"freq": 2000.0, "limit": 45.0},
            ],
        },
    ]
    channel["oct_band_limits"] = [{"kind": "upper", "points": []}]
    payload = _config_payload(config_id, channels=[channel], selected_channels=[0])

    assert svc.validate(payload) == []
    svc.save_config(payload)

    loaded = svc.load_config(config_id)
    assert loaded["channels"][0]["fft_band_limits"] == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 40.0},
                {"freq": 2000.0, "limit": 45.0},
            ],
        }
    ]
    assert loaded["channels"][0]["oct_band_limits"] == []


def test_save_config_persists_stft_color_scale_fields():
    service = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    config = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    config["channels"][0]["stft_color_scale_enabled"] = True
    config["channels"][0]["stft_color_scale_min_db"] = -90.5
    config["channels"][0]["stft_color_scale_max_db"] = -10.25

    config_id = service.save_config(config)

    loaded = service.load_config(config_id)
    channel = loaded["channels"][0]
    assert channel["stft_color_scale_enabled"] is True
    assert channel["stft_color_scale_min_db"] == -90.5
    assert channel["stft_color_scale_max_db"] == -10.25


def test_save_config_persists_stft_score_fields():
    service = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    config = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    config["channels"][0].update(_STFT_SCORE_CUSTOM)

    config_id = service.save_config(config)

    loaded = service.load_config(config_id)
    channel = loaded["channels"][0]
    row = ChannelConfigTable.get(
        (ChannelConfigTable.config == config_id)
        & (ChannelConfigTable.channel_index == 0)
    )
    for field, expected in _STFT_SCORE_CUSTOM.items():
        assert channel[field] == expected
        assert getattr(row, field) == expected


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


def test_validate_rejects_more_than_two_selected_channels():
    svc = _service()
    data = _config_payload(
        1,
        channels=[_channel_payload(0), _channel_payload(1), _channel_payload(2)],
        selected_channels=[0, 1, 2],
    )

    errors = svc.validate(data)

    assert any("最多" in error and "两个通道" in error for error in errors)


def test_validate_rejects_invalid_result_dashboard_layout():
    svc = _service()
    data = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    data["result_dashboard_layout"] = "{bad json"

    errors = svc.validate(data)

    assert any("结果展示布局" in error for error in errors)


def test_validate_rejects_selected_2_layout_binding_for_one_selected_channel():
    svc = _service()
    data = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    data["result_dashboard_layout"] = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "selected_2",
            },
        ],
    }

    errors = svc.validate(data)

    assert any("结果展示布局" in error and "通道2" in error for error in errors)


def test_validate_rejects_unknown_layout_panel_type():
    svc = _service()
    data = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    data["result_dashboard_layout"] = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "custom",
                "type": "custom_widget",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "primary_acoustic",
            },
        ],
    }

    errors = svc.validate(data)

    assert any(
        "结果展示布局" in error and ("面板" in error or "type" in error)
        for error in errors
    )


def test_validate_rejects_invalid_layout_channel_key():
    svc = _service()
    data = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    data["result_dashboard_layout"] = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "bad_channel",
            },
        ],
    }

    errors = svc.validate(data)

    assert any("结果展示布局" in error and "通道" in error for error in errors)


@pytest.mark.parametrize("monitor_channel_index", [True, "1", 1.5, -1])
def test_validate_rejects_invalid_monitor_channel(monitor_channel_index):
    svc = _service()
    payload = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
        monitor_channel_index=monitor_channel_index,
    )

    errors = svc.validate(payload)

    assert any("监听通道" in err for err in errors)


def test_validate_rejects_monitor_channel_outside_managed_channel_count(
    valid_managed_input,
):
    svc = _service()
    valid_managed_input.channel_count = 2
    valid_managed_input.save()
    payload = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
        monitor_channel_index=2,
    )

    errors = svc.validate(payload)

    assert any("监听通道" in err and "输入通道" in err for err in errors)


def test_validate_uses_managed_channel_count_instead_of_smaller_legacy_channel_count():
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="2")
    payload = _config_payload(
        1,
        channels=[_channel_payload(0), _channel_payload(2)],
        selected_channels=[0, 2],
        monitor_channel_index=2,
        vibration_view_channel_index=2,
    )

    errors = svc.validate(payload)

    assert "\u76d1\u542c\u901a\u9053\u5fc5\u987b\u5c0f\u4e8e\u8f93\u5165\u901a\u9053\u6570" not in errors
    assert "\u632f\u52a8\u89c6\u56fe\u901a\u9053\u5fc5\u987b\u5c0f\u4e8e\u8f93\u5165\u901a\u9053\u6570" not in errors


def test_validate_uses_managed_channel_count_instead_of_larger_legacy_channel_count(
    valid_managed_input,
):
    svc = _service()
    valid_managed_input.channel_count = 2
    valid_managed_input.save()
    DeviceTable.create(id=1, input_channel_count="4")
    payload = _config_payload(
        1,
        channels=[_channel_payload(0), _channel_payload(2)],
        selected_channels=[0, 2],
        monitor_channel_index=2,
        vibration_view_channel_index=2,
    )

    errors = svc.validate(payload)

    assert errors.count(
        "\u76d1\u542c\u901a\u9053\u5fc5\u987b\u5c0f\u4e8e\u8f93\u5165\u901a\u9053\u6570"
    ) == 1
    assert errors.count(
        "\u632f\u52a8\u89c6\u56fe\u901a\u9053\u5fc5\u987b\u5c0f\u4e8e\u8f93\u5165\u901a\u9053\u6570"
    ) == 1


@pytest.mark.parametrize(
    "hardware_id",
    [_MISSING, None, True, False, 0, -1, 1.0, "1"],
)
def test_validate_reports_invalid_input_hardware_id_once_without_legacy_fallback(
    hardware_id,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        monitor_channel_index=9,
        vibration_view_channel_index=9,
    )
    if hardware_id is _MISSING:
        payload.pop("input_hardware_device_id")
    else:
        payload["input_hardware_device_id"] = hardware_id

    errors = svc.validate(payload)

    assert errors.count(_INVALID_MANAGED_INPUT_ERROR) == 1


@pytest.mark.parametrize("remove_bound_row", [False, True])
def test_validate_reports_unavailable_input_hardware_once_without_legacy_fallback(
    valid_managed_input,
    remove_bound_row,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        monitor_channel_index=9,
        vibration_view_channel_index=9,
    )
    if remove_bound_row:
        valid_managed_input.delete_instance()
    else:
        payload["input_hardware_device_id"] = 999

    errors = svc.validate(payload)

    assert errors.count(_UNAVAILABLE_MANAGED_INPUT_ERROR) == 1


@pytest.mark.parametrize("channel_count", [None, True, 4.0, "4", 0, -1])
def test_validate_rejects_invalid_managed_input_channel_count_once(
    monkeypatch,
    channel_count,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        monitor_channel_index=9,
        vibration_view_channel_index=9,
    )
    monkeypatch.setattr(
        InputHardwareTable,
        "get_by_id",
        lambda _hardware_id: SimpleNamespace(channel_count=channel_count),
    )

    errors = svc.validate(payload)

    assert errors.count(_UNAVAILABLE_MANAGED_INPUT_ERROR) == 1


def test_validate_reports_managed_input_lookup_exception_once(monkeypatch):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        monitor_channel_index=9,
        vibration_view_channel_index=9,
    )

    def raise_lookup_error(_hardware_id):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(InputHardwareTable, "get_by_id", raise_lookup_error)

    errors = svc.validate(payload)

    assert errors.count(_UNAVAILABLE_MANAGED_INPUT_ERROR) == 1


@pytest.mark.parametrize(
    ("channel_index", "expected_count"),
    [(3, 0), (4, 1)],
)
def test_validate_uses_zero_based_managed_channel_boundaries(
    channel_index,
    expected_count,
):
    svc = _service()
    payload = _config_payload(
        1,
        selected_channels=[channel_index],
        monitor_channel_index=channel_index,
        vibration_view_channel_index=channel_index,
    )

    errors = svc.validate(payload)

    assert errors.count(_MONITOR_CHANNEL_RANGE_ERROR) == expected_count
    assert errors.count(_VIBRATION_CHANNEL_RANGE_ERROR) == expected_count


def test_validate_allows_null_monitor_and_vibration_with_valid_managed_input():
    svc = _service()
    payload = _config_payload(
        1,
        selected_channels=[],
        monitor_channel_index=None,
        vibration_view_channel_index=None,
    )

    errors = svc.validate(payload)

    assert _INVALID_MANAGED_INPUT_ERROR not in errors
    assert _UNAVAILABLE_MANAGED_INPUT_ERROR not in errors
    assert not any("\u76d1\u542c\u901a\u9053" in error for error in errors)
    assert not any("\u632f\u52a8\u89c6\u56fe\u901a\u9053" in error for error in errors)


def test_validate_resolves_managed_input_once_for_both_channel_fields(monkeypatch):
    svc = _service()
    payload = _config_payload(
        1,
        selected_channels=[2],
        monitor_channel_index=2,
        vibration_view_channel_index=2,
    )
    original_get_by_id = InputHardwareTable.get_by_id
    lookup_ids = []

    def counted_get_by_id(hardware_id):
        lookup_ids.append(hardware_id)
        return original_get_by_id(hardware_id)

    monkeypatch.setattr(InputHardwareTable, "get_by_id", counted_get_by_id)

    svc.validate(payload)

    assert lookup_ids == [1]


@pytest.mark.parametrize(
    "hardware_id",
    [_MISSING, None, True, False, 0, -1, 1.0, "1"],
)
def test_validate_factory_runtime_reports_invalid_input_hardware_id_once(
    hardware_id,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        vibration_view_channel_index=9,
    )
    if hardware_id is _MISSING:
        payload.pop("input_hardware_device_id")
    else:
        payload["input_hardware_device_id"] = hardware_id

    errors = svc.validate_factory_runtime(payload)

    assert errors.count(_INVALID_MANAGED_INPUT_ERROR) == 1


@pytest.mark.parametrize("channel_count", [_MISSING, None, True, 4.0, "4", 0, -1])
def test_validate_factory_runtime_reports_unavailable_input_hardware_once(
    monkeypatch,
    channel_count,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        vibration_view_channel_index=9,
    )
    if channel_count is _MISSING:
        payload["input_hardware_device_id"] = 999
    else:
        monkeypatch.setattr(
            InputHardwareTable,
            "get_by_id",
            lambda _hardware_id: SimpleNamespace(channel_count=channel_count),
        )

    errors = svc.validate_factory_runtime(payload)

    assert errors.count(_UNAVAILABLE_MANAGED_INPUT_ERROR) == 1


def test_validate_factory_runtime_reports_managed_lookup_exception_once(
    monkeypatch,
):
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="16")
    payload = _config_payload(
        1,
        selected_channels=[9],
        vibration_view_channel_index=9,
    )

    def raise_lookup_error(_hardware_id):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(InputHardwareTable, "get_by_id", raise_lookup_error)

    errors = svc.validate_factory_runtime(payload)

    assert errors.count(_UNAVAILABLE_MANAGED_INPUT_ERROR) == 1


def test_validate_factory_runtime_resolves_managed_input_once(monkeypatch):
    svc = _service()
    payload = _config_payload(
        1,
        selected_channels=[2],
        vibration_view_channel_index=2,
    )
    original_get_by_id = InputHardwareTable.get_by_id
    lookup_ids = []

    def counted_get_by_id(hardware_id):
        lookup_ids.append(hardware_id)
        return original_get_by_id(hardware_id)

    monkeypatch.setattr(InputHardwareTable, "get_by_id", counted_get_by_id)

    errors = svc.validate_factory_runtime(payload)

    assert errors == []
    assert lookup_ids == [1]


def test_config_service_has_no_legacy_managed_channel_count_dependency():
    source = (
        Path(__file__).parents[1] / "app" / "service" / "config_service.py"
    ).read_text(encoding="utf-8")

    assert "DeviceTable" not in source
    assert "get_channel_count" not in source
    assert "input_channel_count" not in source


def test_validate_accepts_null_vibration_view_channel():
    svc = _service()
    data = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    data["vibration_view_channel_index"] = None

    errors = svc.validate(data)

    assert not any("振动视图通道" in error for error in errors)


def test_validate_accepts_selected_vibration_view_channel():
    svc = _service()
    DeviceTable.create(id=1, input_channel_count="4")
    data = _config_payload(
        1,
        channels=[_channel_payload(0), _channel_payload(2)],
        selected_channels=[0, 2],
    )
    data["vibration_view_channel_index"] = 2

    errors = svc.validate(data)

    assert not any("振动视图通道" in error for error in errors)


@pytest.mark.parametrize("value", [-1, True, "1", 1.5])
def test_validate_rejects_invalid_vibration_view_channel_values(value):
    svc = _service()
    data = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    data["vibration_view_channel_index"] = value

    errors = svc.validate(data)

    assert any("振动视图通道" in error for error in errors)


def test_validate_rejects_vibration_view_channel_not_selected():
    svc = _service()
    data = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    data["vibration_view_channel_index"] = 1

    errors = svc.validate(data)

    assert "振动视图通道必须来自已选择通道" in errors
    assert errors.count("振动视图通道必须来自已选择通道") == 1


def test_validate_rejects_vibration_view_channel_outside_managed_channel_count(
    valid_managed_input,
):
    svc = _service()
    valid_managed_input.channel_count = 2
    valid_managed_input.save()
    data = _config_payload(
        1,
        channels=[_channel_payload(0), _channel_payload(2)],
        selected_channels=[0, 2],
    )
    data["vibration_view_channel_index"] = 2

    errors = svc.validate(data)

    assert any("振动视图通道" in error and "输入通道" in error for error in errors)


@pytest.mark.parametrize("value", [-1, True, "1", 1])
def test_validate_factory_runtime_rejects_invalid_vibration_view_channel(value):
    svc = _service()
    data = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    data["vibration_view_channel_index"] = value

    errors = svc.validate_factory_runtime(data)

    assert any("振动视图通道" in error for error in errors)


@pytest.mark.parametrize(
    "monitor_gain_db",
    [True, "0", float("nan"), float("inf"), -60.1, 35.1],
)
def test_validate_rejects_invalid_monitor_gain(monitor_gain_db):
    svc = _service()
    payload = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
        monitor_gain_db=monitor_gain_db,
    )

    errors = svc.validate(payload)

    assert any("监听增益" in err for err in errors)


def test_validate_accepts_new_monitor_gain_upper_bound_without_changing_default():
    svc = _service()
    payload = _config_payload(
        1,
        channels=[_channel_payload(0)],
        selected_channels=[0],
        monitor_gain_db=35.0,
    )

    assert not any("监听增益" in err for err in svc.validate(payload))
    assert MONITOR_GAIN_DEFAULT_DB == pytest.approx(0.0)


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


def test_validate_rejects_invalid_fft_x_axis_scale():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_x_axis_scale"] = "semilog"

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

    assert any("FFT 横轴模式" in err for err in errors)


def test_validate_rejects_invalid_fft_weighting():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_weighting"] = "B"

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

    assert "通道 1: FFT 计权模式无效" in errors


def test_validate_rejects_enabled_stft_color_scale_with_invalid_range():
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    channel = config["channels"][0]
    channel["stft_color_scale_enabled"] = True
    channel["stft_color_scale_min_db"] = -20.0
    channel["stft_color_scale_max_db"] = -20.0

    errors = service.validate(config)

    assert any("STFT 色阶下限必须小于上限" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stft_color_scale_min_db", float("nan")),
        ("stft_color_scale_min_db", float("inf")),
        ("stft_color_scale_max_db", float("nan")),
        ("stft_color_scale_max_db", float("-inf")),
    ],
)
def test_validate_rejects_enabled_stft_color_scale_with_non_finite_bounds(
    field,
    value,
):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    channel = config["channels"][0]
    channel["stft_color_scale_enabled"] = True
    channel["stft_color_scale_min_db"] = -90.0
    channel["stft_color_scale_max_db"] = -10.0
    channel[field] = value

    errors = service.validate(config)

    assert any("STFT 色阶上下限必须为有限数值" in error for error in errors)


@pytest.mark.parametrize("enabled", ["false", "yes", 1, None])
def test_validate_rejects_stft_color_scale_enabled_non_bool(enabled):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    channel = config["channels"][0]
    channel["stft_color_scale_enabled"] = enabled
    channel["stft_color_scale_min_db"] = -90.0
    channel["stft_color_scale_max_db"] = -10.0

    errors = service.validate(config)

    assert any("STFT 固定色阶开关必须为布尔值" in error for error in errors)


def test_save_config_rejects_enabled_stft_color_scale_with_non_finite_bounds():
    service = _service()
    config_id = ConfigTable.create(config_name="Cfg1").config_id
    config = _config_payload(
        config_id,
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    channel = config["channels"][0]
    channel["stft_color_scale_enabled"] = True
    channel["stft_color_scale_min_db"] = float("nan")
    channel["stft_color_scale_max_db"] = -10.0

    with pytest.raises(ValueError, match="STFT 色阶上下限必须为有限数值"):
        service.save_config(config)


def test_create_imported_config_rejects_enabled_stft_color_scale_with_non_finite_bounds():
    service = _service()
    payload = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    channel = payload["channels"][0]
    channel["stft_color_scale_enabled"] = True
    channel["stft_color_scale_min_db"] = -90.0
    channel["stft_color_scale_max_db"] = float("inf")
    reserved_id = service.reserve_import_config_id()

    with pytest.raises(ValueError, match="STFT 色阶上下限必须为有限数值"):
        service.create_imported_config(payload, reserved_config_id=reserved_id)


def test_create_imported_config_rejects_stft_color_scale_enabled_non_bool():
    service = _service()
    payload = _config_payload(
        999,
        config_name="Imported",
        channels=[_channel_payload(0)],
        selected_channels=[0],
    )
    channel = payload["channels"][0]
    channel["stft_color_scale_enabled"] = "false"
    channel["stft_color_scale_min_db"] = -90.0
    channel["stft_color_scale_max_db"] = -10.0
    reserved_id = service.reserve_import_config_id()

    with pytest.raises(ValueError, match="STFT 固定色阶开关必须为布尔值"):
        service.create_imported_config(payload, reserved_config_id=reserved_id)


@pytest.mark.parametrize("enabled", ["false", 1, None])
def test_validate_rejects_stft_score_enabled_non_bool(enabled):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0]["stft_score_enabled"] = enabled

    errors = service.validate(config)

    assert any("STFT 评分启用必须为布尔值" in error for error in errors)


@pytest.mark.parametrize("summary_mode", ["median", "", None])
def test_validate_rejects_invalid_stft_score_summary_mode(summary_mode):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0]["stft_score_summary_mode"] = summary_mode

    errors = service.validate(config)

    assert any("STFT 评分汇总方式无效" in error for error in errors)


@pytest.mark.parametrize("summary_mode", [[], {"mode": "percentile"}])
def test_validate_rejects_structured_stft_score_summary_mode(summary_mode):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0]["stft_score_summary_mode"] = summary_mode

    errors = service.validate(config)

    assert any("STFT 评分汇总方式无效" in error for error in errors)


@pytest.mark.parametrize(
    "percentile",
    [0.0, -1.0, 100.1, float("nan"), float("inf"), "10", True],
)
def test_validate_rejects_invalid_stft_score_percentile(percentile):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0]["stft_score_percentile"] = percentile

    errors = service.validate(config)

    assert any("STFT 评分分位数" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("stft_score_loudness_reference_sone", "响度参考值"),
        ("stft_score_modulation_reference", "响度变化参考值"),
        ("stft_score_harmonic_ratio_reference", "谐波占比参考值"),
        ("stft_score_frequency_instability_reference", "频率不稳定参考值"),
    ],
)
@pytest.mark.parametrize("value", [0.0, -0.1, float("nan"), float("inf"), "1", True])
def test_validate_rejects_invalid_stft_score_references(field, label, value):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0][field] = value

    errors = service.validate(config)

    assert any(f"STFT 评分{label}必须为正数" in error for error in errors)


@pytest.mark.parametrize(
    ("updates", "expected_fragment"),
    [
        ({"stft_score_harmonic_start_order": 1}, "谐波起始阶次必须大于等于2"),
        (
            {
                "stft_score_harmonic_start_order": 5,
                "stft_score_harmonic_end_order": 4,
            },
            "谐波结束阶次必须大于等于起始阶次",
        ),
    ],
)
def test_validate_rejects_invalid_stft_score_harmonic_order_range(
    updates,
    expected_fragment,
):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0].update(updates)

    errors = service.validate(config)

    assert any(f"STFT 评分{expected_fragment}" in error for error in errors)


@pytest.mark.parametrize("field", ["stft_score_harmonic_start_order", "stft_score_harmonic_end_order"])
@pytest.mark.parametrize("value", [2.5, "3", True])
def test_validate_rejects_non_integer_stft_score_harmonic_orders(field, value):
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0][field] = value

    errors = service.validate(config)

    expected = "起始阶次" if field.endswith("start_order") else "结束阶次"
    assert any(f"STFT 评分谐波{expected}必须为整数" in error for error in errors)


def test_validate_rejects_invalid_stft_score_values_when_disabled():
    service = _service()
    config = _config_payload(1, channels=[_channel_payload(0)], selected_channels=[0])
    config["channels"][0].update(
        {
            "stft_score_enabled": False,
            "stft_score_loudness_reference_sone": 0.0,
            "stft_score_harmonic_start_order": 1,
        }
    )

    errors = service.validate(config)

    assert any("STFT 评分响度参考值必须为正数" in error for error in errors)
    assert any("STFT 评分谐波起始阶次必须大于等于2" in error for error in errors)


def test_validate_accepts_grouped_upper_only_fft_limits():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 50.0},
            ],
        }
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert errors == []


def test_validate_accepts_grouped_lower_only_octave_limits():
    svc = _service()
    channel = _channel_payload(0)
    channel["oct_band_limits"] = [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 20.0},
                {"freq": 250.0, "limit": 25.0},
            ],
        }
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert errors == []


def test_validate_accepts_unsorted_and_duplicate_fft_limit_control_points():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"freq": 300.0, "lower": 60.0, "upper": 90.0},
        {"freq": 100.0, "lower": 30.0, "upper": 80.0},
        {"freq": 300.0, "lower": 80.0, "upper": 100.0},
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert not [error for error in errors if "FFT" in error]


def test_validate_rejects_fft_limits_with_less_than_two_distinct_frequencies():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"freq": 300.0, "lower": 60.0, "upper": 90.0},
        {"freq": 300.0, "lower": 70.0, "upper": 100.0},
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert any("FFT限制表" in error and "至少需要两个不同频率" in error for error in errors)


def test_validate_rejects_fft_duplicate_collapsed_invalid_envelope():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"freq": 100.0, "lower": 30.0, "upper": 60.0},
        {"freq": 100.0, "lower": 70.0, "upper": 80.0},
        {"freq": 200.0, "lower": 30.0, "upper": 90.0},
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert "通道 1: FFT限制表下限必须小于上限" in errors


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("freq", float("inf"), "通道 1: FFT限制表第 1 行频率必须是有限数字"),
        ("freq", float("nan"), "通道 1: FFT限制表第 1 行频率必须是有限数字"),
        ("lower", float("nan"), "通道 1: FFT限制表第 1 行下限必须是有限数字"),
        ("upper", float("inf"), "通道 1: FFT限制表第 1 行上限必须是有限数字"),
    ],
)
def test_validate_rejects_non_finite_fft_limit_values(field, value, message):
    svc = _service()
    channel = _channel_payload(0)
    band = {"freq": 100.0, "lower": 30.0, "upper": 60.0}
    band[field] = value
    channel["fft_band_limits"] = [
        band,
        {"freq": 200.0, "lower": 30.0, "upper": 60.0},
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert message in errors


def test_validate_rejects_mixed_legacy_and_grouped_limit_shapes():
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = [
        {"freq": 1000.0, "lower": 40.0, "upper": 100.0},
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 80.0},
            ],
        },
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert "通道 1: FFT限制表不能混合旧版频点格式和分组格式" in errors


def test_validate_rejects_malformed_grouped_point_data():
    svc = _service()
    channel = _channel_payload(0)
    channel["oct_band_limits"] = [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 20.0},
                {"freq": 250.0, "limit": "bad"},
            ],
        }
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert "通道 1: 倍频程限制表第 1 项第 2 个频点限值必须是有限数字" in errors


@pytest.mark.parametrize(
    ("limits", "expected", "raw_fragment"),
    [
        (
            [
                {
                    "kind": "ceiling",
                    "points": [
                        {"freq": 1000.0, "limit": 100.0},
                        {"freq": 2000.0, "limit": 90.0},
                    ],
                }
            ],
            "通道 1: FFT限制表第 1 项类型必须是上限或下限",
            "upper 或 lower",
        ),
        (
            [{"kind": "upper", "points": "bad"}],
            "通道 1: FFT限制表第 1 项频点列表必须是列表",
            "points 必须是列表",
        ),
    ],
)
def test_validate_maps_grouped_limit_shape_messages_for_ui(
    limits, expected, raw_fragment
):
    svc = _service()
    channel = _channel_payload(0)
    channel["fft_band_limits"] = limits

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert expected in errors
    assert not any(raw_fragment in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("fft_band_limits", None, "通道 1: FFT限制表必须是列表"),
        ("oct_band_limits", {"kind": "upper"}, "通道 1: 倍频程限制表必须是列表"),
    ],
)
def test_validate_rejects_none_or_non_list_limit_fields(field, value, expected):
    svc = _service()
    channel = _channel_payload(0)
    channel[field] = value

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert expected in errors


def test_validate_rejects_octave_one_point_table():
    svc = _service()
    channel = _channel_payload(0)
    channel["oct_band_limits"] = [
        {
            "kind": "upper",
            "points": [{"freq": 1000.0, "limit": 80.0}],
        }
    ]

    errors = svc.validate(
        _config_payload(1, channels=[channel], selected_channels=[0])
    )

    assert "通道 1: 倍频程限制表第 1 项至少需要两个不同频率" in errors


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
            "fault_types": [],
            "input_hardware_device_id": 1,
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
            "input_hardware_device_id": 1,
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
                "fault_types": [],
                "input_hardware_device_id": 1,
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
                "fault_types": [],
                "input_hardware_device_id": 1,
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
                "fault_types": [],
                "input_hardware_device_id": 1,
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
