from __future__ import annotations

from pathlib import Path

from peewee import OperationalError

from model.base_mode import DB_PATH, db


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
            "audio_data_table",
            "users_table",
            "system_info_table",
            "training_model_table",
            "plc_table",
        }
    )

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
