from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from peewee import SqliteDatabase

import app.service.run_persistence.database as database_module
import app.service.run_persistence.startup as startup_module
from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
)
from app.service.run_persistence.database import (
    DatabaseLifecycleCode,
    DatabaseUnavailableError,
    RunPersistenceDatabase,
)
from app.service.run_persistence.schema import (
    RUN_PERSISTENCE_TABLES,
    SchemaValidationCode,
    SchemaValidationFailure,
    SchemaValidationResult,
)
from app.service.run_persistence.startup import settle_startup


STARTUP_AT = "2026-07-27T13:00:00.123456Z"
BEFORE = "2026-07-27T12:00:00.000000Z"


def _insert_run(database, run_id, state="ACTIVE"):
    terminal = state != "ACTIVE"
    database.execute_sql(
        """
        INSERT INTO run (
            run_id, ingress_kind, trigger_source, config_id, hardware_id,
            imported_source_identity, imported_source_path,
            imported_expected_frame_count, causal_revision,
            config_snapshot_json, hardware_snapshot_json,
            calibration_snapshot_json, state, judgment, judgment_source,
            judged_at, terminal_code, terminal_summary, accepted_at,
            terminal_at, updated_at
        ) VALUES (?, 'CAPTURED', 'MANUAL', 1, 2, NULL, NULL, NULL, 0,
                  '{}', '{}', '{}', ?,
                  NULL, 'NONE', NULL, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            state,
            "ALREADY_TERMINAL" if terminal else None,
            "unchanged terminal" if terminal else None,
            BEFORE,
            BEFORE if terminal else None,
            BEFORE,
        ),
    )


def _seed_in_progress(path):
    active_run = str(uuid4())
    terminal_run = str(uuid4())
    audio_id = str(uuid4())
    queued_attempt = str(uuid4())
    running_attempt = str(uuid4())
    terminal_attempt = str(uuid4())
    database = SqliteDatabase(path, pragmas={"foreign_keys": 1})
    database.connect()
    _insert_run(database, active_run)
    _insert_run(database, terminal_run, "FAILED")
    database.execute_sql(
        """
        INSERT INTO audio (
            audio_id, save_job_id, run_id, source_kind, source_identity,
            content_revision, final_path, container_format, sample_rate_hz,
            channel_count, frame_count, duration_ms, byte_size,
            closed_verified_at, created_at
        ) VALUES (?, ?, ?, 'CAPTURED_AUDIO', 'managed-input:startup',
                  1, ?, 'WAV_PCM', 16000, 1, 160, 10, 364, ?, ?)
        """,
        (
            audio_id,
            str(uuid4()),
            active_run,
            str(Path(path).with_suffix(".wav")),
            BEFORE,
            BEFORE,
        ),
    )
    for attempt_id, state, started_at, terminal_at, error_code in (
        (queued_attempt, "QUEUED", None, None, None),
        (running_attempt, "RUNNING", BEFORE, None, None),
        (terminal_attempt, "FAILED", BEFORE, BEFORE, "ALREADY_FAILED"),
    ):
        database.execute_sql(
            """
            INSERT INTO analysis_attempt (
                attempt_id, run_id, audio_id, attempt_kind, state, judgment,
                result_payload_json, error_code, error_summary, queued_at,
                started_at, terminal_at, updated_at
            ) VALUES (?, ?, ?, 'REANALYSIS', ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                active_run,
                audio_id,
                state,
                error_code,
                "unchanged" if error_code else None,
                BEFORE,
                started_at,
                terminal_at,
                BEFORE,
            ),
        )
    database.close()
    return {
        "active_run": active_run,
        "terminal_run": terminal_run,
        "audio_id": audio_id,
        "queued_attempt": queued_attempt,
        "running_attempt": running_attempt,
        "terminal_attempt": terminal_attempt,
    }


def test_startup_settles_attempts_then_runs_in_one_transaction(
    prepared_database_path, monkeypatch
):
    ids = _seed_in_progress(prepared_database_path)
    statements = []
    opens = 0
    original = database_module._open_existing_database

    def traced_open(path):
        nonlocal opens
        opens += 1
        database = original(path)
        if opens == 2:
            database.connection().set_trace_callback(statements.append)
        return database

    monkeypatch.setattr(database_module, "_open_existing_database", traced_open)
    runtime = RunPersistenceDatabase(prepared_database_path)

    result = settle_startup(runtime, STARTUP_AT)

    assert result.ready
    assert result.transaction.state is TransactionState.COMMITTED
    assert runtime.ready
    updates = [sql for sql in statements if sql.lstrip().upper().startswith("UPDATE")]
    assert "UPDATE analysis_attempt" in updates[0]
    assert "UPDATE run" in updates[1]

    database = SqliteDatabase(prepared_database_path)
    database.connect()
    attempts = {
        row[0]: row[1:]
        for row in database.execute_sql(
            """
            SELECT attempt_id, state, error_code, error_summary, terminal_at,
                   updated_at, started_at
            FROM analysis_attempt
            """
        ).fetchall()
    }
    runs = {
        row[0]: row[1:]
        for row in database.execute_sql(
            """
            SELECT run_id, state, terminal_code, terminal_summary, terminal_at,
                   updated_at
            FROM run
            """
        ).fetchall()
    }
    audio_before = database.execute_sql(
        "SELECT * FROM audio WHERE audio_id = ?", (ids["audio_id"],)
    ).fetchone()
    database.close()

    summary = "Previous process ended before terminal outcome"
    assert attempts[ids["queued_attempt"]] == (
        "INTERRUPTED",
        "PROCESS_RESTARTED",
        summary,
        STARTUP_AT,
        STARTUP_AT,
        None,
    )
    assert attempts[ids["running_attempt"]] == (
        "INTERRUPTED",
        "PROCESS_RESTARTED",
        summary,
        STARTUP_AT,
        STARTUP_AT,
        BEFORE,
    )
    assert attempts[ids["terminal_attempt"]] == (
        "FAILED",
        "ALREADY_FAILED",
        "unchanged",
        BEFORE,
        BEFORE,
        BEFORE,
    )
    assert runs[ids["active_run"]] == (
        "INTERRUPTED",
        "PROCESS_RESTARTED",
        summary,
        STARTUP_AT,
        STARTUP_AT,
    )
    assert runs[ids["terminal_run"]] == (
        "FAILED",
        "ALREADY_TERMINAL",
        "unchanged terminal",
        BEFORE,
        BEFORE,
    )
    assert audio_before is not None


def test_missing_database_stops_before_settlement_and_remains_absent(tmp_path):
    target = tmp_path / "missing.sqlite3"
    runtime = RunPersistenceDatabase(target)

    result = settle_startup(runtime, STARTUP_AT)

    assert not result.ready
    assert result.code is DatabaseLifecycleCode.DATABASE_NOT_FOUND
    assert result.transaction is None
    assert not target.exists()


@pytest.mark.parametrize(
    "failure",
    [
        SchemaValidationFailure(
            SchemaValidationCode.DATABASE_APPLICATION_ID_MISMATCH, 1, 0
        ),
        SchemaValidationFailure(
            SchemaValidationCode.DATABASE_USER_VERSION_MISMATCH, 1, 0
        ),
        SchemaValidationFailure(
            SchemaValidationCode.DATABASE_FOREIGN_KEYS_DISABLED, 1, 0
        ),
        SchemaValidationFailure(
            SchemaValidationCode.DATABASE_TABLE_SET_MISMATCH,
            RUN_PERSISTENCE_TABLES,
            frozenset(),
        ),
    ],
)
def test_each_authority_failure_stops_before_settlement(
    prepared_database_path, monkeypatch, failure
):
    monkeypatch.setattr(
        database_module,
        "validate_run_persistence_schema",
        lambda _database: SchemaValidationResult(valid=False, failure=failure),
    )
    runtime = RunPersistenceDatabase(prepared_database_path)

    result = settle_startup(runtime, STARTUP_AT)

    assert not result.ready
    assert result.code.value == failure.code.value
    assert result.transaction is None
    assert runtime.closed


@pytest.mark.parametrize(
    "transaction",
    [
        TransactionResult(
            state=TransactionState.ROLLED_BACK,
            reason="settlement statement failed",
        ),
        TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="settlement outcome unknown",
            restart_required=True,
        ),
    ],
)
def test_settlement_failure_never_readies_and_later_access_opens_nothing(
    prepared_database_path, monkeypatch, transaction
):
    monkeypatch.setattr(
        startup_module,
        "run_short_transaction",
        lambda _database, _work: transaction,
    )
    runtime = RunPersistenceDatabase(prepared_database_path)
    result = settle_startup(runtime, STARTUP_AT)
    opens = 0

    def forbidden_open(_path):
        nonlocal opens
        opens += 1
        raise AssertionError("closed runtime must not open SQLite")

    monkeypatch.setattr(database_module, "_open_existing_database", forbidden_open)

    assert not result.ready
    assert result.transaction is transaction
    assert result.reason == transaction.reason
    assert runtime.closed
    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED
    assert opens == 0
