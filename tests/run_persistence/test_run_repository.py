from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import ntpath
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from app.service.run_persistence import database as database_module
from app.service.multi_run_coordinator_mvp.contracts import ABSENT
from app.service.run_persistence.contracts import (
    ContractValidationError,
    IngressKind,
    Judgment,
    RunInsert,
    RunState,
    TransactionState,
    TriggerSource,
    TransactionResult,
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
from app.service.run_persistence.production_run_audio_adapter import (
    normalize_absolute_path,
)
from tests.run_persistence.conftest import make_analysis_envelope


@pytest.fixture
def repository(prepared_database_path):
    runtime = RunPersistenceDatabase(prepared_database_path)
    assert runtime.open_and_validate_startup().ok
    with runtime._startup_settlement_operation():
        pass
    runtime._finish_startup(TransactionResult(state=TransactionState.COMMITTED))
    return RunRepository(runtime)


def make_run(
    now,
    ingress=IngressKind.CAPTURED,
    *,
    fault_types=(),
    channel_order=(0,),
):
    return RunInsert(
        run_id=str(uuid4()),
        ingress_kind=ingress,
        trigger_source=(
            TriggerSource.MANUAL
            if ingress is IngressKind.CAPTURED
            else TriggerSource.IMPORT
        ),
        config_id=7,
        hardware_id=9 if ingress is IngressKind.CAPTURED else None,
        snapshots=freeze_run_inputs(
            make_analysis_envelope(
                config={
                    "name": "config",
                    "fault_types": list(fault_types),
                },
                channel_mapping={
                    "schema": "audio-channel-map/v1",
                    "channel_order": list(channel_order),
                },
            ),
            {"serial": "hardware"} if ingress is IngressKind.CAPTURED else None,
            {"revision": 3},
        ),
        accepted_at=now,
        imported_source_identity=(
            "sha256:imported-test"
            if ingress is IngressKind.IMPORTED
            else None
        ),
        imported_source_path=(
            "C:/managed/imported-test.wav"
            if ingress is IngressKind.IMPORTED
            else None
        ),
        imported_expected_frame_count=(
            48_000 if ingress is IngressKind.IMPORTED else None
        ),
    )


def _current_trace(*samples):
    from app.service.run_persistence.contracts import CurrentTrace

    return CurrentTrace(samples=tuple(samples))


def test_imported_source_path_is_canonicalized_before_durable_active_insert(
    repository,
    now,
):
    imported = make_run(now, IngressKind.IMPORTED)
    imported = RunInsert(
        run_id=imported.run_id,
        ingress_kind=imported.ingress_kind,
        trigger_source=imported.trigger_source,
        config_id=imported.config_id,
        hardware_id=imported.hardware_id,
        snapshots=imported.snapshots,
        accepted_at=imported.accepted_at,
        imported_source_identity=imported.imported_source_identity,
        imported_source_path="C:/managed/folder/../source.wav",
        imported_expected_frame_count=imported.imported_expected_frame_count,
    )
    canonical = ntpath.normpath("C:/managed/folder/../source.wav")

    assert repository.insert_active(imported).value is True

    assert imported.imported_source_path == canonical
    assert repository.get(imported.run_id).imported_source_path == canonical


def test_imported_source_path_uses_production_canonical_existing_path_spelling(
    repository,
    now,
):
    existing_path = Path(__file__).resolve()
    mixed_case_alias = str(existing_path).upper()
    assert Path(mixed_case_alias).exists()
    canonical = normalize_absolute_path(mixed_case_alias)
    assert mixed_case_alias != canonical
    imported = make_run(now, IngressKind.IMPORTED)
    imported = RunInsert(
        run_id=imported.run_id,
        ingress_kind=imported.ingress_kind,
        trigger_source=imported.trigger_source,
        config_id=imported.config_id,
        hardware_id=imported.hardware_id,
        snapshots=imported.snapshots,
        accepted_at=imported.accepted_at,
        imported_source_identity=imported.imported_source_identity,
        imported_source_path=mixed_case_alias,
        imported_expected_frame_count=imported.imported_expected_frame_count,
    )

    assert repository.insert_active(imported).value is True

    assert imported.imported_source_path == canonical
    assert repository.get(imported.run_id).imported_source_path == canonical


def test_attach_current_trace_round_trips_exact_typed_value(repository, now):
    frozen_run = make_run(now)
    assert repository.insert_active(frozen_run).value is True
    trace = _current_trace((0.0, 1.25), (0.02, 1.5))

    result = repository.attach_current_trace(frozen_run.run_id, trace)
    record = repository.get(frozen_run.run_id)

    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    assert record.current_trace == trace
    with repository._runtime.operation() as database:
        stored = database.execute_sql(
            "SELECT current_trace_json FROM run WHERE run_id = ?",
            (frozen_run.run_id,),
        ).fetchone()[0]
    assert stored == (
        '{"samples":[[0.0,1.25],[0.02,1.5]],'
        '"schema":"run-current-trace/v1","unit":"A"}'
    )


def test_attach_current_trace_requires_exact_active_null_current_row_in_one_transaction(
    repository,
    now,
    monkeypatch,
):
    active = make_run(now)
    terminal = make_run(now)
    assert repository.insert_active(active).value is True
    assert repository.insert_active(terminal).value is True
    assert repository.finish_active(
        terminal.run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="NO_RESULT",
            summary=None,
            terminal_at="2026-07-27T12:35:00.000000Z",
        ),
    ).value is True
    trace = _current_trace((0.0, 1.0))

    statements: list[str] = []
    original_open = database_module._open_existing_database

    def recording_open(path):
        database = original_open(path)
        original_execute = database.execute_sql

        def recording_execute(sql, params=None):
            statements.append(" ".join(sql.split()).upper())
            return original_execute(sql, params)

        database.execute_sql = recording_execute
        return database

    monkeypatch.setattr(
        database_module,
        "_open_existing_database",
        recording_open,
    )

    first = repository.attach_current_trace(active.run_id, trace)
    already_attached = repository.attach_current_trace(active.run_id, trace)
    non_active = repository.attach_current_trace(terminal.run_id, trace)
    missing = repository.attach_current_trace(str(uuid4()), trace)

    assert first.state is TransactionState.COMMITTED
    assert first.value is True
    assert already_attached.state is not TransactionState.COMMITTED
    assert non_active.state is not TransactionState.COMMITTED
    assert missing.state is not TransactionState.COMMITTED
    mutations = [
        statement
        for statement in statements
        if statement.startswith(("UPDATE ", "INSERT ", "DELETE "))
    ]
    assert len(mutations) == 4
    assert all(statement.startswith("UPDATE RUN SET CURRENT_TRACE_JSON =") for statement in mutations)
    assert not any(statement.startswith("SELECT ") for statement in statements)


def test_concurrent_attach_current_trace_allows_exactly_one_commit_without_retry(
    repository,
    now,
    monkeypatch,
):
    frozen_run = make_run(now)
    assert repository.insert_active(frozen_run).value is True
    barrier = threading.Barrier(2)
    open_count = 0
    open_count_lock = threading.Lock()
    original_open = database_module._open_existing_database

    def counted_open(path):
        nonlocal open_count
        with open_count_lock:
            open_count += 1
        return original_open(path)

    monkeypatch.setattr(
        database_module,
        "_open_existing_database",
        counted_open,
    )

    def attach(trace):
        barrier.wait(timeout=5)
        return repository.attach_current_trace(frozen_run.run_id, trace)

    traces = (
        _current_trace((0.0, 1.0)),
        _current_trace((0.0, 2.0)),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(attach, traces))

    assert Counter(result.state for result in results) == Counter(
        {
            TransactionState.COMMITTED: 1,
            TransactionState.ROLLED_BACK: 1,
        }
    )
    assert open_count == 2
    record = repository.get(frozen_run.run_id)
    assert record.current_trace in traces
    assert sum(record.current_trace == trace for trace in traces) == 1


def test_manual_ng_fault_and_revision_update_is_atomic(repository, now):
    frozen_run = make_run(now, fault_types=("异响", "漏装"))
    assert repository.insert_active(frozen_run).value is True
    assert repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="NO_RESULT",
            summary=None,
            terminal_at="2026-07-27T12:35:00.000000Z",
        ),
    ).value is True
    judged_at = "2026-07-27T12:36:00.000000Z"

    result = repository.set_manual_judgment(
        frozen_run.run_id,
        Judgment.FAIL,
        judged_at,
        expected_revision=7,
        fault_type="异响",
        accepted_fault_types=("异响", "漏装"),
    )
    record = repository.get(frozen_run.run_id)

    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    assert (
        record.judgment,
        record.judgment_source,
        record.judged_at,
        record.fault_type,
        record.manual_judgment_revision,
        record.updated_at,
    ) == ("FAIL", "MANUAL", judged_at, "异响", 8, judged_at)
    before = record
    stale = repository.set_manual_judgment(
        frozen_run.run_id,
        Judgment.FAIL,
        "2026-07-27T12:37:00.000000Z",
        expected_revision=7,
        fault_type="漏装",
        accepted_fault_types=("异响", "漏装"),
    )
    assert stale.state is not TransactionState.COMMITTED
    assert repository.get(frozen_run.run_id) == before
    rejected = repository.set_manual_judgment(
        frozen_run.run_id,
        Judgment.FAIL,
        "2026-07-27T12:37:00.000000Z",
        expected_revision=8,
        fault_type="未配置",
        accepted_fault_types=("异响", "漏装"),
    )
    assert rejected.state is not TransactionState.COMMITTED
    assert repository.get(frozen_run.run_id) == before


def test_manual_fault_membership_requires_exact_durable_frozen_list_in_transaction(
    repository,
    now,
    monkeypatch,
):
    frozen_run = make_run(now, fault_types=("异响", "漏装"))
    assert repository.insert_active(frozen_run).value is True
    assert repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="NO_RESULT",
            summary=None,
            terminal_at="2026-07-27T12:35:00.000000Z",
        ),
    ).value is True
    before = repository.get(frozen_run.run_id)
    open_count = 0
    original_open = database_module._open_existing_database

    def counted_open(path):
        nonlocal open_count
        open_count += 1
        return original_open(path)

    monkeypatch.setattr(
        database_module,
        "_open_existing_database",
        counted_open,
    )

    result = repository.set_manual_judgment(
        frozen_run.run_id,
        Judgment.FAIL,
        "2026-07-27T12:36:00.000000Z",
        expected_revision=7,
        fault_type="异响",
        accepted_fault_types=("异响", "未冻结"),
    )

    assert result.state is TransactionState.ROLLED_BACK
    assert open_count == 1
    assert repository.get(frozen_run.run_id) == before


@pytest.mark.parametrize(
    ("judgment", "fault_types", "fault_type", "expected_revision"),
    [
        (Judgment.PASS, ("异响",), None, 3),
        (Judgment.FAIL, (), None, 5),
    ],
)
def test_manual_pass_and_generic_fail_store_null_fault_with_positive_revision(
    repository,
    now,
    judgment,
    fault_types,
    fault_type,
    expected_revision,
):
    frozen_run = make_run(now, fault_types=fault_types)
    assert repository.insert_active(frozen_run).value is True
    assert repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="NO_RESULT",
            summary=None,
            terminal_at="2026-07-27T12:35:00.000000Z",
        ),
    ).value is True

    result = repository.set_manual_judgment(
        frozen_run.run_id,
        judgment,
        "2026-07-27T12:36:00.000000Z",
        expected_revision=expected_revision,
        fault_type=fault_type,
        accepted_fault_types=fault_types,
    )
    record = repository.get(frozen_run.run_id)

    assert result.state is TransactionState.COMMITTED
    assert record.fault_type is None
    assert record.manual_judgment_revision == expected_revision + 1


def test_durable_channel_order_round_trips_without_fallback(repository, now):
    frozen_run = make_run(now, channel_order=(2, 0))
    assert repository.insert_active(frozen_run).value is True

    record = repository.get(frozen_run.run_id)
    channel_mapping = json.loads(record.config_snapshot_json)["channel_mapping"]

    assert channel_mapping == {
        "schema": "audio-channel-map/v1",
        "channel_order": [2, 0],
    }
    with pytest.raises(ContractValidationError):
        make_run(now, channel_order=())
    with pytest.raises(ContractValidationError):
        make_run(now, channel_order=(0, 0))
    with pytest.raises(ContractValidationError):
        RunInsert(
            run_id=str(uuid4()),
            ingress_kind=IngressKind.CAPTURED,
            trigger_source=TriggerSource.MANUAL,
            config_id=7,
            hardware_id=9,
            snapshots=freeze_run_inputs(
                make_analysis_envelope(channel_mapping={"input": [0]}),
                {"serial": "hardware"},
                {"revision": 3},
            ),
            accepted_at=now,
        )


@pytest.mark.parametrize("ingress", [IngressKind.CAPTURED, IngressKind.IMPORTED])
def test_insert_and_get_active_captured_or_imported(repository, now, ingress):
    frozen_run = make_run(now, ingress)

    result = repository.insert_active(frozen_run)
    record = repository.get(frozen_run.run_id)

    assert result.state is TransactionState.COMMITTED
    assert result.value is True
    assert record is not None
    assert record.run_id == frozen_run.run_id
    assert record.ingress_kind == ingress.value
    assert record.state == RunState.ACTIVE.value
    assert record.config_snapshot_json == frozen_run.snapshots.config_snapshot_json
    assert record.hardware_snapshot_json == frozen_run.snapshots.hardware_snapshot_json
    assert record.calibration_snapshot_json == frozen_run.snapshots.calibration_snapshot_json


def test_first_terminal_wins_without_overwriting_accepted_fields(repository, now):
    frozen_run = make_run(now)
    assert repository.insert_active(frozen_run).value is True
    first_at = "2026-07-27T12:35:00.000000Z"
    later_at = "2026-07-27T12:36:00.000000Z"

    first = repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.FAILED,
            code="AUDIO_WRITE_FAILED",
            summary="writer failed",
            terminal_at=first_at,
        ),
    )
    later = repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.CANCELED,
            code="USER_CANCELED",
            summary="later outcome",
            terminal_at=later_at,
        ),
    )
    record = repository.get(frozen_run.run_id)

    assert first.state is TransactionState.COMMITTED
    assert first.value is True
    assert later.state is TransactionState.COMMITTED
    assert later.value is False
    assert record.state == RunState.FAILED.value
    assert record.terminal_code == "AUDIO_WRITE_FAILED"
    assert record.terminal_summary == "writer failed"
    assert record.terminal_at == first_at
    assert record.accepted_at == now
    assert record.config_snapshot_json == frozen_run.snapshots.config_snapshot_json


@pytest.mark.parametrize("judgment", [Judgment.PASS, Judgment.FAIL])
def test_successful_initial_terminal_sets_automatic_judgment(
    repository, now, judgment
):
    frozen_run = make_run(now)
    repository.insert_active(frozen_run)
    terminal_at = "2026-07-27T12:35:00.000000Z"

    result = repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.SUCCEEDED,
            code="ANALYSIS_SUCCEEDED",
            summary="analysis completed",
            terminal_at=terminal_at,
            judgment=judgment,
        ),
    )
    record = repository.get(frozen_run.run_id)

    assert result.value is True
    assert record.judgment == judgment.value
    assert record.judgment_source == "AUTOMATIC"
    assert record.judged_at == terminal_at


def test_manual_judgment_rejects_active_and_changes_only_judgment_projection(
    repository, now
):
    frozen_run = make_run(now)
    repository.insert_active(frozen_run)
    before = repository.get(frozen_run.run_id)

    rejected = repository.set_manual_judgment(
        frozen_run.run_id, Judgment.PASS, "2026-07-27T12:35:00.000000Z"
    )

    assert rejected.state is TransactionState.ROLLED_BACK
    assert rejected.value is None
    assert repository.get(frozen_run.run_id) == before

    terminal_at = "2026-07-27T12:36:00.000000Z"
    repository.finish_active(
        frozen_run.run_id,
        RunTerminal(
            state=RunState.NO_RESULT,
            code="NO_CONFIDENT_RESULT",
            summary="no result",
            terminal_at=terminal_at,
        ),
    )
    terminal_before = repository.get(frozen_run.run_id)
    judged_at = "2026-07-27T12:37:00.000000Z"

    updated = repository.set_manual_judgment(
        frozen_run.run_id, Judgment.FAIL, judged_at
    )
    after = repository.get(frozen_run.run_id)

    assert updated.value is True
    assert after.judgment == "FAIL"
    assert after.judgment_source == "MANUAL"
    assert after.judged_at == judged_at
    assert after.updated_at == judged_at
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
        assert getattr(after, field) == getattr(terminal_before, field)


def test_unknown_from_real_driver_state_quarantines_runtime_before_later_open(
    prepared_database_path, now, monkeypatch
):
    runtime = RunPersistenceDatabase(prepared_database_path)
    assert runtime.open_and_validate_startup().ok
    with runtime._startup_settlement_operation():
        pass
    runtime._finish_startup(TransactionResult(state=TransactionState.COMMITTED))
    repository = RunRepository(runtime)
    frozen_run = make_run(now)
    assert repository.insert_active(frozen_run).state is TransactionState.COMMITTED

    original_open = database_module._open_existing_database
    open_count = 0

    def open_with_no_op_rollback(path):
        nonlocal open_count
        open_count += 1
        database = original_open(path)
        database.rollback = lambda: None
        return database

    monkeypatch.setattr(
        database_module, "_open_existing_database", open_with_no_op_rollback
    )

    result = repository.insert_active(frozen_run)

    assert result.state is TransactionState.UNKNOWN
    assert result.restart_required is True
    assert runtime.closed
    assert open_count == 1

    with pytest.raises(DatabaseUnavailableError) as caught:
        repository.get(frozen_run.run_id)
    assert caught.value.code is DatabaseLifecycleCode.DATABASE_CLOSED
    assert open_count == 1


def test_history_revision_cas_maps_absent_to_zero_and_rejects_stale_writer(
    repository,
    now,
) -> None:
    frozen_run = make_run(now)
    assert repository.insert_active(frozen_run).value is True
    assert repository.get(frozen_run.run_id).causal_revision == 0

    first = repository.advance_causal_revision(
        frozen_run.run_id,
        expected_revision=ABSENT,
        proposed_revision=1,
    )
    stale_absent = repository.advance_causal_revision(
        frozen_run.run_id,
        expected_revision=ABSENT,
        proposed_revision=1,
    )
    second = repository.advance_causal_revision(
        frozen_run.run_id,
        expected_revision=1,
        proposed_revision=2,
    )

    assert first.state is TransactionState.COMMITTED
    assert first.value is True
    assert stale_absent.state is TransactionState.ROLLED_BACK
    assert second.state is TransactionState.COMMITTED
    assert second.value is True
    assert repository.get(frozen_run.run_id).causal_revision == 2
    with pytest.raises(ContractValidationError):
        repository.advance_causal_revision(
            frozen_run.run_id,
            expected_revision=2,
            proposed_revision=4,
        )
