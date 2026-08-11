from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import uuid4

import pytest

from app.service.run_persistence import AudioFinalArtifactError
from app.service.run_persistence import database as database_module
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.contracts import (
    IngressKind,
    RunInsert,
    TransactionResult,
    TransactionState,
    TriggerSource,
    VerifiedAudioFact,
    _issue_verified_audio_fact,
    freeze_run_inputs,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.run_repository import RunRepository
from tests.run_persistence.conftest import make_analysis_envelope


def ready_runtime(path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    assert runtime.open_and_validate_startup().ok
    with runtime._startup_settlement_operation():
        pass
    runtime._finish_startup(TransactionResult(state=TransactionState.COMMITTED))
    return runtime


def insert_run(runtime, now) -> str:
    run_id = str(uuid4())
    result = RunRepository(runtime).insert_active(
        RunInsert(
            run_id=run_id,
            ingress_kind=IngressKind.CAPTURED,
            trigger_source=TriggerSource.MANUAL,
            config_id=1,
            hardware_id=2,
            snapshots=freeze_run_inputs(
                make_analysis_envelope(config={"config": 1})
            ),
            accepted_at=now,
        )
    )
    assert result.value is True
    return run_id


def fact(
    run_id: str,
    final_path: Path,
    now: str,
    *,
    byte_size: int | None = None,
    save_job_id: str | None = None,
    source_kind: str = "CAPTURED_AUDIO",
    source_identity: str = "capture:test",
    content_revision: int = 1,
) -> VerifiedAudioFact:
    return _issue_verified_audio_fact(
        audio_id=str(uuid4()),
        save_job_id=save_job_id or str(uuid4()),
        run_id=run_id,
        source_kind=source_kind,
        source_identity=source_identity,
        content_revision=content_revision,
        final_path=str(final_path.resolve(strict=False)),
        container_format="WAV_PCM",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=8,
        duration_ms=1,
        byte_size=(
            byte_size
            if byte_size is not None
            else final_path.stat().st_size
            if final_path.is_file()
            else 60
        ),
        closed_verified_at=now,
        created_at=now,
    )


class OperationCountingRuntime:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.operation_calls = 0

    def operation(self):
        self.operation_calls += 1
        return self.wrapped.operation()

    def quarantine_unknown(self):
        return self.wrapped.quarantine_unknown()


def test_insert_requires_verified_fact_and_round_trips_immutable_row(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    repository = AudioRepository(runtime)
    final = tmp_path / "final.wav"
    final.write_bytes(b"x" * 60)
    verified = fact(run_id, final, now)

    with pytest.raises(TypeError):
        repository.insert_verified(object())
    forged = object.__new__(VerifiedAudioFact)
    with pytest.raises(TypeError):
        repository.insert_verified(forged)

    result = repository.insert_verified(verified)
    record = repository.get_for_run(run_id)

    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    assert record is not None
    assert record.audio_id == verified.audio_id
    assert record.final_path == verified.final_path
    with pytest.raises(FrozenInstanceError):
        record.byte_size = 1


def test_saved_audio_round_trips_exact_save_job_source_and_integer_revision(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    final = tmp_path / "identity.wav"
    final.write_bytes(b"x" * 60)
    save_job_id = str(uuid4())
    verified = _issue_verified_audio_fact(
        audio_id=str(uuid4()),
        save_job_id=save_job_id,
        run_id=run_id,
        source_kind="CAPTURED_AUDIO",
        source_identity="capture:source-1",
        content_revision=1,
        final_path=str(final.resolve(strict=False)),
        container_format="WAV_PCM",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=8,
        duration_ms=1,
        byte_size=60,
        closed_verified_at=now,
        created_at=now,
    )

    result = AudioRepository(runtime).insert_verified(verified)
    record = AudioRepository(runtime).get_for_run(run_id)

    assert result.state is TransactionState.COMMITTED
    assert (
        record.save_job_id,
        record.source_kind,
        record.source_identity,
        record.content_revision,
    ) == (
        save_job_id,
        "CAPTURED_AUDIO",
        "capture:source-1",
        1,
    )
    assert type(record.content_revision) is int


def test_one_audio_per_run_and_nocase_path_uniqueness_return_exact_failure(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    repository = AudioRepository(runtime)
    first_run = insert_run(runtime, now)
    second_run = insert_run(runtime, now)
    first_path = tmp_path / "Audio.WAV"
    first_path.write_bytes(b"a" * 60)
    first = fact(first_run, first_path, now)
    assert repository.insert_verified(first).state is TransactionState.COMMITTED

    other_path = tmp_path / "other.wav"
    other_path.write_bytes(b"b" * 60)
    duplicate_run = repository.insert_verified(
        fact(first_run, other_path, now)
    )
    duplicate_case_path = tmp_path / "audio.wav"
    duplicate_case_path.write_bytes(b"c" * 60)
    duplicate_path = repository.insert_verified(
        fact(second_run, duplicate_case_path, now)
    )

    assert duplicate_run.state is TransactionState.ROLLED_BACK
    assert duplicate_run.reason.startswith("AUDIO_DB_INSERT_FAILED:")
    assert duplicate_path.state is TransactionState.ROLLED_BACK
    assert duplicate_path.reason.startswith("AUDIO_DB_INSERT_FAILED:")
    assert repository.get_for_run(first_run).audio_id == first.audio_id
    assert repository.get_for_run(second_run) is None


def test_rolled_back_audio_insert_leaves_published_orphan(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    repository = AudioRepository(runtime)
    first_run = insert_run(runtime, now)
    second_run = insert_run(runtime, now)
    final = tmp_path / "published.wav"
    final.write_bytes(b"winner")
    assert repository.insert_verified(fact(first_run, final, now)).value is True

    result = repository.insert_verified(fact(second_run, final, now))

    assert result.state is TransactionState.ROLLED_BACK
    assert final.read_bytes() == b"winner"
    assert repository.get_for_run(second_run) is None


def test_unknown_requests_restart_and_does_not_repair(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    repository = AudioRepository(runtime)
    run_id = insert_run(runtime, now)
    original_open = database_module._open_existing_database

    def open_with_failed_rollback(path):
        database = original_open(path)
        database.rollback = lambda: (_ for _ in ()).throw(OSError("rollback failed"))
        return database

    monkeypatch.setattr(
        database_module, "_open_existing_database", open_with_failed_rollback
    )
    invalid = fact(run_id, tmp_path / "one.wav", now)
    Path(invalid.final_path).write_bytes(b"1" * invalid.byte_size)
    assert repository.insert_verified(invalid).state is TransactionState.COMMITTED
    second = tmp_path / "two.wav"
    second.write_bytes(b"2" * 60)
    result = repository.insert_verified(fact(run_id, second, now))

    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
    assert runtime.closed


@pytest.mark.parametrize("artifact", ["missing", "directory", "size_mismatch"])
def test_insert_rechecks_exact_final_artifact_before_database_work(
    prepared_database_path, tmp_path, now, artifact
):
    runtime = ready_runtime(prepared_database_path)
    tracked_runtime = OperationCountingRuntime(runtime)
    repository = AudioRepository(tracked_runtime)
    run_id = insert_run(runtime, now)
    final = tmp_path / artifact
    if artifact == "directory":
        final.mkdir()
    elif artifact == "size_mismatch":
        final.write_bytes(b"short")

    with pytest.raises(AudioFinalArtifactError) as raised:
        repository.insert_verified(fact(run_id, final, now, byte_size=60))

    assert raised.value.code == "AUDIO_FINAL_SIZE_MISMATCH"
    assert raised.value.final_path == str(final.resolve(strict=False))
    assert "AUDIO_DB_INSERT_FAILED" not in str(raised.value)
    assert tracked_runtime.operation_calls == 0
    assert AudioRepository(runtime).get_for_run(run_id) is None


def test_insert_rechecks_final_readability_before_database_work(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    tracked_runtime = OperationCountingRuntime(runtime)
    repository = AudioRepository(tracked_runtime)
    run_id = insert_run(runtime, now)
    final = tmp_path / "unreadable.wav"
    final.write_bytes(b"x" * 60)
    original_open = Path.open

    def deny_exact_final(path, *args, **kwargs):
        if path == final:
            raise PermissionError("denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_exact_final)
    with pytest.raises(AudioFinalArtifactError) as raised:
        repository.insert_verified(fact(run_id, final, now))

    assert raised.value.code == "AUDIO_FINAL_SIZE_MISMATCH"
    assert raised.value.final_path == str(final.resolve(strict=False))
    assert "denied" in str(raised.value)
    assert "AUDIO_DB_INSERT_FAILED" not in str(raised.value)
    assert tracked_runtime.operation_calls == 0
    assert AudioRepository(runtime).get_for_run(run_id) is None
