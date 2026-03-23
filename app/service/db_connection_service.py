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

    _ANALYSIS_OFFSET_BACKFILL_MARKER = (
        "test_config_analysis_window_offset_backfill_v1"
    )

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
            ("trigger_type", "TEXT DEFAULT 'manual'"),
            ("input_device_name", "TEXT"),
            ("input_device_hostapi", "TEXT"),
            ("input_device_samplerate", "REAL"),
            ("output_device_name", "TEXT"),
            ("output_device_hostapi", "TEXT"),
            ("output_device_samplerate", "REAL"),
            ("selected_channels", "TEXT DEFAULT '[]'"),
            ("plc_device_key", "TEXT DEFAULT NULL"),
            ("plc_device_label", "TEXT DEFAULT NULL"),
            ("analysis_window_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for column_name, type_spec in columns:
            try:
                db.execute_sql(
                    f"ALTER TABLE test_config_table ADD COLUMN {column_name} {type_spec}"
                )
            except OperationalError:
                pass

        # AI binding table migrations
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

        self._backfill_analysis_window_offset_once()

    def _backfill_analysis_window_offset_once(self):
        db.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS migration_marker_table (
                marker_key TEXT PRIMARY KEY,
                applied_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        marker_row = db.execute_sql(
            "SELECT 1 FROM migration_marker_table WHERE marker_key = ?",
            (self._ANALYSIS_OFFSET_BACKFILL_MARKER,),
        ).fetchone()
        if marker_row:
            return

        with db.atomic():
            db.execute_sql(
                """
                UPDATE test_config_table
                SET analysis_window_offset_ms = (
                    SELECT binding.analysis_window_offset_ms
                    FROM test_config_ai_binding_table AS binding
                    WHERE binding.config_id = test_config_table.config_id
                )
                WHERE test_config_table.analysis_window_offset_ms = 0
                  AND EXISTS (
                      SELECT 1
                      FROM test_config_ai_binding_table AS binding
                      WHERE binding.config_id = test_config_table.config_id
                        AND binding.analysis_window_offset_ms != 0
                  )
                """
            )
            db.execute_sql(
                "INSERT INTO migration_marker_table (marker_key) VALUES (?)",
                (self._ANALYSIS_OFFSET_BACKFILL_MARKER,),
            )

    def close(self):
        if not db.is_closed():
            db.close()

    def is_connected(self) -> bool:
        return not db.is_closed()

