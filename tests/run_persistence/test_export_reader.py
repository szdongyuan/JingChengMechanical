from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.service.run_persistence import database as database_module
from app.service.run_persistence.contracts import (
    CurrentTrace,
    IngressKind,
    Judgment,
    RunInsert,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.export_reader import (
    RunExportReadError,
    RunExportReader,
)
from app.service.run_persistence.run_repository import RunRepository, RunTerminal
from app.service.run_persistence.contracts import RunState
from app.service.run_persistence.startup import settle_startup
from app.service.run_record_export.contracts import RunExportRecord


ACCEPTED_AT = "2026-07-27T12:34:56.123456Z"
TERMINAL_AT = "2026-07-27T12:35:56.123456Z"


def _ready_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    assert settle_startup(runtime, ACCEPTED_AT).ready
    return runtime


def _analysis_envelope(
    channel_order: tuple[int, ...] = (0,),
    *,
    fault_types: tuple[str, ...] = (),
    workorder: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": "analysis-input/v1",
        "config": {
            "config_id": 7,
            "label": "配置",
            "fault_types": list(fault_types),
        },
        "workorder": (
            {"workorder_id": "WO-1"}
            if workorder is None
            else dict(workorder)
        ),
        "channel_mapping": {
            "schema": "audio-channel-map/v1",
            "channel_order": list(channel_order),
        },
        "analysis": {
            "window_start_seconds": 0.0,
            "window_end_seconds": 3.0,
            "algorithm_revision": "revision",
            "execution_timeout_seconds": 30.0,
            "queue_timeout_seconds": None,
            "toolchain": None,
        },
    }


def _insert_active(
    runtime: RunPersistenceDatabase,
    *,
    run_id: str | None = None,
    ingress_kind: IngressKind = IngressKind.CAPTURED,
    channel_order: tuple[int, ...] = (0,),
    fault_types: tuple[str, ...] = (),
    hardware_id: int | None = 12,
    workorder: dict[str, object] | None = None,
) -> str:
    assigned = str(uuid4()) if run_id is None else run_id
    if ingress_kind is IngressKind.IMPORTED:
        hardware_id = None
    inserted = RunRepository(runtime).insert_active(
        RunInsert(
            run_id=assigned,
            ingress_kind=ingress_kind,
            trigger_source=(
                TriggerSource.IMPORT
                if ingress_kind is IngressKind.IMPORTED
                else TriggerSource.PLC
            ),
            config_id=7,
            hardware_id=hardware_id,
            imported_source_identity=(
                f"sha256:{assigned}"
                if ingress_kind is IngressKind.IMPORTED
                else None
            ),
            imported_source_path=(
                rf"C:\managed\{assigned}-source.wav"
                if ingress_kind is IngressKind.IMPORTED
                else None
            ),
            imported_expected_frame_count=(
                16 if ingress_kind is IngressKind.IMPORTED else None
            ),
            snapshots=freeze_run_inputs(
                _analysis_envelope(
                    channel_order,
                    fault_types=fault_types,
                    workorder=workorder,
                )
            ),
            accepted_at=ACCEPTED_AT,
        )
    )
    assert inserted.value is True
    return assigned


def _finish(
    runtime: RunPersistenceDatabase,
    run_id: str,
    *,
    judgment: Judgment = Judgment.PASS,
) -> None:
    assert RunRepository(runtime).finish_active(
        run_id,
        RunTerminal(
            state=RunState.SUCCEEDED,
            code="ANALYSIS_SUCCEEDED",
            summary="complete",
            terminal_at=TERMINAL_AT,
            judgment=judgment,
        ),
    ).value


def _insert_audio(
    runtime: RunPersistenceDatabase,
    run_id: str,
    source_path: Path,
    *,
    channel_count: int = 1,
    frame_count: int = 16,
    source_kind: str = "CAPTURED_AUDIO",
) -> str:
    audio_id = str(uuid4())
    with runtime.operation() as database:
        database.execute_sql(
            """
            INSERT INTO audio (
                audio_id, save_job_id, run_id, source_kind, source_identity,
                content_revision, final_path, container_format,
                sample_rate_hz, channel_count, frame_count, duration_ms,
                byte_size, closed_verified_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 'WAV_PCM', 8000, ?, ?, 2, 44, ?, ?)
            """,
            (
                audio_id,
                str(uuid4()),
                run_id,
                source_kind,
                (
                    f"sha256:{run_id}"
                    if source_kind == "IMPORTED_SOURCE"
                    else f"managed-input:{run_id}"
                ),
                str(source_path),
                channel_count,
                frame_count,
                TERMINAL_AT,
                TERMINAL_AT,
            ),
        )
    return audio_id


def _insert_attempt(
    runtime: RunPersistenceDatabase,
    run_id: str,
    audio_id: str,
) -> str:
    attempt_id = str(uuid4())
    with runtime.operation() as database:
        database.execute_sql(
            """
            INSERT INTO analysis_attempt (
                attempt_id, run_id, audio_id, attempt_kind, state, judgment,
                result_payload_json, error_code, error_summary, queued_at,
                started_at, terminal_at, updated_at
            ) VALUES (
                ?, ?, ?, 'INITIAL', 'SUCCEEDED', 'PASS', ?, NULL, NULL,
                ?, ?, ?, ?
            )
            """,
            (
                attempt_id,
                run_id,
                audio_id,
                '{"result":{"summary_payload":{"rows":[]}}}',
                ACCEPTED_AT,
                ACCEPTED_AT,
                TERMINAL_AT,
                TERMINAL_AT,
            ),
        )
    return attempt_id


def _set_attempt_result_payload(
    runtime: RunPersistenceDatabase,
    attempt_id: str,
    payload: dict[str, object] | None,
) -> None:
    with runtime.operation() as database:
        database.execute_sql(
            "UPDATE analysis_attempt SET result_payload_json = ? "
            "WHERE attempt_id = ?",
            (
                None
                if payload is None
                else json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                attempt_id,
            ),
        )


def _replace_config(
    runtime: RunPersistenceDatabase,
    run_id: str,
    envelope: dict[str, object],
) -> None:
    with runtime.operation() as database:
        database.execute_sql(
            "UPDATE run SET config_snapshot_json = ? WHERE run_id = ?",
            (json.dumps(envelope, separators=(",", ":")), run_id),
        )


def _guard_source_io(
    monkeypatch: pytest.MonkeyPatch,
    paths: set[str],
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    real_open = builtins.open
    real_io_open = io.open
    real_stat = os.stat
    real_lstat = os.lstat

    def path_text(value: object) -> str | None:
        try:
            return os.fspath(value)
        except TypeError:
            return None

    def guarded_open(file, *args, **kwargs):
        candidate = path_text(file)
        if candidate in paths:
            calls.append(("open", candidate))
            raise AssertionError("reader must not open a source WAV")
        return real_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):
        candidate = path_text(file)
        if candidate in paths:
            calls.append(("open", candidate))
            raise AssertionError("reader must not open a source WAV")
        return real_io_open(file, *args, **kwargs)

    def guarded_stat(path, *args, **kwargs):
        candidate = path_text(path)
        if candidate in paths:
            calls.append(("stat", candidate))
            raise AssertionError("reader must not stat a source WAV")
        return real_stat(path, *args, **kwargs)

    def guarded_lstat(path, *args, **kwargs):
        candidate = path_text(path)
        if candidate in paths:
            calls.append(("stat", candidate))
            raise AssertionError("reader must not stat a source WAV")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "stat", guarded_stat)
    monkeypatch.setattr(os, "lstat", guarded_lstat)
    return calls


def test_list_durable_includes_active_and_terminal_pre_audio_rows(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    active_id = _insert_active(runtime)
    terminal_id = _insert_active(
        runtime,
        ingress_kind=IngressKind.IMPORTED,
    )
    _finish(runtime, terminal_id)

    before = {}
    with runtime.operation() as database:
        for table in ("run", "audio", "analysis_attempt"):
            before[table] = database.execute_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

    rows = RunExportReader(runtime).list_durable()

    by_id = {row.run_id: row for row in rows}
    assert set(by_id) == {active_id, terminal_id}
    assert by_id[active_id].state == "ACTIVE"
    assert by_id[active_id].hardware_id == 12
    assert by_id[terminal_id].state == "SUCCEEDED"
    assert by_id[terminal_id].ingress_kind == "IMPORTED"
    assert by_id[terminal_id].hardware_id is None
    assert by_id[terminal_id].terminal_at == TERMINAL_AT
    with runtime.operation() as database:
        after = {
            table: database.execute_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("run", "audio", "analysis_attempt")
        }
    assert after == before


def test_read_many_returns_one_closed_terminal_export_value(
    prepared_database_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    first_id = _insert_active(
        runtime,
        channel_order=(4, 1),
        workorder={
            "sn": "  SN-主值-001  ",
            "sn_code": "SN-备用值",
            "model": "  型号-主值  ",
            "product_model": "型号-备用值",
        },
        fault_types=("裂纹",),
    )
    assert RunRepository(runtime).attach_current_trace(
        first_id,
        CurrentTrace(samples=((0.0, 1.25), (0.5, 1.75))),
    ).value
    _finish(runtime, first_id)
    assert RunRepository(runtime).set_manual_judgment(
        first_id,
        Judgment.FAIL,
        TERMINAL_AT,
        expected_revision=0,
        fault_type="裂纹",
        accepted_fault_types=("裂纹",),
    ).value
    first_source = tmp_path / "durable-first.wav"
    _insert_audio(
        runtime,
        first_id,
        first_source,
        channel_count=2,
    )

    second_id = _insert_active(
        runtime,
        ingress_kind=IngressKind.IMPORTED,
        channel_order=(9,),
        workorder={
            "sn": "   ",
            "sn_code": "  SN-备用-002  ",
            "model": 123,
            "product_model": "  型号-备用  ",
        },
    )
    _finish(runtime, second_id)
    second_source = tmp_path / "durable-second.wav"
    _insert_audio(
        runtime,
        second_id,
        second_source,
        source_kind="IMPORTED_SOURCE",
    )

    calls = _guard_source_io(
        monkeypatch,
        {str(first_source), str(second_source)},
    )
    database_opens = []
    real_database_open = database_module._open_existing_database

    def tracked_database_open(path):
        database = real_database_open(path)
        database_opens.append(database)
        return database

    monkeypatch.setattr(
        database_module,
        "_open_existing_database",
        tracked_database_open,
    )

    records = RunExportReader(runtime).read_many((second_id, first_id))

    assert tuple(record.run_id for record in records) == (second_id, first_id)
    assert all(type(record) is RunExportRecord for record in records)
    second, first = records
    assert second.hardware_id is None
    assert second.sn == "SN-备用-002"
    assert second.model == "型号-备用"
    assert second.ingress_kind == "IMPORTED"
    assert second.source_wav_path == str(second_source)
    assert second.channel_order == (9,)
    assert second.current_samples is None
    assert first.hardware_id == 12
    assert first.sn == "SN-主值-001"
    assert first.model == "型号-主值"
    assert first.state == "SUCCEEDED"
    assert first.judgment == "FAIL"
    assert first.judgment_source == "MANUAL"
    assert first.fault_type == "裂纹"
    assert first.manual_judgment_revision == 1
    assert first.terminal_code == "ANALYSIS_SUCCEEDED"
    assert first.source_wav_path == str(first_source)
    assert first.sample_rate_hz == 8000
    assert first.channel_count == 2
    assert first.frame_count == 16
    assert first.channel_order == (4, 1)
    assert first.current_samples == ((0.0, 1.25), (0.5, 1.75))
    assert len(database_opens) == 1
    assert database_opens[0].is_closed()
    assert calls == []


def test_read_many_report_returns_exportable_records_and_skips_bad_facts(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)

    bad_id = _insert_active(runtime)
    _finish(runtime, bad_id)

    good_id = _insert_active(runtime)
    _finish(runtime, good_id)
    _insert_audio(runtime, good_id, tmp_path / "good.wav")

    report = RunExportReader(runtime).read_many_report((bad_id, good_id))

    assert tuple(record.run_id for record in report.records) == (good_id,)
    assert len(report.skipped) == 1
    assert report.skipped[0].run_id == bad_id
    assert (
        "terminal run has no committed audio value"
        in report.skipped[0].reason
    )


def test_read_copy_database_exports_selected_run_audio_and_attempt_rows(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    selected_id = _insert_active(runtime)
    _finish(runtime, selected_id)
    selected_audio = _insert_audio(
        runtime,
        selected_id,
        tmp_path / "selected.wav",
    )
    selected_attempt = _insert_attempt(runtime, selected_id, selected_audio)

    other_id = _insert_active(runtime)
    _finish(runtime, other_id)
    other_audio = _insert_audio(runtime, other_id, tmp_path / "other.wav")
    _insert_attempt(runtime, other_id, other_audio)

    payload = RunExportReader(runtime).read_copy_database((selected_id,))

    copied = sqlite3.connect(":memory:")
    try:
        copied.deserialize(payload)
        assert {
            row[0]
            for row in copied.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        } == {"run", "audio", "analysis_attempt"}
        assert copied.execute("SELECT run_id FROM run").fetchall() == [
            (selected_id,)
        ]
        assert copied.execute("SELECT run_id, audio_id FROM audio").fetchall() == [
            (selected_id, selected_audio)
        ]
        assert copied.execute(
            "SELECT attempt_id, run_id, audio_id FROM analysis_attempt"
        ).fetchall() == [(selected_attempt, selected_id, selected_audio)]
        assert copied.execute(
            "SELECT COUNT(*) FROM run WHERE run_id = ?",
            (other_id,),
        ).fetchone() == (0,)
        assert copied.execute(
            "SELECT COUNT(*) FROM audio WHERE audio_id = ?",
            (other_audio,),
        ).fetchone() == (0,)
    finally:
        copied.close()


def test_read_copy_database_can_be_opened_as_plain_sqlite_file(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    selected_id = _insert_active(runtime)
    _finish(runtime, selected_id)
    _insert_audio(runtime, selected_id, tmp_path / "selected.wav")

    payload = RunExportReader(runtime).read_copy_database((selected_id,))
    target = tmp_path / "copy_data.sqlite3"
    target.write_bytes(payload)

    copied = sqlite3.connect(target)
    try:
        assert copied.execute("SELECT run_id FROM run").fetchall() == [
            (selected_id,)
        ]
    finally:
        copied.close()


def test_read_many_uses_empty_identity_when_frozen_workorder_omits_it(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_active(runtime)
    _finish(runtime, run_id)
    source = tmp_path / "missing-identity.wav"
    _insert_audio(runtime, run_id, source)

    (record,) = RunExportReader(runtime).read_many((run_id,))

    assert record.sn == ""
    assert record.model == ""


def test_read_many_rejects_invalid_selection_and_malformed_facts_before_file_io(
    prepared_database_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    reader = RunExportReader(runtime)

    valid_id = _insert_active(runtime)
    _finish(runtime, valid_id)
    valid_source = tmp_path / "valid-never-open.wav"
    _insert_audio(runtime, valid_id, valid_source)

    active_id = _insert_active(runtime)
    audio_less_id = _insert_active(runtime)
    _finish(runtime, audio_less_id)

    malformed_current_id = _insert_active(runtime)
    _finish(runtime, malformed_current_id)
    malformed_current_source = tmp_path / "current-never-open.wav"
    _insert_audio(runtime, malformed_current_id, malformed_current_source)
    with runtime.operation() as database:
        database.execute_sql(
            "UPDATE run SET current_trace_json = ? WHERE run_id = ?",
            (
                '{"samples":[[0.0,true]],"schema":"run-current-trace/v1",'
                '"unit":"A"}',
                malformed_current_id,
            ),
        )

    missing_channel_id = _insert_active(runtime)
    _finish(runtime, missing_channel_id)
    missing_channel_source = tmp_path / "missing-channel-never-open.wav"
    _insert_audio(runtime, missing_channel_id, missing_channel_source)
    missing_channel = _analysis_envelope()
    missing_channel["channel_mapping"] = {
        "schema": "audio-channel-map/v1"
    }
    _replace_config(runtime, missing_channel_id, missing_channel)

    malformed_channel_id = _insert_active(runtime)
    _finish(runtime, malformed_channel_id)
    malformed_channel_source = tmp_path / "malformed-channel-never-open.wav"
    _insert_audio(runtime, malformed_channel_id, malformed_channel_source)
    malformed_channel = _analysis_envelope()
    malformed_channel["channel_mapping"] = {
        "schema": "audio-channel-map/v1",
        "channel_order": [True],
    }
    _replace_config(runtime, malformed_channel_id, malformed_channel)

    duplicate_channel_id = _insert_active(runtime)
    _finish(runtime, duplicate_channel_id)
    duplicate_channel_source = tmp_path / "duplicate-channel-never-open.wav"
    _insert_audio(
        runtime,
        duplicate_channel_id,
        duplicate_channel_source,
        channel_count=2,
    )
    duplicate_channel = _analysis_envelope()
    duplicate_channel["channel_mapping"] = {
        "schema": "audio-channel-map/v1",
        "channel_order": [0, 0],
    }
    _replace_config(runtime, duplicate_channel_id, duplicate_channel)

    malformed_audio_id = _insert_active(runtime)
    _finish(runtime, malformed_audio_id)
    malformed_audio_source = tmp_path / "malformed-audio-never-open.wav"
    _insert_audio(
        runtime,
        malformed_audio_id,
        malformed_audio_source,
        channel_count=2,
    )

    malformed_judgment_id = _insert_active(runtime)
    _finish(runtime, malformed_judgment_id)
    malformed_judgment_source = tmp_path / "judgment-never-open.wav"
    _insert_audio(
        runtime,
        malformed_judgment_id,
        malformed_judgment_source,
    )
    with runtime.operation() as database:
        database.execute_sql("PRAGMA ignore_check_constraints = ON")
        database.execute_sql(
            "UPDATE run SET judgment = 'BROKEN' WHERE run_id = ?",
            (malformed_judgment_id,),
        )

    malformed_fault_id = _insert_active(
        runtime,
        fault_types=("裂纹",),
    )
    _finish(runtime, malformed_fault_id)
    malformed_fault_source = tmp_path / "fault-never-open.wav"
    _insert_audio(runtime, malformed_fault_id, malformed_fault_source)
    with runtime.operation() as database:
        database.execute_sql(
            """
            UPDATE run
            SET judgment = 'FAIL', judgment_source = 'MANUAL',
                fault_type = '剥落', manual_judgment_revision = 1
            WHERE run_id = ?
            """,
            (malformed_fault_id,),
        )

    malformed_revision_id = _insert_active(
        runtime,
        fault_types=("裂纹",),
    )
    _finish(runtime, malformed_revision_id)
    malformed_revision_source = tmp_path / "revision-never-open.wav"
    _insert_audio(runtime, malformed_revision_id, malformed_revision_source)
    with runtime.operation() as database:
        database.execute_sql("PRAGMA ignore_check_constraints = ON")
        database.execute_sql(
            """
            UPDATE run
            SET judgment = 'FAIL', judgment_source = 'MANUAL',
                fault_type = '裂纹', manual_judgment_revision = 0
            WHERE run_id = ?
            """,
            (malformed_revision_id,),
        )

    malformed_run_id = _insert_active(runtime)
    _finish(runtime, malformed_run_id)
    malformed_run_source = tmp_path / "run-never-open.wav"
    _insert_audio(runtime, malformed_run_id, malformed_run_source)
    with runtime.operation() as database:
        database.execute_sql("PRAGMA ignore_check_constraints = ON")
        database.execute_sql(
            "UPDATE run SET hardware_id = NULL WHERE run_id = ?",
            (malformed_run_id,),
        )

    sources = {
        str(path)
        for path in (
            valid_source,
            malformed_current_source,
            missing_channel_source,
            malformed_channel_source,
            duplicate_channel_source,
            malformed_audio_source,
            malformed_judgment_source,
            malformed_fault_source,
            malformed_revision_source,
            malformed_run_source,
        )
    }
    calls = _guard_source_io(monkeypatch, sources)
    absent_id = str(uuid4())

    invalid_selections = (
        (),
        (valid_id, valid_id),
        (absent_id,),
        (active_id,),
        (audio_less_id,),
        (malformed_current_id,),
        (missing_channel_id,),
        (malformed_channel_id,),
        (duplicate_channel_id,),
        (malformed_audio_id,),
        (malformed_judgment_id,),
        (malformed_fault_id,),
        (malformed_revision_id,),
        (malformed_run_id,),
    )
    for selection in invalid_selections:
        with pytest.raises(RunExportReadError):
            reader.read_many(selection)

    assert calls == []


def test_pending_enrichment_reads_latest_durable_attempt_payload(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_active(runtime)
    _finish(runtime, run_id)
    audio_id = _insert_audio(runtime, run_id, tmp_path / "pending.wav")
    attempt_id = _insert_attempt(runtime, run_id, audio_id)
    artifact_store = AnalysisResultArtifactStore(tmp_path / "run_results")
    artifact_store.ensure_ready()
    reader = RunExportReader(
        runtime,
        result_artifact_store=artifact_store,
    )
    lightweight = {
        "judgment": "PASS",
        "result": {
            "chart_payload": {},
            "summary_payload": {"overall_status": "pass", "rows": []},
        },
    }
    _set_attempt_result_payload(runtime, attempt_id, lightweight)

    assert reader.pending_enrichment_run_ids((run_id,)) == (run_id,)

    full = json.loads(json.dumps(lightweight))
    full["result"]["chart_payload"] = {
        "0": {"fft": {"frequencies_hz": [100.0], "levels_db": [-3.0]}}
    }
    full_json = json.dumps(
        full,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    published = artifact_store.publish(attempt_id, "PASS", full_json)
    with runtime.operation() as database:
        database.execute_sql(
            "UPDATE analysis_attempt SET result_payload_json = ? "
            "WHERE attempt_id = ?",
            (published.reference_json, attempt_id),
        )
    artifact_store.settle_publication(published, retain=True)

    assert reader.pending_enrichment_run_ids((run_id,)) == ()
