from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

import app.service.run_persistence.database as database_module
from app.service.run_persistence.attempt_repository import (
    AttemptOutcome,
    AttemptRepository,
)
from app.service.run_persistence.audio_repository import AudioRepository
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
    _issue_verified_audio_fact,
    freeze_run_inputs,
)
from app.service.run_persistence.database import (
    DatabaseLifecycleCode,
    DatabaseUnavailableError,
    RunPersistenceDatabase,
)
from app.service.run_persistence.run_repository import (
    RunRepository,
    RunTerminal,
)
from tests.run_persistence.conftest import make_analysis_envelope


RUN_ID = "11111111-1111-4111-8111-111111111111"
AUDIO_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
NOW = "2026-07-27T12:34:56.123456Z"


def _ready_runtime(path: Path) -> RunPersistenceDatabase:
    runtime = RunPersistenceDatabase(path)
    assert runtime.open_and_validate_startup().ok
    with runtime._startup_settlement_operation():
        pass
    runtime._finish_startup(
        TransactionResult(state=TransactionState.COMMITTED)
    )
    assert runtime.ready
    return runtime


def test_shutdown_waits_for_entered_operation_then_closes_irreversibly(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)
    operation_entered = threading.Event()
    release_operation = threading.Event()
    shutdown_started = threading.Event()
    shutdown_finished = threading.Event()
    ordering: list[str] = []

    def entered_operation() -> None:
        with runtime.operation():
            ordering.append("operation-entered")
            operation_entered.set()
            assert release_operation.wait(5)
            ordering.append("operation-exiting")
        ordering.append("operation-exited")

    def shutdown() -> None:
        shutdown_started.set()
        runtime.close_after_upstream_stopped()
        ordering.append("shutdown-finished")
        shutdown_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation_future = executor.submit(entered_operation)
        assert operation_entered.wait(5)
        shutdown_future = executor.submit(shutdown)
        assert shutdown_started.wait(5)
        assert not shutdown_finished.is_set()
        release_operation.set()
        operation_future.result(timeout=5)
        shutdown_future.result(timeout=5)

    assert ordering == [
        "operation-entered",
        "operation-exiting",
        "operation-exited",
        "shutdown-finished",
    ]
    assert runtime.closed
    assert not runtime.ready
    with pytest.raises(DatabaseUnavailableError) as caught:
        with runtime.operation():
            pass
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED


def test_shutdown_closes_an_open_connection_owned_by_its_caller_thread(
    prepared_database_path,
):
    runtime = _ready_runtime(prepared_database_path)

    with runtime._operation_connection() as connection:
        assert not connection.is_closed()
        runtime.close_after_upstream_stopped()
        assert connection.is_closed()

    assert runtime.closed


def test_all_public_repository_operations_raise_exact_database_closed(
    prepared_database_path,
    tmp_path,
):
    runtime = _ready_runtime(prepared_database_path)
    run_repository = RunRepository(runtime)
    audio_repository = AudioRepository(runtime)
    attempt_repository = AttemptRepository(runtime)
    final_audio = tmp_path / "verified.wav"
    final_audio.write_bytes(b"x")
    run_insert = RunInsert(
        run_id=RUN_ID,
        ingress_kind=IngressKind.CAPTURED,
        trigger_source=TriggerSource.MANUAL,
        config_id=1,
        hardware_id=2,
        snapshots=freeze_run_inputs(
            make_analysis_envelope(config={"config": 1}),
            {"hardware": 2},
        ),
        accepted_at=NOW,
    )
    run_terminal = RunTerminal(
        state=RunState.SUCCEEDED,
        code="ANALYSIS_SUCCEEDED",
        summary=None,
        terminal_at=NOW,
        judgment=Judgment.PASS,
    )
    audio_fact = _issue_verified_audio_fact(
        audio_id=AUDIO_ID,
        save_job_id="66666666-6666-4666-8666-666666666666",
        run_id=RUN_ID,
        source_kind="CAPTURED_AUDIO",
        source_identity="managed-input:repository-shutdown",
        content_revision=1,
        final_path=str(final_audio),
        container_format="WAV_PCM",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=1,
        duration_ms=1,
        byte_size=1,
        closed_verified_at=NOW,
        created_at=NOW,
    )
    outcome = AttemptOutcome(
        state=AttemptState.FAILED,
        judgment=None,
        result_payload_json=None,
        code="ANALYSIS_FAILED",
        summary="failed",
    )
    attempt_insert = AttemptInsert(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        audio_id=AUDIO_ID,
        attempt_kind=AttemptKind.INITIAL,
        queued_at=NOW,
    )
    operations = [
        lambda: run_repository.insert_active(run_insert),
        lambda: run_repository.finish_active(RUN_ID, run_terminal),
        lambda: run_repository.set_manual_judgment(
            RUN_ID, Judgment.PASS, NOW
        ),
        lambda: run_repository.get(RUN_ID),
        lambda: audio_repository.insert_verified(audio_fact),
        lambda: audio_repository.get_for_run(RUN_ID),
        lambda: attempt_repository.create_queued(attempt_insert),
        lambda: attempt_repository.mark_running(ATTEMPT_ID, NOW),
        lambda: attempt_repository.finish_queued_and_project(
            ATTEMPT_ID, outcome, NOW
        ),
        lambda: attempt_repository.finish_running_and_project(
            ATTEMPT_ID, outcome, NOW
        ),
        lambda: attempt_repository.list_for_run(RUN_ID),
        lambda: attempt_repository.latest_for_run(RUN_ID),
    ]

    runtime.close_after_upstream_stopped()

    for operation in operations:
        with pytest.raises(DatabaseUnavailableError) as caught:
            operation()
        assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED
    startup = runtime.open_and_validate_startup()
    assert not startup.ok
    assert startup.code is DatabaseLifecycleCode.DATABASE_CLOSED


def test_shutdown_is_idempotent_has_no_reopen_and_performs_no_database_write(
    prepared_database_path,
    monkeypatch,
):
    runtime = _ready_runtime(prepared_database_path)
    before_bytes = prepared_database_path.read_bytes()
    before_mtime = prepared_database_path.stat().st_mtime_ns
    opens = 0

    def forbidden_open(_path):
        nonlocal opens
        opens += 1
        raise AssertionError("shutdown must not open a connection")

    monkeypatch.setattr(
        database_module, "_open_existing_database", forbidden_open
    )

    runtime.close_after_upstream_stopped()
    runtime.close_after_upstream_stopped()

    assert runtime.closed
    assert not runtime.ready
    assert opens == 0
    assert prepared_database_path.read_bytes() == before_bytes
    assert prepared_database_path.stat().st_mtime_ns == before_mtime
    assert not hasattr(runtime, "reopen")
    assert not hasattr(runtime, "open_after_shutdown")
