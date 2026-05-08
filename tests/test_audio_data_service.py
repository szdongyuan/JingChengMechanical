from __future__ import annotations

import sqlite3
import zipfile

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
    service.export_zip(str(target), records)

    with zipfile.ZipFile(target) as archive:
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
