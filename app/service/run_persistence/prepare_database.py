"""Offline-only preparation of a dedicated run-persistence database."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import os
from typing import TypeVar
from uuid import uuid4

from peewee import SqliteDatabase

from app.service.run_persistence.schema import (
    RUN_PERSISTENCE_APPLICATION_ID,
    RUN_PERSISTENCE_USER_VERSION,
    validate_run_persistence_schema,
)
from model.analysis_attempt_table import AnalysisAttemptTable
from model.audio_table import AudioTable
from model.run_table import RunTable


_MODELS = (RunTable, AudioTable, AnalysisAttemptTable)
_Result = TypeVar("_Result")


class RunPersistencePreparationError(RuntimeError):
    """Offline database preparation failed without publishing an artifact."""


def _run_phase(name: str, action: Callable[[], _Result]) -> _Result:
    try:
        return action()
    except RunPersistencePreparationError:
        raise
    except Exception as exc:
        raise RunPersistencePreparationError(f"{name} failed: {exc}") from exc


def _reserve_staging_path(staging_path: Path) -> None:
    descriptor = os.open(
        staging_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    )
    os.close(descriptor)


def _open_staging_database(staging_path: Path) -> SqliteDatabase:
    database = SqliteDatabase(
        str(staging_path),
        pragmas={"foreign_keys": 1},
    )
    database.bind(_MODELS)
    database.connect()
    return database


def _create_schema(database: SqliteDatabase) -> None:
    database.create_tables(_MODELS)


def _stamp_markers(database: SqliteDatabase) -> None:
    database.execute_sql(
        f"PRAGMA application_id = {RUN_PERSISTENCE_APPLICATION_ID}"
    )
    database.execute_sql(
        f"PRAGMA user_version = {RUN_PERSISTENCE_USER_VERSION}"
    )


def _close_database(database: SqliteDatabase) -> None:
    database.close()


def _validate_staging_database(staging_path: Path) -> None:
    database = SqliteDatabase(
        str(staging_path),
        pragmas={"foreign_keys": 1},
    )
    database.connect()
    try:
        result = validate_run_persistence_schema(database)
        if not result.valid:
            assert result.failure is not None
            raise RunPersistencePreparationError(result.failure.reason)
    finally:
        database.close()


def _publish_no_replace(staging_path: Path, target_path: Path) -> None:
    os.link(staging_path, target_path)


def _cleanup_staging_once(staging_path: Path) -> None:
    try:
        os.unlink(staging_path)
    except OSError:
        pass


def prepare_run_persistence_database(
    target_path: str | os.PathLike[str],
) -> Path:
    """Create, validate, close, and atomically publish one new database."""

    target = Path(target_path)
    if os.path.lexists(target):
        raise RunPersistencePreparationError(
            f"target already exists: {target}"
        )

    staging = target.with_name(
        f".{target.name}.{uuid4().hex}.staging"
    )
    staging_owned = False
    try:
        _run_phase(
            "_reserve_staging_path",
            lambda: _reserve_staging_path(staging),
        )
        staging_owned = True
        database = _run_phase(
            "_open_staging_database",
            lambda: _open_staging_database(staging),
        )
        try:
            _run_phase("_create_schema", lambda: _create_schema(database))
            _run_phase("_stamp_markers", lambda: _stamp_markers(database))
        finally:
            _run_phase("_close_database", lambda: _close_database(database))

        _run_phase(
            "_validate_staging_database",
            lambda: _validate_staging_database(staging),
        )
        _run_phase(
            "_publish_no_replace",
            lambda: _publish_no_replace(staging, target),
        )
        return target
    finally:
        if staging_owned and os.path.lexists(staging):
            _cleanup_staging_once(staging)
