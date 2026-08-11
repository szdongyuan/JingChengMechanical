from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4
import wave

import pytest

import app.service.run_persistence.database as database_module
import app.service.run_persistence.run_repository as run_repository_module
from app.service.run_persistence.acceptance import RunAcceptanceService
from app.service.run_persistence.attempt_repository import (
    AttemptOutcome,
    AttemptRepository,
)
from app.service.run_persistence.audio_publication import AudioPublication
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.audio_service import AudioPersistenceService
from app.service.run_persistence.contracts import (
    AttemptInsert,
    AttemptKind,
    AttemptState,
    IngressKind,
    Judgment,
    RunInsert,
    RunState,
    TransactionResult,
    TransactionState,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.database import (
    DatabaseLifecycleCode,
    DatabaseUnavailableError,
    RunPersistenceDatabase,
)
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)
from app.service.run_persistence.run_repository import (
    RunRepository,
    RunTerminal,
)
from app.service.run_persistence.startup import settle_startup
from app.service.run_persistence.transactions import run_short_transaction
from tests.run_persistence.conftest import make_analysis_envelope


NOW = "2026-07-27T12:34:56.123456Z"
LATER = "2026-07-27T12:35:56.123456Z"
LATEST = "2026-07-27T12:36:56.123456Z"


def _ready_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    assert settle_startup(runtime, NOW).ready
    return runtime


def _run(
    run_id: str,
    ingress: IngressKind,
    imported_source_path: Path | None = None,
) -> RunInsert:
    return RunInsert(
        run_id=run_id,
        ingress_kind=ingress,
        trigger_source=(
            TriggerSource.MANUAL
            if ingress is IngressKind.CAPTURED
            else TriggerSource.IMPORT
        ),
        config_id=7,
        hardware_id=9 if ingress is IngressKind.CAPTURED else None,
        snapshots=freeze_run_inputs(
            make_analysis_envelope(config={"config": 7}),
            {"hardware": 9} if ingress is IngressKind.CAPTURED else None,
        ),
        accepted_at=NOW,
        imported_source_identity=(
            f"imported:{run_id}"
            if ingress is IngressKind.IMPORTED
            else None
        ),
        imported_source_path=(
            str(imported_source_path.resolve(strict=False))
            if ingress is IngressKind.IMPORTED
            and imported_source_path is not None
            else str((Path.cwd() / f"{run_id}.wav").resolve(strict=False))
            if ingress is IngressKind.IMPORTED
            else None
        ),
        imported_expected_frame_count=(
            8 if ingress is IngressKind.IMPORTED else None
        ),
    )


def _insert_run(
    runtime: RunPersistenceDatabase,
    ingress: IngressKind = IngressKind.IMPORTED,
    imported_source_path: Path | None = None,
) -> str:
    run_id = str(uuid4())
    if ingress is IngressKind.IMPORTED and imported_source_path is None:
        imported_source_path = runtime._path.parent / f"{run_id}.wav"
    result = RunRepository(runtime).insert_active(
        _run(run_id, ingress, imported_source_path)
    )
    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    return run_id


def _make_wav(path: Path, *, frames: int = 8) -> bytes:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(8_000)
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.writeframes(b"\x01\x00" * frames)
    return path.read_bytes()


def _persist_audio(
    runtime: RunPersistenceDatabase,
    tmp_path: Path,
    run_id: str,
    *,
    name: str,
):
    run = RunRepository(runtime).get(run_id)
    source = (
        Path(run.imported_source_path)
        if run.ingress_kind == "IMPORTED"
        else tmp_path / f"{name}-source.wav"
    )
    final = tmp_path / f"{name}-final.wav"
    _make_wav(source)
    result = AudioPersistenceService(
        AudioPublication(),
        AudioRepository(runtime),
        RunRepository(runtime),
    ).persist(
        run_id,
        source,
        final,
        8_000,
        1,
        2,
        NOW,
        save_job_id=str(uuid4()),
        source_kind=(
            "IMPORTED_SOURCE"
            if run.ingress_kind == "IMPORTED"
            else "CAPTURED_AUDIO"
        ),
        source_identity=(
            run.imported_source_identity
            if run.ingress_kind == "IMPORTED"
            else f"capture:{run_id}"
        ),
        content_revision=1,
        expected_frame_count=8,
        source_frame_count=8,
    )
    assert result.initial_attempt_permitted
    assert result.fact is not None
    return result.fact


def _counts(runtime: RunPersistenceDatabase) -> tuple[int, int, int]:
    with runtime.operation() as database:
        return tuple(
            database.execute_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("run", "audio", "analysis_attempt")
        )


def _attempt_insert(
    run_id: str,
    audio_id: str,
    queued_at: str,
    kind: AttemptKind = AttemptKind.INITIAL,
) -> AttemptInsert:
    return AttemptInsert(
        attempt_id=str(uuid4()),
        run_id=run_id,
        audio_id=audio_id,
        attempt_kind=kind,
        queued_at=queued_at,
    )


def test_acceptance_persists_exact_audio_channel_map_once(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = str(uuid4())
    service = RunAcceptanceService(
        RunRepository(runtime),
        freeze_captured=lambda assigned, _request: _run(
            assigned,
            IngressKind.CAPTURED,
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda assigned: assigned == run_id,
        cancel=lambda _assigned: None,
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())
    record = RunRepository(runtime).get(run_id)
    payload = json.loads(record.config_snapshot_json)

    assert result.accepted
    assert payload["channel_mapping"] == {
        "schema": "audio-channel-map/v1",
        "channel_order": [0],
    }
    assert record.config_snapshot_json.count('"audio-channel-map/v1"') == 1
    with runtime.operation() as database:
        assert database.execute_sql(
            """
            SELECT COUNT(*)
            FROM pragma_table_xinfo('run')
            WHERE name LIKE '%channel%'
            """
        ).fetchone()[0] == 0


def test_01_captured_validation_reserve_active_commit_then_permission(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)
    events: list[str] = []
    run_id = str(uuid4())
    service = RunAcceptanceService(
        RunRepository(runtime),
        freeze_captured=lambda assigned, _request: (
            events.append("validated") or _run(assigned, IngressKind.CAPTURED)
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda assigned: events.append("reserved") or assigned == run_id,
        cancel=lambda _assigned: events.append("canceled"),
        close_admission=lambda _reason: events.append("closed"),
    )

    result = service.accept_captured(run_id, object())
    events.append("permission")

    assert events == ["validated", "reserved", "permission"]
    assert result.accepted and result.permission_granted
    assert RunRepository(runtime).get(run_id).state == "ACTIVE"


def test_02_active_insert_rollback_cancels_reservation_once_and_denies_capture(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = str(uuid4())
    canceled: list[str] = []
    service = RunAcceptanceService(
        RunRepository(runtime),
        freeze_captured=lambda assigned, _request: _run(
            assigned, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _assigned: True,
        cancel=canceled.append,
        close_admission=lambda _reason: None,
    )
    assert service.accept_captured(run_id, object()).accepted

    duplicate = service.accept_captured(run_id, object())

    assert not duplicate.accepted
    assert not duplicate.permission_granted
    assert duplicate.reason.startswith("UNIQUE constraint failed:")
    assert canceled == [run_id]
    assert _counts(runtime) == (1, 0, 0)


def test_03_imported_active_commit_then_real_wav_publish_and_audio_commit(
    prepared_database_path,
    tmp_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = str(uuid4())
    source = tmp_path / "import-source.wav"
    final = tmp_path / "managed" / "import-final.wav"
    final.parent.mkdir()
    source_bytes = _make_wav(source)
    service = RunAcceptanceService(
        RunRepository(runtime),
        freeze_captured=lambda *_args: None,
        freeze_imported=lambda assigned, _request: _run(
            assigned, IngressKind.IMPORTED, source
        ),
        reserve=lambda _assigned: (_ for _ in ()).throw(
            AssertionError("import must not reserve capture")
        ),
        cancel=lambda _assigned: None,
        close_admission=lambda _reason: None,
    )
    accepted = service.accept_imported(run_id, object())

    persisted = AudioPersistenceService(
        AudioPublication(),
        AudioRepository(runtime),
        RunRepository(runtime),
    ).persist(
        run_id,
        source,
        final,
        8_000,
        1,
        2,
        NOW,
        save_job_id=str(uuid4()),
        source_kind="IMPORTED_SOURCE",
        source_identity=f"imported:{run_id}",
        content_revision=1,
        expected_frame_count=8,
        source_frame_count=8,
    )

    assert accepted.accepted and accepted.permission_granted
    assert persisted.initial_attempt_permitted
    assert final.read_bytes() == source_bytes
    assert persisted.fact.final_path == str(final.resolve())
    assert AudioRepository(runtime).get_for_run(run_id).audio_id == (
        persisted.fact.audio_id
    )
    collision = AudioPublication().publish(source, final, 8_000, 1, 2)
    assert collision.code.value == "AUDIO_PATH_COLLISION"


def test_04_audio_commit_then_initial_queue_commit_then_scheduler_submit(
    prepared_database_path,
    tmp_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(runtime)
    audio = _persist_audio(runtime, tmp_path, run_id, name="initial")
    attempts = AttemptRepository(runtime)
    attempt = _attempt_insert(run_id, audio.audio_id, LATER)
    submitted: list[str] = []
    observed_states: list[str] = []

    queued = attempts.create_queued(attempt)
    submitted.append(attempt.attempt_id)
    observed_states.append(attempts.latest_for_run(run_id).state)

    assert queued.state is TransactionState.COMMITTED
    assert queued.value is True
    assert submitted == [attempt.attempt_id]
    assert observed_states == ["QUEUED"]
    assert attempts.latest_for_run(run_id).state == "QUEUED"


def test_05_worker_running_commit_precedes_analysis_and_terminal_commit(
    prepared_database_path,
    tmp_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(runtime)
    audio = _persist_audio(runtime, tmp_path, run_id, name="worker")
    attempts = AttemptRepository(runtime)
    attempt = _attempt_insert(run_id, audio.audio_id, NOW)
    assert attempts.create_queued(attempt).value is True
    assert attempts.mark_running(attempt.attempt_id, LATER).value
    analysis_observed_state = attempts.latest_for_run(run_id).state
    outcome = AttemptOutcome(
        state=AttemptState.SUCCEEDED,
        judgment=Judgment.PASS,
        result_payload_json='{"judgment":"PASS"}',
        code=None,
        summary=None,
    )

    finished = attempts.finish_running_and_project(
        attempt.attempt_id, outcome, LATEST
    )

    assert analysis_observed_state == "RUNNING"
    assert finished.value is True
    assert attempts.latest_for_run(run_id).state == "SUCCEEDED"
    run = RunRepository(runtime).get(run_id)
    assert (run.state, run.judgment, run.terminal_code) == (
        "SUCCEEDED",
        "PASS",
        "ANALYSIS_SUCCEEDED",
    )


def test_06_competing_run_terminal_callbacks_are_first_wins(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(runtime)
    repository = RunRepository(runtime)
    barrier = threading.Barrier(2)
    results = []

    def finish(code: str, at: str) -> None:
        barrier.wait()
        results.append(
            (
                code,
                repository.finish_active(
                    run_id,
                    RunTerminal(
                        state=RunState.FAILED,
                        code=code,
                        summary=code,
                        terminal_at=at,
                    ),
                ).value,
            )
        )

    threads = [
        threading.Thread(target=finish, args=("FILE_FAILED", LATER)),
        threading.Thread(target=finish, args=("ANALYSIS_FAILED", LATEST)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(value for _, value in results) == [False, True]
    winner = next(code for code, value in results if value)
    run = repository.get(run_id)
    assert run.terminal_code == winner
    assert run.terminal_summary == winner


def test_07_queue_full_terminalizes_imported_initial_but_not_reanalysis_run(
    prepared_database_path,
    tmp_path,
):
    runtime = _ready_runtime(prepared_database_path)
    initial_run = _insert_run(runtime)
    initial_audio = _persist_audio(
        runtime, tmp_path, initial_run, name="queue-full-initial"
    )
    attempts = AttemptRepository(runtime)
    initial = _attempt_insert(initial_run, initial_audio.audio_id, LATER)
    attempts.create_queued(initial)
    initial_result = attempts.finish_queued_and_project(
        initial.attempt_id,
        AttemptOutcome(
            state=AttemptState.FAILED,
            judgment=None,
            result_payload_json=None,
            code="ANALYSIS_QUEUE_FULL",
            summary="scheduler rejected analysis submission",
        ),
        LATEST,
    )
    assert initial_result.value is True
    assert RunRepository(runtime).get(initial_run).terminal_code == (
        "ANALYSIS_QUEUE_FULL"
    )

    reanalysis_run = _insert_run(runtime)
    _persist_audio(
        runtime, tmp_path, reanalysis_run, name="queue-full-reanalysis"
    )
    runs = RunRepository(runtime)
    assert runs.finish_active(
        reanalysis_run,
        RunTerminal(
            state=RunState.SUCCEEDED,
            code="ORIGINAL",
            summary="original",
            terminal_at=NOW,
            judgment=Judgment.PASS,
        ),
    ).value
    before = runs.get(reanalysis_run)
    reanalysis_audio = AudioRepository(runtime).get_for_run(reanalysis_run)
    reanalysis = _attempt_insert(
        reanalysis_run,
        reanalysis_audio.audio_id,
        LATER,
        AttemptKind.REANALYSIS,
    )
    attempts.create_queued(reanalysis)
    reanalysis_result = attempts.finish_queued_and_project(
        reanalysis.attempt_id,
        AttemptOutcome(
            state=AttemptState.FAILED,
            judgment=None,
            result_payload_json=None,
            code="ANALYSIS_QUEUE_FULL",
            summary="scheduler rejected analysis submission",
        ),
        LATEST,
    )

    assert reanalysis_result.value is True
    assert runs.get(reanalysis_run) == before
    assert attempts.latest_for_run(reanalysis_run).error_code == (
        "ANALYSIS_QUEUE_FULL"
    )


def test_08_pending_manual_stop_and_worker_start_race_has_one_winner(
    prepared_database_path,
    tmp_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(runtime)
    audio = _persist_audio(runtime, tmp_path, run_id, name="cancel-race")
    attempts = AttemptRepository(runtime)
    attempt = _attempt_insert(run_id, audio.audio_id, NOW)
    attempts.create_queued(attempt)
    attempt_id = attempt.attempt_id
    barrier = threading.Barrier(2)
    results = []

    def start() -> None:
        barrier.wait()
        results.append(("start", attempts.mark_running(attempt_id, LATER).value))

    def stop() -> None:
        barrier.wait()
        results.append(
            (
                "stop",
                attempts.finish_queued_and_project(
                    attempt_id,
                    AttemptOutcome(
                        state=AttemptState.CANCELED,
                        judgment=None,
                        result_payload_json=None,
                        code="USER_CANCELED",
                        summary="Canceled before analysis started",
                    ),
                    LATER,
                ).value,
            )
        )

    threads = [threading.Thread(target=start), threading.Thread(target=stop)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(bool(value) for _, value in results) == 1
    attempt = attempts.latest_for_run(run_id)
    assert attempt.state in {"RUNNING", "CANCELED"}
    assert RunRepository(runtime).get(run_id).state == (
        "ACTIVE" if attempt.state == "RUNNING" else "CANCELED"
    )


def test_09_restart_settlement_interrupts_without_external_calls(
    prepared_database_path,
    tmp_path,
):
    old_runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(old_runtime)
    audio = _persist_audio(
        old_runtime, tmp_path, run_id, name="restart-settlement"
    )
    AttemptRepository(old_runtime).create_queued(
        _attempt_insert(run_id, audio.audio_id, NOW)
    )
    external_calls = {
        "submit": 0,
        "ui": 0,
        "plc": 0,
        "resume": 0,
    }

    restarted = RunPersistenceDatabase(prepared_database_path)
    result = settle_startup(restarted, LATER)

    assert result.ready
    assert RunRepository(restarted).get(run_id).state == "INTERRUPTED"
    assert AttemptRepository(restarted).latest_for_run(run_id).state == (
        "INTERRUPTED"
    )
    assert external_calls == {"submit": 0, "ui": 0, "plc": 0, "resume": 0}


def test_10_rollback_reports_exact_reason_once_without_retry_or_repair(
    prepared_database_path,
    tmp_path,
    monkeypatch,
):
    runtime = _ready_runtime(prepared_database_path)
    run_id = _insert_run(runtime)
    audio = _persist_audio(runtime, tmp_path, run_id, name="rollback")
    attempts = AttemptRepository(runtime)
    first = _attempt_insert(run_id, audio.audio_id, NOW)
    assert attempts.create_queued(first).state is TransactionState.COMMITTED
    opens = 0
    original_open = database_module._open_existing_database

    def tracked_open(path):
        nonlocal opens
        opens += 1
        return original_open(path)

    monkeypatch.setattr(database_module, "_open_existing_database", tracked_open)

    duplicate = attempts.create_queued(
        _attempt_insert(run_id, audio.audio_id, LATER)
    )

    assert duplicate.state is TransactionState.ROLLED_BACK
    assert duplicate.reason.startswith(
        "ATTEMPT_QUEUE_DB_FAILED: UNIQUE constraint failed:"
    )
    assert opens == 1
    assert len(attempts.list_for_run(run_id)) == 1


def test_11_unknown_closes_admission_and_requests_restart(
    prepared_database_path,
    monkeypatch,
):
    runtime = _ready_runtime(prepared_database_path)
    close_requests: list[str] = []
    run_id = str(uuid4())
    repository = RunRepository(runtime)
    real_short_transaction = run_repository_module.run_short_transaction
    calls = 0

    def unknown_once(database, work):
        nonlocal calls
        calls += 1
        if calls == 1:
            return TransactionResult(
                state=TransactionState.UNKNOWN,
                reason="commit outcome indeterminate",
                restart_required=True,
            )
        return real_short_transaction(database, work)

    monkeypatch.setattr(
        run_repository_module, "run_short_transaction", unknown_once
    )
    service = RunAcceptanceService(
        repository,
        freeze_captured=lambda assigned, _request: _run(
            assigned, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _assigned: True,
        cancel=lambda _assigned: None,
        close_admission=close_requests.append,
    )

    unknown = service.accept_captured(run_id, object())
    later = service.accept_captured(run_id, object())

    assert unknown.restart_required and not unknown.permission_granted
    assert later.restart_required and not later.permission_granted
    assert close_requests == ["commit outcome indeterminate"]
    assert runtime.closed
    assert _counts_via_sqlite(prepared_database_path) == (0, 0, 0)


def _counts_via_sqlite(path: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("run", "audio", "analysis_attempt")
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("missing-source", "AUDIO_WRITE_FAILED"),
        ("collision", "AUDIO_PATH_COLLISION"),
        ("size", "AUDIO_FINAL_SIZE_MISMATCH"),
    ],
)
def test_12_file_failure_first_wins_and_creates_no_audio_or_attempt(
    prepared_database_path,
    tmp_path,
    monkeypatch,
    failure,
    expected_code,
):
    runtime = _ready_runtime(prepared_database_path)
    source = tmp_path / f"{failure}-source.wav"
    final = tmp_path / f"{failure}-final.wav"
    run_id = _insert_run(runtime, imported_source_path=source)
    if failure != "missing-source":
        _make_wav(source)
    if failure == "collision":
        final.write_bytes(b"unrelated-winner")
    publication = AudioPublication()
    if failure == "size":
        monkeypatch.setattr(
            publication, "_final_size", lambda _path: 999_999
        )
    service = AudioPersistenceService(
        publication,
        AudioRepository(runtime),
        RunRepository(runtime),
    )

    failed = service.persist(
        run_id,
        source,
        final,
        8_000,
        1,
        2,
        LATER,
        save_job_id=str(uuid4()),
        source_kind="IMPORTED_SOURCE",
        source_identity=f"imported:{run_id}",
        content_revision=1,
        expected_frame_count=8,
        source_frame_count=8,
    )
    later = RunRepository(runtime).finish_active(
        run_id,
        RunTerminal(
            state=RunState.CANCELED,
            code="LATE_CALLBACK",
            summary="late",
            terminal_at=LATEST,
        ),
    )

    assert failed.code == expected_code
    assert not failed.initial_attempt_permitted
    assert later.value is True
    assert RunRepository(runtime).get(run_id).terminal_code == "LATE_CALLBACK"
    assert _counts(runtime) == (1, 0, 0)


def test_13_safe_close_follows_last_entered_transaction(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)
    entered = threading.Event()
    release = threading.Event()
    shutdown_started = threading.Event()
    shutdown_finished = threading.Event()
    ordering = []

    def transaction() -> None:
        with runtime.operation() as database:
            def work(_database):
                ordering.append("transaction-entered")
                entered.set()
                assert release.wait(5)
                ordering.append("transaction-finishing")

            assert run_short_transaction(
                database, work
            ).state is TransactionState.COMMITTED
        ordering.append("transaction-exited")

    def shutdown() -> None:
        shutdown_started.set()
        runtime.close_after_upstream_stopped()
        ordering.append("database-closed")
        shutdown_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        transaction_future = executor.submit(transaction)
        assert entered.wait(5)
        shutdown_future = executor.submit(shutdown)
        assert shutdown_started.wait(5)
        assert not shutdown_finished.is_set()
        release.set()
        transaction_future.result(timeout=5)
        shutdown_future.result(timeout=5)

    assert ordering == [
        "transaction-entered",
        "transaction-finishing",
        "transaction-exited",
        "database-closed",
    ]
    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED


def test_14_mutex_serializes_callers_with_distinct_short_connections(
    prepared_database_path,
    monkeypatch,
):
    runtime = _ready_runtime(prepared_database_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    records = []
    active = 0
    maximum_active = 0
    record_lock = threading.Lock()
    original_open = database_module._open_existing_database

    def tracked_open(path):
        connection = original_open(path)
        records.append((threading.get_ident(), connection))
        return connection

    monkeypatch.setattr(database_module, "_open_existing_database", tracked_open)

    def use(first: bool) -> None:
        nonlocal active, maximum_active
        if not first:
            second_started.set()
        with runtime.operation():
            with record_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            if first:
                first_entered.set()
                assert release_first.wait(5)
            with record_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(use, True)
        assert first_entered.wait(5)
        second = executor.submit(use, False)
        assert second_started.wait(5)
        assert len(records) == 1
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert len(records) == 2
    assert records[0][0] != records[1][0]
    assert records[0][1] is not records[1][1]
    assert all(connection.is_closed() for _, connection in records)
    assert maximum_active == 1


def test_15_unrelated_legacy_sqlite_and_wav_assets_remain_byte_exact(
    tmp_path,
):
    managed = tmp_path / "managed-run-persistence"
    legacy = tmp_path / "unrelated-legacy-assets"
    managed.mkdir()
    legacy.mkdir()
    legacy_database = legacy / "jingchen.db"
    with sqlite3.connect(legacy_database) as connection:
        connection.execute("CREATE TABLE legacy_fact (value TEXT)")
        connection.execute("INSERT INTO legacy_fact VALUES ('preserve')")
    legacy_wav = legacy / "legacy-capture.wav"
    _make_wav(legacy_wav, frames=11)
    identities = {
        path: (
            str(path.resolve()),
            path.name,
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in (legacy_database, legacy_wav)
    }
    target = prepare_run_persistence_database(managed / "runs.sqlite3")
    runtime = _ready_runtime(target)
    source = managed / "source.wav"
    _make_wav(source)
    run_id = _insert_run(runtime, imported_source_path=source)
    AudioPersistenceService(
        AudioPublication(),
        AudioRepository(runtime),
        RunRepository(runtime),
    ).persist(
        run_id,
        source,
        managed / "final.wav",
        8_000,
        1,
        2,
        NOW,
        save_job_id=str(uuid4()),
        source_kind="IMPORTED_SOURCE",
        source_identity=f"imported:{run_id}",
        content_revision=1,
        expected_frame_count=8,
        source_frame_count=8,
    )

    for path, before in identities.items():
        assert (
            str(path.resolve()),
            path.name,
            path.read_bytes(),
            path.stat().st_mtime_ns,
        ) == before
