from __future__ import annotations

import sqlite3

import pytest
from peewee import SqliteDatabase

from app.service.run_persistence.contracts import TransactionState
from app.service.run_persistence.transactions import run_short_transaction


class FaultDatabase:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        fail_commit: BaseException | None = None,
        no_op_commit: bool = False,
        fail_rollback: BaseException | None = None,
        unreadable_state: bool = False,
        non_boolean_state: bool = False,
        no_op_rollback: bool = False,
    ) -> None:
        self.database = database
        self.fail_commit = fail_commit
        self.no_op_commit = no_op_commit
        self.fail_rollback = fail_rollback
        self.unreadable_state = unreadable_state
        self.non_boolean_state = non_boolean_state
        self.no_op_rollback = no_op_rollback
        self.rollback_calls = 0
        self.close_calls = 0
        self.driver_active_during_rollback = False

    def begin(self) -> None:
        self.database.begin()

    def execute_sql(self, sql: str):
        return self.database.execute_sql(sql)

    def commit(self) -> None:
        if self.fail_commit is not None:
            raise self.fail_commit
        if self.no_op_commit:
            return
        self.database.commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        self.driver_active_during_rollback = (
            self.database.connection().in_transaction
        )
        if self.fail_rollback is not None:
            raise self.fail_rollback
        if self.no_op_rollback:
            return
        self.database.rollback()

    def connection(self):
        driver = self.database.connection()
        if self.unreadable_state:
            return UnreadableDriverState()
        if self.non_boolean_state:
            return NonBooleanDriverState()
        return driver

    def in_transaction(self) -> bool:
        return self.database.in_transaction()

    def close(self) -> None:
        self.close_calls += 1
        if not self.database.is_closed():
            self.database.close()


class UnreadableDriverState:
    @property
    def in_transaction(self):
        raise RuntimeError("state unreadable")


class NonBooleanDriverState:
    in_transaction = 0


@pytest.fixture
def fault_database(tmp_path):
    database = SqliteDatabase(tmp_path / "transaction.sqlite3")
    database.connect()
    database.execute_sql("CREATE TABLE item (value INTEGER)")
    wrapper = FaultDatabase(database)
    try:
        yield wrapper
    finally:
        if not database.is_closed():
            database.close()


def test_direct_begin_is_visible_only_on_real_sqlite_driver(fault_database):
    fault_database.begin()
    driver = fault_database.database.connection()

    assert fault_database.database.in_transaction() is False
    assert isinstance(driver, sqlite3.Connection)
    assert driver.in_transaction is True

    fault_database.rollback()


def test_commit_returning_normally_is_committed(fault_database):
    calls = 0

    def work(database):
        nonlocal calls
        calls += 1
        database.execute_sql("INSERT INTO item VALUES (1)")
        return "inserted"

    result = run_short_transaction(fault_database, work)

    assert result.state is TransactionState.COMMITTED
    assert result.value == "inserted"
    assert result.reason is None
    assert result.restart_required is False
    assert calls == 1


@pytest.mark.parametrize(
    "faults",
    [
        {"no_op_commit": True},
        {"unreadable_state": True},
        {"non_boolean_state": True},
    ],
)
def test_unconfirmed_commit_is_unknown_and_quarantines_connection(
    fault_database, faults
):
    for name, value in faults.items():
        setattr(fault_database, name, value)

    result = run_short_transaction(
        fault_database,
        lambda database: database.execute_sql("INSERT INTO item VALUES (1)"),
    )

    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
    assert fault_database.rollback_calls == 0
    assert fault_database.close_calls == 1


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("statement failed"),
        sqlite3.OperationalError("database is busy"),
        sqlite3.OperationalError("database table is locked"),
    ],
)
def test_statement_failure_with_confirmed_rollback_is_rolled_back(
    fault_database, error
):
    calls = 0

    def work(_database):
        nonlocal calls
        calls += 1
        raise error

    result = run_short_transaction(fault_database, work)

    assert result.state is TransactionState.ROLLED_BACK
    assert str(error) in result.reason
    assert result.restart_required is False
    assert fault_database.rollback_calls == 1
    assert calls == 1


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("commit failed"),
        sqlite3.OperationalError("database is busy"),
        sqlite3.OperationalError("database table is locked"),
    ],
)
def test_commit_failure_with_confirmed_rollback_is_rolled_back(
    fault_database, error
):
    fault_database.fail_commit = error
    calls = 0

    def work(database):
        nonlocal calls
        calls += 1
        database.execute_sql("INSERT INTO item VALUES (1)")

    result = run_short_transaction(fault_database, work)

    assert result.state is TransactionState.ROLLED_BACK
    assert str(error) in result.reason
    assert result.restart_required is False
    assert fault_database.rollback_calls == 1
    assert calls == 1


@pytest.mark.parametrize(
    "faults",
    [
        {"fail_rollback": RuntimeError("rollback failed")},
        {"unreadable_state": True},
        {"non_boolean_state": True},
        {"no_op_rollback": True},
    ],
)
def test_unconfirmed_rollback_is_unknown_and_quarantines_connection(
    fault_database, faults
):
    for name, value in faults.items():
        setattr(fault_database, name, value)

    result = run_short_transaction(
        fault_database,
        lambda _database: (_ for _ in ()).throw(RuntimeError("statement failed")),
    )

    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
    assert fault_database.rollback_calls == 1
    assert fault_database.close_calls == 1


@pytest.mark.parametrize(
    "faults",
    [
        {"fail_rollback": RuntimeError("rollback failed")},
        {"no_op_rollback": True},
    ],
)
def test_failed_or_no_op_rollback_leaves_real_driver_active_and_is_unknown(
    fault_database, faults
):
    for name, value in faults.items():
        setattr(fault_database, name, value)

    result = run_short_transaction(
        fault_database,
        lambda _database: (_ for _ in ()).throw(RuntimeError("statement failed")),
    )

    assert fault_database.driver_active_during_rollback is True
    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
