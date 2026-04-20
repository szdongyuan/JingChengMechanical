from __future__ import annotations

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
    ConfigPackageService,
)
from app.service.config_service import ConfigService
from app.service.golden_sample_service import GoldenSampleService


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
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "loudness_unit": "sone",
        "loudness_lower_limit": 0.0,
        "loudness_upper_limit": 60.0,
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


def _config_payload(config_id: int, *, config_name: str = "Cfg1") -> dict:
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
        "channels": [_channel_payload(0)],
    }


def _write_wav(path: Path, *, samples: int = 144_000, sample_rate: int = 48_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.linspace(-0.5, 0.5, num=samples, dtype=np.float32)
    wavfile.write(path, sample_rate, data)
    return path


def _golden_binding(source_path: Path) -> dict:
    return {
        "label": source_path.name,
        "source_channel": 0,
        "start_sample": 0,
        "num_samples": 144_000,
        "start_ms": 0,
        "duration_ms": 3000,
        "sample_rate": 48_000.0,
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
            "params_json": "{}",
        },
    )
    return toolchain_id


def _package_entries(*, include_ai_metadata: bool = False, include_ai_package: bool = False) -> dict[str, object]:
    config_payload = {
        "config": _config_payload(999, config_name="Imported"),
        "ai_binding": {
            "enabled": include_ai_metadata,
            "toolchain_id": "motor-ai" if include_ai_metadata else None,
            "timeout_ms": None,
            "params_json": "{}",
        },
        "golden_sample_assets": {},
    }
    manifest = {
        "format": "jingcheng-config-package",
        "format_version": 1,
        "app": "JingChengMechanical",
        "exported_at": "2026-04-20T12:00:00Z",
        "contents": {
            "config": True,
            "golden_samples": False,
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
            "source_package_dir": "D:/source-machine/motor-ai",
            **({"package_archive_path": "ai/package"} if include_ai_package else {}),
        }
    if include_ai_package:
        entries["ai/package/main.py"] = b"print('ok')\n"
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
    assert payload["ai_binding"] == {
        "enabled": False,
        "toolchain_id": None,
        "timeout_ms": None,
        "params_json": "{}",
    }


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
    _bind_toolchain(services, config_id=config_id, package_dir=package_dir)
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
        "source_package_dir",
    }
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
            {"ai/toolchain.json": {"toolchain_id": "motor-ai", "display_name": "Motor AI", "version": "1.0", "entrypoint": "python main.py", "protocol_version": 1, "execution_mode": "resident_process", "default_timeout_ms": 30000, "enabled": True, "description": "portable", "source_package_dir": "D:/source-machine"}},
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
        _package_entries(include_ai_metadata=True, include_ai_package=True),
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
