"""Regression tests for the additive optional PLC-field database migration."""
from __future__ import annotations

import sqlite3

import pytest

from model.plc_field_offset_table import PlcFieldOffsetTable
from scripts import migrate_plc_optional_field_schema as migration


def _create_legacy_database(
    database_path,
    *,
    existing_optional_columns: tuple[str, ...] = (),
) -> None:
    optional_column_sql = "".join(
        f", {column_name} TEXT DEFAULT NULL"
        for column_name in existing_optional_columns
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE plc_field_offset_table (
                plc_id INTEGER PRIMARY KEY,
                start_signal_offset TEXT,
                current_value_offset TEXT
                {optional_column_sql}
            );
            INSERT INTO plc_field_offset_table (
                plc_id, start_signal_offset, current_value_offset
            ) VALUES (3, '0.0', '2.0');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _columns(database_path) -> set[str]:
    connection = sqlite3.connect(database_path)
    try:
        return {
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info("plc_field_offset_table")'
            ).fetchall()
        }
    finally:
        connection.close()


def _read_existing_offsets(database_path) -> tuple[int, str, str]:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            """
            SELECT plc_id, start_signal_offset, current_value_offset
            FROM plc_field_offset_table
            """
        ).fetchone()
    finally:
        connection.close()


def _read_new_offsets(database_path) -> tuple[None, None]:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            """
            SELECT rotation_speed_offset, res_testing_signal_offset
            FROM plc_field_offset_table
            """
        ).fetchone()
    finally:
        connection.close()


def test_plc_field_offset_model_exposes_nullable_optional_protocol_offsets():
    for name in ("rotation_speed_offset", "res_testing_signal_offset"):
        field = PlcFieldOffsetTable._meta.fields[name]

        assert field.null is True
        assert field.default is None


def test_migration_backs_up_adds_only_missing_columns_and_preserves_rows(
    tmp_path,
):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)

    result = migration.migrate_database(database_path)

    assert result.backup_path.is_file()
    assert result.backup_path.parent == database_path.parent
    assert _columns(result.backup_path) == {
        "plc_id",
        "start_signal_offset",
        "current_value_offset",
    }
    assert result.added_columns == (
        "plc_field_offset_table.rotation_speed_offset",
        "plc_field_offset_table.res_testing_signal_offset",
    )
    assert _read_existing_offsets(database_path) == (3, "0.0", "2.0")
    assert _read_new_offsets(database_path) == (None, None)


def test_migration_backup_includes_committed_uncheckpointed_wal_data(tmp_path):
    database_path = tmp_path / "jingchen.db"
    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript(
            """
            CREATE TABLE plc_field_offset_table (
                plc_id INTEGER PRIMARY KEY,
                start_signal_offset TEXT,
                current_value_offset TEXT
            );
            INSERT INTO plc_field_offset_table (
                plc_id, start_signal_offset, current_value_offset
            ) VALUES (3, '0.0', '2.0');
            """
        )
        writer.commit()
        wal_path = database_path.with_name(database_path.name + "-wal")
        assert wal_path.is_file()
        assert wal_path.stat().st_size > 0

        result = migration.migrate_database(database_path)

        assert _columns(result.backup_path) == {
            "plc_id",
            "start_signal_offset",
            "current_value_offset",
        }
        assert _read_existing_offsets(result.backup_path) == (
            3,
            "0.0",
            "2.0",
        )
    finally:
        writer.close()


def test_migration_is_idempotent_and_keeps_backing_up(tmp_path):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)
    first_result = migration.migrate_database(database_path)
    columns_after_first_run = _columns(database_path)

    second_result = migration.migrate_database(database_path)

    assert second_result.added_columns == ()
    assert second_result.backup_path.is_file()
    assert second_result.backup_path != first_result.backup_path
    assert _columns(database_path) == columns_after_first_run


def test_migration_rolls_back_both_column_additions_on_second_failure(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)
    monkeypatch.setattr(
        migration,
        "PLC_FIELD_OFFSET_COLUMNS",
        (
            ("rotation_speed_offset", "TEXT DEFAULT NULL"),
            ("res_testing_signal_offset", "TEXT NOT NULL"),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        migration.migrate_database(database_path)

    assert "rotation_speed_offset" not in _columns(database_path)
    assert "res_testing_signal_offset" not in _columns(database_path)
    assert _read_existing_offsets(database_path) == (3, "0.0", "2.0")
    assert len(list(tmp_path.glob("jingchen.db.backup-*"))) == 1


def test_migration_rejects_missing_required_table_without_mutating_database(
    tmp_path,
):
    database_path = tmp_path / "jingchen.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO unrelated_table (id, value) VALUES (1, 'preserve me')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="plc_field_offset_table"):
        migration.migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT id, value FROM unrelated_table"
        ).fetchall() == [(1, "preserve me")]
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        } == {"unrelated_table"}
    finally:
        connection.close()


def test_migration_preserves_existing_optional_column_and_adds_only_missing(
    tmp_path,
):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(
        database_path,
        existing_optional_columns=("rotation_speed_offset",),
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            UPDATE plc_field_offset_table
            SET rotation_speed_offset = '6.0'
            WHERE plc_id = 3
            """
        )
        connection.commit()
    finally:
        connection.close()

    result = migration.migrate_database(database_path)

    assert result.added_columns == (
        "plc_field_offset_table.res_testing_signal_offset",
    )
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            """
            SELECT rotation_speed_offset, res_testing_signal_offset
            FROM plc_field_offset_table
            """
        ).fetchone() == ("6.0", None)
    finally:
        connection.close()
