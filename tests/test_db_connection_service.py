"""Regression tests for startup DB bootstrap and migration repair."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from app.service.config_service import ConfigService
from app.service.hardwareselect_service import HardwareSelectService
from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.audio_data_table import AudioDataTable
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from model.system_info_table import SystemInfoTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable as AiBindingTable
from model.test_config_table import TestConfigTable as ConfigTable
from model.training_model_table import TrainingModelTable
from model.users_table import UsersTable
from ui.modules.login import encrypt_password


EXPECTED_RUNTIME_TABLES = {
    "test_config_table",
    "channel_config_table",
    "ai_toolchain_table",
    "test_config_ai_binding_table",
    "ai_execution_table",
    "device_table",
    "audio_data_table",
    "users_table",
    "system_info_table",
    "training_model_table",
}

RUNTIME_MODELS = [
    ConfigTable,
    ChannelConfigTable,
    AiToolchainTable,
    AiBindingTable,
    AiExecutionTable,
    DeviceTable,
    AudioDataTable,
    UsersTable,
    SystemInfoTable,
    TrainingModelTable,
]


@contextmanager
def _temporary_runtime_db(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp_db = SqliteDatabase(db_path, pragmas={"foreign_keys": 1})

    import app.service.db_connection_service as svc_mod
    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", tmp_db)
    monkeypatch.setattr(svc_mod, "db", tmp_db)

    with tmp_db.bind_ctx(RUNTIME_MODELS):
        try:
            yield tmp_db, svc_mod.DbConnectionService(), svc_mod
        finally:
            if not tmp_db.is_closed():
                tmp_db.close()

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _index_definitions(tmp_db, table_name: str) -> list[tuple[str, tuple[str, ...], bool]]:
    cursor = tmp_db.execute_sql(f"PRAGMA index_list({table_name})")
    definitions = []
    for _, index_name, unique, *_ in cursor.fetchall():
        info = tmp_db.execute_sql(f"PRAGMA index_info({index_name})").fetchall()
        definitions.append((index_name, tuple(row[2] for row in info), bool(unique)))
    return definitions


def _table_columns(tmp_db, table_name: str) -> dict[str, tuple]:
    cursor = tmp_db.execute_sql(f"PRAGMA table_info({table_name})")
    return {row[1]: row for row in cursor.fetchall()}


def _runtime_table_names(tmp_db) -> set[str]:
    cursor = tmp_db.execute_sql(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    )
    return {row[0] for row in cursor.fetchall()}


def test_connect_bootstraps_full_runtime_schema_and_default_rows(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, svc_mod):
        assert {
            model._meta.table_name
            for model in svc_mod.DbConnectionService.BOOTSTRAP_MODELS
        } == EXPECTED_RUNTIME_TABLES

        svc.connect()

        assert _runtime_table_names(tmp_db) == EXPECTED_RUNTIME_TABLES
        assert "fft_nfft" in _table_columns(tmp_db, "channel_config_table")
        assert tmp_db.execute_sql(
            "SELECT id FROM device_table ORDER BY id"
        ).fetchall() == [(1,)]

        admin_row = tmp_db.execute_sql(
            """
            SELECT user_name, password, access_level, user_created_time, user_updated_time
            FROM users_table
            ORDER BY user_id
            """
        ).fetchone()
        assert admin_row[:3] == (
            "admin",
            encrypt_password("admin", "admin"),
            "Admin",
        )
        assert admin_row[3]
        assert admin_row[4]

        login_user = (
            UsersTable.select()
            .where(
                UsersTable.user_name == "admin",
                UsersTable.password == encrypt_password("admin", "admin"),
                UsersTable.access_level == "Admin",
            )
            .first()
        )
        assert login_user is not None


def test_connect_bootstraps_ai_binding_table_without_score_threshold(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, _):
        svc.connect()

        columns = _table_columns(tmp_db, "test_config_ai_binding_table")

        assert "score_threshold" not in columns


def test_connect_is_idempotent_for_seeded_rows(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, _):
        svc.connect()
        svc.connect()

        assert tmp_db.execute_sql(
            "SELECT COUNT(*) FROM device_table WHERE id = 1"
        ).fetchone() == (1,)
        assert tmp_db.execute_sql("SELECT COUNT(*) FROM users_table").fetchone() == (1,)
        assert tmp_db.execute_sql(
            "SELECT COUNT(*) FROM users_table WHERE user_name = 'admin'"
        ).fetchone() == (1,)


def test_connect_repairs_device_singleton_and_preserves_existing_rows(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, _):
        tmp_db.connect()
        tmp_db.execute_sql(
            """
            CREATE TABLE device_table (
                id INTEGER PRIMARY KEY,
                input_selected_device_id INTEGER,
                input_selected_device_name TEXT,
                input_selected_device_type TEXT,
                input_selected_device_samplerate TEXT,
                input_channel_count TEXT,
                output_selected_device_id INTEGER,
                output_selected_device_name TEXT,
                output_selected_device_type TEXT,
                output_selected_device_samplerate TEXT,
                output_channel_count TEXT,
                mic_cal TEXT
            )
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO device_table (
                id,
                input_selected_device_id,
                input_selected_device_name,
                input_selected_device_type,
                input_selected_device_samplerate,
                input_channel_count,
                output_selected_device_id,
                output_selected_device_name,
                output_selected_device_type,
                output_selected_device_samplerate,
                output_channel_count,
                mic_cal
            ) VALUES (
                2,
                7,
                'Legacy Input',
                'ASIO',
                '48000',
                '2',
                9,
                'Legacy Output',
                'WASAPI',
                '48000',
                '2',
                '{"In1": {"deviation_value": "1"}}'
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE users_table (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                access_level TEXT NOT NULL,
                user_created_time TEXT,
                user_updated_time TEXT
            )
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO users_table (
                user_id,
                user_name,
                password,
                access_level,
                user_created_time,
                user_updated_time
            ) VALUES (1, 'engineer', 'seeded', 'Engineer', '2026-04-02 00:00:00', '2026-04-02 00:00:00')
            """
        )

        svc.connect()

        assert tmp_db.execute_sql(
            "SELECT id FROM device_table ORDER BY id"
        ).fetchall() == [(1,), (2,)]
        assert tmp_db.execute_sql(
            """
            SELECT input_selected_device_name, output_selected_device_name
            FROM device_table
            WHERE id = 2
            """
        ).fetchone() == ("Legacy Input", "Legacy Output")
        assert tmp_db.execute_sql(
            "SELECT user_name, access_level FROM users_table ORDER BY user_id"
        ).fetchall() == [("engineer", "Engineer")]

        assert HardwareSelectService.load_io_devices() == {}


def test_connect_blank_device_singleton_stays_safe_for_startup_consumers(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (_, svc, _):
        svc.connect()

        device_info = HardwareSelectService.load_io_devices()

        assert device_info == {}
        input_info = device_info.get("input", {})
        assert float(input_info.get("samplerate", 48000.0)) == 48000.0
        assert int(input_info.get("channel_count", 1)) == 1


@pytest.mark.parametrize("mic_cal_value", [None, ""])
def test_connect_sparse_saved_device_row_keeps_mic_cal_mapping_for_startup_consumers(
    monkeypatch,
    mic_cal_value,
):
    with _temporary_runtime_db(monkeypatch) as (_, svc, _):
        svc.connect()
        DeviceTable.update(
            input_selected_device_id=7,
            input_selected_device_name="Legacy Input",
            input_selected_device_type="ASIO",
            input_selected_device_samplerate="48000",
            input_channel_count="2",
            mic_cal=mic_cal_value,
        ).where(DeviceTable.id == 1).execute()

        device_info = HardwareSelectService.load_io_devices()

        assert device_info == {
            "input": {
                "id": 7,
                "name": "Legacy Input",
                "type": "ASIO",
                "samplerate": "48000",
                "channel_count": "2",
            },
            "mic_cal": {},
        }
        assert device_info.get("mic_cal").get("In1", {}).get("is_selected") is None


def test_connect_migrates_legacy_config_tables_and_preserves_extra_columns(
    monkeypatch,
):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, _):
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
                updated_time TEXT NOT NULL DEFAULT '',
                legacy_note TEXT DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE channel_config_table (
                channel_config_id INTEGER PRIMARY KEY,
                config_id INTEGER NOT NULL REFERENCES test_config_table(config_id) ON DELETE CASCADE,
                channel_index INTEGER NOT NULL,
                stft_nfft INTEGER NOT NULL DEFAULT 1024,
                stft_overlap REAL NOT NULL DEFAULT 0.5,
                spl_weighting TEXT NOT NULL DEFAULT 'A',
                spl_time_constant REAL NOT NULL DEFAULT 0.125,
                spl_upper_limit REAL NOT NULL DEFAULT 85.0,
                spl_lower_limit REAL NOT NULL DEFAULT 40.0,
                fft_band_limits TEXT NOT NULL DEFAULT '[]',
                oct_weighting TEXT NOT NULL DEFAULT 'A',
                oct_band_limits TEXT NOT NULL DEFAULT '[]',
                ai_config TEXT NOT NULL DEFAULT '{}'
            )
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
                updated_time,
                legacy_note
            ) VALUES (1, 'Legacy Config', 5.0, 3.0, 0, '', '', 'keep me')
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO channel_config_table (
                channel_config_id,
                config_id,
                channel_index,
                stft_nfft,
                stft_overlap,
                spl_weighting,
                spl_time_constant,
                spl_upper_limit,
                spl_lower_limit,
                fft_band_limits,
                oct_weighting,
                oct_band_limits,
                ai_config
            ) VALUES (1, 1, 0, 1024, 0.5, 'A', 0.125, 85.0, 40.0, '[]', 'A', '[]', '{}')
            """
        )

        svc.connect()

        config_columns = _table_columns(tmp_db, "test_config_table")
        assert {
            "input_device_name",
            "input_device_hostapi",
            "input_device_samplerate",
            "output_device_name",
            "output_device_hostapi",
            "output_device_samplerate",
            "selected_channels",
            "plc_device_key",
            "plc_device_label",
            "analysis_window_offset_ms",
            "legacy_note",
        } <= set(config_columns)
        assert tmp_db.execute_sql(
            "SELECT legacy_note FROM test_config_table WHERE config_id = 1"
        ).fetchone() == ("keep me",)

        channel_columns = _table_columns(tmp_db, "channel_config_table")
        assert {
            "fft_nfft",
            "loudness_unit",
            "loudness_lower_limit",
            "loudness_upper_limit",
            "golden_sample_path",
            "golden_sample_label",
            "golden_sample_source_channel",
            "golden_sample_start_sample",
            "golden_sample_num_samples",
            "golden_sample_start_ms",
            "golden_sample_duration_ms",
            "golden_sample_sample_rate",
            "fft_golden_mode",
            "fft_show_golden_overlay",
            "oct_golden_mode",
            "oct_show_golden_overlay",
        } <= set(channel_columns)

        loaded = ConfigService(
            bus=None,
            golden_sample_service=SimpleNamespace(),
        ).load_config(1)
        assert loaded["analysis_window_offset_ms"] == 0
        assert loaded["plc_device_key"] is None
        assert loaded["plc_device_label"] is None
        assert loaded["selected_channels"] == [0]
        assert loaded["channels"] == [
            {
                "channel_index": 0,
                "stft_nfft": 1024,
                "fft_nfft": 4096,
                "stft_overlap": 0.5,
                "spl_weighting": "A",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "loudness_unit": "sone",
                "loudness_lower_limit": 0.0,
                "loudness_upper_limit": 60.0,
                "fft_band_limits": [],
                "oct_weighting": "A",
                "oct_band_limits": [],
                "ai_config": {},
                "golden_sample_path": None,
                "golden_sample_label": None,
                "golden_sample_source_channel": None,
                "golden_sample_start_sample": None,
                "golden_sample_num_samples": None,
                "golden_sample_start_ms": None,
                "golden_sample_duration_ms": None,
                "golden_sample_sample_rate": None,
                "fft_golden_mode": "off",
                "fft_show_golden_overlay": False,
                "oct_golden_mode": "off",
                "oct_show_golden_overlay": False,
            }
        ]


def test_connect_removes_score_threshold_from_legacy_ai_binding_table(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, _):
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
                score_threshold REAL DEFAULT NULL,
                updated_time TEXT NOT NULL DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE INDEX testconfigaibindingtable_toolchain_id
            ON test_config_ai_binding_table (toolchain_id)
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
                score_threshold,
                updated_time
            ) VALUES (1, 1, 1, 'legacy-toolchain', 1200, 250, '{"legacy": true}', 0.72, '')
            """
        )

        svc.connect()

        columns = _table_columns(tmp_db, "test_config_ai_binding_table")
        assert "score_threshold" not in columns
        assert "analysis_window_offset_ms" in columns
        assert tmp_db.execute_sql(
            """
            SELECT enabled, toolchain_id, timeout_ms, analysis_window_offset_ms, params_json
            FROM test_config_ai_binding_table
            WHERE config_id = 1
            """
        ).fetchone() == (
            1,
            "legacy-toolchain",
            1200,
            250,
            '{"legacy": true}',
        )
        assert (
            "testconfigaibindingtable_toolchain_id",
            ("toolchain_id",),
            False,
        ) in _index_definitions(tmp_db, "test_config_ai_binding_table")


def test_connect_migrates_legacy_ai_runtime_tables(monkeypatch):
    with _temporary_runtime_db(monkeypatch) as (tmp_db, svc, _):
        tmp_db.connect()
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
                updated_time TEXT NOT NULL DEFAULT '',
                legacy_source TEXT DEFAULT ''
            )
            """
        )
        tmp_db.execute_sql(
            """
            CREATE TABLE ai_execution_table (
                request_id TEXT PRIMARY KEY,
                test_run_id TEXT NOT NULL,
                config_id INTEGER NOT NULL,
                toolchain_id TEXT NOT NULL,
                module_status TEXT NOT NULL,
                started_time TEXT,
                finished_time TEXT,
                duration_ms INTEGER,
                exit_code INTEGER,
                error_summary TEXT,
                stdout_excerpt TEXT,
                stderr_excerpt TEXT,
                ai_label TEXT,
                score REAL,
                summary TEXT,
                confidence REAL,
                extra_json TEXT,
                artifacts_json TEXT,
                raw_response_json TEXT,
                legacy_payload TEXT DEFAULT ''
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
                updated_time,
                legacy_source
            ) VALUES (
                'legacy-toolchain',
                'Legacy Toolchain',
                '1.0.0',
                'legacy.entry',
                30000,
                1,
                '',
                '',
                'keep'
            )
            """
        )
        tmp_db.execute_sql(
            """
            INSERT INTO ai_execution_table (
                request_id,
                test_run_id,
                config_id,
                toolchain_id,
                module_status,
                started_time,
                finished_time,
                duration_ms,
                exit_code,
                error_summary,
                stdout_excerpt,
                stderr_excerpt,
                ai_label,
                score,
                summary,
                confidence,
                extra_json,
                artifacts_json,
                raw_response_json,
                legacy_payload
            ) VALUES (
                'req-1',
                'run-1',
                99,
                'legacy-toolchain',
                'completed',
                '2026-04-02 00:00:00',
                '2026-04-02 00:00:01',
                1000,
                0,
                NULL,
                'stdout',
                NULL,
                'OK',
                0.9,
                'summary',
                0.8,
                '{}',
                '[]',
                '{}',
                'keep'
            )
            """
        )

        svc.connect()

        toolchain_columns = _table_columns(tmp_db, "ai_toolchain_table")
        assert {
            "protocol_version",
            "execution_mode",
            "description",
            "legacy_source",
        } <= set(toolchain_columns)
        assert tmp_db.execute_sql(
            """
            SELECT protocol_version, execution_mode, description, legacy_source
            FROM ai_toolchain_table
            WHERE toolchain_id = 'legacy-toolchain'
            """
        ).fetchone() == (1, "oneshot_process", "", "keep")

        execution_columns = _table_columns(tmp_db, "ai_execution_table")
        assert {
            "toolchain_name",
            "toolchain_version",
            "protocol_version",
            "window_overridden",
            "default_window_offset_ms",
            "default_window_duration_ms",
            "final_window_offset_ms",
            "final_window_duration_ms",
            "is_current_effective",
            "request_json_path",
            "response_json_path",
            "artifacts_dir",
            "legacy_payload",
        } <= set(execution_columns)
        assert tmp_db.execute_sql(
            """
            SELECT
                protocol_version,
                window_overridden,
                default_window_offset_ms,
                default_window_duration_ms,
                final_window_offset_ms,
                final_window_duration_ms,
                is_current_effective,
                request_json_path,
                response_json_path,
                artifacts_dir,
                legacy_payload
            FROM ai_execution_table
            WHERE request_id = 'req-1'
            """
        ).fetchone() == (1, 0, 0, 0, 0, 0, 0, None, None, None, "keep")
