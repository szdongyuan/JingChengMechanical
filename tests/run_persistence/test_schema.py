from __future__ import annotations

from uuid import uuid4

import pytest
from peewee import IntegrityError, SqliteDatabase

from app.service.run_persistence.schema import validate_run_persistence_schema
from model.analysis_attempt_table import AnalysisAttemptTable
from model.audio_table import AudioTable
from model.run_table import RunTable


RUN_COLUMNS = {
    "run_id",
    "ingress_kind",
    "trigger_source",
    "config_id",
    "hardware_id",
    "imported_source_identity",
    "imported_source_path",
    "imported_expected_frame_count",
    "causal_revision",
    "config_snapshot_json",
    "hardware_snapshot_json",
    "calibration_snapshot_json",
    "current_trace_json",
    "state",
    "judgment",
    "judgment_source",
    "fault_type",
    "manual_judgment_revision",
    "judged_at",
    "terminal_code",
    "terminal_summary",
    "accepted_at",
    "terminal_at",
    "updated_at",
}
AUDIO_COLUMNS = {
    "audio_id",
    "save_job_id",
    "run_id",
    "source_kind",
    "source_identity",
    "content_revision",
    "final_path",
    "container_format",
    "sample_rate_hz",
    "channel_count",
    "frame_count",
    "duration_ms",
    "byte_size",
    "closed_verified_at",
    "created_at",
}
ATTEMPT_COLUMNS = {
    "attempt_id",
    "run_id",
    "audio_id",
    "attempt_kind",
    "state",
    "judgment",
    "result_payload_json",
    "error_code",
    "error_summary",
    "queued_at",
    "started_at",
    "terminal_at",
    "updated_at",
}

V2_RUN_COLUMNS = [
    "run_id",
    "ingress_kind",
    "trigger_source",
    "config_id",
    "hardware_id",
    "imported_source_identity",
    "imported_source_path",
    "imported_expected_frame_count",
    "causal_revision",
    "config_snapshot_json",
    "hardware_snapshot_json",
    "calibration_snapshot_json",
    "current_trace_json",
    "state",
    "judgment",
    "judgment_source",
    "fault_type",
    "manual_judgment_revision",
    "judged_at",
    "terminal_code",
    "terminal_summary",
    "accepted_at",
    "terminal_at",
    "updated_at",
]


def _id() -> str:
    return str(uuid4())


def _run_values(now: str, **overrides):
    values = {
        "run_id": _id(),
        "ingress_kind": "CAPTURED",
        "trigger_source": "MANUAL",
        "config_id": 1,
        "hardware_id": 2,
        "imported_source_identity": None,
        "imported_source_path": None,
        "imported_expected_frame_count": None,
        "causal_revision": 0,
        "config_snapshot_json": '{"config_id":1}',
        "hardware_snapshot_json": '{"hardware_id":2}',
        "calibration_snapshot_json": '{"kind":"NONE"}',
        "current_trace_json": None,
        "state": "ACTIVE",
        "judgment": None,
        "judgment_source": "NONE",
        "fault_type": None,
        "manual_judgment_revision": None,
        "judged_at": None,
        "terminal_code": None,
        "terminal_summary": None,
        "accepted_at": now,
        "terminal_at": None,
        "updated_at": now,
    }
    values.update(overrides)
    return values


def _create_run(now: str, **overrides) -> str:
    values = _run_values(now, **overrides)
    RunTable.create(**values)
    return values["run_id"]


def _audio_values(run_id: str, now: str, **overrides):
    values = {
        "audio_id": _id(),
        "save_job_id": _id(),
        "run_id": run_id,
        "source_kind": "CAPTURED_AUDIO",
        "source_identity": "capture:test",
        "content_revision": 1,
        "final_path": f"C:/managed/{_id()}.wav",
        "container_format": "WAV_PCM",
        "sample_rate_hz": 48_000,
        "channel_count": 1,
        "frame_count": 48_000,
        "duration_ms": 1_000,
        "byte_size": 96_044,
        "closed_verified_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return values


def _create_audio(run_id: str, now: str, **overrides) -> str:
    values = _audio_values(run_id, now, **overrides)
    AudioTable.create(**values)
    return values["audio_id"]


def _attempt_values(run_id: str, audio_id: str, now: str, **overrides):
    values = {
        "attempt_id": _id(),
        "run_id": run_id,
        "audio_id": audio_id,
        "attempt_kind": "INITIAL",
        "state": "QUEUED",
        "judgment": None,
        "result_payload_json": None,
        "error_code": None,
        "error_summary": None,
        "queued_at": now,
        "started_at": None,
        "terminal_at": None,
        "updated_at": now,
    }
    values.update(overrides)
    return values


def test_fresh_v2_schema_has_exact_tables_columns_and_checks(
    raw_schema_database,
) -> None:
    tables = raw_schema_database.execute_sql(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
        """
    ).fetchall()
    columns = raw_schema_database.execute_sql(
        'PRAGMA table_xinfo("run")'
    ).fetchall()
    run_sql = raw_schema_database.execute_sql(
        """
        SELECT sql
        FROM sqlite_schema
        WHERE type = 'table' AND name = 'run'
        """
    ).fetchone()[0]

    assert {row[0] for row in tables} == {
        "run",
        "audio",
        "analysis_attempt",
    }
    assert [row[1] for row in columns] == V2_RUN_COLUMNS
    assert all(row[4] is None and row[6] == 0 for row in columns)
    assert run_sql.upper().count("CHECK (") == 17


def test_v2_has_no_channel_order_table_or_column(
    raw_schema_database,
) -> None:
    tables = raw_schema_database.execute_sql(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table' AND name NOT GLOB 'sqlite_*'
        """
    ).fetchall()
    columns = raw_schema_database.execute_sql(
        'PRAGMA table_xinfo("run")'
    ).fetchall()

    assert {row[0] for row in tables} == {
        "run",
        "audio",
        "analysis_attempt",
    }
    assert "channel_order" not in {row[1] for row in columns}
    assert "channel_mapping" not in {row[1] for row in columns}


def test_fixture_contains_exactly_three_conceptual_tables(
    run_persistence_database,
) -> None:
    rows = run_persistence_database.execute_sql(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()

    assert {row[0] for row in rows} == {"run", "audio", "analysis_attempt"}


@pytest.mark.parametrize(
    ("table_name", "expected_columns"),
    [
        ("run", RUN_COLUMNS),
        ("audio", AUDIO_COLUMNS),
        ("analysis_attempt", ATTEMPT_COLUMNS),
    ],
)
def test_tables_have_exact_spec_columns(
    run_persistence_database, table_name: str, expected_columns: set[str]
) -> None:
    columns = run_persistence_database.execute_sql(
        f'PRAGMA table_info("{table_name}")'
    ).fetchall()

    assert {column[1] for column in columns} == expected_columns


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ingress_kind", "OTHER"),
        ("trigger_source", "OTHER"),
        ("state", "OTHER"),
        ("judgment", "OTHER"),
        ("judgment_source", "OTHER"),
    ],
)
def test_run_enum_checks(
    run_persistence_database, now: str, field: str, value: str
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(**_run_values(now, **{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "config_snapshot_json",
        "hardware_snapshot_json",
        "calibration_snapshot_json",
    ],
)
@pytest.mark.parametrize("value", ["not-json", "[]", "null", "1"])
def test_run_snapshots_require_valid_json_objects(
    run_persistence_database, now: str, field: str, value: str
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(**_run_values(now, **{field: value}))


def test_current_trace_sql_check_has_only_nullable_valid_object_gate(
    run_persistence_database,
    now: str,
) -> None:
    _create_run(now, current_trace_json=None)
    _create_run(now, current_trace_json='{"samples":[]}')
    oversized_object = '{"padding":"' + ("x" * (8 * 1024 * 1024 + 1)) + '"}'
    _create_run(now, current_trace_json=oversized_object)

    for invalid in ("not-json", "[]", "null", "1"):
        with pytest.raises(IntegrityError):
            RunTable.create(
                **_run_values(now, current_trace_json=invalid)
            )


def test_manual_fault_and_revision_sql_checks_are_exact(
    run_persistence_database,
    now: str,
) -> None:
    terminal = {
        "state": "SUCCEEDED",
        "judgment": "FAIL",
        "judgment_source": "MANUAL",
        "judged_at": now,
        "terminal_code": "ANALYSIS_SUCCEEDED",
        "terminal_at": now,
    }
    valid_run_id = _create_run(
        now,
        **terminal,
        fault_type="异响",
        manual_judgment_revision=1,
    )

    invalid_cases = (
        {"manual_judgment_revision": None},
        {"manual_judgment_revision": 0},
        {
            "judgment_source": "AUTOMATIC",
            "manual_judgment_revision": 1,
        },
        {
            "judgment_source": "AUTOMATIC",
            "fault_type": "异响",
            "manual_judgment_revision": None,
        },
        {
            "judgment": "PASS",
            "fault_type": "异响",
            "manual_judgment_revision": 1,
        },
    )
    for overrides in invalid_cases:
        with pytest.raises(IntegrityError):
            RunTable.create(
                **_run_values(now, **(terminal | overrides))
            )
    with pytest.raises(IntegrityError):
        run_persistence_database.execute_sql(
            """
            UPDATE run
            SET manual_judgment_revision = 1.5
            WHERE run_id = ?
            """,
            (valid_run_id,),
        )


def test_captured_run_requires_hardware(
    run_persistence_database, now: str
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(**_run_values(now, hardware_id=None))


@pytest.mark.parametrize(
    "overrides",
    [
        {"ingress_kind": "IMPORTED", "trigger_source": "MANUAL"},
        {"ingress_kind": "CAPTURED", "trigger_source": "IMPORT"},
    ],
)
def test_import_trigger_is_used_exactly_for_imported_runs(
    run_persistence_database, now: str, overrides: dict[str, str]
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(**_run_values(now, **overrides))

    _create_run(
        now,
        ingress_kind="IMPORTED",
        trigger_source="IMPORT",
        hardware_id=None,
        imported_source_identity="sha256:imported-test",
        imported_source_path="C:/managed/imported-test.wav",
        imported_expected_frame_count=48_000,
        hardware_snapshot_json='{"kind":"NONE"}',
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"terminal_at": "2026-07-27T12:35:00.000000Z"},
        {"terminal_code": "SHOULD_BE_NULL"},
        {"terminal_summary": "should be null"},
        {
            "judgment": "PASS",
            "judgment_source": "AUTOMATIC",
            "judged_at": "2026-07-27T12:35:00.000000Z",
        },
    ],
)
def test_active_run_terminal_fields_and_judgment_are_null(
    run_persistence_database, now: str, overrides: dict[str, str]
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(**_run_values(now, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"terminal_at": None, "terminal_code": "DONE"},
        {"terminal_at": "2026-07-27T12:35:00.000000Z", "terminal_code": None},
    ],
)
def test_terminal_run_requires_terminal_time_and_code(
    run_persistence_database, now: str, overrides: dict[str, str | None]
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(
            **_run_values(
                now,
                **{
                    "state": "FAILED",
                    "terminal_at": "2026-07-27T12:35:00.000000Z",
                    "terminal_code": "FAILED",
                    **overrides,
                },
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"judgment_source": "NONE", "judgment": "PASS", "judged_at": None},
        {
            "judgment_source": "NONE",
            "judgment": None,
            "judged_at": "2026-07-27T12:35:00.000000Z",
        },
        {"judgment_source": "AUTOMATIC", "judgment": None, "judged_at": None},
        {"judgment_source": "MANUAL", "judgment": "FAIL", "judged_at": None},
    ],
)
def test_run_judgment_source_value_and_time_are_paired(
    run_persistence_database, now: str, overrides: dict[str, str | None]
) -> None:
    with pytest.raises(IntegrityError):
        RunTable.create(
            **_run_values(
                now,
                state="SUCCEEDED",
                terminal_at="2026-07-27T12:35:00.000000Z",
                terminal_code="ANALYSIS_SUCCEEDED",
                **overrides,
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        "sample_rate_hz",
        "channel_count",
        "frame_count",
        "duration_ms",
        "byte_size",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_audio_numeric_fields_must_be_positive(
    run_persistence_database, now: str, field: str, value: int
) -> None:
    run_id = _create_run(now)
    with pytest.raises(IntegrityError):
        AudioTable.create(**_audio_values(run_id, now, **{field: value}))


def test_audio_container_format_is_wav_pcm(
    run_persistence_database, now: str
) -> None:
    run_id = _create_run(now)
    with pytest.raises(IntegrityError):
        AudioTable.create(
            **_audio_values(run_id, now, container_format="COMPRESSED")
        )


def test_audio_is_unique_per_run_and_path_is_unique_case_insensitively(
    run_persistence_database, now: str
) -> None:
    run_a = _create_run(now)
    run_b = _create_run(now)
    _create_audio(run_a, now, final_path="C:/Managed/Result.wav")

    with pytest.raises(IntegrityError):
        AudioTable.create(**_audio_values(run_a, now))
    with pytest.raises(IntegrityError):
        AudioTable.create(
            **_audio_values(run_b, now, final_path="c:/managed/result.WAV")
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempt_kind": "OTHER"},
        {"state": "OTHER"},
        {"judgment": "OTHER"},
    ],
)
def test_attempt_enum_checks(
    run_persistence_database, now: str, overrides: dict[str, str]
) -> None:
    run_id = _create_run(now)
    audio_id = _create_audio(run_id, now)
    with pytest.raises(IntegrityError):
        AnalysisAttemptTable.create(
            **_attempt_values(run_id, audio_id, now, **overrides)
        )


@pytest.mark.parametrize(
    ("state", "valid", "invalid_overrides"),
    [
        (
            "QUEUED",
            {},
            {"started_at": "2026-07-27T12:35:00.000000Z"},
        ),
        (
            "RUNNING",
            {"started_at": "2026-07-27T12:35:00.000000Z"},
            {"started_at": None},
        ),
        (
            "SUCCEEDED",
            {
                "started_at": "2026-07-27T12:35:00.000000Z",
                "terminal_at": "2026-07-27T12:36:00.000000Z",
                "judgment": "PASS",
                "result_payload_json": '{"score":0.9}',
            },
            {"result_payload_json": None},
        ),
        (
            "NO_RESULT",
            {
                "started_at": "2026-07-27T12:35:00.000000Z",
                "terminal_at": "2026-07-27T12:36:00.000000Z",
                "error_code": "NO_DECISION",
            },
            {"error_code": None},
        ),
        (
            "FAILED",
            {
                "terminal_at": "2026-07-27T12:36:00.000000Z",
                "error_code": "ANALYSIS_FAILED",
            },
            {"judgment": "FAIL"},
        ),
        (
            "CANCELED",
            {
                "terminal_at": "2026-07-27T12:36:00.000000Z",
                "error_code": "USER_CANCELED",
            },
            {"terminal_at": None},
        ),
        (
            "INTERRUPTED",
            {
                "terminal_at": "2026-07-27T12:36:00.000000Z",
                "error_code": "PROCESS_RESTARTED",
            },
            {"error_code": None},
        ),
    ],
)
def test_attempt_state_timestamp_result_and_error_checks(
    run_persistence_database,
    now: str,
    state: str,
    valid: dict[str, str],
    invalid_overrides: dict[str, str | None],
) -> None:
    run_id = _create_run(now)
    audio_id = _create_audio(run_id, now)
    base = _attempt_values(
        run_id,
        audio_id,
        now,
        attempt_kind="REANALYSIS",
        state=state,
        **valid,
    )
    AnalysisAttemptTable.create(**base)

    invalid = {**base, "attempt_id": _id(), **invalid_overrides}
    with pytest.raises(IntegrityError):
        AnalysisAttemptTable.create(**invalid)


def test_non_null_attempt_result_payload_requires_valid_json(
    run_persistence_database, now: str
) -> None:
    run_id = _create_run(now)
    audio_id = _create_audio(run_id, now)
    with pytest.raises(IntegrityError):
        AnalysisAttemptTable.create(
            **_attempt_values(
                run_id,
                audio_id,
                now,
                attempt_kind="REANALYSIS",
                state="SUCCEEDED",
                started_at=now,
                terminal_at=now,
                judgment="PASS",
                result_payload_json="not-json",
            )
        )


def test_only_one_initial_attempt_per_run_but_reanalysis_has_no_ordinal(
    run_persistence_database, now: str
) -> None:
    run_id = _create_run(now)
    audio_id = _create_audio(run_id, now)
    AnalysisAttemptTable.create(**_attempt_values(run_id, audio_id, now))

    with pytest.raises(IntegrityError):
        AnalysisAttemptTable.create(**_attempt_values(run_id, audio_id, now))

    AnalysisAttemptTable.create(
        **_attempt_values(
            run_id, audio_id, now, attempt_kind="REANALYSIS"
        )
    )
    AnalysisAttemptTable.create(
        **_attempt_values(
            run_id, audio_id, now, attempt_kind="REANALYSIS"
        )
    )
    assert "ordinal" not in ATTEMPT_COLUMNS
    assert "sequence" not in ATTEMPT_COLUMNS


def test_attempt_audio_and_run_must_match(
    run_persistence_database, now: str
) -> None:
    run_a = _create_run(now)
    run_b = _create_run(now)
    audio_for_run_b = _create_audio(run_b, now)

    with pytest.raises(IntegrityError):
        AnalysisAttemptTable.create(
            attempt_id=_id(),
            run_id=run_a,
            audio_id=audio_for_run_b,
            attempt_kind="INITIAL",
            state="QUEUED",
            queued_at=now,
            updated_at=now,
        )


def test_foreign_keys_restrict_deletes(
    run_persistence_database, now: str
) -> None:
    run_id = _create_run(now)
    audio_id = _create_audio(run_id, now)
    AnalysisAttemptTable.create(**_attempt_values(run_id, audio_id, now))

    with pytest.raises(IntegrityError):
        AudioTable.delete().where(AudioTable.audio_id == audio_id).execute()
    with pytest.raises(IntegrityError):
        RunTable.delete().where(RunTable.run_id == run_id).execute()


def test_read_only_schema_validation_accepts_exact_schema(
    validator_database,
) -> None:
    result = validate_run_persistence_schema(validator_database)

    assert result.valid is True
    assert result.failure is None


@pytest.mark.parametrize("actual", [0, 7])
def test_schema_validation_rejects_missing_or_wrong_application_id(
    validator_database, actual: int
) -> None:
    validator_database.execute_sql(f"PRAGMA application_id = {actual}")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_APPLICATION_ID_MISMATCH"
    assert result.failure.expected == 1245924688
    assert result.failure.actual == actual


@pytest.mark.parametrize("actual", [0, 1, 2, 4])
def test_schema_validation_rejects_missing_or_wrong_user_version(
    validator_database, actual: int
) -> None:
    validator_database.execute_sql(f"PRAGMA user_version = {actual}")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_USER_VERSION_MISMATCH"
    assert result.failure.expected == 3
    assert result.failure.actual == actual


def test_schema_validation_rejects_disabled_foreign_keys(
    validator_database,
) -> None:
    validator_database.execute_sql("PRAGMA foreign_keys = OFF")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_FOREIGN_KEYS_DISABLED"
    assert result.failure.expected == 1
    assert result.failure.actual == 0


def test_schema_validation_reports_exact_missing_table(
    validator_database,
) -> None:
    validator_database.execute_sql("DROP TABLE analysis_attempt")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_TABLE_SET_MISMATCH"
    assert result.failure.expected == frozenset(
        {"run", "audio", "analysis_attempt"}
    )
    assert result.failure.actual == frozenset({"run", "audio"})


def test_schema_validation_reports_exact_extra_user_table(
    validator_database,
) -> None:
    validator_database.execute_sql("CREATE TABLE unrelated (id INTEGER)")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_TABLE_SET_MISMATCH"
    assert result.failure.expected == frozenset(
        {"run", "audio", "analysis_attempt"}
    )
    assert result.failure.actual == frozenset(
        {"run", "audio", "analysis_attempt", "unrelated"}
    )


def test_schema_validation_treats_sqlite_underscore_as_literal_prefix(
    validator_database,
) -> None:
    validator_database.execute_sql("CREATE TABLE sqliteXextra (id INTEGER)")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_TABLE_SET_MISMATCH"
    assert result.failure.expected == frozenset(
        {"run", "audio", "analysis_attempt"}
    )
    assert result.failure.actual == frozenset(
        {"run", "audio", "analysis_attempt", "sqliteXextra"}
    )


def test_schema_validation_accepts_marker_preserving_column_weakening(
    validator_database,
) -> None:
    validator_database.execute_sql(
        "ALTER TABLE audio RENAME COLUMN byte_size TO size_bytes"
    )

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None


def _cloned_schema_database(
    source: SqliteDatabase,
    tmp_path,
    mutate_sql,
) -> SqliteDatabase:
    schema_rows = source.execute_sql(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name
        """
    ).fetchall()
    statements = [row[3] for row in schema_rows]
    statements = mutate_sql(statements)
    database = SqliteDatabase(
        str(tmp_path / f"mutated-{_id()}.sqlite3"),
        pragmas={"foreign_keys": 1},
    )
    database.connect()
    for statement in statements:
        database.execute_sql(statement)
    database.execute_sql("PRAGMA application_id = 1245924688")
    database.execute_sql("PRAGMA user_version = 3")
    return database


def test_schema_validation_rejects_changed_run_checks(
    validator_database, tmp_path
) -> None:
    required = "CHECK (ingress_kind <> 'CAPTURED' OR hardware_id IS NOT NULL)"

    def remove_check(statements):
        return [statement.replace(f", {required}", "") for statement in statements]

    database = _cloned_schema_database(
        validator_database, tmp_path, remove_check
    )
    try:
        result = validate_run_persistence_schema(database)
    finally:
        database.close()

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_RUN_CHECKS_MISMATCH"


def test_schema_validation_rejects_duplicate_sixteenth_run_check(
    validator_database,
    tmp_path,
) -> None:
    duplicate = "CHECK (ingress_kind IN ('CAPTURED', 'IMPORTED'))"

    def add_duplicate_check(statements):
        changed = []
        for statement in statements:
            if statement.startswith('CREATE TABLE "run"'):
                statement = statement.replace(
                    ", CHECK (trigger_source",
                    f", {duplicate}, CHECK (trigger_source",
                    1,
                )
            changed.append(statement)
        return changed

    database = _cloned_schema_database(
        validator_database,
        tmp_path,
        add_duplicate_check,
    )
    try:
        result = validate_run_persistence_schema(database)
    finally:
        database.close()

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_RUN_CHECKS_MISMATCH"


def test_schema_validation_rejects_duplicate_identical_foreign_key_group(
    validator_database,
    tmp_path,
) -> None:
    foreign_key = (
        "FOREIGN KEY (run_id) REFERENCES run(run_id) ON DELETE RESTRICT"
    )

    def add_duplicate_foreign_key(statements):
        changed = []
        for statement in statements:
            if statement.startswith('CREATE TABLE "audio"'):
                statement = statement.replace(
                    f", {foreign_key}",
                    f", {foreign_key}, {foreign_key}",
                    1,
                )
            changed.append(statement)
        return changed

    database = _cloned_schema_database(
        validator_database,
        tmp_path,
        add_duplicate_foreign_key,
    )
    try:
        result = validate_run_persistence_schema(database)
    finally:
        database.close()

    assert result.valid is False
    assert result.failure is not None
    assert result.failure.code.value == "DATABASE_RUN_CHECKS_MISMATCH"


def test_schema_validation_does_not_inspect_foreign_key_definitions(
    validator_database, tmp_path
) -> None:
    def weaken_fk(statements):
        changed = []
        for statement in statements:
            if statement.startswith('CREATE TABLE "audio"'):
                statement = statement.replace(
                    "REFERENCES run(run_id) ON DELETE RESTRICT",
                    "REFERENCES run(run_id) ON DELETE CASCADE",
                )
            changed.append(statement)
        return changed

    database = _cloned_schema_database(
        validator_database, tmp_path, weaken_fk
    )
    try:
        result = validate_run_persistence_schema(database)
    finally:
        database.close()

    assert result.valid is False
    assert result.failure is not None


def test_schema_validation_does_not_inspect_unique_constraints(
    validator_database,
) -> None:
    validator_database.execute_sql("DROP INDEX audiotable_run_id")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None


@pytest.mark.parametrize(
    ("index_name", "replacement_sql", "expected_item"),
    [
        (
            "audiotable_run_id",
            """
            CREATE UNIQUE INDEX audiotable_run_id
            ON audio(run_id)
            WHERE container_format = 'WAV_PCM'
            """,
            "unique:audio(run_id)",
        ),
        (
            "audiotable_audio_id_run_id",
            """
            CREATE UNIQUE INDEX audiotable_audio_id_run_id
            ON audio(audio_id, run_id)
            WHERE container_format = 'WAV_PCM'
            """,
            "unique:audio(audio_id,run_id)",
        ),
        (
            "audiotable_final_path",
            """
            CREATE UNIQUE INDEX audiotable_final_path
            ON audio(final_path)
            WHERE container_format = 'WAV_PCM'
            """,
            "unique:audio(final_path)",
        ),
        (
            "audiotable_final_path",
            """
            CREATE UNIQUE INDEX audiotable_final_path
            ON audio(final_path COLLATE BINARY)
            """,
            "unique:audio(final_path)",
        ),
    ],
)
def test_schema_validation_does_not_inspect_index_definitions(
    validator_database,
    index_name: str,
    replacement_sql: str,
    expected_item: str,
) -> None:
    validator_database.execute_sql(f'DROP INDEX "{index_name}"')
    validator_database.execute_sql(replacement_sql)

    result = validate_run_persistence_schema(validator_database)

    assert expected_item
    assert result.valid is False
    assert result.failure is not None


def test_schema_validation_does_not_inspect_unexpected_indexes(
    validator_database,
) -> None:
    validator_database.execute_sql(
        """
        CREATE UNIQUE INDEX unexpected_attempt_run_unique
        ON analysis_attempt(run_id)
        """
    )

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None


def test_schema_validation_does_not_inspect_triggers(
    validator_database,
) -> None:
    validator_database.execute_sql(
        """
        CREATE TRIGGER reject_run_insert
        BEFORE INSERT ON run
        BEGIN
            SELECT RAISE(ABORT, 'blocked');
        END
        """
    )

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is True
    assert result.failure is None


def test_schema_validation_does_not_inspect_missing_partial_index(
    validator_database,
) -> None:
    validator_database.execute_sql("DROP INDEX uq_attempt_initial_per_run")

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None


def test_schema_validation_does_not_inspect_partial_index_predicate(
    validator_database,
) -> None:
    validator_database.execute_sql("DROP INDEX uq_attempt_initial_per_run")
    validator_database.execute_sql(
        """
        CREATE UNIQUE INDEX uq_attempt_initial_per_run
        ON analysis_attempt(run_id)
        WHERE attempt_kind = 'INITIAL' AND state = 'QUEUED'
        """
    )

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is False
    assert result.failure is not None


def test_schema_validation_is_read_only_and_preserves_rows_and_file_bytes(
    validator_database, now: str, monkeypatch
) -> None:
    run_id = _create_run(now)
    database_path = validator_database.database
    with open(database_path, "rb") as database_file:
        before_bytes = database_file.read()
    statements: list[str] = []
    original_execute = validator_database.execute_sql

    def record_execute(sql, params=None):
        statements.append(sql)
        return original_execute(sql, params)

    monkeypatch.setattr(validator_database, "execute_sql", record_execute)

    result = validate_run_persistence_schema(validator_database)

    assert result.valid is True
    assert RunTable.get_by_id(run_id).config_snapshot_json == '{"config_id":1}'
    with open(database_path, "rb") as database_file:
        assert database_file.read() == before_bytes
    assert statements
    assert not any(
        forbidden in statement.upper()
        for statement in statements
        for forbidden in ("CREATE ", "ALTER ", "DROP ")
    )


def test_fresh_v2_persists_import_source_expected_frames_and_causal_revision(
    raw_schema_database,
    now: str,
) -> None:
    run_id = _id()
    values = _run_values(
        now,
        run_id=run_id,
        ingress_kind="IMPORTED",
        trigger_source="IMPORT",
        hardware_id=None,
        imported_source_identity="sha256:imported-source",
        imported_source_path="C:/managed/import.wav",
        imported_expected_frame_count=48_000,
        causal_revision=0,
    )

    RunTable.create(**values)
    stored = raw_schema_database.execute_sql(
        """
        SELECT imported_source_identity, imported_source_path,
               imported_expected_frame_count, causal_revision
        FROM run WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    assert stored == (
        "sha256:imported-source",
        "C:/managed/import.wav",
        48_000,
        0,
    )


def test_fresh_v2_audio_row_persists_save_source_and_content_facts(
    raw_schema_database,
    now: str,
) -> None:
    run_id = _create_run(now, causal_revision=0)
    values = _audio_values(
        run_id,
        now,
        save_job_id=_id(),
        source_kind="CAPTURED_AUDIO",
        source_identity="capture:immutable",
        content_revision=1,
    )

    AudioTable.create(**values)
    stored = raw_schema_database.execute_sql(
        """
        SELECT save_job_id, source_kind, source_identity, content_revision
        FROM audio WHERE audio_id = ?
        """,
        (values["audio_id"],),
    ).fetchone()

    assert stored == (
        values["save_job_id"],
        "CAPTURED_AUDIO",
        "capture:immutable",
        1,
    )


def test_old_v2_shape_fails_validation_without_migration_or_repair(
    tmp_path,
) -> None:
    path = tmp_path / "old-v2.sqlite3"
    database = SqliteDatabase(str(path), pragmas={"foreign_keys": 1})
    database.connect()
    database.execute_sql(
        """
        CREATE TABLE run (
            run_id TEXT NOT NULL PRIMARY KEY,
            ingress_kind TEXT NOT NULL,
            trigger_source TEXT NOT NULL,
            config_id INTEGER NOT NULL,
            hardware_id INTEGER,
            config_snapshot_json TEXT NOT NULL,
            hardware_snapshot_json TEXT NOT NULL,
            calibration_snapshot_json TEXT NOT NULL,
            current_trace_json TEXT,
            state TEXT NOT NULL,
            judgment TEXT,
            judgment_source TEXT NOT NULL,
            fault_type TEXT,
            manual_judgment_revision INTEGER,
            judged_at TEXT,
            terminal_code TEXT,
            terminal_summary TEXT,
            accepted_at TEXT NOT NULL,
            terminal_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK (ingress_kind IN ('CAPTURED', 'IMPORTED')),
            CHECK (trigger_source IN ('PLC', 'MANUAL', 'SCANNER', 'IMPORT')),
            CHECK (state IN ('ACTIVE', 'SUCCEEDED', 'NO_RESULT', 'FAILED',
                             'CANCELED', 'INTERRUPTED')),
            CHECK (judgment IS NULL OR judgment IN ('PASS', 'FAIL')),
            CHECK (judgment_source IN ('NONE', 'AUTOMATIC', 'MANUAL')),
            CHECK (ingress_kind <> 'CAPTURED' OR hardware_id IS NOT NULL),
            CHECK ((ingress_kind = 'IMPORTED' AND trigger_source = 'IMPORT')
                OR (ingress_kind <> 'IMPORTED' AND trigger_source <> 'IMPORT')),
            CHECK (json_valid(config_snapshot_json)
                AND json_type(config_snapshot_json) = 'object'),
            CHECK (json_valid(hardware_snapshot_json)
                AND json_type(hardware_snapshot_json) = 'object'),
            CHECK (json_valid(calibration_snapshot_json)
                AND json_type(calibration_snapshot_json) = 'object'),
            CHECK ((state = 'ACTIVE' AND terminal_at IS NULL
                AND terminal_code IS NULL AND terminal_summary IS NULL
                AND judgment_source = 'NONE')
                OR (state <> 'ACTIVE' AND terminal_at IS NOT NULL
                    AND terminal_code IS NOT NULL)),
            CHECK ((judgment_source = 'NONE' AND judgment IS NULL
                AND judged_at IS NULL)
                OR (judgment_source <> 'NONE' AND judgment IS NOT NULL
                    AND judged_at IS NOT NULL)),
            CHECK ((judgment_source = 'MANUAL'
                AND manual_judgment_revision IS NOT NULL
                AND typeof(manual_judgment_revision) = 'integer'
                AND manual_judgment_revision > 0)
                OR (judgment_source <> 'MANUAL'
                    AND manual_judgment_revision IS NULL
                    AND fault_type IS NULL)),
            CHECK (fault_type IS NULL OR
                (judgment_source = 'MANUAL' AND judgment = 'FAIL')),
            CHECK (current_trace_json IS NULL OR
                (json_valid(current_trace_json)
                 AND json_type(current_trace_json) = 'object'))
        )
        """
    )
    database.execute_sql(
        """
        CREATE TABLE audio (
            audio_id TEXT NOT NULL PRIMARY KEY,
            run_id TEXT NOT NULL,
            final_path TEXT NOT NULL COLLATE NOCASE,
            container_format TEXT NOT NULL,
            sample_rate_hz INTEGER NOT NULL,
            channel_count INTEGER NOT NULL,
            frame_count INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            byte_size INTEGER NOT NULL,
            closed_verified_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    database.execute_sql(
        """
        CREATE TABLE analysis_attempt (
            attempt_id TEXT NOT NULL PRIMARY KEY,
            run_id TEXT NOT NULL,
            audio_id TEXT NOT NULL,
            attempt_kind TEXT NOT NULL,
            state TEXT NOT NULL,
            judgment TEXT,
            result_payload_json TEXT,
            error_code TEXT,
            error_summary TEXT,
            queued_at TEXT NOT NULL,
            started_at TEXT,
            terminal_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    database.execute_sql("PRAGMA application_id = 1245924688")
    database.execute_sql("PRAGMA user_version = 2")
    before = database.execute_sql(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()

    result = validate_run_persistence_schema(database)
    after = database.execute_sql(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    database.close()

    assert result.valid is False
    assert before == after


def test_fresh_v2_schema_matches_all_markers_column_tuples_checks_indexes_and_foreign_keys(
    validator_database,
) -> None:
    expected_columns = {
        "run": (
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
        ),
        "audio": (
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
        ),
        "analysis_attempt": (
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
        ),
    }
    actual_columns = {}
    for table in expected_columns:
        rows = validator_database.execute_sql(
            f'PRAGMA table_xinfo("{table}")'
        ).fetchall()
        actual_columns[table] = tuple(
            (row[1], row[2], row[3], row[4], row[5], row[6])
            for row in rows
        )

    expected_indexes = {
        "run": {
            ("sqlite_autoindex_run_1", 1, (("run_id", "BINARY"),), None),
        },
        "audio": {
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
        },
        "analysis_attempt": {
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
        },
    }
    actual_indexes = {}
    for table in expected_indexes:
        table_indexes = set()
        for row in validator_database.execute_sql(
            f'PRAGMA index_list("{table}")'
        ).fetchall():
            name, unique, partial = row[1], row[2], row[4]
            keys = tuple(
                (item[2], item[4])
                for item in validator_database.execute_sql(
                    f'PRAGMA index_xinfo("{name}")'
                ).fetchall()
                if item[5] == 1
            )
            sql_row = validator_database.execute_sql(
                "SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
            predicate = None
            if partial:
                predicate = sql_row[0].split(" WHERE ", 1)[1]
            table_indexes.add((name, unique, keys, predicate))
        actual_indexes[table] = table_indexes

    expected_foreign_keys = {
        "run": set(),
        "audio": {
            (("run_id",), "run", ("run_id",), "NO ACTION", "RESTRICT", "NONE"),
        },
        "analysis_attempt": {
            (("run_id",), "run", ("run_id",), "NO ACTION", "RESTRICT", "NONE"),
            (
                ("audio_id", "run_id"),
                "audio",
                ("audio_id", "run_id"),
                "NO ACTION",
                "RESTRICT",
                "NONE",
            ),
        },
    }
    actual_foreign_keys = {}
    for table in expected_foreign_keys:
        groups = {}
        for row in validator_database.execute_sql(
            f'PRAGMA foreign_key_list("{table}")'
        ).fetchall():
            groups.setdefault(row[0], []).append(row)
        actual_foreign_keys[table] = {
            (
                tuple(item[3] for item in rows),
                rows[0][2],
                tuple(item[4] for item in rows),
                rows[0][5],
                rows[0][6],
                rows[0][7],
            )
            for _, rows in sorted(groups.items())
        }

    assert validator_database.execute_sql(
        "PRAGMA application_id"
    ).fetchone()[0] == 1245924688
    assert validator_database.execute_sql(
        "PRAGMA user_version"
    ).fetchone()[0] == 3
    assert validator_database.execute_sql(
        "PRAGMA foreign_keys"
    ).fetchone()[0] == 1
    assert actual_columns == expected_columns
    assert actual_indexes == expected_indexes
    assert actual_foreign_keys == expected_foreign_keys
    assert validate_run_persistence_schema(validator_database).valid is True


def test_imported_run_check_rejects_null_identity_or_path(
    raw_schema_database,
    now: str,
) -> None:
    base = _run_values(
        now,
        ingress_kind="IMPORTED",
        trigger_source="IMPORT",
        hardware_id=None,
        imported_source_identity="sha256:source",
        imported_source_path="C:/managed/import.wav",
        imported_expected_frame_count=10,
        causal_revision=0,
    )
    for identity, path in (
        (None, "C:/managed/import.wav"),
        ("sha256:source", None),
    ):
        values = dict(base)
        values.update(
            run_id=_id(),
            imported_source_identity=identity,
            imported_source_path=path,
        )
        with pytest.raises(IntegrityError):
            RunTable.create(**values)
