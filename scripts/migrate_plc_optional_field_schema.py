"""Add the optional ordinary-PLC protocol columns required by the application.

The migration is intentionally additive: it backs up the explicitly supplied
database first and then adds only columns that are absent. It is safe to run
more than once.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Sequence


PLC_FIELD_OFFSET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rotation_speed_offset", "TEXT DEFAULT NULL"),
    ("res_testing_signal_offset", "TEXT DEFAULT NULL"),
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
    rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {str(row[1]) for row in rows}


def _backup_database(
    source: sqlite3.Connection,
    backup_path: Path,
) -> None:
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    except Exception:
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise
    else:
        destination.close()


def migrate_database(database_path: str | Path) -> MigrationResult:
    """Back up a database and add missing optional PLC protocol columns."""
    resolved_path = Path(database_path).expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {resolved_path}")

    backup_path = _backup_path_for(resolved_path)
    added_columns: list[str] = []
    connection = sqlite3.connect(resolved_path)
    try:
        _backup_database(connection, backup_path)

        required_tables = {"plc_field_offset_table"}
        missing_tables = sorted(required_tables - _table_names(connection))
        if missing_tables:
            raise RuntimeError(
                "Database schema is incomplete; missing tables: "
                + ", ".join(missing_tables)
            )

        existing_columns = _column_names(
            connection,
            "plc_field_offset_table",
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            for column_name, definition in PLC_FIELD_OFFSET_COLUMNS:
                if column_name in existing_columns:
                    continue
                connection.execute(
                    f'ALTER TABLE "plc_field_offset_table" '
                    f'ADD COLUMN "{column_name}" {definition}'
                )
                existing_columns.add(column_name)
                added_columns.append(
                    f"plc_field_offset_table.{column_name}"
                )
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
        description=(
            "Back up a JingCheng database and add optional PLC protocol "
            "columns."
        )
    )
    parser.add_argument(
        "database",
        type=Path,
        help="Explicit path to the jingchen.db database file.",
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
