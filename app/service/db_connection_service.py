from __future__ import annotations

from consts.time_consts import now_str
from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.audio_data_table import AudioDataTable
from model.base_mode import db
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from model.system_info_table import SystemInfoTable
from model.test_config_ai_binding_table import TestConfigAiBindingTable
from model.test_config_table import TestConfigTable
from model.training_model_table import TrainingModelTable
from model.users_table import UsersTable


class DbConnectionService:
    """
    数据库连接服务（长连接）。

    目标：
    - 程序启动时 connect 一次（或首次使用时自动 connect）
    - 程序退出时 close 一次
    - 业务代码/CRUD 不需要每次手动 connect/close
    """

    BOOTSTRAP_MODELS = (
        TestConfigTable,
        ChannelConfigTable,
        AiToolchainTable,
        TestConfigAiBindingTable,
        AiExecutionTable,
        DeviceTable,
        AudioDataTable,
        UsersTable,
        SystemInfoTable,
        TrainingModelTable,
    )

    def connect(self):
        db.connect(reuse_if_open=True)
        db.create_tables(self.BOOTSTRAP_MODELS, safe=True)
        self._migrate()
        self._seed_device_row()
        self._seed_default_admin_if_empty()

    def _migrate(self):
        self._ensure_columns(
            "test_config_table",
            [
                ("input_device_name", "TEXT"),
                ("input_device_hostapi", "TEXT"),
                ("input_device_samplerate", "REAL"),
                ("output_device_name", "TEXT"),
                ("output_device_hostapi", "TEXT"),
                ("output_device_samplerate", "REAL"),
                ("selected_channels", "TEXT DEFAULT '[]'"),
                ("plc_device_key", "TEXT"),
                ("plc_device_label", "TEXT"),
                ("analysis_window_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        self._ensure_columns(
            "channel_config_table",
            [
                ("loudness_unit", "TEXT DEFAULT 'sone'"),
                ("loudness_lower_limit", "REAL DEFAULT 0"),
                ("loudness_upper_limit", "REAL DEFAULT 60"),
                ("golden_sample_path", "TEXT DEFAULT NULL"),
                ("golden_sample_label", "TEXT DEFAULT NULL"),
                ("golden_sample_source_channel", "INTEGER DEFAULT NULL"),
                ("golden_sample_start_sample", "INTEGER DEFAULT NULL"),
                ("golden_sample_num_samples", "INTEGER DEFAULT NULL"),
                ("golden_sample_start_ms", "INTEGER DEFAULT NULL"),
                ("golden_sample_duration_ms", "INTEGER DEFAULT NULL"),
                ("golden_sample_sample_rate", "REAL DEFAULT NULL"),
                ("fft_golden_mode", "TEXT DEFAULT 'off'"),
                ("fft_show_golden_overlay", "INTEGER NOT NULL DEFAULT 0"),
                ("oct_golden_mode", "TEXT DEFAULT 'off'"),
                ("oct_show_golden_overlay", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        self._ensure_columns(
            "test_config_ai_binding_table",
            [
                ("score_threshold", "REAL DEFAULT NULL"),
            ],
        )
        self._ensure_columns(
            "ai_toolchain_table",
            [
                ("protocol_version", "INTEGER NOT NULL DEFAULT 1"),
                ("execution_mode", "TEXT NOT NULL DEFAULT 'oneshot_process'"),
                ("description", "TEXT NOT NULL DEFAULT ''"),
            ],
        )
        self._ensure_columns(
            "ai_execution_table",
            [
                ("toolchain_name", "TEXT"),
                ("toolchain_version", "TEXT"),
                ("protocol_version", "INTEGER NOT NULL DEFAULT 1"),
                ("window_overridden", "INTEGER NOT NULL DEFAULT 0"),
                ("default_window_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("default_window_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("final_window_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("final_window_duration_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("is_current_effective", "INTEGER NOT NULL DEFAULT 0"),
                ("request_json_path", "TEXT"),
                ("response_json_path", "TEXT"),
                ("artifacts_dir", "TEXT"),
            ],
        )
        self._backfill_channel_loudness_defaults()
        self._ensure_ai_binding_indexes()

    def _ensure_columns(self, table_name: str, columns: list[tuple[str, str]]):
        existing_columns = self._get_table_columns(table_name)
        for column_name, type_spec in columns:
            if column_name in existing_columns:
                continue
            db.execute_sql(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {type_spec}"
            )

    def _get_table_columns(self, table_name: str) -> set[str]:
        cursor = db.execute_sql(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

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

    def _seed_device_row(self):
        db.execute_sql("INSERT OR IGNORE INTO device_table (id) VALUES (1)")

    def _seed_default_admin_if_empty(self):
        if UsersTable.select().limit(1).exists():
            return
        from ui.modules.login import encrypt_password

        timestamp = now_str()
        UsersTable.create(
            user_name="admin",
            password=encrypt_password("admin", "admin"),
            access_level="Admin",
            user_created_time=timestamp,
            user_updated_time=timestamp,
        )

    def close(self):
        if not db.is_closed():
            db.close()

    def is_connected(self) -> bool:
        return not db.is_closed()
