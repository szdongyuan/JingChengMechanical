from __future__ import annotations

from consts.time_consts import now_str
from model.ai_execution_table import AiExecutionTable
from model.ai_toolchain_table import AiToolchainTable
from model.audio_data_table import AudioDataTable
from model.base_mode import db
from model.channel_config_table import ChannelConfigTable
from model.device_table import DeviceTable
from model.plc_table import PlcTable
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
        PlcTable,
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
                ("allow_incomplete_recording_analysis", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        self._ensure_test_config_table_plc_fk()
        self._ensure_columns(
            "channel_config_table",
            [
                ("fft_nfft", "INTEGER NOT NULL DEFAULT 4096"),
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
        self._remove_obsolete_ai_binding_score_threshold()
        self._ensure_columns(
            "ai_toolchain_table",
            [
                ("protocol_version", "INTEGER NOT NULL DEFAULT 1"),
                ("execution_mode", "TEXT NOT NULL DEFAULT 'resident_process'"),
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
        self._backfill_channel_fft_defaults()
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
        return {row[1] for row in self._get_table_column_rows(table_name)}

    def _get_table_column_rows(self, table_name: str) -> list[tuple]:
        cursor = db.execute_sql(f"PRAGMA table_info({table_name})")
        return cursor.fetchall()

    def _get_table_foreign_keys(self, table_name: str) -> list[tuple]:
        cursor = db.execute_sql(f"PRAGMA foreign_key_list({table_name})")
        return cursor.fetchall()

    def _ensure_test_config_table_plc_fk(self):
        if not self._test_config_table_requires_plc_fk_rebuild():
            self._ensure_test_config_table_indexes()
            return

        existing_columns = self._get_table_column_rows("test_config_table")
        retained_columns = [row for row in existing_columns if row[1] != "plc_id"]
        column_definitions = [
            self._build_test_config_column_definition(row) for row in retained_columns
        ]
        column_definitions.append(
            '"plc_id" INTEGER REFERENCES "plc_table" ("id") ON DELETE SET NULL'
        )

        insert_columns = [row[1] for row in retained_columns] + ["plc_id"]
        select_columns = [self._quote_identifier(row[1]) for row in retained_columns]
        select_columns.append('NULL AS "plc_id"')

        db.execute_sql("PRAGMA foreign_keys = OFF")
        try:
            with db.atomic():
                db.execute_sql(
                    """
                    CREATE TABLE test_config_table__plc_rebuild (
                        {}
                    )
                    """.format(",\n                        ".join(column_definitions))
                )
                db.execute_sql(
                    """
                    INSERT INTO test_config_table__plc_rebuild ({})
                    SELECT {}
                    FROM test_config_table
                    """.format(
                        ", ".join(
                            self._quote_identifier(column_name)
                            for column_name in insert_columns
                        ),
                        ", ".join(select_columns),
                    )
                )
                db.execute_sql("DROP TABLE test_config_table")
                db.execute_sql(
                    """
                    ALTER TABLE test_config_table__plc_rebuild
                    RENAME TO test_config_table
                    """
                )
                db.execute_sql(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS testconfigtable_config_name
                    ON test_config_table (config_name)
                    """
                )
                db.execute_sql(
                    """
                    CREATE INDEX IF NOT EXISTS testconfigtable_plc_id
                    ON test_config_table (plc_id)
                    """
                )
        finally:
            db.execute_sql("PRAGMA foreign_keys = ON")

    def _test_config_table_requires_plc_fk_rebuild(self) -> bool:
        columns = {row[1]: row for row in self._get_table_column_rows("test_config_table")}
        plc_column = columns.get("plc_id")
        if plc_column is None:
            return True
        if plc_column[3]:
            return True

        return not any(
            row[2] == "plc_table"
            and row[3] == "plc_id"
            and row[4] == "id"
            and row[6].upper() == "SET NULL"
            for row in self._get_table_foreign_keys("test_config_table")
        )

    def _build_test_config_column_definition(self, column_row: tuple) -> str:
        _, column_name, column_type, not_null, default_value, primary_key = column_row
        definition = [self._quote_identifier(column_name)]
        if column_type:
            definition.append(column_type)
        if primary_key:
            definition.append("PRIMARY KEY")
        if not_null and not primary_key:
            definition.append("NOT NULL")
        if default_value is not None:
            definition.append(f"DEFAULT {default_value}")
        return " ".join(definition)

    def _quote_identifier(self, identifier: str) -> str:
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    def _ensure_test_config_table_indexes(self):
        existing_indexes = {
            (columns, unique)
            for columns, unique in self._get_table_index_definitions("test_config_table")
        }
        if (("config_name",), True) not in existing_indexes:
            db.execute_sql(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS testconfigtable_config_name
                ON test_config_table (config_name)
                """
            )
        if (("plc_id",), False) not in existing_indexes:
            db.execute_sql(
                """
                CREATE INDEX IF NOT EXISTS testconfigtable_plc_id
                ON test_config_table (plc_id)
                """
            )

    def _get_table_index_definitions(self, table_name: str) -> list[tuple[tuple[str, ...], bool]]:
        cursor = db.execute_sql(f"PRAGMA index_list({table_name})")
        definitions = []
        for _, index_name, unique, *_ in cursor.fetchall():
            info = db.execute_sql(f"PRAGMA index_info({index_name})").fetchall()
            definitions.append((tuple(row[2] for row in info), bool(unique)))
        return definitions

    def _remove_obsolete_ai_binding_score_threshold(self):
        columns = self._get_table_columns("test_config_ai_binding_table")
        if "score_threshold" not in columns:
            return

        column_definitions = [
            "binding_id INTEGER PRIMARY KEY",
            "config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE",
            "enabled INTEGER NOT NULL DEFAULT 0",
            "toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL",
            "timeout_ms INTEGER",
        ]
        copy_columns = [
            "binding_id",
            "config_id",
            "enabled",
            "toolchain_id",
            "timeout_ms",
        ]
        if "analysis_window_offset_ms" in columns:
            column_definitions.append(
                "analysis_window_offset_ms INTEGER NOT NULL DEFAULT 0"
            )
            copy_columns.append("analysis_window_offset_ms")
        column_definitions.extend(
            [
                "params_json TEXT NOT NULL DEFAULT '{}'",
                "updated_time TEXT NOT NULL DEFAULT ''",
            ]
        )
        copy_columns.extend(["params_json", "updated_time"])
        column_list_sql = ", ".join(copy_columns)

        with db.atomic():
            db.execute_sql(
                """
                ALTER TABLE test_config_ai_binding_table
                RENAME TO test_config_ai_binding_table_old
                """
            )
            db.execute_sql(
                """
                CREATE TABLE test_config_ai_binding_table (
                    {}
                )
                """.format(",\n                    ".join(column_definitions))
            )
            db.execute_sql(
                f"""
                INSERT INTO test_config_ai_binding_table ({column_list_sql})
                SELECT {column_list_sql}
                FROM test_config_ai_binding_table_old
                """
            )
            db.execute_sql("DROP TABLE test_config_ai_binding_table_old")
            self._ensure_ai_binding_indexes()

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

    def _backfill_channel_fft_defaults(self):
        if "fft_nfft" not in self._get_table_columns("channel_config_table"):
            return
        db.execute_sql(
            """
            UPDATE channel_config_table
            SET fft_nfft = COALESCE(fft_nfft, 4096)
            WHERE fft_nfft IS NULL
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
