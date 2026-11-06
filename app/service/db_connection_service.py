from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from peewee import OperationalError

from model.base_mode import DB_PATH, db
from app.service.torque.migration import (
    TorqueSchemaError,
    apply_torque_migration,
    needs_torque_migration,
    validate_torque_schema,
)


class DatabaseStartupError(RuntimeError):
    """Raised when the prepared startup database is missing or incomplete."""


class DbConnectionService:
    """
    数据库连接服务（长连接）。

    目标：
    - 程序启动时 connect 一次（或首次使用时自动 connect）
    - 程序退出时 close 一次
    - 业务代码/CRUD 不需要每次手动 connect/close
    """

    REQUIRED_RUNTIME_TABLES = frozenset(
        {
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
    )
    _REQUIRED_CHANNEL_CONFIG_COLUMNS = frozenset(
        {
            "fft_weighting",
            "fft_x_axis_scale",
            "stft_color_scale_enabled",
            "stft_color_scale_min_db",
            "stft_color_scale_max_db",
            "stft_score_enabled",
            "stft_score_summary_mode",
            "stft_score_percentile",
            "stft_score_loudness_reference_sone",
            "stft_score_modulation_reference",
            "stft_score_harmonic_ratio_reference",
            "stft_score_frequency_instability_reference",
            "stft_score_harmonic_start_order",
            "stft_score_harmonic_end_order",
        }
    )
    _REQUIRED_TEST_CONFIG_COLUMNS = frozenset(
        {
            "fault_types_json",
            "manual_judgment_buttons_visible",
            "vibration_view_channel_index",
            "input_hardware_device_id",
            "result_dashboard_layout",
            "current_meter_enabled",
            "current_meter_source",
            "current_meter_host",
            "current_meter_port",
            "current_meter_unit_id",
            "current_meter_register_type",
            "current_meter_register_address",
            "current_meter_register_count",
            "current_meter_data_type",
            "current_meter_byte_order",
            "current_meter_word_order",
            "current_meter_scale",
            "current_meter_offset",
            "current_meter_poll_interval_ms",
            "current_meter_timeout_ms",
        }
    )
    _REQUIRED_MANAGED_AUDIO_INPUT_COLUMNS = frozenset(
        {
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
    )
    _REQUIRED_PLC_FIELD_OFFSET_COLUMNS = frozenset(
        {
            "current_value_offset",
            "rotation_speed_offset",
            "res_testing_signal_offset",
        }
    )
    _REQUIRED_HISTORICAL_METRIC_CACHE_COLUMNS = frozenset(
        {
            "cache_id",
            "audio_data_id",
            "file_path",
            "file_size",
            "file_mtime_ns",
            "record_date",
            "product_model",
            "barcode",
            "label",
            "label_source",
            "analyse_config",
            "config_hash",
            "calibration_hash",
            "algorithm_version",
            "channel_index",
            "channel_label",
            "metric_key",
            "metric_label",
            "metric_value",
            "metric_unit",
            "status",
            "error_message",
            "computed_at",
        }
    )
    _REQUIRED_HISTORICAL_METRIC_CACHE_INDEXES = {
        (
            (
                "audio_data_id",
                "config_hash",
                "algorithm_version",
                "channel_index",
                "metric_key",
            ),
            True,
        ),
        (("audio_data_id",), False),
        (("product_model", "record_date"), False),
        (("metric_key", "status"), False),
    }
    _REQUIRED_HISTORICAL_METRIC_CACHE_COLUMN_DEFINITIONS = {
        "cache_id": ("INTEGER", True, True),
        "audio_data_id": ("TEXT", True, False),
        "file_path": ("TEXT", True, False),
        "file_size": ("INTEGER", True, False),
        "file_mtime_ns": ("INTEGER", True, False),
        "record_date": ("TEXT", True, False),
        "product_model": ("TEXT", True, False),
        "barcode": ("TEXT", False, False),
        "label": ("TEXT", False, False),
        "label_source": ("TEXT", False, False),
        "analyse_config": ("TEXT", False, False),
        "config_hash": ("TEXT", True, False),
        "calibration_hash": ("TEXT", True, False),
        "algorithm_version": ("TEXT", True, False),
        "channel_index": ("INTEGER", True, False),
        "channel_label": ("TEXT", True, False),
        "metric_key": ("TEXT", True, False),
        "metric_label": ("TEXT", True, False),
        "metric_value": ("REAL", False, False),
        "metric_unit": ("TEXT", True, False),
        "status": ("TEXT", True, False),
        "error_message": ("TEXT", False, False),
        "computed_at": ("TEXT", True, False),
    }

    def connect(self):
        self._ensure_database_file_exists()
        try:
            db.connect(reuse_if_open=True)
        except OperationalError as exc:
            if self._is_sqlite_file_open_error(exc):
                raise DatabaseStartupError(
                    f"Database file cannot be opened: {DB_PATH}. "
                    "Copy a prepared database before starting the application."
                ) from exc
            raise
        try:
            self._ensure_required_runtime_tables()
            self._ensure_required_runtime_columns(
                allow_missing_manual_judgment_visibility=True,
            )
            self._migrate_compatible_schema()
            self._ensure_required_runtime_columns()
            if DB_PATH != ":memory:":
                validate_torque_schema(db)
        except TorqueSchemaError as exc:
            if not db.is_closed():
                db.close()
            raise DatabaseStartupError(str(exc)) from exc
        except Exception:
            if not db.is_closed():
                db.close()
            raise

    def _ensure_database_file_exists(self) -> None:
        if DB_PATH == ":memory:":
            return

        try:
            exists = Path(DB_PATH).is_file()
        except OSError as exc:
            raise DatabaseStartupError(
                f"Database file does not exist: {DB_PATH}. "
                "Copy a prepared database before starting the application."
            ) from exc

        if not exists:
            raise DatabaseStartupError(
                f"Database file does not exist: {DB_PATH}. "
                "Copy a prepared database before starting the application."
            )

    def _migrate_compatible_schema(self) -> None:
        if DB_PATH == ":memory:":
            return
        column_name = "manual_judgment_buttons_visible"
        needs_manual_column = column_name not in self._table_columns("test_config_table")
        needs_torque_tables = needs_torque_migration(db)
        if not needs_manual_column and not needs_torque_tables:
            return

        database_path = Path(DB_PATH).resolve()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = database_path.with_name(
            f"{database_path.name}.backup-auto-{stamp}"
        )
        destination = None
        try:
            destination = sqlite3.connect(backup_path)
            db.connection().backup(destination)
        except Exception as exc:
            if destination is not None:
                destination.close()
            backup_path.unlink(missing_ok=True)
            raise DatabaseStartupError(
                "Database compatibility backup failed before schema upgrade: "
                f"{backup_path}."
            ) from exc
        else:
            destination.close()

        try:
            with db.atomic():
                if needs_manual_column:
                    db.execute_sql(
                        'ALTER TABLE "test_config_table" '
                        'ADD COLUMN "manual_judgment_buttons_visible" '
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                if needs_torque_tables:
                    apply_torque_migration(db)
                self._ensure_required_runtime_columns()
                validate_torque_schema(db)
        except Exception as exc:
            raise DatabaseStartupError(
                "Database compatibility upgrade failed; the original backup "
                f"is available at: {backup_path}."
            ) from exc

    def _ensure_required_runtime_tables(self) -> None:
        if DB_PATH == ":memory:":
            return

        existing_tables = self._runtime_table_names()
        missing_tables = sorted(self.REQUIRED_RUNTIME_TABLES - existing_tables)
        if missing_tables:
            raise DatabaseStartupError(
                "Database schema is incomplete: missing required runtime tables: "
                f"{', '.join(missing_tables)}. "
                "Copy a prepared database before starting the application."
            )

    def _ensure_required_runtime_columns(
        self,
        *,
        allow_missing_manual_judgment_visibility: bool = False,
    ) -> None:
        if DB_PATH == ":memory:":
            return

        self._ensure_required_table_columns(
            "channel_config_table",
            self._REQUIRED_CHANNEL_CONFIG_COLUMNS,
        )
        self._ensure_required_table_columns(
            "test_config_table",
            self._REQUIRED_TEST_CONFIG_COLUMNS
            - (
                {"manual_judgment_buttons_visible"}
                if allow_missing_manual_judgment_visibility
                else set()
            ),
        )
        self._ensure_fault_types_column_definition()
        if (
            not allow_missing_manual_judgment_visibility
            or "manual_judgment_buttons_visible" in self._table_columns("test_config_table")
        ):
            self._ensure_manual_judgment_visibility_column_definition()
        self._ensure_required_table_columns(
            "managed_audio_input_table",
            self._REQUIRED_MANAGED_AUDIO_INPUT_COLUMNS,
        )
        self._ensure_required_table_columns(
            "plc_field_offset_table",
            self._REQUIRED_PLC_FIELD_OFFSET_COLUMNS,
        )
        self._ensure_required_table_columns(
            "historical_metric_cache_table",
            self._REQUIRED_HISTORICAL_METRIC_CACHE_COLUMNS,
        )
        self._ensure_historical_metric_cache_column_definitions()
        self._ensure_historical_metric_cache_indexes()

    def _ensure_required_table_columns(
        self,
        table_name: str,
        required_columns: frozenset[str],
    ) -> None:
        missing_columns = sorted(required_columns - self._table_columns(table_name))
        if missing_columns:
            raise DatabaseStartupError(
                "Database schema is incomplete: missing required "
                f"{table_name} columns: "
                f"{', '.join(missing_columns)}. "
                "Copy a prepared database before starting the application."
            )

    def _table_columns(self, table_name: str) -> set[str]:
        cursor = db.execute_sql(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    def _ensure_historical_metric_cache_column_definitions(self) -> None:
        actual_definitions = self._table_column_definitions(
            "historical_metric_cache_table"
        )
        expected_definitions = (
            self._REQUIRED_HISTORICAL_METRIC_CACHE_COLUMN_DEFINITIONS
        )
        invalid_columns = sorted(
            column_name
            for column_name, expected in expected_definitions.items()
            if actual_definitions.get(column_name) != expected
        )
        if invalid_columns:
            raise DatabaseStartupError(
                "Database schema is incomplete: invalid "
                "historical_metric_cache_table column definitions: "
                f"{', '.join(invalid_columns)}. "
                "Copy a prepared database before starting the application."
            )

    def _ensure_fault_types_column_definition(self) -> None:
        cursor = db.execute_sql("PRAGMA table_info(test_config_table)")
        row = next(
            (candidate for candidate in cursor.fetchall() if candidate[1] == "fault_types_json"),
            None,
        )
        actual = (
            None
            if row is None
            else (row[2].upper(), bool(row[3]), row[4], int(row[5]))
        )
        if actual != ("TEXT", True, "'[]'", 0):
            raise DatabaseStartupError(
                "Database schema is incomplete: invalid test_config_table "
                "column definition: fault_types_json. "
                "Copy a prepared database before starting the application."
            )

    def _ensure_manual_judgment_visibility_column_definition(self) -> None:
        cursor = db.execute_sql("PRAGMA table_info(test_config_table)")
        row = next(
            (
                candidate
                for candidate in cursor.fetchall()
                if candidate[1] == "manual_judgment_buttons_visible"
            ),
            None,
        )
        actual = (
            None
            if row is None
            else (row[2].upper(), bool(row[3]), row[4], int(row[5]))
        )
        if actual != ("INTEGER", True, "0", 0):
            raise DatabaseStartupError(
                "Database schema is incomplete: invalid test_config_table "
                "column definition: manual_judgment_buttons_visible. "
                "Copy a prepared database before starting the application."
            )

    def _table_column_definitions(
        self,
        table_name: str,
    ) -> dict[str, tuple[str, bool, bool]]:
        cursor = db.execute_sql(f"PRAGMA table_info({table_name})")
        return {
            row[1]: (row[2].upper(), bool(row[3]), bool(row[5]))
            for row in cursor.fetchall()
        }

    def _ensure_historical_metric_cache_indexes(self) -> None:
        existing_indexes = self._table_index_definitions(
            "historical_metric_cache_table"
        )
        missing_indexes = (
            self._REQUIRED_HISTORICAL_METRIC_CACHE_INDEXES - existing_indexes
        )
        if missing_indexes:
            raise DatabaseStartupError(
                "Database schema is incomplete: missing required "
                "historical_metric_cache_table indexes. "
                "Copy a prepared database before starting the application."
            )

    def _table_index_definitions(
        self,
        table_name: str,
    ) -> set[tuple[tuple[str, ...], bool]]:
        cursor = db.execute_sql(f"PRAGMA index_list({table_name})")
        definitions = set()
        for _, index_name, unique, *_ in cursor.fetchall():
            info = db.execute_sql(f"PRAGMA index_info({index_name})").fetchall()
            definitions.add((tuple(row[2] for row in info), bool(unique)))
        return definitions

    def _is_sqlite_file_open_error(self, error: OperationalError) -> bool:
        if DB_PATH == ":memory:":
            return False
        return "unable to open database file" in str(error).lower()

    def _runtime_table_names(self) -> set[str]:
        cursor = db.execute_sql(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
        return {row[0] for row in cursor.fetchall()}

    def close(self):
        if not db.is_closed():
            db.close()

    def is_connected(self) -> bool:
        return not db.is_closed()
