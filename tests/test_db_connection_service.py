"""Regression tests for connection-only database startup."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from peewee import OperationalError, SqliteDatabase

import app.service.db_connection_service as svc_mod
import model.base_mode as base_mod
from app.service.db_connection_service import DatabaseStartupError


EXPECTED_RUNTIME_TABLES = {
    "test_config_table",
    "channel_config_table",
    "ai_toolchain_table",
    "test_config_ai_binding_table",
    "ai_execution_table",
    "device_table",
    "managed_audio_input_table",
    "audio_data_table",
    "users_table",
    "system_info_table",
    "training_model_table",
    "plc_table",
    "plc_field_offset_table",
    "historical_metric_cache_table",
}


def _runtime_table_names(tmp_db: SqliteDatabase) -> set[str]:
    cursor = tmp_db.execute_sql(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    )
    return {row[0] for row in cursor.fetchall()}


def _table_columns(tmp_db: SqliteDatabase, table_name: str) -> dict[str, tuple]:
    cursor = tmp_db.execute_sql(f"PRAGMA table_info({table_name})")
    return {row[1]: row for row in cursor.fetchall()}


def _table_column_names(tmp_db: SqliteDatabase, table_name: str) -> list[str]:
    cursor = tmp_db.execute_sql(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def _index_definitions(
    tmp_db: SqliteDatabase,
    table_name: str,
) -> list[tuple[str, tuple[str, ...], bool]]:
    cursor = tmp_db.execute_sql(f"PRAGMA index_list({table_name})")
    definitions = []
    for _, index_name, unique, *_ in cursor.fetchall():
        info = tmp_db.execute_sql(f"PRAGMA index_info({index_name})").fetchall()
        definitions.append((index_name, tuple(row[2] for row in info), bool(unique)))
    return definitions


def _create_sqlite_file(db_path: Path) -> None:
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    tmp_db.close()


CHANNEL_CONFIG_COLUMN_SQL = {
    "fft_weighting": "TEXT NOT NULL DEFAULT 'Z'",
    "fft_x_axis_scale": "TEXT NOT NULL DEFAULT 'linear'",
    "stft_color_scale_enabled": "INTEGER NOT NULL DEFAULT 0",
    "stft_color_scale_min_db": "REAL NOT NULL DEFAULT -120.0",
    "stft_color_scale_max_db": "REAL NOT NULL DEFAULT 0.0",
    "stft_score_enabled": "INTEGER NOT NULL DEFAULT 1",
    "stft_score_summary_mode": "TEXT NOT NULL DEFAULT 'percentile'",
    "stft_score_percentile": "REAL NOT NULL DEFAULT 10.0",
    "stft_score_loudness_reference_sone": "REAL NOT NULL DEFAULT 10.0",
    "stft_score_modulation_reference": "REAL NOT NULL DEFAULT 0.20",
    "stft_score_harmonic_ratio_reference": "REAL NOT NULL DEFAULT 0.25",
    "stft_score_frequency_instability_reference": "REAL NOT NULL DEFAULT 0.05",
    "stft_score_harmonic_start_order": "INTEGER NOT NULL DEFAULT 2",
    "stft_score_harmonic_end_order": "INTEGER NOT NULL DEFAULT 100",
}


TEST_CONFIG_COLUMN_SQL = {
    "fault_types_json": "TEXT NOT NULL DEFAULT '[]'",
    "input_hardware_device_id": "INTEGER DEFAULT NULL",
    "vibration_view_channel_index": "INTEGER DEFAULT NULL",
    "result_dashboard_layout": "TEXT DEFAULT NULL",
    "current_meter_enabled": "INTEGER NOT NULL DEFAULT 0",
    "current_meter_source": "TEXT NOT NULL DEFAULT 'modbus_tcp'",
    "current_meter_host": "TEXT DEFAULT NULL",
    "current_meter_port": "INTEGER NOT NULL DEFAULT 502",
    "current_meter_unit_id": "INTEGER NOT NULL DEFAULT 1",
    "current_meter_register_type": "TEXT NOT NULL DEFAULT 'input'",
    "current_meter_register_address": "INTEGER NOT NULL DEFAULT 0",
    "current_meter_register_count": "INTEGER NOT NULL DEFAULT 1",
    "current_meter_data_type": "TEXT NOT NULL DEFAULT 'uint16'",
    "current_meter_byte_order": "TEXT NOT NULL DEFAULT 'big'",
    "current_meter_word_order": "TEXT NOT NULL DEFAULT 'big'",
    "current_meter_scale": "REAL NOT NULL DEFAULT 0.001",
    "current_meter_offset": "REAL NOT NULL DEFAULT 0.0",
    "current_meter_poll_interval_ms": "INTEGER NOT NULL DEFAULT 200",
    "current_meter_timeout_ms": "INTEGER NOT NULL DEFAULT 1000",
}


def test_runtime_schema_requires_managed_input_without_legacy_hardware_fallback():
    service = svc_mod.DbConnectionService()
    assert "managed_audio_input_table" in service.REQUIRED_RUNTIME_TABLES
    assert "input_hardware_table" not in service.REQUIRED_RUNTIME_TABLES
    assert service._REQUIRED_MANAGED_AUDIO_INPUT_COLUMNS == {
        "hardware_id",
        "alias",
        "hardware_revision",
        "stable_backend_uid",
        "descriptor_device_name",
        "descriptor_hostapi_name",
        "descriptor_sample_rate_hz",
        "descriptor_input_channels",
        "calibration_revision",
        "calibration_json",
        "created_time",
        "updated_time",
    }


@pytest.mark.parametrize(
    "definition",
    [
        None,
        "TEXT DEFAULT '[]'",
        "INTEGER NOT NULL DEFAULT '[]'",
        "TEXT NOT NULL DEFAULT '{}'",
        'TEXT NOT NULL DEFAULT "[]"',
    ],
)
def test_connect_requires_exact_fault_types_column_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    definition: str | None,
) -> None:
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"test_config_table"}):
        _create_table(tmp_db, table_name)
    columns = dict(TEST_CONFIG_COLUMN_SQL)
    if definition is None:
        columns.pop("fault_types_json")
    else:
        columns["fault_types_json"] = definition
    column_sql = ", ".join(
        f"{name} {column_definition}"
        for name, column_definition in columns.items()
    )
    tmp_db.execute_sql(
        f"CREATE TABLE test_config_table (id INTEGER PRIMARY KEY, {column_sql})"
    )
    tmp_db.execute_sql("INSERT INTO test_config_table (id) VALUES (1)")
    before = tmp_db.execute_sql("PRAGMA table_info(test_config_table)").fetchall()
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="fault_types_json"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert check_db.execute_sql(
            "PRAGMA table_info(test_config_table)"
        ).fetchall() == before
        assert check_db.execute_sql(
            "SELECT id FROM test_config_table"
        ).fetchall() == [(1,)]
    finally:
        check_db.close()


def test_connect_rejects_fault_types_column_in_primary_key_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"test_config_table"}):
        _create_table(tmp_db, table_name)
    columns = dict(TEST_CONFIG_COLUMN_SQL)
    columns["fault_types_json"] = "TEXT NOT NULL DEFAULT '[]' PRIMARY KEY"
    column_sql = ", ".join(
        f"{name} {column_definition}"
        for name, column_definition in columns.items()
    )
    tmp_db.execute_sql(
        f"CREATE TABLE test_config_table (id INTEGER, {column_sql})"
    )
    tmp_db.execute_sql("INSERT INTO test_config_table (id) VALUES (1)")
    before = tmp_db.execute_sql("PRAGMA table_info(test_config_table)").fetchall()
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="fault_types_json"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert check_db.execute_sql(
            "PRAGMA table_info(test_config_table)"
        ).fetchall() == before
        assert check_db.execute_sql(
            "SELECT id, fault_types_json FROM test_config_table"
        ).fetchall() == [(1, "[]")]
    finally:
        check_db.close()


PLC_FIELD_OFFSET_COLUMN_SQL = {
    "current_value_offset": "TEXT DEFAULT NULL",
    "rotation_speed_offset": "TEXT DEFAULT NULL",
    "res_testing_signal_offset": "TEXT DEFAULT NULL",
}


HISTORICAL_METRIC_CACHE_COLUMN_SQL = {
    "cache_id": "INTEGER NOT NULL PRIMARY KEY",
    "audio_data_id": "TEXT NOT NULL",
    "file_path": "TEXT NOT NULL",
    "file_size": "INTEGER NOT NULL",
    "file_mtime_ns": "INTEGER NOT NULL",
    "record_date": "TEXT NOT NULL",
    "product_model": "TEXT NOT NULL",
    "barcode": "TEXT",
    "label": "TEXT",
    "label_source": "TEXT",
    "analyse_config": "TEXT",
    "config_hash": "TEXT NOT NULL",
    "calibration_hash": "TEXT NOT NULL",
    "algorithm_version": "TEXT NOT NULL",
    "channel_index": "INTEGER NOT NULL",
    "channel_label": "TEXT NOT NULL",
    "metric_key": "TEXT NOT NULL",
    "metric_label": "TEXT NOT NULL",
    "metric_value": "REAL",
    "metric_unit": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    "error_message": "TEXT",
    "computed_at": "TEXT NOT NULL",
}


def _create_historical_metric_cache_indexes(tmp_db: SqliteDatabase) -> None:
    tmp_db.execute_sql(
        """
        CREATE UNIQUE INDEX historical_metric_cache_unique_metric
        ON historical_metric_cache_table (
            audio_data_id,
            config_hash,
            algorithm_version,
            channel_index,
            metric_key
        )
        """
    )
    tmp_db.execute_sql(
        """
        CREATE INDEX historical_metric_cache_audio_data_id
        ON historical_metric_cache_table (audio_data_id)
        """
    )
    tmp_db.execute_sql(
        """
        CREATE INDEX historical_metric_cache_product_date
        ON historical_metric_cache_table (product_model, record_date)
        """
    )
    tmp_db.execute_sql(
        """
        CREATE INDEX historical_metric_cache_metric_status
        ON historical_metric_cache_table (metric_key, status)
        """
    )


def _create_table(tmp_db: SqliteDatabase, table_name: str) -> None:
    if table_name == "managed_audio_input_table":
        tmp_db.execute_sql(
            """
            CREATE TABLE managed_audio_input_table (
                hardware_id INTEGER PRIMARY KEY,
                alias TEXT NOT NULL,
                hardware_revision INTEGER NOT NULL,
                stable_backend_uid TEXT,
                descriptor_device_name TEXT NOT NULL,
                descriptor_hostapi_name TEXT NOT NULL,
                descriptor_sample_rate_hz REAL NOT NULL,
                descriptor_input_channels INTEGER NOT NULL,
                calibration_revision INTEGER NOT NULL,
                calibration_json TEXT NOT NULL,
                created_time TEXT NOT NULL,
                updated_time TEXT NOT NULL
            )
            """
        )
        return
    if table_name == "test_config_table":
        column_sql = ", ".join(
            f"{name} {definition}"
            for name, definition in TEST_CONFIG_COLUMN_SQL.items()
        )
        tmp_db.execute_sql(
            f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, {column_sql})"
        )
        return
    if table_name == "channel_config_table":
        column_sql = ", ".join(
            f"{name} {definition}"
            for name, definition in CHANNEL_CONFIG_COLUMN_SQL.items()
        )
        tmp_db.execute_sql(
            f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, {column_sql})"
        )
        return
    if table_name == "historical_metric_cache_table":
        column_sql = ", ".join(
            f"{name} {definition}"
            for name, definition in HISTORICAL_METRIC_CACHE_COLUMN_SQL.items()
        )
        tmp_db.execute_sql(f"CREATE TABLE {table_name} ({column_sql})")
        _create_historical_metric_cache_indexes(tmp_db)
        return
    if table_name == "plc_field_offset_table":
        column_sql = ", ".join(
            f"{name} {definition}"
            for name, definition in PLC_FIELD_OFFSET_COLUMN_SQL.items()
        )
        tmp_db.execute_sql(
            f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, {column_sql})"
        )
        return
    tmp_db.execute_sql(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)")


def _create_runtime_tables(tmp_db: SqliteDatabase) -> None:
    for table_name in sorted(EXPECTED_RUNTIME_TABLES):
        _create_table(tmp_db, table_name)


def test_database_name_and_options_file_path_uses_open_existing_uri(tmp_path):
    db_path = tmp_path / "jingchen.db"

    database_name, options = base_mod._database_name_and_options(str(db_path))

    assert database_name == f"{db_path.resolve().as_uri()}?mode=rw"
    assert options == {"uri": True}


def test_database_name_and_options_literal_memory_keeps_test_database():
    database_name, options = base_mod._database_name_and_options(":memory:")

    assert database_name == ":memory:"
    assert options == {}


def test_shared_database_uses_open_existing_uri_for_file_paths():
    if base_mod.DB_PATH == ":memory:":
        pytest.skip("literal in-memory database is the only non-file startup mode")

    assert base_mod.db.database == f"{Path(base_mod.DB_PATH).resolve().as_uri()}?mode=rw"
    assert base_mod.db.connect_params.get("uri") is True


def test_connect_missing_database_path_raises_without_creating_file(
    monkeypatch,
    tmp_path,
):
    missing_path = tmp_path / "missing" / "jingchen.db"
    spy_db = MagicMock()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(missing_path))
    monkeypatch.setattr(svc_mod, "db", spy_db)

    with pytest.raises(DatabaseStartupError, match="Database file does not exist"):
        svc_mod.DbConnectionService().connect()

    spy_db.connect.assert_not_called()
    assert not missing_path.exists()
    assert not missing_path.parent.exists()


def test_connect_nonliteral_memory_uri_is_treated_as_file_path(monkeypatch):
    spy_db = MagicMock()
    monkeypatch.setattr(svc_mod, "DB_PATH", "file::memory:?cache=shared")
    monkeypatch.setattr(svc_mod, "db", spy_db)

    with pytest.raises(DatabaseStartupError, match="Database file does not exist"):
        svc_mod.DbConnectionService().connect()

    spy_db.connect.assert_not_called()


def test_connect_wraps_sqlite_file_open_failure_after_path_check(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    db_path.touch()
    open_error = OperationalError("unable to open database file")
    spy_db = MagicMock()
    spy_db.connect.side_effect = open_error
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", spy_db)

    with pytest.raises(DatabaseStartupError, match="Database file cannot be opened") as exc:
        svc_mod.DbConnectionService().connect()

    assert exc.value.__cause__ is open_error
    spy_db.connect.assert_called_once_with(reuse_if_open=True)
    spy_db.execute_sql.assert_not_called()


def test_connect_existing_empty_database_raises_without_creating_tables(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    _create_sqlite_file(db_path)
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="Database schema is incomplete"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _runtime_table_names(check_db) == set()
    finally:
        check_db.close()


def test_connect_existing_partial_database_raises_and_preserves_data(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    tmp_db.execute_sql("CREATE TABLE users_table (user_id INTEGER PRIMARY KEY, user_name TEXT)")
    tmp_db.execute_sql(
        "INSERT INTO users_table (user_id, user_name) VALUES (7, 'operator')"
    )
    tmp_db.execute_sql("CREATE TABLE test_config_table (id INTEGER PRIMARY KEY)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="Database schema is incomplete"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _runtime_table_names(check_db) == {"users_table", "test_config_table"}
        assert _table_columns(check_db, "users_table") == {
            "user_id": (0, "user_id", "INTEGER", 0, None, 1),
            "user_name": (1, "user_name", "TEXT", 0, None, 0),
        }
        assert check_db.execute_sql(
            "SELECT user_id, user_name FROM users_table"
        ).fetchall() == [(7, "operator")]
    finally:
        check_db.close()


def test_connect_existing_prepared_database_accepts_required_channel_columns(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.execute_sql("INSERT INTO channel_config_table (id) VALUES (1)")
    tmp_db.execute_sql("DROP TABLE users_table")
    tmp_db.execute_sql(
        "CREATE TABLE users_table (user_id INTEGER PRIMARY KEY, user_name TEXT)"
    )
    tmp_db.execute_sql(
        "INSERT INTO users_table (user_id, user_name) VALUES (7, 'operator')"
    )
    tmp_db.execute_sql(
        "CREATE UNIQUE INDEX users_table_user_name ON users_table (user_name)"
    )
    expected_tables = _runtime_table_names(tmp_db)
    expected_user_columns = _table_columns(tmp_db, "users_table")
    expected_indexes = _index_definitions(tmp_db, "users_table")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    svc_mod.DbConnectionService().connect()

    assert _runtime_table_names(tmp_db) == expected_tables
    assert expected_tables == EXPECTED_RUNTIME_TABLES
    assert _table_columns(tmp_db, "users_table") == expected_user_columns
    channel_columns = _table_columns(tmp_db, "channel_config_table")
    assert channel_columns["fft_weighting"][2] == "TEXT"
    assert tmp_db.execute_sql(
        "SELECT fft_weighting FROM channel_config_table WHERE id = 1"
    ).fetchone() == ("Z",)
    assert channel_columns["fft_x_axis_scale"][2] == "TEXT"
    assert tmp_db.execute_sql(
        "SELECT fft_x_axis_scale FROM channel_config_table WHERE id = 1"
    ).fetchone() == ("linear",)
    assert _index_definitions(tmp_db, "users_table") == expected_indexes
    assert tmp_db.execute_sql(
        "SELECT user_id, user_name FROM users_table"
    ).fetchall() == [(7, "operator")]
    assert tmp_db.execute_sql("SELECT COUNT(*) FROM device_table").fetchone() == (0,)
    tmp_db.close()


def test_connect_existing_prepared_database_accepts_stft_color_scale_columns(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.execute_sql("INSERT INTO channel_config_table (id) VALUES (1)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    svc_mod.DbConnectionService().connect()

    channel_columns = _table_columns(tmp_db, "channel_config_table")
    assert channel_columns["stft_color_scale_enabled"][2] == "INTEGER"
    assert channel_columns["stft_color_scale_min_db"][2] == "REAL"
    assert channel_columns["stft_color_scale_max_db"][2] == "REAL"
    row = tmp_db.execute_sql(
        "SELECT stft_color_scale_enabled, stft_color_scale_min_db, stft_color_scale_max_db "
        "FROM channel_config_table WHERE id = 1"
    ).fetchone()
    assert row == (0, -120.0, 0.0)
    tmp_db.close()


def test_connect_existing_prepared_database_accepts_stft_score_columns(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.execute_sql("INSERT INTO channel_config_table (id) VALUES (1)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    svc_mod.DbConnectionService().connect()

    columns = _table_columns(tmp_db, "channel_config_table")
    assert columns["stft_score_enabled"][2] == "INTEGER"
    assert columns["stft_score_summary_mode"][2] == "TEXT"
    assert columns["stft_score_percentile"][2] == "REAL"
    row = tmp_db.execute_sql(
        "SELECT stft_score_enabled, stft_score_summary_mode, "
        "stft_score_percentile, stft_score_harmonic_start_order, "
        "stft_score_harmonic_end_order FROM channel_config_table WHERE id = 1"
    ).fetchone()
    assert row == (1, "percentile", 10.0, 2, 100)
    tmp_db.close()


def test_connect_existing_prepared_database_accepts_historical_metric_cache_table(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    svc_mod.DbConnectionService().connect()

    columns = _table_columns(tmp_db, "historical_metric_cache_table")
    assert columns["audio_data_id"][2] == "TEXT"
    assert columns["audio_data_id"][3] == 1
    assert columns["file_mtime_ns"][2] == "INTEGER"
    assert columns["file_mtime_ns"][3] == 1
    assert columns["metric_value"][2] == "REAL"
    assert columns["metric_value"][3] == 0
    assert columns["cache_id"][5] == 1
    indexes = _index_definitions(tmp_db, "historical_metric_cache_table")
    assert (
        "historical_metric_cache_unique_metric",
        (
            "audio_data_id",
            "config_hash",
            "algorithm_version",
            "channel_index",
            "metric_key",
        ),
        True,
    ) in indexes
    assert any(
        columns == ("metric_key", "status") and unique is False
        for _name, columns, unique in indexes
    )
    tmp_db.close()


def test_connect_rejects_incomplete_channel_config_schema_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"channel_config_table"}):
        _create_table(tmp_db, table_name)
    tmp_db.execute_sql("CREATE TABLE channel_config_table (id INTEGER PRIMARY KEY)")
    tmp_db.execute_sql("INSERT INTO channel_config_table (id) VALUES (1)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="missing required channel_config_table"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _table_column_names(check_db, "channel_config_table") == ["id"]
        assert check_db.execute_sql(
            "SELECT id FROM channel_config_table"
        ).fetchall() == [(1,)]
    finally:
        check_db.close()


def test_connect_rejects_missing_vibration_view_column_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"test_config_table"}):
        _create_table(tmp_db, table_name)
    tmp_db.execute_sql("CREATE TABLE test_config_table (id INTEGER PRIMARY KEY)")
    tmp_db.execute_sql("INSERT INTO test_config_table (id) VALUES (1)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="missing required test_config_table"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _table_column_names(check_db, "test_config_table") == ["id"]
        assert check_db.execute_sql(
            "SELECT id FROM test_config_table"
        ).fetchall() == [(1,)]
    finally:
        check_db.close()


def test_connect_rejects_missing_current_meter_source_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"test_config_table"}):
        _create_table(tmp_db, table_name)
    columns_without_source = {
        name: definition
        for name, definition in TEST_CONFIG_COLUMN_SQL.items()
        if name != "current_meter_source"
    }
    column_sql = ", ".join(
        f"{name} {definition}"
        for name, definition in columns_without_source.items()
    )
    tmp_db.execute_sql(
        f"CREATE TABLE test_config_table (id INTEGER PRIMARY KEY, {column_sql})"
    )
    tmp_db.execute_sql("INSERT INTO test_config_table (id) VALUES (1)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="current_meter_source"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert "current_meter_source" not in _table_column_names(
            check_db, "test_config_table"
        )
        assert check_db.execute_sql(
            "SELECT id FROM test_config_table"
        ).fetchall() == [(1,)]
    finally:
        check_db.close()


def test_connect_rejects_missing_plc_current_value_offset_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(
        EXPECTED_RUNTIME_TABLES - {"plc_field_offset_table"}
    ):
        _create_table(tmp_db, table_name)
    tmp_db.execute_sql(
        "CREATE TABLE plc_field_offset_table (plc_id INTEGER PRIMARY KEY)"
    )
    tmp_db.execute_sql("INSERT INTO plc_field_offset_table (plc_id) VALUES (9)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="current_value_offset"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _table_column_names(check_db, "plc_field_offset_table") == [
            "plc_id"
        ]
        assert check_db.execute_sql(
            "SELECT plc_id FROM plc_field_offset_table"
        ).fetchall() == [(9,)]
    finally:
        check_db.close()


def test_connect_rejects_missing_optional_plc_protocol_columns_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.execute_sql("DROP TABLE plc_field_offset_table")
    tmp_db.execute_sql(
        """
        CREATE TABLE plc_field_offset_table (
            plc_id INTEGER PRIMARY KEY,
            current_value_offset TEXT DEFAULT NULL
        )
        """
    )
    tmp_db.execute_sql(
        """
        INSERT INTO plc_field_offset_table (plc_id, current_value_offset)
        VALUES (9, '2.0')
        """
    )
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="rotation_speed_offset"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _table_column_names(check_db, "plc_field_offset_table") == [
            "plc_id",
            "current_value_offset",
        ]
        assert check_db.execute_sql(
            """
            SELECT plc_id, current_value_offset
            FROM plc_field_offset_table
            """
        ).fetchall() == [(9, "2.0")]
    finally:
        check_db.close()


def test_connect_rejects_missing_historical_metric_cache_column_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(
        EXPECTED_RUNTIME_TABLES - {"historical_metric_cache_table"}
    ):
        _create_table(tmp_db, table_name)
    broken_columns = {
        name: definition
        for name, definition in HISTORICAL_METRIC_CACHE_COLUMN_SQL.items()
        if name != "calibration_hash"
    }
    tmp_db.execute_sql(
        "CREATE TABLE historical_metric_cache_table ("
        + ", ".join(
            f"{name} {definition}" for name, definition in broken_columns.items()
        )
        + ")"
    )
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(
        DatabaseStartupError,
        match="missing required historical_metric_cache_table",
    ):
        svc_mod.DbConnectionService().connect()


def test_connect_rejects_missing_historical_metric_cache_unique_index_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    _create_runtime_tables(tmp_db)
    tmp_db.execute_sql("DROP INDEX historical_metric_cache_unique_metric")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(
        DatabaseStartupError,
        match="missing required historical_metric_cache_table indexes",
    ):
        svc_mod.DbConnectionService().connect()


def test_connect_rejects_wrong_historical_metric_cache_column_definition_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(
        EXPECTED_RUNTIME_TABLES - {"historical_metric_cache_table"}
    ):
        _create_table(tmp_db, table_name)
    broken_columns = dict(HISTORICAL_METRIC_CACHE_COLUMN_SQL)
    broken_columns["metric_value"] = "TEXT"
    tmp_db.execute_sql(
        "CREATE TABLE historical_metric_cache_table ("
        + ", ".join(
            f"{name} {definition}" for name, definition in broken_columns.items()
        )
        + ")"
    )
    _create_historical_metric_cache_indexes(tmp_db)
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(
        DatabaseStartupError,
        match="invalid historical_metric_cache_table column definitions",
    ):
        svc_mod.DbConnectionService().connect()


def test_connect_rejects_missing_result_dashboard_layout_column_without_mutating(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "jingchen.db"
    tmp_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    tmp_db.connect()
    for table_name in sorted(EXPECTED_RUNTIME_TABLES - {"test_config_table"}):
        _create_table(tmp_db, table_name)
    tmp_db.execute_sql(
        "CREATE TABLE test_config_table "
        "(id INTEGER PRIMARY KEY, vibration_view_channel_index INTEGER DEFAULT NULL)"
    )
    tmp_db.execute_sql("INSERT INTO test_config_table (id) VALUES (1)")
    tmp_db.close()
    monkeypatch.setattr(svc_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with pytest.raises(DatabaseStartupError, match="missing required test_config_table"):
        svc_mod.DbConnectionService().connect()

    check_db = SqliteDatabase(str(db_path), pragmas={"foreign_keys": 1})
    check_db.connect()
    try:
        assert _table_column_names(check_db, "test_config_table") == [
            "id",
            "vibration_view_channel_index",
        ]
        assert check_db.execute_sql(
            "SELECT id, vibration_view_channel_index FROM test_config_table"
        ).fetchall() == [(1, None)]
    finally:
        check_db.close()


def test_connect_allows_literal_memory_database_and_skips_file_schema_checks(
    monkeypatch,
):
    tmp_db = SqliteDatabase(":memory:", pragmas={"foreign_keys": 1})
    monkeypatch.setattr(svc_mod, "DB_PATH", ":memory:")
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    svc_mod.DbConnectionService().connect()

    assert not tmp_db.is_closed()
    assert _runtime_table_names(tmp_db) == set()
    tmp_db.close()
