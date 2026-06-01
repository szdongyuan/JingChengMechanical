from __future__ import annotations

import sqlite3
import zipfile

import pytest

from app.service.audio_data_service import AudioDataService
from model.audio_data_table import AudioDataTable


def test_audio_data_service_columns_include_label_source():
    columns = AudioDataService().list_column_names()

    assert "label_source" in columns
    assert columns.index("label_source") > columns.index("labels")


def test_audio_data_export_db_includes_label_source(tmp_path):
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFF")
    AudioDataTable.create(
        audio_data_id="audio-1",
        file_path=str(wav_path),
        product_model="M1",
        sample_rate=44100,
        record_date="2026-05-08T10:00:00+08:00",
        labels="OK",
        label_source="manual",
        barcode="SN-1",
        analyse_config="Config A",
        config_content="{}",
    )

    target = tmp_path / "export.zip"
    service = AudioDataService()
    records = service.list_records()
    progress_values: list[int] = []
    service.export_zip(str(target), records, progress_callback=progress_values.append)

    with zipfile.ZipFile(target) as archive:
        assert {
            info.filename: info.compress_type
            for info in archive.infolist()
        } == {
            "export.db": zipfile.ZIP_STORED,
            "OK/audio-1.wav": zipfile.ZIP_STORED,
        }
        db_name = [name for name in archive.namelist() if name.endswith(".db")][0]
        archive.extract(db_name, tmp_path)
    con = sqlite3.connect(tmp_path / db_name)
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(audio_data_table)")]
        row = con.execute("SELECT label_source FROM audio_data_table").fetchone()
    finally:
        con.close()

    assert "label_source" in columns
    assert row == ("manual",)
    assert progress_values == [1, 2]


def test_audio_data_export_removes_temp_db_when_first_progress_callback_fails(tmp_path):
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFF")
    AudioDataTable.create(
        audio_data_id="audio-cancel",
        file_path=str(wav_path),
        product_model="M1",
        sample_rate=44100,
        record_date="2026-05-08T10:00:00+08:00",
        labels="OK",
        label_source="manual",
        barcode="SN-1",
        analyse_config="Config A",
        config_content="{}",
    )

    target = tmp_path / "export.zip"
    service = AudioDataService()

    def cancel_on_first_progress(_value):
        raise RuntimeError("cancel")

    with pytest.raises(RuntimeError, match="cancel"):
        service.export_zip(
            str(target),
            service.list_records(),
            progress_callback=cancel_on_first_progress,
        )

    assert not (tmp_path / "export.zip.tmp.db").exists()


def test_audio_data_delete_record_removes_database_row_and_audio_file(tmp_path):
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFF")
    AudioDataTable.create(
        audio_data_id="audio-delete",
        file_path=str(wav_path),
        product_model="M1",
        sample_rate=44100,
        record_date="2026-05-08T10:00:00+08:00",
        labels="OK",
        label_source="manual",
        barcode="SN-1",
        analyse_config="Config A",
        config_content="{}",
    )

    AudioDataService().delete_record("audio-delete")

    assert AudioDataTable.get_or_none(
        AudioDataTable.audio_data_id == "audio-delete"
    ) is None
    assert not wav_path.exists()
