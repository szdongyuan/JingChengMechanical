from __future__ import annotations

from pathlib import Path
from uuid import uuid4
import wave

import pytest

from app.service.run_persistence.audio_publication import AudioPublication
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.audio_service import AudioPersistenceService
from app.service.run_persistence.contracts import (
    ContractValidationError,
    IngressKind,
    RunInsert,
    TransactionResult,
    TransactionState,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.run_repository import RunRepository
from tests.run_persistence.test_audio_repository import (
    OperationCountingRuntime,
    fact,
    insert_run,
    ready_runtime,
)
from tests.run_persistence.conftest import make_analysis_envelope


def make_wav(path: Path) -> bytes:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(8_000)
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.writeframes(b"\x01\x00" * 8)
    return path.read_bytes()


class CountingRunRepository:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.finish_calls = 0

    def finish_active(self, *args, **kwargs):
        self.finish_calls += 1
        return self.wrapped.finish_active(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self.wrapped.get(*args, **kwargs)


class FixedTerminalResultRunRepository:
    def __init__(self, result):
        self.result = result
        self.finish_calls = 0

    def finish_active(self, *args, **kwargs):
        self.finish_calls += 1
        return self.result


class MutatingAudioRepository:
    def __init__(self, wrapped, mutate):
        self.wrapped = wrapped
        self.mutate = mutate

    def insert_verified(self, fact):
        self.mutate(Path(fact.final_path))
        return self.wrapped.insert_verified(fact)


def counts(runtime):
    with runtime.operation() as database:
        return (
            database.execute_sql("SELECT COUNT(*) FROM audio").fetchone()[0],
            database.execute_sql(
                "SELECT COUNT(*) FROM analysis_attempt"
            ).fetchone()[0],
        )


def insert_imported_run(
    runtime,
    now: str,
    source_path: Path,
    *,
    source_identity: str = "sha256:immutable-source",
    expected_frame_count: int = 8,
) -> str:
    run_id = str(uuid4())
    result = RunRepository(runtime).insert_active(
        RunInsert(
            run_id=run_id,
            ingress_kind=IngressKind.IMPORTED,
            trigger_source=TriggerSource.IMPORT,
            config_id=1,
            hardware_id=None,
            snapshots=freeze_run_inputs(
                make_analysis_envelope(config={"config": 1})
            ),
            accepted_at=now,
            imported_source_identity=source_identity,
            imported_source_path=str(source_path.resolve(strict=False)),
            imported_expected_frame_count=expected_frame_count,
        )
    )
    assert result.value is True
    return run_id


def _persist_exact(
    service: AudioPersistenceService,
    *,
    run_id: str,
    source: Path,
    final: Path,
    now: str,
    save_job_id: str | None = None,
    source_kind: str = "CAPTURED_AUDIO",
    source_identity: str = "capture:source-1",
    content_revision: int = 1,
    expected_frame_count: int = 8,
    source_frame_count: int = 8,
):
    return service.persist(
        run_id=run_id,
        save_job_id=save_job_id or str(uuid4()),
        source_kind=source_kind,
        source_identity=source_identity,
        content_revision=content_revision,
        source_path=source,
        final_path=final,
        sample_rate_hz=8_000,
        channel_count=1,
        sample_width_bytes=2,
        expected_frame_count=expected_frame_count,
        source_frame_count=source_frame_count,
        now=now,
    )


def test_success_publishes_then_commits_audio_before_initial_permission(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    service = AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
    )

    result = _persist_exact(
        service,
        run_id=run_id,
        source=source,
        final=tmp_path / "final.wav",
        now=now,
    )

    assert result.initial_attempt_permitted is True
    assert result.fact is not None
    assert result.fact.final_path == str((tmp_path / "final.wav").resolve())
    assert AudioRepository(runtime).get_for_run(run_id).final_path == result.fact.final_path
    assert counts(runtime) == (1, 0)


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("write", "AUDIO_WRITE_FAILED"),
        ("close", "AUDIO_CLOSE_FAILED"),
        ("verify", "AUDIO_VERIFY_FAILED"),
        ("finalize", "AUDIO_FINALIZE_FAILED"),
        ("size", "AUDIO_FINAL_SIZE_MISMATCH"),
    ],
)
def test_file_failures_return_once_without_terminalizing_and_leave_no_rows(
    prepared_database_path, tmp_path, now, monkeypatch, fault, code
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / f"{fault}.wav"
    content = make_wav(source)
    final = tmp_path / f"{fault}-final.wav"
    publication = AudioPublication()
    runs = CountingRunRepository(RunRepository(runtime))
    service = AudioPersistenceService(
        publication, AudioRepository(runtime), runs
    )

    if fault == "size":
        monkeypatch.setattr(publication, "_final_size", lambda path: len(content) + 1)
    else:
        def fail(*args, **kwargs):
            raise OSError(f"{fault} failed")

        monkeypatch.setattr(publication, f"_{fault}", fail)

    result = _persist_exact(
        service, run_id=run_id, source=source, final=final, now=now
    )
    run = runs.wrapped.get(run_id)

    assert result.code == code
    assert result.initial_attempt_permitted is False
    assert runs.finish_calls == 0
    assert run.state == "ACTIVE"
    assert run.terminal_code is None
    assert counts(runtime) == (0, 0)
    assert final.exists() is (fault == "size")


@pytest.mark.parametrize(
    ("error", "final_exists"),
    [
        (FileNotFoundError("final disappeared"), False),
        (PermissionError("final is inaccessible"), True),
    ],
)
def test_post_final_recheck_errors_return_without_terminalizing_or_audio(
    prepared_database_path, tmp_path, now, monkeypatch, error, final_exists
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    source_bytes = make_wav(source)
    final = tmp_path / "final.wav"
    publication = AudioPublication()
    runs = CountingRunRepository(RunRepository(runtime))

    def fail_recheck(path):
        if isinstance(error, FileNotFoundError):
            path.unlink()
        raise error

    monkeypatch.setattr(publication, "_final_size", fail_recheck)
    result = _persist_exact(
        AudioPersistenceService(publication, AudioRepository(runtime), runs),
        run_id=run_id,
        source=source,
        final=final,
        now=now,
    )

    assert result.code == "AUDIO_FINAL_SIZE_MISMATCH"
    assert result.initial_attempt_permitted is False
    assert runs.finish_calls == 0
    assert runs.wrapped.get(run_id).state == "ACTIVE"
    assert counts(runtime) == (0, 0)
    assert final.exists() is final_exists
    if final_exists:
        assert final.read_bytes() == source_bytes


@pytest.mark.parametrize("mutation", ["remove", "resize"])
def test_repository_final_preflight_failure_returns_without_terminalizing(
    prepared_database_path, tmp_path, now, mutation
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    final = tmp_path / "final.wav"
    tracked_runtime = OperationCountingRuntime(runtime)
    repository = AudioRepository(tracked_runtime)
    retained_orphan = b"changed-after-publication"

    def mutate(published_final):
        if mutation == "remove":
            published_final.unlink()
        else:
            published_final.write_bytes(retained_orphan)

    runs = CountingRunRepository(RunRepository(runtime))
    result = _persist_exact(
        AudioPersistenceService(
            AudioPublication(),
            MutatingAudioRepository(repository, mutate),
            runs,
        ),
        run_id=run_id,
        source=source,
        final=final,
        now=now,
    )

    assert result.code == "AUDIO_FINAL_SIZE_MISMATCH"
    assert result.initial_attempt_permitted is False
    assert result.restart_required is False
    assert result.database_result is None
    assert result.fact is not None
    assert result.publication_result is not None
    assert result.publication_result.ok is True
    assert result.run_result is None
    assert runs.finish_calls == 0
    assert tracked_runtime.operation_calls == 0
    assert runs.wrapped.get(run_id).state == "ACTIVE"
    assert counts(runtime) == (0, 0)
    if mutation == "remove":
        assert not final.exists()
    else:
        assert final.read_bytes() == retained_orphan


def test_collision_preserves_target_bytes_and_mtime(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    final = tmp_path / "final.wav"
    final.write_bytes(b"winner")
    before = final.stat().st_mtime_ns
    service = AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
    )

    result = _persist_exact(
        service, run_id=run_id, source=source, final=final, now=now
    )

    assert result.code == "AUDIO_PATH_COLLISION"
    assert final.read_bytes() == b"winner"
    assert final.stat().st_mtime_ns == before
    assert counts(runtime) == (0, 0)


def test_cleanup_failure_reports_diagnostic_without_terminalizing(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    publication = AudioPublication()
    runs = CountingRunRepository(RunRepository(runtime))
    service = AudioPersistenceService(publication, AudioRepository(runtime), runs)
    monkeypatch.setattr(
        publication, "_verify", lambda *args: (_ for _ in ()).throw(ValueError("bad"))
    )
    monkeypatch.setattr(
        publication, "_cleanup", lambda path: (_ for _ in ()).throw(OSError("locked"))
    )

    result = _persist_exact(
        service,
        run_id=run_id,
        source=source,
        final=tmp_path / "final.wav",
        now=now,
    )

    assert result.code == "AUDIO_VERIFY_FAILED"
    assert result.cleanup_code == "AUDIO_TEMP_CLEANUP_FAILED"
    assert result.cleanup_path is not None
    assert runs.finish_calls == 0
    assert counts(runtime) == (0, 0)


def test_publication_failure_does_not_consult_unknown_terminal_result(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    publication = AudioPublication()
    monkeypatch.setattr(
        publication,
        "_verify",
        lambda *args: (_ for _ in ()).throw(ValueError("verify failed")),
    )
    runs = CountingRunRepository(RunRepository(runtime))

    result = _persist_exact(
        AudioPersistenceService(publication, AudioRepository(runtime), runs),
        run_id=run_id,
        source=source,
        final=tmp_path / "final.wav",
        now=now,
    )

    assert result.code == "AUDIO_VERIFY_FAILED"
    assert result.reason == "verify failed"
    assert result.restart_required is False
    assert result.run_result is None
    assert result.publication_result is not None
    assert result.publication_result.code.value == "AUDIO_VERIFY_FAILED"
    assert result.publication_result.reason == "verify failed"
    assert runs.finish_calls == 0
    assert counts(runtime) == (0, 0)


def test_publication_failure_does_not_consult_rolled_back_terminal_result(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    publication = AudioPublication()
    monkeypatch.setattr(
        publication,
        "_verify",
        lambda *args: (_ for _ in ()).throw(ValueError("verify failed")),
    )
    runs = CountingRunRepository(RunRepository(runtime))

    result = _persist_exact(
        AudioPersistenceService(publication, AudioRepository(runtime), runs),
        run_id=run_id,
        source=source,
        final=tmp_path / "final.wav",
        now=now,
    )

    assert result.code == "AUDIO_VERIFY_FAILED"
    assert result.reason == "verify failed"
    assert result.restart_required is False
    assert result.run_result is None
    assert result.publication_result is not None
    assert result.publication_result.code.value == "AUDIO_VERIFY_FAILED"
    assert result.publication_result.reason == "verify failed"
    assert runs.finish_calls == 0
    assert counts(runtime) == (0, 0)


def test_publication_failure_does_not_consult_committed_terminal_result(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    publication = AudioPublication()
    monkeypatch.setattr(
        publication,
        "_verify",
        lambda *args: (_ for _ in ()).throw(ValueError("verify failed")),
    )
    runs = CountingRunRepository(RunRepository(runtime))

    result = _persist_exact(
        AudioPersistenceService(publication, AudioRepository(runtime), runs),
        run_id=run_id,
        source=source,
        final=tmp_path / "final.wav",
        now=now,
    )

    assert result.code == "AUDIO_VERIFY_FAILED"
    assert result.reason == "verify failed"
    assert result.restart_required is False
    assert result.run_result is None
    assert result.publication_result is not None
    assert result.publication_result.code.value == "AUDIO_VERIFY_FAILED"
    assert runs.finish_calls == 0
    assert counts(runtime) == (0, 0)


def test_audio_insert_rollback_leaves_orphan_without_repair_terminal(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    audio_repository = AudioRepository(runtime)
    existing = tmp_path / "existing.wav"
    existing.write_bytes(b"already-published")
    assert audio_repository.insert_verified(fact(run_id, existing, now)).value
    source = tmp_path / "source.wav"
    orphan_bytes = make_wav(source)
    orphan = tmp_path / "orphan.wav"
    runs = CountingRunRepository(RunRepository(runtime))
    service = AudioPersistenceService(AudioPublication(), audio_repository, runs)

    result = _persist_exact(
        service, run_id=run_id, source=source, final=orphan, now=now
    )

    assert result.code == "AUDIO_DB_INSERT_FAILED"
    assert result.database_result.state is TransactionState.ROLLED_BACK
    assert orphan.read_bytes() == orphan_bytes
    assert runs.finish_calls == 0
    assert runs.wrapped.get(run_id).state == "ACTIVE"
    assert counts(runtime) == (1, 0)


def test_audio_insert_unknown_is_indeterminate_and_requires_restart(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    orphan_bytes = make_wav(source)
    orphan = tmp_path / "orphan.wav"
    repository = AudioRepository(runtime)
    runs = CountingRunRepository(RunRepository(runtime))
    monkeypatch.setattr(
        repository,
        "insert_verified",
        lambda fact: TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="commit outcome indeterminate",
            restart_required=True,
        ),
    )

    result = _persist_exact(
        AudioPersistenceService(AudioPublication(), repository, runs),
        run_id=run_id,
        source=source,
        final=orphan,
        now=now,
    )

    assert result.code == "AUDIO_DB_STATE_UNKNOWN"
    assert result.restart_required is True
    assert result.initial_attempt_permitted is False
    assert orphan.read_bytes() == orphan_bytes
    assert runs.finish_calls == 0
    assert counts(runtime) == (0, 0)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("run_id", "not-a-uuid"),
        ("now", "not-a-timestamp"),
        ("sample_rate_hz", 0),
        ("sample_rate_hz", True),
        ("sample_rate_hz", 8_000.0),
        ("channel_count", 0),
        ("channel_count", False),
        ("sample_width_bytes", -1),
        ("sample_width_bytes", True),
    ],
)
def test_invalid_non_file_contracts_fail_before_publication_side_effects(
    prepared_database_path, tmp_path, now, field, invalid
):
    runtime = ready_runtime(prepared_database_path)
    run_id = insert_run(runtime, now)
    source = tmp_path / "source.wav"
    make_wav(source)
    final = tmp_path / "final.wav"
    values = {
        "run_id": run_id,
        "source_path": source,
        "final_path": final,
        "sample_rate_hz": 8_000,
        "channel_count": 1,
        "sample_width_bytes": 2,
        "now": now,
        "save_job_id": str(uuid4()),
        "source_kind": "CAPTURED_AUDIO",
        "source_identity": "capture:source-1",
        "content_revision": 1,
        "expected_frame_count": 8,
        "source_frame_count": 8,
    }
    values[field] = invalid

    with pytest.raises(ContractValidationError, match=field):
        AudioPersistenceService(
            AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
        ).persist(**values)

    assert not final.exists()
    assert list(tmp_path.glob("*.part")) == []
    assert counts(runtime) == (0, 0)


def test_imported_save_rejects_active_source_identity_mismatch_without_partial_effect(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    source = tmp_path / "import.wav"
    make_wav(source)
    run_id = insert_imported_run(runtime, now, source)
    final = tmp_path / "final.wav"
    service = AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
    )

    result = _persist_exact(
        service,
        run_id=run_id,
        source=source,
        final=final,
        now=now,
        source_kind="IMPORTED_SOURCE",
        source_identity="sha256:different-source",
    )

    assert result.initial_attempt_permitted is False
    assert result.code == "AUDIO_REQUEST_MISMATCH"
    assert not final.exists()
    assert counts(runtime) == (0, 0)
    assert RunRepository(runtime).get(run_id).state == "ACTIVE"


def test_imported_save_rejects_active_expected_frame_mismatch_without_partial_effect(
    prepared_database_path,
    tmp_path,
    now,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    source = tmp_path / "import.wav"
    make_wav(source)
    run_id = insert_imported_run(
        runtime,
        now,
        source,
        expected_frame_count=9,
    )
    final = tmp_path / "final.wav"
    service = AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
    )

    result = _persist_exact(
        service,
        run_id=run_id,
        source=source,
        final=final,
        now=now,
        source_kind="IMPORTED_SOURCE",
        source_identity="sha256:immutable-source",
        expected_frame_count=8,
    )

    assert result.initial_attempt_permitted is False
    assert result.code == "AUDIO_REQUEST_MISMATCH"
    assert not final.exists()
    assert counts(runtime) == (0, 0)
    assert RunRepository(runtime).get(run_id).state == "ACTIVE"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_kind": "IMPORTED_SOURCE"},
        {"expected_frame_count": 9, "source_frame_count": 8},
        {"content_revision": 2},
    ],
)
def test_captured_save_rejects_frozen_metadata_or_save_identity_mismatch_without_partial_effect(
    prepared_database_path,
    tmp_path,
    now,
    changes,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    source = tmp_path / "capture.wav"
    make_wav(source)
    run_id = insert_run(runtime, now)
    final = tmp_path / "final.wav"
    service = AudioPersistenceService(
        AudioPublication(), AudioRepository(runtime), RunRepository(runtime)
    )
    values = {
        "service": service,
        "run_id": run_id,
        "source": source,
        "final": final,
        "now": now,
    }
    values.update(changes)

    result = _persist_exact(**values)

    assert result.initial_attempt_permitted is False
    assert result.code in {"AUDIO_REQUEST_MISMATCH", "AUDIO_VERIFY_FAILED"}
    assert not final.exists()
    assert counts(runtime) == (0, 0)
    assert RunRepository(runtime).get(run_id).state == "ACTIVE"


def test_audio_persistence_failure_never_terminalizes_run(
    prepared_database_path,
    tmp_path,
    now,
    monkeypatch,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    source = tmp_path / "capture.wav"
    make_wav(source)
    run_id = insert_run(runtime, now)
    runs = CountingRunRepository(RunRepository(runtime))
    publication = AudioPublication()
    monkeypatch.setattr(
        publication,
        "_verify",
        lambda *_args: (_ for _ in ()).throw(ValueError("invalid staged WAV")),
    )
    service = AudioPersistenceService(
        publication,
        AudioRepository(runtime),
        runs,
    )

    result = _persist_exact(
        service,
        run_id=run_id,
        source=source,
        final=tmp_path / "final.wav",
        now=now,
    )

    assert result.initial_attempt_permitted is False
    assert result.code == "AUDIO_VERIFY_FAILED"
    assert runs.finish_calls == 0
    assert runs.wrapped.get(run_id).state == "ACTIVE"
    assert counts(runtime) == (0, 0)
