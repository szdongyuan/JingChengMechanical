from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QLabel,
)

from app.service.analysis.imported_audio import ImportedChannelAssignment
from ui.popups.import_audio_analysis_dialog import ImportAudioAnalysisDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_wav(
    path: Path,
    *,
    sample_rate: int = 25_600,
    duration_sec: float = 2.0,
    channels: int = 1,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = round(sample_rate * duration_sec)
    base = np.linspace(-0.25, 0.25, num=samples, dtype=np.float32)
    if channels <= 1:
        data = base
    else:
        data = np.column_stack(
            [base + np.float32(index / 10) for index in range(channels)]
        ).astype(np.float32)
    wavfile.write(path, sample_rate, data)
    return path


def _wav_subformat_guid(format_tag: int) -> bytes:
    return struct.pack(
        "<IHH8s",
        format_tag,
        0x0000,
        0x0010,
        bytes.fromhex("800000aa00389b71"),
    )


def _write_extensible_wav(
    path: Path,
    *,
    subformat_tag: int,
    sample_rate: int = 25_600,
    channels: int = 1,
    bits_per_sample: int = 16,
    valid_bits_per_sample: int | None = None,
    duration_sec: float = 2.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    container_bytes = bits_per_sample // 8
    block_align = channels * container_bytes
    frame_count = max(1, round(sample_rate * duration_sec))
    data = b"\x00" * (frame_count * block_align)
    fmt_data = struct.pack(
        "<HHIIHHHHI16s",
        0xFFFE,
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        bits_per_sample,
        22,
        bits_per_sample if valid_bits_per_sample is None else valid_bits_per_sample,
        0,
        _wav_subformat_guid(subformat_tag),
    )
    riff_size = 4 + (8 + len(fmt_data)) + (8 + len(data))
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt_data))
        + fmt_data
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return path


def _write_ieee_float_wav_header(
    path: Path,
    *,
    bits_per_sample: int,
    sample_rate: int = 25_600,
    channels: int = 1,
    duration_sec: float = 2.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    container_bytes = bits_per_sample // 8
    block_align = channels * container_bytes
    frame_count = max(1, round(sample_rate * duration_sec))
    data = b"\x00" * (frame_count * block_align)
    fmt_data = struct.pack(
        "<HHIIHH",
        3,
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        bits_per_sample,
    )
    riff_size = 4 + (8 + len(fmt_data)) + (8 + len(data))
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt_data))
        + fmt_data
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return path


def _config(
    config_id: int = 101,
    *,
    selected_channels: list[int] | None = None,
    analysis_window_offset_ms: int = 750,
    analysis_duration: float = 1.25,
) -> dict:
    return {
        "config_id": config_id,
        "config_name": f"C{config_id}",
        "selected_channels": [0] if selected_channels is None else selected_channels,
        "analysis_window_offset_ms": analysis_window_offset_ms,
        "analysis_duration": analysis_duration,
    }


def _dialog(
    *,
    configs: list[dict] | None = None,
    default_config_id: int | None = 101,
) -> ImportAudioAnalysisDialog:
    return ImportAudioAnalysisDialog(
        configs=[_config()] if configs is None else configs,
        default_config_id=default_config_id,
        parent=None,
    )


def _mapping_combo(dialog: ImportAudioAnalysisDialog, channel_index: int) -> QComboBox:
    combo = dialog.findChild(QComboBox, f"wav_channel_mapping_{channel_index}")
    assert combo is not None
    return combo


def _select_mapping(
    dialog: ImportAudioAnalysisDialog,
    wav_channel_index: int,
    data: tuple[str, int | None],
) -> None:
    combo = _mapping_combo(dialog, wav_channel_index)
    for index in range(combo.count()):
        if combo.itemData(index) == data:
            combo.setCurrentIndex(index)
            return
    raise AssertionError(f"mapping option not found: {data!r}")


def test_import_dialog_defaults_to_selected_config_window(qapp, tmp_path):
    wav_path = _write_wav(tmp_path / "input.wav", sample_rate=25_600, channels=2)
    dialog = _dialog(
        configs=[
            _config(
                101,
                selected_channels=[0],
                analysis_window_offset_ms=750,
                analysis_duration=1.25,
            )
        ],
        default_config_id=101,
    )

    dialog.load_wav_path(str(wav_path))

    request = dialog.current_request()
    assert request is not None
    assert request.config_id == 101
    assert request.start_sec == pytest.approx(0.75)
    assert request.duration_sec == pytest.approx(1.25)
    assert request.use_mic_calibration is True


def test_import_dialog_requires_config_choice_when_no_default(qapp, tmp_path):
    wav_path = _write_wav(tmp_path / "input.wav", sample_rate=25_600, channels=1)
    dialog = _dialog(
        configs=[
            _config(101, selected_channels=[0]),
            _config(202, selected_channels=[0]),
        ],
        default_config_id=None,
    )

    dialog.load_wav_path(str(wav_path))

    assert dialog.current_request() is None
    assert "配置" in (dialog.validation_error() or "")


def test_import_dialog_displays_wav_metadata_after_load(qapp, tmp_path):
    wav_path = _write_wav(
        tmp_path / "metadata.wav",
        sample_rate=48_000,
        duration_sec=1.5,
        channels=3,
    )
    dialog = _dialog()

    dialog.load_wav_path(str(wav_path))

    assert str(wav_path) in dialog.findChild(QLabel, "wav_path_value").text()
    assert dialog.findChild(QLabel, "wav_name_value").text() == "metadata.wav"
    assert "48000" in dialog.findChild(QLabel, "wav_sample_rate_value").text()
    assert dialog.findChild(QLabel, "wav_channel_count_value").text() == "3"
    assert "1.5" in dialog.findChild(QLabel, "wav_duration_value").text()


def test_import_dialog_loads_wav_metadata_without_full_audio_decode(
    qapp,
    tmp_path,
    monkeypatch,
):
    wav_path = _write_wav(
        tmp_path / "metadata_only.wav",
        sample_rate=48_000,
        duration_sec=1.5,
        channels=2,
    )

    def fail_load_wav(_path):
        raise AssertionError("dialog preview must not fully load WAV samples")

    monkeypatch.setattr(
        "ui.popups.import_audio_analysis_dialog.imported_audio.load_wav",
        fail_load_wav,
    )
    dialog = _dialog()

    dialog.load_wav_path(str(wav_path))

    assert dialog.findChild(QLabel, "wav_name_value").text() == "metadata_only.wav"
    assert "48000" in dialog.findChild(QLabel, "wav_sample_rate_value").text()
    assert dialog.findChild(QLabel, "wav_channel_count_value").text() == "2"
    assert "1.5" in dialog.findChild(QLabel, "wav_duration_value").text()
    assert not hasattr(dialog._wav, "buffer")

    request = dialog.current_request()
    assert request is not None
    assert request.wav_path == str(wav_path)
    assert request.assignments == (
        ImportedChannelAssignment(0, "config", 0),
        ImportedChannelAssignment(1, "chart", None),
    )


def test_import_dialog_rejects_unknown_extensible_wav_subformat(qapp, tmp_path):
    wav_path = _write_extensible_wav(
        tmp_path / "unknown_extensible.wav",
        subformat_tag=0x12345678,
        bits_per_sample=16,
    )
    dialog = _dialog()

    dialog.load_wav_path(str(wav_path))

    assert dialog.current_request() is None
    assert "格式" in (dialog.validation_error() or "")
    assert "不支持" in (dialog.validation_error() or "")


@pytest.mark.parametrize(
    ("subformat_tag", "bits_per_sample"),
    [
        (1, 16),
        (3, 32),
    ],
)
def test_import_dialog_accepts_supported_extensible_wav_subformats(
    qapp,
    tmp_path,
    subformat_tag,
    bits_per_sample,
):
    wav_path = _write_extensible_wav(
        tmp_path / f"supported_extensible_{subformat_tag}.wav",
        subformat_tag=subformat_tag,
        bits_per_sample=bits_per_sample,
    )
    dialog = _dialog()

    dialog.load_wav_path(str(wav_path))

    assert dialog.validation_error() is None
    assert dialog.current_request() is not None


def test_import_dialog_rejects_ieee_float_wav_with_unsupported_bit_depth(
    qapp,
    tmp_path,
):
    wav_path = _write_ieee_float_wav_header(
        tmp_path / "float16.wav",
        bits_per_sample=16,
    )
    dialog = _dialog()

    dialog.load_wav_path(str(wav_path))

    assert dialog.current_request() is None
    assert "格式" in (dialog.validation_error() or "")
    assert "不支持" in (dialog.validation_error() or "")


def test_import_dialog_rejects_non_wav_and_uses_wav_file_picker_filter(
    qapp,
    tmp_path,
    monkeypatch,
):
    non_wav_path = tmp_path / "input.txt"
    non_wav_path.write_text("not audio", encoding="utf-8")
    dialog = _dialog()

    dialog.load_wav_path(str(non_wav_path))

    assert dialog.current_request() is None
    assert "WAV" in (dialog.validation_error() or "")

    captured = {}

    def fake_get_open_file_name(parent, title, directory, file_filter):
        captured["parent"] = parent
        captured["title"] = title
        captured["directory"] = directory
        captured["file_filter"] = file_filter
        return (str(tmp_path / "chosen.wav"), "")

    monkeypatch.setattr(
        "ui.popups.import_audio_analysis_dialog.QFileDialog.getOpenFileName",
        fake_get_open_file_name,
    )

    assert dialog._pick_wav_path() == str(tmp_path / "chosen.wav")
    assert captured == {
        "parent": dialog,
        "title": "选择 WAV 文件",
        "directory": "",
        "file_filter": "WAV 文件 (*.wav)",
    }


def test_import_dialog_default_mapping_maps_selected_channels_and_chart_only_extras(
    qapp,
    tmp_path,
):
    wav_path = _write_wav(tmp_path / "input.wav", channels=4)
    dialog = _dialog(configs=[_config(selected_channels=[0, 2])])

    dialog.load_wav_path(str(wav_path))

    request = dialog.current_request()
    assert request is not None
    assert request.assignments == (
        ImportedChannelAssignment(0, "config", 0),
        ImportedChannelAssignment(1, "chart", None),
        ImportedChannelAssignment(2, "config", 2),
        ImportedChannelAssignment(3, "chart", None),
    )


def test_import_dialog_duplicate_config_mapping_prevents_acceptance(qapp, tmp_path):
    wav_path = _write_wav(tmp_path / "input.wav", channels=2)
    dialog = _dialog(configs=[_config(selected_channels=[0, 1])])
    dialog.load_wav_path(str(wav_path))

    _select_mapping(dialog, 1, ("config", 0))

    button_box = dialog.findChild(QDialogButtonBox, "dialog_buttons")
    assert button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled() is False
    assert dialog.current_request() is None
    assert "重复" in (dialog.validation_error() or "")


def test_import_dialog_start_duration_override_is_reflected_in_request(qapp, tmp_path):
    wav_path = _write_wav(tmp_path / "input.wav", duration_sec=4.0)
    dialog = _dialog()
    dialog.load_wav_path(str(wav_path))

    dialog.findChild(QDoubleSpinBox, "start_sec_spin").setValue(1.1)
    dialog.findChild(QDoubleSpinBox, "duration_sec_spin").setValue(2.2)

    request = dialog.current_request()
    assert request is not None
    assert request.start_sec == pytest.approx(1.1)
    assert request.duration_sec == pytest.approx(2.2)


def test_import_dialog_calibration_toggle_is_reflected_in_request(qapp, tmp_path):
    wav_path = _write_wav(tmp_path / "input.wav")
    dialog = _dialog()
    dialog.load_wav_path(str(wav_path))

    dialog.findChild(QCheckBox, "mic_calibration_checkbox").setChecked(False)

    request = dialog.current_request()
    assert request is not None
    assert request.use_mic_calibration is False
