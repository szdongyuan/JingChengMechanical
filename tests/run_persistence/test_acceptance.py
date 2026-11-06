from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from uuid import uuid4

import pytest
from peewee import SqliteDatabase

from app.service.run_persistence.acceptance import (
    AcceptanceInputError,
    RunAcceptanceService,
)
from app.service.run_persistence.contracts import (
    ContractValidationError,
    IngressKind,
    RunInsert,
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
from app.service.run_persistence.run_repository import RunRepository
from tests.run_persistence.conftest import make_analysis_envelope


def _ready_repository(path):
    runtime = RunPersistenceDatabase(path)
    assert runtime.open_and_validate_startup().ok
    with runtime._startup_settlement_operation():
        pass
    runtime._finish_startup(TransactionResult(state=TransactionState.COMMITTED))
    return runtime, RunRepository(runtime)


def _run_for(run_id, now, ingress, imported_source_path=None):
    return RunInsert(
        run_id=run_id,
        ingress_kind=ingress,
        trigger_source=(
            TriggerSource.MANUAL
            if ingress is IngressKind.CAPTURED
            else TriggerSource.IMPORT
        ),
        config_id=1,
        hardware_id=2 if ingress is IngressKind.CAPTURED else None,
        imported_source_identity=(
            f"sha256:{run_id}" if ingress is IngressKind.IMPORTED else None
        ),
        imported_source_path=(
            str(Path(imported_source_path).resolve(strict=False))
            if ingress is IngressKind.IMPORTED
            and imported_source_path is not None
            else (
                r"C:\managed\acceptance-import.wav"
                if ingress is IngressKind.IMPORTED
                else None
            )
        ),
        imported_expected_frame_count=(
            1 if ingress is IngressKind.IMPORTED else None
        ),
        snapshots=freeze_run_inputs(
            make_analysis_envelope(config={"config": 1}),
            {"hardware": 2} if ingress is IngressKind.CAPTURED else None,
        ),
        accepted_at=now,
    )


def _counts(path) -> tuple[int, int, int]:
    database = SqliteDatabase(path)
    database.connect()
    try:
        return tuple(
            database.execute_sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("run", "audio", "analysis_attempt")
        )
    finally:
        database.close()


def test_close_admission_callback_is_mandatory():
    with pytest.raises(TypeError, match="close_admission"):
        RunAcceptanceService(
            object(),
            freeze_captured=lambda *_args: None,
            freeze_imported=lambda *_args: None,
            reserve=lambda _run_id: True,
            cancel=lambda _run_id: None,
        )


def test_acceptance_has_no_internal_id_factory_compatibility() -> None:
    with pytest.raises(TypeError, match="id_factory"):
        RunAcceptanceService(
            object(),
            freeze_captured=lambda *_args: None,
            freeze_imported=lambda *_args: None,
            reserve=lambda _run_id: True,
            cancel=lambda _run_id: None,
            close_admission=lambda _reason: None,
            id_factory=lambda: str(uuid4()),
        )


@pytest.mark.parametrize("method_name", ["accept_captured", "accept_imported"])
def test_caller_run_id_is_validated_before_admission_lock(method_name: str) -> None:
    service = RunAcceptanceService(
        object(),
        freeze_captured=lambda *_args: None,
        freeze_imported=lambda *_args: None,
        reserve=lambda _run_id: True,
        cancel=lambda _run_id: None,
        close_admission=lambda _reason: None,
    )

    class LockMustNotBeEntered:
        def __enter__(self):
            raise AssertionError("admission lock entered before UUID validation")

        def __exit__(self, *_args):
            return None

    service._admission_lock = LockMustNotBeEntered()

    with pytest.raises(ContractValidationError, match="UUIDv4"):
        getattr(service, method_name)("not-canonical", object())


def test_captured_uses_exact_caller_run_id_for_every_boundary(now: str) -> None:
    run_id = str(uuid4())
    request = object()
    seen: list[tuple[str, object]] = []

    class Repository:
        def insert_active(self, frozen_run):
            seen.append(("insert", frozen_run.run_id))
            return TransactionResult(
                state=TransactionState.COMMITTED,
                value=True,
            )

    def freeze_captured(assigned, received):
        seen.append(("freeze", (assigned, received)))
        return _run_for(assigned, now, IngressKind.CAPTURED)

    service = RunAcceptanceService(
        Repository(),
        freeze_captured=freeze_captured,
        freeze_imported=lambda *_args: None,
        reserve=lambda assigned: seen.append(("reserve", assigned)) or True,
        cancel=lambda assigned: seen.append(("cancel", assigned)),
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, request)

    assert result.run_id == run_id
    assert result.accepted
    assert seen == [
        ("freeze", (run_id, request)),
        ("reserve", run_id),
        ("insert", run_id),
    ]


def test_captured_validation_failure_never_reserves_or_writes(
    prepared_database_path,
):
    run_id = str(uuid4())
    _runtime, repository = _ready_repository(prepared_database_path)
    reserved = []

    def reject(_run_id, _request):
        raise AcceptanceInputError("invalid captured input")

    service = RunAcceptanceService(
        repository,
        freeze_captured=reject,
        freeze_imported=reject,
        reserve=lambda run_id: reserved.append(run_id) or True,
        cancel=lambda _run_id: None,
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())

    assert not result.accepted
    assert not result.permission_granted
    assert result.reason == "invalid captured input"
    assert reserved == []
    assert _counts(prepared_database_path) == (0, 0, 0)


def test_captured_reservation_rejection_creates_no_run(
    prepared_database_path, now
):
    run_id = str(uuid4())
    _runtime, repository = _ready_repository(prepared_database_path)
    canceled = []
    service = RunAcceptanceService(
        repository,
        freeze_captured=lambda run_id, _request: _run_for(
            run_id, now, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _run_id: False,
        cancel=canceled.append,
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())

    assert not result.accepted
    assert not result.permission_granted
    assert canceled == []
    assert _counts(prepared_database_path) == (0, 0, 0)


def test_captured_rollback_cancels_once_and_never_grants_capture_permission(now):
    run_id = str(uuid4())
    class Repository:
        def insert_active(self, _run):
            return TransactionResult(
                state=TransactionState.ROLLED_BACK,
                reason="insert failed",
            )

    canceled = []
    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda run_id, _request: _run_for(
            run_id, now, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _run_id: True,
        cancel=canceled.append,
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())

    assert not result.accepted
    assert not result.permission_granted
    assert result.reason == "insert failed"
    assert canceled == [result.run_id]


@pytest.mark.parametrize(
    "code",
    [
        DatabaseLifecycleCode.DATABASE_CLOSED,
        DatabaseLifecycleCode.DATABASE_NOT_READY,
    ],
)
def test_captured_database_unavailable_after_reserve_cancels_once(now, code):
    run_id = str(uuid4())
    class Repository:
        def insert_active(self, _run):
            raise DatabaseUnavailableError(code, "database admission unavailable")

    canceled = []
    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda run_id, _request: _run_for(
            run_id, now, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _run_id: True,
        cancel=canceled.append,
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())

    assert not result.accepted
    assert not result.permission_granted
    assert (
        result.reason
        == f"{code.value}: database admission unavailable"
    )
    assert canceled == [result.run_id]


def test_captured_committed_false_cancels_once(now):
    run_id = str(uuid4())
    class Repository:
        def insert_active(self, _run):
            return TransactionResult(
                state=TransactionState.COMMITTED,
                value=False,
                reason="ACTIVE row was not inserted",
            )

    canceled = []
    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda run_id, _request: _run_for(
            run_id, now, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _run_id: True,
        cancel=canceled.append,
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())

    assert not result.accepted
    assert not result.permission_granted
    assert result.reason == "ACTIVE row was not inserted"
    assert canceled == [result.run_id]


def test_captured_commit_grants_permission_only_after_insert_commit(now):
    run_id = str(uuid4())
    sequence = []

    class Repository:
        def insert_active(self, _run):
            sequence.append("commit")
            return TransactionResult(
                state=TransactionState.COMMITTED, value=True
            )

    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda run_id, _request: _run_for(
            run_id, now, IngressKind.CAPTURED
        ),
        freeze_imported=lambda *_args: None,
        reserve=lambda _run_id: sequence.append("reserve") or True,
        cancel=lambda _run_id: sequence.append("cancel"),
        close_admission=lambda _reason: None,
    )

    result = service.accept_captured(run_id, object())
    if result.permission_granted:
        sequence.append("capture-start-permitted")

    assert result.accepted
    assert result.permission_granted
    assert sequence == ["reserve", "commit", "capture-start-permitted"]


def test_unknown_closes_admission_and_requires_restart(now):
    first_run_id = str(uuid4())
    callback_run_id = str(uuid4())
    later_captured_run_id = str(uuid4())
    later_imported_run_id = str(uuid4())
    calls = []

    def freeze_captured(run_id, _request):
        calls.append("freeze_captured")
        return _run_for(run_id, now, IngressKind.CAPTURED)

    def freeze_imported(run_id, _request):
        calls.append("freeze_imported")
        return _run_for(run_id, now, IngressKind.IMPORTED)

    class Repository:
        def insert_active(self, _run):
            calls.append("insert")
            return TransactionResult(
                state=TransactionState.UNKNOWN,
                reason="outcome unknown",
                restart_required=True,
            )

    closed = []
    callback_results = []
    service_holder = []

    def close_admission(reason):
        closed.append(reason)
        callback_results.append(
            service_holder[0].accept_imported(callback_run_id, object())
        )

    service = RunAcceptanceService(
        Repository(),
        freeze_captured=freeze_captured,
        freeze_imported=freeze_imported,
        reserve=lambda _run_id: calls.append("reserve") or True,
        cancel=lambda _run_id: None,
        close_admission=close_admission,
    )
    service_holder.append(service)

    result = service.accept_captured(first_run_id, object())
    calls_after_unknown = list(calls)
    later_captured = service.accept_captured(later_captured_run_id, object())
    later_imported = service.accept_imported(later_imported_run_id, object())

    assert not result.accepted
    assert not result.permission_granted
    assert result.restart_required
    assert result.run_id == first_run_id
    assert closed == ["outcome unknown"]
    assert callback_results[0].restart_required
    assert callback_results[0].run_id == callback_run_id
    assert not callback_results[0].permission_granted
    assert not later_captured.accepted
    assert not later_captured.permission_granted
    assert later_captured.restart_required
    assert later_captured.run_id == later_captured_run_id
    assert not later_imported.accepted
    assert not later_imported.permission_granted
    assert later_imported.restart_required
    assert later_imported.run_id == later_imported_run_id
    assert calls == calls_after_unknown
    assert closed == ["outcome unknown"]


def test_unknown_blocks_concurrent_later_admission_before_validation(now):
    first_run_id = str(uuid4())
    later_run_id = str(uuid4())
    first_inserted = threading.Event()
    release_unknown = threading.Event()
    later_actor_started = threading.Event()
    imported_frozen = threading.Event()

    class Repository:
        def insert_active(self, _run):
            first_inserted.set()
            assert release_unknown.wait(5)
            return TransactionResult(
                state=TransactionState.UNKNOWN,
                reason="outcome unknown",
                restart_required=True,
            )

    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda run_id, _request: _run_for(
            run_id, now, IngressKind.CAPTURED
        ),
        freeze_imported=lambda run_id, _request: (
            imported_frozen.set()
            or _run_for(run_id, now, IngressKind.IMPORTED)
        ),
        reserve=lambda _run_id: True,
        cancel=lambda _run_id: None,
        close_admission=lambda _reason: None,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.accept_captured, first_run_id, object())
        assert first_inserted.wait(5)

        def accept_later_imported():
            later_actor_started.set()
            return service.accept_imported(later_run_id, object())

        later = executor.submit(accept_later_imported)
        assert later_actor_started.wait(5)
        assert not imported_frozen.wait(0.1)
        release_unknown.set()
        first_result = first.result(timeout=5)
        later_result = later.result(timeout=5)

    assert first_result.restart_required
    assert later_result.restart_required
    assert not imported_frozen.is_set()


@pytest.mark.parametrize(
    "state", [TransactionState.COMMITTED, TransactionState.ROLLED_BACK]
)
def test_imported_never_reserves_or_cancels_and_preserves_source(
    tmp_path, now, state
):
    run_id = str(uuid4())
    source = tmp_path / "source.wav"
    source.write_bytes(b"source bytes")
    original = source.read_bytes()
    calls = []

    class Repository:
        def insert_active(self, frozen_run):
            assert frozen_run.run_id == run_id
            calls.append("insert")
            return TransactionResult(
                state=state,
                value=True if state is TransactionState.COMMITTED else None,
                reason=(
                    None
                    if state is TransactionState.COMMITTED
                    else "insert failed"
                ),
            )

    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda *_args: None,
        freeze_imported=lambda run_id, request: _run_for(
            run_id, now, IngressKind.IMPORTED, request
        ),
        reserve=lambda _run_id: calls.append("reserve") or True,
        cancel=lambda _run_id: calls.append("cancel"),
        close_admission=lambda _reason: None,
    )

    result = service.accept_imported(run_id, source)

    assert result.permission_granted is (
        state is TransactionState.COMMITTED
    )
    assert result.run_id == run_id
    assert calls == ["insert"]
    assert source.exists()
    assert source.read_bytes() == original


def test_imported_validation_failure_never_inserts_or_grants_copy_permission():
    run_id = str(uuid4())
    calls = []

    class Repository:
        def insert_active(self, _run):
            calls.append("insert")

    service = RunAcceptanceService(
        Repository(),
        freeze_captured=lambda *_args: None,
        freeze_imported=lambda _run_id, _request: (
            _ for _ in ()
        ).throw(AcceptanceInputError("unsupported WAV")),
        reserve=lambda _run_id: calls.append("reserve"),
        cancel=lambda _run_id: calls.append("cancel"),
        close_admission=lambda _reason: None,
    )

    result = service.accept_imported(run_id, Path("missing.wav"))

    assert not result.accepted
    assert not result.permission_granted
    assert calls == []
