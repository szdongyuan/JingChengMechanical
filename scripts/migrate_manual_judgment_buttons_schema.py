"""Add the per-configuration manual-judgment button visibility column.

The migration operates only on the explicitly supplied database, creates a
recoverable timestamped backup, and is safe to run more than once.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Sequence


COLUMN_NAME = "manual_judgment_buttons_visible"
COLUMN_DEFINITION = "INTEGER NOT NULL DEFAULT 0"
EXPECTED_COLUMN_INFO = ("INTEGER", True, "0", 0)


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


def _column_info(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, tuple[str, bool, str | None, int]]:
    rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {
        str(row[1]): (str(row[2]).upper(), bool(row[3]), row[4], int(row[5]))
        for row in rows
    }


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
    """Back up and migrate one explicitly supplied JingCheng database."""
    resolved_path = Path(database_path).expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {resolved_path}")

    added_columns: list[str] = []
    connection = sqlite3.connect(resolved_path)
    backup_path = _backup_path_for(resolved_path)
    try:
        _backup_database(connection, backup_path)
        connection.execute("BEGIN IMMEDIATE")
        try:
            if "test_config_table" not in _table_names(connection):
                raise RuntimeError(
                    "Database schema is incomplete; missing tables: "
                    "test_config_table"
                )

            columns = _column_info(connection, "test_config_table")
            existing_definition = columns.get(COLUMN_NAME)
            if existing_definition is not None:
                if existing_definition != EXPECTED_COLUMN_INFO:
                    raise RuntimeError(
                        "Database schema is incompatible; invalid "
                        "test_config_table column definition: "
                        f"{COLUMN_NAME}"
                    )
            else:
                connection.execute(
                    'ALTER TABLE "test_config_table" '
                    f'ADD COLUMN "{COLUMN_NAME}" {COLUMN_DEFINITION}'
                )
                added_columns.append(f"test_config_table.{COLUMN_NAME}")
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
            "Back up a JingCheng database and add the manual-judgment "
            "button visibility column."
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
