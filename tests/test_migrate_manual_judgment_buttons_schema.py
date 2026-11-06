"""Tests for the manual-judgment button visibility schema migration."""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3

import pytest

import scripts.migrate_manual_judgment_buttons_schema as migration
from scripts.migrate_manual_judgment_buttons_schema import migrate_database


EXPECTED_DEFINITION = ("INTEGER", True, "0", 0)


def _create_database(
    database_path: Path,
    *,
    visibility_definition: str | None = None,
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        extra_column = (
            ""
            if visibility_definition is None
            else ", manual_judgment_buttons_visible " + visibility_definition
        )
        config_id_definition = (
            "INTEGER"
            if visibility_definition and "PRIMARY KEY" in visibility_definition
            else "INTEGER PRIMARY KEY"
        )
        connection.execute(
            "CREATE TABLE test_config_table ("
            f"config_id {config_id_definition}, "
            "config_name TEXT NOT NULL"
            f"{extra_column})"
        )
        connection.executemany(
            "INSERT INTO test_config_table (config_id, config_name) VALUES (?, ?)",
            [(1, "Alpha"), (2, "Beta")],
        )
        connection.commit()
    finally:
        connection.close()


def _column_definition(database_path: Path) -> tuple[str, bool, str | None, int]:
    connection = sqlite3.connect(database_path)
    try:
        row = next(
            row
            for row in connection.execute(
                "PRAGMA table_info(test_config_table)"
            ).fetchall()
            if row[1] == "manual_judgment_buttons_visible"
        )
        return row[2].upper(), bool(row[3]), row[4], int(row[5])
    finally:
        connection.close()


def test_migration_rejects_missing_resolved_database_without_creating_backup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing" / "jingchen.db"

    with pytest.raises(
        FileNotFoundError,
        match=re.escape(str(database_path.resolve())),
    ):
        migrate_database(database_path)

    assert list(tmp_path.rglob("*.backup-*")) == []


def test_migration_rejects_database_without_required_table_and_keeps_backup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jingchen.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="test_config_table"):
        migrate_database(database_path)

    backups = list(tmp_path.glob("jingchen.db.backup-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == [("unrelated",)]
    finally:
        backup.close()


def test_migration_adds_exact_column_preserves_rows_and_creates_backup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jingchen.db"
    _create_database(database_path)

    result = migrate_database(database_path)

    assert result.database_path == database_path.resolve()
    assert result.backup_path.parent == database_path.parent.resolve()
    assert result.backup_path.name.startswith("jingchen.db.backup-")
    assert result.backup_path.is_file()
    assert result.added_columns == (
        "test_config_table.manual_judgment_buttons_visible",
    )
    assert _column_definition(database_path) == EXPECTED_DEFINITION

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT config_id, config_name, manual_judgment_buttons_visible "
            "FROM test_config_table ORDER BY config_id"
        ).fetchall() == [(1, "Alpha", 0), (2, "Beta", 0)]
    finally:
        connection.close()

    backup = sqlite3.connect(result.backup_path)
    try:
        backup_columns = {
            row[1]
            for row in backup.execute("PRAGMA table_info(test_config_table)")
        }
        assert "manual_judgment_buttons_visible" not in backup_columns
        assert backup.execute(
            "SELECT config_id, config_name FROM test_config_table ORDER BY config_id"
        ).fetchall() == [(1, "Alpha"), (2, "Beta")]
    finally:
        backup.close()


def test_migration_backup_includes_committed_wal_content(tmp_path: Path) -> None:
    database_path = tmp_path / "jingchen.db"
    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE test_config_table ("
            "config_id INTEGER PRIMARY KEY, config_name TEXT NOT NULL)"
        )
        writer.execute(
            "INSERT INTO test_config_table (config_id, config_name) "
            "VALUES (1, 'WAL row')"
        )
        writer.commit()
        wal_path = database_path.with_name(database_path.name + "-wal")
        assert wal_path.is_file()
        assert wal_path.stat().st_size > 0

        result = migrate_database(database_path)

        backup = sqlite3.connect(result.backup_path)
        try:
            assert backup.execute(
                "SELECT config_id, config_name FROM test_config_table"
            ).fetchall() == [(1, "WAL row")]
            assert "manual_judgment_buttons_visible" not in {
                row[1]
                for row in backup.execute("PRAGMA table_info(test_config_table)")
            }
        finally:
            backup.close()
    finally:
        writer.close()


def test_backup_failure_removes_incomplete_backup(tmp_path: Path) -> None:
    backup_path = tmp_path / "jingchen.db.backup-failed"

    class FailingSource:
        def backup(self, destination: sqlite3.Connection) -> None:
            destination.execute("CREATE TABLE partial (id INTEGER)")
            destination.commit()
            raise RuntimeError("simulated backup failure")

    with pytest.raises(RuntimeError, match="simulated backup failure"):
        migration._backup_database(FailingSource(), backup_path)

    assert not backup_path.exists()


def test_migration_is_idempotent_for_exact_existing_column(tmp_path: Path) -> None:
    database_path = tmp_path / "jingchen.db"
    _create_database(
        database_path,
        visibility_definition="INTEGER NOT NULL DEFAULT 0",
    )

    first = migrate_database(database_path)
    second = migrate_database(database_path)

    assert first.added_columns == ()
    assert second.added_columns == ()
    assert first.backup_path != second.backup_path
    assert first.backup_path.is_file()
    assert second.backup_path.is_file()
    assert _column_definition(database_path) == EXPECTED_DEFINITION


@pytest.mark.parametrize(
    "definition",
    [
        "INTEGER DEFAULT 0",
        "TEXT NOT NULL DEFAULT 0",
        "INTEGER NOT NULL DEFAULT 1",
        "INTEGER NOT NULL DEFAULT 0 PRIMARY KEY",
    ],
)
def test_migration_rejects_incompatible_existing_definition_and_rolls_back(
    tmp_path: Path,
    definition: str,
) -> None:
    database_path = tmp_path / "jingchen.db"
    _create_database(database_path, visibility_definition=definition)
    before_bytes = database_path.read_bytes()

    with pytest.raises(RuntimeError, match="manual_judgment_buttons_visible"):
        migrate_database(database_path)

    assert database_path.read_bytes() == before_bytes
    backups = list(tmp_path.glob("jingchen.db.backup-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute(
            "SELECT config_id, config_name FROM test_config_table ORDER BY config_id"
        ).fetchall() == [(1, "Alpha"), (2, "Beta")]
    finally:
        backup.close()
