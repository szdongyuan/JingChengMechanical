"""Tests for DbConnectionService creating AI tables at startup."""
from __future__ import annotations

import os
import tempfile

from peewee import SqliteDatabase

from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable


def _index_definitions(tmp_db, table_name: str) -> list[tuple[tuple[str, ...], bool]]:
    cursor = tmp_db.execute_sql(f"PRAGMA index_list({table_name})")
    definitions = []
    for _, index_name, unique, *_ in cursor.fetchall():
        info = tmp_db.execute_sql(f"PRAGMA index_info({index_name})").fetchall()
        definitions.append((tuple(row[2] for row in info), bool(unique)))
    return definitions


def _bind_all_models(tmp_db):
    from model.channel_config_table import ChannelConfigTable
    from model.test_config_table import TestConfigTable

    all_models = [
        TestConfigTable,
        ChannelConfigTable,
        AiToolchainTable,
        AiBindingTable,
        AiExecutionTable,
    ]
    tmp_db.bind(all_models)
    return all_models


def test_connect_creates_ai_tables(monkeypatch):
    """DbConnectionService.connect() must create all 3 AI tables in a fresh DB."""
    # Use a temp file DB so we can inspect it independently
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        tmp_db = SqliteDatabase(db_path, pragmas={"foreign_keys": 1})
        # Monkeypatch the module-level db object used by DbConnectionService
        import model.base_mode as base_mod

        monkeypatch.setattr(base_mod, "db", tmp_db)

        # Re-import to pick up monkeypatched db
        from app.service.db_connection_service import DbConnectionService

        svc = DbConnectionService()
        # We need to also monkeypatch the db reference in the service module
        import app.service.db_connection_service as svc_mod

        monkeypatch.setattr(svc_mod, "db", tmp_db)

        # Rebind all model classes to the temp DB
        _bind_all_models(tmp_db)

        svc.connect()

        # Verify all AI tables exist by querying them
        cursor = tmp_db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {row[0] for row in cursor.fetchall()}
        assert "ai_toolchain_table" in table_names
        assert "test_config_ai_binding_table" in table_names
        assert "ai_execution_table" in table_names
        assert "migration_marker_table" not in table_names

        cursor = tmp_db.execute_sql("PRAGMA table_info(test_config_ai_binding_table)")
        column_names = {row[1] for row in cursor.fetchall()}
        assert "score_threshold" in column_names
        assert "analysis_window_offset_ms" not in column_names
        assert (("toolchain_id",), False) in _index_definitions(
            tmp_db, "test_config_ai_binding_table"
        )

        svc.close()
        tmp_db.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass  # Windows may still hold the file briefly


def test_migrate_adds_plc_and_analysis_window_offset_columns_to_existing_db(monkeypatch):
    """DbConnectionService.connect() must add missing config columns to existing DBs."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        tmp_db = SqliteDatabase(db_path, pragmas={"foreign_keys": 1})
        import model.base_mode as base_mod

        monkeypatch.setattr(base_mod, "db", tmp_db)

        from app.service.db_connection_service import DbConnectionService

        svc = DbConnectionService()
        import app.service.db_connection_service as svc_mod

        monkeypatch.setattr(svc_mod, "db", tmp_db)

        _bind_all_models(tmp_db)

        tmp_db.connect()
        tmp_db.execute_sql(
            """
            CREATE TABLE test_config_table (
                config_id INTEGER PRIMARY KEY,
                config_name TEXT NOT NULL UNIQUE,
                record_duration REAL NOT NULL DEFAULT 5.0,
                analysis_duration REAL NOT NULL DEFAULT 3.0,
                scan_triggers_test INTEGER NOT NULL DEFAULT 0,
                created_time TEXT NOT NULL,
                updated_time TEXT NOT NULL
            )
            """
        )
        tmp_db.close()

        svc.connect()

        cursor = tmp_db.execute_sql("PRAGMA table_info(test_config_table)")
        column_names = {row[1] for row in cursor.fetchall()}

        assert "trigger_type" in column_names
        assert "input_device_name" in column_names
        assert "input_device_hostapi" in column_names
        assert "input_device_samplerate" in column_names
        assert "output_device_name" in column_names
        assert "output_device_hostapi" in column_names
        assert "output_device_samplerate" in column_names
        assert "selected_channels" in column_names
        assert "plc_device_key" in column_names
        assert "plc_device_label" in column_names
        assert "analysis_window_offset_ms" in column_names

        svc.close()
        tmp_db.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass  # Windows may still hold the file briefly


def test_migrate_existing_binding_table_keeps_legacy_offset_column_untouched(
    monkeypatch,
):
    """connect() should not run legacy binding-table rebuild logic for old offset columns."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        tmp_db = SqliteDatabase(db_path, pragmas={"foreign_keys": 1})
        import model.base_mode as base_mod

        monkeypatch.setattr(base_mod, "db", tmp_db)

        from app.service.db_connection_service import DbConnectionService

        svc = DbConnectionService()
        import app.service.db_connection_service as svc_mod

        monkeypatch.setattr(svc_mod, "db", tmp_db)

        _bind_all_models(tmp_db)

        # Create a binding table WITHOUT score_threshold (simulating old schema)
        tmp_db.connect()
        tmp_db.execute_sql(
            """
            CREATE TABLE test_config_table (
                config_id INTEGER PRIMARY KEY,
                config_name TEXT NOT NULL UNIQUE,
                record_duration REAL NOT NULL DEFAULT 5.0,
                analysis_duration REAL NOT NULL DEFAULT 3.0,
                scan_triggers_test INTEGER NOT NULL DEFAULT 0,
                created_time TEXT NOT NULL DEFAULT '',
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE ai_toolchain_table (
                toolchain_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '',
                entrypoint TEXT NOT NULL,
                default_timeout_ms INTEGER NOT NULL DEFAULT 30000,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_time TEXT NOT NULL DEFAULT '',
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE test_config_ai_binding_table (
                binding_id INTEGER PRIMARY KEY,
                config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0,
                toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL,
                timeout_ms INTEGER,
                analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0,
                params_json TEXT NOT NULL DEFAULT '{}',
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO ai_toolchain_table (
                toolchain_id,
                display_name,
                version,
                entrypoint,
                default_timeout_ms,
                enabled,
                created_time,
                updated_time
            ) VALUES ('legacy-toolchain', 'Legacy Toolchain', '1.0.0', 'legacy.entry', 30000, 1, '', '')
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO test_config_table (
                config_id,
                config_name,
                record_duration,
                analysis_duration,
                scan_triggers_test,
                created_time,
                updated_time
            ) VALUES (1, 'Legacy Config', 5.0, 3.0, 0, '', '')
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO test_config_ai_binding_table (
                binding_id,
                config_id,
                enabled,
                toolchain_id,
                timeout_ms,
                analysis_window_offset_ms,
                params_json,
                updated_time
            ) VALUES (1, 1, 1, 'legacy-toolchain', 1200, 250, '{"legacy": true}', '')
            """
        )
        tmp_db.close()

        svc.connect()

        cursor = tmp_db.execute_sql("PRAGMA table_info(test_config_ai_binding_table)")
        columns = {row[1]: row for row in cursor.fetchall()}
        assert "score_threshold" in columns
        assert "analysis_window_offset_ms" in columns
        assert columns["score_threshold"][4] == "NULL"
        assert (("toolchain_id",), False) in _index_definitions(
            tmp_db, "test_config_ai_binding_table"
        )

        cursor = tmp_db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {row[0] for row in cursor.fetchall()}
        assert "migration_marker_table" not in table_names

        cursor = tmp_db.execute_sql(
            """
            SELECT enabled, toolchain_id, timeout_ms, analysis_window_offset_ms, params_json, score_threshold
            FROM test_config_ai_binding_table
            WHERE config_id = 1
            """
        )
        assert cursor.fetchone() == (
            1,
            "legacy-toolchain",
            1200,
            250,
            '{"legacy": true}',
            None,
        )

        svc.close()
        tmp_db.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

