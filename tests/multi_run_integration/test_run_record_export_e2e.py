from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock
import wave
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

import app.services_container as services_module
from app.service.run_persistence.contracts import CurrentTrace
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.run_repository import RunRepository
from tests.run_persistence.test_export_reader import (
    _finish,
    _insert_active,
    _insert_audio,
    _ready_runtime,
)
from ui.modules.data_management import DataManagementModule


@pytest.fixture
def prepared_database_path(tmp_path):
    from app.service.run_persistence.prepare_database import (
        prepare_run_persistence_database,
    )

    return prepare_run_persistence_database(
        tmp_path / "prepared-run-persistence.sqlite3"
    )


def _write_pcm16(
    path: Path,
    channels: tuple[tuple[int, ...], ...],
) -> None:
    interleaved = bytearray()
    for frame_index in range(len(channels[0])):
        for channel in channels:
            interleaved.extend(
                channel[frame_index].to_bytes(2, "little", signed=True)
            )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(len(channels))
        writer.setsampwidth(2)
        writer.setframerate(8_000)
        writer.writeframes(bytes(interleaved))


def _read_mono_samples(payload: bytes) -> tuple[int, ...]:
    from io import BytesIO

    with wave.open(BytesIO(payload), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 8_000
        raw = reader.readframes(reader.getnframes())
    return tuple(
        int.from_bytes(raw[offset : offset + 2], "little", signed=True)
        for offset in range(0, len(raw), 2)
    )


def test_data_management_exports_selected_terminal_runs_through_service_container(
    monkeypatch,
    prepared_database_path,
    tmp_path,
):
    runtime: RunPersistenceDatabase = _ready_runtime(prepared_database_path)
    first_channels = ((100, -200, 300), (-1, 2, -3))
    second_channels = ((7, 8, 9),)
    first_source = tmp_path / "first-source.wav"
    second_source = tmp_path / "second-source.wav"
    _write_pcm16(first_source, first_channels)
    _write_pcm16(second_source, second_channels)

    first_id = _insert_active(
        runtime,
        channel_order=(4, 9),
        hardware_id=23,
    )
    assert RunRepository(runtime).attach_current_trace(
        first_id,
        CurrentTrace(samples=((0.0, 1.25), (0.5, 1.75))),
    ).value
    _finish(runtime, first_id)
    _insert_audio(
        runtime,
        first_id,
        first_source,
        channel_count=2,
        frame_count=3,
    )

    second_id = _insert_active(
        runtime,
        channel_order=(6,),
        hardware_id=41,
    )
    _finish(runtime, second_id)
    _insert_audio(
        runtime,
        second_id,
        second_source,
        channel_count=1,
        frame_count=3,
    )

    monkeypatch.setattr(
        services_module.DbConnectionService,
        "connect",
        lambda self: None,
    )
    monkeypatch.setattr(
        services_module,
        "HardwareSelectService",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        services_module,
        "InputCalibrationService",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        services_module,
        "ConfigService",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        services_module,
        "AiService",
        MagicMock(return_value=MagicMock()),
    )
    legacy_export = MagicMock(
        side_effect=AssertionError(
            "legacy SQLite-plus-WAV export must be unreachable"
        )
    )
    monkeypatch.setattr(
        services_module,
        "AudioDataService",
        MagicMock(
            return_value=MagicMock(export_zip=legacy_export),
        ),
    )

    services = services_module.ServiceContainer(
        run_persistence_database=runtime,
    )
    app = QApplication.instance() or QApplication([])
    module = DataManagementModule(services)
    rows_by_id = {
        module.table.item(row_index, 1).data(
            Qt.ItemDataRole.UserRole
        ): row_index
        for row_index in range(module.table.rowCount())
    }
    assert set(rows_by_id) == {first_id, second_id}
    for run_id in (first_id, second_id):
        module.table.item(rows_by_id[run_id], 0).setCheckState(
            Qt.CheckState.Checked
        )

    chosen = tmp_path / "selected-runs"
    messages: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(chosen), "Zip files (*.zip)"),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: messages.append(("success", args)),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: messages.append(("failure", args)),
    )

    module._export_selected_rows()

    target = chosen.with_suffix(".zip")
    assert target.is_file()
    assert [kind for kind, _args in messages] == ["success"]
    assert str(target) in messages[0][1][2]
    legacy_export.assert_not_called()

    expected_members = {
        f"{first_id}/manifest.json",
        f"{first_id}/channel-001.wav",
        f"{first_id}/channel-002.wav",
        f"{first_id}/current.csv",
        f"{second_id}/manifest.json",
        f"{second_id}/channel-001.wav",
    }
    with ZipFile(target, "r") as archive:
        assert set(archive.namelist()) == expected_members
        first_manifest = json.loads(
            archive.read(f"{first_id}/manifest.json")
        )
        second_manifest = json.loads(
            archive.read(f"{second_id}/manifest.json")
        )
        assert first_manifest["run"]["hardware_id"] == 23
        assert first_manifest["channels"] == [
            {
                "position": 1,
                "source_channel": 4,
                "file": "channel-001.wav",
            },
            {
                "position": 2,
                "source_channel": 9,
                "file": "channel-002.wav",
            },
        ]
        assert second_manifest["run"]["hardware_id"] == 41
        assert second_manifest["channels"] == [
            {
                "position": 1,
                "source_channel": 6,
                "file": "channel-001.wav",
            }
        ]
        assert _read_mono_samples(
            archive.read(f"{first_id}/channel-001.wav")
        ) == first_channels[0]
        assert _read_mono_samples(
            archive.read(f"{first_id}/channel-002.wav")
        ) == first_channels[1]
        assert _read_mono_samples(
            archive.read(f"{second_id}/channel-001.wav")
        ) == second_channels[0]
        assert archive.read(f"{first_id}/current.csv") == (
            b"time_sec,current_a\n0.0,1.25\n0.5,1.75\n"
        )
        assert first_manifest["current"] == {
            "present": True,
            "file": "current.csv",
        }
        assert second_manifest["current"] == {
            "present": False,
            "file": None,
        }
