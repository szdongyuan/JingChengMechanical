from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


CREATE_INPUT_HARDWARE_TABLE = """
CREATE TABLE input_hardware_table (
    hardware_id INTEGER NOT NULL PRIMARY KEY,
    alias TEXT NOT NULL UNIQUE,
    device_name TEXT NOT NULL,
    hostapi TEXT NOT NULL,
    samplerate REAL NOT NULL,
    channel_count INTEGER NOT NULL,
    mic_cal TEXT NOT NULL DEFAULT '{}',
    created_time TEXT NOT NULL,
    updated_time TEXT NOT NULL
)
"""


def migrate(database_path: Path, *, make_backup: bool = True) -> Path | None:
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    backup_path = None
    if make_backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database_path.with_suffix(f".before_input_hardware_{stamp}.db")
        shutil.copy2(database_path, backup_path)

    connection = sqlite3.connect(database_path)
    try:
        with connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "input_hardware_table" not in tables:
                connection.execute(CREATE_INPUT_HARDWARE_TABLE)
                connection.execute(
                    """
                    CREATE UNIQUE INDEX input_hardware_identity
                    ON input_hardware_table(
                        device_name, hostapi, samplerate, channel_count
                    )
                    """
                )

            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(test_config_table)")
            }
            if "input_hardware_device_id" not in columns:
                connection.execute(
                    "ALTER TABLE test_config_table "
                    "ADD COLUMN input_hardware_device_id INTEGER DEFAULT NULL"
                )
            # Intentionally do not migrate device_table or legacy bindings.
            # Existing/new values are not touched when this script is rerun.
    finally:
        connection.close()
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    backup = migrate(args.database, make_backup=not args.no_backup)
    print(f"Migrated: {args.database.resolve()}")
    if backup is not None:
        print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
