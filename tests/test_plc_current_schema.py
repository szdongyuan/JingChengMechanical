"""Regression tests for the additive PLC-current database migration."""
from __future__ import annotations

import sqlite3

import pytest

from model.plc_field_offset_table import PlcFieldOffsetTable
from scripts import migrate_plc_current_schema as migration


def _create_legacy_database(database_path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE test_config_table (
                config_id INTEGER PRIMARY KEY,
                config_name TEXT NOT NULL,
                legacy_note TEXT
            );
            INSERT INTO test_config_table (
                config_id, config_name, legacy_note
            ) VALUES (7, 'Legacy config', 'preserve me');

            CREATE TABLE plc_field_offset_table (
                plc_id INTEGER PRIMARY KEY,
                start_signal_offset TEXT
            );
            INSERT INTO plc_field_offset_table (
                plc_id, start_signal_offset
            ) VALUES (3, '0.0');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _columns(database_path, table_name: str) -> set[str]:
    connection = sqlite3.connect(database_path)
    try:
        return {
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        }
    finally:
        connection.close()


def test_plc_field_offset_model_exposes_nullable_current_value_offset():
    field = PlcFieldOffsetTable._meta.fields["current_value_offset"]

    assert field.null is True
    assert field.default is None


def test_migration_backs_up_first_adds_missing_columns_and_preserves_data(tmp_path):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)

    result = migration.migrate_database(database_path)

    assert result.backup_path.is_file()
    assert result.backup_path.parent == database_path.parent
    assert _columns(result.backup_path, "test_config_table") == {
        "config_id",
        "config_name",
        "legacy_note",
    }
    assert _columns(database_path, "test_config_table") >= {
        name for name, _definition in migration.TEST_CONFIG_COLUMNS
    }
    assert "current_value_offset" in _columns(
        database_path, "plc_field_offset_table"
    )

    connection = sqlite3.connect(database_path)
    try:
        config_row = connection.execute(
            """
            SELECT config_id, config_name, legacy_note,
                   current_meter_enabled, current_meter_source,
                   current_meter_host, current_meter_port
            FROM test_config_table
            """
        ).fetchone()
        plc_row = connection.execute(
            """
            SELECT plc_id, start_signal_offset, current_value_offset
            FROM plc_field_offset_table
            """
        ).fetchone()
    finally:
        connection.close()

    assert config_row == (
        7,
        "Legacy config",
        "preserve me",
        0,
        "modbus_tcp",
        None,
        502,
    )
    assert plc_row == (3, "0.0", None)
    assert len(result.added_columns) == 16


def test_migration_is_idempotent_and_keeps_backing_up(tmp_path):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)
    first_result = migration.migrate_database(database_path)
    columns_after_first_run = {
        table_name: _columns(database_path, table_name)
        for table_name in ("test_config_table", "plc_field_offset_table")
    }

    second_result = migration.migrate_database(database_path)

    assert second_result.added_columns == ()
    assert second_result.backup_path.is_file()
    assert second_result.backup_path != first_result.backup_path
    assert {
        table_name: _columns(database_path, table_name)
        for table_name in ("test_config_table", "plc_field_offset_table")
    } == columns_after_first_run


def test_migration_rolls_back_all_column_additions_on_failure(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)
    monkeypatch.setattr(
        migration,
        "TEST_CONFIG_COLUMNS",
        (
            ("temporary_valid", "TEXT DEFAULT NULL"),
            ("temporary_invalid", "TEXT NOT NULL"),
        ),
    )
    monkeypatch.setattr(migration, "PLC_FIELD_OFFSET_COLUMNS", ())

    with pytest.raises(sqlite3.OperationalError):
        migration.migrate_database(database_path)

    assert "temporary_valid" not in _columns(database_path, "test_config_table")
    assert "temporary_invalid" not in _columns(database_path, "test_config_table")
    assert len(list(tmp_path.glob("jingchen.db.backup-*"))) == 1


def test_migration_rejects_missing_required_table_without_mutating_database(tmp_path):
    database_path = tmp_path / "jingchen.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE test_config_table (config_id INTEGER PRIMARY KEY)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="plc_field_offset_table"):
        migration.migrate_database(database_path)

    assert _columns(database_path, "test_config_table") == {"config_id"}
    assert len(list(tmp_path.glob("jingchen.db.backup-*"))) == 1
