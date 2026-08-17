from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

import pytest

import app.service.run_persistence.data_management as data_management_module
from app.service.run_persistence.contracts import (
    ContractValidationError,
    TransactionResult,
    TransactionState,
)
from app.service.run_persistence.data_management import (
    DeleteBatchReport,
    DeleteRunResult,
    DeleteRunStatus,
    ManagementJudgment,
    RunDataManagementService,
    RunManagementPage,
    RunManagementQuery,
    RunManagementRow,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.startup import settle_startup
from app.service.run_persistence.transactions import run_short_transaction


ACCEPTED_AT = "2026-07-27T12:34:56.123456Z"
TERMINAL_AT = "2026-07-27T12:35:56.123456Z"
ALL_JUDGMENTS = frozenset(ManagementJudgment)


def _ready_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    assert settle_startup(runtime, ACCEPTED_AT).ready
    return runtime


def _query(**overrides: object) -> RunManagementQuery:
    values: dict[str, object] = {
        "model": "",
        "sn_contains": "",
        "accepted_start_utc": None,
        "accepted_end_exclusive_utc": None,
        "judgments": ALL_JUDGMENTS,
        "page": 1,
        "page_size": 20,
    }
    values.update(overrides)
    return RunManagementQuery(**values)


def _insert_run(
    runtime: RunPersistenceDatabase,
    *,
    run_id: str | None = None,
    accepted_at: str = ACCEPTED_AT,
    workorder: object = None,
    state: str = "SUCCEEDED",
    judgment: str | None = "PASS",
    judgment_source: str = "AUTOMATIC",
    ingress_kind: str = "CAPTURED",
    imported_source_path: str | None = None,
) -> str:
    assigned = str(uuid4()) if run_id is None else run_id
    if workorder is None:
        workorder = {}
    config_snapshot_json = json.dumps(
        {"workorder": workorder},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    terminal_at = None if state == "ACTIVE" else TERMINAL_AT
    terminal_code = None if state == "ACTIVE" else "DONE"
    if state == "ACTIVE":
        judgment = None
        judgment_source = "NONE"
    with runtime.operation() as database:
        database.execute_sql(
            """
            INSERT INTO run (
                run_id, ingress_kind, trigger_source, config_id, hardware_id,
                imported_source_identity, imported_source_path,
                imported_expected_frame_count, causal_revision,
                config_snapshot_json, hardware_snapshot_json,
                calibration_snapshot_json, current_trace_json, state,
                judgment, judgment_source, fault_type,
                manual_judgment_revision, judged_at, terminal_code,
                terminal_summary, accepted_at, terminal_at, updated_at
            ) VALUES (
                ?, ?, ?, 7, ?, ?, ?, ?, 0,
                ?, '{}', '{}', NULL, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?, ?
            )
            """,
            (
                assigned,
                ingress_kind,
                "IMPORT" if ingress_kind == "IMPORTED" else "PLC",
                None if ingress_kind == "IMPORTED" else 12,
                f"sha256:{assigned}" if ingress_kind == "IMPORTED" else None,
                imported_source_path,
                16 if ingress_kind == "IMPORTED" else None,
                config_snapshot_json,
                state,
                judgment,
                judgment_source,
                terminal_at if judgment is not None else None,
                terminal_code,
                accepted_at,
                terminal_at,
                terminal_at or accepted_at,
            ),
        )
    return assigned


def _row(**overrides: object) -> RunManagementRow:
    values: dict[str, object] = {
        "run_id": str(uuid4()),
        "accepted_at": ACCEPTED_AT,
        "sn": "",
        "model": "",
        "ingress_kind": "CAPTURED",
        "trigger_source": "PLC",
        "config_id": 7,
        "hardware_id": "12",
        "state": "SUCCEEDED",
        "judgment": "PASS",
        "judgment_source": "AUTOMATIC",
        "fault_type": None,
        "manual_judgment_revision": None,
        "terminal_code": "DONE",
        "terminal_at": TERMINAL_AT,
    }
    values.update(overrides)
    return RunManagementRow(**values)


def test_query_contracts_are_frozen_slotted_and_validate_exact_values() -> None:
    request = _query()
    assert request.judgments == ALL_JUDGMENTS
    with pytest.raises((AttributeError, TypeError)):
        request.page = 2  # type: ignore[misc]
    assert not hasattr(request, "__dict__")

    invalid = (
        {"model": 1},
        {"sn_contains": None},
        {"fault_type_contains": None},
        {"accepted_start_utc": "2026-07-27T00:00:00Z"},
        {"accepted_end_exclusive_utc": "2026-07-27T00:00:00.000000+00:00"},
        {"judgments": set(ALL_JUDGMENTS)},
        {"judgments": frozenset({"OK"})},
        {"page": True},
        {"page": 0},
        {"page_size": 10},
    )
    for override in invalid:
        with pytest.raises((ContractValidationError, TypeError, ValueError)):
            _query(**override)

    with pytest.raises(ContractValidationError, match="start"):
        _query(
            accepted_start_utc="2026-07-27T12:00:00.000000Z",
            accepted_end_exclusive_utc="2026-07-27T12:00:00.000000Z",
        )


def test_query_filters_saved_fault_type_across_configurations(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    bearing = _insert_run(runtime, accepted_at=ACCEPTED_AT)
    winding = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:55.123456Z",
    )
    with runtime.operation() as database:
        database.execute_sql(
            """
            UPDATE run
            SET judgment = 'FAIL',
                judgment_source = 'MANUAL',
                manual_judgment_revision = 1,
                fault_type = ?
            WHERE run_id = ?
            """,
            ("轴承异响", bearing),
        )
        database.execute_sql(
            """
            UPDATE run
            SET judgment = 'FAIL',
                judgment_source = 'MANUAL',
                manual_judgment_revision = 1,
                fault_type = ?
            WHERE run_id = ?
            """,
            ("绕组异常", winding),
        )

    rows = RunDataManagementService(runtime).query_page(
        _query(fault_type_contains="轴承")
    ).rows

    assert [row.run_id for row in rows] == [bearing]


def test_query_fault_type_good_matches_pass_rows_displayed_as_good(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    fail = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:58.123456Z",
        judgment="FAIL",
    )
    newest_pass = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:57.123456Z",
    )
    oldest_pass = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:55.123456Z",
    )

    rows = RunDataManagementService(runtime).query_page(
        _query(fault_type_contains="良品")
    ).rows

    assert [row.run_id for row in rows] == [newest_pass, oldest_pass]
    assert fail not in {row.run_id for row in rows}


def test_query_fault_type_bad_matches_untyped_fail_rows_displayed_as_bad(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    untyped_fail = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:58.123456Z",
        judgment="FAIL",
    )
    specific_fail = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:57.123456Z",
        judgment="FAIL",
    )
    with runtime.operation() as database:
        database.execute_sql(
            """
            UPDATE run
            SET judgment_source = 'MANUAL',
                manual_judgment_revision = 1,
                fault_type = ?
            WHERE run_id = ?
            """,
            ("轴承异响", specific_fail),
        )

    rows = RunDataManagementService(runtime).query_page(
        _query(fault_type_contains="不良")
    ).rows

    assert [row.run_id for row in rows] == [untyped_fail]
    assert specific_fail not in {row.run_id for row in rows}


def test_result_contracts_are_frozen_slotted_and_exact() -> None:
    row = _row()
    page = RunManagementPage(
        rows=(row,),
        total_count=1,
        page=1,
        page_size=20,
        total_pages=1,
    )
    result = DeleteRunResult(
        run_id=row.run_id,
        status=DeleteRunStatus.DELETED,
        reason="deleted",
        residual_path=None,
        database_committed=True,
    )
    report = DeleteBatchReport(
        results=(result,),
        unattempted_run_ids=(),
        restart_required=False,
    )
    for value in (row, page, result, report):
        assert not hasattr(value, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            value.reason = "changed"  # type: ignore[attr-defined]


def test_query_projects_trimmed_sn_model_fallbacks_and_safe_invalid_json(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    preferred = _insert_run(
        runtime,
        workorder={
            "sn": "  SN-PRIMARY  ",
            "sn_code": "SN-FALLBACK",
            "model": " ",
            "product_model": "  M-42  ",
        },
    )
    fallback = _insert_run(
        runtime,
        workorder={
            "sn": 123,
            "sn_code": "  SN-CODE  ",
            "model": [],
            "product_model": " MODEL-FALLBACK ",
        },
        accepted_at="2026-07-27T12:34:55.123456Z",
    )
    invalid = _insert_run(
        runtime,
        workorder={},
        accepted_at="2026-07-27T12:34:54.123456Z",
    )
    scalar = _insert_run(
        runtime,
        workorder={},
        accepted_at="2026-07-27T12:34:53.123456Z",
    )
    with runtime.operation() as database:
        database.execute_sql("PRAGMA ignore_check_constraints = ON")
        database.execute_sql(
            "UPDATE run SET config_snapshot_json = 'not-json' WHERE run_id = ?",
            (invalid,),
        )
        database.execute_sql(
            "UPDATE run SET config_snapshot_json = '[]' WHERE run_id = ?",
            (scalar,),
        )

    rows = RunDataManagementService(runtime).query_page(_query()).rows
    projected = {row.run_id: (row.sn, row.model) for row in rows}
    assert projected[preferred] == ("SN-PRIMARY", "M-42")
    assert projected[fallback] == ("SN-CODE", "MODEL-FALLBACK")
    assert projected[invalid] == ("", "")
    assert projected[scalar] == ("", "")


def test_query_treats_sn_wildcards_literally_and_model_as_nocase_exact(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    percent = _insert_run(runtime, workorder={"sn": "A%_B", "model": " Pump-X "})
    _insert_run(
        runtime,
        workorder={"sn": "AXXB", "model": "Pump-X-extra"},
        accepted_at="2026-07-27T12:34:55.123456Z",
    )

    service = RunDataManagementService(runtime)
    assert tuple(
        row.run_id
        for row in service.query_page(
            _query(sn_contains=" %_ ", model=" pump-x ")
        ).rows
    ) == (percent,)


@pytest.mark.parametrize(
    ("judgments", "expected"),
    [
        (
            frozenset({ManagementJudgment.OK}),
            {"PASS"},
        ),
        (
            frozenset({ManagementJudgment.NG}),
            {"FAIL"},
        ),
        (
            frozenset({ManagementJudgment.UNJUDGED}),
            {None},
        ),
        (ALL_JUDGMENTS, {"PASS", "FAIL", None}),
        (frozenset(), set()),
    ],
)
def test_query_maps_all_and_zero_judgment_choices(
    prepared_database_path: Path,
    judgments: frozenset[ManagementJudgment],
    expected: set[str | None],
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    _insert_run(runtime, judgment="PASS")
    _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:55.123456Z",
        judgment="FAIL",
    )
    _insert_run(
        runtime,
        accepted_at="2026-07-27T12:34:54.123456Z",
        state="FAILED",
        judgment=None,
        judgment_source="NONE",
    )

    page = RunDataManagementService(runtime).query_page(
        _query(judgments=judgments)
    )
    assert {row.judgment for row in page.rows} == expected
    assert page.total_count == len(expected)


def test_query_uses_inclusive_start_exclusive_end_and_stable_order(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    low_id = "00000000-0000-4000-8000-000000000001"
    high_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    before = _insert_run(
        runtime,
        accepted_at="2026-07-27T11:59:59.999999Z",
    )
    _insert_run(
        runtime,
        run_id=low_id,
        accepted_at="2026-07-27T12:00:00.000000Z",
    )
    _insert_run(
        runtime,
        run_id=high_id,
        accepted_at="2026-07-27T12:00:00.000000Z",
    )
    end = _insert_run(
        runtime,
        accepted_at="2026-07-27T12:00:01.000000Z",
    )

    page = RunDataManagementService(runtime).query_page(
        _query(
            accepted_start_utc="2026-07-27T12:00:00.000000Z",
            accepted_end_exclusive_utc="2026-07-27T12:00:01.000000Z",
        )
    )
    assert tuple(row.run_id for row in page.rows) == (high_id, low_id)
    assert before not in {row.run_id for row in page.rows}
    assert end not in {row.run_id for row in page.rows}


@pytest.mark.parametrize("page_size", [20, 50, 100, 200])
def test_query_accepts_all_page_sizes_and_clamps_after_count(
    prepared_database_path: Path,
    page_size: int,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    _insert_run(runtime)
    page = RunDataManagementService(runtime).query_page(
        _query(page=99, page_size=page_size)
    )
    assert page.page == 1
    assert page.page_size == page_size
    assert page.total_pages == 1
    assert page.total_count == 1


class _BarrierConnection:
    def __init__(
        self,
        database,
        start_insert: threading.Event,
        inserted: threading.Event,
    ) -> None:
        self._database = database
        self._start_insert = start_insert
        self._inserted = inserted

    def __getattr__(self, name: str):
        return getattr(self._database, name)

    def execute_sql(self, sql: str, params=None, commit=None):
        cursor = self._database.execute_sql(sql, params)
        if "COUNT(*)" in sql:
            self._start_insert.set()
            assert self._inserted.wait(timeout=5)
        return cursor


class _BarrierRuntime(RunPersistenceDatabase):
    def __init__(
        self,
        path: Path,
        start_insert: threading.Event,
        inserted: threading.Event,
    ) -> None:
        super().__init__(path)
        self._start_insert = start_insert
        self._inserted = inserted

    @contextmanager
    def operation(self):
        with super().operation() as database:
            yield _BarrierConnection(
                database,
                self._start_insert,
                self._inserted,
            )


def test_query_count_and_rows_share_one_read_snapshot(
    prepared_database_path: Path,
) -> None:
    base = _ready_runtime(prepared_database_path)
    first = _insert_run(base)
    base.close_after_upstream_stopped()

    start_insert = threading.Event()
    inserted = threading.Event()
    writer_done = threading.Event()

    def insert_concurrently() -> None:
        assert start_insert.wait(timeout=5)
        connection = sqlite3.connect(str(prepared_database_path), timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            assigned = str(uuid4())
            connection.execute(
                """
                INSERT INTO run (
                    run_id, ingress_kind, trigger_source, config_id, hardware_id,
                    imported_source_identity, imported_source_path,
                    imported_expected_frame_count, causal_revision,
                    config_snapshot_json, hardware_snapshot_json,
                    calibration_snapshot_json, current_trace_json, state,
                    judgment, judgment_source, fault_type,
                    manual_judgment_revision, judged_at, terminal_code,
                    terminal_summary, accepted_at, terminal_at, updated_at
                ) VALUES (
                    ?, 'CAPTURED', 'PLC', 7, 12, NULL, NULL, NULL, 0,
                    '{"workorder":{}}', '{}', '{}', NULL, 'SUCCEEDED',
                    'PASS', 'AUTOMATIC', NULL, NULL, ?, 'DONE', NULL, ?, ?, ?
                )
                """,
                (assigned, TERMINAL_AT, ACCEPTED_AT, TERMINAL_AT, TERMINAL_AT),
            )
            inserted.set()
            connection.commit()
            writer_done.set()
        finally:
            connection.close()

    writer = threading.Thread(target=insert_concurrently)
    writer.start()
    runtime = _BarrierRuntime(prepared_database_path, start_insert, inserted)
    assert settle_startup(runtime, ACCEPTED_AT).ready

    page = RunDataManagementService(runtime).query_page(_query())
    writer.join(timeout=5)
    assert writer_done.is_set()
    assert page.total_count == 1
    assert tuple(row.run_id for row in page.rows) == (first,)
    assert RunDataManagementService(runtime).query_page(_query()).total_count == 2


def _insert_audio(
    runtime: RunPersistenceDatabase,
    run_id: str,
    final_path: Path,
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
            ) VALUES (
                ?, ?, ?, 'CAPTURED_AUDIO', ?, 1, ?, 'WAV_PCM',
                8000, 1, 16, 2, 44, ?, ?
            )
            """,
            (
                audio_id,
                str(uuid4()),
                run_id,
                f"managed-input:{run_id}",
                str(final_path.resolve()),
                TERMINAL_AT,
                TERMINAL_AT,
            ),
        )
    return audio_id


def _insert_attempt(
    runtime: RunPersistenceDatabase,
    run_id: str,
    audio_id: str,
    state: str,
) -> str:
    attempt_id = str(uuid4())
    started_at = TERMINAL_AT if state == "RUNNING" else None
    terminal_at = TERMINAL_AT if state not in {"QUEUED", "RUNNING"} else None
    error_code = "DONE" if terminal_at else None
    with runtime.operation() as database:
        database.execute_sql(
            """
            INSERT INTO analysis_attempt (
                attempt_id, run_id, audio_id, attempt_kind, state,
                judgment, result_payload_json, error_code, error_summary,
                queued_at, started_at, terminal_at, updated_at
            ) VALUES (
                ?, ?, ?, 'REANALYSIS', ?, NULL, NULL, ?, NULL,
                ?, ?, ?, ?
            )
            """,
            (
                attempt_id,
                run_id,
                audio_id,
                state,
                error_code,
                ACCEPTED_AT,
                started_at,
                terminal_at,
                TERMINAL_AT,
            ),
        )
    return attempt_id


def _counts(
    runtime: RunPersistenceDatabase,
    run_id: str,
) -> tuple[int, int, int]:
    with runtime.operation() as database:
        return tuple(
            database.execute_sql(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            for table in ("run", "audio", "analysis_attempt")
        )


def test_delete_terminal_without_audio_commits_and_missing_run_is_not_found(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(runtime)
    service = RunDataManagementService(runtime)

    deleted = service.delete_one(run_id)
    missing = service.delete_one(run_id)

    assert deleted == DeleteRunResult(
        run_id=run_id,
        status=DeleteRunStatus.DELETED,
        reason="deleted",
        residual_path=None,
        database_committed=True,
    )
    assert missing.status is DeleteRunStatus.NOT_FOUND
    assert missing.database_committed is False


def test_delete_removes_attempt_audio_run_child_first_then_unlinks_wav(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    final = tmp_path / "final.wav"
    final.write_bytes(b"RIFF")
    run_id = _insert_run(runtime)
    audio_id = _insert_audio(runtime, run_id, final)
    _insert_attempt(runtime, run_id, audio_id, "FAILED")
    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.DELETED
    assert result.database_committed is True
    assert _counts(runtime, run_id) == (0, 0, 0)
    assert not final.exists()


@pytest.mark.parametrize("attempt_state", ["QUEUED", "RUNNING"])
def test_delete_rejects_active_attempts_and_preserves_everything(
    prepared_database_path: Path,
    tmp_path: Path,
    attempt_state: str,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    final = tmp_path / f"{attempt_state}.wav"
    final.write_bytes(b"RIFF")
    run_id = _insert_run(runtime)
    audio_id = _insert_audio(runtime, run_id, final)
    _insert_attempt(runtime, run_id, audio_id, attempt_state)

    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.REJECTED_ATTEMPT
    assert result.database_committed is False
    assert _counts(runtime, run_id) == (1, 1, 1)
    assert final.exists()


def test_delete_rejects_active_run_without_file_io(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    final = tmp_path / "active.wav"
    final.write_bytes(b"RIFF")
    run_id = _insert_run(runtime, state="ACTIVE")
    _insert_audio(runtime, run_id, final)

    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.REJECTED_ACTIVE
    assert result.database_committed is False
    assert _counts(runtime, run_id) == (1, 1, 0)
    assert final.exists()


def test_delete_missing_wav_succeeds_and_imported_source_is_preserved(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    source = tmp_path / "imported-source.wav"
    source.write_bytes(b"SOURCE")
    missing_final = tmp_path / "already-missing.wav"
    run_id = _insert_run(
        runtime,
        ingress_kind="IMPORTED",
        imported_source_path=str(source.resolve()),
    )
    _insert_audio(runtime, run_id, missing_final)

    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.DELETED
    assert result.residual_path is None
    assert source.read_bytes() == b"SOURCE"


def test_delete_database_rollback_retains_wav_and_database_rows(
    prepared_database_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    final = tmp_path / "rollback.wav"
    final.write_bytes(b"RIFF")
    run_id = _insert_run(runtime)
    _insert_audio(runtime, run_id, final)

    monkeypatch.setattr(
        data_management_module,
        "run_short_transaction",
        lambda _database, _work: TransactionResult(
            state=TransactionState.ROLLED_BACK,
            reason="forced rollback",
        ),
    )
    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.ROLLED_BACK
    assert result.reason == "forced rollback"
    assert result.database_committed is False
    assert final.exists()
    assert _counts(runtime, run_id) == (1, 1, 0)


def test_delete_committed_unlink_failure_reports_residual_absolute_path(
    prepared_database_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    final = (tmp_path / "locked.wav").resolve()
    final.write_bytes(b"RIFF")
    run_id = _insert_run(runtime)
    _insert_audio(runtime, run_id, final)
    real_unlink = Path.unlink

    def fail_target(path: Path, *args, **kwargs):
        if path == final:
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.PARTIAL_FILE_REMAINS
    assert result.database_committed is True
    assert result.residual_path == str(final)
    assert "locked" in result.reason
    assert _counts(runtime, run_id) == (0, 0, 0)
    assert final.exists()


def test_delete_unknown_quarantines_runtime_and_does_no_file_io(
    prepared_database_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    final = tmp_path / "unknown.wav"
    final.write_bytes(b"RIFF")
    run_id = _insert_run(runtime)
    _insert_audio(runtime, run_id, final)
    monkeypatch.setattr(
        data_management_module,
        "run_short_transaction",
        lambda _database, _work: TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="commit state unreadable",
            restart_required=True,
        ),
    )

    result = RunDataManagementService(runtime).delete_one(run_id)

    assert result.status is DeleteRunStatus.UNKNOWN
    assert result.database_committed is False
    assert result.residual_path is None
    assert runtime.closed
    assert final.exists()


def test_delete_many_preserves_order_continues_ordinary_failures(
    prepared_database_path: Path,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    active = _insert_run(runtime, state="ACTIVE")
    missing = str(uuid4())
    terminal = _insert_run(runtime)

    report = RunDataManagementService(runtime).delete_many(
        (active, missing, terminal)
    )

    assert tuple(result.run_id for result in report.results) == (
        active,
        missing,
        terminal,
    )
    assert tuple(result.status for result in report.results) == (
        DeleteRunStatus.REJECTED_ACTIVE,
        DeleteRunStatus.NOT_FOUND,
        DeleteRunStatus.DELETED,
    )
    assert report.unattempted_run_ids == ()
    assert report.restart_required is False


def test_delete_many_stops_at_unknown_and_reports_unattempted(
    prepared_database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ready_runtime(prepared_database_path)
    run_ids = tuple(str(uuid4()) for _ in range(4))
    outcomes = iter(
        (
            TransactionResult(state=TransactionState.COMMITTED, value=None),
            TransactionResult(
                state=TransactionState.UNKNOWN,
                reason="unknown",
                restart_required=True,
            ),
        )
    )
    monkeypatch.setattr(
        data_management_module,
        "run_short_transaction",
        lambda _database, _work: next(outcomes),
    )

    report = RunDataManagementService(runtime).delete_many(run_ids)

    assert tuple(result.run_id for result in report.results) == run_ids[:2]
    assert report.results[-1].status is DeleteRunStatus.UNKNOWN
    assert report.unattempted_run_ids == run_ids[2:]
    assert report.restart_required is True


class _RowCountCursor:
    def __init__(self, cursor, rowcount: int) -> None:
        self._cursor = cursor
        self.rowcount = rowcount

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _DeleteOrderConnection:
    def __init__(self, database, statements: list[str], wrong_run_count: bool):
        self._database = database
        self._statements = statements
        self._wrong_run_count = wrong_run_count

    def __getattr__(self, name: str):
        return getattr(self._database, name)

    def execute_sql(self, sql: str, params=None):
        normalized = " ".join(sql.split()).upper()
        cursor = self._database.execute_sql(sql, params)
        if normalized.startswith("DELETE"):
            self._statements.append(normalized)
        if self._wrong_run_count and normalized.startswith("DELETE FROM RUN "):
            return _RowCountCursor(cursor, 2)
        return cursor


class _DeleteOrderRuntime(RunPersistenceDatabase):
    def __init__(self, path: Path, *, wrong_run_count: bool = False) -> None:
        super().__init__(path)
        self.statements: list[str] = []
        self.wrong_run_count = wrong_run_count

    @contextmanager
    def operation(self):
        with super().operation() as database:
            yield _DeleteOrderConnection(
                database,
                self.statements,
                self.wrong_run_count,
            )


def test_delete_uses_exact_child_first_order_and_requires_one_run_row(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    base = _ready_runtime(prepared_database_path)
    run_id = _insert_run(base)
    audio_id = _insert_audio(base, run_id, tmp_path / "missing.wav")
    _insert_attempt(base, run_id, audio_id, "FAILED")
    base.close_after_upstream_stopped()
    runtime = _DeleteOrderRuntime(prepared_database_path, wrong_run_count=True)
    assert settle_startup(runtime, ACCEPTED_AT).ready

    result = RunDataManagementService(runtime).delete_one(run_id)

    assert [statement.split()[2] for statement in runtime.statements] == [
        "ANALYSIS_ATTEMPT",
        "AUDIO",
        "RUN",
    ]
    assert result.status is DeleteRunStatus.ROLLED_BACK
    assert _counts(runtime, run_id) == (1, 1, 1)


class _RaceConnection:
    def __init__(
        self,
        database,
        match: str,
        reached: threading.Event,
        release: threading.Event,
    ) -> None:
        self._database = database
        self._match = match
        self._reached = reached
        self._release = release

    def __getattr__(self, name: str):
        return getattr(self._database, name)

    def execute_sql(self, sql: str, params=None):
        cursor = self._database.execute_sql(sql, params)
        if self._match in " ".join(sql.split()).upper():
            self._reached.set()
            assert self._release.wait(timeout=5)
        return cursor


class _RaceRuntime(RunPersistenceDatabase):
    def __init__(
        self,
        path: Path,
        match: str,
        reached: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(path)
        self._match = match
        self._reached = reached
        self._release = release

    @contextmanager
    def operation(self):
        with super().operation() as database:
            yield _RaceConnection(
                database,
                self._match,
                self._reached,
                self._release,
            )


def _queue_attempt_transaction(
    runtime: RunPersistenceDatabase,
    run_id: str,
    audio_id: str,
    *,
    require_run: bool = False,
) -> TransactionResult:
    def queue(database):
        if require_run:
            exists = database.execute_sql(
                "SELECT 1 FROM run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if exists is None:
                raise RuntimeError("NOT_FOUND")
        return database.execute_sql(
            """
            INSERT INTO analysis_attempt (
                attempt_id, run_id, audio_id, attempt_kind, state,
                judgment, result_payload_json, error_code, error_summary,
                queued_at, started_at, terminal_at, updated_at
            ) VALUES (
                ?, ?, ?, 'REANALYSIS', 'QUEUED',
                NULL, NULL, NULL, NULL, ?, NULL, NULL, ?
            )
            """,
            (str(uuid4()), run_id, audio_id, ACCEPTED_AT, ACCEPTED_AT),
        ).rowcount

    with runtime.operation() as database:
        return run_short_transaction(database, queue)


def test_delete_race_queue_transaction_first_is_rejected(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    base = _ready_runtime(prepared_database_path)
    run_id = _insert_run(base)
    audio_id = _insert_audio(base, run_id, tmp_path / "missing.wav")
    base.close_after_upstream_stopped()
    queue_inserted = threading.Event()
    allow_queue_commit = threading.Event()
    runtime = _RaceRuntime(
        prepared_database_path,
        "INSERT INTO ANALYSIS_ATTEMPT",
        queue_inserted,
        allow_queue_commit,
    )
    assert settle_startup(runtime, ACCEPTED_AT).ready
    outcomes: dict[str, object] = {}

    queue_thread = threading.Thread(
        target=lambda: outcomes.setdefault(
            "queue",
            _queue_attempt_transaction(runtime, run_id, audio_id),
        )
    )
    queue_thread.start()
    assert queue_inserted.wait(timeout=5)
    delete_started = threading.Event()

    def delete() -> None:
        delete_started.set()
        outcomes["delete"] = RunDataManagementService(runtime).delete_one(run_id)

    delete_thread = threading.Thread(target=delete)
    delete_thread.start()
    assert delete_started.wait(timeout=5)
    allow_queue_commit.set()
    queue_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert outcomes["queue"].state is TransactionState.COMMITTED
    assert outcomes["delete"].status is DeleteRunStatus.REJECTED_ATTEMPT
    assert _counts(runtime, run_id) == (1, 1, 1)


def test_delete_race_delete_transaction_first_makes_reanalysis_not_found(
    prepared_database_path: Path,
    tmp_path: Path,
) -> None:
    base = _ready_runtime(prepared_database_path)
    run_id = _insert_run(base)
    audio_id = _insert_audio(base, run_id, tmp_path / "missing.wav")
    base.close_after_upstream_stopped()
    run_deleted = threading.Event()
    allow_delete_commit = threading.Event()
    runtime = _RaceRuntime(
        prepared_database_path,
        "DELETE FROM RUN ",
        run_deleted,
        allow_delete_commit,
    )
    assert settle_startup(runtime, ACCEPTED_AT).ready
    outcomes: dict[str, object] = {}
    delete_thread = threading.Thread(
        target=lambda: outcomes.setdefault(
            "delete",
            RunDataManagementService(runtime).delete_one(run_id),
        )
    )
    delete_thread.start()
    assert run_deleted.wait(timeout=5)
    reanalysis_started = threading.Event()

    def reanalyze() -> None:
        reanalysis_started.set()
        outcomes["reanalysis"] = _queue_attempt_transaction(
            runtime,
            run_id,
            audio_id,
            require_run=True,
        )

    reanalysis_thread = threading.Thread(target=reanalyze)
    reanalysis_thread.start()
    assert reanalysis_started.wait(timeout=5)
    allow_delete_commit.set()
    delete_thread.join(timeout=5)
    reanalysis_thread.join(timeout=5)

    assert outcomes["delete"].status is DeleteRunStatus.DELETED
    assert outcomes["reanalysis"].state is TransactionState.ROLLED_BACK
    assert outcomes["reanalysis"].reason == "NOT_FOUND"
    assert _counts(runtime, run_id) == (0, 0, 0)
