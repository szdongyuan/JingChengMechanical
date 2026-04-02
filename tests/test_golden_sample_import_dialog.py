from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QLabel, QLineEdit, QSpinBox

from app.service.golden_sample_service import GoldenSampleService
from ui.popups.golden_sample_import_dialog import GoldenSampleImportDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    duration_sec: float = 4.0,
    channels: int = 1,
) -> tuple[Path, np.ndarray]:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = round(sample_rate * duration_sec)
    base = np.linspace(-0.5, 0.5, num=samples, dtype=np.float32)
    if channels <= 1:
        data = base
    else:
        data = np.column_stack([base + float(index) for index in range(channels)]).astype(
            np.float32
        )
    wavfile.write(path, sample_rate, data)
    return path, data


def _dialog(
    tmp_path: Path,
    *,
    wav_path: Path,
    analysis_duration: float | None,
    config_sample_rate,
) -> GoldenSampleImportDialog:
    return GoldenSampleImportDialog(
        source_path=str(wav_path),
        analysis_duration=analysis_duration,
        config_sample_rate=config_sample_rate,
        golden_sample_service=GoldenSampleService(root_dir=str(tmp_path / "managed")),
    )


@pytest.mark.parametrize("analysis_duration", [None, 0.0, -1.0])
def test_dialog_rejects_missing_zero_or_negative_analysis_duration(
    qapp,
    tmp_path,
    analysis_duration,
):
    wav_path, _ = _write_temp_wav(tmp_path / "source.wav")
    dialog = _dialog(
        tmp_path,
        wav_path=wav_path,
        analysis_duration=analysis_duration,
        config_sample_rate=48_000.0,
    )
    button_box = dialog.findChild(QDialogButtonBox, "dialog_buttons")
    status = dialog.findChild(QLabel, "validation_message")

    assert button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled() is False
    assert "分析时长" in status.text()


@pytest.mark.parametrize("config_sample_rate", [None, 0, -48_000.0])
def test_dialog_rejects_missing_or_invalid_config_sample_rate(
    qapp,
    tmp_path,
    config_sample_rate,
):
    wav_path, _ = _write_temp_wav(tmp_path / "source.wav")
    dialog = _dialog(
        tmp_path,
        wav_path=wav_path,
        analysis_duration=3.0,
        config_sample_rate=config_sample_rate,
    )
    button_box = dialog.findChild(QDialogButtonBox, "dialog_buttons")
    status = dialog.findChild(QLabel, "validation_message")

    assert button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled() is False
    assert "采样率" in status.text()


def test_dialog_rejects_file_shorter_than_analysis_duration(qapp, tmp_path):
    wav_path, _ = _write_temp_wav(tmp_path / "short.wav", duration_sec=2.5)
    dialog = _dialog(
        tmp_path,
        wav_path=wav_path,
        analysis_duration=3.0,
        config_sample_rate=48_000.0,
    )
    button_box = dialog.findChild(QDialogButtonBox, "dialog_buttons")
    status = dialog.findChild(QLabel, "validation_message")

    assert button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled() is False
    assert "短于" in status.text()


def test_dialog_rejects_sample_rate_mismatch(qapp, tmp_path):
    wav_path, _ = _write_temp_wav(tmp_path / "source.wav", sample_rate=44_100)
    dialog = _dialog(
        tmp_path,
        wav_path=wav_path,
        analysis_duration=3.0,
        config_sample_rate=48_000.0,
    )
    button_box = dialog.findChild(QDialogButtonBox, "dialog_buttons")
    status = dialog.findChild(QLabel, "validation_message")

    assert button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled() is False
    assert "采样率" in status.text()


def test_dialog_locks_start_to_zero_when_file_matches_required_duration(qapp, tmp_path):
    wav_path, _ = _write_temp_wav(
        tmp_path / "exact.wav",
        duration_sec=3.0,
        channels=2,
    )
    dialog = _dialog(
        tmp_path,
        wav_path=wav_path,
        analysis_duration=3.0,
        config_sample_rate=48_000.0,
    )
    source_channel = dialog.findChild(QComboBox, "source_channel_selector")
    start_ms = dialog.findChild(QSpinBox, "start_ms_spin")
    end_ms = dialog.findChild(QLineEdit, "end_ms_display")
    sample_rate = dialog.findChild(QLabel, "file_sample_rate_value")
    channel_count = dialog.findChild(QLabel, "file_channel_count_value")
    total_duration = dialog.findChild(QLabel, "file_total_duration_value")
    required_duration = dialog.findChild(QLabel, "required_golden_duration_value")

    assert source_channel.count() == 2
    assert start_ms.value() == 0
    assert start_ms.isEnabled() is False
    assert start_ms.maximum() == 0
    assert end_ms.isReadOnly() is True
    assert end_ms.text() == "3000 ms"
    assert "48000" in sample_rate.text()
    assert channel_count.text() == "2"
    assert total_duration.text() == "3000 ms"
    assert required_duration.text() == "3000 ms"


def test_dialog_returns_normalized_multichannel_staged_metadata_on_confirm(qapp, tmp_path):
    wav_path, data = _write_temp_wav(
        tmp_path / "source.wav",
        duration_sec=4.0,
        channels=3,
    )
    dialog = _dialog(
        tmp_path,
        wav_path=wav_path,
        analysis_duration=1.5,
        config_sample_rate=48_000.0,
    )
    source_channel = dialog.findChild(QComboBox, "source_channel_selector")
    start_ms = dialog.findChild(QSpinBox, "start_ms_spin")
    end_ms = dialog.findChild(QLineEdit, "end_ms_display")

    source_channel.setCurrentIndex(2)
    start_ms.setValue(250)
    qapp.processEvents()

    assert end_ms.text() == "1750 ms"

    dialog._on_confirm()
    metadata = dialog.staged_metadata()
    saved_sample_rate, staged_data = wavfile.read(metadata["staged_path"])

    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert metadata["label"] == "source.wav"
    assert metadata["source_channel"] == 2
    assert metadata["start_sample"] == 12_000
    assert metadata["num_samples"] == 72_000
    assert metadata["start_ms"] == 250
    assert metadata["duration_ms"] == 1500
    assert metadata["sample_rate"] == 48_000.0
    assert saved_sample_rate == 48_000
    assert staged_data.ndim == 1
    assert staged_data.shape[0] == 72_000
    np.testing.assert_allclose(
        staged_data[:32],
        data[12_000 : 12_000 + 72_000, 2][:32],
    )


def test_dialog_keeps_open_and_surfaces_error_when_staging_fails(
    qapp,
    tmp_path,
    monkeypatch,
):
    wav_path, _ = _write_temp_wav(
        tmp_path / "source.wav",
        duration_sec=4.0,
        channels=2,
    )

    class _ExplodingGoldenSampleService:
        def create_staged_sample(self, **_kwargs):
            raise OSError("source vanished")

    dialog = GoldenSampleImportDialog(
        source_path=str(wav_path),
        analysis_duration=1.5,
        config_sample_rate=48_000.0,
        golden_sample_service=_ExplodingGoldenSampleService(),
    )
    warnings = []
    monkeypatch.setattr(
        "ui.popups.golden_sample_import_dialog.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    dialog._on_confirm()

    status = dialog.findChild(QLabel, "validation_message")

    assert warnings
    assert "source vanished" in str(warnings[-1][-1])
    assert dialog.result() != int(QDialog.DialogCode.Accepted)
    assert dialog.staged_metadata() is None
    assert "导入失败" in status.text()
