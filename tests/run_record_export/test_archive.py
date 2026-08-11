from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import wave
from zipfile import ZipFile

import pytest


RUN_ID = "3e8c5a8e-0ec9-47f8-ae4b-55461024f552"
IMPORTED_RUN_ID = "4942f413-6398-4ab2-a776-2155acfd821d"


def _write_pcm16_wav(
    path: Path,
    *,
    sample_rate_hz: int = 8_000,
    channels: tuple[tuple[int, ...], ...] = ((100, -200, 300), (-1, 2, -3)),
) -> None:
    frame_count = len(channels[0])
    assert all(len(channel) == frame_count for channel in channels)
    interleaved = bytearray()
    for frame_index in range(frame_count):
        for channel in channels:
            interleaved.extend(
                int(channel[frame_index]).to_bytes(2, "little", signed=True)
            )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(len(channels))
        writer.setsampwidth(2)
        writer.setframerate(sample_rate_hz)
        writer.writeframes(bytes(interleaved))


def _read_pcm16_member(payload: bytes) -> tuple[int, int, int, tuple[int, ...]]:
    with wave.open(BytesIO(payload), "rb") as reader:
        frame_count = reader.getnframes()
        pcm_payload = reader.readframes(frame_count)
        values = tuple(
            int.from_bytes(sample, "little", signed=True)
            for sample in (
                pcm_payload[offset : offset + 2]
                for offset in range(0, frame_count * 2, 2)
            )
        )
        return (
            reader.getnchannels(),
            reader.getframerate(),
            frame_count,
            values,
        )


def _record(
    source_wav_path: Path,
    *,
    imported: bool = False,
    sn: str = "SN-EXPORT-001",
    model: str = "型号-甲",
    current_samples: tuple[tuple[float, float], ...] | None = (
        (0.0, 1.25),
        (0.2, 1.5),
    ),
):
    from app.service.run_record_export.contracts import RunExportRecord

    return RunExportRecord(
        run_id=IMPORTED_RUN_ID if imported else RUN_ID,
        accepted_at="2026-07-28T01:02:03.000000Z",
        ingress_kind="IMPORTED" if imported else "CAPTURED",
        trigger_source="IMPORT" if imported else "PLC",
        config_id=17,
        hardware_id=None if imported else 23,
        sn=sn,
        model=model,
        state="SUCCEEDED",
        judgment="PASS",
        judgment_source="AUTOMATIC",
        fault_type=None,
        manual_judgment_revision=None,
        terminal_code="ANALYSIS_SUCCEEDED",
        terminal_at="2026-07-28T01:02:04.000000Z",
        source_wav_path=str(source_wav_path),
        sample_rate_hz=8_000,
        channel_count=2,
        frame_count=3,
        channel_order=(4, 9),
        current_samples=current_samples,
    )


def _temp_artifacts(parent: Path) -> list[Path]:
    return list(parent.glob(".run-archive-*.tmp"))


def test_resolve_export_target_applies_extension_only_when_missing(tmp_path):
    from app.service.run_record_export.archive import resolve_export_target

    assert resolve_export_target(str(tmp_path / "operator-choice")) == str(
        tmp_path / "operator-choice.zip"
    )
    assert resolve_export_target(str(tmp_path / "operator-choice.ZIP")) == str(
        tmp_path / "operator-choice.ZIP"
    )
    assert resolve_export_target(str(tmp_path / "operator-choice.data")) == str(
        tmp_path / "operator-choice.data"
    )


def test_write_run_archive_splits_mono_pcm16_and_preserves_rate_and_frames(
    tmp_path,
):
    from app.service.run_record_export.archive import write_run_archive

    source = tmp_path / "captured.wav"
    _write_pcm16_wav(source)
    record = _record(source)

    chosen = tmp_path / "selected-runs"
    result = write_run_archive((record,), str(chosen))
    target = tmp_path / "selected-runs.zip"

    assert result == str(target)
    with ZipFile(target, "r") as archive:
        assert archive.namelist() == [
            "index.sqlite3",
            f"{RUN_ID}/manifest.json",
            f"{RUN_ID}/channel-001.wav",
            f"{RUN_ID}/channel-002.wav",
            f"{RUN_ID}/current.csv",
        ]
        manifest = json.loads(archive.read(f"{RUN_ID}/manifest.json"))
        assert manifest == {
            "schema": "jingcheng-run-export/v1",
            "run": {
                "run_id": RUN_ID,
                "accepted_at": "2026-07-28T01:02:03.000000Z",
                "ingress_kind": "CAPTURED",
                "trigger_source": "PLC",
                "config_id": 17,
                "hardware_id": 23,
                "sn": "SN-EXPORT-001",
                "model": "型号-甲",
            },
            "outcome": {
                "result": "OK",
                "state": "SUCCEEDED",
                "judgment": "PASS",
                "judgment_source": "AUTOMATIC",
                "fault_type": None,
                "manual_judgment_revision": None,
                "terminal_code": "ANALYSIS_SUCCEEDED",
                "terminal_at": "2026-07-28T01:02:04.000000Z",
            },
            "audio": {
                "sample_rate_hz": 8_000,
                "frame_count": 3,
            },
            "channels": [
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
            ],
            "current": {"present": True, "file": "current.csv"},
        }
        assert _read_pcm16_member(
            archive.read(f"{RUN_ID}/channel-001.wav")
        ) == (1, 8_000, 3, (100, -200, 300))
        assert _read_pcm16_member(
            archive.read(f"{RUN_ID}/channel-002.wav")
        ) == (1, 8_000, 3, (-1, 2, -3))
        assert archive.read(f"{RUN_ID}/current.csv") == (
            b"time_sec,current_a\n0.0,1.25\n0.2,1.5\n"
        )

        index = sqlite3.connect(":memory:")
        try:
            index.deserialize(archive.read("index.sqlite3"))
            assert index.execute("PRAGMA application_id").fetchone() == (
                1245926744,
            )
            assert index.execute("PRAGMA user_version").fetchone() == (1,)
            assert dict(index.execute("SELECT key, value FROM export_metadata")) == {
                "archive_schema": "jingcheng-run-export/v1",
                "index_schema": "jingcheng-run-export-index/v1",
            }
            assert index.execute(
                """
                SELECT run_id, export_order, sn, model, result,
                       manifest_path, current_csv_path
                FROM export_run
                """
            ).fetchone() == (
                RUN_ID,
                1,
                "SN-EXPORT-001",
                "型号-甲",
                "OK",
                f"{RUN_ID}/manifest.json",
                f"{RUN_ID}/current.csv",
            )
            assert tuple(
                index.execute(
                    """
                    SELECT position, source_channel, wav_path
                    FROM export_channel
                    ORDER BY position
                    """
                )
            ) == (
                (1, 4, f"{RUN_ID}/channel-001.wav"),
                (2, 9, f"{RUN_ID}/channel-002.wav"),
            )
        finally:
            index.close()


def test_manifest_uses_null_hardware_id_for_imported_run(tmp_path):
    from app.service.run_record_export.archive import write_run_archive

    source = tmp_path / "imported.wav"
    _write_pcm16_wav(source)
    record = _record(source, imported=True, current_samples=None)
    target = tmp_path / "imported.data"

    assert write_run_archive((record,), str(target)) == str(target)

    with ZipFile(target, "r") as archive:
        assert archive.namelist() == [
            "index.sqlite3",
            f"{IMPORTED_RUN_ID}/manifest.json",
            f"{IMPORTED_RUN_ID}/channel-001.wav",
            f"{IMPORTED_RUN_ID}/channel-002.wav",
        ]
        manifest = json.loads(archive.read(f"{IMPORTED_RUN_ID}/manifest.json"))
        assert set(manifest) == {
            "schema",
            "run",
            "outcome",
            "audio",
            "channels",
            "current",
        }
        assert manifest["run"]["hardware_id"] is None
        assert manifest["current"] == {"present": False, "file": None}
        assert all(
            set(channel) == {"position", "source_channel", "file"}
            for channel in manifest["channels"]
        )


def test_index_database_preserves_selected_order_and_unicode_identity(tmp_path):
    from app.service.run_record_export.archive import write_run_archive

    imported_source = tmp_path / "imported.wav"
    captured_source = tmp_path / "captured.wav"
    _write_pcm16_wav(imported_source)
    _write_pcm16_wav(captured_source)
    records = (
        _record(
            imported_source,
            imported=True,
            sn="SN-第二条",
            model="型号-乙",
            current_samples=None,
        ),
        _record(captured_source, sn="SN-第一条", model="型号-甲"),
    )
    target = tmp_path / "ordered.zip"

    assert write_run_archive(records, str(target)) == str(target)

    with ZipFile(target, "r") as archive:
        index_payload = archive.read("index.sqlite3")
    index = sqlite3.connect(":memory:")
    try:
        index.deserialize(index_payload)
        assert tuple(
            index.execute(
                """
                SELECT export_order, run_id, sn, model, current_csv_path
                FROM export_run
                ORDER BY export_order
                """
            )
        ) == (
            (1, IMPORTED_RUN_ID, "SN-第二条", "型号-乙", None),
            (
                2,
                RUN_ID,
                "SN-第一条",
                "型号-甲",
                f"{RUN_ID}/current.csv",
            ),
        )
        assert index.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        index.close()


def test_write_run_archive_publishes_sibling_temp_after_close_testzip_once_replace(
    tmp_path, monkeypatch
):
    from app.service.run_record_export import archive as archive_module

    source = tmp_path / "captured.wav"
    _write_pcm16_wav(source)
    record = _record(source)
    target = tmp_path / "publication.zip"
    events: list[tuple[str, Path]] = []
    real_zip_file = archive_module.zipfile.ZipFile
    real_replace = archive_module.os.replace

    class RecordingZipFile(real_zip_file):
        def __init__(self, file, mode="r", *args, **kwargs):
            self._recorded_mode = mode
            self._recorded_path = Path(file)
            events.append((f"open-{mode}", self._recorded_path))
            super().__init__(file, mode, *args, **kwargs)

        def close(self):
            was_open = self.fp is not None
            super().close()
            if was_open:
                events.append((f"close-{self._recorded_mode}", self._recorded_path))

        def testzip(self):
            assert any(name == "close-w" for name, _ in events)
            events.append(("testzip", self._recorded_path))
            return super().testzip()

    def recording_replace(source_path, target_path):
        events.append(("replace", Path(source_path)))
        assert Path(source_path).parent == target.parent
        assert Path(source_path) != target
        assert not target.exists()
        return real_replace(source_path, target_path)

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", RecordingZipFile)
    monkeypatch.setattr(archive_module.os, "replace", recording_replace)

    assert archive_module.write_run_archive((record,), str(target)) == str(target)

    write_paths = [path for name, path in events if name == "open-w"]
    assert len(write_paths) == 1
    assert [name for name, _ in events].count("testzip") == 1
    assert [name for name, _ in events].count("replace") == 1
    assert [name for name, _ in events].index("close-w") < [
        name for name, _ in events
    ].index("testzip")
    assert [name for name, _ in events].index("testzip") < [
        name for name, _ in events
    ].index("replace")
    assert not write_paths[0].exists()


def test_write_run_archive_uses_short_target_independent_temp_prefix(
    tmp_path, monkeypatch
):
    from app.service.run_record_export import archive as archive_module

    source = tmp_path / "captured.wav"
    _write_pcm16_wav(source)
    record = _record(source)
    target = tmp_path / f"{'x' * 251}.zip"
    mkstemp_arguments = {}

    def recording_mkstemp(*, prefix, suffix, dir):
        mkstemp_arguments.update(prefix=prefix, suffix=suffix, dir=dir)
        raise OSError("stop after temporary filename inspection")

    monkeypatch.setattr(archive_module.tempfile, "mkstemp", recording_mkstemp)

    with pytest.raises(
        archive_module.RunArchiveError,
        match="stop after temporary filename inspection",
    ):
        archive_module.write_run_archive((record,), str(target))

    assert mkstemp_arguments == {
        "prefix": ".run-archive-",
        "suffix": ".tmp",
        "dir": str(target.parent),
    }


def test_write_run_archive_replaces_unrelated_existing_target(
    tmp_path, monkeypatch
):
    from app.service.run_record_export import archive as archive_module

    source = tmp_path / "captured.wav"
    _write_pcm16_wav(source)
    record = _record(source)
    target = tmp_path / "existing.zip"
    original = b"unrelated-existing-target"
    target.write_bytes(original)
    real_replace = archive_module.os.replace
    replace_calls = 0

    def asserting_replace(source_path, target_path):
        nonlocal replace_calls
        replace_calls += 1
        assert Path(target_path).read_bytes() == original
        return real_replace(source_path, target_path)

    monkeypatch.setattr(archive_module.os, "replace", asserting_replace)

    assert archive_module.write_run_archive((record,), str(target)) == str(target)
    assert replace_calls == 1
    assert target.read_bytes() != original
    with ZipFile(target, "r") as archive:
        assert archive.testzip() is None


@pytest.mark.parametrize("failure_stage", ["write", "close", "test"])
def test_write_run_archive_failure_cleans_temp_without_partial_target(
    tmp_path, monkeypatch, failure_stage
):
    from app.service.run_record_export import archive as archive_module

    source = tmp_path / f"{failure_stage}.wav"
    _write_pcm16_wav(source)
    record = _record(source)
    target = tmp_path / f"{failure_stage}.zip"
    original = b"existing-target-must-survive"
    target.write_bytes(original)
    real_zip_file = archive_module.zipfile.ZipFile
    replace_calls = 0

    class FailingZipFile(real_zip_file):
        _close_failure_raised = False

        def __init__(self, file, mode="r", *args, **kwargs):
            self._failure_mode = mode
            super().__init__(file, mode, *args, **kwargs)

        def writestr(self, *args, **kwargs):
            if failure_stage == "write":
                raise OSError("injected write failure")
            return super().writestr(*args, **kwargs)

        def close(self):
            super().close()
            if (
                failure_stage == "close"
                and self._failure_mode == "w"
                and not type(self)._close_failure_raised
            ):
                type(self)._close_failure_raised = True
                raise OSError("injected close failure")

        def testzip(self):
            if failure_stage == "test":
                raise OSError("injected test failure")
            return super().testzip()

    def forbidden_replace(*args):
        nonlocal replace_calls
        replace_calls += 1
        raise AssertionError("replace must not run after archive failure")

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", FailingZipFile)
    monkeypatch.setattr(archive_module.os, "replace", forbidden_replace)

    with pytest.raises(Exception, match=f"injected {failure_stage} failure"):
        archive_module.write_run_archive((record,), str(target))

    assert replace_calls == 0
    assert target.read_bytes() == original
    assert _temp_artifacts(tmp_path) == []


@pytest.mark.parametrize("alias_kind", ["resolved", "hardlink", "samefile-error"])
def test_write_run_archive_rejects_target_source_alias_before_any_mutation(
    tmp_path, monkeypatch, alias_kind
):
    from app.service.run_record_export import archive as archive_module

    source = tmp_path / "selected-source.zip"
    _write_pcm16_wav(source)
    source_before = source.read_bytes()
    record = _record(source)
    target = source

    if alias_kind == "resolved":
        chosen = source.with_suffix("")
    else:
        target = tmp_path / f"{alias_kind}.zip"
        if alias_kind == "hardlink":
            os.link(source, target)
        else:
            target.write_bytes(b"unrelated-existing-target")
            monkeypatch.setattr(
                archive_module.os.path,
                "samefile",
                lambda *_: (_ for _ in ()).throw(OSError("samefile failed")),
            )
        chosen = target

    target_before = target.read_bytes() if target.exists() else None
    temp_calls = 0
    replace_calls = 0

    def forbidden_mkstemp(*args, **kwargs):
        nonlocal temp_calls
        temp_calls += 1
        raise AssertionError("alias rejection must precede temp creation")

    def forbidden_replace(*args, **kwargs):
        nonlocal replace_calls
        replace_calls += 1
        raise AssertionError("alias rejection must precede target mutation")

    monkeypatch.setattr(archive_module.tempfile, "mkstemp", forbidden_mkstemp)
    monkeypatch.setattr(archive_module.os, "replace", forbidden_replace)

    expected_message = "samefile failed" if alias_kind == "samefile-error" else "alias"
    with pytest.raises(Exception, match=expected_message):
        archive_module.write_run_archive((record,), str(chosen))

    assert temp_calls == 0
    assert replace_calls == 0
    assert source.read_bytes() == source_before
    if target_before is not None:
        assert target.read_bytes() == target_before
