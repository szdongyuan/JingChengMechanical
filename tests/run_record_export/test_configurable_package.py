from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import itertools
import json
from pathlib import Path
import wave
from zipfile import BadZipFile, ZipFile

import openpyxl
import pytest

from app.service.run_record_export.contracts import (
    AiExportValue,
    AudioExportFacts,
    CurrentExportFacts,
    RunDataExportOptions,
    RunDataExportReadReport,
    RunDataExportRecord,
    RunDataExportSkippedRecord,
)
from app.service.run_record_export.package import write_run_data_export


RUN_IDS = tuple(
    f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 4_001)
)
EXPORTED_AT = datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc)


def _write_wav(path: Path, *, frames: int = 3, channels: int = 2) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(8_000)
        writer.writeframes(b"\x01\x00" * frames * channels)


def _record(
    index: int,
    source: Path | None,
    *,
    accepted_at: str = "2026-09-03T01:02:03.000000Z",
    sn: str = ' SN</bad> ',
    audio_issue: str | None = None,
    current_issue: str | None = None,
) -> RunDataExportRecord:
    return RunDataExportRecord(
        run_id=RUN_IDS[index], selection_index=index,
        accepted_at=accepted_at,
        terminal_at="2026-09-03T01:03:03.000000Z",
        sn=sn, model="M1", state="SUCCEEDED", terminal_code="completed",
        judgment="PASS", judgment_source="AUTOMATIC", fault_type=None,
        audio=AudioExportFacts(
            str(source) if source is not None and audio_issue is None else None,
            8_000 if source is not None and audio_issue is None else None,
            2 if source is not None and audio_issue is None else None,
            3 if source is not None and audio_issue is None else None,
            (0, 2) if source is not None and audio_issue is None else None,
            audio_issue,
        ),
        current=CurrentExportFacts(
            ((0.0, 1.25), (0.2, -3.5)) if current_issue is None else None,
            current_issue,
        ),
    )


def _report(*records, skipped=()) -> RunDataExportReadReport:
    selected = [None] * (len(records) + len(skipped))
    for record in records:
        selected[record.selection_index] = record.run_id
    for skip in skipped:
        selected[skip.selection_index] = skip.run_id
    return RunDataExportReadReport(tuple(selected), tuple(records), tuple(skipped))


ALL_OPTIONS = tuple(
    values for values in itertools.product((False, True), repeat=4) if any(values)
)


@pytest.mark.parametrize("values", ALL_OPTIONS)
def test_all_15_option_combinations_route_and_emit_exact_selected_roots(
    tmp_path, values
):
    source = tmp_path / "source.wav"
    _write_wav(source)
    options = RunDataExportOptions(*values)
    database = b"unchanged-db-bytes" if options.include_database else None

    result = write_run_data_export(
        _report(_record(0, source)), str(tmp_path / "export"),
        ai_values={RUN_IDS[0]: AiExportValue(0.95, " RAW ")},
        options=options, database_bytes=database, clock=lambda: EXPORTED_AT,
    )

    output = Path(result.target_path)
    assert result.output_kind == options.output_kind
    if options.output_kind == "xlsx":
        assert output.suffix == ".xlsx"
        workbook = openpyxl.load_workbook(output, read_only=True)
        assert workbook.sheetnames == ["检测记录"]
        workbook.close()
        return

    assert output.suffix == ".zip"
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        root = json.loads(archive.read("export_manifest.json"))
        directory = root["records"][0]["directory"]
        assert directory == "records/000001_SN_bad_"
        assert f"{directory}/manifest.json" in names
        assert ("检测记录.xlsx" in names) is options.include_excel
        assert ("copy_data.sqlite3" in names) is options.include_database
        assert any(name.endswith("/audio/CH1.wav") for name in names) is options.include_audio
        assert any(name.endswith("/audio/CH3.wav") for name in names) is options.include_audio
        assert any(name.endswith("/current.csv") for name in names) is options.include_current_csv
        if options.include_database:
            assert archive.read("copy_data.sqlite3") == database
        record_manifest = json.loads(archive.read(f"{directory}/manifest.json"))
        declared_audio = [item["path"] for item in record_manifest["audio"]["channels"]]
        assert all(path in names for path in declared_audio)
        if options.include_excel:
            workbook = openpyxl.load_workbook(BytesIO(archive.read("检测记录.xlsx")), read_only=True)
            assert workbook["检测记录"]["J2"].value == ("\n".join(declared_audio) if declared_audio else None)
            workbook.close()


def test_attachment_issues_are_recoverable_and_counts_use_run_and_file_units(tmp_path):
    source = tmp_path / "source.wav"
    _write_wav(source)
    report = _report(
        _record(0, None, audio_issue="audio read issue", current_issue="current read issue"),
        _record(1, source, sn="good"),
    )
    target = tmp_path / "partial.zip"
    result = write_run_data_export(
        report, str(target), ai_values={},
        options=RunDataExportOptions(False, True, False, True),
        clock=lambda: EXPORTED_AT,
    )
    assert (
        result.counts.audio_runs_emitted, result.counts.audio_runs_skipped,
        result.counts.audio_files_emitted, result.counts.current_runs_emitted,
        result.counts.current_runs_skipped,
    ) == (1, 1, 2, 1, 1)
    with ZipFile(target) as archive:
        root = json.loads(archive.read("export_manifest.json"))
        assert [(s["run_id"], s["component"]) for s in root["skips"]] == [
            (RUN_IDS[0], "audio"), (RUN_IDS[0], "current")
        ]


def test_package_audio_does_not_call_byte_returning_split_helper(
    tmp_path,
    monkeypatch,
):
    import app.service.run_record_export.package as package_module

    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "streamed.zip"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("v2 package must not build complete channel bytes")

    monkeypatch.setattr(
        package_module,
        "split_pcm16_wav",
        forbidden,
        raising=False,
    )

    result = write_run_data_export(
        _report(_record(0, source)),
        str(target),
        ai_values={},
        options=RunDataExportOptions(False, True, False, False),
        clock=lambda: EXPORTED_AT,
    )

    assert result.counts.audio_files_emitted == 2
    with ZipFile(target) as archive:
        assert archive.testzip() is None


def test_package_streamed_staged_audio_has_exact_channel_content(tmp_path):
    source = tmp_path / "distinct.wav"
    channels = ((-32_768, -2, 17, 32_767), (101, 202, 303, 404))
    interleaved = bytearray()
    for frame_index in range(4):
        for channel in channels:
            interleaved.extend(
                channel[frame_index].to_bytes(2, "little", signed=True)
            )
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8_000)
        writer.writeframes(bytes(interleaved))
    record = _record(0, source)
    object.__setattr__(
        record,
        "audio",
        AudioExportFacts(str(source), 8_000, 2, 4, (0, 2), None),
    )
    target = tmp_path / "exact-audio.zip"

    write_run_data_export(
        _report(record),
        str(target),
        ai_values={},
        options=RunDataExportOptions(False, True, False, False),
        clock=lambda: EXPORTED_AT,
    )

    with ZipFile(target) as archive:
        for member_name, expected in (
            ("records/000001_SN_bad_/audio/CH1.wav", channels[0]),
            ("records/000001_SN_bad_/audio/CH3.wav", channels[1]),
        ):
            with wave.open(BytesIO(archive.read(member_name)), "rb") as reader:
                assert reader.getnchannels() == 1
                assert reader.getframerate() == 8_000
                assert reader.getnframes() == 4
                payload = reader.readframes(4)
                actual = tuple(
                    int.from_bytes(payload[offset : offset + 2], "little", signed=True)
                    for offset in range(0, len(payload), 2)
                )
                assert actual == expected


def test_package_cancellation_cleans_partially_streamed_audio_stage(
    tmp_path,
    monkeypatch,
):
    import app.service.run_record_export.package as package_module

    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "stream-cancel.zip"
    armed = False

    def canceling_stream(
        _source_wav_path,
        *,
        output_paths,
        cancel_check,
        **_kwargs,
    ):
        nonlocal armed
        Path(output_paths[0]).write_bytes(b"partial WAV")
        armed = True
        cancel_check()

    monkeypatch.setattr(
        package_module,
        "write_pcm16_wav_channels",
        canceling_stream,
        raising=False,
    )

    def cancel():
        if armed:
            raise RuntimeError("cancel partial stream")

    with pytest.raises(RuntimeError, match="cancel partial stream"):
        write_run_data_export(
            _report(_record(0, source)),
            str(target),
            ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT,
            cancel_check=cancel,
        )

    assert not target.exists()
    assert list(tmp_path.glob(".run-export-stage-*")) == []


def test_zip_excel_sanitizes_ooxml_text_but_manifest_keeps_raw_values(tmp_path):
    raw_sn = " SN\x01raw "
    raw_model = "=model\x0braw"
    raw_fault = "+fault\x0craw"
    raw_label = " label\x1fraw "
    record = _record(0, None, sn=raw_sn, audio_issue="missing")
    object.__setattr__(record, "model", raw_model)
    object.__setattr__(record, "fault_type", raw_fault)
    target = tmp_path / "illegal-text.zip"

    write_run_data_export(
        _report(record),
        str(target),
        ai_values={RUN_IDS[0]: AiExportValue(0.5, raw_label)},
        options=RunDataExportOptions(True, False, False, True),
        clock=lambda: EXPORTED_AT,
    )

    with ZipFile(target) as archive:
        root = json.loads(archive.read("export_manifest.json"))
        record_manifest = json.loads(
            archive.read(root["records"][0]["manifest"])
        )
        workbook = openpyxl.load_workbook(
            BytesIO(archive.read("检测记录.xlsx")),
            read_only=True,
            data_only=False,
        )
        try:
            row = tuple(workbook["检测记录"].iter_rows(min_row=2, values_only=True))[0]
        finally:
            workbook.close()

    assert row[2] == " SN\ufffdraw "
    assert row[3] == "=model\ufffdraw"
    assert row[6] == "+fault\ufffdraw"
    assert row[8] == " label\ufffdraw "
    assert record_manifest["run"]["sn"] == raw_sn
    assert record_manifest["run"]["model"] == raw_model
    assert record_manifest["outcome"]["fault_type"] == raw_fault
    assert record_manifest["ai"]["label"] == raw_label


def test_audio_only_with_zero_emitted_attachments_fails_generically(tmp_path):
    target = tmp_path / "zero.zip"
    with pytest.raises(Exception, match="no useful selected artifacts"):
        write_run_data_export(
            _report(_record(0, None, audio_issue="missing")), str(target),
            ai_values={}, options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT,
        )
    assert not target.exists()


def test_duplicate_source_channel_collision_skips_all_audio_without_partial_member(tmp_path):
    source = tmp_path / "source.wav"
    _write_wav(source)
    record = _record(0, source)
    object.__setattr__(record.audio, "channel_order", (0, 0))
    target = tmp_path / "collision.zip"
    result = write_run_data_export(
        _report(record), str(target), ai_values={},
        options=RunDataExportOptions(True, True, False, False),
        clock=lambda: EXPORTED_AT,
    )
    assert result.counts.audio_runs_skipped == 1
    assert result.counts.audio_files_emitted == 0
    with ZipFile(target) as archive:
        assert not any("/audio/" in name for name in archive.namelist())


def test_primary_and_attachment_skips_follow_selection_then_component_order(tmp_path):
    older = _record(0, None, accepted_at="2026-09-01T00:00:00.000000Z", audio_issue="a0", current_issue="c0")
    newer = _record(2, None, accepted_at="2026-09-03T00:00:00.000000Z", audio_issue="a2", current_issue="c2")
    primary = RunDataExportSkippedRecord(RUN_IDS[1], "primary", "bad", 1)
    target = tmp_path / "order.zip"
    write_run_data_export(
        _report(older, newer, skipped=(primary,)), str(target), ai_values={},
        options=RunDataExportOptions(True, True, False, True), clock=lambda: EXPORTED_AT,
    )
    with ZipFile(target) as archive:
        root = json.loads(archive.read("export_manifest.json"))
        assert [entry["run_id"] for entry in root["records"]] == [RUN_IDS[2], RUN_IDS[0]]
        assert [(s["run_id"], s["component"]) for s in root["skips"]] == [
            (RUN_IDS[0], "audio"), (RUN_IDS[0], "current"),
            (RUN_IDS[1], "primary"), (RUN_IDS[2], "audio"), (RUN_IDS[2], "current"),
        ]


@pytest.mark.parametrize("failure", ["validate", "replace", "cancel"])
def test_failure_cancel_cleanup_preserves_existing_target(tmp_path, monkeypatch, failure):
    import app.service.run_record_export.package as package_module
    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "existing_target.zip"
    original = b"existing-valid-target"
    target.write_bytes(original)
    armed = False
    if failure == "validate":
        monkeypatch.setattr(package_module, "_validate_zip", lambda *_a, **_k: (_ for _ in ()).throw(OSError("validate failure")))
    elif failure == "replace":
        monkeypatch.setattr(package_module.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("replace failure")))
    else:
        real_validate = package_module._validate_zip
        def arm(*args, **kwargs):
            nonlocal armed
            real_validate(*args, **kwargs)
            armed = True
        monkeypatch.setattr(package_module, "_validate_zip", arm)
    def cancel():
        if armed:
            raise RuntimeError("cancel now")
    with pytest.raises(Exception, match="failure|cancel now"):
        write_run_data_export(
            _report(_record(0, source)), str(target), ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT, cancel_check=cancel,
        )
    assert target.read_bytes() == original
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_all_selected_primaries_invalid_preserves_existing_target(tmp_path):
    primary = RunDataExportSkippedRecord(RUN_IDS[0], "primary", "bad", 0)
    report = RunDataExportReadReport((RUN_IDS[0],), (), (primary,))
    target = tmp_path / "all-invalid.xlsx"
    target.write_bytes(b"existing")

    with pytest.raises(Exception, match="no valid primary"):
        write_run_data_export(
            report, str(target), ai_values={},
            options=RunDataExportOptions(True, False, False, False),
            clock=lambda: EXPORTED_AT,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


@pytest.mark.parametrize(
    "failure_stage",
    [
        "excel_builder", "xlsx_validation", "record_manifest",
        "root_manifest", "manifest_closure", "mkstemp", "member_write",
    ],
)
def test_complete_builder_and_publication_failure_matrix_preserves_target_and_cleans_temp(
    tmp_path, monkeypatch, failure_stage
):
    import app.service.run_record_export.package as package_module

    source = tmp_path / "source.wav"
    _write_wav(source)
    xlsx = failure_stage in {"excel_builder", "xlsx_validation"}
    target = tmp_path / ("existing.xlsx" if xlsx else "existing.zip")
    target.write_bytes(b"existing")

    def fail(*_args, **_kwargs):
        raise OSError(f"{failure_stage} failure")

    if failure_stage == "excel_builder":
        monkeypatch.setattr(package_module, "build_excel_workbook", fail)
    elif failure_stage == "xlsx_validation":
        monkeypatch.setattr(package_module, "_validate_xlsx", fail)
    elif failure_stage == "record_manifest":
        monkeypatch.setattr(package_module, "build_record_manifest", fail)
    elif failure_stage == "root_manifest":
        monkeypatch.setattr(package_module, "build_export_manifest", fail)
    elif failure_stage == "manifest_closure":
        monkeypatch.setattr(package_module, "validate_declared_members", fail)
    elif failure_stage == "mkstemp":
        monkeypatch.setattr(package_module.tempfile, "mkstemp", fail)
    elif failure_stage == "member_write":
        monkeypatch.setattr(package_module, "_write_zip_member", fail)

    options = (
        RunDataExportOptions(True, False, False, False)
        if xlsx else RunDataExportOptions(False, True, False, False)
    )
    with pytest.raises(Exception, match=f"{failure_stage} failure"):
        write_run_data_export(
            _report(_record(0, source)), str(target), ai_values={},
            options=options, clock=lambda: EXPORTED_AT,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_cancellation_during_preparation_preserves_existing_target(tmp_path):
    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "prepare-cancel.zip"
    target.write_bytes(b"existing")

    with pytest.raises(RuntimeError, match="prepare cancel"):
        write_run_data_export(
            _report(_record(0, source)), str(target), ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT,
            cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("prepare cancel")),
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_cancellation_during_multichunk_member_write_cleans_temp_and_preserves_target(
    tmp_path, monkeypatch
):
    import app.service.run_record_export.package as package_module

    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "chunk-cancel.zip"
    target.write_bytes(b"existing")
    large_payload = b"x" * (2 * 1024 * 1024 + 1)
    armed = False
    checks_after_arm = 0
    real_open = package_module.zipfile.ZipFile.open

    def write_large_staged_channels(
        _source_wav_path,
        *,
        output_paths,
        **_kwargs,
    ):
        for output_path in output_paths:
            Path(output_path).write_bytes(large_payload)

    monkeypatch.setattr(
        package_module,
        "write_pcm16_wav_channels",
        write_large_staged_channels,
    )

    def recording_open(self, name, mode="r", *args, **kwargs):
        nonlocal armed
        if mode == "w" and str(name).endswith("/audio/CH1.wav"):
            armed = True
        return real_open(self, name, mode, *args, **kwargs)

    monkeypatch.setattr(package_module.zipfile.ZipFile, "open", recording_open)

    def cancel():
        nonlocal checks_after_arm
        if armed:
            checks_after_arm += 1
            if checks_after_arm == 1:
                raise RuntimeError("chunk cancel")

    with pytest.raises(RuntimeError, match="chunk cancel"):
        write_run_data_export(
            _report(_record(0, source)), str(target), ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT, cancel_check=cancel,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_cancellation_during_multichunk_zip_validation_cleans_all_temporary_state(
    tmp_path, monkeypatch
):
    import app.service.run_record_export.package as package_module

    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "validation-cancel.zip"
    target.write_bytes(b"existing")
    large_payload = b"x" * (2 * 1024 * 1024 + 1)
    validation_bytes = 0
    validation_read_sizes = []
    armed = False
    real_open = package_module.zipfile.ZipFile.open

    def write_large_staged_channels(
        _source_wav_path,
        *,
        output_paths,
        **_kwargs,
    ):
        for output_path in output_paths:
            Path(output_path).write_bytes(large_payload)

    monkeypatch.setattr(
        package_module,
        "write_pcm16_wav_channels",
        write_large_staged_channels,
    )

    class TrackingReader:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def read(self, size=-1):
            nonlocal armed, validation_bytes
            validation_read_sizes.append(size)
            chunk = self._stream.read(size)
            validation_bytes += len(chunk)
            if chunk:
                armed = True
            return chunk

    def track_validation_open(self, name, mode="r", *args, **kwargs):
        stream = real_open(self, name, mode, *args, **kwargs)
        member_name = getattr(name, "filename", str(name))
        if self.mode == "r" and member_name.endswith("/audio/CH1.wav"):
            return TrackingReader(stream)
        return stream

    monkeypatch.setattr(
        package_module.zipfile.ZipFile,
        "open",
        track_validation_open,
    )

    def cancel():
        if armed:
            raise RuntimeError("validation cancel")

    with pytest.raises(RuntimeError, match="validation cancel"):
        write_run_data_export(
            _report(_record(0, source)),
            str(target),
            ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT,
            cancel_check=cancel,
        )

    assert validation_read_sizes
    assert max(validation_read_sizes) <= 1024 * 1024
    assert validation_bytes <= 1024 * 1024
    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []
    assert list(tmp_path.glob(".run-export-stage-*")) == []


def test_completion_progress_failure_occurs_before_atomic_replace(
    tmp_path,
):
    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "completion-progress.zip"
    target.write_bytes(b"existing")

    def progress(completed, total, _message):
        if completed == total:
            raise RuntimeError("completion progress failure")

    with pytest.raises(Exception, match="completion progress failure"):
        write_run_data_export(
            _report(_record(0, source)), str(target), ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT, progress_callback=progress,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_incoming_skip_reasons_are_bounded_before_both_manifest_levels(tmp_path):
    reason = "x" * 900
    record = _record(0, None, audio_issue="unused")
    skip = RunDataExportSkippedRecord(RUN_IDS[0], "audio", reason, 0)
    report = RunDataExportReadReport((RUN_IDS[0],), (record,), (skip,))
    target = tmp_path / "bounded-reason.zip"

    write_run_data_export(
        report, str(target), ai_values={},
        options=RunDataExportOptions(True, True, False, False),
        clock=lambda: EXPORTED_AT,
    )

    with ZipFile(target) as archive:
        root = json.loads(archive.read("export_manifest.json"))
        record_path = root["records"][0]["manifest"]
        per_record = json.loads(archive.read(record_path))
        assert root["skips"][0]["reason"] == "x" * 500
        assert per_record["audio"]["reason"] == "x" * 500


def test_explicit_zip_crc_bad_member_failure_preserves_target_and_cleans_temp(
    tmp_path, monkeypatch
):
    import app.service.run_record_export.package as package_module

    source = tmp_path / "source.wav"
    _write_wav(source)
    target = tmp_path / "crc-failure.zip"
    target.write_bytes(b"existing")
    real_open = package_module.zipfile.ZipFile.open

    class BadCrcReader:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def read(self, _size=-1):
            raise BadZipFile("Bad CRC-32 for file 'records/bad.wav'")

    def bad_crc_open(self, name, mode="r", *args, **kwargs):
        stream = real_open(self, name, mode, *args, **kwargs)
        member_name = getattr(name, "filename", str(name))
        if self.mode == "r" and member_name.endswith("/audio/CH1.wav"):
            return BadCrcReader(stream)
        return stream

    monkeypatch.setattr(package_module.zipfile.ZipFile, "open", bad_crc_open)

    with pytest.raises(Exception, match="failed CRC"):
        write_run_data_export(
            _report(_record(0, source)), str(target), ai_values={},
            options=RunDataExportOptions(False, True, False, False),
            clock=lambda: EXPORTED_AT,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []


def test_multiple_realistic_audio_records_publish_from_cleaned_staging(
    tmp_path, monkeypatch
):
    import app.service.run_record_export.package as package_module

    records = []
    for index in range(3):
        source = tmp_path / f"source-{index}.wav"
        _write_wav(source, frames=550_000, channels=2)
        record = _record(index, source, sn=f"SN-{index}")
        object.__setattr__(
            record,
            "audio",
            AudioExportFacts(str(source), 8_000, 2, 550_000, (0, 1), None),
        )
        records.append(record)
    target = tmp_path / "multiple-audio.zip"
    staged_audio_members = []
    real_write_member = package_module._write_zip_member

    def inspect_member(archive, member, cancel_check):
        if member.name.endswith(".wav"):
            assert isinstance(member.payload, Path)
            assert member.payload.is_file()
            staged_audio_members.append(member.name)
        return real_write_member(archive, member, cancel_check)

    monkeypatch.setattr(package_module, "_write_zip_member", inspect_member)

    result = write_run_data_export(
        _report(*records), str(target), ai_values={},
        options=RunDataExportOptions(False, True, False, False),
        clock=lambda: EXPORTED_AT,
    )

    assert result.counts.audio_files_emitted == 6
    assert len(staged_audio_members) == 6
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len([name for name in archive.namelist() if name.endswith(".wav")]) == 6
    assert list(tmp_path.glob(".run-export-stage-*")) == []


def test_large_selection_cancellation_during_excel_row_preserves_existing_target(
    tmp_path,
):
    records = [
        _record(index, None, sn=f"SN-{index}", audio_issue="missing")
        for index in range(2_000)
    ]
    target = tmp_path / "large-row-cancel.xlsx"
    target.write_bytes(b"existing")
    excel_phase = False
    row_checks = 0

    def progress(completed, _total, _message):
        nonlocal excel_phase
        if completed == len(records):
            excel_phase = True

    def cancel():
        nonlocal row_checks
        if excel_phase:
            row_checks += 1
            if row_checks == 100:
                raise RuntimeError("Excel row cancellation")

    with pytest.raises(RuntimeError, match="Excel row cancellation"):
        write_run_data_export(
            _report(*records), str(target), ai_values={},
            options=RunDataExportOptions(True, False, False, False),
            clock=lambda: EXPORTED_AT, progress_callback=progress,
            cancel_check=cancel,
        )

    assert row_checks == 100
    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []
    assert list(tmp_path.glob(".run-export-stage-*")) == []


@pytest.mark.parametrize(
    "boundary",
    ["excel", "record_manifest", "root_manifest", "closure", "package_write"],
)
def test_cancellation_checkpoint_after_each_fallible_builder_boundary(
    tmp_path, monkeypatch, boundary
):
    import app.service.run_record_export.package as package_module

    target = tmp_path / f"boundary-{boundary}.zip"
    target.write_bytes(b"existing")
    armed = False
    member_count = 0

    if boundary == "excel":
        real = package_module.build_excel_workbook
        def wrap(*args, **kwargs):
            nonlocal armed
            result = real(*args, **kwargs)
            armed = True
            return result
        monkeypatch.setattr(package_module, "build_excel_workbook", wrap)
    elif boundary == "record_manifest":
        real = package_module.build_record_manifest
        def wrap(*args, **kwargs):
            nonlocal armed
            result = real(*args, **kwargs)
            armed = True
            return result
        monkeypatch.setattr(package_module, "build_record_manifest", wrap)
    elif boundary == "root_manifest":
        real = package_module.build_export_manifest
        def wrap(*args, **kwargs):
            nonlocal armed
            result = real(*args, **kwargs)
            armed = True
            return result
        monkeypatch.setattr(package_module, "build_export_manifest", wrap)
    elif boundary == "closure":
        real = package_module.validate_declared_members
        def wrap(*args, **kwargs):
            nonlocal armed
            result = real(*args, **kwargs)
            armed = True
            return result
        monkeypatch.setattr(package_module, "validate_declared_members", wrap)
    else:
        real = package_module._write_zip_member
        def wrap(*args, **kwargs):
            nonlocal armed, member_count
            result = real(*args, **kwargs)
            member_count += 1
            if member_count == 4:
                armed = True
            return result
        monkeypatch.setattr(package_module, "_write_zip_member", wrap)

    def cancel():
        if armed:
            raise RuntimeError(f"cancel after {boundary}")

    with pytest.raises(RuntimeError, match=f"cancel after {boundary}"):
        write_run_data_export(
            _report(_record(0, None, audio_issue="missing")), str(target),
            ai_values={},
            options=RunDataExportOptions(True, False, True, False),
            database_bytes=b"database", clock=lambda: EXPORTED_AT,
            cancel_check=cancel,
        )

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []
    assert list(tmp_path.glob(".run-export-stage-*")) == []


def test_large_selection_streams_large_member_and_reports_monotonic_progress(tmp_path, monkeypatch):
    import app.service.run_record_export.package as package_module
    source = tmp_path / "large.wav"
    _write_wav(source, frames=1_100_000, channels=1)
    records = [
        _record(index, None, sn=f"SN-{index}", audio_issue="missing")
        for index in range(2_000)
    ]
    object.__setattr__(records[0], "audio", AudioExportFacts(str(source), 8_000, 1, 1_100_000, (0,), None))
    progress, streamed, writestr_sizes = [], [], []
    real_zip = package_module.zipfile.ZipFile
    class RecordingZip(real_zip):
        def open(self, name, mode="r", *args, **kwargs):
            if mode == "w":
                streamed.append(str(name))
            return super().open(name, mode, *args, **kwargs)
        def writestr(self, zinfo_or_arcname, data, *args, **kwargs):
            writestr_sizes.append(len(data))
            return super().writestr(zinfo_or_arcname, data, *args, **kwargs)
    monkeypatch.setattr(package_module.zipfile, "ZipFile", RecordingZip)
    target = tmp_path / "large_selection.zip"
    result = write_run_data_export(
        _report(*records), str(target), ai_values={},
        options=RunDataExportOptions(True, True, False, False), clock=lambda: EXPORTED_AT,
        progress_callback=lambda completed, total, message: progress.append((completed, total, message)),
    )
    assert result.counts.exported_runs == 2_000
    assert any(name.endswith("/audio/CH1.wav") for name in streamed)
    assert max(writestr_sizes, default=0) <= 1024 * 1024
    assert [item[0] for item in progress] == sorted(item[0] for item in progress)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len(json.loads(archive.read("export_manifest.json"))["records"]) == 2_000
