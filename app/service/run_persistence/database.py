"""Process-local admission and short-connection lifecycle for run persistence."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import threading
from typing import Iterator

from peewee import SqliteDatabase

from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
)
from app.service.run_persistence.schema import validate_run_persistence_schema


_REPOSITORY_MUTEX = threading.RLock()


class DatabaseLifecycleCode(str, Enum):
    DATABASE_NOT_FOUND = "DATABASE_NOT_FOUND"
    DATABASE_OPEN_FAILED = "DATABASE_OPEN_FAILED"
    DATABASE_APPLICATION_ID_MISMATCH = "DATABASE_APPLICATION_ID_MISMATCH"
    DATABASE_USER_VERSION_MISMATCH = "DATABASE_USER_VERSION_MISMATCH"
    DATABASE_FOREIGN_KEYS_DISABLED = "DATABASE_FOREIGN_KEYS_DISABLED"
    DATABASE_TABLE_SET_MISMATCH = "DATABASE_TABLE_SET_MISMATCH"
    DATABASE_RUN_COLUMNS_MISMATCH = "DATABASE_RUN_COLUMNS_MISMATCH"
    DATABASE_RUN_CHECKS_MISMATCH = "DATABASE_RUN_CHECKS_MISMATCH"
    DATABASE_NOT_READY = "DATABASE_NOT_READY"
    DATABASE_CLOSED = "DATABASE_CLOSED"


class DatabaseUnavailableError(RuntimeError):
    def __init__(self, code: DatabaseLifecycleCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class DatabaseStartupOpenResult:
    ok: bool
    code: DatabaseLifecycleCode | None = None
    reason: str | None = None


def _open_existing_database(path: Path) -> SqliteDatabase:
    uri = f"{path.as_uri()}?mode=rw"
    database = SqliteDatabase(
        uri,
        uri=True,
        pragmas={"foreign_keys": 1},
    )
    database.connect(reuse_if_open=False)
    return database


class RunPersistenceDatabase:
    """One runtime gate with one new same-thread connection per operation."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()
        self._validated = False
        self._ready = False
        self._closed = False
        self._current_thread_connection = threading.local()

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    def open_and_validate_startup(self) -> DatabaseStartupOpenResult:
        with _REPOSITORY_MUTEX:
            if self._closed:
                return DatabaseStartupOpenResult(
                    ok=False,
                    code=DatabaseLifecycleCode.DATABASE_CLOSED,
                    reason="run persistence database is permanently closed",
                )
            if not self._path.exists():
                self.close_startup_failed()
                return DatabaseStartupOpenResult(
                    ok=False,
                    code=DatabaseLifecycleCode.DATABASE_NOT_FOUND,
                    reason=f"database does not exist: {self._path}",
                )

            database: SqliteDatabase | None = None
            try:
                database = _open_existing_database(self._path)
                database.execute_sql("PRAGMA foreign_keys = ON")
                validation = validate_run_persistence_schema(database)
            except Exception as exc:
                self.close_startup_failed()
                return DatabaseStartupOpenResult(
                    ok=False,
                    code=DatabaseLifecycleCode.DATABASE_OPEN_FAILED,
                    reason=f"database open failed: {exc}",
                )
            finally:
                if database is not None and not database.is_closed():
                    database.close()

            if not validation.valid:
                assert validation.failure is not None
                self.close_startup_failed()
                return DatabaseStartupOpenResult(
                    ok=False,
                    code=DatabaseLifecycleCode(validation.failure.code.value),
                    reason=validation.failure.reason,
                )

            self._validated = True
            return DatabaseStartupOpenResult(ok=True)

    @contextmanager
    def operation(self) -> Iterator[SqliteDatabase]:
        with _REPOSITORY_MUTEX:
            if self._closed:
                raise DatabaseUnavailableError(
                    DatabaseLifecycleCode.DATABASE_CLOSED,
                    "run persistence database is permanently closed",
                )
            if not self._ready:
                raise DatabaseUnavailableError(
                    DatabaseLifecycleCode.DATABASE_NOT_READY,
                    "startup settlement has not committed",
                )
            with self._operation_connection() as database:
                yield database

    @contextmanager
    def _startup_settlement_operation(self) -> Iterator[SqliteDatabase]:
        with _REPOSITORY_MUTEX:
            if self._closed or not self._validated or self._ready:
                code = (
                    DatabaseLifecycleCode.DATABASE_CLOSED
                    if self._closed
                    else DatabaseLifecycleCode.DATABASE_NOT_READY
                )
                raise DatabaseUnavailableError(
                    code,
                    "startup settlement path is unavailable",
                )
            with self._operation_connection() as database:
                yield database

    @contextmanager
    def _operation_connection(self) -> Iterator[SqliteDatabase]:
        database: SqliteDatabase | None = None
        previous = getattr(
            self._current_thread_connection, "database", None
        )
        try:
            database = _open_existing_database(self._path)
            self._current_thread_connection.database = database
            database.execute_sql("PRAGMA foreign_keys = ON")
            foreign_keys = database.execute_sql(
                "PRAGMA foreign_keys"
            ).fetchone()[0]
            if foreign_keys != 1:
                raise DatabaseUnavailableError(
                    DatabaseLifecycleCode.DATABASE_OPEN_FAILED,
                    "SQLite foreign keys could not be enabled",
                )
            yield database
        finally:
            if database is not None and not database.is_closed():
                database.close()
            if previous is None:
                self._current_thread_connection.__dict__.pop(
                    "database", None
                )
            else:
                self._current_thread_connection.database = previous

    def _close_current_thread_connection_if_open(self) -> None:
        database = getattr(
            self._current_thread_connection, "database", None
        )
        if database is not None and not database.is_closed():
            database.close()

    def close_after_upstream_stopped(self) -> None:
        with _REPOSITORY_MUTEX:
            self._closed = True
            self._close_current_thread_connection_if_open()

    def _finish_startup(self, result: TransactionResult[object]) -> None:
        with _REPOSITORY_MUTEX:
            if (
                self._validated
                and not self._closed
                and result.state is TransactionState.COMMITTED
            ):
                self._ready = True
                return
            self.close_startup_failed()

    def quarantine_unknown(self) -> None:
        with _REPOSITORY_MUTEX:
            self._ready = False
            self._closed = True

    def close_startup_failed(self) -> None:
        with _REPOSITORY_MUTEX:
            self._ready = False
            self._closed = True
