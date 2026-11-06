from __future__ import annotations

import json
from pathlib import Path
import threading
from uuid import uuid4

import pytest

import app.service.run_persistence.attempt_repository as attempt_repository_module
from app.service.run_persistence.attempt_repository import (
    AttemptOutcome,
    AttemptRepository,
)
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.contracts import (
    AttemptInsert,
    AttemptKind,
    AttemptState,
    ContractValidationError,
    IngressKind,
    Judgment,
    RunInsert,
    RunState,
    TransactionResult,
    TransactionState,
    TriggerSource,
    freeze_run_inputs,
)
from app.service.run_persistence.run_repository import RunRepository, RunTerminal
from tests.run_persistence.test_audio_repository import (
    fact,
    ready_runtime,
)
from tests.run_persistence.conftest import make_analysis_envelope


LATER = "2026-07-27T12:35:00.000000Z"
LATEST = "2026-07-27T12:36:00.000000Z"


def attempt_insert(
    run_id: str,
    audio_id: str,
    now: str,
    *,
    kind: AttemptKind = AttemptKind.INITIAL,
    attempt_id: str | None = None,
) -> AttemptInsert:
    return AttemptInsert(
        attempt_id=str(uuid4()) if attempt_id is None else attempt_id,
        run_id=run_id,
        audio_id=audio_id,
        attempt_kind=kind,
        queued_at=now,
    )


def seed_audio(runtime, tmp_path: Path, now: str):
    run_id = str(uuid4())
    inserted = RunRepository(runtime).insert_active(
        RunInsert(
            run_id=run_id,
            ingress_kind=IngressKind.CAPTURED,
            trigger_source=TriggerSource.MANUAL,
            config_id=1,
            hardware_id=2,
            snapshots=freeze_run_inputs(
                make_analysis_envelope(
                    config={"config": 1, "fault_types": []}
                )
            ),
            accepted_at=now,
        )
    )
    assert inserted.value is True
    final = tmp_path / f"{run_id}.wav"
    final.write_bytes(b"completed")
    audio_fact = fact(run_id, final, now)
    assert AudioRepository(runtime).insert_verified(audio_fact).value
    return run_id, audio_fact


def succeeded(judgment: Judgment) -> AttemptOutcome:
    return AttemptOutcome(
        state=AttemptState.SUCCEEDED,
        judgment=judgment,
        result_payload_json=json.dumps({"judgment": judgment.value}),
        code=None,
        summary="analysis completed",
    )


def failed(state=AttemptState.FAILED, code="ANALYSIS_FAILED"):
    return AttemptOutcome(
        state=state,
        judgment=None,
        result_payload_json=None,
        code=code,
        summary="analysis did not succeed",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"judgment": "PASS"},
        [1, "two", False, None],
        "scalar",
        42,
        2.5,
        True,
        None,
    ],
)
def test_success_outcome_accepts_every_standard_json_top_level_value(payload):
    outcome = AttemptOutcome(
        state=AttemptState.SUCCEEDED,
        judgment=Judgment.PASS,
        result_payload_json=json.dumps(payload),
        code=None,
        summary=None,
    )

    assert json.loads(outcome.result_payload_json) == payload


@pytest.mark.parametrize(
    "payload_json", ["{", "NaN", "Infinity", "-Infinity", "[NaN]"]
)
def test_success_outcome_rejects_invalid_or_non_standard_json_before_db(
    payload_json,
):
    with pytest.raises(ContractValidationError, match="valid JSON"):
        AttemptOutcome(
            state=AttemptState.SUCCEEDED,
            judgment=Judgment.PASS,
            result_payload_json=payload_json,
            code=None,
            summary=None,
        )


def test_create_queued_uses_exact_prevalidated_insert_without_allocating_id(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(run_id, audio.audio_id, now)

    monkeypatch.setattr(
        attempt_repository_module,
        "new_uuid4",
        lambda: pytest.fail("repository must not allocate an attempt ID"),
        raising=False,
    )
    queued = repository.create_queued(attempt)
    stored = repository.latest_for_run(run_id)

    assert queued.state is TransactionState.COMMITTED
    assert queued.value is True
    assert stored.attempt_id == attempt.attempt_id
    assert stored.run_id == attempt.run_id
    assert stored.audio_id == attempt.audio_id
    assert stored.attempt_kind == attempt.attempt_kind.value
    assert stored.queued_at == attempt.queued_at
    assert stored.updated_at == attempt.queued_at


def test_create_queued_rejects_non_exact_attempt_insert_before_db_operation(
    prepared_database_path, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    repository = AttemptRepository(runtime)
    operations = 0

    def forbidden_operation():
        nonlocal operations
        operations += 1
        raise AssertionError("invalid input must be rejected before DB access")

    monkeypatch.setattr(runtime, "operation", forbidden_operation)

    with pytest.raises(TypeError, match="AttemptInsert"):
        repository.create_queued(object())

    assert operations == 0


def test_create_queued_revalidates_exact_attempt_insert_before_db_operation(
    prepared_database_path, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    repository = AttemptRepository(runtime)
    forged = object.__new__(AttemptInsert)
    object.__setattr__(forged, "attempt_id", str(uuid4()))
    object.__setattr__(forged, "run_id", str(uuid4()))
    object.__setattr__(forged, "audio_id", str(uuid4()))
    object.__setattr__(forged, "attempt_kind", AttemptKind.INITIAL)
    object.__setattr__(forged, "queued_at", LATER)
    object.__setattr__(forged, "state", AttemptState.RUNNING)
    operations = 0

    def forbidden_operation():
        nonlocal operations
        operations += 1
        raise AssertionError("invalid input must be rejected before DB access")

    monkeypatch.setattr(runtime, "operation", forbidden_operation)

    with pytest.raises(ContractValidationError, match="state must be QUEUED"):
        repository.create_queued(forged)

    assert operations == 0


def test_create_queued_unknown_quarantines_once_without_retry(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    calls = 0

    def unknown_once(_database, _work):
        nonlocal calls
        calls += 1
        return TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="queue commit outcome indeterminate",
            restart_required=True,
        )

    monkeypatch.setattr(
        attempt_repository_module, "run_short_transaction", unknown_once
    )

    result = repository.create_queued(
        attempt_insert(run_id, audio.audio_id, now)
    )

    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
    assert calls == 1
    assert runtime.closed


def test_queue_rollback_exact_code_only_one_initial_and_wrong_relation(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    first_run, first_audio = seed_audio(runtime, tmp_path, now)
    second_run, _ = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    first = attempt_insert(first_run, first_audio.audio_id, now)
    assert repository.create_queued(first).state is TransactionState.COMMITTED

    duplicate = repository.create_queued(
        attempt_insert(first_run, first_audio.audio_id, LATER)
    )
    wrong_relation = repository.create_queued(
        attempt_insert(
            second_run,
            first_audio.audio_id,
            now,
            kind=AttemptKind.REANALYSIS,
        )
    )

    assert duplicate.state is TransactionState.ROLLED_BACK
    assert duplicate.reason.startswith("ATTEMPT_QUEUE_DB_FAILED:")
    assert wrong_relation.state is TransactionState.ROLLED_BACK
    assert wrong_relation.reason.startswith("ATTEMPT_QUEUE_DB_FAILED:")
    assert len(repository.list_for_run(first_run)) == 1
    assert repository.list_for_run(second_run) == ()


def test_reanalysis_uses_caller_preallocated_ids_and_latest_order(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    RunRepository(runtime).finish_active(
        run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="NO_RESULT",
            summary=None,
            terminal_at=LATER,
        ),
    )
    repository = AttemptRepository(runtime)
    first_attempt = attempt_insert(
        run_id, audio.audio_id, now, kind=AttemptKind.REANALYSIS
    )
    second_attempt = attempt_insert(
        run_id, audio.audio_id, LATEST, kind=AttemptKind.REANALYSIS
    )

    first = repository.create_queued(first_attempt)
    second = repository.create_queued(second_attempt)

    assert first.value is True
    assert second.value is True
    assert first_attempt.attempt_id != second_attempt.attempt_id
    assert repository.latest_for_run(run_id).attempt_id == second_attempt.attempt_id
    assert [row.attempt_kind for row in repository.list_for_run(run_id)] == [
        "REANALYSIS",
        "REANALYSIS",
    ]


def test_running_rowcount_must_commit_before_worker_and_terminal_requires_running(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    queued = repository.create_queued(attempt)
    worker_calls = []

    premature = repository.finish_running_and_project(
        attempt.attempt_id, failed(), LATER
    )
    running = repository.mark_running(attempt.attempt_id, LATER)
    if running.state is TransactionState.COMMITTED and running.value:
        worker_calls.append("executed")
    repeated = repository.mark_running(attempt.attempt_id, LATEST)

    assert premature.value is False
    assert running.value is True
    assert repeated.value is False
    assert worker_calls == ["executed"]


def test_queued_and_running_terminal_writers_require_their_exact_source_state(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    queued_winner = attempt_insert(run_id, audio.audio_id, now)
    running_winner = attempt_insert(
        run_id,
        audio.audio_id,
        LATER,
        kind=AttemptKind.REANALYSIS,
    )
    repository.create_queued(queued_winner)
    repository.create_queued(running_winner)
    assert repository.mark_running(running_winner.attempt_id, LATER).value is True

    queued_result = repository.finish_queued_and_project(
        queued_winner.attempt_id,
        failed(AttemptState.FAILED, "ANALYSIS_QUEUE_FULL"),
        LATEST,
    )
    wrong_running = repository.finish_running_and_project(
        queued_winner.attempt_id,
        failed(),
        LATEST,
    )
    wrong_queued = repository.finish_queued_and_project(
        running_winner.attempt_id,
        failed(AttemptState.CANCELED, "USER_CANCELED"),
        LATEST,
    )
    running_result = repository.finish_running_and_project(
        running_winner.attempt_id,
        failed(),
        LATEST,
    )

    assert queued_result.value is True
    assert wrong_running.value is False
    assert wrong_queued.value is False
    assert running_result.value is True


@pytest.mark.parametrize("state", [AttemptState.SUCCEEDED, AttemptState.NO_RESULT])
def test_queued_terminal_rejects_running_only_outcomes_before_db(
    prepared_database_path, monkeypatch, state
):
    runtime = ready_runtime(prepared_database_path)
    repository = AttemptRepository(runtime)
    outcome = (
        succeeded(Judgment.PASS)
        if state is AttemptState.SUCCEEDED
        else failed(AttemptState.NO_RESULT, "ANALYSIS_NO_RESULT")
    )
    operations = 0

    def forbidden_operation():
        nonlocal operations
        operations += 1
        raise AssertionError("invalid queued outcome must not access DB")

    monkeypatch.setattr(runtime, "operation", forbidden_operation)

    with pytest.raises(ContractValidationError, match="queued terminal"):
        repository.finish_queued_and_project(str(uuid4()), outcome, LATER)

    assert operations == 0


@pytest.mark.parametrize(
    ("outcome", "run_state", "code"),
    [
        (succeeded(Judgment.PASS), "SUCCEEDED", "ANALYSIS_SUCCEEDED"),
        (failed(AttemptState.NO_RESULT, "NO_CONFIDENT_RESULT"), "NO_RESULT", "NO_CONFIDENT_RESULT"),
        (failed(), "FAILED", "ANALYSIS_FAILED"),
        (failed(AttemptState.CANCELED, "USER_CANCELED"), "CANCELED", "USER_CANCELED"),
        (
            failed(AttemptState.INTERRUPTED, "PROCESS_RESTARTED"),
            "INTERRUPTED",
            "PROCESS_RESTARTED",
        ),
    ],
)
def test_initial_terminal_and_run_first_wins_project_in_one_transaction(
    prepared_database_path, tmp_path, now, outcome, run_state, code
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    attempt_insert_value = attempt_insert(run_id, audio.audio_id, now)
    assert repository.create_queued(attempt_insert_value).value is True
    assert repository.mark_running(attempt_insert_value.attempt_id, LATER).value

    result = repository.finish_running_and_project(
        attempt_insert_value.attempt_id, outcome, LATEST
    )
    attempt = repository.latest_for_run(run_id)
    run = RunRepository(runtime).get(run_id)

    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    assert attempt.state == outcome.state.value
    assert attempt.terminal_at == LATEST
    assert attempt.result_payload_json == outcome.result_payload_json
    assert run.state == run_state
    assert run.terminal_code == code
    assert run.terminal_at == LATEST
    if outcome.state is AttemptState.SUCCEEDED:
        assert run.judgment == "PASS"
        assert run.judgment_source == "AUTOMATIC"


@pytest.mark.parametrize(
    "outcome",
    [
        failed(AttemptState.NO_RESULT, "NO_CONFIDENT_RESULT"),
        failed(AttemptState.FAILED, "ANALYSIS_FAILED"),
        failed(AttemptState.CANCELED, "USER_CANCELED"),
        failed(AttemptState.INTERRUPTED, "PROCESS_RESTARTED"),
    ],
)
def test_non_succeeded_reanalysis_preserves_entire_run_projection(
    prepared_database_path, tmp_path, now, outcome
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    runs = RunRepository(runtime)
    runs.finish_active(
        run_id,
        RunTerminal(
            state=RunState.SUCCEEDED,
            code="ORIGINAL",
            summary="original",
            terminal_at=LATER,
            judgment=Judgment.PASS,
        ),
    )
    before = runs.get(run_id)
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(
        run_id, audio.audio_id, now, kind=AttemptKind.REANALYSIS
    )
    repository.create_queued(attempt)
    repository.mark_running(attempt.attempt_id, LATER)

    repository.finish_running_and_project(attempt.attempt_id, outcome, LATEST)
    after = runs.get(run_id)

    assert after == before
    assert repository.latest_for_run(run_id).state == outcome.state.value


def test_concurrent_successful_reanalysis_last_committer_sets_current_judgment(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    runs = RunRepository(runtime)
    runs.finish_active(
        run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="ORIGINAL",
            summary=None,
            terminal_at=now,
        ),
    )
    repository = AttemptRepository(runtime)
    first_insert = attempt_insert(
        run_id, audio.audio_id, now, kind=AttemptKind.REANALYSIS
    )
    second_insert = attempt_insert(
        run_id, audio.audio_id, LATER, kind=AttemptKind.REANALYSIS
    )
    repository.create_queued(first_insert)
    repository.create_queued(second_insert)
    first = first_insert.attempt_id
    second = second_insert.attempt_id
    repository.mark_running(first, LATER)
    repository.mark_running(second, LATER)
    first_committed = threading.Event()

    def finish_first():
        repository.finish_running_and_project(
            first, succeeded(Judgment.PASS), LATER
        )
        first_committed.set()

    def finish_second():
        first_committed.wait()
        repository.finish_running_and_project(
            second, succeeded(Judgment.FAIL), LATEST
        )

    threads = [threading.Thread(target=finish_first), threading.Thread(target=finish_second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [row.state for row in repository.list_for_run(run_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    run = runs.get(run_id)
    assert run.judgment == "FAIL"
    assert run.judgment_source == "AUTOMATIC"
    assert run.judged_at == LATEST
    assert run.state == "NO_RESULT"
    assert run.terminal_at == now


def test_successful_reanalysis_updates_only_current_judgment_projection(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    runs = RunRepository(runtime)
    assert runs.finish_active(
        run_id,
        RunTerminal(
            state=RunState.FAILED,
            code="ORIGINAL_TERMINAL",
            summary="original summary",
            terminal_at=now,
        ),
    ).value
    assert runs.set_manual_judgment(run_id, Judgment.FAIL, LATER).value
    before = runs.get(run_id)
    assert before.fault_type is None
    assert before.manual_judgment_revision == 1
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(
        run_id, audio.audio_id, LATER, kind=AttemptKind.REANALYSIS
    )
    repository.create_queued(attempt)
    repository.mark_running(attempt.attempt_id, LATER)
    outcome = succeeded(Judgment.PASS)

    result = repository.finish_running_and_project(
        attempt.attempt_id, outcome, LATEST
    )
    after = runs.get(run_id)
    stored_attempt = repository.latest_for_run(run_id)

    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    assert after.judgment == "PASS"
    assert after.judgment_source == "AUTOMATIC"
    assert after.judged_at == LATEST
    assert after.fault_type is None
    assert after.manual_judgment_revision is None
    assert after.updated_at == LATEST
    for field in (
        "run_id",
        "ingress_kind",
        "trigger_source",
        "config_id",
        "hardware_id",
        "config_snapshot_json",
        "hardware_snapshot_json",
        "calibration_snapshot_json",
        "state",
        "terminal_code",
        "terminal_summary",
        "accepted_at",
        "terminal_at",
    ):
        assert getattr(after, field) == getattr(before, field)
    assert stored_attempt.result_payload_json == outcome.result_payload_json


def test_successful_reanalysis_rolls_back_attempt_if_run_is_not_terminal(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(
        run_id, audio.audio_id, now, kind=AttemptKind.REANALYSIS
    )
    repository.create_queued(attempt)
    repository.mark_running(attempt.attempt_id, LATER)

    result = repository.finish_running_and_project(
        attempt.attempt_id, succeeded(Judgment.PASS), LATEST
    )

    assert result.state is TransactionState.ROLLED_BACK
    assert "reanalysis run projection" in result.reason
    assert repository.latest_for_run(run_id).state == "RUNNING"
    run = RunRepository(runtime).get(run_id)
    assert run.state == "ACTIVE"
    assert run.judgment is None


def test_terminal_unknown_quarantines_once_without_retry(
    prepared_database_path, tmp_path, now, monkeypatch
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    repository.create_queued(attempt)
    calls = 0

    def unknown_once(_database, _work):
        nonlocal calls
        calls += 1
        return TransactionResult(
            state=TransactionState.UNKNOWN,
            reason="terminal commit outcome indeterminate",
            restart_required=True,
        )

    monkeypatch.setattr(
        attempt_repository_module, "run_short_transaction", unknown_once
    )

    result = repository.finish_queued_and_project(
        attempt.attempt_id,
        failed(AttemptState.CANCELED, "USER_CANCELED"),
        LATER,
    )

    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
    assert calls == 1
    assert runtime.closed


@pytest.mark.parametrize(
    "reason",
    ["ANALYSIS_QUEUE_FULL", "ANALYSIS_SCHEDULER_UNAVAILABLE"],
)
def test_queued_failure_projects_initial_but_preserves_reanalysis(
    prepared_database_path, tmp_path, now, reason
):
    runtime = ready_runtime(prepared_database_path)
    initial_run, initial_audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    initial = attempt_insert(initial_run, initial_audio.audio_id, now)
    repository.create_queued(initial)
    queued_failure = failed(AttemptState.FAILED, reason)
    initial_result = repository.finish_queued_and_project(
        initial.attempt_id, queued_failure, LATER
    )
    assert initial_result.value is True
    assert RunRepository(runtime).get(initial_run).terminal_code == reason
    assert repository.latest_for_run(initial_run).error_code == reason

    re_run, re_audio = seed_audio(runtime, tmp_path, now)
    runs = RunRepository(runtime)
    runs.finish_active(
        re_run,
        RunTerminal(
            state=RunState.SUCCEEDED,
            code="ORIGINAL",
            summary="original",
            terminal_at=now,
            judgment=Judgment.PASS,
        ),
    )
    before = runs.get(re_run)
    reanalysis = attempt_insert(
        re_run, re_audio.audio_id, now, kind=AttemptKind.REANALYSIS
    )
    repository.create_queued(reanalysis)
    re_result = repository.finish_queued_and_project(
        reanalysis.attempt_id, queued_failure, LATER
    )

    assert re_result.value is True
    assert runs.get(re_run) == before
    assert repository.latest_for_run(re_run).error_code == reason

    imported_path = tmp_path / "imported.wav"
    imported_identity = "sha256:attempt-repository-import"
    imported_run = RunInsert(
        run_id=str(uuid4()),
        ingress_kind=IngressKind.IMPORTED,
        trigger_source=TriggerSource.IMPORT,
        config_id=3,
        hardware_id=None,
        imported_source_identity=imported_identity,
        imported_source_path=str(imported_path.resolve(strict=False)),
        imported_expected_frame_count=8,
        snapshots=freeze_run_inputs(
            make_analysis_envelope(config={"import": True})
        ),
        accepted_at=now,
    )
    assert runs.insert_active(imported_run).value is True
    imported_path.write_bytes(b"completed")
    imported_audio = fact(
        imported_run.run_id,
        imported_path,
        now,
        source_kind="IMPORTED_SOURCE",
        source_identity=imported_identity,
        content_revision=1,
    )
    assert AudioRepository(runtime).insert_verified(imported_audio).value is True
    imported = attempt_insert(
        imported_run.run_id,
        imported_audio.audio_id,
        now,
    )
    repository.create_queued(imported)
    assert repository.finish_queued_and_project(
        imported.attempt_id, queued_failure, LATER
    ).value is True
    assert runs.get(imported_run.run_id).terminal_code == reason


def test_queued_cancel_races_running_and_projects_only_initial(
    prepared_database_path, tmp_path, now
):
    runtime = ready_runtime(prepared_database_path)
    run_id, audio = seed_audio(runtime, tmp_path, now)
    repository = AttemptRepository(runtime)
    attempt = attempt_insert(run_id, audio.audio_id, now)
    repository.create_queued(attempt)
    attempt_id = attempt.attempt_id
    barrier = threading.Barrier(2)
    results = []

    def start():
        barrier.wait()
        results.append(("running", repository.mark_running(attempt_id, LATER).value))

    def cancel():
        barrier.wait()
        results.append(
            (
                "cancel",
                repository.finish_queued_and_project(
                    attempt_id,
                    failed(AttemptState.CANCELED, "USER_CANCELED"),
                    LATER,
                ).value,
            )
        )

    threads = [threading.Thread(target=start), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(bool(result) for _, result in results) == 1
    state = repository.latest_for_run(run_id).state
    assert state in ("RUNNING", "CANCELED")
    if state == "CANCELED":
        assert RunRepository(runtime).get(run_id).state == "CANCELED"
    else:
        assert RunRepository(runtime).get(run_id).state == "ACTIVE"
    assert not hasattr(repository, "release_slot")
