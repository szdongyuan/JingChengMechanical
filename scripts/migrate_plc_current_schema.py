"""Add the Modbus and PLC-current columns required by the application.

The migration is intentionally additive: it backs up the database first and
then adds only columns that are absent. It is safe to run more than once.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
from typing import Sequence


TEST_CONFIG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("current_meter_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("current_meter_source", "TEXT NOT NULL DEFAULT 'modbus_tcp'"),
    ("current_meter_host", "TEXT DEFAULT NULL"),
    ("current_meter_port", "INTEGER NOT NULL DEFAULT 502"),
    ("current_meter_unit_id", "INTEGER NOT NULL DEFAULT 1"),
    ("current_meter_register_type", "TEXT NOT NULL DEFAULT 'input'"),
    ("current_meter_register_address", "INTEGER NOT NULL DEFAULT 0"),
    ("current_meter_register_count", "INTEGER NOT NULL DEFAULT 1"),
    ("current_meter_data_type", "TEXT NOT NULL DEFAULT 'uint16'"),
    ("current_meter_byte_order", "TEXT NOT NULL DEFAULT 'big'"),
    ("current_meter_word_order", "TEXT NOT NULL DEFAULT 'big'"),
    ("current_meter_scale", "REAL NOT NULL DEFAULT 0.001"),
    ("current_meter_offset", "REAL NOT NULL DEFAULT 0.0"),
    ("current_meter_poll_interval_ms", "INTEGER NOT NULL DEFAULT 200"),
    ("current_meter_timeout_ms", "INTEGER NOT NULL DEFAULT 1000"),
)

PLC_FIELD_OFFSET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("current_value_offset", "TEXT DEFAULT NULL"),
)


@dataclass(frozen=True)
class MigrationResult:
    database_path: Path
    backup_path: Path
    added_columns: tuple[str, ...]


def _backup_path_for(database_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stem = f"{database_path.name}.backup-{timestamp}"
    candidate = database_path.with_name(stem)
    suffix = 1
    while candidate.exists():
        candidate = database_path.with_name(f"{stem}-{suffix}")
        suffix += 1
    return candidate


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    # table_name is always one of the constants declared in this module.
    rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {str(row[1]) for row in rows}


def migrate_database(database_path: str | Path) -> MigrationResult:
    """Back up and add all missing PLC-current schema columns."""
    resolved_path = Path(database_path).expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {resolved_path}")

    backup_path = _backup_path_for(resolved_path)
    shutil.copy2(resolved_path, backup_path)

    added_columns: list[str] = []
    connection = sqlite3.connect(resolved_path)
    try:
        required_tables = {"test_config_table", "plc_field_offset_table"}
        missing_tables = sorted(required_tables - _table_names(connection))
        if missing_tables:
            raise RuntimeError(
                "Database schema is incomplete; missing tables: "
                + ", ".join(missing_tables)
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            for table_name, definitions in (
                ("test_config_table", TEST_CONFIG_COLUMNS),
                ("plc_field_offset_table", PLC_FIELD_OFFSET_COLUMNS),
            ):
                existing_columns = _column_names(connection, table_name)
                for column_name, definition in definitions:
                    if column_name in existing_columns:
                        continue
                    connection.execute(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {definition}'
                    )
                    existing_columns.add(column_name)
                    added_columns.append(f"{table_name}.{column_name}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()

    return MigrationResult(
        database_path=resolved_path,
        backup_path=backup_path,
        added_columns=tuple(added_columns),
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up a JingCheng database and add PLC-current columns."
    )
    parser.add_argument(
        "database",
        type=Path,
        help="Path to the jingchen.db database file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = migrate_database(args.database)
    print(f"Database: {result.database_path}")
    print(f"Backup: {result.backup_path}")
    if result.added_columns:
        print("Added columns: " + ", ".join(result.added_columns))
    else:
        print("Schema already up to date; no columns added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
