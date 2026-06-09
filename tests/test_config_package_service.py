from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

import app.service.config_package_service as config_package_module
from app.service.ai_service import AiService
from app.service.config_package_service import (
    ConfigPackageExportOptions,
    ConfigPackageImportDecision,
    ConfigPackageService,
)
from app.service.config_service import ConfigService
from app.service.current_meter_service import normalize_current_meter_config
from app.service.golden_sample_service import GoldenSampleService
from model.channel_config_table import ChannelConfigTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable
from model.test_config_table import TestConfigTable as ConfigTable

_TOOLCHAIN_MARKER_NAME = ".jingcheng_toolchain_id"


class FakeBus:
    config_changed = MagicMock()


def _make_services(tmp_path: Path) -> SimpleNamespace:
    FakeBus.config_changed.reset_mock()
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    config_service = ConfigService(FakeBus(), golden_sample_service=golden_service)
    ai_service = AiService()
    return SimpleNamespace(
        config_service=config_service,
        ai_service=ai_service,
        config_package_service=ConfigPackageService(
            config_service=config_service,
            ai_service=ai_service,
        ),
    )


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
            analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (plc_id) REFERENCES plc_table (id) ON DELETE SET NULL
        )
        """
    )
    ChannelConfigTable.create_table(safe=True)
    AiBindingTable.create_table(safe=True)


def _recreate_accepted_legacy_config_schema_without_current_meter_columns() -> None:
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
            vibration_view_channel_index INTEGER,
            analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0,
            monitor_channel_index INTEGER,
            monitor_gain_db REAL,
            FOREIGN KEY (plc_id) REFERENCES plc_table (id) ON DELETE SET NULL
        )
        """
    )
    ChannelConfigTable.create_table(safe=True)
    AiBindingTable.create_table(safe=True)


def _config_table_columns() -> set[str]:
    cursor = ConfigTable._meta.database.execute_sql("PRAGMA table_info(test_config_table)")
    return {row[1] for row in cursor.fetchall()}


@pytest.fixture
def services(tmp_path: Path) -> SimpleNamespace:
    bundle = _make_services(tmp_path)
    try:
        yield bundle
    finally:
        bundle.ai_service.shutdown()


def _channel_payload(channel_index: int) -> dict:
    return {
        "channel_index": channel_index,
        "stft_nfft": 1024,
        "fft_nfft": 4096,
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
        "loudness_unit": "sone",
        "loudness_lower_enabled": False,
        "loudness_upper_enabled": False,
        "loudness_lower_limit": 0.0,
        "loudness_upper_limit": 60.0,
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


_STFT_SCORE_CUSTOM = {
    "stft_score_enabled": False,
    "stft_score_summary_mode": "min",
    "stft_score_percentile": 35.0,
    "stft_score_loudness_reference_sone": 12.5,
    "stft_score_modulation_reference": 0.35,
    "stft_score_harmonic_ratio_reference": 0.45,
    "stft_score_frequency_instability_reference": 0.08,
    "stft_score_harmonic_start_order": 3,
    "stft_score_harmonic_end_order": 80,
}


def _config_payload(
    config_id: int,
    *,
    config_name: str = "Cfg1",
    monitor_channel_index: int | None = 0,
    monitor_gain_db: float | None = 0.0,
    vibration_view_channel_index: int | None = None,
) -> dict:
    return {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "analysis_window_offset_ms": 0,
        "allow_incomplete_recording_analysis": False,
        "scan_triggers_test": False,
        "plc_id": None,
        "input_device_name": "Mic 1",
        "input_device_hostapi": "ASIO",
        "input_device_samplerate": 48_000.0,
        "output_device_name": "Speaker 1",
        "output_device_hostapi": "WASAPI",
        "output_device_samplerate": 44_100.0,
        "selected_channels": [0],
        "monitor_channel_index": monitor_channel_index,
        "monitor_gain_db": monitor_gain_db,
        "vibration_view_channel_index": vibration_view_channel_index,
        "channels": [_channel_payload(0)],
    }


def _channel_payload_with_golden(channel_index: int, **binding) -> dict:
    channel = _channel_payload(channel_index)
    if "label" in binding:
        channel["golden_sample_label"] = binding["label"]
    if "source_channel" in binding:
        channel["golden_sample_source_channel"] = binding["source_channel"]
    if "start_sample" in binding:
        channel["golden_sample_start_sample"] = binding["start_sample"]
    if "num_samples" in binding:
        channel["golden_sample_num_samples"] = binding["num_samples"]
    if "start_ms" in binding:
        channel["golden_sample_start_ms"] = binding["start_ms"]
    if "duration_ms" in binding:
        channel["golden_sample_duration_ms"] = binding["duration_ms"]
    if "sample_rate" in binding:
        channel["golden_sample_sample_rate"] = binding["sample_rate"]
    channel.update({
        key: value
        for key, value in binding.items()
        if key
        not in {
            "label",
            "source_channel",
            "start_sample",
            "num_samples",
            "start_ms",
            "duration_ms",
            "sample_rate",
        }
    })
    return channel


def _write_wav(path: Path, *, samples: int = 144_000, sample_rate: int = 48_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.linspace(-0.5, 0.5, num=samples, dtype=np.float32)
    wavfile.write(path, sample_rate, data)
    return path


def _golden_binding(
    source_path: Path,
    *,
    num_samples: int = 144_000,
    sample_rate: float = 48_000.0,
    duration_ms: int = 3000,
) -> dict:
    return {
        "label": source_path.name,
        "source_channel": 0,
        "start_sample": 0,
        "num_samples": num_samples,
        "start_ms": 0,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
    }


def _toolchain_payload(package_dir: Path, **overrides) -> dict:
    payload = {
        "toolchain_id": "motor-ai",
        "display_name": "Motor AI",
        "version": "1.0",
        "entrypoint": "python main.py",
        "package_dir": str(package_dir),
        "default_timeout_ms": 30_000,
        "enabled": True,
        "description": "portable",
        "execution_mode": "resident_process",
        "params_json": "{}",
    }
    payload.update(overrides)
    return payload


def _create_saved_config(
    services: SimpleNamespace,
    *,
    name: str = "Cfg1",
    golden_binding: dict | None = None,
) -> int:
    config_id = services.config_service.create_config(name)
    payload = _config_payload(config_id, config_name=name)
    if golden_binding is not None:
        payload["channels"][0].update(golden_binding)
    services.config_service.save_config(payload)
    return config_id


def _bind_toolchain(
    services: SimpleNamespace,
    *,
    config_id: int,
    package_dir: Path,
    binding_enabled: bool = True,
    **toolchain_overrides,
) -> str:
    toolchain_id = services.ai_service.save_toolchain(
        None,
        _toolchain_payload(package_dir, **toolchain_overrides),
    )
    services.ai_service.save_binding(
        config_id,
        {
            "enabled": binding_enabled,
            "toolchain_id": toolchain_id,
            "timeout_ms": None,
        },
    )
    return toolchain_id


def _package_entries(
    *,
    include_ai_metadata: bool = False,
    include_ai_package: bool = False,
    config_payload: dict | None = None,
    ai_binding: dict | None = None,
    golden_assets: dict[int, bytes] | None = None,
    toolchain_overrides: dict | None = None,
) -> dict[str, object]:
    config_root = config_payload or _config_payload(999, config_name="Imported")
    golden_assets = golden_assets or {}
    binding_payload = ai_binding
    if binding_payload is None:
        binding_payload = {
            "enabled": include_ai_metadata,
            "toolchain_id": "motor-ai" if include_ai_metadata else None,
            "timeout_ms": None,
        }
    config_payload = {
        "config": config_root,
        "ai_binding": binding_payload,
        "golden_sample_assets": {
            str(channel_index): f"golden_samples/channel_{channel_index}.wav"
            for channel_index in golden_assets
        },
    }
    manifest = {
        "format": "jingcheng-config-package",
        "format_version": 1,
        "app": "JingChengMechanical",
        "exported_at": "2026-04-20T12:00:00Z",
        "contents": {
            "config": True,
            "golden_samples": bool(golden_assets),
            "ai_toolchain": include_ai_metadata,
            "ai_package": include_ai_package,
        },
    }
    entries: dict[str, object] = {
        "manifest.json": manifest,
        "config/config.json": config_payload,
    }
    if include_ai_metadata:
        entries["ai/toolchain.json"] = {
            "toolchain_id": "motor-ai",
            "display_name": "Motor AI",
            "version": "1.0",
            "entrypoint": "python main.py",
            "protocol_version": 1,
            "execution_mode": "resident_process",
            "default_timeout_ms": 30_000,
            "enabled": True,
            "description": "portable",
            "params_json": "{}",
            "source_package_dir": "D:/source-machine/motor-ai",
            **({"package_archive_path": "ai/package"} if include_ai_package else {}),
        }
        if toolchain_overrides:
            entries["ai/toolchain.json"].update(toolchain_overrides)
    if include_ai_package:
        entries["ai/package/main.py"] = b"print('ok')\n"
    for channel_index, payload in golden_assets.items():
        entries[f"golden_samples/channel_{channel_index}.wav"] = payload
    return entries


def _write_package(path: Path, entries: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in entries.items():
            if isinstance(payload, zipfile.ZipInfo):
                zf.writestr(payload, b"payload")
                continue
            if isinstance(payload, bytes):
                zf.writestr(name, payload)
                continue
            zf.writestr(name, json.dumps(payload, ensure_ascii=False))
    return path


def test_export_writes_manifest_and_single_config_payload(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="Cfg1")
    target = tmp_path / "export.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(),
    )

    with zipfile.ZipFile(target) as zf:
        assert "manifest.json" in zf.namelist()
        assert "config/config.json" in zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
        payload = json.loads(zf.read("config/config.json"))

    assert manifest["format"] == "jingcheng-config-package"
    assert manifest["format_version"] == 1
    assert manifest["app"] == "JingChengMechanical"
    assert isinstance(manifest["exported_at"], str)
    assert manifest["contents"]["config"] is True
    assert payload["config"]["config_name"] == "Cfg1"
    channel = payload["config"]["channels"][0]
    assert channel["spl_lower_enabled"] is False
    assert channel["spl_average_upper_limit"] == 85.0
    assert channel["loudness_average_upper_enabled"] is False
    assert channel["loudness_average_upper_limit"] == 60.0
    assert payload["ai_binding"] == {
        "enabled": False,
        "toolchain_id": None,
        "timeout_ms": None,
    }


def test_export_writes_monitor_fields_in_config_payload(
    tmp_path: Path,
    services: SimpleNamespace,
):
    config_id = services.config_service.create_config("CfgMonitor")
    payload = _config_payload(
        config_id,
        config_name="CfgMonitor",
        monitor_channel_index=2,
        monitor_gain_db=-9.5,
    )
    services.config_service.save_config(payload)
    target = tmp_path / "monitor.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(),
    )

    with zipfile.ZipFile(target) as zf:
        package_payload = json.loads(zf.read("config/config.json"))

    assert package_payload["config"]["monitor_channel_index"] == 2
    assert package_payload["config"]["monitor_gain_db"] == -9.5


def test_export_writes_vibration_view_channel_in_config_payload(
    tmp_path: Path,
    services: SimpleNamespace,
):
    config_id = services.config_service.create_config("CfgVibration")
    payload = _config_payload(
        config_id,
        config_name="CfgVibration",
        vibration_view_channel_index=0,
    )
    services.config_service.save_config(payload)
    target = tmp_path / "vibration.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(),
    )

    with zipfile.ZipFile(target) as zf:
        package_payload = json.loads(zf.read("config/config.json"))

    assert package_payload["config"]["vibration_view_channel_index"] == 0


def test_export_package_preserves_stft_color_scale_fields(
    tmp_path: Path,
    services: SimpleNamespace,
):
    config_id = services.config_service.create_config("Color Scale")
    payload = _config_payload(config_id, config_name="Color Scale")
    payload["channels"][0].update(
        {
            "stft_color_scale_enabled": True,
            "stft_color_scale_min_db": -88.0,
            "stft_color_scale_max_db": -8.0,
        }
    )
    services.config_service.save_config(payload)
    target = tmp_path / "stft-color-scale-export.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(),
    )

    with zipfile.ZipFile(target) as zf:
        package_payload = json.loads(zf.read("config/config.json"))

    channel = package_payload["config"]["channels"][0]
    assert channel["stft_color_scale_enabled"] is True
    assert channel["stft_color_scale_min_db"] == pytest.approx(-88.0)
    assert channel["stft_color_scale_max_db"] == pytest.approx(-8.0)


def test_export_package_preserves_stft_score_fields(
    tmp_path: Path,
    services: SimpleNamespace,
):
    config_id = services.config_service.create_config("STFT Score")
    payload = _config_payload(config_id, config_name="STFT Score")
    payload["channels"][0].update(_STFT_SCORE_CUSTOM)
    services.config_service.save_config(payload)
    target = tmp_path / "stft-score-export.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(),
    )

    with zipfile.ZipFile(target) as zf:
        package_payload = json.loads(zf.read("config/config.json"))

    channel = package_payload["config"]["channels"][0]
    for field, expected in _STFT_SCORE_CUSTOM.items():
        assert channel[field] == expected


def test_export_includes_selected_golden_samples(tmp_path: Path, services: SimpleNamespace):
    source_wav = _write_wav(tmp_path / "source.wav")
    config_id = _create_saved_config(
        services,
        name="CfgWithGolden",
        golden_binding=services.config_service.golden_sample_service.restore_imported_sample(
            config_id=1,
            channel_index=0,
            source_path=str(source_wav),
            metadata=_golden_binding(source_wav),
        ),
    )
    target = tmp_path / "golden.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(include_golden_samples=True),
    )

    with zipfile.ZipFile(target) as zf:
        payload = json.loads(zf.read("config/config.json"))
        manifest = json.loads(zf.read("manifest.json"))

        assert "golden_samples/channel_0.wav" in zf.namelist()
        assert zf.read("golden_samples/channel_0.wav")

    assert payload["golden_sample_assets"] == {"0": "golden_samples/channel_0.wav"}
    assert manifest["contents"]["golden_samples"] is True


def test_export_includes_ai_toolchain_metadata(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="CfgWithAi")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    _bind_toolchain(
        services,
        config_id=config_id,
        package_dir=package_dir,
        params_json='{"exported":true}',
    )
    target = tmp_path / "toolchain.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(include_ai_toolchain=True),
    )

    with zipfile.ZipFile(target) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        toolchain = json.loads(zf.read("ai/toolchain.json"))

    assert manifest["contents"]["ai_toolchain"] is True
    assert manifest["contents"]["ai_package"] is False
    assert set(toolchain) == {
        "toolchain_id",
        "display_name",
        "version",
        "entrypoint",
        "protocol_version",
        "execution_mode",
        "default_timeout_ms",
        "enabled",
        "description",
        "params_json",
        "source_package_dir",
    }
    assert toolchain["params_json"] == '{"exported":true}'
    assert toolchain["source_package_dir"] == str(package_dir)
    assert "package_dir" not in toolchain


def test_export_includes_ai_package_files(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="CfgWithAiPackage")
    package_dir = tmp_path / "package"
    (package_dir / "models").mkdir(parents=True)
    (package_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (package_dir / "models" / "weights.bin").write_bytes(b"\x00\x01")
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)
    target = tmp_path / "toolchain-package.dy"

    services.config_package_service.export_package(
        config_id=config_id,
        target_path=str(target),
        options=ConfigPackageExportOptions(
            include_ai_toolchain=True,
            include_ai_package=True,
        ),
    )

    with zipfile.ZipFile(target) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        toolchain = json.loads(zf.read("ai/toolchain.json"))

        assert "ai/package/main.py" in zf.namelist()
        assert "ai/package/models/weights.bin" in zf.namelist()

    assert manifest["contents"]["ai_toolchain"] is True
    assert manifest["contents"]["ai_package"] is True
    assert toolchain["package_archive_path"] == "ai/package"
    assert "package_dir" not in toolchain


def test_export_requires_ai_toolchain_when_ai_package_requested(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="Cfg1")

    with pytest.raises(ValueError, match="include_ai_toolchain"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(include_ai_package=True),
        )


def test_export_rejects_missing_bound_toolchain_when_ai_metadata_requested(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="Cfg1")

    with pytest.raises(ValueError, match="toolchain"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(include_ai_toolchain=True),
        )


def test_export_rejects_missing_package_dir_when_ai_package_requested(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)
    package_dir.rmdir()

    with pytest.raises(ValueError, match="package_dir"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_empty_package_dir_when_ai_package_requested(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)

    with pytest.raises(ValueError, match="regular files|empty"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_missing_selected_golden_sample_file(tmp_path: Path, services: SimpleNamespace):
    source_wav = _write_wav(tmp_path / "source.wav")
    binding = services.config_service.golden_sample_service.restore_imported_sample(
        config_id=1,
        channel_index=0,
        source_path=str(source_wav),
        metadata=_golden_binding(source_wav),
    )
    config_id = _create_saved_config(services, name="Cfg1", golden_binding=binding)
    Path(binding["golden_sample_path"]).unlink()

    with pytest.raises(ValueError, match="golden"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(include_golden_samples=True),
        )


def test_export_rejects_symlink_in_ai_package_dir(tmp_path: Path, services: SimpleNamespace):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    link_path = package_dir / "linked.py"
    try:
        os.symlink(package_dir / "main.py", link_path)
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("symlink creation is not available")

    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)

    with pytest.raises(ValueError, match="symlink|reparse"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_duplicate_normalized_ai_archive_names(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    source_a = package_dir / "a.bin"
    source_b = package_dir / "b.bin"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)

    monkeypatch.setattr(
        services.config_package_service,
        "_iter_ai_package_files",
        lambda _path: [
            (str(source_a), "ai/package/model.bin", 1),
            (str(source_b), "ai/package/model.bin", 1),
        ],
    )

    with pytest.raises(ValueError, match="duplicate"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_case_insensitive_ai_archive_collisions_on_windows(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    if os.name != "nt":
        pytest.skip("case-insensitive collision validation is Windows-only")

    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    source_a = package_dir / "a.bin"
    source_b = package_dir / "b.bin"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)

    monkeypatch.setattr(
        services.config_package_service,
        "_iter_ai_package_files",
        lambda _path: [
            (str(source_a), "ai/package/Model.bin", 1),
            (str(source_b), "ai/package/model.bin", 1),
        ],
    )

    with pytest.raises(ValueError, match="case-insensitive"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_ai_package_path_length_limit(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.txt").write_bytes(b"a")
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)
    monkeypatch.setattr(config_package_module, "MAX_NORMALIZED_PATH_LENGTH_WINDOWS", 20)

    with pytest.raises(ValueError, match="path length"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_ai_package_file_count_limit(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (package_dir / "weights.bin").write_bytes(b"aa")
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)
    monkeypatch.setattr(config_package_module, "MAX_FILE_COUNT", 3)

    with pytest.raises(ValueError, match="file count"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_export_rejects_ai_package_size_limit(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    config_id = _create_saved_config(services, name="Cfg1")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (package_dir / "weights.bin").write_bytes(b"0123456789")
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)
    monkeypatch.setattr(config_package_module, "MAX_UNCOMPRESSED_BYTES", 20)

    with pytest.raises(ValueError, match="uncompressed"):
        services.config_package_service.export_package(
            config_id=config_id,
            target_path=str(tmp_path / "bad.dy"),
            options=ConfigPackageExportOptions(
                include_ai_toolchain=True,
                include_ai_package=True,
            ),
        )


def test_inspect_package_rejects_non_zip(tmp_path: Path, services: SimpleNamespace):
    path = tmp_path / "not-a-zip.dy"
    path.write_text("nope", encoding="utf-8")

    with pytest.raises(ValueError, match="ZIP"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_missing_manifest(tmp_path: Path, services: SimpleNamespace):
    path = _write_package(tmp_path / "missing-manifest.dy", {"config/config.json": {"config": _config_payload(1)}})

    with pytest.raises(ValueError, match="manifest.json"):
        services.config_package_service.inspect_package(str(path))


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ({"C:/evil.txt": b"x"}, "drive"),
        ({"C:evil.txt": b"x"}, "drive"),
        ({"dir/C:evil.txt": b"x"}, "drive"),
        ({"/evil.txt": b"x"}, "absolute"),
        ({"../evil.txt": b"x"}, "traversal"),
    ],
)
def test_inspect_package_rejects_unsafe_entry_names(
    tmp_path: Path,
    services: SimpleNamespace,
    entries: dict[str, object],
    match: str,
):
    base = _package_entries()
    base.update(entries)
    path = _write_package(tmp_path / f"{match}.dy", base)

    with pytest.raises(ValueError, match=match):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_duplicate_normalized_entry_names(tmp_path: Path, services: SimpleNamespace):
    path = tmp_path / "duplicate.dy"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_package_entries()["manifest.json"]))
        zf.writestr("config/config.json", json.dumps(_package_entries()["config/config.json"]))
        zf.writestr("dup.bin", b"a")
        zf.writestr("dup.bin", b"b")

    with pytest.raises(ValueError, match="duplicate"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_case_insensitive_collisions_on_windows(tmp_path: Path, services: SimpleNamespace):
    if os.name != "nt":
        pytest.skip("case-insensitive collision validation is Windows-only")

    path = _write_package(
        tmp_path / "casefold.dy",
        {
            **_package_entries(),
            "AI/Package/model.bin": b"a",
            "ai/package/model.bin": b"b",
        },
    )

    with pytest.raises(ValueError, match="case-insensitive"):
        services.config_package_service.inspect_package(str(path))


@pytest.mark.parametrize(
    ("manifest_updates", "entry_updates", "match"),
    [
        ({"format": "wrong"}, {}, "format"),
        ({"format_version": 2}, {}, "format_version"),
        ({"contents": {"config": False, "golden_samples": False, "ai_toolchain": False, "ai_package": False}}, {}, "contents.config"),
        ({}, {"config/config.json": None}, "config/config.json"),
        ({}, {"config/extra.json": {}}, "single-config"),
        ({"contents": {"config": True, "golden_samples": True, "ai_toolchain": False, "ai_package": False}}, {}, "golden"),
        ({"contents": {"config": True, "golden_samples": False, "ai_toolchain": True, "ai_package": False}}, {}, "ai/toolchain.json"),
        ({"contents": {"config": True, "golden_samples": False, "ai_toolchain": False, "ai_package": True}}, {}, "ai_package"),
        (
            {"contents": {"config": True, "golden_samples": False, "ai_toolchain": True, "ai_package": True}},
            {"ai/toolchain.json": {"toolchain_id": "motor-ai", "display_name": "Motor AI", "version": "1.0", "entrypoint": "python main.py", "protocol_version": 1, "execution_mode": "resident_process", "default_timeout_ms": 30000, "enabled": True, "description": "portable", "params_json": "{}", "source_package_dir": "D:/source-machine"}},
            "package_archive_path",
        ),
    ],
)
def test_inspect_package_rejects_invalid_manifest_layout(
    tmp_path: Path,
    services: SimpleNamespace,
    manifest_updates: dict,
    entry_updates: dict[str, object | None],
    match: str,
):
    if match == "package_archive_path":
        entries = _package_entries(include_ai_metadata=True, include_ai_package=False)
    else:
        entries = _package_entries()
    entries["manifest.json"] = {
        **entries["manifest.json"],
        **manifest_updates,
    }
    for name, payload in entry_updates.items():
        if payload is None:
            entries.pop(name, None)
        else:
            entries[name] = payload
    path = _write_package(tmp_path / f"{match}.dy", entries)

    with pytest.raises(ValueError, match=match):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_symlink_like_entries(tmp_path: Path, services: SimpleNamespace):
    info = zipfile.ZipInfo("ai/package/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = tmp_path / "symlink.dy"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_package_entries()["manifest.json"]))
        zf.writestr("config/config.json", json.dumps(_package_entries()["config/config.json"]))
        zf.writestr(info, b"target")

    with pytest.raises(ValueError, match="symlink|special"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_import_size_limit(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _write_package(
        tmp_path / "too-large.dy",
        {
            **_package_entries(),
            "payload.bin": b"0123456789",
        },
    )
    monkeypatch.setattr(config_package_module, "MAX_UNCOMPRESSED_BYTES", 20)

    with pytest.raises(ValueError, match="uncompressed"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_import_file_count_limit(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _write_package(
        tmp_path / "too-many.dy",
        {
            **_package_entries(),
            "payload-a.bin": b"a",
            "payload-b.bin": b"b",
        },
    )
    monkeypatch.setattr(config_package_module, "MAX_FILE_COUNT", 3)

    with pytest.raises(ValueError, match="file count"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_exact_package_archive_path_file(tmp_path: Path, services: SimpleNamespace):
    entries = _package_entries(include_ai_metadata=True, include_ai_package=True)
    entries.pop("ai/package/main.py")
    entries["ai/package"] = b"not-a-child-file"
    path = _write_package(
        tmp_path / "exact-package-path.dy",
        entries,
    )

    with pytest.raises(ValueError, match="package_archive_path"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_mixed_exact_package_archive_path_file_and_children(
    tmp_path: Path,
    services: SimpleNamespace,
):
    entries = _package_entries(include_ai_metadata=True, include_ai_package=True)
    entries["ai/package"] = b"not-a-child-file"
    path = _write_package(
        tmp_path / "mixed-exact-package-path.dy",
        entries,
    )

    with pytest.raises(ValueError, match="package_archive_path"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_rejects_undeclared_ai_package_payload(tmp_path: Path, services: SimpleNamespace):
    path = _write_package(
        tmp_path / "undeclared-ai-package.dy",
        {
            **_package_entries(include_ai_metadata=True, include_ai_package=False),
            "ai/package/model.bin": b"\x00\x01",
        },
    )

    with pytest.raises(ValueError, match="ai_package"):
        services.config_package_service.inspect_package(str(path))


def test_inspect_package_reports_metadata_only_ai_conflict_defaults_to_existing_toolchain(
    tmp_path: Path,
    services: SimpleNamespace,
):
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    services.ai_service.save_toolchain(None, _toolchain_payload(local_package_dir))
    path = _write_package(
        tmp_path / "metadata-only.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=False),
    )

    preview = services.config_package_service.inspect_package(str(path))

    assert preview.config_name == "Imported"
    assert preview.ai_toolchain_id == "motor-ai"
    assert preview.ai_conflict is True
    assert preview.ai_action_options == (
        "use_existing_toolchain",
        "clear_binding",
        "cancel",
    )
    assert preview.default_ai_action == "use_existing_toolchain"


def test_inspect_package_warns_when_metadata_only_ai_has_no_local_match(
    tmp_path: Path,
    services: SimpleNamespace,
):
    path = _write_package(
        tmp_path / "metadata-only.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=False),
    )

    preview = services.config_package_service.inspect_package(str(path))

    assert preview.ai_toolchain_id == "motor-ai"
    assert preview.ai_conflict is False
    assert preview.ai_action_options == ("clear_binding",)
    assert preview.default_ai_action == "clear_binding"
    assert any("runnable AI package" in warning for warning in preview.warnings)


def test_inspect_package_reports_config_and_runnable_ai_conflicts(
    tmp_path: Path,
    services: SimpleNamespace,
):
    _create_saved_config(services, name="Imported")
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    services.ai_service.save_toolchain(None, _toolchain_payload(local_package_dir))
    path = _write_package(
        tmp_path / "runnable.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            toolchain_overrides={"params_json": '{ "imported": true }'},
        ),
    )

    preview = services.config_package_service.inspect_package(str(path))

    assert preview.config_conflict is True
    assert "overwrite" in preview.config_action_options
    assert "use_existing_config" in preview.config_action_options
    assert "cancel" in preview.config_action_options
    assert preview.ai_conflict is True
    assert preview.ai_action_options == (
        "overwrite",
        "use_existing_toolchain",
        "cancel",
    )
    assert preview.default_ai_action in preview.ai_action_options


def test_inspect_package_detects_conflict_with_legacy_schema_missing_monitor_columns(
    tmp_path: Path,
    services: SimpleNamespace,
):
    _recreate_legacy_config_schema_without_monitor_columns()
    ConfigTable._meta.database.execute_sql(
        """
        INSERT INTO test_config_table (
            config_id,
            config_name,
            record_duration,
            analysis_duration,
            scan_triggers_test,
            selected_channels
        )
        VALUES (1, 'Imported', 5.0, 3.0, 0, '[0]')
        """
    )
    path = _write_package(
        tmp_path / "legacy-conflict.dy",
        _package_entries(config_payload=_config_payload(999, config_name="Imported")),
    )

    preview = services.config_package_service.inspect_package(str(path))

    assert preview.config_conflict is True
    assert preview.config_action_options == ("overwrite", "use_existing_config", "cancel")
    assert preview.default_config_action == "use_existing_config"


def _managed_toolchain_dir_name(toolchain_id: str) -> str:
    digest = hashlib.sha256(toolchain_id.encode("utf-8")).hexdigest()[:16]
    return f"{toolchain_id}_{digest}"


def _write_toolchain_marker(path: Path, toolchain_id: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / _TOOLCHAIN_MARKER_NAME).write_text(toolchain_id, encoding="utf-8")


def test_import_package_creates_new_config_by_default_and_restores_golden_samples(
    tmp_path: Path,
    services: SimpleNamespace,
):
    source_wav = _write_wav(
        tmp_path / "source-machine.wav",
        samples=288_000,
        sample_rate=96_000,
    )
    imported_channel = _channel_payload_with_golden(
        0,
        **_golden_binding(
            source_wav,
            num_samples=288_000,
            sample_rate=96_000.0,
        ),
        fft_golden_mode="absolute",
        fft_show_golden_overlay=True,
        oct_golden_mode="absolute",
        oct_show_golden_overlay=True,
    )
    imported_config = _config_payload(999, config_name="Imported")
    imported_config.update(
        {
            "plc_id": 123,
            "input_device_name": "Imported Mic",
            "input_device_hostapi": "Imported API",
            "input_device_samplerate": 96_000.0,
            "output_device_name": "Imported Speaker",
            "output_device_hostapi": "Imported Output API",
            "output_device_samplerate": 44_100.0,
            "channels": [imported_channel],
            "selected_channels": [0],
        }
    )
    path = _write_package(
        tmp_path / "import.dy",
        _package_entries(
            config_payload=imported_config,
            golden_assets={0: source_wav.read_bytes()},
        ),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(),
    )

    assert result.status == "created"
    assert result.config_id not in (None, 999)
    assert result.ai_binding_status == "no_ai_metadata"

    loaded = services.config_service.load_config(result.config_id)
    channel = loaded["channels"][0]

    assert loaded["plc_id"] is None
    assert loaded["input_device_name"] == "Imported Mic"
    assert loaded["input_device_hostapi"] == "Imported API"
    assert loaded["input_device_samplerate"] == 96_000.0
    assert loaded["output_device_name"] == "Imported Speaker"
    assert loaded["output_device_hostapi"] == "Imported Output API"
    assert loaded["output_device_samplerate"] == 44_100.0
    assert channel["golden_sample_path"] != str(source_wav)
    assert Path(channel["golden_sample_path"]).exists()
    assert services.config_service.golden_sample_service.is_managed_path(
        channel["golden_sample_path"]
    )


def test_import_package_create_supports_accepted_legacy_schema_without_current_meter_columns(
    tmp_path: Path,
    services: SimpleNamespace,
):
    _recreate_accepted_legacy_config_schema_without_current_meter_columns()
    imported_config = _config_payload(
        999,
        config_name="Imported Legacy",
        vibration_view_channel_index=0,
    )
    path = _write_package(
        tmp_path / "legacy-without-current-meter.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="create"),
    )

    loaded = services.config_service.load_config(result.config_id)
    row = ConfigTable._meta.database.execute_sql(
        """
        SELECT created_time, updated_time
        FROM test_config_table
        WHERE config_id = ?
        """,
        (result.config_id,),
    ).fetchone()

    assert "vibration_view_channel_index" in _config_table_columns()
    assert "current_meter_enabled" not in _config_table_columns()
    assert result.status == "created"
    assert result.config_id == 1
    assert loaded["config_id"] == result.config_id
    assert loaded["config_name"] == "Imported Legacy"
    assert loaded["vibration_view_channel_index"] == 0
    assert loaded["current_meter"] == normalize_current_meter_config(None)
    assert len(loaded["channels"]) == 1
    assert loaded["channels"][0]["channel_index"] == 0
    assert row[0] not in (None, "legacy-created")
    assert row[1] not in (None, "legacy-updated")


def test_import_package_config_conflict_use_existing_skips_entire_package_import(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    existing_id = _create_saved_config(services, name="Imported")
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    (local_package_dir / "main.py").write_text("print('local')\n", encoding="utf-8")
    services.ai_service.save_toolchain(None, _toolchain_payload(local_package_dir))
    source_wav = _write_wav(tmp_path / "source-machine.wav")
    imported_config = _config_payload(999, config_name="Imported")
    imported_config["channels"] = [
        _channel_payload_with_golden(0, **_golden_binding(source_wav))
    ]
    imported_config["selected_channels"] = [0]
    path = _write_package(
        tmp_path / "conflict.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            config_payload=imported_config,
            golden_assets={0: source_wav.read_bytes()},
        ),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(
            config_action="use_existing_config",
            ai_action="overwrite",
        ),
    )

    assert result.status == "kept_existing"
    assert result.config_id == existing_id
    assert result.ai_binding_status == "unchanged"
    assert services.config_service.list_configs() == [
        {"config_id": existing_id, "config_name": "Imported"}
    ]
    assert services.ai_service.get_toolchain("motor-ai").package_dir == str(local_package_dir)
    assert not (tmp_path / "data" / "ai_toolchains").exists()


def test_import_package_overwrites_existing_config_and_keeps_local_id(
    tmp_path: Path,
    services: SimpleNamespace,
):
    config_id = _create_saved_config(services, name="Imported")
    imported_config = _config_payload(999, config_name="Imported")
    imported_config.update(
        {
            "plc_id": 999,
            "input_device_name": "Source Mic",
            "input_device_hostapi": "Source API",
            "input_device_samplerate": 44_100.0,
            "output_device_name": "Source Speaker",
            "output_device_hostapi": "Source Out API",
            "output_device_samplerate": 48_000.0,
        }
    )
    path = _write_package(
        tmp_path / "overwrite.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="overwrite"),
    )

    loaded = services.config_service.load_config(config_id)

    assert result.status == "overwritten"
    assert result.config_id == config_id
    assert loaded["config_id"] == config_id
    assert loaded["plc_id"] is None
    assert loaded["input_device_name"] == "Source Mic"
    assert loaded["input_device_hostapi"] == "Source API"
    assert loaded["input_device_samplerate"] == 44_100.0
    assert loaded["output_device_name"] == "Source Speaker"
    assert loaded["output_device_hostapi"] == "Source Out API"
    assert loaded["output_device_samplerate"] == 48_000.0


def test_import_package_round_trips_limit_switch_fields(
    tmp_path: Path,
    services: SimpleNamespace,
):
    imported_config = _config_payload(999, config_name="Imported")
    imported_config["channels"][0].update(
        {
            "spl_lower_enabled": True,
            "spl_lower_limit": 45.0,
            "spl_upper_enabled": False,
            "spl_average_upper_enabled": True,
            "spl_average_upper_limit": 78.0,
            "loudness_lower_enabled": True,
            "loudness_lower_limit": 2.5,
            "loudness_average_upper_enabled": True,
            "loudness_average_upper_limit": 9.5,
        }
    )
    path = _write_package(
        tmp_path / "limits.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="create"),
    )

    channel = services.config_service.load_config(result.config_id)["channels"][0]
    assert channel["spl_lower_enabled"] is True
    assert channel["spl_lower_limit"] == 45.0
    assert channel["spl_upper_enabled"] is False
    assert channel["spl_average_upper_enabled"] is True
    assert channel["spl_average_upper_limit"] == 78.0
    assert channel["loudness_lower_enabled"] is True
    assert channel["loudness_lower_limit"] == 2.5
    assert channel["loudness_average_upper_enabled"] is True
    assert channel["loudness_average_upper_limit"] == 9.5


def test_import_package_round_trips_stft_color_scale_fields(
    tmp_path: Path,
    services: SimpleNamespace,
):
    imported_config = _config_payload(999, config_name="Imported")
    imported_config["channels"][0].update(
        {
            "stft_color_scale_enabled": True,
            "stft_color_scale_min_db": -88.0,
            "stft_color_scale_max_db": -8.0,
        }
    )
    path = _write_package(
        tmp_path / "stft-color-scale-import.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="create"),
    )

    channel = services.config_service.load_config(result.config_id)["channels"][0]
    assert channel["stft_color_scale_enabled"] is True
    assert channel["stft_color_scale_min_db"] == pytest.approx(-88.0)
    assert channel["stft_color_scale_max_db"] == pytest.approx(-8.0)


def test_import_package_round_trips_stft_score_fields(
    tmp_path: Path,
    services: SimpleNamespace,
):
    imported_config = _config_payload(999, config_name="Imported")
    imported_config["channels"][0].update(_STFT_SCORE_CUSTOM)
    path = _write_package(
        tmp_path / "stft-score-import.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="create"),
    )

    channel = services.config_service.load_config(result.config_id)["channels"][0]
    for field, expected in _STFT_SCORE_CUSTOM.items():
        assert channel[field] == expected


def test_import_package_round_trips_monitor_fields(
    tmp_path: Path,
    services: SimpleNamespace,
):
    imported_config = _config_payload(
        999,
        config_name="Imported",
        monitor_channel_index=2,
        monitor_gain_db=6.5,
    )
    path = _write_package(
        tmp_path / "monitor.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="create"),
    )

    loaded = services.config_service.load_config(result.config_id)
    assert loaded["monitor_channel_index"] == 2
    assert loaded["monitor_gain_db"] == 6.5


def test_import_package_round_trips_vibration_view_channel(
    tmp_path: Path,
    services: SimpleNamespace,
):
    imported_config = _config_payload(
        999,
        config_name="Imported",
        vibration_view_channel_index=0,
    )
    path = _write_package(
        tmp_path / "vibration.dy",
        _package_entries(config_payload=imported_config),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="create"),
    )

    loaded = services.config_service.load_config(result.config_id)
    assert loaded["vibration_view_channel_index"] == 0


def test_import_package_overwrite_rollback_preserves_old_golden_binding(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    existing_id = _create_saved_config(services, name="Imported")
    old_source = _write_wav(tmp_path / "existing-source.wav")
    old_binding = services.config_service.golden_sample_service.restore_imported_sample(
        config_id=existing_id,
        channel_index=0,
        source_path=str(old_source),
        metadata=_golden_binding(old_source),
    )
    payload = _config_payload(existing_id, config_name="Imported")
    payload["channels"][0].update(old_binding)
    services.config_service.save_config(payload)

    new_source = _write_wav(tmp_path / "new-source.wav")
    imported_config = _config_payload(999, config_name="Imported")
    imported_config["channels"] = [
        _channel_payload_with_golden(0, **_golden_binding(new_source))
    ]
    imported_config["selected_channels"] = [0]
    path = _write_package(
        tmp_path / "rollback.dy",
        _package_entries(
            config_payload=imported_config,
            golden_assets={0: new_source.read_bytes()},
        ),
    )

    original_replace = services.config_service._replace_channel_rows

    def _explode(*args, **kwargs):
        original_replace(*args, **kwargs)
        raise RuntimeError("db exploded")

    monkeypatch.setattr(services.config_service, "_replace_channel_rows", _explode)

    with pytest.raises(RuntimeError, match="db exploded"):
        services.config_package_service.import_package(
            str(path),
            ConfigPackageImportDecision(config_action="overwrite"),
        )

    loaded = services.config_service.load_config(existing_id)
    assert loaded["channels"][0]["golden_sample_path"] == old_binding["golden_sample_path"]
    assert Path(old_binding["golden_sample_path"]).exists()
    assert sorted((tmp_path / "managed").rglob("*.wav")) == [
        Path(old_binding["golden_sample_path"])
    ]


def test_import_package_cancel_aborts_without_mutation(
    tmp_path: Path,
    services: SimpleNamespace,
):
    existing_id = _create_saved_config(services, name="Imported")
    path = _write_package(
        tmp_path / "cancel.dy",
        _package_entries(config_payload=_config_payload(999, config_name="Imported")),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(config_action="cancel"),
    )

    assert result.status == "cancelled"
    assert result.config_id is None
    assert result.ai_binding_status == "cancelled"
    assert services.config_service.list_configs() == [
        {"config_id": existing_id, "config_name": "Imported"}
    ]


def test_import_package_metadata_only_without_local_toolchain_clears_binding_and_warns(
    tmp_path: Path,
    services: SimpleNamespace,
):
    path = _write_package(
        tmp_path / "metadata-only.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=False),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(ai_action="clear_binding"),
    )

    binding = services.ai_service.resolve_binding(result.config_id)

    assert result.status == "created"
    assert result.ai_binding_status == "cleared_binding"
    assert binding["enabled"] is False
    assert binding["toolchain_id"] is None
    assert services.ai_service.list_toolchains() == []
    assert any("runnable AI package" in warning for warning in result.warnings)


def test_import_package_metadata_only_matching_local_toolchain_uses_existing_by_default(
    tmp_path: Path,
    services: SimpleNamespace,
):
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    toolchain_id = services.ai_service.save_toolchain(
        None,
        _toolchain_payload(local_package_dir),
    )
    original_toolchain = services.ai_service.get_toolchain(toolchain_id)
    path = _write_package(
        tmp_path / "metadata-default.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=False),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(),
    )

    rebound = services.ai_service.resolve_binding(result.config_id)
    toolchain = services.ai_service.get_toolchain(toolchain_id)

    assert result.ai_binding_status == "used_existing_toolchain"
    assert rebound["toolchain_id"] == toolchain_id
    assert toolchain.package_dir == str(local_package_dir)
    assert toolchain.updated_time == original_toolchain.updated_time


def test_import_package_runnable_ai_create_restores_managed_package_dir(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    path = _write_package(
        tmp_path / "runnable.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            toolchain_overrides={"params_json": '{ "imported": true }'},
        ),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(),
    )

    binding = services.ai_service.resolve_binding(result.config_id)
    toolchain = services.ai_service.get_toolchain("motor-ai")

    assert result.ai_binding_status == "created_toolchain"
    assert binding["toolchain_id"] == "motor-ai"
    assert binding["params_json"] == '{"imported":true}'
    assert toolchain is not None
    assert toolchain.params_json == '{"imported":true}'
    assert Path(toolchain.package_dir).is_dir()
    assert toolchain.package_dir.startswith(str(tmp_path / "data" / "ai_toolchains"))
    assert (Path(toolchain.package_dir) / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert (Path(toolchain.package_dir) / _TOOLCHAIN_MARKER_NAME).read_text(encoding="utf-8") == "motor-ai"


def test_import_package_runnable_ai_use_existing_toolchain_keeps_local_metadata_and_package(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    (local_package_dir / "main.py").write_text("print('local')\n", encoding="utf-8")
    services.ai_service.save_toolchain(
        None,
        _toolchain_payload(
            local_package_dir,
            version="9.9",
            display_name="Local AI",
        ),
    )
    path = _write_package(
        tmp_path / "runnable-use-existing.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=True),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(ai_action="use_existing_toolchain"),
    )

    toolchain = services.ai_service.get_toolchain("motor-ai")

    assert result.ai_binding_status == "used_existing_toolchain"
    assert toolchain.version == "9.9"
    assert toolchain.display_name == "Local AI"
    assert toolchain.package_dir == str(local_package_dir)
    assert (local_package_dir / "main.py").read_text(encoding="utf-8") == "print('local')\n"


def test_import_package_runnable_ai_overwrite_external_package_dir_keeps_external_dir(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    external_dir = tmp_path / "external-tool"
    external_dir.mkdir()
    (external_dir / "main.py").write_text("print('external')\n", encoding="utf-8")
    services.ai_service.save_toolchain(
        None,
        _toolchain_payload(external_dir, version="0.9"),
    )
    path = _write_package(
        tmp_path / "runnable-overwrite.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            toolchain_overrides={"version": "2.0"},
        ),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(ai_action="overwrite"),
    )

    toolchain = services.ai_service.get_toolchain("motor-ai")

    assert result.ai_binding_status == "overwrote_toolchain"
    assert toolchain.version == "2.0"
    assert toolchain.package_dir != str(external_dir)
    assert toolchain.package_dir.startswith(str(tmp_path / "data" / "ai_toolchains"))
    assert external_dir.exists()
    assert (external_dir / "main.py").read_text(encoding="utf-8") == "print('external')\n"
    assert (Path(toolchain.package_dir) / "main.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_import_package_overwrite_normalizes_whitespace_padded_toolchain_id_conflict(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    (local_package_dir / "main.py").write_text("print('local')\n", encoding="utf-8")
    services.ai_service.save_toolchain(
        None,
        _toolchain_payload(local_package_dir, version="0.9"),
    )
    invalidations: list[str] = []
    monkeypatch.setattr(
        services.ai_service._resident_runtime,
        "invalidate",
        lambda toolchain_id: invalidations.append(toolchain_id),
    )
    path = _write_package(
        tmp_path / "trimmed-overwrite.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            toolchain_overrides={
                "toolchain_id": "  motor-ai  ",
                "version": "2.0",
            },
        ),
    )

    preview = services.config_package_service.inspect_package(str(path))

    assert preview.ai_toolchain_id == "motor-ai"
    assert preview.ai_conflict is True
    assert preview.ai_action_options == (
        "overwrite",
        "use_existing_toolchain",
        "cancel",
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(ai_action="overwrite"),
    )

    binding = services.ai_service.resolve_binding(result.config_id)
    toolchain = services.ai_service.get_toolchain("motor-ai")

    assert result.ai_binding_status == "overwrote_toolchain"
    assert binding["toolchain_id"] == "motor-ai"
    assert toolchain.version == "2.0"
    assert Path(toolchain.package_dir).name == _managed_toolchain_dir_name("motor-ai")
    assert invalidations == ["motor-ai"]


def test_import_package_overwrite_managed_ai_package_restores_backup_on_db_failure(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    managed_dir = tmp_path / "data" / "ai_toolchains" / _managed_toolchain_dir_name("motor-ai")
    _write_toolchain_marker(managed_dir, "motor-ai")
    (managed_dir / "main.py").write_text("print('old')\n", encoding="utf-8")
    services.ai_service.save_toolchain(
        None,
        _toolchain_payload(managed_dir, version="0.9"),
    )
    path = _write_package(
        tmp_path / "ai-rollback.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            toolchain_overrides={"version": "2.0"},
        ),
    )

    original_replace = services.config_service._replace_channel_rows

    def _explode(*args, **kwargs):
        original_replace(*args, **kwargs)
        raise RuntimeError("db exploded")

    monkeypatch.setattr(services.config_service, "_replace_channel_rows", _explode)

    with pytest.raises(RuntimeError, match="db exploded"):
        services.config_package_service.import_package(
            str(path),
            ConfigPackageImportDecision(ai_action="overwrite"),
        )

    toolchain = services.ai_service.get_toolchain("motor-ai")
    assert toolchain.package_dir == str(managed_dir)
    assert (managed_dir / "main.py").read_text(encoding="utf-8") == "print('old')\n"


def test_import_package_overwrite_busy_resident_toolchain_holds_mutation_lease(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    managed_dir = tmp_path / "data" / "ai_toolchains" / _managed_toolchain_dir_name("motor-ai")
    _write_toolchain_marker(managed_dir, "motor-ai")
    (managed_dir / "main.py").write_text("print('old')\n", encoding="utf-8")
    services.ai_service.save_toolchain(
        None,
        _toolchain_payload(managed_dir, version="0.9"),
    )

    @contextmanager
    def _raise_busy(toolchain_id: str):
        assert toolchain_id == "motor-ai"
        raise ValueError("Resident toolchain is busy")
        yield

    invalidations: list[str] = []
    monkeypatch.setattr(
        services.ai_service._resident_runtime,
        "hold_mutation_lease",
        _raise_busy,
    )
    monkeypatch.setattr(
        services.ai_service._resident_runtime,
        "invalidate",
        lambda toolchain_id: invalidations.append(toolchain_id),
    )
    path = _write_package(
        tmp_path / "busy-resident.dy",
        _package_entries(
            include_ai_metadata=True,
            include_ai_package=True,
            toolchain_overrides={"version": "2.0"},
        ),
    )

    with pytest.raises(ValueError, match="busy"):
        services.config_package_service.import_package(
            str(path),
            ConfigPackageImportDecision(ai_action="overwrite"),
        )

    toolchain = services.ai_service.get_toolchain("motor-ai")
    assert toolchain.version == "0.9"
    assert toolchain.package_dir == str(managed_dir)
    assert (managed_dir / "main.py").read_text(encoding="utf-8") == "print('old')\n"
    assert services.config_service.list_configs() == []
    assert invalidations == []


def test_import_package_reuses_same_marker_managed_ai_directory(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    managed_dir = tmp_path / "data" / "ai_toolchains" / _managed_toolchain_dir_name("motor-ai")
    _write_toolchain_marker(managed_dir, "motor-ai")
    (managed_dir / "stale.txt").write_text("stale", encoding="utf-8")
    path = _write_package(
        tmp_path / "reuse-marker.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=True),
    )

    services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(),
    )

    toolchain = services.ai_service.get_toolchain("motor-ai")
    assert toolchain.package_dir == str(managed_dir)
    assert (managed_dir / "main.py").exists()
    assert not (managed_dir / "stale.txt").exists()


def test_import_package_uses_suffix_when_managed_ai_directory_marker_collides(
    tmp_path: Path,
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_package_module, "base_dir", str(tmp_path))
    managed_root = tmp_path / "data" / "ai_toolchains"
    base_name = _managed_toolchain_dir_name("motor-ai")
    _write_toolchain_marker(managed_root / base_name, "other-a")
    _write_toolchain_marker(managed_root / f"{base_name}_2", "other-b")
    path = _write_package(
        tmp_path / "suffix-collision.dy",
        _package_entries(include_ai_metadata=True, include_ai_package=True),
    )

    services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(),
    )

    toolchain = services.ai_service.get_toolchain("motor-ai")
    assert Path(toolchain.package_dir).name == f"{base_name}_3"


def test_import_package_without_ai_metadata_clears_binding_without_mutating_local_toolchains(
    tmp_path: Path,
    services: SimpleNamespace,
):
    local_package_dir = tmp_path / "local-tool"
    local_package_dir.mkdir()
    services.ai_service.save_toolchain(None, _toolchain_payload(local_package_dir))
    original = services.ai_service.get_toolchain("motor-ai")
    path = _write_package(
        tmp_path / "no-ai-metadata.dy",
        _package_entries(
            include_ai_metadata=False,
            include_ai_package=False,
            ai_binding={
                "enabled": True,
                "toolchain_id": "motor-ai",
                "timeout_ms": None,
            },
        ),
    )

    result = services.config_package_service.import_package(
        str(path),
        ConfigPackageImportDecision(),
    )

    binding = services.ai_service.resolve_binding(result.config_id)
    toolchain = services.ai_service.get_toolchain("motor-ai")

    assert result.ai_binding_status == "no_ai_metadata"
    assert binding["enabled"] is False
    assert binding["toolchain_id"] is None
    assert toolchain.package_dir == str(local_package_dir)
    assert toolchain.updated_time == original.updated_time
