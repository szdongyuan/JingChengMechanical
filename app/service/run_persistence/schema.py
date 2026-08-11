"""Minimal read-only authority validation for run persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from peewee import SqliteDatabase


RUN_PERSISTENCE_APPLICATION_ID = 1245924688
RUN_PERSISTENCE_USER_VERSION = 2
RUN_PERSISTENCE_TABLES = frozenset({"run", "audio", "analysis_attempt"})
RUN_PERSISTENCE_RUN_COLUMNS = (
    ("run_id", "TEXT", 1, None, 1, 0),
    ("ingress_kind", "TEXT", 1, None, 0, 0),
    ("trigger_source", "TEXT", 1, None, 0, 0),
    ("config_id", "INTEGER", 1, None, 0, 0),
    ("hardware_id", "INTEGER", 0, None, 0, 0),
    ("imported_source_identity", "TEXT", 0, None, 0, 0),
    ("imported_source_path", "TEXT", 0, None, 0, 0),
    ("imported_expected_frame_count", "INTEGER", 0, None, 0, 0),
    ("causal_revision", "INTEGER", 1, None, 0, 0),
    ("config_snapshot_json", "TEXT", 1, None, 0, 0),
    ("hardware_snapshot_json", "TEXT", 1, None, 0, 0),
    ("calibration_snapshot_json", "TEXT", 1, None, 0, 0),
    ("current_trace_json", "TEXT", 0, None, 0, 0),
    ("state", "TEXT", 1, None, 0, 0),
    ("judgment", "TEXT", 0, None, 0, 0),
    ("judgment_source", "TEXT", 1, None, 0, 0),
    ("fault_type", "TEXT", 0, None, 0, 0),
    ("manual_judgment_revision", "INTEGER", 0, None, 0, 0),
    ("judged_at", "TEXT", 0, None, 0, 0),
    ("terminal_code", "TEXT", 0, None, 0, 0),
    ("terminal_summary", "TEXT", 0, None, 0, 0),
    ("accepted_at", "TEXT", 1, None, 0, 0),
    ("terminal_at", "TEXT", 0, None, 0, 0),
    ("updated_at", "TEXT", 1, None, 0, 0),
)
RUN_PERSISTENCE_AUDIO_COLUMNS = (
    ("audio_id", "TEXT", 1, None, 1, 0),
    ("save_job_id", "TEXT", 1, None, 0, 0),
    ("run_id", "TEXT", 1, None, 0, 0),
    ("source_kind", "TEXT", 1, None, 0, 0),
    ("source_identity", "TEXT", 1, None, 0, 0),
    ("content_revision", "INTEGER", 1, None, 0, 0),
    ("final_path", "TEXT", 1, None, 0, 0),
    ("container_format", "TEXT", 1, None, 0, 0),
    ("sample_rate_hz", "INTEGER", 1, None, 0, 0),
    ("channel_count", "INTEGER", 1, None, 0, 0),
    ("frame_count", "INTEGER", 1, None, 0, 0),
    ("duration_ms", "INTEGER", 1, None, 0, 0),
    ("byte_size", "INTEGER", 1, None, 0, 0),
    ("closed_verified_at", "TEXT", 1, None, 0, 0),
    ("created_at", "TEXT", 1, None, 0, 0),
)
RUN_PERSISTENCE_ATTEMPT_COLUMNS = (
    ("attempt_id", "TEXT", 1, None, 1, 0),
    ("run_id", "TEXT", 1, None, 0, 0),
    ("audio_id", "TEXT", 1, None, 0, 0),
    ("attempt_kind", "TEXT", 1, None, 0, 0),
    ("state", "TEXT", 1, None, 0, 0),
    ("judgment", "TEXT", 0, None, 0, 0),
    ("result_payload_json", "TEXT", 0, None, 0, 0),
    ("error_code", "TEXT", 0, None, 0, 0),
    ("error_summary", "TEXT", 0, None, 0, 0),
    ("queued_at", "TEXT", 1, None, 0, 0),
    ("started_at", "TEXT", 0, None, 0, 0),
    ("terminal_at", "TEXT", 0, None, 0, 0),
    ("updated_at", "TEXT", 1, None, 0, 0),
)

_RUN_CHECK_TEXTS = (
    "ingress_kind IN ('CAPTURED', 'IMPORTED')",
    "trigger_source IN ('PLC', 'MANUAL', 'SCANNER', 'IMPORT')",
    "state IN ('ACTIVE', 'SUCCEEDED', 'NO_RESULT', 'FAILED', 'CANCELED', 'INTERRUPTED')",
    "judgment IS NULL OR judgment IN ('PASS', 'FAIL')",
    "judgment_source IN ('NONE', 'AUTOMATIC', 'MANUAL')",
    "ingress_kind <> 'CAPTURED' OR hardware_id IS NOT NULL",
    "(ingress_kind = 'IMPORTED' AND trigger_source = 'IMPORT') OR "
    "(ingress_kind <> 'IMPORTED' AND trigger_source <> 'IMPORT')",
    "json_valid(config_snapshot_json) AND json_type(config_snapshot_json) = 'object'",
    "json_valid(hardware_snapshot_json) AND json_type(hardware_snapshot_json) = 'object'",
    "json_valid(calibration_snapshot_json) AND json_type(calibration_snapshot_json) = 'object'",
    "(state = 'ACTIVE' AND terminal_at IS NULL AND terminal_code IS NULL "
    "AND terminal_summary IS NULL AND judgment_source = 'NONE') OR "
    "(state <> 'ACTIVE' AND terminal_at IS NOT NULL AND terminal_code IS NOT NULL)",
    "(judgment_source = 'NONE' AND judgment IS NULL AND judged_at IS NULL) OR "
    "(judgment_source <> 'NONE' AND judgment IS NOT NULL AND judged_at IS NOT NULL)",
    "(judgment_source = 'MANUAL' AND manual_judgment_revision IS NOT NULL "
    "AND typeof(manual_judgment_revision) = 'integer' "
    "AND manual_judgment_revision > 0) OR "
    "(judgment_source <> 'MANUAL' AND manual_judgment_revision IS NULL "
    "AND fault_type IS NULL)",
    "fault_type IS NULL OR (judgment_source = 'MANUAL' AND judgment = 'FAIL')",
    "current_trace_json IS NULL OR (json_valid(current_trace_json) "
    "AND json_type(current_trace_json) = 'object')",
    "(ingress_kind = 'CAPTURED' AND imported_source_identity IS NULL "
    "AND imported_source_path IS NULL "
    "AND imported_expected_frame_count IS NULL) OR "
    "(ingress_kind = 'IMPORTED' "
    "AND imported_source_identity IS NOT NULL "
    "AND imported_source_path IS NOT NULL "
    "AND length(trim(imported_source_identity)) > 0 "
    "AND length(trim(imported_source_path)) > 0 "
    "AND typeof(imported_expected_frame_count) = 'integer' "
    "AND imported_expected_frame_count > 0)",
    "typeof(causal_revision) = 'integer' AND causal_revision >= 0",
)

_AUDIO_CHECK_TEXTS = (
    "source_kind IN ('CAPTURED_AUDIO', 'IMPORTED_SOURCE')",
    "length(trim(source_identity)) > 0",
    "typeof(content_revision) = 'integer' AND content_revision > 0",
    "container_format = 'WAV_PCM'",
    "sample_rate_hz > 0",
    "channel_count > 0",
    "frame_count > 0",
    "duration_ms > 0",
    "byte_size > 0",
)

_ATTEMPT_CHECK_TEXTS = (
    "attempt_kind IN ('INITIAL', 'REANALYSIS')",
    "state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'NO_RESULT', 'FAILED', "
    "'CANCELED', 'INTERRUPTED')",
    "judgment IS NULL OR judgment IN ('PASS', 'FAIL')",
    "result_payload_json IS NULL OR json_valid(result_payload_json)",
    "(state = 'QUEUED' AND started_at IS NULL AND terminal_at IS NULL "
    "AND judgment IS NULL AND result_payload_json IS NULL "
    "AND error_code IS NULL) OR "
    "(state = 'RUNNING' AND started_at IS NOT NULL "
    "AND terminal_at IS NULL AND judgment IS NULL "
    "AND result_payload_json IS NULL AND error_code IS NULL) OR "
    "(state = 'SUCCEEDED' AND started_at IS NOT NULL "
    "AND terminal_at IS NOT NULL AND judgment IS NOT NULL "
    "AND result_payload_json IS NOT NULL AND error_code IS NULL) OR "
    "(state = 'NO_RESULT' AND started_at IS NOT NULL "
    "AND terminal_at IS NOT NULL AND judgment IS NULL "
    "AND error_code IS NOT NULL) OR "
    "(state IN ('FAILED', 'CANCELED', 'INTERRUPTED') "
    "AND terminal_at IS NOT NULL AND judgment IS NULL "
    "AND error_code IS NOT NULL)",
)


class SchemaValidationCode(str, Enum):
    DATABASE_APPLICATION_ID_MISMATCH = "DATABASE_APPLICATION_ID_MISMATCH"
    DATABASE_USER_VERSION_MISMATCH = "DATABASE_USER_VERSION_MISMATCH"
    DATABASE_FOREIGN_KEYS_DISABLED = "DATABASE_FOREIGN_KEYS_DISABLED"
    DATABASE_TABLE_SET_MISMATCH = "DATABASE_TABLE_SET_MISMATCH"
    DATABASE_RUN_COLUMNS_MISMATCH = "DATABASE_RUN_COLUMNS_MISMATCH"
    DATABASE_RUN_CHECKS_MISMATCH = "DATABASE_RUN_CHECKS_MISMATCH"


@dataclass(frozen=True)
class SchemaValidationFailure:
    code: SchemaValidationCode
    expected: object
    actual: object

    @property
    def reason(self) -> str:
        return (
            f"{self.code.value}: expected {self.expected!r}, "
            f"actual {self.actual!r}"
        )


@dataclass(frozen=True)
class SchemaValidationResult:
    valid: bool
    failure: SchemaValidationFailure | None = None


def _failure(
    code: SchemaValidationCode,
    expected: object,
    actual: object,
) -> SchemaValidationResult:
    return SchemaValidationResult(
        valid=False,
        failure=SchemaValidationFailure(
            code=code,
            expected=expected,
            actual=actual,
        ),
    )


def _normalize_sql_whitespace(value: str) -> str:
    result: list[str] = []
    pending_space = False
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            result.append(character)
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    result.append(value[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in ("'", '"'):
            if pending_space and result:
                result.append(" ")
            pending_space = False
            quote = character
            result.append(character)
        elif character in " \t\r\n\f\v":
            pending_space = True
        else:
            if pending_space and result:
                result.append(" ")
            pending_space = False
            result.append(character)
        index += 1
    return "".join(result).strip()


def _extract_check_expressions(create_sql: str) -> tuple[str, ...]:
    expressions: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(create_sql):
        character = create_sql[index]
        if quote is not None:
            if character == quote:
                if index + 1 < len(create_sql) and create_sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
            index += 1
            continue
        if create_sql[index:index + 5].upper() != "CHECK":
            index += 1
            continue
        cursor = index + 5
        while cursor < len(create_sql) and create_sql[cursor].isspace():
            cursor += 1
        if cursor >= len(create_sql) or create_sql[cursor] != "(":
            index += 5
            continue
        start = cursor + 1
        depth = 1
        cursor += 1
        inner_quote: str | None = None
        while cursor < len(create_sql) and depth:
            current = create_sql[cursor]
            if inner_quote is not None:
                if current == inner_quote:
                    if (
                        cursor + 1 < len(create_sql)
                        and create_sql[cursor + 1] == inner_quote
                    ):
                        cursor += 1
                    else:
                        inner_quote = None
            elif current in ("'", '"'):
                inner_quote = current
            elif current == "(":
                depth += 1
            elif current == ")":
                depth -= 1
            cursor += 1
        if depth != 0:
            return ()
        expressions.append(
            _normalize_sql_whitespace(create_sql[start:cursor - 1])
        )
        index = cursor
    return tuple(expressions)


RUN_PERSISTENCE_RUN_CHECKS = frozenset(
    _normalize_sql_whitespace(value) for value in _RUN_CHECK_TEXTS
)
RUN_PERSISTENCE_AUDIO_CHECKS = frozenset(
    _normalize_sql_whitespace(value) for value in _AUDIO_CHECK_TEXTS
)
RUN_PERSISTENCE_ATTEMPT_CHECKS = frozenset(
    _normalize_sql_whitespace(value) for value in _ATTEMPT_CHECK_TEXTS
)
RUN_PERSISTENCE_COLUMNS = {
    "run": RUN_PERSISTENCE_RUN_COLUMNS,
    "audio": RUN_PERSISTENCE_AUDIO_COLUMNS,
    "analysis_attempt": RUN_PERSISTENCE_ATTEMPT_COLUMNS,
}
RUN_PERSISTENCE_CHECKS = {
    "run": RUN_PERSISTENCE_RUN_CHECKS,
    "audio": RUN_PERSISTENCE_AUDIO_CHECKS,
    "analysis_attempt": RUN_PERSISTENCE_ATTEMPT_CHECKS,
}
RUN_PERSISTENCE_INDEXES = {
    "run": frozenset(
        {
            ("sqlite_autoindex_run_1", 1, (("run_id", "BINARY"),), None),
        }
    ),
    "audio": frozenset(
        {
            ("sqlite_autoindex_audio_1", 1, (("audio_id", "BINARY"),), None),
            ("audiotable_save_job_id", 1, (("save_job_id", "BINARY"),), None),
            ("audiotable_run_id", 1, (("run_id", "BINARY"),), None),
            ("audiotable_final_path", 1, (("final_path", "NOCASE"),), None),
            (
                "audiotable_audio_id_run_id",
                1,
                (("audio_id", "BINARY"), ("run_id", "BINARY")),
                None,
            ),
        }
    ),
    "analysis_attempt": frozenset(
        {
            (
                "sqlite_autoindex_analysis_attempt_1",
                1,
                (("attempt_id", "BINARY"),),
                None,
            ),
            (
                "uq_attempt_initial_per_run",
                1,
                (("run_id", "BINARY"),),
                "attempt_kind = 'INITIAL'",
            ),
        }
    ),
}
RUN_PERSISTENCE_FOREIGN_KEYS = {
    "run": (),
    "audio": (
        (
            ("run_id",),
            "run",
            ("run_id",),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
    "analysis_attempt": (
        (
            ("audio_id", "run_id"),
            "audio",
            ("audio_id", "run_id"),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
        (
            ("run_id",),
            "run",
            ("run_id",),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
}


def _table_columns(database: SqliteDatabase, table: str) -> tuple:
    return tuple(
        (row[1], row[2], row[3], row[4], row[5], row[6])
        for row in database.execute_sql(
            f'PRAGMA table_xinfo("{table}")'
        ).fetchall()
    )


def _table_checks(database: SqliteDatabase, table: str) -> tuple[str, ...]:
    row = database.execute_sql(
        """
        SELECT sql
        FROM sqlite_schema
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone()
    if row is None or type(row[0]) is not str:
        return ()
    return _extract_check_expressions(row[0])


def _table_indexes(database: SqliteDatabase, table: str) -> frozenset:
    indexes = set()
    for row in database.execute_sql(
        f'PRAGMA index_list("{table}")'
    ).fetchall():
        name = row[1]
        keys = tuple(
            (item[2], item[4])
            for item in database.execute_sql(
                f'PRAGMA index_xinfo("{name}")'
            ).fetchall()
            if item[5] == 1
        )
        predicate = None
        if row[4]:
            sql_row = database.execute_sql(
                """
                SELECT sql
                FROM sqlite_schema
                WHERE type = 'index' AND name = ?
                """,
                (name,),
            ).fetchone()
            if (
                sql_row is None
                or type(sql_row[0]) is not str
                or " WHERE " not in sql_row[0].upper()
            ):
                predicate = ""
            else:
                upper = sql_row[0].upper()
                where_at = upper.index(" WHERE ")
                predicate = _normalize_sql_whitespace(
                    sql_row[0][where_at + len(" WHERE "):]
                )
        indexes.add((name, row[2], keys, predicate))
    return frozenset(indexes)


def _table_foreign_keys(database: SqliteDatabase, table: str) -> tuple:
    groups: dict[int, list[tuple]] = {}
    for row in database.execute_sql(
        f'PRAGMA foreign_key_list("{table}")'
    ).fetchall():
        groups.setdefault(row[0], []).append(row)
    foreign_keys = []
    for rows in groups.values():
        ordered_rows = sorted(rows, key=lambda value: value[1])
        foreign_keys.append(
            (
                tuple(item[3] for item in ordered_rows),
                rows[0][2],
                tuple(item[4] for item in ordered_rows),
                rows[0][5],
                rows[0][6],
                rows[0][7],
            )
        )
    return tuple(sorted(foreign_keys))


def validate_run_persistence_schema(
    database: SqliteDatabase,
) -> SchemaValidationResult:
    """Read fixed markers, connection FK state, and the exact user-table set."""

    application_id = database.execute_sql("PRAGMA application_id").fetchone()[0]
    if application_id != RUN_PERSISTENCE_APPLICATION_ID:
        return _failure(
            SchemaValidationCode.DATABASE_APPLICATION_ID_MISMATCH,
            RUN_PERSISTENCE_APPLICATION_ID,
            application_id,
        )

    user_version = database.execute_sql("PRAGMA user_version").fetchone()[0]
    if user_version != RUN_PERSISTENCE_USER_VERSION:
        return _failure(
            SchemaValidationCode.DATABASE_USER_VERSION_MISMATCH,
            RUN_PERSISTENCE_USER_VERSION,
            user_version,
        )

    foreign_keys = database.execute_sql("PRAGMA foreign_keys").fetchone()[0]
    if foreign_keys != 1:
        return _failure(
            SchemaValidationCode.DATABASE_FOREIGN_KEYS_DISABLED,
            1,
            foreign_keys,
        )

    table_rows = database.execute_sql(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
        """
    ).fetchall()
    actual_tables = frozenset(row[0] for row in table_rows)
    if actual_tables != RUN_PERSISTENCE_TABLES:
        return _failure(
            SchemaValidationCode.DATABASE_TABLE_SET_MISMATCH,
            RUN_PERSISTENCE_TABLES,
            actual_tables,
        )

    for table, expected_columns in RUN_PERSISTENCE_COLUMNS.items():
        actual_columns = _table_columns(database, table)
        if actual_columns != expected_columns:
            return _failure(
                SchemaValidationCode.DATABASE_RUN_COLUMNS_MISMATCH,
                (table, expected_columns),
                (table, actual_columns),
            )

    for table, expected_checks in RUN_PERSISTENCE_CHECKS.items():
        actual_check_expressions = _table_checks(database, table)
        actual_checks = frozenset(actual_check_expressions)
        if (
            len(actual_check_expressions) != len(expected_checks)
            or actual_checks != expected_checks
        ):
            return _failure(
                SchemaValidationCode.DATABASE_RUN_CHECKS_MISMATCH,
                (table, expected_checks),
                (table, actual_checks),
            )

    for table, expected_indexes in RUN_PERSISTENCE_INDEXES.items():
        actual_indexes = _table_indexes(database, table)
        if actual_indexes != expected_indexes:
            return _failure(
                SchemaValidationCode.DATABASE_RUN_CHECKS_MISMATCH,
                (table, expected_indexes),
                (table, actual_indexes),
            )

    for table, expected_foreign_keys in RUN_PERSISTENCE_FOREIGN_KEYS.items():
        actual_foreign_keys = _table_foreign_keys(database, table)
        if actual_foreign_keys != expected_foreign_keys:
            return _failure(
                SchemaValidationCode.DATABASE_RUN_CHECKS_MISMATCH,
                (table, expected_foreign_keys),
                (table, actual_foreign_keys),
            )

    return SchemaValidationResult(valid=True)
