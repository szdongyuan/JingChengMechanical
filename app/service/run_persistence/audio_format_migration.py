"""Backup-first transactional migration of the recognized v2 audio constraint."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from peewee import SqliteDatabase

from app.service.run_persistence.schema import (
    RUN_PERSISTENCE_USER_VERSION,
    validate_run_persistence_schema,
)


def _require_schema(database: SqliteDatabase, version: int, *, foreign_keys=True) -> None:
    validation = validate_run_persistence_schema(
        database, expected_user_version=version, require_foreign_keys=foreign_keys,
    )
    if not validation.valid:
        raise ValueError(f"Audio format migration refused: {validation.failure.reason}")


def migrate_known_v2(database: SqliteDatabase, path: Path) -> Path | None:
    """Upgrade known v2 in place; return the consistent pre-migration backup path.

    The caller owns the connection and startup admission. Unknown versions are
    left for the ordinary schema validator. No external audio files are opened.
    """
    if database.execute_sql('PRAGMA user_version').fetchone()[0] != 2:
        return None
    _require_schema(database, 2)
    backup_path = path.with_name(f'{path.name}.v2-backup-{uuid4().hex}.sqlite3')
    database.execute_sql('PRAGMA foreign_keys = OFF')
    try:
        database.execute_sql('BEGIN IMMEDIATE')
        _require_schema(database, 2, foreign_keys=False)
        # A separate reader can back up under our reserved write lock, including
        # committed WAL pages. No writer can change this snapshot during backup.
        with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
        create_sql = database.execute_sql(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audio'"
        ).fetchone()[0]
        indexes = database.execute_sql(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name='audio' AND sql IS NOT NULL"
        ).fetchall()
        create_sql = re.sub(
            r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"audio"|audio|\[audio\]|`audio`)',
            'CREATE TABLE "audio_format_v3"', create_sql, count=1, flags=re.IGNORECASE,
        )
        create_sql = re.sub(
            r"container_format\s*=\s*'WAV_PCM'",
            "container_format IN ('WAV_PCM', 'WAV_IEEE_FLOAT')", create_sql, count=1,
        )
        database.execute_sql(create_sql)
        database.execute_sql('INSERT INTO audio_format_v3 SELECT * FROM audio')
        database.execute_sql('DROP TABLE audio')
        database.execute_sql('ALTER TABLE audio_format_v3 RENAME TO audio')
        for (sql,) in indexes:
            database.execute_sql(sql)
        database.execute_sql(f'PRAGMA user_version = {RUN_PERSISTENCE_USER_VERSION}')
        _require_schema(database, RUN_PERSISTENCE_USER_VERSION, foreign_keys=False)
        if database.execute_sql('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Audio format migration failed foreign key validation')
        if database.execute_sql('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Audio format migration failed integrity validation')
        database.execute_sql('COMMIT')
    except Exception:
        if database.connection().in_transaction:
            database.execute_sql('ROLLBACK')
        raise
    finally:
        database.execute_sql('PRAGMA foreign_keys = ON')
    return backup_path
