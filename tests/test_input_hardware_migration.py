from __future__ import annotations

import sqlite3

from scripts.migrate_input_hardware_schema import migrate


def test_migration_creates_empty_registry_and_does_not_import_legacy_device(tmp_path):
    database = tmp_path / "jingchen.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE device_table (
            id INTEGER PRIMARY KEY,
            input_selected_device_name TEXT,
            mic_cal TEXT
        );
        INSERT INTO device_table VALUES (1, 'Legacy Mic', '{"In1": {}}');
        CREATE TABLE test_config_table (
            config_id INTEGER PRIMARY KEY,
            config_name TEXT,
            input_device_name TEXT
        );
        INSERT INTO test_config_table VALUES (1, 'Old Config', 'Legacy Mic');
        """
    )
    connection.close()

    migrate(database, make_backup=False)

    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(test_config_table)")
        }
        assert "input_hardware_device_id" in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM input_hardware_table"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT input_hardware_device_id FROM test_config_table"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT input_selected_device_name FROM device_table"
        ).fetchone()[0] == "Legacy Mic"
        connection.execute(
            """
            INSERT INTO input_hardware_table (
                alias, device_name, hostapi, samplerate, channel_count,
                mic_cal, created_time, updated_time
            ) VALUES ('A', 'Mic A', 'ASIO', 48000, 2, '{}', 'now', 'now')
            """
        )
        hardware_id = connection.execute(
            "SELECT hardware_id FROM input_hardware_table"
        ).fetchone()[0]
        connection.execute(
            "UPDATE test_config_table SET input_hardware_device_id = ?",
            (hardware_id,),
        )
        connection.commit()
    finally:
        connection.close()

    migrate(database, make_backup=False)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT input_hardware_device_id FROM test_config_table"
        ).fetchone()[0] == hardware_id
    finally:
        connection.close()
