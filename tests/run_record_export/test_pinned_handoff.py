from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import wave
from zipfile import ZipFile

from app.service.run_record_export.contracts import RunExportRecord


RUN_ID = "3e8c5a8e-0ec9-47f8-ae4b-55461024f552"


class _FrozenReader:
    def __init__(self, records: tuple[RunExportRecord, ...]) -> None:
        self._records = records
        self.selections: list[tuple[str, ...]] = []
        self.copy_selections: list[tuple[str, ...]] = []

    def read_many(
        self,
        run_ids: tuple[str, ...],
    ) -> tuple[RunExportRecord, ...]:
        self.selections.append(run_ids)
        return self._records

    def read_copy_database(self, run_ids: tuple[str, ...]) -> bytes:
        self.copy_selections.append(run_ids)
        database = sqlite3.connect(":memory:")
        try:
            database.execute("CREATE TABLE run(run_id TEXT PRIMARY KEY)")
            database.execute("INSERT INTO run(run_id) VALUES (?)", (RUN_ID,))
            return database.serialize()
        finally:
            database.close()


def _write_pcm16_wav(path: Path) -> None:
    channels = ((100, -200, 300), (-1, 2, -3))
    interleaved = bytearray()
    for frame_index in range(3):
        for channel in channels:
            interleaved.extend(
                channel[frame_index].to_bytes(2, "little", signed=True)
            )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8_000)
        writer.writeframes(bytes(interleaved))


def test_reader_value_maps_one_based_archive_positions_through_frozen_channel_order(
    tmp_path: Path,
) -> None:
    from app.service.run_record_export.archive import RunArchiveService

    source = tmp_path / "captured.wav"
    _write_pcm16_wav(source)
    record = RunExportRecord(
        run_id=RUN_ID,
        accepted_at="2026-07-28T01:02:03.000000Z",
        ingress_kind="CAPTURED",
        trigger_source="PLC",
        config_id=17,
        hardware_id=23,
        sn="SN-PINNED-001",
        model="MODEL-PINNED",
        state="SUCCEEDED",
        judgment="PASS",
        judgment_source="AUTOMATIC",
        fault_type=None,
        manual_judgment_revision=None,
        terminal_code="ANALYSIS_SUCCEEDED",
        terminal_at="2026-07-28T01:02:04.000000Z",
        source_wav_path=str(source),
        sample_rate_hz=8_000,
        channel_count=2,
        frame_count=3,
        channel_order=(4, 9),
        current_samples=None,
    )
    reader = _FrozenReader((record,))
    service = RunArchiveService(reader)
    target = tmp_path / "selected.zip"

    assert service.export((RUN_ID,), str(target)) == str(target)
    assert reader.selections == [(RUN_ID,)]
    assert reader.copy_selections == [(RUN_ID,)]

    with ZipFile(target, "r") as archive:
        copied = sqlite3.connect(":memory:")
        try:
            copied.deserialize(archive.read("copy_data.sqlite3"))
            assert copied.execute("SELECT run_id FROM run").fetchall() == [
                (RUN_ID,)
            ]
        finally:
            copied.close()
        manifest = json.loads(archive.read(f"{RUN_ID}/manifest.json"))

    rows = manifest["channels"]
    assert [set(row) for row in rows] == [
        {"position", "source_channel", "file"},
        {"position", "source_channel", "file"},
    ]
    for position, row in enumerate(rows, start=1):
        assert row == {
            "position": position,
            "source_channel": record.channel_order[position - 1],
            "file": f"channel-{position:03d}.wav",
        }
        assert (manifest["run"]["hardware_id"], row["source_channel"]) == (
            record.hardware_id,
            record.channel_order[position - 1],
        )
