from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading

import pytest

import app.service.run_persistence.database as database_module
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
    SchemaValidationCode,
    SchemaValidationFailure,
    SchemaValidationResult,
)
from app.service.run_persistence.transactions import run_short_transaction


def _validated_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    result = runtime.open_and_validate_startup()
    assert result.ok
    return runtime


def _ready_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = _validated_runtime(path)
    with runtime._startup_settlement_operation():
        pass
    runtime._finish_startup(
        TransactionResult(state=TransactionState.COMMITTED)
    )
    assert runtime.ready
    return runtime


def test_missing_database_is_not_created(tmp_path):
    target = tmp_path / "missing.sqlite3"
    runtime = RunPersistenceDatabase(target)

    result = runtime.open_and_validate_startup()

    assert not result.ok
    assert result.code is DatabaseLifecycleCode.DATABASE_NOT_FOUND
    assert not target.exists()
    assert runtime.closed
    assert not runtime.ready


def test_existing_unusable_target_is_open_failure(tmp_path):
    target = tmp_path / "database-directory"
    target.mkdir()

    result = RunPersistenceDatabase(target).open_and_validate_startup()

    assert not result.ok
    assert result.code is DatabaseLifecycleCode.DATABASE_OPEN_FAILED
    assert target.is_dir()


@pytest.mark.parametrize(
    ("pragma", "value", "expected_code"),
    [
        ("application_id", 0, "DATABASE_APPLICATION_ID_MISMATCH"),
        ("user_version", 0, "DATABASE_USER_VERSION_MISMATCH"),
    ],
)
def test_authority_failure_closes_runtime(
    prepared_database_path, pragma, value, expected_code
):
    from peewee import SqliteDatabase

    database = SqliteDatabase(prepared_database_path)
    database.connect()
    database.execute_sql(f"PRAGMA {pragma} = {value}")
    database.close()
    runtime = RunPersistenceDatabase(prepared_database_path)

    result = runtime.open_and_validate_startup()

    assert not result.ok
    assert result.code.value == expected_code
    assert runtime.closed
    assert not runtime.ready
    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED


def test_table_set_failure_happens_before_settlement(prepared_database_path):
    from peewee import SqliteDatabase

    database = SqliteDatabase(prepared_database_path)
    database.connect()
    database.execute_sql("CREATE TABLE unexpected (value INTEGER)")
    database.close()
    runtime = RunPersistenceDatabase(prepared_database_path)

    result = runtime.open_and_validate_startup()

    assert not result.ok
    assert result.code.value == "DATABASE_TABLE_SET_MISMATCH"
    with pytest.raises(DatabaseUnavailableError):
        with runtime._startup_settlement_operation():
            pass


@pytest.mark.parametrize(
    "code",
    [
        SchemaValidationCode.DATABASE_RUN_COLUMNS_MISMATCH,
        SchemaValidationCode.DATABASE_RUN_CHECKS_MISMATCH,
    ],
)
def test_run_shape_failure_codes_surface_through_runtime_startup(
    prepared_database_path,
    monkeypatch,
    code,
):
    monkeypatch.setattr(
        database_module,
        "validate_run_persistence_schema",
        lambda _database: SchemaValidationResult(
            valid=False,
            failure=SchemaValidationFailure(
                code=code,
                expected="expected",
                actual="actual",
            ),
        ),
    )

    result = RunPersistenceDatabase(
        prepared_database_path
    ).open_and_validate_startup()

    assert not result.ok
    assert result.code.value == code.value


def test_validation_success_does_not_make_ordinary_operations_ready(
    prepared_database_path,
):
    runtime = _validated_runtime(prepared_database_path)

    assert not runtime.ready
    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_NOT_READY
    with runtime._startup_settlement_operation() as connection:
        assert connection.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize(
    "state", [TransactionState.ROLLED_BACK, TransactionState.UNKNOWN]
)
def test_failed_settlement_permanently_closes_runtime(
    prepared_database_path, state, monkeypatch
):
    runtime = _validated_runtime(prepared_database_path)
    runtime._finish_startup(
        TransactionResult(
            state=state,
            reason="settlement failed",
            restart_required=state is TransactionState.UNKNOWN,
        )
    )
    opens = 0
    original = database_module._open_existing_database

    def count_open(path):
        nonlocal opens
        opens += 1
        return original(path)

    monkeypatch.setattr(database_module, "_open_existing_database", count_open)

    assert runtime.closed
    assert not runtime.ready
    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED
    assert opens == 0


def test_every_ready_operation_uses_and_closes_a_new_same_thread_connection(
    prepared_database_path, monkeypatch
):
    runtime = _ready_runtime(prepared_database_path)
    records: list[tuple[int, int, object]] = []
    original = database_module._open_existing_database

    def tracked_open(path):
        connection = original(path)
        records.append((threading.get_ident(), -1, connection))
        original_close = connection.close

        def tracked_close():
            opened_thread, _, recorded = records[-1]
            records[-1] = (opened_thread, threading.get_ident(), recorded)
            return original_close()

        connection.close = tracked_close
        return connection

    monkeypatch.setattr(database_module, "_open_existing_database", tracked_open)

    for _ in range(2):
        with runtime.operation() as connection:
            assert connection.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1

    assert len(records) == 2
    assert records[0][2] is not records[1][2]
    assert all(opened == closed == threading.get_ident() for opened, closed, _ in records)
    assert all(connection.is_closed() for _, _, connection in records)


def test_relative_database_identity_is_frozen_at_runtime_construction(tmp_path):
    from app.service.run_persistence.prepare_database import (
        prepare_run_persistence_database,
    )

    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    relative_path = Path("data") / "run-persistence.sqlite3"
    cwd_a.mkdir()
    cwd_b.mkdir()
    (cwd_a / relative_path.parent).mkdir()
    (cwd_b / relative_path.parent).mkdir()
    target_a = prepare_run_persistence_database(cwd_a / relative_path)
    target_b = prepare_run_persistence_database(cwd_b / relative_path)
    target_b_bytes = target_b.read_bytes()
    target_b_mtime = target_b.stat().st_mtime_ns
    original_cwd = Path.cwd()

    try:
        os.chdir(cwd_a)
        runtime = _ready_runtime(relative_path)

        os.chdir(cwd_b)
        with runtime.operation() as connection:
            opened_path = Path(
                connection.execute_sql("PRAGMA database_list").fetchone()[2]
            )

        assert opened_path == target_a.resolve()
        assert target_b.read_bytes() == target_b_bytes
        assert target_b.stat().st_mtime_ns == target_b_mtime
    finally:
        os.chdir(original_cwd)


def test_repository_mutex_serializes_callers_before_second_connection_open(
    prepared_database_path, monkeypatch
):
    runtime = _ready_runtime(prepared_database_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_actor_started = threading.Event()
    second_opened = threading.Event()
    open_count = 0
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    original = database_module._open_existing_database

    def tracked_open(path):
        nonlocal open_count
        with lock:
            open_count += 1
            if open_count == 2:
                second_opened.set()
        return original(path)

    monkeypatch.setattr(database_module, "_open_existing_database", tracked_open)

    def use_database(first: bool):
        nonlocal active, maximum_active
        if not first:
            second_actor_started.set()
        with runtime.operation():
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            if first:
                first_entered.set()
                assert release_first.wait(5)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(use_database, True)
        assert first_entered.wait(5)
        second = executor.submit(use_database, False)
        assert second_actor_started.wait(5)
        assert not second_opened.wait(0.1)
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert second_opened.is_set()
    assert maximum_active == 1


def test_unknown_quarantine_prevents_any_later_connection_open(
    prepared_database_path, monkeypatch
):
    runtime = _ready_runtime(prepared_database_path)
    runtime.quarantine_unknown()
    opens = 0

    def forbidden_open(_path):
        nonlocal opens
        opens += 1
        raise AssertionError("must not open")

    monkeypatch.setattr(database_module, "_open_existing_database", forbidden_open)

    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED
    assert opens == 0


def test_rolled_back_operation_connection_closes_in_finally(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)

    with runtime.operation() as connection:
        opened = connection
        result = run_short_transaction(
            connection,
            lambda _database: (_ for _ in ()).throw(
                RuntimeError("statement failed")
            ),
        )

    assert result.state is TransactionState.ROLLED_BACK
    assert opened.is_closed()
