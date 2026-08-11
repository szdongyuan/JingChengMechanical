from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread

import pytest

from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AudioStorageKind,
)
from app.service.run_persistence import (
    AttemptKind,
    AttemptState,
    BridgeAuthorityStatus,
    BridgeCancelResult,
    BridgeCancelStatus,
    BridgeSubmissionResult,
    BridgeSubmissionStatus,
    BridgeTerminalReport,
    BridgeTerminalStatus,
    InitialAnalysisRequest,
    Judgment,
    TransactionResult,
    TransactionState,
)
from app.service.multi_run_coordinator_mvp import (
    CoordinatorLifecycle,
    CoordinatorPhase,
    HistoryFactKind,
    StartStatus,
    TerminalConclusion,
    TerminalErrorCategory,
    TriggerSource,
)

from ._support import (
    ATTEMPT_IDS,
    NOW,
    RUN_IDS,
    Harness,
    captured_request,
    completed_capture,
    imported_request,
    saved_outcome,
)


def _committed(value: bool = True) -> TransactionResult[bool]:
    return TransactionResult(TransactionState.COMMITTED, value)


def terminal_report(
    run_id: str,
    attempt_id: str = ATTEMPT_IDS[0],
    *,
    status: BridgeTerminalStatus = BridgeTerminalStatus.COMMITTED,
    state: AttemptState | None = AttemptState.SUCCEEDED,
    judgment: Judgment | None = Judgment.PASS,
    result_json: str | None = '{"judgment":"PASS","result":{}}',
    code: str = "ANALYSIS_SUCCEEDED",
    summary: str | None = None,
    restart_required: bool = False,
) -> BridgeTerminalReport:
    persistence = None
    terminal_at = None
    if status is BridgeTerminalStatus.COMMITTED:
        persistence = _committed()
        terminal_at = "2026-07-27T09:00:00.000000Z"
    elif status is BridgeTerminalStatus.LATE_IGNORED:
        persistence = _committed(False)
        terminal_at = "2026-07-27T09:00:00.000000Z"
    elif status is BridgeTerminalStatus.DB_FAILED:
        persistence = TransactionResult(
            TransactionState.ROLLED_BACK,
            None,
            reason="database failed",
        )
    return BridgeTerminalReport(
        status=status,
        run_id=run_id,
        analysis_attempt_id=attempt_id,
        attempt_kind=AttemptKind.INITIAL,
        attempt_state=state,
        judgment=judgment,
        result_payload_json=result_json,
        code=code,
        summary=summary,
        terminal_at=terminal_at,
        persistence_result=persistence,
        restart_required=restart_required,
    )


def drive_to_bridge(
    harness: Harness,
    *,
    imported: bool = False,
    captured_source: TriggerSource = TriggerSource.MANUAL,
) -> None:
    coordinator = harness.coordinator()
    if imported:
        coordinator.start_imported(imported_request())
    else:
        coordinator.start_captured(
            captured_request(source=captured_source)
        )
        reservation = harness.resident.register_calls[0]
        harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    save_sink(saved_outcome(save_request))
    harness.coordinator_instance = coordinator


def begin_pending_contender_during_bridge_submit(
    harness: Harness,
    *,
    source: TriggerSource = TriggerSource.MANUAL,
):
    submit_entered = Event()
    submit_release = Event()
    harness.scheduler.hook.entered = submit_entered
    harness.scheduler.hook.release = submit_release
    coordinator = harness.coordinator()
    request = captured_request(source=source)
    coordinator.start_captured(request)
    reservation = harness.resident.register_calls[0]
    harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    save_thread = Thread(
        target=lambda: save_sink(saved_outcome(save_request))
    )

    save_thread.start()
    assert submit_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[request.run_id]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ANALYSIS_FAILED,
        "terminal contender during bridge submit",
    )
    submit_release.set()
    return coordinator, context, save_thread


@pytest.mark.parametrize("imported", [False, True])
def test_initial_handoff_uses_exact_bridge_request_and_bridge_identity(
    imported: bool,
) -> None:
    harness = Harness()

    drive_to_bridge(harness, imported=imported)

    coordinator = harness.coordinator_instance
    request, _ = harness.scheduler.initial_calls[0]
    assert type(request) is InitialAnalysisRequest
    assert request.run_id == request.audio_reference.audio_data_id.replace(
        request.audio_reference.audio_data_id, request.run_id
    )
    assert request.audio_reference.storage_kind is (
        AudioStorageKind.IMPORTED_FINAL_AUDIO
        if imported
        else AudioStorageKind.CAPTURED_FINAL_WAV
    )
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert coordinator.snapshot().accepted_active_run_ids == (request.run_id,)
    assert not hasattr(harness.persistence, "create_queued_attempt")
    assert harness.persistence.terminal_calls == []


def test_initial_handoff_uses_durable_byte_size_revision_contract() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    request = captured_request()
    coordinator.start_captured(request)
    reservation = harness.resident.register_calls[0]
    harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    outcome = saved_outcome(save_request)
    audio = outcome.persisted_audio
    assert audio is not None
    audio = replace(
        audio,
        byte_size=4_096,
        content_revision=7,
    )

    save_sink(replace(outcome, persisted_audio=audio))

    bridge_request, _ = harness.scheduler.initial_calls[0]
    assert type(bridge_request) is InitialAnalysisRequest
    assert bridge_request.audio_reference.content_revision == (
        f"{audio.audio_id}:4096"
    )
    assert bridge_request.audio_reference.content_revision != (
        f"{audio.audio_id}:{audio.content_revision}"
    )
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert coordinator.snapshot().restart_required is False


def test_pending_contender_after_acquisition_is_bridge_cancel_only() -> None:
    harness = Harness()
    cancel_entered = Event()
    cancel_release = Event()
    harness.scheduler.cancel_hook.entered = cancel_entered
    harness.scheduler.cancel_hook.release = cancel_release

    coordinator, context, save_thread = (
        begin_pending_contender_during_bridge_submit(
            harness,
            source=TriggerSource.PLC,
        )
    )

    assert cancel_entered.wait(2)
    assert len(harness.scheduler.cancel_requests) == 1
    cancel_request = harness.scheduler.cancel_requests[0]
    assert cancel_request.run_id == context.run_id
    assert cancel_request.analysis_attempt_id == ATTEMPT_IDS[0]
    assert cancel_request.origin is AnalysisCancelOrigin.USER_STOP
    assert cancel_request.reason == "terminal contender during bridge submit"
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert harness.persistence.terminal_calls == []
    assert not [
        fact
        for fact in harness.projector.calls
        if fact.kind
        in {
            HistoryFactKind.ANALYZING,
            HistoryFactKind.RUN_TERMINATED,
        }
    ]

    cancel_release.set()
    save_thread.join(2)

    assert not save_thread.is_alive()
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert harness.persistence.terminal_calls == []
    report = terminal_report(
        context.run_id,
        state=AttemptState.CANCELED,
        judgment=None,
        result_json=None,
        code="USER_CANCELED",
    )
    harness.scheduler.complete_report(report)
    harness.scheduler.complete_report(report)

    assert coordinator.snapshot().analysis_attempt_ids == ()
    assert coordinator.snapshot().restart_required is True
    terminal_history = [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert (
        terminal_history[0].projection.conclusion
        is TerminalConclusion.MANUAL_STOPPED
    )
    assert len(harness.plc.publish_calls) == 1
    assert len(harness.scheduler.cancel_requests) == 1
    assert harness.persistence.terminal_calls == []
    duplicate_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge initial duplicate report"
    ]
    assert len(duplicate_diagnostics) == 1
    assert len(duplicate_diagnostics[0].message.encode("utf-8")) <= 512


def test_contradictory_duplicate_report_closes_admission_once() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    original = terminal_report(run_id)
    contradictory = terminal_report(
        run_id,
        state=AttemptState.CANCELED,
        judgment=None,
        result_json=None,
        code="USER_CANCELED",
    )

    harness.scheduler.complete_report(original)
    for _ in range(3):
        harness.scheduler.complete_report(contradictory)

    terminal_history = [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert terminal_history[0].projection.conclusion is TerminalConclusion.OK
    assert len(harness.plc.publish_calls) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.coordinator_instance.snapshot().restart_required is True
    admission = harness.coordinator_instance.start_imported(
        imported_request(RUN_IDS[1])
    )
    assert admission.status is StartStatus.INGRESS_CLOSED
    assert len(
        [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "bridge initial duplicate report"
        ]
    ) == 1


def test_consumed_bound_sink_rejects_repeated_mismatched_reports_once() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    original = terminal_report(run_id)
    harness.scheduler.complete_report(original)

    _, sink = harness.scheduler.initial_calls[0]
    mismatched = terminal_report(run_id, ATTEMPT_IDS[1])
    sink(mismatched)
    assert harness.coordinator_instance.snapshot().restart_required is True
    assert len(
        [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "bridge initial duplicate report"
        ]
    ) == 1
    sink(original)
    sink(mismatched)

    assert harness.coordinator_instance.snapshot().restart_required is True
    terminal_history = [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert terminal_history[0].projection.conclusion is TerminalConclusion.OK
    assert len(harness.plc.publish_calls) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    duplicate_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge initial duplicate report"
    ]
    assert len(duplicate_diagnostics) == 1
    assert len(duplicate_diagnostics[0].message.encode("utf-8")) <= 512
    admission = harness.coordinator_instance.start_imported(
        imported_request(RUN_IDS[1])
    )
    assert admission.status is StartStatus.INGRESS_CLOSED


@pytest.mark.parametrize(
    "cancel_failure",
    ["exception", "malformed", "rejected"],
)
def test_pending_contender_cancel_failure_quarantines_without_persistence(
    cancel_failure: str,
) -> None:
    harness = Harness()
    if cancel_failure == "exception":
        harness.scheduler.cancel_exception = RuntimeError("cancel failed")
    elif cancel_failure == "malformed":
        harness.scheduler.cancel_result = object()
    else:
        harness.scheduler.cancel_result = BridgeCancelResult(
            status=BridgeCancelStatus.REJECTED,
            run_id=captured_request().run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            origin=AnalysisCancelOrigin.USER_STOP,
            code="BRIDGE_CANCEL_REJECTED",
            reason="cancel rejected",
            receipt=None,
        )

    coordinator, context, save_thread = (
        begin_pending_contender_during_bridge_submit(harness)
    )
    save_thread.join(2)

    assert not save_thread.is_alive()
    assert len(harness.scheduler.cancel_requests) == 1
    assert coordinator.snapshot().restart_required is True
    assert coordinator.snapshot().analysis_attempt_ids == ()
    assert harness.persistence.terminal_calls == []
    harness.scheduler.complete_report(
        terminal_report(
            context.run_id,
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
        )
    )
    assert not [
        fact
        for fact in harness.projector.calls
        if fact.kind
        in {
            HistoryFactKind.ANALYZING,
            HistoryFactKind.RUN_TERMINATED,
        }
    ]
    assert harness.plc.publish_calls == []


@pytest.mark.parametrize("report_failure", ["non_committed", "malformed"])
def test_pending_contender_report_failure_is_fail_closed(
    report_failure: str,
) -> None:
    harness = Harness()
    coordinator, context, save_thread = (
        begin_pending_contender_during_bridge_submit(harness)
    )
    save_thread.join(2)
    assert not save_thread.is_alive()
    if report_failure == "non_committed":
        report = terminal_report(
            context.run_id,
            status=BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
            state=None,
            judgment=None,
            result_json=None,
            code="ATTEMPT_TERMINAL_CALL_FAILED",
            restart_required=True,
        )
    else:
        report = terminal_report(
            context.run_id,
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
        )
        object.__setattr__(report, "attempt_state", AttemptState.SUCCEEDED)

    harness.scheduler.complete_report(report)

    assert coordinator.snapshot().restart_required is True
    assert coordinator.snapshot().analysis_attempt_ids == ()
    assert harness.persistence.terminal_calls == []
    assert not [
        fact
        for fact in harness.projector.calls
        if fact.kind
        in {
            HistoryFactKind.ANALYZING,
            HistoryFactKind.RUN_TERMINATED,
        }
    ]
    assert harness.plc.publish_calls == []


def test_sink_before_return_is_held_and_consumed_once() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    report = terminal_report(run_id)
    harness.scheduler.bridge_results.append(
        harness.scheduler.accepted_bridge_result(
            run_id,
            early=True,
        )
    )
    harness.scheduler.synchronous_report = report

    drive_to_bridge(harness)
    harness.scheduler.complete_report(report)

    terminal_history = [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.coordinator_instance.snapshot().analysis_attempt_ids == ()


def test_held_first_report_is_claimed_before_concurrent_duplicate() -> None:
    harness = Harness()
    request = captured_request(source=TriggerSource.PLC)
    first = terminal_report(request.run_id)
    contradictory = terminal_report(
        request.run_id,
        state=AttemptState.CANCELED,
        judgment=None,
        result_json=None,
        code="USER_CANCELED",
    )
    harness.scheduler.bridge_results.append(
        harness.scheduler.accepted_bridge_result(
            request.run_id,
            early=True,
        )
    )
    harness.scheduler.synchronous_report = first
    coordinator = harness.coordinator()
    original_consume = coordinator._consume_bridge_initial_report
    first_consume_entered = Event()
    first_consume_release = Event()
    consume_calls = 0

    def blocked_first_consume(context, report):
        nonlocal consume_calls
        consume_calls += 1
        if consume_calls == 1:
            first_consume_entered.set()
            assert first_consume_release.wait(2)
        return original_consume(context, report)

    coordinator._consume_bridge_initial_report = blocked_first_consume
    coordinator.start_captured(request)
    reservation = harness.resident.register_calls[0]
    harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    save_thread = Thread(
        target=lambda: save_sink(saved_outcome(save_request))
    )

    save_thread.start()
    assert first_consume_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[request.run_id]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ANALYSIS_FAILED,
        "competing coordinator terminal",
    )
    _, terminal_sink = harness.scheduler.initial_calls[0]
    terminal_sink(contradictory)
    terminal_sink(first)
    terminal_sink(contradictory)

    assert harness.persistence.terminal_calls == []
    assert coordinator.snapshot().restart_required is True
    assert not [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.plc.publish_calls == []
    assert len(
        [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "bridge initial duplicate report"
        ]
    ) == 1

    first_consume_release.set()
    save_thread.join(2)

    assert not save_thread.is_alive()
    terminal_history = [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert terminal_history[0].projection.conclusion is TerminalConclusion.OK
    assert len(harness.plc.publish_calls) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []


def test_async_report_claim_excludes_coordinator_terminal_contender() -> None:
    harness = Harness()
    request = captured_request(source=TriggerSource.PLC)
    first = terminal_report(request.run_id)

    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    coordinator = harness.coordinator_instance
    original_map = coordinator._terminal_from_bridge_report
    mapping_entered = Event()
    mapping_release = Event()

    def blocked_map(context, report):
        mapping_entered.set()
        assert mapping_release.wait(2)
        return original_map(context, report)

    coordinator._terminal_from_bridge_report = blocked_map
    _, terminal_sink = harness.scheduler.initial_calls[0]
    report_thread = Thread(target=lambda: terminal_sink(first))

    report_thread.start()
    assert mapping_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[request.run_id]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ANALYSIS_FAILED,
        "competing coordinator terminal",
    )

    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert not [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]

    mapping_release.set()
    report_thread.join(2)

    assert not report_thread.is_alive()
    terminal_history = [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert terminal_history[0].projection.conclusion is TerminalConclusion.OK
    assert len(harness.plc.publish_calls) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []


def test_non_committed_report_claim_excludes_coordinator_terminal() -> None:
    harness = Harness()
    request = captured_request(source=TriggerSource.PLC)
    report = terminal_report(
        request.run_id,
        status=BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
        state=None,
        judgment=None,
        result_json=None,
        code="ATTEMPT_TERMINAL_CALL_FAILED",
        restart_required=True,
    )

    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    coordinator = harness.coordinator_instance
    original_fail_closed = coordinator._fail_closed_bridge_context
    quarantine_entered = Event()
    quarantine_release = Event()

    def blocked_fail_closed(context, stage, reason):
        quarantine_entered.set()
        assert quarantine_release.wait(2)
        return original_fail_closed(context, stage, reason)

    coordinator._fail_closed_bridge_context = blocked_fail_closed
    _, terminal_sink = harness.scheduler.initial_calls[0]
    report_thread = Thread(target=lambda: terminal_sink(report))

    report_thread.start()
    assert quarantine_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[request.run_id]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ANALYSIS_FAILED,
        "competing coordinator terminal",
    )

    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []

    quarantine_release.set()
    report_thread.join(2)

    assert not report_thread.is_alive()
    assert coordinator.snapshot().restart_required is True
    assert not [
        fact
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.plc.publish_calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert len(
        [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "bridge initial report"
        ]
    ) == 1


def test_post_linearization_sink_can_physically_run_before_return_once() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    harness.scheduler.synchronous_report = terminal_report(run_id)

    drive_to_bridge(harness)

    terminal_history = [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_history) == 1
    assert harness.coordinator_instance.snapshot().restart_required is False


@pytest.mark.parametrize(
    "mutation",
    [
        "state_unknown_with_committed",
        "accepted_early_with_non_committed",
        "rejected_with_non_committed",
        "state_unknown_with_mismatched_code",
        "rejected_with_mismatched_committed_code",
        "rejected_with_mismatched_committed_summary",
    ],
)
def test_contradictory_early_result_report_pairs_fail_closed(
    mutation: str,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    accepted = harness.scheduler.accepted_bridge_result(run_id)
    non_committed = terminal_report(
        run_id,
        status=BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
        state=None,
        judgment=None,
        result_json=None,
        code="ATTEMPT_TERMINAL_CALL_FAILED",
        restart_required=True,
    )
    if mutation == "state_unknown_with_committed":
        result = BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=True,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="ATTEMPT_TERMINAL_CALL_FAILED",
            reason=None,
            submit_receipt=accepted.submit_receipt,
        )
        report = terminal_report(run_id)
    elif mutation == "accepted_early_with_non_committed":
        result = harness.scheduler.accepted_bridge_result(
            run_id,
            early=True,
        )
        report = non_committed
    elif mutation == "rejected_with_non_committed":
        result = BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=False,
            code="ANALYSIS_SCHEDULER_SUBMIT_FAILED",
            reason=None,
            submit_receipt=None,
        )
        report = non_committed
    elif mutation == "state_unknown_with_mismatched_code":
        result = BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=True,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="ANALYSIS_DB_STATE_UNKNOWN",
            reason=None,
            submit_receipt=accepted.submit_receipt,
        )
        report = non_committed
    else:
        result = BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=False,
            code="ANALYSIS_SCHEDULER_SUBMIT_FAILED",
            reason=None,
            submit_receipt=None,
        )
        report = terminal_report(
            run_id,
            state=AttemptState.FAILED,
            judgment=None,
            result_json=None,
            code=(
                "ANALYSIS_QUEUE_FULL"
                if mutation == "rejected_with_mismatched_committed_code"
                else "ANALYSIS_SCHEDULER_SUBMIT_FAILED"
            ),
            summary=(
                None
                if mutation == "rejected_with_mismatched_committed_code"
                else "unexpected summary"
            ),
        )
    harness.scheduler.bridge_results.append(result)
    harness.scheduler.synchronous_report = report

    drive_to_bridge(harness, captured_source=TriggerSource.PLC)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []


def test_acquired_rejected_result_consumes_exact_committed_report() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=False,
            code="ANALYSIS_SCHEDULER_SUBMIT_FAILED",
            reason=None,
            submit_receipt=None,
        )
    )
    harness.scheduler.synchronous_report = terminal_report(
        run_id,
        state=AttemptState.FAILED,
        judgment=None,
        result_json=None,
        code="ANALYSIS_SCHEDULER_SUBMIT_FAILED",
    )

    drive_to_bridge(harness)

    assert harness.coordinator_instance.snapshot().restart_required is False
    terminal = [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal) == 1
    assert terminal[0].projection.conclusion is TerminalConclusion.ERROR
    assert harness.persistence.terminal_calls == []


def test_acquired_state_unknown_consumes_exact_non_committed_report() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    accepted = harness.scheduler.accepted_bridge_result(run_id)
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=True,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="ATTEMPT_TERMINAL_CALL_FAILED",
            reason=None,
            submit_receipt=accepted.submit_receipt,
        )
    )
    harness.scheduler.synchronous_report = terminal_report(
        run_id,
        status=BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
        state=None,
        judgment=None,
        result_json=None,
        code="ATTEMPT_TERMINAL_CALL_FAILED",
        restart_required=True,
    )

    drive_to_bridge(harness, captured_source=TriggerSource.PLC)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []


def test_async_sink_at_acquired_install_point_sees_only_atomic_state() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    request = captured_request()
    report = terminal_report(request.run_id)
    coordinator.start_captured(request)
    reservation = harness.resident.register_calls[0]
    harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    original_install = coordinator._install_acquired_initial_result
    install_entered = Event()
    install_release = Event()

    def blocked_install(context, action, result, attempt_id):
        install_entered.set()
        assert install_release.wait(2)
        return original_install(context, action, result, attempt_id)

    coordinator._install_acquired_initial_result = blocked_install
    save_thread = Thread(target=lambda: save_sink(saved_outcome(save_request)))
    save_thread.start()
    assert install_entered.wait(2)
    _, terminal_sink = harness.scheduler.initial_calls[0]

    terminal_sink(report)

    with coordinator._lock:
        context = coordinator._runs[request.run_id]
        assert context.bridge_submission_reconciled is False
        assert context.early_analysis_report is report
        assert context.attempt_id is None
        assert context.phase is CoordinatorPhase.ANALYSIS_SUBMITTING

    install_release.set()
    save_thread.join(2)

    assert not save_thread.is_alive()
    assert coordinator.snapshot().restart_required is False
    assert coordinator.snapshot().analysis_attempt_ids == ()
    assert len(
        [
            fact
            for fact in harness.projector.calls
            if fact.kind is HistoryFactKind.RUN_TERMINATED
        ]
    ) == 1


def test_validated_acquired_install_gap_preserves_bridge_sole_writer() -> None:
    harness = Harness()
    coordinator = harness.coordinator()
    request = captured_request(source=TriggerSource.PLC)
    coordinator.start_captured(request)
    reservation = harness.resident.register_calls[0]
    harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    original_install = coordinator._install_acquired_initial_result
    install_entered = Event()
    install_release = Event()

    def blocked_install(context, action, result, attempt_id):
        install_entered.set()
        assert install_release.wait(2)
        return original_install(context, action, result, attempt_id)

    coordinator._install_acquired_initial_result = blocked_install
    save_thread = Thread(target=lambda: save_sink(saved_outcome(save_request)))

    save_thread.start()
    assert install_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[request.run_id]
    coordinator._terminalize(
        context,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ANALYSIS_FAILED,
        "terminal contender after acquired result",
    )

    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_requests == []

    install_release.set()
    save_thread.join(2)

    assert not save_thread.is_alive()
    assert len(harness.scheduler.cancel_requests) == 1
    assert harness.scheduler.cancel_requests[0].analysis_attempt_id == (
        ATTEMPT_IDS[0]
    )
    assert coordinator.snapshot().analysis_attempt_ids == (ATTEMPT_IDS[0],)
    assert harness.persistence.terminal_calls == []

    harness.scheduler.complete_report(
        terminal_report(
            request.run_id,
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="USER_CANCELED",
        )
    )

    assert len(
        [
            fact
            for fact in harness.projector.calls
            if fact.kind is HistoryFactKind.RUN_TERMINATED
        ]
    ) == 1
    assert len(harness.plc.publish_calls) == 1
    assert harness.persistence.terminal_calls == []


def test_result_report_identity_mismatch_fails_closed() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    harness.scheduler.synchronous_report = terminal_report(
        run_id,
        ATTEMPT_IDS[1],
    )

    drive_to_bridge(harness)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.persistence.terminal_calls == []


def test_malformed_early_report_permanently_quarantines_context() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    malformed = terminal_report(run_id)
    object.__setattr__(malformed, "result_payload_json", "x")
    harness.scheduler.synchronous_report = malformed

    drive_to_bridge(harness, captured_source=TriggerSource.PLC)

    assert harness.coordinator_instance.snapshot().analysis_attempt_ids == ()
    harness.scheduler.complete_report(malformed)
    harness.scheduler.complete_report(terminal_report(run_id))

    snapshot = harness.coordinator_instance.snapshot()
    assert snapshot.restart_required is True
    assert snapshot.analysis_attempt_ids == ()
    assert not [
        call
        for call in harness.projector.calls
        if call.kind
        in {
            HistoryFactKind.ANALYZING,
            HistoryFactKind.RUN_TERMINATED,
        }
    ]
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []
    assert harness.persistence.terminal_calls == []
    assert len(harness.scheduler.initial_calls) == 1
    assert len(
        [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "bridge initial report"
        ]
    ) == 1
    with harness.coordinator_instance._lock:
        context = harness.coordinator_instance._runs[run_id]
        quarantine = context.bridge_terminal_quarantine
        assert quarantine is not None
        assert quarantine.run_id == run_id
        assert context.terminal_claimed is True
        assert context.history_allowed is False


@pytest.mark.parametrize("with_held_report", [False, True])
def test_unknown_submission_latches_restart_without_terminal_write(
    with_held_report: bool,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=True,
            code="ATTEMPT_QUEUE_CALL_FAILED",
            reason=None,
            submit_receipt=None,
        )
    )
    if with_held_report:
        harness.scheduler.synchronous_report = terminal_report(run_id)

    drive_to_bridge(harness)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.initial_calls
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]


@pytest.mark.parametrize("with_held_report", [False, True])
def test_not_acquired_uses_only_pre_bridge_terminal_persistence(
    with_held_report: bool,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=False,
            code="ANALYSIS_RUN_NOT_FOUND",
            reason=None,
            submit_receipt=None,
        )
    )
    if with_held_report:
        harness.scheduler.synchronous_report = terminal_report(run_id)

    drive_to_bridge(harness)

    if with_held_report:
        assert harness.persistence.terminal_calls == []
        assert harness.coordinator_instance.snapshot().restart_required is True
        assert not [
            call
            for call in harness.projector.calls
            if call.kind is HistoryFactKind.RUN_TERMINATED
        ]
    else:
        assert len(harness.persistence.terminal_calls) == 1
        assert harness.persistence.terminal_calls[0].attempt_id is None
        assert harness.coordinator_instance.snapshot().restart_required is False


@pytest.mark.parametrize(
    ("code", "restart_required", "lifecycle", "admission_status"),
    [
        (
            "ANALYSIS_DB_CLOSED",
            True,
            CoordinatorLifecycle.CLOSED,
            StartStatus.INGRESS_CLOSED,
        ),
        (
            "ANALYSIS_ADMISSION_CLOSED",
            False,
            CoordinatorLifecycle.OPEN,
            StartStatus.ACCEPTED_ACTIVE,
        ),
    ],
)
def test_not_acquired_restart_matrix_preserves_pre_bridge_settlement(
    code: str,
    restart_required: bool,
    lifecycle: CoordinatorLifecycle,
    admission_status: StartStatus,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=restart_required,
            code=code,
            reason=None,
            submit_receipt=None,
        )
    )

    drive_to_bridge(harness)

    snapshot = harness.coordinator_instance.snapshot()
    assert snapshot.restart_required is restart_required
    assert snapshot.lifecycle is lifecycle
    assert len(harness.persistence.terminal_calls) == 1
    assert harness.persistence.terminal_calls[0].attempt_id is None
    assert len(
        [
            call
            for call in harness.projector.calls
            if call.kind is HistoryFactKind.RUN_TERMINATED
        ]
    ) == 1
    assert len(harness.scheduler.initial_calls) == 1
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []
    restart_diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "bridge initial restart required"
    ]
    assert len(restart_diagnostics) == int(restart_required)
    if restart_required:
        assert code in restart_diagnostics[0].message

    admission = harness.coordinator_instance.start_imported(
        imported_request(RUN_IDS[1])
    )

    assert admission.status is admission_status
    assert len(harness.scheduler.initial_calls) == 1
    assert (
        harness.coordinator_instance.snapshot().restart_required
        is restart_required
    )


@pytest.mark.parametrize(
    "status",
    [
        BridgeTerminalStatus.LATE_IGNORED,
        BridgeTerminalStatus.DB_FAILED,
        BridgeTerminalStatus.DB_UNKNOWN,
        BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
        BridgeTerminalStatus.UNSUPPORTED,
    ],
)
def test_only_committed_bridge_reports_publish(status: BridgeTerminalStatus) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    report = terminal_report(
        run_id,
        status=status,
        state=(
            None
            if status
            in {
                BridgeTerminalStatus.DB_UNKNOWN,
                BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
                BridgeTerminalStatus.UNSUPPORTED,
            }
            else AttemptState.SUCCEEDED
        ),
        judgment=(
            None
            if status
            in {
                BridgeTerminalStatus.DB_UNKNOWN,
                BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
                BridgeTerminalStatus.UNSUPPORTED,
            }
            else Judgment.PASS
        ),
        result_json=(
            None
            if status
            in {
                BridgeTerminalStatus.DB_UNKNOWN,
                BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
                BridgeTerminalStatus.UNSUPPORTED,
            }
            else '{"judgment":"PASS","result":{}}'
        ),
        code={
            BridgeTerminalStatus.LATE_IGNORED: "LATE_OUTCOME_IGNORED",
            BridgeTerminalStatus.DB_FAILED: "ATTEMPT_TERMINAL_DB_FAILED",
            BridgeTerminalStatus.DB_UNKNOWN: "ANALYSIS_DB_STATE_UNKNOWN",
            BridgeTerminalStatus.TERMINAL_WRITE_FAILED:
                "ATTEMPT_TERMINAL_CALL_FAILED",
            BridgeTerminalStatus.UNSUPPORTED: "UNSUPPORTED_SCHEDULER_OUTCOME",
        }[status],
        restart_required=status
        in {
            BridgeTerminalStatus.DB_UNKNOWN,
            BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
        },
    )

    drive_to_bridge(harness)
    harness.scheduler.complete_report(report)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.persistence.terminal_calls == []


def test_committed_initial_plc_failure_retires_and_later_start_continues() -> None:
    harness = Harness()
    request = captured_request(source=TriggerSource.PLC)
    harness.projector.exception = RuntimeError("history failed")
    harness.plc.publish_exception = RuntimeError("PLC failed")

    coordinator = harness.coordinator()
    coordinator.start_captured(request)
    reservation = harness.resident.register_calls[0]
    harness.resident.complete(completed_capture(reservation))
    save_request, save_sink = harness.save_dispatcher.calls[0]
    save_sink(saved_outcome(save_request))
    harness.scheduler.complete_report(terminal_report(request.run_id))

    assert len(harness.plc.publish_calls) == 1
    assert len(harness.diagnostics.calls) >= 2
    assert coordinator.snapshot().pending_publication_run_ids == ()
    assert coordinator.snapshot().restart_required is False
    assert (
        coordinator.start_captured(
            captured_request(RUN_IDS[1], source=TriggerSource.PLC)
        ).status
        is StartStatus.ACCEPTED_ACTIVE
    )
    assert len(harness.plc.publish_calls) == 1


@pytest.mark.parametrize(
    (
        "state",
        "judgment",
        "result_json",
        "code",
        "summary",
        "conclusion",
        "category",
    ),
    [
        (
            AttemptState.SUCCEEDED,
            Judgment.PASS,
            '{"judgment":"PASS","result":{}}',
            "ANALYSIS_SUCCEEDED",
            None,
            TerminalConclusion.OK,
            None,
        ),
        (
            AttemptState.SUCCEEDED,
            Judgment.FAIL,
            '{"judgment":"FAIL","result":{}}',
            "ANALYSIS_SUCCEEDED",
            None,
            TerminalConclusion.NG,
            None,
        ),
        (
            AttemptState.NO_RESULT,
            None,
            None,
            "ANALYSIS_NO_RESULT",
            None,
            TerminalConclusion.NO_RESULT,
            None,
        ),
        (
            AttemptState.FAILED,
            None,
            None,
            "ANALYSIS_EXECUTION_FAILED",
            "analysis failed",
            TerminalConclusion.ERROR,
            TerminalErrorCategory.ANALYSIS_FAILED,
        ),
        (
            AttemptState.CANCELED,
            None,
            None,
            "USER_CANCELED",
            "operator stopped",
            TerminalConclusion.MANUAL_STOPPED,
            TerminalErrorCategory.MANUAL_STOPPED,
        ),
        (
            AttemptState.CANCELED,
            None,
            None,
            "ANALYSIS_SCHEDULER_SHUTDOWN",
            "scheduler shutdown",
            TerminalConclusion.ERROR,
            TerminalErrorCategory.SHUTDOWN_CANCELLED,
        ),
    ],
)
def test_committed_report_closed_mapping(
    state,
    judgment,
    result_json,
    code,
    summary,
    conclusion,
    category,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness)

    harness.scheduler.complete_report(
        terminal_report(
            run_id,
            state=state,
            judgment=judgment,
            result_json=result_json,
            code=code,
            summary=summary,
        )
    )

    terminal = next(
        fact.projection
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    )
    assert terminal.conclusion is conclusion
    assert terminal.error_category is category


def test_frozen_ai_failure_reaches_coordinator_terminal_reason() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness)
    summary = "FROZEN_AI_UNSUPPORTED:sha256:" + "b" * 64

    harness.scheduler.complete_report(
        terminal_report(
            run_id,
            state=AttemptState.FAILED,
            judgment=None,
            result_json=None,
            code="ANALYSIS_EXECUTION_FAILED",
            summary=summary,
        )
    )

    terminal = next(
        fact.projection
        for fact in harness.projector.calls
        if fact.kind is HistoryFactKind.RUN_TERMINATED
    )
    assert terminal.reason == summary


@pytest.mark.parametrize(
    ("state", "judgment", "result_json", "code"),
    [
        (
            AttemptState.SUCCEEDED,
            Judgment.PASS,
            '{"judgment":"PASS","result":{}}',
            "ANALYSIS_FAILED",
        ),
        (AttemptState.NO_RESULT, None, None, "ANALYSIS_SUCCEEDED"),
        (AttemptState.FAILED, None, None, "ANALYSIS_NO_RESULT"),
        (AttemptState.FAILED, None, None, "USER_CANCELED"),
        (AttemptState.CANCELED, None, None, "ANALYSIS_FAILED"),
        (AttemptState.CANCELED, None, None, "ANALYSIS_NO_RESULT"),
        (AttemptState.INTERRUPTED, None, None, "PROCESS_RESTARTED"),
    ],
)
def test_committed_state_code_mutations_fail_closed(
    state,
    judgment,
    result_json,
    code,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)

    harness.scheduler.complete_report(
        terminal_report(
            run_id,
            state=state,
            judgment=judgment,
            result_json=result_json,
            code=code,
            summary="terminal summary",
        )
    )

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("judgment", Judgment.FAIL),
        ("result_payload_json", '{"judgment":"FAIL","result":{}}'),
    ],
)
def test_committed_judgment_result_mutations_fail_closed(
    field_name,
    value,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    malformed = terminal_report(run_id)
    object.__setattr__(malformed, field_name, value)

    harness.scheduler.complete_report(malformed)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.plc.publish_calls == []


def test_malformed_exact_report_fails_closed_without_publication() -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    malformed = terminal_report(run_id)
    object.__setattr__(malformed, "result_payload_json", '{"unknown":true}')

    harness.scheduler.complete_report(malformed)

    assert harness.coordinator_instance.snapshot().restart_required is True
    assert not [
        call
        for call in harness.projector.calls
        if call.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert harness.plc.publish_calls == []


@pytest.mark.parametrize("raises", [False, True])
def test_task3b_terminal_history_attempt_advances_exact_causal_cursor(
    raises: bool,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    coordinator = harness.coordinator_instance
    original_publish = harness.history.publish

    def one_way_publish(event):
        assert not coordinator._lock._is_owned()
        original_publish(event)
        if event.kind is HistoryFactKind.RUN_TERMINATED and raises:
            raise RuntimeError("terminal dispatch failed")
        return object()

    harness.history.publish = one_way_publish
    plc_entered = Event()
    plc_release = Event()
    harness.plc.hooks["publish"].entered = plc_entered
    harness.plc.hooks["publish"].release = plc_release
    report_thread = Thread(
        target=lambda: harness.scheduler.complete_report(
            terminal_report(run_id)
        )
    )

    report_thread.start()
    assert plc_entered.wait(2)
    terminal_events = [
        event
        for event in harness.history.calls
        if event.kind is HistoryFactKind.RUN_TERMINATED
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].expected_revision == 4
    assert terminal_events[0].proposed_revision == 5
    with coordinator._lock:
        publication = coordinator._runs[run_id]
        assert publication.last_published_revision == 5
    plc_release.set()
    report_thread.join(2)
    assert not report_thread.is_alive()
    assert len(harness.plc.publish_calls) == 1
    if raises:
        assert len(
            [
                fact
                for fact in harness.diagnostics.calls
                if fact.stage == "history publication"
            ]
        ) == 1


def _invalid_terminal_projected_event(event, defect):
    if defect == "structural-object":
        return object()
    if defect == "malformed-exact-type":
        object.__setattr__(event, "run_id", object())
    elif defect == "cross-run":
        object.__setattr__(event, "run_id", RUN_IDS[1])
    elif defect == "kind":
        object.__setattr__(
            event,
            "kind",
            HistoryFactKind.REANALYSIS_COMPLETED,
        )
    elif defect == "operation":
        object.__setattr__(event, "operation_id", ATTEMPT_IDS[1])
    elif defect == "projection":
        object.__setattr__(
            event,
            "projection",
            replace(
                event.projection,
                result_payload=replace(
                    event.projection.result_payload,
                    value_id="mismatched-result",
                    canonical_bytes=b"mismatched-result",
                ),
            ),
        )
    elif defect == "expected-revision":
        object.__setattr__(event, "expected_revision", 3)
        object.__setattr__(event, "proposed_revision", 4)
    else:
        assert defect == "causal-successor"
        object.__setattr__(event, "proposed_revision", 6)
    return event


@pytest.mark.parametrize(
    "defect",
    [
        "structural-object",
        "malformed-exact-type",
        "cross-run",
        "kind",
        "operation",
        "projection",
        "expected-revision",
        "causal-successor",
    ],
)
def test_task3b_terminal_projector_output_is_validated_before_dispatch(
    defect: str,
) -> None:
    harness = Harness()
    run_id = captured_request().run_id
    drive_to_bridge(harness, captured_source=TriggerSource.PLC)
    coordinator = harness.coordinator_instance
    original_project = harness.projector.project

    def project(fact):
        event = original_project(fact)
        if fact.kind is HistoryFactKind.RUN_TERMINATED:
            return _invalid_terminal_projected_event(event, defect)
        return event

    harness.projector.project = project
    history_call_count = len(harness.history.calls)
    plc_entered = Event()
    plc_release = Event()
    harness.plc.hooks["publish"].entered = plc_entered
    harness.plc.hooks["publish"].release = plc_release
    thread = Thread(
        target=lambda: harness.scheduler.complete_report(
            terminal_report(run_id)
        )
    )

    thread.start()
    assert plc_entered.wait(2)
    assert len(harness.history.calls) == history_call_count
    assert not [
        event
        for event in harness.history.calls
        if getattr(event, "kind", None) is HistoryFactKind.RUN_TERMINATED
    ]
    with coordinator._lock:
        publication = coordinator._runs[run_id]
        assert publication.last_published_revision == 4
        assert not publication.history_in_flight
        assert publication.plc_in_flight
    plc_release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(harness.plc.publish_calls) == 1
    diagnostics = [
        fact
        for fact in harness.diagnostics.calls
        if fact.stage == "history projector"
    ]
    assert len(diagnostics) == 1
    assert len(diagnostics[0].message.encode("utf-8")) <= 512
