from __future__ import annotations

import inspect
import math
from threading import Event, Thread

import pytest

from app.service.analysis.scheduler import AnalysisCancelOrigin
from app.service.plc_result_sequencer import PlcMutationDisposition
from app.service.run_persistence import (
    AttemptState,
    BridgeAuthorityStatus,
    BridgeCancelResult,
    BridgeCancelStatus,
    BridgeSubmissionResult,
    BridgeSubmissionStatus,
    BridgeTerminalStatus,
)
from app.service.multi_run_coordinator_mvp import (
    AudioSaveOutcome,
    AudioSaveStatus,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CommitResult,
    CoordinatorLifecycle,
    CoordinatorShutdownStatus,
    HistoryFactKind,
    OperationStatus,
    ResidentCancelResult,
    ResidentCancelStatus,
    SaveCancelResult,
    SaveCancelStatus,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
    SaveSubmitStatus,
    StartStatus,
    StopStatus,
    TerminalConclusion,
    TerminalErrorCategory,
    TriggerSource,
)
from app.service.multi_run_coordinator_mvp.coordinator import (
    MultiRunCoordinator,
    _PublishingContext,
    _RunContext,
)

from ._support import (
    ATTEMPT_IDS,
    RUN_IDS,
    SAVE_JOB_IDS,
    Harness,
    captured_request,
    completed_capture,
    imported_request,
    saved_outcome,
)
from .test_bridge_initial import drive_to_bridge, terminal_report
from .test_result_operations import (
    db_failed_reanalysis_report,
    manual_request,
    reanalysis_report,
    reanalysis_request,
    result_facts,
)


def _shutdown_mutation_snapshot(coordinator):
    with coordinator._lock:
        return (
            coordinator._lifecycle,
            coordinator._shutdown_begun,
            coordinator._shutdown_begin_in_progress,
            coordinator._shutdown_report,
            coordinator._shutdown_finish_in_progress,
            coordinator._restart_required,
            tuple(coordinator._stop_errors),
            tuple(coordinator._restart_errors),
            tuple(coordinator._shutdown_errors),
            frozenset(coordinator._child_cancel_actions),
            frozenset(
                coordinator._child_cancel_action_durable.items()
            ),
            frozenset(coordinator._result_history_actions),
            coordinator._shutdown_blockers_locked(),
        )


def test_lifecycle_external_cancel_call_site_inventory_is_exhaustive() -> None:
    expected = {
        "self._resident.cancel(": {
            "_dispatch_shutdown_cancel",
            "_cancel_frozen_reservation",
        },
        "self._save_dispatcher.cancel(": {
            "_dispatch_shutdown_cancel",
            "_cancel_frozen_save_job",
        },
        "self._scheduler.cancel(": {
            "_dispatch_shutdown_cancel",
            "_request_bridge_reanalysis_cancel",
            "_cancel_frozen_scheduler_attempt",
        },
    }
    coordinator_source = inspect.getsource(MultiRunCoordinator)

    for call, owners in expected.items():
        assert coordinator_source.count(call) == len(owners)
        assert {
            owner
            for owner in owners
            if call in inspect.getsource(getattr(MultiRunCoordinator, owner))
        } == owners


def test_stop_all_idle_closes_admission_for_the_operation_then_reopens() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    report = coordinator.stop_all(0.0)

    assert report.status is StopStatus.SETTLED
    assert report.pending_analysis_attempt_ids == ()
    assert report.restart_required is False
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN


@pytest.mark.parametrize("value", [True, -1.0, math.nan, math.inf, -math.inf])
def test_stop_all_rejects_invalid_timeout_before_mutation(value) -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    with pytest.raises(ValueError):
        coordinator.stop_all(value)

    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN
    assert harness.resident.cancel_calls == []
    assert harness.save_dispatcher.cancel_calls == []
    assert harness.scheduler.cancel_requests == []


def test_begin_shutdown_is_permanent_immediate_and_idempotent() -> None:
    harness = Harness()
    coordinator = harness.coordinator()

    assert coordinator.begin_shutdown() is None
    assert coordinator.begin_shutdown() is None

    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED
    assert coordinator.start_imported(imported_request()).status is StartStatus.INGRESS_CLOSED
    assert harness.save_dispatcher.shutdown_calls == []
    assert harness.resident.cancel_calls == []
    assert harness.scheduler.cancel_requests == []


@pytest.mark.parametrize("value", [True, 1, math.nan, math.inf, -math.inf])
def test_finish_shutdown_rejects_non_exact_finite_float_before_mutation(value) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.begin_shutdown()
    before = _shutdown_mutation_snapshot(coordinator)
    child_calls = (
        tuple(harness.resident.cancel_calls),
        tuple(harness.save_dispatcher.cancel_calls),
        tuple(harness.scheduler.cancel_requests),
        tuple(harness.save_dispatcher.shutdown_calls),
    )
    diagnostics = tuple(harness.diagnostics.calls)
    monotonic_calls = harness.monotonic.calls

    with pytest.raises(ValueError):
        coordinator.finish_shutdown(value)

    assert _shutdown_mutation_snapshot(coordinator) == before
    assert (
        tuple(harness.resident.cancel_calls),
        tuple(harness.save_dispatcher.cancel_calls),
        tuple(harness.scheduler.cancel_requests),
        tuple(harness.save_dispatcher.shutdown_calls),
    ) == child_calls
    assert tuple(harness.diagnostics.calls) == diagnostics
    assert harness.monotonic.calls == monotonic_calls


def test_stop_all_initial_monotonic_failure_is_zero_mutation(
    monkeypatch,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    before = _shutdown_mutation_snapshot(coordinator)

    def failing_clock():
        raise RuntimeError("initial monotonic unavailable")

    monkeypatch.setattr(coordinator, "_monotonic_clock", failing_clock)

    with pytest.raises(RuntimeError, match="initial monotonic unavailable"):
        coordinator.stop_all(1.0)

    assert _shutdown_mutation_snapshot(coordinator) == before
    assert harness.resident.cancel_calls == []
    assert harness.save_dispatcher.cancel_calls == []
    assert harness.scheduler.cancel_requests == []
    assert harness.diagnostics.calls == []


def test_finish_shutdown_passes_absolute_deadline_unchanged_once_and_caches() -> None:
    harness = Harness()
    harness.monotonic.values.extend([10.0])
    coordinator = harness.coordinator()
    coordinator.begin_shutdown()

    report = coordinator.finish_shutdown(123.25)
    repeated = coordinator.finish_shutdown(999.0)

    assert report.status is CoordinatorShutdownStatus.CLOSED
    assert report.save_dispatcher_report.status is SaveDispatcherShutdownStatus.CLOSED
    assert report.restart_required is False
    assert repeated is report
    assert harness.save_dispatcher.shutdown_calls == [123.25]


def test_finish_shutdown_rejects_overlap_until_begin_fully_returns(
    monkeypatch,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    begin_worker = Thread(target=coordinator.begin_shutdown)
    begin_worker.start()
    assert cancel_entered.wait(2)
    wait_calls = []

    def forbidden_wait(timeout):
        wait_calls.append(timeout)
        raise AssertionError("overlap rejection must not wait")

    monkeypatch.setattr(coordinator._condition, "wait", forbidden_wait)
    before = _shutdown_mutation_snapshot(coordinator)
    child_calls = (
        tuple(harness.resident.cancel_calls),
        tuple(harness.save_dispatcher.cancel_calls),
        tuple(harness.scheduler.cancel_requests),
        tuple(harness.save_dispatcher.shutdown_calls),
    )
    diagnostics = tuple(harness.diagnostics.calls)

    try:
        with pytest.raises(
            RuntimeError,
            match=(
                "^finish_shutdown cannot overlap begin_shutdown "
                "or finish_shutdown$"
            ),
        ):
            coordinator.finish_shutdown(0.0)
        assert _shutdown_mutation_snapshot(coordinator) == before
        assert (
            tuple(harness.resident.cancel_calls),
            tuple(harness.save_dispatcher.cancel_calls),
            tuple(harness.scheduler.cancel_requests),
            tuple(harness.save_dispatcher.shutdown_calls),
        ) == child_calls
        assert tuple(harness.diagnostics.calls) == diagnostics
        assert wait_calls == []
    finally:
        cancel_release.set()
        begin_worker.join(2)
    assert not begin_worker.is_alive()


def test_finish_shutdown_rejects_concurrent_final_pass_without_mutation(
    monkeypatch,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.begin_shutdown()
    owner_entered = Event()
    owner_release = Event()
    original_shutdown = harness.save_dispatcher.shutdown

    def blocked_shutdown(deadline):
        owner_entered.set()
        assert owner_release.wait(2)
        return original_shutdown(deadline)

    monkeypatch.setattr(harness.save_dispatcher, "shutdown", blocked_shutdown)
    owner_reports = []
    owner = Thread(
        target=lambda: owner_reports.append(coordinator.finish_shutdown(10.0))
    )
    owner.start()
    assert owner_entered.wait(2)
    wait_calls = []

    def forbidden_wait(timeout):
        wait_calls.append(timeout)
        raise AssertionError("overlap rejection must not wait")

    monkeypatch.setattr(coordinator._condition, "wait", forbidden_wait)
    before = _shutdown_mutation_snapshot(coordinator)
    child_calls = (
        tuple(harness.resident.cancel_calls),
        tuple(harness.save_dispatcher.cancel_calls),
        tuple(harness.scheduler.cancel_requests),
        tuple(harness.save_dispatcher.shutdown_calls),
    )
    diagnostics = tuple(harness.diagnostics.calls)

    try:
        with pytest.raises(
            RuntimeError,
            match=(
                "^finish_shutdown cannot overlap begin_shutdown "
                "or finish_shutdown$"
            ),
        ):
            coordinator.finish_shutdown(20.0)
        assert _shutdown_mutation_snapshot(coordinator) == before
        assert (
            tuple(harness.resident.cancel_calls),
            tuple(harness.save_dispatcher.cancel_calls),
            tuple(harness.scheduler.cancel_requests),
            tuple(harness.save_dispatcher.shutdown_calls),
        ) == child_calls
        assert tuple(harness.diagnostics.calls) == diagnostics
        assert wait_calls == []
    finally:
        owner_release.set()
        owner.join(2)
    assert not owner.is_alive()
    assert len(owner_reports) == 1


@pytest.mark.parametrize("restart_source", ["stop_timeout", "malformed_report"])
def test_finish_shutdown_preserves_existing_restart_latch_without_live_blockers(
    restart_source: str,
) -> None:
    harness = Harness()
    if restart_source == "stop_timeout":
        drive_to_bridge(harness, imported=True)
        coordinator = harness.coordinator_instance
        assert coordinator.stop_all(0.0).restart_required is True
        harness.scheduler.complete_report(
            terminal_report(
                RUN_IDS[0],
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="USER_CANCELED",
                summary="late user stop",
            )
        )
        assert coordinator.snapshot().analysis_attempt_ids == ()
    else:
        harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
        harness.scheduler.synchronous_report = object()
        coordinator = harness.coordinator()
        result = coordinator.start_reanalysis(reanalysis_request())
        assert result.status is OperationStatus.PERSISTENCE_REJECTED
        assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert coordinator.snapshot().restart_required is True

    coordinator.begin_shutdown()
    report = coordinator.finish_shutdown(10.0)
    repeated = coordinator.finish_shutdown(20.0)

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == ()
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert report.pending_publication_run_ids == ()
    assert report.errors == ()
    assert report.restart_required is True
    assert repeated is report
    assert harness.save_dispatcher.shutdown_calls == [10.0]


def test_finish_shutdown_past_deadline_reports_exact_blockers_and_latches() -> None:
    harness = Harness()
    harness.monotonic.values.extend([10.0])
    coordinator = harness.coordinator()
    with coordinator._lock:
        context = _RunContext(imported_request())
        coordinator._runs[context.run_id] = context
    coordinator.begin_shutdown()

    report = coordinator.finish_shutdown(9.0)

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == (imported_request().run_id,)
    assert report.restart_required is True
    assert coordinator.snapshot().restart_required is True
    assert harness.save_dispatcher.shutdown_calls == [9.0]


def test_closed_admission_rejects_result_operations() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.begin_shutdown()

    result = coordinator.apply_manual(object())

    assert result.status is OperationStatus.INVALID_REQUEST


def _blocked_reanalysis_submit(harness: Harness):
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.scheduler.hook.entered = entered
    harness.scheduler.hook.release = release
    coordinator = harness.coordinator()
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )
    worker.start()
    assert entered.wait(2)
    return coordinator, worker, release, results


def test_stop_intent_survives_in_flight_reanalysis_authority_acquisition() -> None:
    harness = Harness()
    coordinator, worker, release, results = _blocked_reanalysis_submit(harness)

    first = coordinator.stop_all(0.0)
    release.set()
    worker.join(2)
    repeated = coordinator.stop_all(0.0)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.SUBMITTED
    assert first.status is StopStatus.INCOMPLETE
    assert repeated.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]


def test_shutdown_intent_survives_in_flight_reanalysis_authority_acquisition() -> None:
    harness = Harness()
    coordinator, worker, release, results = _blocked_reanalysis_submit(harness)

    coordinator.begin_shutdown()
    release.set()
    worker.join(2)
    report = coordinator.finish_shutdown(-1.0)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.SUBMITTED
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]
    assert report.remaining_attempt_ids == (ATTEMPT_IDS[0],)
    assert report.restart_required is True


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("terminal_kind", ["committed", "noncommitted"])
def test_reanalysis_held_terminal_wins_lifecycle_race_without_cancel(
    permanent: bool,
    terminal_kind: str,
) -> None:
    harness = Harness()
    if terminal_kind == "committed":
        harness.scheduler.synchronous_report = reanalysis_report()
    else:
        accepted = harness.scheduler.accepted_bridge_result(RUN_IDS[0])
        harness.scheduler.bridge_results.append(
            BridgeSubmissionResult(
                status=BridgeSubmissionStatus.STATE_UNKNOWN,
                authority=BridgeAuthorityStatus.ACQUIRED,
                run_id=RUN_IDS[0],
                analysis_attempt_id=ATTEMPT_IDS[0],
                scheduler_accepted=True,
                terminal_delivered_before_return=True,
                restart_required=True,
                code="ATTEMPT_TERMINAL_DB_FAILED",
                reason="terminal database failed",
                submit_receipt=accepted.submit_receipt,
            )
        )
        harness.scheduler.synchronous_report = db_failed_reanalysis_report(
            "ATTEMPT_TERMINAL_DB_FAILED",
            "terminal database failed",
        )
    coordinator, worker, release, results = _blocked_reanalysis_submit(harness)

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.SUBMITTED
    assert harness.scheduler.cancel_requests == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()


@pytest.mark.parametrize("permanent", [False, True])
def test_malformed_reanalysis_held_terminal_keeps_quarantine_without_cancel(
    permanent: bool,
) -> None:
    harness = Harness()
    harness.scheduler.synchronous_report = object()
    coordinator, worker, release, results = _blocked_reanalysis_submit(harness)

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.PERSISTENCE_REJECTED
    assert harness.scheduler.cancel_requests == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert coordinator.snapshot().restart_required is True


def test_shutdown_race_with_not_acquired_reanalysis_does_not_cancel() -> None:
    harness = Harness()
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=False,
            code="ATTEMPT_QUEUE_DB_FAILED",
            reason="queue transaction rolled back",
            submit_receipt=None,
        )
    )
    coordinator, worker, release, results = _blocked_reanalysis_submit(harness)

    coordinator.begin_shutdown()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.SCHEDULER_REJECTED
    assert harness.scheduler.cancel_requests == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()


def test_finish_shutdown_caches_dispatcher_exception_with_no_save_jobs() -> None:
    harness = Harness()
    harness.save_dispatcher.shutdown_exception = RuntimeError("save close failed")
    coordinator = harness.coordinator()
    coordinator.begin_shutdown()

    report = coordinator.finish_shutdown(10.0)
    repeated = coordinator.finish_shutdown(20.0)

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.restart_required is True
    assert report.save_dispatcher_report.status is (
        SaveDispatcherShutdownStatus.CLOSED
    )
    assert report.save_dispatcher_report.errors
    assert report.errors
    assert repeated is report
    assert harness.save_dispatcher.shutdown_calls == [10.0]


def test_begin_shutdown_cancels_each_live_child_once() -> None:
    capture = Harness()
    capture_coordinator = capture.coordinator()
    capture_coordinator.start_captured(captured_request())
    capture_coordinator.begin_shutdown()
    capture_coordinator.begin_shutdown()
    assert len(capture.resident.cancel_calls) == 1

    save = Harness()
    save_coordinator = save.coordinator()
    save_coordinator.start_imported(imported_request())
    save_coordinator.begin_shutdown()
    save_coordinator.begin_shutdown()
    assert save.save_dispatcher.cancel_calls == [
        (RUN_IDS[0], save.save_dispatcher.calls[0][0].save_job_id)
    ]

    initial = Harness()
    drive_to_bridge(initial, imported=True)
    initial_coordinator = initial.coordinator_instance
    initial_coordinator.begin_shutdown()
    initial_coordinator.begin_shutdown()
    assert [request.origin for request in initial.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]

    reanalysis = Harness()
    reanalysis.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    reanalysis_coordinator = reanalysis.coordinator()
    reanalysis_coordinator.start_reanalysis(reanalysis_request())
    reanalysis_coordinator.begin_shutdown()
    reanalysis_coordinator.begin_shutdown()
    assert [
        request.origin for request in reanalysis.scheduler.cancel_requests
    ] == [AnalysisCancelOrigin.USER_STOP]


def test_stop_prebridge_accepted_run_uses_existing_terminal_path() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())

    report = coordinator.stop_all(1.0)

    assert report.status is StopStatus.SETTLED
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN
    assert len(harness.persistence.terminal_calls) == 1
    assert (
        harness.persistence.terminal_calls[0].conclusion
        is TerminalConclusion.MANUAL_STOPPED
    )
    assert len(harness.resident.cancel_calls) == 1


def _blocked_imported_active_commit(harness: Harness):
    entered = Event()
    release = Event()
    harness.persistence.hooks["active"].entered = entered
    harness.persistence.hooks["active"].release = release
    coordinator = harness.coordinator()
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.start_imported(imported_request())
        )
    )
    worker.start()
    assert entered.wait(2)
    return coordinator, worker, release, results


@pytest.mark.parametrize("permanent", [False, True])
def test_lifecycle_intent_reconciles_in_flight_captured_active_commit(
    permanent: bool,
) -> None:
    harness = Harness()
    entered = Event()
    release = Event()
    harness.persistence.hooks["active"].entered = entered
    harness.persistence.hooks["active"].release = release
    coordinator = harness.coordinator()
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.start_captured(captured_request())
        )
    )
    worker.start()
    assert entered.wait(2)

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.persistence.terminal_calls) == 1
    assert len(harness.resident.cancel_calls) == 1
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.cancel_requests == []


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize(
    "active_result",
    ["committed", "rejected", "exception"],
)
def test_lifecycle_intent_reconciles_in_flight_imported_active_commit(
    permanent: bool,
    active_result: str,
) -> None:
    harness = Harness()
    if active_result == "rejected":
        harness.persistence.active_results.append(CommitResult.UNAVAILABLE)
    elif active_result == "exception":
        harness.persistence.active_exception = RuntimeError("active failed")
    coordinator, worker, release, results = _blocked_imported_active_commit(
        harness
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        first = coordinator.stop_all(0.0)
        assert first.pending_ingress_run_ids == (RUN_IDS[0],)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.cancel_requests == []
    if active_result == "committed":
        assert results[0].status is StartStatus.ACCEPTED_TERMINAL
        assert len(harness.persistence.terminal_calls) == 1
        assert (
            harness.persistence.terminal_calls[0].conclusion
            is TerminalConclusion.MANUAL_STOPPED
        )
    else:
        assert results[0].status is StartStatus.ACTIVE_COMMIT_REJECTED
        assert harness.persistence.terminal_calls == []

    if permanent:
        report = coordinator.finish_shutdown(-1.0)
        assert report.status is CoordinatorShutdownStatus.CLOSED
    else:
        repeated = coordinator.stop_all(0.0)
        assert repeated.status is StopStatus.CLOSED


def _blocked_save_job_id_factory(
    harness: Harness,
    *,
    source_kind: str,
    factory_outcome: str,
):
    entered = Event()
    release = Event()

    def factory():
        entered.set()
        assert release.wait(2)
        if factory_outcome == "exception":
            raise RuntimeError("save ID unavailable")
        if factory_outcome == "non_id":
            return "not-a-uuid"
        return SAVE_JOB_IDS[0]

    coordinator = harness.coordinator()
    coordinator._save_job_id_factory = factory
    results = []
    if source_kind == "captured":
        harness.resident.synchronous_outcome = completed_capture
        start = lambda: coordinator.start_captured(
            captured_request(source=TriggerSource.PLC)
        )
    else:
        start = lambda: coordinator.start_imported(imported_request())
    worker = Thread(target=lambda: results.append(start()))
    worker.start()
    assert entered.wait(2)
    return coordinator, worker, release, results


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("source_kind", ["imported", "captured"])
@pytest.mark.parametrize(
    "factory_outcome",
    ["valid", "exception", "non_id"],
)
def test_lifecycle_intent_reconciles_blocked_save_job_id_factory(
    permanent: bool,
    source_kind: str,
    factory_outcome: str,
) -> None:
    harness = Harness()
    coordinator, worker, release, results = _blocked_save_job_id_factory(
        harness,
        source_kind=source_kind,
        factory_outcome=factory_outcome,
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        first = coordinator.stop_all(0.0)
        assert first.status is StopStatus.INCOMPLETE
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.persistence.terminal_calls) == 1
    assert (
        harness.persistence.terminal_calls[0].conclusion
        is TerminalConclusion.MANUAL_STOPPED
    )
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.initial_calls == []
    assert coordinator.snapshot().ingress_run_ids == ()
    assert coordinator.snapshot().accepted_active_run_ids == ()
    terminal_history = [
        event
        for event in harness.history.calls
        if event.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == (0 if permanent else 1)
    if source_kind == "captured":
        assert len(harness.plc.publish_calls) == 1
        assert harness.plc.reject_calls == []
    else:
        assert harness.plc.publish_calls == []
        assert harness.plc.reject_calls == []


@pytest.mark.parametrize("source_kind", ["imported", "captured"])
@pytest.mark.parametrize("factory_outcome", ["exception", "non_id"])
def test_save_job_id_factory_failure_without_lifecycle_intent_is_unchanged(
    source_kind: str,
    factory_outcome: str,
) -> None:
    harness = Harness()
    coordinator, worker, release, results = _blocked_save_job_id_factory(
        harness,
        source_kind=source_kind,
        factory_outcome=factory_outcome,
    )

    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert len(harness.persistence.terminal_calls) == 1
    terminal = harness.persistence.terminal_calls[0]
    assert terminal.conclusion is TerminalConclusion.ERROR
    assert terminal.error_category is TerminalErrorCategory.AUDIO_SAVE_FAILED
    assert "save job ID" in terminal.reason
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.initial_calls == []


def test_stop_timeout_after_late_active_acceptance_has_exact_run_evidence() -> None:
    harness = Harness()
    harness.monotonic.values.extend([0.0, 0.0, 1.0])
    terminal_entered = Event()
    terminal_release = Event()
    harness.persistence.hooks["terminal"].entered = terminal_entered
    harness.persistence.hooks["terminal"].release = terminal_release
    coordinator, start_worker, active_release, _ = (
        _blocked_imported_active_commit(harness)
    )
    wait_entered = Event()
    original_wait = coordinator._condition.wait

    def observed_wait(timeout):
        wait_entered.set()
        return original_wait(timeout)

    coordinator._condition.wait = observed_wait
    reports = []
    errors = []

    def stop():
        try:
            reports.append(coordinator.stop_all(1.0))
        except Exception as exc:
            errors.append(exc)

    stop_worker = Thread(target=stop)
    stop_worker.start()
    assert wait_entered.wait(2)
    active_release.set()
    stop_worker.join(2)

    assert not stop_worker.is_alive()
    assert errors == []
    assert reports[0].status is StopStatus.INCOMPLETE
    assert reports[0].canceled_accepted_run_ids == (RUN_IDS[0],)
    assert reports[0].pending_ingress_run_ids == ()

    terminal_release.set()
    start_worker.join(2)
    assert not start_worker.is_alive()
    assert len(harness.persistence.terminal_calls) == 1


def test_same_deadline_late_active_terminal_settles_stop(
    monkeypatch,
) -> None:
    harness = Harness()
    coordinator, start_worker, active_release, _ = (
        _blocked_imported_active_commit(harness)
    )
    wait_entered = Event()
    original_wait = coordinator._condition.wait

    def observed_wait(timeout):
        wait_entered.set()
        return original_wait(timeout)

    monkeypatch.setattr(coordinator._condition, "wait", observed_wait)
    reports = []
    errors = []

    def stop():
        try:
            reports.append(coordinator.stop_all(1.0))
        except Exception as exc:
            errors.append(exc)

    stop_worker = Thread(target=stop)
    stop_worker.start()
    assert wait_entered.wait(2)
    active_release.set()
    stop_worker.join(0.5)
    if stop_worker.is_alive():
        with coordinator._lock:
            harness.monotonic.values.append(1.0)
            coordinator._condition.notify_all()
        stop_worker.join(2)
    start_worker.join(2)

    assert not stop_worker.is_alive()
    assert not start_worker.is_alive()
    assert errors == []
    assert reports[0].status is StopStatus.SETTLED
    assert len(harness.persistence.terminal_calls) == 1


def _blocked_imported_save_submit(harness: Harness):
    entered = Event()
    release = Event()
    harness.save_dispatcher.hook.entered = entered
    harness.save_dispatcher.hook.release = release
    coordinator = harness.coordinator()
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.start_imported(imported_request())
        )
    )
    worker.start()
    assert entered.wait(2)
    return coordinator, worker, release, results


@pytest.mark.parametrize("failure_stage", ["clock", "wait"])
def test_stop_all_post_mutation_wait_failure_closes_with_exact_report(
    monkeypatch,
    failure_stage: str,
) -> None:
    harness = Harness()
    coordinator, worker, release, _ = _blocked_imported_save_submit(harness)
    clock_calls = 0
    wait_calls = 0

    def clock():
        nonlocal clock_calls
        clock_calls += 1
        if failure_stage == "clock" and clock_calls == 2:
            raise RuntimeError("stop clock failed")
        return 0.0

    def wait(_timeout):
        nonlocal wait_calls
        wait_calls += 1
        raise RuntimeError("stop wait failed")

    monkeypatch.setattr(coordinator, "_monotonic_clock", clock)
    if failure_stage == "wait":
        monkeypatch.setattr(coordinator._condition, "wait", wait)

    report = coordinator.stop_all(1.0)

    assert report.status is StopStatus.INCOMPLETE
    assert report.timed_out is True
    assert report.restart_required is True
    assert report.canceled_accepted_run_ids == (RUN_IDS[0],)
    assert len(report.errors) == 1
    assert failure_stage in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED
    assert coordinator.snapshot().restart_required is True
    assert wait_calls == (1 if failure_stage == "wait" else 0)

    release.set()
    worker.join(2)
    assert not worker.is_alive()
    request, sink = harness.save_dispatcher.calls[0]
    sink(_cancelled_save_outcome(request))
    repeated = coordinator.stop_all(1.0)
    assert repeated.status is StopStatus.CLOSED
    assert repeated.restart_required is True
    assert len(harness.save_dispatcher.cancel_calls) == 1


def test_stop_wait_failure_after_preaccept_settlement_has_no_stale_identity(
    monkeypatch,
) -> None:
    harness = Harness()
    harness.imported_freezer.exception = RuntimeError(
        "planned preaccept rejection"
    )
    publication_entered = Event()
    publication_release = Event()
    harness.history.hooks[HistoryFactKind.RUN_TERMINATED].entered = (
        publication_entered
    )
    harness.history.hooks[HistoryFactKind.RUN_TERMINATED].release = (
        publication_release
    )
    coordinator = harness.coordinator()
    start_results = []
    start_done = Event()

    def start() -> None:
        try:
            start_results.append(
                coordinator.start_imported(imported_request())
            )
        finally:
            start_done.set()
            with coordinator._lock:
                coordinator._condition.notify_all()

    start_worker = Thread(target=start)
    start_worker.start()
    assert publication_entered.wait(2)

    original_wait = coordinator._condition.wait
    wait_entered = Event()
    wait_calls = 0

    def failing_wait(timeout):
        nonlocal wait_calls
        wait_calls += 1
        wait_entered.set()
        while not start_done.is_set():
            original_wait(timeout)
        raise RuntimeError("stop wait failed after settlement")

    monkeypatch.setattr(coordinator._condition, "wait", failing_wait)
    reports = []
    stop_errors = []

    def stop() -> None:
        try:
            reports.append(coordinator.stop_all(1.0))
        except Exception as exc:
            stop_errors.append(exc)

    stop_worker = Thread(target=stop)
    stop_worker.start()
    assert wait_entered.wait(2)
    publication_release.set()
    start_worker.join(2)
    stop_worker.join(2)

    assert not start_worker.is_alive()
    assert not stop_worker.is_alive()
    assert len(start_results) == 1
    assert stop_errors == []
    assert len(reports) == 1
    report = reports[0]
    assert report.status is StopStatus.INCOMPLETE
    assert report.pending_ingress_run_ids == ()
    assert report.pending_publication_run_ids == ()
    assert report.canceled_accepted_run_ids == ()
    assert report.canceled_reanalysis_attempt_ids == ()
    assert report.pending_analysis_attempt_ids == ()
    assert report.timed_out is False
    assert 0 < len(report.errors) <= 32
    assert "stop wait failed after settlement" in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert report.restart_required is True
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED
    assert coordinator.snapshot().restart_required is True
    assert wait_calls == 1
    assert len(harness.imported_freezer.calls) == 1
    assert len(harness.history.calls) == 1

    repeated = coordinator.stop_all(1.0)

    assert repeated.status is StopStatus.CLOSED
    assert repeated.restart_required is True
    assert len(harness.imported_freezer.calls) == 1
    assert len(harness.history.calls) == 1


@pytest.mark.parametrize("failure_stage", ["clock", "wait"])
def test_finish_shutdown_post_dispatch_wait_failure_caches_actual_report(
    monkeypatch,
    failure_stage: str,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    context = _RunContext(imported_request())
    with coordinator._lock:
        coordinator._runs[context.run_id] = context
    coordinator.begin_shutdown()
    dispatcher_report = SaveDispatcherShutdownReport(
        status=SaveDispatcherShutdownStatus.CLOSED,
        accepted_count=0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=(),
        live_worker_ids=(),
        errors=(),
    )
    harness.save_dispatcher.shutdown = lambda deadline: (
        harness.save_dispatcher.shutdown_calls.append(deadline)
        or dispatcher_report
    )
    wait_calls = 0

    def failing_clock():
        if failure_stage == "clock":
            raise RuntimeError("finish clock failed")
        return 0.0

    def failing_wait(_timeout):
        nonlocal wait_calls
        wait_calls += 1
        raise RuntimeError("finish wait failed")

    monkeypatch.setattr(coordinator, "_monotonic_clock", failing_clock)
    if failure_stage == "wait":
        monkeypatch.setattr(coordinator._condition, "wait", failing_wait)

    report = coordinator.finish_shutdown(10.0)
    repeated = coordinator.finish_shutdown(20.0)

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == (RUN_IDS[0],)
    assert report.save_dispatcher_report is dispatcher_report
    assert report.restart_required is True
    assert len(report.errors) == 1
    assert failure_stage in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert repeated is report
    assert harness.save_dispatcher.shutdown_calls == [10.0]
    assert wait_calls == (1 if failure_stage == "wait" else 0)
    with coordinator._lock:
        assert coordinator._shutdown_finish_in_progress is False


def _cancelled_save_outcome(request) -> AudioSaveOutcome:
    return AudioSaveOutcome(
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        status=AudioSaveStatus.CANCELLED,
        persisted_audio=None,
        error_category=TerminalErrorCategory.AUDIO_SAVE_CANCELLED,
        reason="save canceled",
    )


def _configure_save_cancel_receipt(harness: Harness, request, receipt: str) -> None:
    if receipt == "exception":
        harness.save_dispatcher.cancel_exception = RuntimeError("cancel failed")
        return
    if receipt == "duck":
        class DuckSaveCancelResult:
            status = SaveCancelStatus.CANCEL_REQUESTED
            run_id = request.run_id
            save_job_id = request.save_job_id

        harness.save_dispatcher.cancel_result = DuckSaveCancelResult()
        return
    status = (
        SaveCancelStatus.NOT_FOUND
        if receipt == "not_found"
        else SaveCancelStatus.ALREADY_TERMINAL
        if receipt == "already_terminal"
        else SaveCancelStatus.CANCEL_REQUESTED
    )
    result = SaveCancelResult(
        status=status,
        run_id=RUN_IDS[1] if receipt == "wrong_run" else request.run_id,
        save_job_id=(
            SAVE_JOB_IDS[1]
            if receipt == "wrong_job"
            else request.save_job_id
        ),
        reason="",
    )
    if receipt == "malformed_exact":
        object.__setattr__(result, "reason", "malformed")
    harness.save_dispatcher.cancel_result = result


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize(
    "receipt",
    [
        "cancel_requested",
        "already_terminal",
        "not_found",
        "malformed_exact",
        "wrong_run",
        "wrong_job",
        "duck",
        "exception",
    ],
)
def test_save_cancel_exact_boundary_retains_job_until_exact_completion(
    permanent: bool,
    receipt: str,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    request, sink = harness.save_dispatcher.calls[0]
    _configure_save_cancel_receipt(harness, request, receipt)

    if permanent:
        coordinator.begin_shutdown()
        report = coordinator.finish_shutdown(-1.0)
        assert report.remaining_save_job_ids == (request.save_job_id,)
    else:
        report = coordinator.stop_all(0.0)
        assert report.status is StopStatus.INCOMPLETE
    exact_success = receipt in {"cancel_requested", "already_terminal"}

    with coordinator._lock:
        assert request.save_job_id in coordinator._save_jobs
        assert coordinator._child_cancel_actions == set()
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]
    cancel_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if "save" in fact.stage.lower() and "cancel" in fact.stage.lower()
    ]
    assert len(cancel_diagnostics) == (0 if exact_success else 1)
    if cancel_diagnostics:
        assert len(cancel_diagnostics[0].message.encode("utf-8")) <= 512

    sink(_cancelled_save_outcome(request))
    with coordinator._lock:
        assert request.save_job_id not in coordinator._save_jobs
    assert len(harness.save_dispatcher.cancel_calls) == 1


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize(
    "receipt",
    [
        "cancel_requested",
        "already_terminal",
        "not_found",
        "malformed_exact",
        "wrong_run",
        "wrong_job",
        "duck",
        "exception",
    ],
)
def test_save_completion_racing_cancel_return_settles_truthfully(
    permanent: bool,
    receipt: str,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    request, sink = harness.save_dispatcher.calls[0]
    _configure_save_cancel_receipt(harness, request, receipt)
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    reports = []
    lifecycle_worker = Thread(
        target=(
            coordinator.begin_shutdown
            if permanent
            else lambda: reports.append(coordinator.stop_all(1.0))
        )
    )
    lifecycle_worker.start()
    assert cancel_entered.wait(2)

    sink(_cancelled_save_outcome(request))
    cancel_release.set()
    lifecycle_worker.join(2)

    assert not lifecycle_worker.is_alive()
    with coordinator._lock:
        assert request.save_job_id not in coordinator._save_jobs
        assert coordinator._child_cancel_actions == set()
    if permanent:
        report = coordinator.finish_shutdown(1.0)
        assert report.status is (
            CoordinatorShutdownStatus.INCOMPLETE
            if receipt == "exception"
            else CoordinatorShutdownStatus.CLOSED
        )
        assert len(report.errors) == (1 if receipt == "exception" else 0)
        assert report.restart_required is (receipt == "exception")
    else:
        assert reports[0].status is StopStatus.SETTLED
        assert len(reports[0].errors) == (1 if receipt == "exception" else 0)
        assert reports[0].restart_required is False
    exact_success = receipt in {"cancel_requested", "already_terminal"}
    cancel_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if "save" in fact.stage.lower() and "cancel" in fact.stage.lower()
    ]
    assert len(cancel_diagnostics) == (0 if exact_success else 1)
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]


def test_settled_stop_errors_do_not_poison_later_stop_or_shutdown() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    request, sink = harness.save_dispatcher.calls[0]
    harness.save_dispatcher.cancel_exception = RuntimeError("cancel failed")
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    reports = []
    stop_worker = Thread(
        target=lambda: reports.append(coordinator.stop_all(1.0))
    )
    stop_worker.start()
    assert cancel_entered.wait(2)
    sink(_cancelled_save_outcome(request))
    cancel_release.set()
    stop_worker.join(2)

    assert not stop_worker.is_alive()
    assert reports[0].status is StopStatus.SETTLED
    assert len(reports[0].errors) == 1
    for _ in range(5):
        later_stop = coordinator.stop_all(0.0)
        assert later_stop.status is StopStatus.SETTLED
        assert later_stop.errors == ()

    coordinator.begin_shutdown()
    shutdown = coordinator.finish_shutdown(1.0)
    assert shutdown.status is CoordinatorShutdownStatus.CLOSED
    assert shutdown.errors == ()


def test_invalid_established_save_cancel_does_not_enter_stop_error_scope() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    request, sink = harness.save_dispatcher.calls[0]
    harness.save_dispatcher.cancel_result = object()
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    reports = []
    stop_worker = Thread(target=lambda: reports.append(coordinator.stop_all(1.0)))
    stop_worker.start()
    assert cancel_entered.wait(2)
    sink(_cancelled_save_outcome(request))
    cancel_release.set()
    stop_worker.join(2)

    assert not stop_worker.is_alive()
    assert reports[0].status is StopStatus.SETTLED
    assert reports[0].errors == ()
    assert reports[0].restart_required is False
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN
    assert len(harness.diagnostics.calls) == 1
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]

    coordinator.begin_shutdown()
    shutdown = coordinator.finish_shutdown(1.0)
    assert shutdown.status is CoordinatorShutdownStatus.CLOSED
    assert shutdown.errors == ()


@pytest.mark.parametrize("permanent", [False, True])
def test_active_lifecycle_error_scope_is_bounded_to_32(permanent: bool) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    contexts = []
    with coordinator._lock:
        for value in range(1, 41):
            run_id = f"40000000-0000-4000-8000-{value:012d}"
            save_job_id = f"50000000-0000-4000-8000-{value:012d}"
            context = _RunContext(imported_request(run_id))
            context.durable_active = True
            context.save_child_accepted = True
            context.save_job_id = save_job_id
            coordinator._runs[run_id] = context
            coordinator._save_jobs[save_job_id] = context
            contexts.append(context)
    harness.save_dispatcher.cancel_exception = RuntimeError("cancel failed")

    if permanent:
        coordinator.begin_shutdown()
        report = coordinator.finish_shutdown(-1.0)
    else:
        report = coordinator.stop_all(0.0)

    assert report.status in {
        StopStatus.INCOMPLETE,
        CoordinatorShutdownStatus.INCOMPLETE,
    }
    assert len(report.errors) == 32
    with coordinator._lock:
        errors = (
            coordinator._shutdown_errors
            if permanent
            else coordinator._restart_errors
        )
        assert len(errors) == 32
    cancel_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if "save" in fact.stage.lower() and "cancel" in fact.stage.lower()
    ]
    assert len(cancel_diagnostics) == 40


@pytest.mark.parametrize("permanent", [False, True])
def test_late_accepted_save_submit_dispatches_one_tracked_lifecycle_cancel(
    permanent: bool,
) -> None:
    harness = Harness()
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator, worker, submit_release, _ = _blocked_imported_save_submit(
        harness
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    assert cancel_entered.wait(2)

    request, sink = harness.save_dispatcher.calls[0]
    with coordinator._lock:
        assert len(coordinator._child_cancel_actions) == 1
        assert request.save_job_id in coordinator._save_jobs
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_requests == []

    cancel_release.set()
    worker.join(2)
    assert not worker.is_alive()
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()
        assert request.save_job_id in coordinator._save_jobs
    assert harness.save_dispatcher.cancel_calls == [
        (RUN_IDS[0], request.save_job_id)
    ]

    sink(_cancelled_save_outcome(request))
    with coordinator._lock:
        assert request.save_job_id not in coordinator._save_jobs
    assert len(harness.persistence.terminal_calls) == 1
    assert harness.scheduler.cancel_requests == []


def test_closed_stop_does_not_misclassify_late_durable_save_cancel() -> None:
    harness = Harness()
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator, worker, submit_release, _ = _blocked_imported_save_submit(
        harness
    )
    first = coordinator.stop_all(0.0)
    submit_release.set()
    assert cancel_entered.wait(2)
    request, _ = harness.save_dispatcher.calls[0]

    try:
        repeated = coordinator.stop_all(0.0)

        assert first.status is StopStatus.INCOMPLETE
        assert repeated.status is StopStatus.CLOSED
        assert repeated.pending_ingress_run_ids == ()
        assert repeated.canceled_accepted_run_ids == (RUN_IDS[0],)
        assert repeated.pending_analysis_attempt_ids == ()
    finally:
        cancel_release.set()
        worker.join(2)

    assert not worker.is_alive()
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]


@pytest.mark.parametrize("durable_active", [False, True])
def test_resident_cancel_action_retains_exact_run_classification_after_retirement(
    durable_active: bool,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    context = _RunContext(captured_request())
    reservation_result = harness.resident.reserve(
        context.run_id,
        "hardware-1",
    )
    assert reservation_result.reservation is not None
    context.reservation = reservation_result.reservation
    context.durable_active = durable_active
    with coordinator._lock:
        coordinator._runs[context.run_id] = context
    cancel_entered = Event()
    cancel_release = Event()
    harness.resident.hooks["cancel"].entered = cancel_entered
    harness.resident.hooks["cancel"].release = cancel_release
    begin_worker = Thread(target=coordinator.begin_shutdown)
    begin_worker.start()
    assert cancel_entered.wait(2)

    try:
        with coordinator._lock:
            coordinator._runs.pop(context.run_id)
            blockers = coordinator._shutdown_blockers_locked()

        assert blockers[0] == (() if durable_active else (context.run_id,))
        assert blockers[1] == ((context.run_id,) if durable_active else ())
        assert blockers[2] == ()
        assert blockers[3] == ()
        assert blockers[4] == ()
    finally:
        cancel_release.set()
        begin_worker.join(2)

    assert not begin_worker.is_alive()
    assert len(harness.resident.cancel_calls) == 1


@pytest.mark.parametrize("retire_context", [False, True])
def test_finish_shutdown_classifies_late_durable_save_cancel_exactly_once(
    retire_context: bool,
) -> None:
    harness = Harness()
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator, worker, submit_release, _ = _blocked_imported_save_submit(
        harness
    )
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    request, _ = harness.save_dispatcher.calls[0]

    try:
        if retire_context:
            with coordinator._lock:
                coordinator._runs.pop(request.run_id)
                coordinator._save_jobs.pop(request.save_job_id)

        report = coordinator.finish_shutdown(-1.0)

        assert report.status is CoordinatorShutdownStatus.INCOMPLETE
        assert report.remaining_ingress_run_ids == ()
        assert report.remaining_run_ids == (request.run_id,)
        assert report.remaining_attempt_ids == ()
        assert report.remaining_save_job_ids == (request.save_job_id,)
        assert report.pending_publication_run_ids == ()
        assert report.restart_required is True
    finally:
        cancel_release.set()
        worker.join(2)

    assert not worker.is_alive()
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("cancel_receipt", ["normal", "exception", "malformed"])
def test_save_completion_during_late_cancel_reconciles_after_cancel_return(
    permanent: bool,
    cancel_receipt: str,
) -> None:
    harness = Harness()
    if cancel_receipt == "exception":
        harness.save_dispatcher.cancel_exception = RuntimeError("cancel failed")
    elif cancel_receipt == "malformed":
        harness.save_dispatcher.cancel_result = object()
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator, worker, submit_release, _ = _blocked_imported_save_submit(
        harness
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    assert cancel_entered.wait(2)
    request, sink = harness.save_dispatcher.calls[0]

    sink(_cancelled_save_outcome(request))
    with coordinator._lock:
        assert request.save_job_id not in coordinator._save_jobs
        assert len(coordinator._child_cancel_actions) == 1
    assert harness.persistence.terminal_calls == []

    cancel_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(harness.persistence.terminal_calls) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()
        assert RUN_IDS[0] not in coordinator._runs


@pytest.mark.parametrize("permanent", [False, True])
def test_late_accepted_save_cancel_exception_keeps_job_without_retry(
    permanent: bool,
) -> None:
    harness = Harness()
    harness.save_dispatcher.cancel_exception = RuntimeError("cancel failed")
    coordinator, worker, submit_release, _ = _blocked_imported_save_submit(
        harness
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    worker.join(2)

    assert not worker.is_alive()
    request, sink = harness.save_dispatcher.calls[0]
    with coordinator._lock:
        assert request.save_job_id in coordinator._save_jobs
        assert coordinator._child_cancel_actions == set()
    assert harness.save_dispatcher.cancel_calls == [
        (RUN_IDS[0], request.save_job_id)
    ]
    assert harness.persistence.terminal_calls == []
    assert len(harness.diagnostics.calls) == 1

    if permanent:
        report = coordinator.finish_shutdown(-1.0)
        assert report.remaining_save_job_ids == (request.save_job_id,)
        coordinator.begin_shutdown()
    else:
        report = coordinator.stop_all(0.0)
        assert report.canceled_accepted_run_ids == (RUN_IDS[0],)
    assert len(harness.save_dispatcher.cancel_calls) == 1

    sink(_cancelled_save_outcome(request))
    with coordinator._lock:
        assert request.save_job_id not in coordinator._save_jobs
    assert len(harness.persistence.terminal_calls) == 1


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("submit_result", ["rejected", "exception"])
def test_late_nonaccepted_save_submit_does_not_cancel_owner(
    permanent: bool,
    submit_result: str,
) -> None:
    harness = Harness()
    if submit_result == "rejected":
        harness.save_dispatcher.status = SaveSubmitStatus.SATURATED
    else:
        harness.save_dispatcher.exception = RuntimeError("submit failed")
    coordinator, worker, submit_release, results = _blocked_imported_save_submit(
        harness
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is StartStatus.ACCEPTED_TERMINAL
    assert harness.save_dispatcher.cancel_calls == []
    assert len(harness.persistence.terminal_calls) == 1
    assert harness.scheduler.cancel_requests == []


def _blocked_manual_history(harness: Harness, boundary: str):
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    kind = HistoryFactKind.TERMINAL_RESULT_REPLACED
    hook = (
        harness.projector.hooks[kind]
        if boundary == "projector"
        else harness.history.hooks[kind]
    )
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.apply_manual(manual_request())
        )
    )
    worker.start()
    assert entered.wait(2)
    return coordinator, worker, release, results


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("boundary", ["projector", "sink"])
@pytest.mark.parametrize("raises", [False, True])
def test_result_history_dispatch_is_lifecycle_blocker_until_return(
    permanent: bool,
    boundary: str,
    raises: bool,
) -> None:
    harness = Harness()
    if raises:
        if boundary == "projector":
            harness.projector.exception = RuntimeError("projector failed")
        else:
            harness.history.exception = RuntimeError("sink failed")
    coordinator, operation_worker, release, _ = _blocked_manual_history(
        harness,
        boundary,
    )
    lifecycle_reports = []
    lifecycle_done = Event()

    if permanent:
        coordinator.begin_shutdown()

        def lifecycle():
            lifecycle_reports.append(coordinator.finish_shutdown(1.0))
            lifecycle_done.set()

    else:

        def lifecycle():
            lifecycle_reports.append(coordinator.stop_all(1.0))
            lifecycle_done.set()

    lifecycle_worker = Thread(target=lifecycle)
    lifecycle_worker.start()
    completed_while_blocked = lifecycle_done.wait(0.1)
    release.set()
    operation_worker.join(2)
    lifecycle_worker.join(2)

    assert completed_while_blocked is False
    assert not operation_worker.is_alive()
    assert not lifecycle_worker.is_alive()
    if permanent:
        assert lifecycle_reports[0].status is CoordinatorShutdownStatus.CLOSED
    else:
        assert lifecycle_reports[0].status is StopStatus.SETTLED


def test_reanalysis_started_projector_remains_blocker_after_report_retirement() -> None:
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.projector.hooks[HistoryFactKind.REANALYSIS_STARTED].entered = entered
    harness.projector.hooks[HistoryFactKind.REANALYSIS_STARTED].release = release
    coordinator = harness.coordinator()
    start_worker = Thread(
        target=lambda: coordinator.start_reanalysis(reanalysis_request())
    )
    start_worker.start()
    assert entered.wait(2)

    stop_reports = []
    stop_done = Event()

    def stop():
        stop_reports.append(coordinator.stop_all(1.0))
        stop_done.set()

    stop_worker = Thread(target=stop)
    stop_worker.start()
    harness.scheduler.complete_report(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )
    completed_while_blocked = stop_done.wait(0.1)
    release.set()
    start_worker.join(2)
    stop_worker.join(2)

    assert completed_while_blocked is False
    assert not start_worker.is_alive()
    assert not stop_worker.is_alive()
    assert stop_reports[0].status is StopStatus.SETTLED


def test_reanalysis_terminal_sink_remains_blocker_after_context_retirement() -> None:
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request())
    entered = Event()
    release = Event()
    harness.history.hooks[HistoryFactKind.REANALYSIS_COMPLETED].entered = entered
    harness.history.hooks[HistoryFactKind.REANALYSIS_COMPLETED].release = release
    report_worker = Thread(
        target=lambda: harness.scheduler.complete_report(reanalysis_report())
    )
    report_worker.start()
    assert entered.wait(2)

    stop_reports = []
    stop_done = Event()
    stop_worker = Thread(
        target=lambda: (
            stop_reports.append(coordinator.stop_all(1.0)),
            stop_done.set(),
        )
    )
    stop_worker.start()
    completed_while_blocked = stop_done.wait(0.1)
    release.set()
    report_worker.join(2)
    stop_worker.join(2)

    assert completed_while_blocked is False
    assert not report_worker.is_alive()
    assert not stop_worker.is_alive()
    assert stop_reports[0].status is StopStatus.SETTLED


@pytest.mark.parametrize("stage", ["register", "start", "plc_mark"])
def test_begin_shutdown_cancels_resident_during_unrelated_capture_action(
    stage: str,
) -> None:
    harness = Harness()
    hook = (
        harness.plc.hooks["mark"]
        if stage == "plc_mark"
        else harness.resident.hooks[stage]
    )
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    worker = Thread(
        target=lambda: coordinator.start_captured(
            captured_request(
                source=(
                    TriggerSource.PLC
                    if stage == "plc_mark"
                    else TriggerSource.MANUAL
                )
            )
        )
    )
    worker.start()
    assert entered.wait(2)

    coordinator.begin_shutdown()
    cancel_count_while_blocked = len(harness.resident.cancel_calls)
    release.set()
    worker.join(2)

    assert cancel_count_while_blocked == 1
    assert not worker.is_alive()
    assert len(harness.resident.cancel_calls) == 1
    if stage == "register":
        assert harness.resident.start_calls == []
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.cancel_requests == []


@pytest.mark.parametrize("boundary", ["projector", "sink"])
def test_begin_shutdown_cancels_acquired_initial_during_history_action(
    boundary: str,
) -> None:
    harness = Harness()
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    hook = (
        harness.projector.hooks[HistoryFactKind.ANALYZING]
        if boundary == "projector"
        else harness.history.hooks[HistoryFactKind.ANALYZING]
    )
    entered = Event()
    release = Event()
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    worker = Thread(
        target=lambda: coordinator.start_imported(imported_request())
    )
    worker.start()
    assert entered.wait(2)

    coordinator.begin_shutdown()
    cancel_count_while_blocked = len(harness.scheduler.cancel_requests)
    release.set()
    worker.join(2)

    assert cancel_count_while_blocked == 1
    assert not worker.is_alive()
    assert len(harness.scheduler.cancel_requests) == 1
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        assert context.action is None


def test_publish_active_does_not_leak_sink_action_after_shutdown_intent() -> None:
    harness = Harness()
    entered = Event()
    release = Event()
    harness.projector.hooks[HistoryFactKind.RECORDING].entered = entered
    harness.projector.hooks[HistoryFactKind.RECORDING].release = release
    coordinator = harness.coordinator()
    worker = Thread(
        target=lambda: coordinator.start_captured(captured_request())
    )
    worker.start()
    assert entered.wait(2)

    coordinator.begin_shutdown()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    with coordinator._lock:
        context = coordinator._runs.get(RUN_IDS[0])
        assert context is None or context.action is None
    assert harness.resident.start_calls == []


def _capture_cancel_race_outcome(reservation, outcome_kind: str) -> CaptureOutcome:
    if outcome_kind == "completed":
        return completed_capture(reservation)
    return CaptureOutcome(
        run_id=reservation.run_id,
        reservation=reservation,
        status=CaptureOutcomeStatus.FAILED,
        audio=None,
        error_category=TerminalErrorCategory.CAPTURE_FAILED,
        reason="capture failed during cancel",
    )


def _configure_resident_cancel_receipt(
    harness: Harness,
    reservation,
    receipt: str,
) -> None:
    if receipt == "cancelled":
        return
    if receipt == "already_terminal":
        harness.resident.cancel_result = ResidentCancelResult(
            status=ResidentCancelStatus.ALREADY_TERMINAL,
            run_id=reservation.run_id,
            reservation_id=reservation.reservation_id,
            reason="",
        )
    elif receipt == "invalid":
        harness.resident.cancel_result = ResidentCancelResult(
            status=ResidentCancelStatus.INVALID,
            run_id=reservation.run_id,
            reservation_id=reservation.reservation_id,
            reason="binding remains externally owned",
        )
    elif receipt == "malformed_exact":
        malformed = ResidentCancelResult(
            status=ResidentCancelStatus.CANCELLED,
            run_id=reservation.run_id,
            reservation_id=reservation.reservation_id,
            reason="",
        )
        object.__setattr__(malformed, "reason", "invalid success reason")
        harness.resident.cancel_result = malformed
    elif receipt == "wrong_identity":
        harness.resident.cancel_result = ResidentCancelResult(
            status=ResidentCancelStatus.CANCELLED,
            run_id=RUN_IDS[1],
            reservation_id=reservation.reservation_id,
            reason="",
        )
    elif receipt == "wrong_type":
        harness.resident.cancel_result = object()
    else:
        harness.resident.cancel_exception = RuntimeError("cancel failed")


@pytest.mark.parametrize("failure_kind", ["exception", "invalid"])
def test_begin_during_reserve_classifies_late_resident_cancel(
    failure_kind: str,
) -> None:
    harness = Harness()
    reserve_entered = Event()
    reserve_release = Event()
    harness.resident.hooks["reserve"].entered = reserve_entered
    harness.resident.hooks["reserve"].release = reserve_release
    coordinator = harness.coordinator()
    results = []
    start_worker = Thread(
        target=lambda: results.append(
            coordinator.start_captured(captured_request())
        )
    )
    start_worker.start()
    assert reserve_entered.wait(2)
    coordinator.begin_shutdown()
    if failure_kind == "exception":
        harness.resident.cancel_exception = RuntimeError("cancel failed")
    else:
        harness.resident.cancel_result = object()
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    reserve_release.set()
    try:
        assert diagnostic_entered.wait(2)
        with coordinator._lock:
            context = coordinator._runs[RUN_IDS[0]]
            reservation = context.reservation
            assert reservation is not None
            assert context.action is not None
            assert len(coordinator._child_cancel_actions) == 1
            assert coordinator._shutdown_blockers_locked()[0] == (
                RUN_IDS[0],
            )
            assert len(coordinator._shutdown_errors) == (
                1 if failure_kind == "exception" else 0
            )
            assert coordinator._restart_required is (
                failure_kind == "exception"
            )
            if failure_kind == "invalid":
                context.early_capture_outcome = completed_capture(
                    reservation
                )
    finally:
        diagnostic_release.set()
        start_worker.join(2)

    assert not start_worker.is_alive()
    assert len(results) == 1
    assert len(harness.diagnostics.calls) == 1
    assert len(harness.resident.cancel_calls) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()

    report = coordinator.finish_shutdown(-1.0)
    assert report.status is (
        CoordinatorShutdownStatus.INCOMPLETE
        if failure_kind == "exception"
        else CoordinatorShutdownStatus.CLOSED
    )
    assert report.remaining_ingress_run_ids == (
        (RUN_IDS[0],) if failure_kind == "exception" else ()
    )
    assert len(report.errors) == (
        1 if failure_kind == "exception" else 0
    )
    assert report.restart_required is (failure_kind == "exception")
    assert len(harness.resident.cancel_calls) == 1


@pytest.mark.parametrize("failure_kind", ["exception", "invalid"])
@pytest.mark.parametrize("retired", [False, True])
def test_late_resident_cancel_diagnostic_blocks_action_and_notifies_once(
    monkeypatch,
    failure_kind: str,
    retired: bool,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    reservation = harness.resident.reserve(
        RUN_IDS[0],
        "hardware-1",
    ).reservation
    assert reservation is not None
    with coordinator._lock:
        context = _RunContext(captured_request())
        context.reservation = reservation
        context.cancel_requested = True
        coordinator._runs[context.run_id] = context
        coordinator._shutdown_begun = True
        coordinator._lifecycle = CoordinatorLifecycle.CLOSED
    if failure_kind == "exception":
        harness.resident.cancel_exception = RuntimeError("cancel failed")
    else:
        harness.resident.cancel_result = object()
    cancel_entered = Event()
    cancel_release = Event()
    harness.resident.hooks["cancel"].entered = cancel_entered
    harness.resident.hooks["cancel"].release = cancel_release
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    worker = Thread(
        target=lambda: coordinator._cancel_frozen_reservation(
            context,
            reservation,
        )
    )
    worker.start()
    assert cancel_entered.wait(2)
    if retired:
        with coordinator._lock:
            coordinator._runs.pop(context.run_id)
    notifications = _count_condition_notifications(
        monkeypatch,
        coordinator,
    )
    cancel_release.set()
    try:
        assert diagnostic_entered.wait(2)
        with coordinator._lock:
            assert context.action is not None
            assert len(coordinator._child_cancel_actions) == 1
            assert len(coordinator._shutdown_errors) == (
                1 if failure_kind == "exception" else 0
            )
            assert coordinator._restart_required is (
                failure_kind == "exception"
            )
    finally:
        diagnostic_release.set()
        worker.join(2)

    assert not worker.is_alive()
    assert len(notifications) == 1
    assert len(harness.diagnostics.calls) == 1
    assert len(harness.resident.cancel_calls) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize(
    "receipt",
    [
        "cancelled",
        "already_terminal",
        "invalid",
        "malformed_exact",
        "wrong_identity",
        "wrong_type",
        "exception",
    ],
)
def test_lifecycle_retires_resident_only_after_exact_settled_cancel(
    permanent: bool,
    receipt: str,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())
    reservation = harness.resident.register_calls[0]
    _configure_resident_cancel_receipt(harness, reservation, receipt)

    if permanent:
        coordinator.begin_shutdown()
        report = coordinator.finish_shutdown(-1.0)
    else:
        report = coordinator.stop_all(0.0)
    clean = receipt in {"cancelled", "already_terminal"}

    assert len(harness.resident.cancel_calls) == 1
    if permanent:
        assert report.status is (
            CoordinatorShutdownStatus.CLOSED
            if clean
            else CoordinatorShutdownStatus.INCOMPLETE
        )
        assert report.remaining_run_ids == (() if clean else (RUN_IDS[0],))
    else:
        assert report.status is (
            StopStatus.SETTLED if clean else StopStatus.INCOMPLETE
        )
        assert coordinator.snapshot().lifecycle is (
            CoordinatorLifecycle.OPEN
            if clean
            else CoordinatorLifecycle.CLOSED
        )
    assert coordinator.snapshot().restart_required is (not clean)
    cancel_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if "resident" in fact.stage.lower()
        and "cancel" in fact.stage.lower()
    ]
    assert len(cancel_diagnostics) == (0 if clean else 1)
    if cancel_diagnostics:
        assert len(cancel_diagnostics[0].message.encode("utf-8")) <= 512

    coordinator.begin_shutdown()
    final = coordinator.finish_shutdown(-1.0)
    assert final.status is (
        CoordinatorShutdownStatus.CLOSED
        if clean
        else CoordinatorShutdownStatus.INCOMPLETE
    )
    assert final.remaining_run_ids == (() if clean else (RUN_IDS[0],))
    assert final.restart_required is (not clean)
    assert len(harness.resident.cancel_calls) == 1


def test_late_reservation_cancel_exception_retains_ingress_blocker() -> None:
    harness = Harness()
    harness.persistence.active_results.append(CommitResult.UNAVAILABLE)
    harness.resident.cancel_exception = RuntimeError("cancel failed")
    coordinator = harness.coordinator()

    result = coordinator.start_captured(captured_request())
    coordinator.begin_shutdown()
    report = coordinator.finish_shutdown(-1.0)

    assert result.status is StartStatus.ACTIVE_COMMIT_REJECTED
    assert len(harness.resident.cancel_calls) == 1
    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == (RUN_IDS[0],)
    assert report.remaining_run_ids == ()
    assert report.restart_required is True
    assert coordinator.finish_shutdown(10.0) is report
    assert len(harness.resident.cancel_calls) == 1


@pytest.mark.parametrize(
    "cancel_receipt",
    ["normal", "invalid", "exception", "malformed"],
)
@pytest.mark.parametrize("outcome_kind", ["completed", "failed"])
@pytest.mark.parametrize("permanent", [False, True])
def test_lifecycle_drains_capture_outcome_racing_resident_cancel(
    cancel_receipt: str,
    outcome_kind: str,
    permanent: bool,
) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())
    reservation = harness.resident.register_calls[0]
    if cancel_receipt == "invalid":
        _configure_resident_cancel_receipt(harness, reservation, "invalid")
    elif cancel_receipt == "exception":
        _configure_resident_cancel_receipt(harness, reservation, "exception")
    elif cancel_receipt == "malformed":
        _configure_resident_cancel_receipt(harness, reservation, "wrong_type")
    cancel_entered = Event()
    cancel_release = Event()
    harness.resident.hooks["cancel"].entered = cancel_entered
    harness.resident.hooks["cancel"].release = cancel_release
    lifecycle_reports = []
    lifecycle_worker = Thread(
        target=(
            coordinator.begin_shutdown
            if permanent
            else lambda: lifecycle_reports.append(coordinator.stop_all(1.0))
        )
    )
    lifecycle_worker.start()
    assert cancel_entered.wait(2)

    harness.resident.complete(
        _capture_cancel_race_outcome(reservation, outcome_kind)
    )
    cancel_release.set()
    lifecycle_worker.join(2)
    report = (
        coordinator.finish_shutdown(-1.0)
        if permanent
        else lifecycle_reports[0]
    )

    assert not lifecycle_worker.is_alive()
    assert len(harness.resident.cancel_calls) == 1
    assert len(harness.persistence.terminal_calls) == 1
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.cancel_requests == []
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []
    assert report.status is (
        (
            CoordinatorShutdownStatus.CLOSED
            if cancel_receipt != "exception"
            else CoordinatorShutdownStatus.INCOMPLETE
        )
        if permanent
        else StopStatus.SETTLED
    )
    if permanent:
        assert report.remaining_run_ids == ()
    assert report.restart_required is (
        permanent and cancel_receipt == "exception"
    )
    assert len(report.errors) == (
        1 if cancel_receipt == "exception" else 0
    )


def _configure_plc_terminal_receipt(
    harness: Harness,
    receipt: str,
) -> None:
    if receipt == "applied":
        return
    if receipt == "exception":
        harness.plc.publish_exception = RuntimeError(
            "terminal publication failed"
        )
    elif receipt == "applied_value":
        harness.plc.publish_result = PlcMutationDisposition.APPLIED.value
    elif receipt == "malformed":
        harness.plc.publish_result = object()
    else:
        harness.plc.publish_result = PlcMutationDisposition[receipt.upper()]


def _drive_plc_terminal(
    harness: Harness,
    terminal_source: str,
):
    if terminal_source == "prebridge":
        harness.resident.start_exception = RuntimeError("capture start failed")
        coordinator = harness.coordinator()
        coordinator.start_captured(
            captured_request(source=TriggerSource.PLC)
        )
        return coordinator
    drive_to_bridge(
        harness,
        captured_source=TriggerSource.PLC,
    )
    harness.scheduler.complete_report(terminal_report(RUN_IDS[0]))
    return harness.coordinator_instance


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("terminal_source", ["prebridge", "bridge"])
@pytest.mark.parametrize(
    "receipt",
    [
        "applied",
        "excluded",
        "duplicate_or_stale",
        "invalid_state",
        "closed",
        "applied_value",
        "malformed",
        "exception",
    ],
)
def test_plc_terminal_settlement_remains_lifecycle_blocker_until_applied(
    permanent: bool,
    terminal_source: str,
    receipt: str,
) -> None:
    harness = Harness()
    _configure_plc_terminal_receipt(harness, receipt)
    coordinator = _drive_plc_terminal(harness, terminal_source)
    applied = receipt == "applied"

    snapshot = coordinator.snapshot()
    assert snapshot.pending_publication_run_ids == (
        () if applied else (RUN_IDS[0],)
    )
    assert snapshot.restart_required is (not applied)
    if not applied:
        with coordinator._lock:
            publication = coordinator._runs[RUN_IDS[0]]
            assert isinstance(publication, _PublishingContext)
            assert publication.ticket is harness.plc.publish_calls[0][0]
            assert publication.ticket.run_id == RUN_IDS[0]

    if permanent:
        coordinator.begin_shutdown()
        report = coordinator.finish_shutdown(-1.0)
        assert report.status is (
            CoordinatorShutdownStatus.CLOSED
            if applied
            else CoordinatorShutdownStatus.INCOMPLETE
        )
        assert report.pending_publication_run_ids == (
            () if applied else (RUN_IDS[0],)
        )
    else:
        report = coordinator.stop_all(0.0)
        assert report.status is (
            StopStatus.SETTLED if applied else StopStatus.CLOSED
        )
        assert report.pending_publication_run_ids == (
            () if applied else (RUN_IDS[0],)
        )
    assert report.restart_required is (not applied)

    coordinator.begin_shutdown()
    final = coordinator.finish_shutdown(-1.0)
    assert final.status is (
        CoordinatorShutdownStatus.CLOSED
        if applied
        else CoordinatorShutdownStatus.INCOMPLETE
    )
    assert final.pending_publication_run_ids == (
        () if applied else (RUN_IDS[0],)
    )
    assert final.restart_required is (not applied)
    assert len(harness.plc.publish_calls) == 1
    assert harness.plc.reject_calls == []
    assert len(harness.plc.register_calls) == 1


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("receipt", ["closed", "exception"])
def test_failed_plc_terminal_publication_racing_lifecycle_stays_owned(
    permanent: bool,
    receipt: str,
) -> None:
    harness = Harness()
    _configure_plc_terminal_receipt(harness, receipt)
    entered = Event()
    release = Event()
    harness.plc.hooks["publish"].entered = entered
    harness.plc.hooks["publish"].release = release
    results = []
    worker = Thread(
        target=lambda: results.append(
            _drive_plc_terminal(harness, "bridge")
        )
    )
    worker.start()
    assert entered.wait(2)
    coordinator = harness.coordinator_instance

    if permanent:
        coordinator.begin_shutdown()
    else:
        first = coordinator.stop_all(0.0)
        assert first.status is StopStatus.INCOMPLETE
        assert first.pending_publication_run_ids == (RUN_IDS[0],)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    if permanent:
        report = coordinator.finish_shutdown(-1.0)
        assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    else:
        report = coordinator.stop_all(0.0)
        assert report.status is StopStatus.CLOSED
    assert report.pending_publication_run_ids == (RUN_IDS[0],)
    assert report.restart_required is True
    assert len(harness.plc.publish_calls) == 1
    assert harness.plc.reject_calls == []


def test_stop_waits_for_acquired_attempt_report_then_reopens() -> None:
    harness = Harness()
    drive_to_bridge(harness, imported=True)
    coordinator = harness.coordinator_instance
    cancel_entered = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    reports = []
    worker = Thread(target=lambda: reports.append(coordinator.stop_all(1.0)))

    worker.start()
    assert cancel_entered.wait(2)
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="user stopped",
        )
    )
    worker.join(2)

    assert not worker.is_alive()
    assert reports[0].status is StopStatus.SETTLED
    assert reports[0].pending_analysis_attempt_ids == ()
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN


def test_timed_out_stop_consumes_late_report_without_reopening() -> None:
    harness = Harness()
    drive_to_bridge(harness, imported=True)
    coordinator = harness.coordinator_instance

    timed_out = coordinator.stop_all(0.0)
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="late user stop",
        )
    )
    repeated = coordinator.stop_all(0.0)

    assert timed_out.status is StopStatus.INCOMPLETE
    assert timed_out.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert repeated.status is StopStatus.CLOSED
    assert repeated.restart_required is True
    assert coordinator.snapshot().analysis_attempt_ids == ()
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED


def test_begin_shutdown_child_exception_is_diagnosed_once(monkeypatch) -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    coordinator.start_captured(captured_request())
    calls = []

    def failing_cancel(reservation):
        calls.append(reservation)
        raise RuntimeError("resident cancel failed")

    monkeypatch.setattr(harness.resident, "cancel", failing_cancel)
    coordinator.begin_shutdown()
    coordinator.begin_shutdown()

    assert len(calls) == 1
    assert len(harness.diagnostics.calls) == 1


def test_finish_shutdown_recomputes_remaining_duration_before_every_wait(
    monkeypatch,
) -> None:
    harness = Harness()
    harness.monotonic.values.extend([2.0, 4.0])
    coordinator = harness.coordinator()
    context = _RunContext(imported_request())
    with coordinator._lock:
        coordinator._runs[context.run_id] = context
    coordinator.begin_shutdown()
    waits = []

    def observe_wait(timeout):
        waits.append(timeout)
        if len(waits) == 2:
            coordinator._runs.pop(context.run_id)

    monkeypatch.setattr(coordinator._condition, "wait", observe_wait)

    report = coordinator.finish_shutdown(10.0)

    assert waits == [8.0, 6.0]
    assert report.status is CoordinatorShutdownStatus.CLOSED


def test_finish_shutdown_past_deadline_never_waits(monkeypatch) -> None:
    harness = Harness()
    harness.monotonic.values.append(5.0)
    coordinator = harness.coordinator()
    context = _RunContext(imported_request())
    with coordinator._lock:
        coordinator._runs[context.run_id] = context
    coordinator.begin_shutdown()

    def forbidden_wait(timeout):
        raise AssertionError(f"past deadline waited for {timeout}")

    monkeypatch.setattr(coordinator._condition, "wait", forbidden_wait)

    report = coordinator.finish_shutdown(5.0)

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == (RUN_IDS[0],)


def test_finish_shutdown_reports_live_save_job_without_retry() -> None:
    harness = Harness()
    harness.save_dispatcher.shutdown_exception = RuntimeError("save close failed")
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    save_job_id = harness.save_dispatcher.calls[0][0].save_job_id
    coordinator.begin_shutdown()

    report = coordinator.finish_shutdown(-1.0)
    repeated = coordinator.finish_shutdown(100.0)

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_save_job_ids == (save_job_id,)
    assert report.save_dispatcher_report.remaining_save_job_ids == (save_job_id,)
    assert repeated is report
    assert harness.save_dispatcher.shutdown_calls == [-1.0]


def _blocked_lifecycle_cancel_after_acquired(harness: Harness):
    coordinator, submit_worker, submit_release, _ = _blocked_reanalysis_submit(
        harness
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    return (
        coordinator,
        submit_worker,
        submit_release,
        cancel_entered,
        cancel_release,
    )


def test_closed_stop_classifies_retired_bridge_cancel_as_attempt_only() -> None:
    harness = Harness()
    (
        coordinator,
        submit_worker,
        submit_release,
        cancel_entered,
        cancel_release,
    ) = _blocked_lifecycle_cancel_after_acquired(harness)

    first = coordinator.stop_all(0.0)
    submit_release.set()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    sink(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )

    try:
        repeated = coordinator.stop_all(0.0)

        assert first.status is StopStatus.INCOMPLETE
        assert repeated.status is StopStatus.CLOSED
        assert repeated.pending_ingress_run_ids == ()
        assert repeated.pending_publication_run_ids == ()
        assert repeated.canceled_accepted_run_ids == ()
        assert repeated.canceled_reanalysis_attempt_ids == ()
        assert repeated.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    finally:
        cancel_release.set()
        submit_worker.join(2)

    assert not submit_worker.is_alive()


def test_finish_shutdown_classifies_retired_bridge_cancel_as_run_and_attempt() -> None:
    harness = Harness()
    (
        coordinator,
        submit_worker,
        submit_release,
        cancel_entered,
        cancel_release,
    ) = _blocked_lifecycle_cancel_after_acquired(harness)
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    sink(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )

    try:
        report = coordinator.finish_shutdown(-1.0)

        assert report.status is CoordinatorShutdownStatus.INCOMPLETE
        assert report.remaining_ingress_run_ids == ()
        assert report.remaining_run_ids == (RUN_IDS[0],)
        assert report.remaining_attempt_ids == (ATTEMPT_IDS[0],)
        assert report.remaining_save_job_ids == ()
        assert report.pending_publication_run_ids == ()
        assert report.restart_required is True
    finally:
        cancel_release.set()
        submit_worker.join(2)

    assert not submit_worker.is_alive()


def test_initial_terminal_retirement_does_not_drop_cancel_exception() -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned initial cancel failure"
    )
    drive_to_bridge(harness, imported=True)
    coordinator = harness.coordinator_instance
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    begin_worker = Thread(target=coordinator.begin_shutdown)
    begin_worker.start()
    assert cancel_entered.wait(2)

    try:
        harness.scheduler.complete_report(
            terminal_report(
                RUN_IDS[0],
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="USER_CANCELED",
                summary="canceled while shutdown cancel was in flight",
            )
        )
        assert coordinator.snapshot().analysis_attempt_ids == ()
    finally:
        cancel_release.set()
        begin_worker.join(2)

    assert not begin_worker.is_alive()
    report = coordinator.finish_shutdown(-1.0)
    repeated = coordinator.finish_shutdown(10.0)
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge-initial shutdown cancel"
    ]

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == ()
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert len(report.errors) == 1
    assert "planned initial cancel failure" in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert report.restart_required is True
    assert repeated is report
    assert len(diagnostics) == 1
    assert "planned initial cancel failure" in diagnostics[0].message
    assert len(diagnostics[0].message.encode("utf-8")) <= 512
    assert len(harness.scheduler.cancel_requests) == 1

    coordinator.begin_shutdown()
    assert len(harness.scheduler.cancel_requests) == 1


def test_reanalysis_terminal_retirement_does_not_drop_cancel_exception() -> None:
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned reanalysis cancel failure"
    )
    coordinator = harness.coordinator()
    started = coordinator.start_reanalysis(reanalysis_request())
    assert started.status is OperationStatus.SUBMITTED
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    begin_worker = Thread(target=coordinator.begin_shutdown)
    begin_worker.start()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]

    try:
        sink(
            reanalysis_report(
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="REANALYSIS_CANCELED",
            )
        )
        assert coordinator.snapshot().result_operation_attempt_ids == ()
    finally:
        cancel_release.set()
        begin_worker.join(2)

    assert not begin_worker.is_alive()
    report = coordinator.finish_shutdown(-1.0)
    repeated = coordinator.finish_shutdown(10.0)
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge-reanalysis shutdown cancel"
    ]

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == ()
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert len(report.errors) == 1
    assert "planned reanalysis cancel failure" in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert report.restart_required is True
    assert repeated is report
    assert len(diagnostics) == 1
    assert "planned reanalysis cancel failure" in diagnostics[0].message
    assert len(diagnostics[0].message.encode("utf-8")) <= 512
    assert len(harness.scheduler.cancel_requests) == 1

    coordinator.begin_shutdown()
    assert len(harness.scheduler.cancel_requests) == 1


def test_stop_waits_for_late_acquired_cancel_action_after_terminal_report() -> None:
    harness = Harness()
    (
        coordinator,
        submit_worker,
        submit_release,
        cancel_entered,
        cancel_release,
    ) = _blocked_lifecycle_cancel_after_acquired(harness)
    stop_results = []
    stop_done = Event()

    def stop():
        stop_results.append(coordinator.stop_all(1.0))
        stop_done.set()

    stop_worker = Thread(target=stop)
    stop_worker.start()
    submit_release.set()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    sink(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )

    assert not stop_done.wait(0.2)
    cancel_release.set()
    submit_worker.join(2)
    stop_worker.join(2)

    assert stop_results[0].status is StopStatus.SETTLED
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]


def test_finish_waits_for_late_acquired_cancel_action_after_terminal_report() -> None:
    harness = Harness()
    (
        coordinator,
        submit_worker,
        submit_release,
        cancel_entered,
        cancel_release,
    ) = _blocked_lifecycle_cancel_after_acquired(harness)
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    sink(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )
    finish_results = []
    finish_done = Event()

    def finish():
        finish_results.append(coordinator.finish_shutdown(1.0))
        finish_done.set()

    finish_worker = Thread(target=finish)
    finish_worker.start()
    assert not finish_done.wait(0.2)
    cancel_release.set()
    submit_worker.join(2)
    finish_worker.join(2)

    assert finish_results[0].status is CoordinatorShutdownStatus.CLOSED
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]


def _blocked_initial_submit(
    harness: Harness,
    *,
    bridge_result=None,
    synchronous_report=None,
):
    if bridge_result is not None:
        harness.scheduler.bridge_results.append(bridge_result)
    harness.scheduler.synchronous_report = synchronous_report
    submit_entered = Event()
    submit_release = Event()
    harness.scheduler.hook.entered = submit_entered
    harness.scheduler.hook.release = submit_release
    coordinator = harness.coordinator()
    coordinator.start_imported(imported_request())
    save_request, save_sink = harness.save_dispatcher.calls[0]
    worker = Thread(target=lambda: save_sink(saved_outcome(save_request)))
    worker.start()
    assert submit_entered.wait(2)
    return coordinator, worker, submit_release


def test_late_acquired_initial_retirement_retains_cancel_exception() -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned late initial cancel failure"
    )
    coordinator, submit_worker, submit_release = _blocked_initial_submit(
        harness
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)

    try:
        harness.scheduler.complete_report(
            terminal_report(
                RUN_IDS[0],
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="USER_CANCELED",
                summary="late initial canceled during shutdown",
            )
        )
        assert coordinator.snapshot().analysis_attempt_ids == ()
    finally:
        cancel_release.set()
        submit_worker.join(2)

    assert not submit_worker.is_alive()
    report = coordinator.finish_shutdown(-1.0)
    repeated = coordinator.finish_shutdown(10.0)
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "Scheduler cancel"
    ]

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == ()
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert len(report.errors) == 1
    assert "planned late initial cancel failure" in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert report.restart_required is True
    assert repeated is report
    assert len(diagnostics) == 1
    assert "planned late initial cancel failure" in diagnostics[0].message
    assert len(diagnostics[0].message.encode("utf-8")) <= 512
    assert len(harness.scheduler.cancel_requests) == 1

    coordinator.begin_shutdown()
    assert len(harness.scheduler.cancel_requests) == 1


def test_late_acquired_reanalysis_retirement_retains_cancel_exception() -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned late reanalysis cancel failure"
    )
    coordinator, submit_worker, submit_release, _ = (
        _blocked_reanalysis_submit(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]

    try:
        sink(
            reanalysis_report(
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="REANALYSIS_CANCELED",
            )
        )
        assert coordinator.snapshot().result_operation_attempt_ids == ()
    finally:
        cancel_release.set()
        submit_worker.join(2)

    assert not submit_worker.is_alive()
    report = coordinator.finish_shutdown(-1.0)
    repeated = coordinator.finish_shutdown(10.0)
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge reanalysis cancel"
    ]

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_ingress_run_ids == ()
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert len(report.errors) == 1
    assert "planned late reanalysis cancel failure" in report.errors[0]
    assert len(report.errors[0].encode("utf-8")) <= 512
    assert report.restart_required is True
    assert repeated is report
    assert len(diagnostics) == 1
    assert "planned late reanalysis cancel failure" in diagnostics[0].message
    assert len(diagnostics[0].message.encode("utf-8")) <= 512
    assert len(harness.scheduler.cancel_requests) == 1

    coordinator.begin_shutdown()
    assert len(harness.scheduler.cancel_requests) == 1


def _install_blocked_failing_diagnostic(harness: Harness):
    entered = Event()
    release = Event()

    def record(fact) -> None:
        harness.diagnostics.calls.append(fact)
        entered.set()
        if not release.wait(5):
            raise AssertionError("blocked diagnostic was not released")
        raise RuntimeError("planned isolated diagnostic failure")

    harness.diagnostics.record = record
    return entered, release


def _observe_finish_during_blocked_diagnostic(
    coordinator,
    cancel_worker: Thread,
    diagnostic_entered: Event,
    diagnostic_release: Event,
):
    assert diagnostic_entered.wait(2)
    with coordinator._lock:
        blocked_state = (
            len(coordinator._shutdown_errors),
            coordinator._restart_required,
            len(coordinator._child_cancel_actions),
        )
    reports = []
    finish_done = Event()

    def finish() -> None:
        reports.append(coordinator.finish_shutdown(1.0))
        finish_done.set()

    finish_worker = Thread(target=finish)
    finish_worker.start()
    completed_while_diagnostic_blocked = finish_done.wait(0.2)
    diagnostic_release.set()
    cancel_worker.join(2)
    finish_worker.join(2)

    assert not cancel_worker.is_alive()
    assert not finish_worker.is_alive()
    assert len(reports) == 1
    return blocked_state, completed_while_diagnostic_blocked, reports[0]


def test_snapshot_cancel_error_blocks_finish_through_diagnostic() -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned snapshot cancel failure"
    )
    drive_to_bridge(harness, imported=True)
    coordinator = harness.coordinator_instance
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    begin_worker = Thread(target=coordinator.begin_shutdown)
    begin_worker.start()
    assert cancel_entered.wait(2)
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="snapshot cancel raced terminal retirement",
        )
    )
    assert coordinator.snapshot().analysis_attempt_ids == ()
    cancel_release.set()
    assert diagnostic_entered.wait(2)
    with coordinator._lock:
        blocked_state = (
            len(coordinator._shutdown_errors),
            coordinator._restart_required,
            len(coordinator._child_cancel_actions),
        )
    with pytest.raises(
        RuntimeError,
        match=(
            "^finish_shutdown cannot overlap begin_shutdown "
            "or finish_shutdown$"
        ),
    ):
        coordinator.finish_shutdown(1.0)
    diagnostic_release.set()
    begin_worker.join(2)
    assert not begin_worker.is_alive()
    report = coordinator.finish_shutdown(1.0)

    assert blocked_state == (1, True, 1)
    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert len(report.errors) == 1
    assert report.restart_required is True
    assert len(harness.diagnostics.calls) == 1
    assert len(harness.scheduler.cancel_requests) == 1


def test_global_cancel_error_blocks_finish_through_diagnostic() -> None:
    harness = Harness()
    harness.save_dispatcher.cancel_exception = RuntimeError(
        "planned global save cancel failure"
    )
    coordinator, submit_worker, submit_release, _ = (
        _blocked_imported_save_submit(harness)
    )
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    request, _ = harness.save_dispatcher.calls[0]
    with coordinator._lock:
        coordinator._runs.pop(request.run_id)
        coordinator._save_jobs.pop(request.save_job_id)
    cancel_release.set()

    blocked_state, completed_early, report = (
        _observe_finish_during_blocked_diagnostic(
            coordinator,
            submit_worker,
            diagnostic_entered,
            diagnostic_release,
        )
    )

    assert blocked_state == (1, True, 1)
    assert completed_early is False
    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert len(report.errors) == 1
    assert report.restart_required is True
    assert len(harness.diagnostics.calls) == 1
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]


def test_late_initial_cancel_error_blocks_finish_through_diagnostic() -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned late initial cancel failure"
    )
    coordinator, submit_worker, submit_release = _blocked_initial_submit(
        harness
    )
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="late initial cancel raced terminal retirement",
        )
    )
    assert coordinator.snapshot().analysis_attempt_ids == ()
    cancel_release.set()

    blocked_state, completed_early, report = (
        _observe_finish_during_blocked_diagnostic(
            coordinator,
            submit_worker,
            diagnostic_entered,
            diagnostic_release,
        )
    )

    assert blocked_state == (1, True, 1)
    assert completed_early is False
    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert len(report.errors) == 1
    assert report.restart_required is True
    assert len(harness.diagnostics.calls) == 1
    assert len(harness.scheduler.cancel_requests) == 1


def test_late_reanalysis_cancel_error_blocks_finish_through_diagnostic() -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError(
        "planned late reanalysis cancel failure"
    )
    coordinator, submit_worker, submit_release, _ = (
        _blocked_reanalysis_submit(harness)
    )
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    sink(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    cancel_release.set()

    blocked_state, completed_early, report = (
        _observe_finish_during_blocked_diagnostic(
            coordinator,
            submit_worker,
            diagnostic_entered,
            diagnostic_release,
        )
    )

    assert blocked_state == (1, True, 1)
    assert completed_early is False
    assert report.status is CoordinatorShutdownStatus.INCOMPLETE
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert len(report.errors) == 1
    assert report.restart_required is True
    assert len(harness.diagnostics.calls) == 1
    assert len(harness.scheduler.cancel_requests) == 1


def _count_condition_notifications(monkeypatch, coordinator):
    notifications = []
    original_notify_all = coordinator._condition.notify_all

    def notify_all() -> None:
        notifications.append(None)
        original_notify_all()

    monkeypatch.setattr(
        coordinator._condition,
        "notify_all",
        notify_all,
    )
    return notifications


@pytest.mark.parametrize("retired", [False, True])
def test_reusable_stop_cancel_reconciliation_notifies_once(
    monkeypatch,
    retired: bool,
) -> None:
    harness = Harness()
    drive_to_bridge(harness, imported=True)
    coordinator = harness.coordinator_instance
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        context.cancel_requested = True
    worker = Thread(target=lambda: coordinator._request_stop_for_run(context))
    worker.start()
    assert cancel_entered.wait(2)
    if retired:
        with coordinator._lock:
            coordinator._runs.pop(context.run_id)
            coordinator._attempts.pop(context.attempt_id)
    notifications = _count_condition_notifications(
        monkeypatch,
        coordinator,
    )
    cancel_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(notifications) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()


@pytest.mark.parametrize("retired", [False, True])
def test_global_save_cancel_reconciliation_notifies_once(
    monkeypatch,
    retired: bool,
) -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release, _ = (
        _blocked_imported_save_submit(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.save_dispatcher.cancel_hook.entered = cancel_entered
    harness.save_dispatcher.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    request, _ = harness.save_dispatcher.calls[0]
    if retired:
        with coordinator._lock:
            coordinator._runs.pop(request.run_id)
            coordinator._save_jobs.pop(request.save_job_id)
    notifications = _count_condition_notifications(
        monkeypatch,
        coordinator,
    )
    cancel_release.set()
    submit_worker.join(2)

    assert not submit_worker.is_alive()
    assert len(notifications) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()


@pytest.mark.parametrize("retired", [False, True])
def test_late_initial_cancel_reconciliation_notifies_once(
    monkeypatch,
    retired: bool,
) -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release = _blocked_initial_submit(
        harness
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    if retired:
        harness.scheduler.complete_report(
            terminal_report(
                RUN_IDS[0],
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="USER_CANCELED",
                summary="late initial cancel raced terminal retirement",
            )
        )
        assert coordinator.snapshot().analysis_attempt_ids == ()
    notifications = _count_condition_notifications(
        monkeypatch,
        coordinator,
    )
    cancel_release.set()
    submit_worker.join(2)

    assert not submit_worker.is_alive()
    assert len(notifications) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()


@pytest.mark.parametrize("retired", [False, True])
def test_late_reanalysis_cancel_reconciliation_notifies_once(
    monkeypatch,
    retired: bool,
) -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release, _ = (
        _blocked_reanalysis_submit(harness)
    )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    if retired:
        _, sink = harness.scheduler.reanalysis_calls[0]
        sink(
            reanalysis_report(
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="REANALYSIS_CANCELED",
            )
        )
        assert coordinator.snapshot().result_operation_attempt_ids == ()
    notifications = _count_condition_notifications(
        monkeypatch,
        coordinator,
    )
    cancel_release.set()
    submit_worker.join(2)

    assert not submit_worker.is_alive()
    assert len(notifications) == 1
    with coordinator._lock:
        assert coordinator._child_cancel_actions == set()


def test_stop_intent_survives_in_flight_initial_authority_acquisition() -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release = _blocked_initial_submit(harness)

    first = coordinator.stop_all(0.0)
    submit_release.set()
    submit_worker.join(2)
    repeated = coordinator.stop_all(0.0)

    assert not submit_worker.is_alive()
    assert first.status is StopStatus.INCOMPLETE
    assert repeated.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]


def test_shutdown_intent_survives_in_flight_initial_authority_acquisition() -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release = _blocked_initial_submit(harness)

    coordinator.begin_shutdown()
    submit_release.set()
    submit_worker.join(2)
    report = coordinator.finish_shutdown(-1.0)

    assert not submit_worker.is_alive()
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]
    assert report.remaining_attempt_ids == (ATTEMPT_IDS[0],)


def _configure_bridge_cancel_failure(
    harness: Harness,
    cancel_failure: str,
) -> None:
    if cancel_failure == "exception":
        harness.scheduler.cancel_exception = RuntimeError("cancel failed")
    elif cancel_failure == "malformed":
        harness.scheduler.cancel_result = object()
    else:
        harness.scheduler.cancel_result = BridgeCancelResult(
            status=BridgeCancelStatus.REJECTED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=ATTEMPT_IDS[0],
            origin=AnalysisCancelOrigin.USER_STOP,
            code="BRIDGE_CANCEL_REJECTED",
            reason="cancel rejected",
            receipt=None,
        )


def _assert_settled_cancel_classification(
    report,
    *,
    failure_kind: str,
) -> None:
    thrown = failure_kind == "exception"
    assert report.status is (
        CoordinatorShutdownStatus.INCOMPLETE
        if thrown
        else CoordinatorShutdownStatus.CLOSED
    )
    assert report.remaining_ingress_run_ids == ()
    assert report.remaining_run_ids == ()
    assert report.remaining_attempt_ids == ()
    assert report.remaining_save_job_ids == ()
    assert len(report.errors) == (1 if thrown else 0)
    assert report.restart_required is thrown


@pytest.mark.parametrize("failure_kind", ["exception", "malformed"])
def test_permanent_snapshot_cancel_classifies_throw_vs_invalid_return(
    failure_kind: str,
) -> None:
    harness = Harness()
    _configure_bridge_cancel_failure(harness, failure_kind)
    drive_to_bridge(harness, imported=True)
    coordinator = harness.coordinator_instance

    coordinator.begin_shutdown()

    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert coordinator.snapshot().restart_required is (
        failure_kind == "exception"
    )
    assert len(harness.diagnostics.calls) == 1
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="snapshot cancel settled later",
        )
    )
    report = coordinator.finish_shutdown(1.0)

    _assert_settled_cancel_classification(
        report,
        failure_kind=failure_kind,
    )
    assert len(harness.scheduler.cancel_requests) == 1


@pytest.mark.parametrize("failure_kind", ["exception", "malformed"])
def test_permanent_global_save_cancel_classifies_throw_vs_invalid_return(
    failure_kind: str,
) -> None:
    harness = Harness()
    if failure_kind == "exception":
        harness.save_dispatcher.cancel_exception = RuntimeError(
            "cancel failed"
        )
    else:
        harness.save_dispatcher.cancel_result = object()
    coordinator, worker, submit_release, _ = _blocked_imported_save_submit(
        harness
    )

    coordinator.begin_shutdown()
    submit_release.set()
    worker.join(2)

    assert not worker.is_alive()
    request, sink = harness.save_dispatcher.calls[0]
    with coordinator._lock:
        assert request.save_job_id in coordinator._save_jobs
    assert coordinator.snapshot().restart_required is (
        failure_kind == "exception"
    )
    assert len(harness.diagnostics.calls) == 1
    sink(_cancelled_save_outcome(request))
    report = coordinator.finish_shutdown(1.0)

    _assert_settled_cancel_classification(
        report,
        failure_kind=failure_kind,
    )
    assert harness.save_dispatcher.cancel_calls == [
        (request.run_id, request.save_job_id)
    ]


@pytest.mark.parametrize("failure_kind", ["exception", "malformed"])
def test_permanent_late_initial_cancel_classifies_throw_vs_invalid_return(
    failure_kind: str,
) -> None:
    harness = Harness()
    _configure_bridge_cancel_failure(harness, failure_kind)
    coordinator, worker, submit_release = _blocked_initial_submit(harness)

    coordinator.begin_shutdown()
    submit_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert coordinator.snapshot().restart_required is (
        failure_kind == "exception"
    )
    assert len(harness.diagnostics.calls) == 1
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="late initial cancel settled later",
        )
    )
    report = coordinator.finish_shutdown(1.0)

    _assert_settled_cancel_classification(
        report,
        failure_kind=failure_kind,
    )
    assert len(harness.scheduler.cancel_requests) == 1


@pytest.mark.parametrize("failure_kind", ["exception", "malformed"])
def test_permanent_late_reanalysis_cancel_classifies_throw_vs_invalid_return(
    failure_kind: str,
) -> None:
    harness = Harness()
    _configure_bridge_cancel_failure(harness, failure_kind)
    coordinator, worker, submit_release, results = (
        _blocked_reanalysis_submit(harness)
    )

    coordinator.begin_shutdown()
    submit_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.SUBMITTED
    assert coordinator.snapshot().result_operation_attempt_ids == (
        ATTEMPT_IDS[0],
    )
    assert coordinator.snapshot().restart_required is (
        failure_kind == "exception"
    )
    assert len(harness.diagnostics.calls) == 1
    harness.scheduler.complete_report(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )
    report = coordinator.finish_shutdown(1.0)

    _assert_settled_cancel_classification(
        report,
        failure_kind=failure_kind,
    )
    assert len(harness.scheduler.cancel_requests) == 1


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize(
    "cancel_failure",
    ["exception", "malformed", "rejected"],
)
def test_late_acquired_initial_lifecycle_cancel_failure_keeps_report_blocker(
    permanent: bool,
    cancel_failure: str,
) -> None:
    harness = Harness()
    _configure_bridge_cancel_failure(harness, cancel_failure)
    coordinator, submit_worker, submit_release = _blocked_initial_submit(harness)

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    submit_worker.join(2)

    assert not submit_worker.is_alive()
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert coordinator.snapshot().restart_required is (
        not permanent or cancel_failure == "exception"
    )
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []
    assert len(harness.scheduler.cancel_requests) == 1
    cancel_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "Scheduler cancel"
    ]
    assert len(cancel_diagnostics) == 1
    assert len(cancel_diagnostics[0].message.encode("utf-8")) <= 512

    if permanent:
        report = coordinator.finish_shutdown(-1.0)
        repeated = coordinator.finish_shutdown(999.0)
        assert report.remaining_attempt_ids == (ATTEMPT_IDS[0],)
        assert repeated is report
    else:
        report = coordinator.stop_all(0.0)
        assert report.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert len(harness.scheduler.cancel_requests) == 1

    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="user stopped",
        )
    )
    assert coordinator.snapshot().analysis_attempt_ids == ()
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize(
    "cancel_failure",
    ["exception", "malformed", "rejected"],
)
def test_reanalysis_lifecycle_cancel_failure_keeps_report_blocker(
    permanent: bool,
    cancel_failure: str,
) -> None:
    harness = Harness()
    _configure_bridge_cancel_failure(harness, cancel_failure)
    coordinator, worker, release, results = _blocked_reanalysis_submit(harness)

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert results[0].status is OperationStatus.SUBMITTED
    assert coordinator.snapshot().result_operation_attempt_ids == (
        ATTEMPT_IDS[0],
    )
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []
    assert len(harness.scheduler.cancel_requests) == 1
    cancel_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge reanalysis cancel"
    ]
    assert len(cancel_diagnostics) == 1
    assert len(cancel_diagnostics[0].message.encode("utf-8")) <= 512

    if permanent:
        report = coordinator.finish_shutdown(-1.0)
        assert report.remaining_attempt_ids == (ATTEMPT_IDS[0],)
    else:
        report = coordinator.stop_all(0.0)
        assert report.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert len(harness.scheduler.cancel_requests) == 1

    harness.scheduler.complete_report(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []


@pytest.mark.parametrize("permanent", [False, True])
def test_initial_lifecycle_race_not_acquired_never_cancels(permanent) -> None:
    harness = Harness()
    rejected = BridgeSubmissionResult(
        status=BridgeSubmissionStatus.REJECTED,
        authority=BridgeAuthorityStatus.NOT_ACQUIRED,
        run_id=RUN_IDS[0],
        analysis_attempt_id=ATTEMPT_IDS[0],
        scheduler_accepted=False,
        terminal_delivered_before_return=False,
        restart_required=False,
        code="ATTEMPT_QUEUE_DB_FAILED",
        reason="queue transaction rolled back",
        submit_receipt=None,
    )
    coordinator, submit_worker, submit_release = _blocked_initial_submit(
        harness,
        bridge_result=rejected,
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    submit_worker.join(2)

    assert not submit_worker.is_alive()
    assert harness.scheduler.cancel_requests == []
    assert coordinator.snapshot().analysis_attempt_ids == ()


@pytest.mark.parametrize("permanent", [False, True])
def test_initial_lifecycle_race_early_terminal_does_not_cancel(permanent) -> None:
    harness = Harness()
    report = terminal_report(RUN_IDS[0])
    early = harness.scheduler.accepted_bridge_result(
        RUN_IDS[0],
        early=True,
    )
    coordinator, submit_worker, submit_release = _blocked_initial_submit(
        harness,
        bridge_result=early,
        synchronous_report=report,
    )

    if permanent:
        coordinator.begin_shutdown()
    else:
        coordinator.stop_all(0.0)
    submit_release.set()
    submit_worker.join(2)

    assert not submit_worker.is_alive()
    assert harness.scheduler.cancel_requests == []
    assert coordinator.snapshot().analysis_attempt_ids == ()


def test_stop_waits_for_late_acquired_initial_cancel_after_terminal_report() -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release = _blocked_initial_submit(harness)
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    stop_results = []
    stop_done = Event()

    def stop():
        stop_results.append(coordinator.stop_all(1.0))
        stop_done.set()

    stop_worker = Thread(target=stop)
    stop_worker.start()
    submit_release.set()
    assert cancel_entered.wait(2)
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="user stopped",
        )
    )

    assert not stop_done.wait(0.2)
    cancel_release.set()
    submit_worker.join(2)
    stop_worker.join(2)

    assert stop_results[0].status is StopStatus.SETTLED
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]


def test_finish_waits_for_late_acquired_initial_cancel_after_terminal_report() -> None:
    harness = Harness()
    coordinator, submit_worker, submit_release = _blocked_initial_submit(harness)
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    coordinator.begin_shutdown()
    submit_release.set()
    assert cancel_entered.wait(2)
    harness.scheduler.complete_report(
        terminal_report(
            RUN_IDS[0],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
            summary="user stopped",
        )
    )
    finish_results = []
    finish_done = Event()

    def finish():
        finish_results.append(coordinator.finish_shutdown(1.0))
        finish_done.set()

    finish_worker = Thread(target=finish)
    finish_worker.start()
    assert not finish_done.wait(0.2)
    cancel_release.set()
    submit_worker.join(2)
    finish_worker.join(2)

    assert finish_results[0].status is CoordinatorShutdownStatus.CLOSED
    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP
    ]


def _settle_reusable_bridge_attempt(
    harness: Harness,
    *,
    initial: bool,
    committed: bool = True,
) -> None:
    if initial:
        harness.scheduler.complete_report(
            terminal_report(
                RUN_IDS[0],
                status=(
                    BridgeTerminalStatus.COMMITTED
                    if committed
                    else BridgeTerminalStatus.DB_FAILED
                ),
                state=(
                    AttemptState.CANCELED
                    if committed
                    else AttemptState.FAILED
                ),
                judgment=None,
                result_json=None,
                code=(
                    "USER_CANCELED"
                    if committed
                    else "ATTEMPT_TERMINAL_DB_FAILED"
                ),
                summary=(
                    "stop cancel settled"
                    if committed
                    else "terminal database failed"
                ),
            )
        )
        return
    harness.scheduler.complete_report(
        (
            reanalysis_report(
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="REANALYSIS_CANCELED",
            )
            if committed
            else db_failed_reanalysis_report(
                "ATTEMPT_TERMINAL_DB_FAILED",
                "terminal database failed",
            )
        )
    )


def _join_stop_worker(
    harness: Harness,
    coordinator,
    worker: Thread,
) -> None:
    worker.join(2)
    if worker.is_alive():
        with coordinator._lock:
            harness.monotonic.values.append(2.0)
            coordinator._condition.notify_all()
        worker.join(2)


@pytest.mark.parametrize(
    "seam",
    [
        "current-initial",
        "current-initial-after-deadline",
        "current-reanalysis",
        "late-initial",
        "late-reanalysis",
    ],
)
def test_reusable_bridge_cancel_exception_settles_and_reopens(
    monkeypatch,
    seam: str,
) -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError("cancel failed")
    submit_worker = None
    submit_release = None
    submit_entered = None
    after_deadline = seam == "current-initial-after-deadline"
    if after_deadline:
        harness.scheduler.bridge_results.extend(
            harness.scheduler.accepted_bridge_result(run_id, attempt_id)
            for run_id, attempt_id in zip(
                RUN_IDS[:2], ATTEMPT_IDS[:2], strict=True
            )
        )
        drive_to_bridge(harness, imported=True)
        coordinator = harness.coordinator_instance
        coordinator.start_imported(imported_request(RUN_IDS[1]))
        save_request, save_sink = harness.save_dispatcher.calls[-1]
        save_sink(saved_outcome(save_request))
    elif seam == "current-initial":
        drive_to_bridge(harness, imported=True)
        coordinator = harness.coordinator_instance
    elif seam == "current-reanalysis":
        harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
        coordinator = harness.coordinator()
        assert (
            coordinator.start_reanalysis(reanalysis_request()).status
            is OperationStatus.SUBMITTED
        )
    elif seam == "late-initial":
        submit_entered = Event()
        submit_release = Event()
        harness.scheduler.hook.entered = submit_entered
        harness.scheduler.hook.release = submit_release
        coordinator = harness.coordinator()
        coordinator.start_imported(imported_request())
        save_request, save_sink = harness.save_dispatcher.calls[0]
        submit_worker = Thread(
            target=lambda: save_sink(saved_outcome(save_request))
        )
    else:
        harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
        submit_entered = Event()
        submit_release = Event()
        harness.scheduler.hook.entered = submit_entered
        harness.scheduler.hook.release = submit_release
        coordinator = harness.coordinator()
        submit_worker = Thread(
            target=lambda: coordinator.start_reanalysis(
                reanalysis_request()
            )
        )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    diagnostic_entered, diagnostic_release = (
        _install_blocked_failing_diagnostic(harness)
    )
    stop_waiting = Event()
    if submit_worker is not None:
        original_wait = coordinator._condition.wait

        def observed_wait(timeout):
            stop_waiting.set()
            return original_wait(timeout)

        monkeypatch.setattr(
            coordinator._condition,
            "wait",
            observed_wait,
        )
    stop_results = []
    stop_worker = Thread(
        target=lambda: stop_results.append(coordinator.stop_all(2.0))
    )
    if after_deadline:
        harness.monotonic.values.append(0.0)
    observed_state = None
    observed_blockers = None
    tracked_after_report = None
    submit_started = False
    stop_started = False
    try:
        if submit_worker is not None:
            submit_worker.start()
            submit_started = True
            assert submit_entered is not None
            assert submit_entered.wait(2)
        stop_worker.start()
        stop_started = True
        if submit_worker is not None:
            assert stop_waiting.wait(2)
            assert submit_release is not None
            submit_release.set()
        assert cancel_entered.wait(2)
        cancel_release.set()
        assert diagnostic_entered.wait(2)
        with coordinator._lock:
            observed_state = (
                coordinator._lifecycle,
                coordinator._restart_required,
                len(coordinator._stop_errors),
                len(coordinator._child_cancel_actions),
            )
            observed_blockers = (
                frozenset(
                    action.attempt_id
                    for action in coordinator._child_cancel_actions
                    if action.attempt_id is not None
                ),
                frozenset(coordinator._attempts),
            )
        if after_deadline:
            harness.monotonic.values.append(3.0)
        _settle_reusable_bridge_attempt(
            harness,
            initial="initial" in seam,
        )
        if after_deadline:
            harness.scheduler.complete_report(
                terminal_report(
                    RUN_IDS[1],
                    attempt_id=ATTEMPT_IDS[1],
                    state=AttemptState.CANCELED,
                    judgment=None,
                    result_json=None,
                    code="USER_CANCELED",
                    summary="other OneShot settled without cancellation",
                )
            )
        with coordinator._lock:
            tracked_after_report = len(
                coordinator._child_cancel_actions
            )
    finally:
        if submit_release is not None:
            submit_release.set()
        cancel_release.set()
        diagnostic_release.set()
        if submit_started and submit_worker is not None:
            submit_worker.join(2)
        if stop_started:
            _join_stop_worker(harness, coordinator, stop_worker)

    assert submit_worker is None or not submit_worker.is_alive()
    assert not stop_worker.is_alive()
    assert observed_state == (
        CoordinatorLifecycle.STOPPING,
        False,
        1,
        1,
    )
    assert tracked_after_report == 1
    assert len(stop_results) == 1
    report = stop_results[0]
    assert report.status is StopStatus.SETTLED
    assert len(report.errors) == 1
    assert report.restart_required is False
    snapshot = coordinator.snapshot()
    assert snapshot.lifecycle is CoordinatorLifecycle.OPEN
    assert snapshot.restart_required is False
    assert len(harness.diagnostics.calls) == 1
    assert len(harness.scheduler.cancel_requests) == 1
    if after_deadline:
        assert observed_blockers == (
            frozenset((ATTEMPT_IDS[0],)),
            frozenset(ATTEMPT_IDS[:2]),
        )
        assert report.timed_out is False
        assert "cancel failed" in report.errors[0]
        assert (
            report.pending_ingress_run_ids,
            report.pending_publication_run_ids,
            report.pending_analysis_attempt_ids,
        ) == ((), (), ())
        assert (
            snapshot.ingress_run_ids,
            snapshot.accepted_active_run_ids,
            snapshot.analysis_attempt_ids,
            snapshot.result_operation_attempt_ids,
            snapshot.pending_publication_run_ids,
        ) == ((), (), (), (), ())
        assert report.canceled_accepted_run_ids == tuple(
            sorted(RUN_IDS[:2])
        )
        cancel_request = harness.scheduler.cancel_requests[0]
        assert cancel_request.run_id == RUN_IDS[0]
        assert (
            cancel_request.analysis_attempt_id == ATTEMPT_IDS[0]
        )
        assert (
            len(harness.scheduler.initial_calls),
            len(harness.scheduler.reanalysis_calls),
        ) == (2, 0)
        assert harness.monotonic.calls == 1
        assert tuple(harness.monotonic.values) == (3.0,)

    coordinator.begin_shutdown()
    shutdown = coordinator.finish_shutdown(-1.0)
    assert shutdown.status is CoordinatorShutdownStatus.CLOSED
    assert shutdown.errors == ()
    assert shutdown.restart_required is False
    assert len(harness.scheduler.cancel_requests) == 1


def test_reusable_bridge_cancel_exception_isolates_other_oneshot() -> None:
    harness = Harness()
    harness.scheduler.bridge_results.extend(
        [
            harness.scheduler.accepted_bridge_result(
                RUN_IDS[0],
                ATTEMPT_IDS[0],
            ),
            harness.scheduler.accepted_bridge_result(
                RUN_IDS[1],
                ATTEMPT_IDS[1],
            ),
        ]
    )
    coordinator = harness.coordinator()
    for run_id in RUN_IDS[:2]:
        coordinator.start_imported(imported_request(run_id))
        save_request, save_sink = harness.save_dispatcher.calls[-1]
        save_sink(saved_outcome(save_request))
    original_cancel = harness.scheduler.cancel
    all_cancels_dispatched = Event()

    def selective_cancel(request):
        harness.scheduler.cancel_exception = (
            RuntimeError("first OneShot cancel failed")
            if request.analysis_attempt_id == ATTEMPT_IDS[0]
            else None
        )
        try:
            return original_cancel(request)
        finally:
            if len(harness.scheduler.cancel_requests) == 2:
                all_cancels_dispatched.set()

    harness.scheduler.cancel = selective_cancel
    stop_results = []
    stop_worker = Thread(
        target=lambda: stop_results.append(coordinator.stop_all(2.0))
    )
    stop_started = False
    try:
        stop_worker.start()
        stop_started = True
        assert all_cancels_dispatched.wait(2)
        for run_id in RUN_IDS[:2]:
            harness.scheduler.complete_report(
                terminal_report(
                    run_id,
                    attempt_id=(
                        ATTEMPT_IDS[0]
                        if run_id == RUN_IDS[0]
                        else ATTEMPT_IDS[1]
                    ),
                    state=AttemptState.CANCELED,
                    judgment=None,
                    result_json=None,
                    code="USER_CANCELED",
                    summary="OneShot settled",
                )
            )
    finally:
        if stop_started:
            _join_stop_worker(harness, coordinator, stop_worker)

    assert not stop_worker.is_alive()
    assert len(stop_results) == 1
    assert stop_results[0].status is StopStatus.SETTLED
    assert len(stop_results[0].errors) == 1
    assert stop_results[0].restart_required is False
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.OPEN
    assert [
        request.analysis_attempt_id
        for request in harness.scheduler.cancel_requests
    ] == list(ATTEMPT_IDS[:2])
    assert len(harness.diagnostics.calls) == 1


@pytest.mark.parametrize("attempt_kind", ["initial", "reanalysis"])
def test_reusable_bridge_cancel_exception_timeout_retains_exact_blocker(
    attempt_kind: str,
) -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError("cancel failed")
    if attempt_kind == "initial":
        drive_to_bridge(harness, imported=True)
        coordinator = harness.coordinator_instance
    else:
        harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
        coordinator = harness.coordinator()
        assert (
            coordinator.start_reanalysis(reanalysis_request()).status
            is OperationStatus.SUBMITTED
        )

    report = coordinator.stop_all(0.0)

    assert report.status is StopStatus.INCOMPLETE
    assert report.pending_analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert len(report.errors) == 1
    assert report.restart_required is True
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED
    assert len(harness.scheduler.cancel_requests) == 1


@pytest.mark.parametrize("attempt_kind", ["initial", "reanalysis"])
def test_reusable_bridge_noncommitted_report_retains_restart_authority(
    attempt_kind: str,
) -> None:
    harness = Harness()
    harness.scheduler.cancel_exception = RuntimeError("cancel failed")
    if attempt_kind == "initial":
        drive_to_bridge(harness, imported=True)
        coordinator = harness.coordinator_instance
    else:
        harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
        coordinator = harness.coordinator()
        assert (
            coordinator.start_reanalysis(reanalysis_request()).status
            is OperationStatus.SUBMITTED
        )
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release
    stop_results = []
    stop_worker = Thread(
        target=lambda: stop_results.append(coordinator.stop_all(2.0))
    )
    stop_started = False
    try:
        stop_worker.start()
        stop_started = True
        assert cancel_entered.wait(2)
        _settle_reusable_bridge_attempt(
            harness,
            initial=attempt_kind == "initial",
            committed=False,
        )
    finally:
        cancel_release.set()
        if stop_started:
            _join_stop_worker(harness, coordinator, stop_worker)

    assert not stop_worker.is_alive()
    assert len(stop_results) == 1
    assert stop_results[0].status is (
        StopStatus.INCOMPLETE
        if attempt_kind == "initial"
        else StopStatus.CLOSED
    )
    assert len(stop_results[0].errors) == 1
    assert stop_results[0].restart_required is True
    assert coordinator.snapshot().lifecycle is CoordinatorLifecycle.CLOSED
    assert len(harness.scheduler.cancel_requests) == 1


@pytest.mark.parametrize(
    ("case", "release_calls", "worker_assertions"),
    [
        (
            test_reusable_bridge_cancel_exception_settles_and_reopens,
            (
                "submit_release.set()",
                "cancel_release.set()",
                "diagnostic_release.set()",
            ),
            (
                "assert submit_worker is None or not submit_worker.is_alive()",
                "assert not stop_worker.is_alive()",
            ),
        ),
        (
            test_reusable_bridge_cancel_exception_isolates_other_oneshot,
            (),
            ("assert not stop_worker.is_alive()",),
        ),
        (
            test_reusable_bridge_noncommitted_report_retains_restart_authority,
            ("cancel_release.set()",),
            ("assert not stop_worker.is_alive()",),
        ),
    ],
)
def test_reusable_bridge_thread_harness_guarantees_cleanup(
    case,
    release_calls: tuple[str, ...],
    worker_assertions: tuple[str, ...],
) -> None:
    lines = inspect.getsource(case).splitlines()
    cleanup_try = next(
        index for index, line in enumerate(lines) if line == "    try:"
    )
    cleanup_finally = next(
        index
        for index, line in enumerate(lines)
        if index > cleanup_try and line == "    finally:"
    )
    worker_starts = [
        index for index, line in enumerate(lines) if ".start()" in line
    ]

    assert worker_starts
    assert all(
        cleanup_try < index < cleanup_finally for index in worker_starts
    )

    cleanup_tail = "\n".join(lines[cleanup_finally:])
    join_offsets = [
        offset
        for token in (".join(", "_join_stop_worker(")
        if (offset := cleanup_tail.find(token)) >= 0
    ]
    assert join_offsets
    first_join = min(join_offsets)
    for release_call in release_calls:
        release_offset = cleanup_tail.find(release_call)
        assert 0 <= release_offset < first_join
    for worker_assertion in worker_assertions:
        assert (
            f"    {worker_assertion}"
            in lines[cleanup_finally + 1 :]
        )
