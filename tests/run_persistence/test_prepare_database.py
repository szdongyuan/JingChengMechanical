from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import threading

import pytest
from peewee import SqliteDatabase

import app.service.run_persistence.prepare_database as prepare_module
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.prepare_database import (
    RunPersistencePreparationError,
    prepare_run_persistence_database,
)
import scripts.prepare_run_persistence_database as release_script


EXPECTED_SCHEMA_OBJECTS = {
    ("table", "run", "run"),
    ("table", "audio", "audio"),
    ("table", "analysis_attempt", "analysis_attempt"),
    ("index", "audiotable_save_job_id", "audio"),
    ("index", "audiotable_run_id", "audio"),
    ("index", "audiotable_final_path", "audio"),
    ("index", "audiotable_audio_id_run_id", "audio"),
    ("index", "uq_attempt_initial_per_run", "analysis_attempt"),
}


def _staging_files(target: Path) -> list[Path]:
    return list(target.parent.glob(f".{target.name}.*.staging"))


def test_preparer_publishes_closed_valid_exact_schema(
    prepared_database_path: Path,
) -> None:
    database = SqliteDatabase(
        str(prepared_database_path),
        pragmas={"foreign_keys": 1},
    )
    database.connect(reuse_if_open=True)
    try:
        objects = set(
            database.execute_sql(
                """
                SELECT type, name, tbl_name
                FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        )
        assert objects == EXPECTED_SCHEMA_OBJECTS
        assert database.execute_sql("PRAGMA application_id").fetchone()[0] == 1245924688
        assert database.execute_sql("PRAGMA user_version").fetchone()[0] == 2
    finally:
        database.close()

    prepared_database_path.unlink()
    assert not prepared_database_path.exists()


def test_preparer_never_touches_an_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "existing.sqlite3"
    target.write_bytes(b"owner data")
    fixed_time = 1_700_000_000_000_000_000
    os.utime(target, ns=(fixed_time, fixed_time))
    before = (target.read_bytes(), target.stat().st_mtime_ns)

    with pytest.raises(RunPersistencePreparationError, match="exists"):
        prepare_run_persistence_database(target)

    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert _staging_files(target) == []


def test_preparer_staging_collision_preserves_competitor_and_does_not_retry(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "collision.sqlite3"
    staging_hex = "1234567890abcdef1234567890abcdef"
    staging = target.with_name(f".{target.name}.{staging_hex}.staging")
    competitor_bytes = b"competitor-owned staging data"
    competitor_time = 1_700_000_000_000_000_000
    staging.write_bytes(competitor_bytes)
    os.utime(staging, ns=(competitor_time, competitor_time))
    before = (staging.read_bytes(), staging.stat().st_mtime_ns)
    uuid_calls = 0
    open_calls = 0
    original_open = prepare_module._open_staging_database

    def colliding_uuid():
        nonlocal uuid_calls
        uuid_calls += 1
        return SimpleNamespace(hex=staging_hex)

    def count_open(path: Path):
        nonlocal open_calls
        open_calls += 1
        return original_open(path)

    monkeypatch.setattr(prepare_module, "uuid4", colliding_uuid)
    monkeypatch.setattr(prepare_module, "_open_staging_database", count_open)

    with pytest.raises(RunPersistencePreparationError):
        prepare_run_persistence_database(target)

    assert uuid_calls == 1
    assert open_calls == 0
    assert not target.exists()
    assert staging.exists()
    assert (staging.read_bytes(), staging.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    "failing_step",
    [
        "_create_schema",
        "_stamp_markers",
        "_validate_staging_database",
        "_close_database",
        "_publish_no_replace",
    ],
)
def test_preparer_failure_never_publishes_and_cleans_staging_once(
    tmp_path: Path, monkeypatch, failing_step: str
) -> None:
    target = tmp_path / f"{failing_step}.sqlite3"
    original_unlink = prepare_module.os.unlink
    original_step = getattr(prepare_module, failing_step)
    cleanup_calls: list[Path] = []

    def fail(*args, **kwargs):
        if failing_step == "_close_database":
            original_step(*args, **kwargs)
        raise OSError(f"{failing_step} failed")

    def count_cleanup(path):
        cleanup_calls.append(Path(path))
        original_unlink(path)

    monkeypatch.setattr(prepare_module, failing_step, fail)
    monkeypatch.setattr(prepare_module.os, "unlink", count_cleanup)

    with pytest.raises(RunPersistencePreparationError, match=failing_step):
        prepare_run_persistence_database(target)

    assert not target.exists()
    assert len(cleanup_calls) == 1
    assert _staging_files(target) == []


def test_schema_creation_failure_cannot_stamp_markers(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "create-failure.sqlite3"
    stamp_calls = 0

    def fail_creation(database):
        raise OSError("schema creation failed")

    def count_stamp(database):
        nonlocal stamp_calls
        stamp_calls += 1

    monkeypatch.setattr(prepare_module, "_create_schema", fail_creation)
    monkeypatch.setattr(prepare_module, "_stamp_markers", count_stamp)

    with pytest.raises(RunPersistencePreparationError):
        prepare_run_persistence_database(target)

    assert stamp_calls == 0
    assert not target.exists()


def test_no_replace_race_preserves_winner_bytes_and_mtime(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "race.sqlite3"
    ready_to_publish = threading.Event()
    winner_created = threading.Event()
    original_publish = prepare_module._publish_no_replace
    winner_bytes = b"competing owner"
    winner_time = 1_700_000_000_000_000_000

    def pause_before_publish(staging: Path, final_target: Path) -> None:
        ready_to_publish.set()
        assert winner_created.wait(timeout=5)
        original_publish(staging, final_target)

    def create_winner() -> None:
        assert ready_to_publish.wait(timeout=5)
        target.write_bytes(winner_bytes)
        os.utime(target, ns=(winner_time, winner_time))
        winner_created.set()

    monkeypatch.setattr(
        prepare_module, "_publish_no_replace", pause_before_publish
    )
    contender = threading.Thread(target=create_winner)
    contender.start()
    try:
        with pytest.raises(RunPersistencePreparationError, match="publish"):
            prepare_run_persistence_database(target)
    finally:
        contender.join(timeout=5)

    assert not contender.is_alive()
    assert target.read_bytes() == winner_bytes
    assert target.stat().st_mtime_ns == winner_time
    assert _staging_files(target) == []


def test_release_gate_failure_skips_preparer_and_leaves_target_absent(
    tmp_path: Path,
) -> None:
    target = tmp_path / "gated.sqlite3"
    commands: list[tuple[list[str], bool]] = []
    prepare_calls = 0

    def failed_command(command, check):
        commands.append((command, check))
        return SimpleNamespace(returncode=7)

    def count_prepare(path):
        nonlocal prepare_calls
        prepare_calls += 1

    result = release_script.main(
        target,
        run_command=failed_command,
        prepare_database=count_prepare,
    )

    assert result == 7
    assert commands == [
        (
            [
                "python",
                "-m",
                "pytest",
                "tests/run_persistence/test_schema.py",
                "-q",
            ],
            False,
        )
    ]
    assert prepare_calls == 0
    assert not target.exists()


def test_release_gate_success_calls_preparer_exactly_once_after_schema_pass(
    tmp_path: Path,
) -> None:
    target = tmp_path / "gated.sqlite3"
    events: list[tuple[str, object]] = []

    def passed_command(command, check):
        events.append(("schema", (command, check)))
        return SimpleNamespace(returncode=0)

    def count_prepare(path):
        events.append(("prepare", Path(path)))
        return Path(path)

    result = release_script.main(
        target,
        run_command=passed_command,
        prepare_database=count_prepare,
    )

    assert result == 0
    assert events == [
        (
            "schema",
            (
                [
                    "python",
                    "-m",
                    "pytest",
                    "tests/run_persistence/test_schema.py",
                    "-q",
                ],
                False,
            ),
        ),
        ("prepare", target),
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE audio RENAME COLUMN byte_size TO size_bytes",
        "ALTER TABLE analysis_attempt RENAME COLUMN error_summary TO detail",
    ],
)
def test_startup_rejects_incompatible_audio_or_attempt_shape_before_readiness(
    prepared_database_path: Path,
    statement: str,
) -> None:
    database = SqliteDatabase(
        str(prepared_database_path),
        pragmas={"foreign_keys": 1},
    )
    database.connect()
    database.execute_sql(statement)
    database.close()
    runtime = RunPersistenceDatabase(prepared_database_path)

    result = runtime.open_and_validate_startup()

    assert result.ok is False
    assert runtime.ready is False
    assert runtime.closed is True
