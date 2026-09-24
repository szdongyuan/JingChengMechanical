from __future__ import annotations

import pytest
from peewee import SqliteDatabase


def make_analysis_envelope(**overrides):
    analysis = {
        "window_start_seconds": 0.0,
        "window_end_seconds": 3.0,
        "algorithm_revision": "revision",
        "execution_timeout_seconds": 30.0,
        "queue_timeout_seconds": None,
        "toolchain": None,
    }
    analysis.update(overrides.pop("analysis", {}))
    envelope = {
        "schema": "analysis-input/v1",
        "config": {
            "config_id": 7,
            "label": "配置",
            "fault_types": [],
        },
        "workorder": {"workorder_id": "WO-1"},
        "channel_mapping": {
            "schema": "audio-channel-map/v1",
            "channel_order": [0],
        },
        "analysis": analysis,
    }
    envelope.update(overrides)
    return envelope


@pytest.fixture
def raw_schema_database(tmp_path):
    from model.analysis_attempt_table import AnalysisAttemptTable
    from model.audio_table import AudioTable
    from model.run_table import RunTable

    db_path = tmp_path / "run-persistence.sqlite3"
    database = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    models = (RunTable, AudioTable, AnalysisAttemptTable)
    database.bind(models)
    database.connect()
    database.create_tables(models)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def run_persistence_database(raw_schema_database):
    """Compatibility name for the raw model/DDL behavior fixture."""

    return raw_schema_database


@pytest.fixture
def validator_database(raw_schema_database):
    raw_schema_database.execute_sql("PRAGMA application_id = 1245924688")
    raw_schema_database.execute_sql("PRAGMA user_version = 3")
    return raw_schema_database


@pytest.fixture
def prepared_database_path(tmp_path):
    from app.service.run_persistence.prepare_database import (
        prepare_run_persistence_database,
    )

    target = tmp_path / "prepared-run-persistence.sqlite3"
    return prepare_run_persistence_database(target)


@pytest.fixture
def now() -> str:
    return "2026-07-27T12:34:56.123456Z"
