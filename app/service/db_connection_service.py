from __future__ import annotations

from peewee import OperationalError

from model.base_mode import db
from model.test_config_table import TestConfigTable
from model.channel_config_table import ChannelConfigTable
from model.ai_toolchain_table import AiToolchainTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.ai_execution_table import AiExecutionTable


class DbConnectionService:
    """
    数据库连接服务（长连接）。

    目标：
    - 程序启动时 connect 一次（或首次使用时自动 connect）
    - 程序退出时 close 一次
    - 业务代码/CRUD 不需要每次手动 connect/close
    """

    def connect(self):
        db.connect(reuse_if_open=True)
        db.create_tables(
            [
                TestConfigTable,
                ChannelConfigTable,
                AiToolchainTable,
                TestConfigAiBindingTable,
                AiExecutionTable,
            ],
            safe=True,
        )
        self._migrate()

    def _migrate(self):
        columns = [
            ("input_device_name", "TEXT"),
            ("input_device_hostapi", "TEXT"),
            ("input_device_samplerate", "REAL"),
            ("output_device_name", "TEXT"),
            ("output_device_hostapi", "TEXT"),
            ("output_device_samplerate", "REAL"),
            ("selected_channels", "TEXT DEFAULT '[]'"),
            ("analysis_window_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for column_name, type_spec in columns:
            try:
                db.execute_sql(
                    f"ALTER TABLE test_config_table ADD COLUMN {column_name} {type_spec}"
                )
            except OperationalError:
                pass
        self._ensure_channel_config_columns()
        self._backfill_channel_loudness_defaults()
        self._ensure_ai_binding_columns()
        self._ensure_ai_binding_indexes()

    def _get_table_columns(self, table_name: str) -> set[str]:
        cursor = db.execute_sql(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    def _ensure_ai_binding_columns(self):
        binding_columns = [
            ("score_threshold", "REAL DEFAULT NULL"),
        ]
        for column_name, type_spec in binding_columns:
            try:
                db.execute_sql(
                    f"ALTER TABLE test_config_ai_binding_table ADD COLUMN {column_name} {type_spec}"
                )
            except OperationalError:
                pass

    def _ensure_channel_config_columns(self):
        channel_columns = [
            ("loudness_unit", "TEXT DEFAULT 'sone'"),
            ("loudness_lower_limit", "REAL DEFAULT 0"),
            ("loudness_upper_limit", "REAL DEFAULT 60"),
        ]
        for column_name, type_spec in channel_columns:
            try:
                db.execute_sql(
                    f"ALTER TABLE channel_config_table ADD COLUMN {column_name} {type_spec}"
                )
            except OperationalError:
                pass

    def _backfill_channel_loudness_defaults(self):
        channel_columns = self._get_table_columns("channel_config_table")
        required = {
            "loudness_unit",
            "loudness_lower_limit",
            "loudness_upper_limit",
        }
        if not required.issubset(channel_columns):
            return
        db.execute_sql(
            """
            UPDATE channel_config_table
            SET
                loudness_unit = COALESCE(loudness_unit, 'sone'),
                loudness_lower_limit = COALESCE(loudness_lower_limit, 0),
                loudness_upper_limit = COALESCE(loudness_upper_limit, 60)
            WHERE
                loudness_unit IS NULL
                OR loudness_lower_limit IS NULL
                OR loudness_upper_limit IS NULL
            """
        )

    def _ensure_ai_binding_indexes(self):
        db.execute_sql(
            """
            CREATE INDEX IF NOT EXISTS testconfigaibindingtable_toolchain_id
            ON test_config_ai_binding_table (toolchain_id)
            """
        )

    def close(self):
        if not db.is_closed():
            db.close()

    def is_connected(self) -> bool:
        return not db.is_closed()

