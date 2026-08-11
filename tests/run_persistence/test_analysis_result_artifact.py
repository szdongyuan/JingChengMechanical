from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import zlib

import pytest

import app.service.run_persistence.attempt_repository as attempt_repository_module
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactError,
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.attempt_repository import (
    AttemptOutcome,
    AttemptRepository,
)
from app.service.run_persistence.contracts import (
    AttemptState,
    Judgment,
    TransactionResult,
    TransactionState,
)
from app.service.run_persistence.data_management import (
    DeleteRunStatus,
    RunDataManagementService,
)
from tests.run_persistence.test_attempt_repository import (
    LATER,
    attempt_insert,
    seed_audio,
)
from tests.run_persistence.test_audio_repository import ready_runtime


def _large_canonical_payload() -> str:
    return json.dumps(
        {
            "judgment": "PASS",
            "result": {
                "chart_payload": {
                    "stft": [
                        round((index % 997) / 997.0, 8)
                        for index in range(120_000)
                    ]
                }
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _successful_outcome(payload: str) -> AttemptOutcome:
    return AttemptOutcome(
        state=AttemptState.SUCCEEDED,
        judgment=Judgment.PASS,
        result_payload_json=payload,
        code=None,
        summary="analysis completed",
        result_payload_zlib=zlib.compress(payload.encode("utf-8"), level=1),
    )


def test_store_atomically_publishes_compact_reference_and_round_trips(
    tmp_path: Path,
) -> None:
    store = AnalysisResultArtifactStore(tmp_path / "results")
    payload = _large_canonical_payload()
    attempt_id = "0f77d1f6-5162-4df0-bc56-181855042d45"

    published = store.publish(
        attempt_id,
        "PASS",
        payload,
        precompressed_zlib=zlib.compress(payload.encode("utf-8"), level=1),
    )

    reference = json.loads(published.reference_json)
    assert published.created is True
    assert published.path.is_file()
    assert len(published.reference_json.encode("utf-8")) < 512
    assert reference["schema"] == "analysis-result-artifact/v1"
    assert reference["judgment"] == "PASS"
    assert reference["uncompressed_bytes"] == len(payload.encode("utf-8"))
    assert reference["compressed_bytes"] == published.path.stat().st_size
    assert store.load_reference(
        published.reference_json,
        expected_judgment="PASS",
    ) == payload
    assert not tuple(store.root.glob("*.part"))


def test_store_rejects_corruption_and_unsafe_reference_paths(
    tmp_path: Path,
) -> None:
    store = AnalysisResultArtifactStore(tmp_path / "results")
    payload = _large_canonical_payload()
    published = store.publish(
        "5b209b64-25e3-4229-9736-93ab84bbd351",
        "PASS",
        payload,
    )
    published.path.write_bytes(b"corrupt")

    with pytest.raises(AnalysisResultArtifactError):
        store.load_reference(published.reference_json)

    unsafe = json.loads(published.reference_json)
    unsafe["artifact_file"] = "../outside.json.zlib"
    with pytest.raises(AnalysisResultArtifactError, match="unsafe"):
        store.path_for_reference(json.dumps(unsafe))


def test_store_rejects_precompressed_bytes_for_a_different_payload(
    tmp_path: Path,
) -> None:
    store = AnalysisResultArtifactStore(tmp_path / "results")

    with pytest.raises(AnalysisResultArtifactError, match="checksum"):
        store.publish(
            "2b655ca0-ec51-4d86-91c1-ea92b4edb917",
            "PASS",
            '{"judgment":"PASS","result":{"value":1}}',
            precompressed_zlib=zlib.compress(
                b'{"judgment":"PASS","result":{"value":2}}'
            ),
        )

    assert not tuple(store.root.glob("*.json.zlib"))
    assert not tuple(store.root.glob("*.part"))


def test_concurrent_duplicate_publication_has_one_artifact_owner(
    tmp_path: Path,
) -> None:
    store = AnalysisResultArtifactStore(tmp_path / "results")
    payload = _large_canonical_payload()
    attempt_id = "d86481c9-9d64-428b-96d9-4c71da8c30dc"

    with ThreadPoolExecutor(max_workers=2) as pool:
        published = tuple(
            pool.map(
                lambda _index: store.publish(
                    attempt_id,
                    "PASS",
                    payload,
                ),
                range(2),
            )
        )

    assert sorted(item.created for item in published) == [False, True]
    assert published[0].path == published[1].path
    assert store.load_reference(published[0].reference_json) == payload


def test_attempt_repository_stores_reference_but_reads_full_payload(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    store = AnalysisResultArtifactStore(tmp_path / "results")
    repository = AttemptRepository(
        runtime,
        result_artifact_store=store,
    )
    attempt = attempt_insert(run_id, audio.audio_id, now)
    payload = _large_canonical_payload()
    assert repository.create_queued(attempt).value is True
    assert repository.mark_running(attempt.attempt_id, LATER).value is True

    finished = repository.finish_running_and_project(
        attempt.attempt_id,
        _successful_outcome(payload),
        "2026-07-27T12:36:00.000000Z",
    )

    with runtime.operation() as database:
        stored_reference = database.execute_sql(
            """
            SELECT result_payload_json
            FROM analysis_attempt
            WHERE attempt_id = ?
            """,
            (attempt.attempt_id,),
        ).fetchone()[0]
    record = repository.latest_successful_for_run(run_id)
    assert finished.state is TransactionState.COMMITTED
    assert finished.value is True
    assert AnalysisResultArtifactStore.is_reference(stored_reference)
    assert len(stored_reference.encode("utf-8")) < 512
    assert record is not None
    assert record.result_payload_json == payload
    assert store.path_for_reference(stored_reference).is_file()


def test_late_duplicate_does_not_remove_already_committed_artifact(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    store = AnalysisResultArtifactStore(tmp_path / "results")
    repository = AttemptRepository(runtime, result_artifact_store=store)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    payload = _large_canonical_payload()
    outcome = _successful_outcome(payload)
    assert repository.create_queued(attempt).value is True
    assert repository.mark_running(attempt.attempt_id, LATER).value is True
    assert repository.finish_running_and_project(
        attempt.attempt_id,
        outcome,
        "2026-07-27T12:36:00.000000Z",
    ).value is True

    late = repository.finish_running_and_project(
        attempt.attempt_id,
        outcome,
        "2026-07-27T12:37:00.000000Z",
    )

    assert late.state is TransactionState.COMMITTED
    assert late.value is False
    assert repository.latest_successful_for_run(run_id).result_payload_json == payload


def test_concurrent_duplicate_terminal_writes_keep_the_committed_artifact(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    store = AnalysisResultArtifactStore(tmp_path / "results")
    repository = AttemptRepository(runtime, result_artifact_store=store)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    payload = _large_canonical_payload()
    outcome = _successful_outcome(payload)
    assert repository.create_queued(attempt).value is True
    assert repository.mark_running(attempt.attempt_id, LATER).value is True
    original_write = repository._write
    publication_barrier = threading.Barrier(2)

    def write_after_both_publications(work):
        publication_barrier.wait(timeout=5)
        return original_write(work)

    monkeypatch.setattr(repository, "_write", write_after_both_publications)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda index: repository.finish_running_and_project(
                    attempt.attempt_id,
                    outcome,
                    f"2026-07-27T12:3{6 + index}:00.000000Z",
                ),
                range(2),
            )
        )

    assert sorted(result.value for result in results) == [False, True]
    stored = repository.latest_successful_for_run(run_id)
    assert stored is not None
    assert stored.result_payload_json == payload
    assert len(tuple(store.root.glob("*.json.zlib"))) == 1


def test_rolled_back_terminal_write_removes_new_unreferenced_artifact(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    store = AnalysisResultArtifactStore(tmp_path / "results")
    repository = AttemptRepository(runtime, result_artifact_store=store)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    assert repository.create_queued(attempt).value is True
    assert repository.mark_running(attempt.attempt_id, LATER).value is True
    monkeypatch.setattr(
        attempt_repository_module,
        "run_short_transaction",
        lambda _database, _work: TransactionResult(
            state=TransactionState.ROLLED_BACK,
            reason="forced rollback",
        ),
    )

    result = repository.finish_running_and_project(
        attempt.attempt_id,
        _successful_outcome(_large_canonical_payload()),
        "2026-07-27T12:36:00.000000Z",
    )

    assert result.state is TransactionState.ROLLED_BACK
    assert not tuple(store.root.glob("*.json.zlib"))


def test_delete_run_removes_audio_and_result_artifact_after_db_commit(
    prepared_database_path: Path,
    tmp_path: Path,
    now: str,
) -> None:
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    store = AnalysisResultArtifactStore(tmp_path / "results")
    repository = AttemptRepository(runtime, result_artifact_store=store)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    assert repository.create_queued(attempt).value is True
    assert repository.mark_running(attempt.attempt_id, LATER).value is True
    assert repository.finish_running_and_project(
        attempt.attempt_id,
        _successful_outcome(_large_canonical_payload()),
        "2026-07-27T12:36:00.000000Z",
    ).value is True
    artifact = next(store.root.glob("*.json.zlib"))

    deleted = RunDataManagementService(
        runtime,
        result_artifact_store=store,
    ).delete_one(run_id)

    assert deleted.status is DeleteRunStatus.DELETED
    assert deleted.database_committed is True
    assert not Path(audio.final_path).exists()
    assert not artifact.exists()
