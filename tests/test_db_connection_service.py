"""Tests for DbConnectionService creating AI tables at startup."""
from __future__ import annotations

import os
import tempfile

from peewee import SqliteDatabase

from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable


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

        svc.connect()

        # Verify all AI tables exist by querying them
        cursor = tmp_db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {row[0] for row in cursor.fetchall()}
        assert "ai_toolchain_table" in table_names
        assert "test_config_ai_binding_table" in table_names
        assert "ai_execution_table" in table_names

        svc.close()
        tmp_db.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass  # Windows may still hold the file briefly
