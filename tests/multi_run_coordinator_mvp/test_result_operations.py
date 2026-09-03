from __future__ import annotations

import dataclasses
import hashlib
import inspect
import math
from threading import Event, Thread

import pytest

import app.service.multi_run_coordinator_mvp as package
from app.service.analysis.scheduler import AnalysisBundleIdentity, AnalysisCancelOrigin
from app.service.run_persistence import (
    AttemptKind,
    AttemptState,
    BridgeAuthorityStatus,
    BridgeSubmissionResult,
    BridgeSubmissionStatus,
    BridgeTerminalReport,
    BridgeTerminalStatus,
    EnrichmentCommittedEvent,
    Judgment,
    TransactionResult,
    TransactionState,
)

from ._support import (
    ATTEMPT_IDS,
    AUDIO_IDS,
    RUN_IDS,
    SAVE_JOB_IDS,
    Harness,
    captured_request,
    completed_capture,
    payload,
    run_config_payload,
    saved_outcome,
)


def terminal_projection(
    conclusion: package.TerminalConclusion = package.TerminalConclusion.OK,
    *,
    fault_type: str | None = None,
    manual_judgment_revision: int | None = None,
) -> package.ResultProjection:
    kwargs = dict(
        phase=package.ResultProjectionPhase.TERMINAL,
        conclusion=conclusion,
        error_category=None,
        reason="",
        history_labels=payload("result-labels"),
        result_payload=payload("old-result"),
    )
    if "fault_type" in {field.name for field in dataclasses.fields(package.ResultProjection)}:
        kwargs["fault_type"] = fault_type
        kwargs["manual_judgment_revision"] = manual_judgment_revision
    return package.ResultProjection(**kwargs)


def projection_for_phase(
    phase: package.ResultProjectionPhase,
) -> package.ResultProjection:
    if phase in {
        package.ResultProjectionPhase.RECORDING,
        package.ResultProjectionPhase.WAITING_SAVE,
        package.ResultProjectionPhase.WAITING_ANALYSIS,
        package.ResultProjectionPhase.ANALYZING,
    }:
        return package.ResultProjection(
            phase=phase,
            conclusion=None,
            error_category=None,
            reason="",
            history_labels=payload("result-labels"),
            result_payload=None,
        )
    return package.ResultProjection(
        phase=phase,
        conclusion=package.TerminalConclusion.OK,
        error_category=None,
        reason="",
        history_labels=payload("result-labels"),
        result_payload=payload("old-result"),
    )


def result_facts(
    run_id: str = RUN_IDS[0],
    *,
    manual: bool = True,
    reanalysis: bool = True,
    audio: bool = True,
    revision: int = 7,
    fault_types: tuple[str, ...] = (),
    fault_type: str | None = None,
    manual_judgment_revision: int | None = None,
) -> package.ResultControlFacts:
    kwargs = dict(
        run_id=run_id,
        config_payload=run_config_payload(fault_types=fault_types),
        calibration_payload=payload("result-calibration"),
        analysis_input=payload("result-analysis"),
        audio=(
            package.PersistedAudioReference(
                audio_id=AUDIO_IDS[0],
                save_job_id=SAVE_JOB_IDS[0],
                run_id=run_id,
                source_kind=package.AudioSaveSourceKind.CAPTURED_AUDIO,
                source_identity="capture-source",
                final_path="C:/audio/final.wav",
                sample_rate_hz=16_000,
                channel_count=1,
                frame_count=2,
                byte_size=4,
                format=package.PersistedAudioFormat.PCM16_WAV,
                content_revision=1,
            )
            if audio
            else None
        ),
        manual_judgment_allowed=manual,
        manual_reanalysis_allowed=reanalysis,
        current_projection=terminal_projection(
            package.TerminalConclusion.NG
            if fault_type is not None
            else package.TerminalConclusion.OK,
            fault_type=fault_type,
            manual_judgment_revision=manual_judgment_revision,
        ),
    )
    if "causal_revision" in {
        field.name for field in dataclasses.fields(package.ResultControlFacts)
    }:
        kwargs["causal_revision"] = revision
    return package.ResultControlFacts(**kwargs)


def bypass_dataclass(instance, **changes):
    bypassed = object.__new__(type(instance))
    for field in dataclasses.fields(instance):
        object.__setattr__(
            bypassed,
            field.name,
            changes.get(field.name, getattr(instance, field.name)),
        )
    return bypassed


def omit_dataclass_field(value, omitted_field):
    bypassed = object.__new__(type(value))
    for field in dataclasses.fields(value):
        if field.name != omitted_field:
            object.__setattr__(bypassed, field.name, getattr(value, field.name))
    return bypassed


def manual_request(
    run_id: str = RUN_IDS[0],
    revision: int = 7,
    conclusion: package.TerminalConclusion = package.TerminalConclusion.NG,
    fault_type: str | None = None,
) -> package.ManualJudgmentRequest:
    kwargs = dict(
        run_id=run_id,
        expected_revision=revision,
        conclusion=conclusion,
        bounded_note="operator override",
    )
    if "fault_type" in {
        field.name for field in dataclasses.fields(package.ManualJudgmentRequest)
    }:
        kwargs["fault_type"] = fault_type
    return package.ManualJudgmentRequest(**kwargs)


def reanalysis_request(
    run_id: str = RUN_IDS[0],
    revision: int = 7,
    start: float = 1.25,
    end: float = 3.75,
) -> package.ReanalysisRequest:
    return package.ReanalysisRequest(
        run_id=run_id,
        expected_revision=revision,
        start_sec=start,
        end_sec=end,
    )


def reanalysis_report(
    run_id: str = RUN_IDS[0],
    attempt_id: str = ATTEMPT_IDS[0],
    *,
    state: AttemptState = AttemptState.SUCCEEDED,
    judgment: Judgment | None = Judgment.FAIL,
    result_json: str | None = '{"judgment":"FAIL","result":{}}',
    code: str = "ANALYSIS_SUCCEEDED",
) -> BridgeTerminalReport:
    return BridgeTerminalReport(
        status=BridgeTerminalStatus.COMMITTED,
        run_id=run_id,
        analysis_attempt_id=attempt_id,
        attempt_kind=AttemptKind.REANALYSIS,
        attempt_state=state,
        judgment=judgment,
        result_payload_json=result_json,
        code=code,
        summary=None,
        terminal_at="2026-07-27T09:00:00.000000Z",
        persistence_result=TransactionResult(TransactionState.COMMITTED, True),
        restart_required=False,
    )


def drive_pending_enrichment_decision(harness: Harness):
    decision_json = '{"judgment":"PASS","result":{"chart_payload":{}}}'
    coordinator = harness.coordinator()
    request = captured_request()
    coordinator.start_captured(request)
    harness.resident.complete(
        completed_capture(harness.resident.register_calls[0])
    )
    save_request, save_sink = harness.save_dispatcher.calls[0]
    save_sink(saved_outcome(save_request))
    harness.scheduler.complete_report(
        BridgeTerminalReport(
            status=BridgeTerminalStatus.COMMITTED,
            run_id=request.run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            attempt_kind=AttemptKind.INITIAL,
            attempt_state=AttemptState.SUCCEEDED,
            judgment=Judgment.PASS,
            result_payload_json=decision_json,
            code="ANALYSIS_SUCCEEDED",
            summary=None,
            terminal_at="2026-07-27T09:00:00.000000Z",
            persistence_result=TransactionResult(
                TransactionState.COMMITTED,
                True,
            ),
            restart_required=False,
        )
    )
    terminal_event = next(
        event
        for event in reversed(harness.history.calls)
        if event.kind is package.HistoryFactKind.RUN_TERMINATED
    )
    terminal_revision = hashlib.sha256(
        decision_json.encode("utf-8")
    ).hexdigest()
    harness.persistence.facts_by_run[request.run_id] = dataclasses.replace(
        result_facts(
            run_id=request.run_id,
            revision=terminal_event.proposed_revision,
            fault_types=("异响",),
        ),
        current_projection=terminal_event.projection,
    )
    return coordinator, request.run_id, terminal_event, terminal_revision


def pending_enrichment_event(
    run_id: str,
    terminal_revision: str,
) -> EnrichmentCommittedEvent:
    return EnrichmentCommittedEvent(
        identity=AnalysisBundleIdentity(
            run_id=run_id,
            analysis_attempt_id=ATTEMPT_IDS[0],
            content_revision="audio-content-r1",
            algorithm_revision="algorithm-r1",
            config_identity="config-r1",
            calibration_identity="calibration-r1",
            channel_mapping_identity="channel-map-r1",
            window_start_seconds=0.0,
            window_end_seconds=1.0,
        ),
        judgment=Judgment.PASS,
        result_payload_json=(
            '{"judgment":"PASS","result":{"chart_payload":{"0":{}}}}'
        ),
        terminal_revision=terminal_revision,
        committed_at="2026-07-27T09:00:00.100000Z",
    )


_MALFORMED_REANALYSIS_REPORT_CASES = (
    "missing_run_id",
    "missing_status",
    "missing_persistence_result",
    "restart_required",
    "attempt_state",
    "code",
    "summary",
    "terminal_at",
    "result_payload_json",
    "persistence_state",
    "persistence_value",
)


def malformed_reanalysis_report(case):
    report = reanalysis_report()
    if case == "missing_run_id":
        return omit_dataclass_field(report, "run_id")
    if case == "missing_status":
        return omit_dataclass_field(report, "status")
    if case == "missing_persistence_result":
        return omit_dataclass_field(report, "persistence_result")
    if case == "restart_required":
        return bypass_dataclass(report, restart_required=1)
    if case == "attempt_state":
        return bypass_dataclass(report, attempt_state="SUCCEEDED")
    if case == "code":
        return bypass_dataclass(report, code="")
    if case == "summary":
        return bypass_dataclass(report, summary=1)
    if case == "terminal_at":
        return bypass_dataclass(report, terminal_at="not-a-timestamp")
    if case == "result_payload_json":
        return bypass_dataclass(report, result_payload_json="{")
    if case == "persistence_state":
        return bypass_dataclass(
            report,
            persistence_result=bypass_dataclass(
                report.persistence_result,
                state="COMMITTED",
            ),
        )
    if case == "persistence_value":
        return bypass_dataclass(
            report,
            persistence_result=bypass_dataclass(
                report.persistence_result,
                value=1,
            ),
        )
    raise AssertionError(f"unknown malformed report case: {case}")


def committed_failed_reanalysis_report(
    code: str,
    summary: str | None,
) -> BridgeTerminalReport:
    return BridgeTerminalReport(
        status=BridgeTerminalStatus.COMMITTED,
        run_id=RUN_IDS[0],
        analysis_attempt_id=ATTEMPT_IDS[0],
        attempt_kind=AttemptKind.REANALYSIS,
        attempt_state=AttemptState.FAILED,
        judgment=None,
        result_payload_json=None,
        code=code,
        summary=summary,
        terminal_at="2026-07-27T09:00:00.000000Z",
        persistence_result=TransactionResult(TransactionState.COMMITTED, True),
        restart_required=False,
    )


def db_failed_reanalysis_report(
    code: str,
    summary: str | None,
) -> BridgeTerminalReport:
    return BridgeTerminalReport(
        status=BridgeTerminalStatus.DB_FAILED,
        run_id=RUN_IDS[0],
        analysis_attempt_id=ATTEMPT_IDS[0],
        attempt_kind=AttemptKind.REANALYSIS,
        attempt_state=AttemptState.FAILED,
        judgment=None,
        result_payload_json=None,
        code=code,
        summary=summary,
        terminal_at=None,
        persistence_result=TransactionResult(
            TransactionState.ROLLED_BACK,
            None,
            reason="terminal database failed",
        ),
        restart_required=False,
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (0, 1.0),
        (0.0, 1),
        (False, 1.0),
        (0.0, True),
        (math.nan, 1.0),
        (0.0, math.inf),
        (-0.1, 1.0),
        (1.0, 1.0),
        (2.0, 1.0),
    ],
)
def test_reanalysis_window_is_exact_finite_float_contract(start, end):
    with pytest.raises((TypeError, ValueError)):
        reanalysis_request(start=start, end=end)


def test_reanalysis_request_has_exact_frozen_selected_window_surface():
    request = reanalysis_request(start=0.125, end=0.875)

    assert dataclasses.is_dataclass(request)
    assert request.__dataclass_params__.frozen is True
    assert tuple(inspect.signature(type(request)).parameters) == (
        "run_id",
        "expected_revision",
        "start_sec",
        "end_sec",
    )
    assert (request.start_sec, request.end_sec) == (0.125, 0.875)


def test_manual_registers_provisional_before_unlocked_fact_load_and_is_single_flight():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.persistence.hooks["facts"].entered = entered
    harness.persistence.hooks["facts"].release = release
    coordinator = harness.coordinator()
    result_box = []

    worker = Thread(target=lambda: result_box.append(coordinator.apply_manual(manual_request())))
    worker.start()
    assert entered.wait(2)
    snapshot = coordinator.snapshot()
    assert snapshot.result_operation_attempt_ids == ()
    assert coordinator.apply_manual(manual_request()).status is package.OperationStatus.OPERATION_BUSY
    assert coordinator.start_reanalysis(reanalysis_request()).status is package.OperationStatus.OPERATION_BUSY

    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert result_box[0].status is package.OperationStatus.COMPLETED


def test_manual_fault_requires_frozen_membership_and_exact_revision() -> None:
    cases = (
        (
            result_facts(fault_types=("异响", "漏装")),
            manual_request(fault_type="异响"),
            package.OperationStatus.COMPLETED,
        ),
        (
            result_facts(fault_types=("异响", "漏装")),
            manual_request(fault_type="其他"),
            package.OperationStatus.INVALID_REQUEST,
        ),
        (
            result_facts(fault_types=("异响",)),
            manual_request(
                conclusion=package.TerminalConclusion.OK,
                fault_type="异响",
            ),
            package.OperationStatus.INVALID_REQUEST,
        ),
        (
            result_facts(fault_types=()),
            manual_request(fault_type=None),
            package.OperationStatus.COMPLETED,
        ),
        (
            result_facts(revision=8, fault_types=("异响",)),
            manual_request(revision=7, fault_type="异响"),
            package.OperationStatus.STALE_REVISION,
        ),
    )
    for facts, request, expected_status in cases:
        harness = Harness()
        harness.persistence.facts_by_run[RUN_IDS[0]] = facts
        coordinator = harness.coordinator()

        result = coordinator.apply_manual(request)

        assert result.status is expected_status
        expected_calls = 1 if expected_status is package.OperationStatus.COMPLETED else 0
        assert len(harness.persistence.manual_calls) == expected_calls
        if expected_calls:
            persisted_request, persisted_facts = harness.persistence.manual_calls[0]
            assert persisted_request is request
            assert persisted_facts is facts


def test_reanalysis_supersession_clears_or_preserves_manual_fault_fields() -> None:
    prior_facts = result_facts(
        fault_types=("异响",),
        fault_type="异响",
        manual_judgment_revision=7,
    )
    cases = (
        (
            reanalysis_report(),
            package.HistoryFactKind.REANALYSIS_COMPLETED,
            None,
            None,
        ),
        (
            committed_failed_reanalysis_report(
                "ANALYSIS_EXECUTION_FAILED",
                "failed",
            ),
            package.HistoryFactKind.REANALYSIS_COMPLETED,
            "异响",
            7,
        ),
        (
            reanalysis_report(
                state=AttemptState.CANCELED,
                judgment=None,
                result_json=None,
                code="REANALYSIS_CANCELED",
            ),
            package.HistoryFactKind.REANALYSIS_CANCELLED,
            "异响",
            7,
        ),
    )
    for report, expected_kind, expected_fault, expected_manual_revision in cases:
        harness = Harness()
        harness.persistence.facts_by_run[RUN_IDS[0]] = prior_facts
        coordinator = harness.coordinator()
        assert coordinator.start_reanalysis(reanalysis_request()).status is (
            package.OperationStatus.SUBMITTED
        )

        harness.scheduler.complete_report(report)

        event = harness.history.calls[-1]
        assert event.kind is expected_kind
        assert event.projection.fault_type == expected_fault
        assert event.projection.manual_judgment_revision == expected_manual_revision


@pytest.mark.parametrize(
    ("facts", "status"),
    [
        (None, package.OperationStatus.NOT_FOUND),
        (result_facts(manual=False), package.OperationStatus.NOT_ALLOWED),
    ],
)
def test_manual_rejects_missing_or_disallowed_facts(facts, status):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = facts
    coordinator = harness.coordinator()

    result = coordinator.apply_manual(manual_request())

    assert result.status is status
    assert harness.persistence.manual_calls == []
    assert harness.history.calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()


@pytest.mark.parametrize("operation", ["manual", "reanalysis"])
@pytest.mark.parametrize(
    "case",
    [
        "run_identity",
        "manual_flag",
        "reanalysis_flag",
        "config_payload",
        "calibration_payload",
        "analysis_input",
        "audio",
        "projection_missing_field",
        "projection_combination",
        "projection_history_labels",
        "projection_result_payload",
    ],
)
def test_result_facts_boundary_recursively_rejects_exact_malformed_values(
    operation,
    case,
):
    harness = Harness()
    facts = result_facts()
    invalid_payload = bypass_dataclass(
        payload("invalid-payload"),
        canonical_bytes=b"",
    )
    invalid_projection = {
        "projection_missing_field": omit_dataclass_field(
            facts.current_projection,
            "conclusion",
        ),
        "projection_combination": bypass_dataclass(
            facts.current_projection,
            conclusion=None,
        ),
        "projection_history_labels": bypass_dataclass(
            facts.current_projection,
            history_labels=invalid_payload,
        ),
        "projection_result_payload": bypass_dataclass(
            facts.current_projection,
            result_payload=invalid_payload,
        ),
    }.get(case, facts.current_projection)
    invalid_facts = bypass_dataclass(
        facts,
        run_id=RUN_IDS[1] if case == "run_identity" else facts.run_id,
        manual_judgment_allowed=(
            1 if case == "manual_flag" else facts.manual_judgment_allowed
        ),
        manual_reanalysis_allowed=(
            1
            if case == "reanalysis_flag"
            else facts.manual_reanalysis_allowed
        ),
        config_payload=(
            invalid_payload
            if case == "config_payload"
            else facts.config_payload
        ),
        calibration_payload=(
            invalid_payload
            if case == "calibration_payload"
            else facts.calibration_payload
        ),
        analysis_input=(
            invalid_payload
            if case == "analysis_input"
            else facts.analysis_input
        ),
        audio=(
            bypass_dataclass(facts.audio, sample_rate_hz=True)
            if case == "audio"
            else facts.audio
        ),
        current_projection=invalid_projection,
    )
    harness.persistence.facts_by_run[RUN_IDS[0]] = invalid_facts
    coordinator = harness.coordinator()

    result = (
        coordinator.apply_manual(manual_request())
        if operation == "manual"
        else coordinator.start_reanalysis(reanalysis_request())
    )

    assert result.status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.persistence.facts_calls == [RUN_IDS[0]]
    assert harness.persistence.manual_calls == []
    assert harness.scheduler.reanalysis_calls == []
    assert harness.projector.calls == []
    assert harness.history.calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert len(harness.diagnostics.calls) == 1


def test_manual_committed_replacement_uses_ui_revision_once_and_never_plc_or_analysis():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()

    result = coordinator.apply_manual(manual_request(revision=7))

    assert result.status is package.OperationStatus.COMPLETED
    assert result.attempt_id is None
    assert len(harness.persistence.manual_calls) == 1
    assert len(harness.history.calls) == 1
    event = harness.history.calls[0]
    assert event.kind is package.HistoryFactKind.TERMINAL_RESULT_REPLACED
    assert (event.expected_revision, event.proposed_revision) == (7, 8)
    assert harness.scheduler.reanalysis_calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []
    assert coordinator.snapshot().accepted_active_run_ids == ()


def test_manual_history_block_preserves_enrichment_at_original_decision_revision():
    harness = Harness()
    coordinator, run_id, terminal_event, terminal_revision = (
        drive_pending_enrichment_decision(harness)
    )
    original_revision = terminal_event.proposed_revision
    history_count = len(harness.history.calls)
    initial_analysis_count = len(harness.scheduler.initial_calls)
    plc_register_count = len(harness.plc.register_calls)
    plc_publish_count = len(harness.plc.publish_calls)
    entered = Event()
    release = Event()
    hook = harness.history.hooks[
        package.HistoryFactKind.TERMINAL_RESULT_REPLACED
    ]
    hook.entered = entered
    hook.release = release
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.apply_manual(
                manual_request(
                    run_id=run_id,
                    revision=original_revision,
                    fault_type="异响",
                )
            )
        )
    )

    worker.start()
    assert entered.wait(2)
    projected = coordinator.project_enrichment(
        pending_enrichment_event(run_id, terminal_revision)
    )

    assert type(projected) is package.ResultEnrichmentProjection
    assert projected.expected_revision == original_revision
    assert len(harness.persistence.manual_calls) == 1
    assert len(harness.history.calls) == history_count + 1
    assert len(harness.scheduler.initial_calls) == initial_analysis_count
    assert len(harness.scheduler.reanalysis_calls) == 0
    assert len(harness.plc.register_calls) == plc_register_count
    assert len(harness.plc.publish_calls) == plc_publish_count
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert results[0].status is package.OperationStatus.COMPLETED
    assert coordinator.project_enrichment(
        pending_enrichment_event(run_id, terminal_revision)
    ) is None


def test_manual_history_enqueue_advances_enrichment_to_exact_new_revision():
    harness = Harness()
    coordinator, run_id, terminal_event, terminal_revision = (
        drive_pending_enrichment_decision(harness)
    )
    entered = Event()
    release = Event()
    hook = harness.history.hooks[
        package.HistoryFactKind.TERMINAL_RESULT_REPLACED
    ]
    hook.entered = entered
    hook.release = release
    results = []
    worker = Thread(
        target=lambda: results.append(
            coordinator.apply_manual(
                manual_request(
                    run_id=run_id,
                    revision=terminal_event.proposed_revision,
                    fault_type="异响",
                )
            )
        )
    )

    worker.start()
    assert entered.wait(2)
    manual_event = harness.history.calls[-1]
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    projected = coordinator.project_enrichment(
        pending_enrichment_event(run_id, terminal_revision)
    )

    assert results[0].status is package.OperationStatus.COMPLETED
    assert type(projected) is package.ResultEnrichmentProjection
    assert projected.expected_revision == manual_event.proposed_revision
    assert manual_event.proposed_revision == terminal_event.proposed_revision + 1
    assert coordinator.project_enrichment(
        pending_enrichment_event(run_id, terminal_revision)
    ) is None


def test_reanalysis_start_and_cancel_do_not_restore_old_enrichment_ownership():
    harness = Harness()
    coordinator, run_id, terminal_event, terminal_revision = (
        drive_pending_enrichment_decision(harness)
    )
    harness.scheduler.bridge_results.append(
        harness.scheduler.accepted_bridge_result(run_id, ATTEMPT_IDS[1])
    )

    submitted = coordinator.start_reanalysis(
        reanalysis_request(
            run_id=run_id,
            revision=terminal_event.proposed_revision,
        )
    )
    coordinator.cancel_reanalysis(
        package.ReanalysisCancelRequest(
            run_id=run_id,
            attempt_id=submitted.attempt_id,
            expected_revision=terminal_event.proposed_revision + 1,
        )
    )
    harness.scheduler.complete_report(
        reanalysis_report(
            run_id=run_id,
            attempt_id=ATTEMPT_IDS[1],
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )

    assert submitted.status is package.OperationStatus.SUBMITTED
    assert [
        event.kind for event in harness.history.calls[-2:]
    ] == [
        package.HistoryFactKind.REANALYSIS_STARTED,
        package.HistoryFactKind.REANALYSIS_CANCELLED,
    ]
    assert coordinator.project_enrichment(
        pending_enrichment_event(run_id, terminal_revision)
    ) is None


def test_manual_history_exception_is_diagnostic_only_after_durable_commit():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.history.exception = RuntimeError("UI unavailable")
    coordinator = harness.coordinator()

    result = coordinator.apply_manual(manual_request())

    assert result.status is package.OperationStatus.COMPLETED
    assert len(harness.persistence.manual_calls) == 1
    assert len(harness.history.calls) == 1
    assert harness.scheduler.cancel_calls == []
    assert harness.diagnostics.calls


@pytest.mark.parametrize(
    "manual_result",
    [
        package.ManualProjectionResult(
            status=package.ManualProjectionStatus.REJECTED,
            run_id=RUN_IDS[0],
            replacement_projection=None,
            committed_revision=None,
            reason="stale durable revision",
        ),
        package.ManualProjectionResult(
            status=package.ManualProjectionStatus.UNAVAILABLE,
            run_id=RUN_IDS[0],
            replacement_projection=None,
            committed_revision=None,
            reason="database unavailable",
        ),
    ],
)
def test_manual_noncommitted_persistence_retires_without_history(manual_result):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.persistence.manual_results.append(manual_result)
    coordinator = harness.coordinator()

    result = coordinator.apply_manual(manual_request())

    assert result.status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.history.calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()


@pytest.mark.parametrize(
    "case",
    [
        "run_identity",
        "outer_reason",
        "projection_missing_field",
        "projection_combination",
        "projection_history_labels",
        "projection_result_payload",
    ],
)
def test_manual_result_boundary_recursively_rejects_exact_malformed_values(case):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    projection = terminal_projection(
        package.TerminalConclusion.NG,
        manual_judgment_revision=8,
    )
    invalid_payload = bypass_dataclass(
        payload("invalid-payload"),
        canonical_bytes=b"",
    )
    invalid_projection = {
        "projection_missing_field": omit_dataclass_field(
            projection,
            "conclusion",
        ),
        "projection_combination": bypass_dataclass(
            projection,
            conclusion=None,
        ),
        "projection_history_labels": bypass_dataclass(
            projection,
            history_labels=invalid_payload,
        ),
        "projection_result_payload": bypass_dataclass(
            projection,
            result_payload=invalid_payload,
        ),
    }.get(case, projection)
    committed = package.ManualProjectionResult(
        status=package.ManualProjectionStatus.COMMITTED,
        run_id=RUN_IDS[0],
        replacement_projection=projection,
        committed_revision=8,
        reason="",
    )
    invalid_result = bypass_dataclass(
        committed,
        run_id=RUN_IDS[1] if case == "run_identity" else committed.run_id,
        replacement_projection=invalid_projection,
        reason=1 if case == "outer_reason" else committed.reason,
    )
    harness.persistence.manual_results.append(invalid_result)
    coordinator = harness.coordinator()

    result = coordinator.apply_manual(manual_request())

    assert result.status is package.OperationStatus.PERSISTENCE_REJECTED
    assert len(harness.persistence.manual_calls) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.reanalysis_calls == []
    assert harness.projector.calls == []
    assert harness.history.calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert len(harness.diagnostics.calls) == 1


def test_eviction_during_manual_commit_suppresses_only_history_not_durable_fact():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.persistence.hooks["manual"].entered = entered
    harness.persistence.hooks["manual"].release = release
    coordinator = harness.coordinator()
    result_box = []
    worker = Thread(target=lambda: result_box.append(coordinator.apply_manual(manual_request())))

    worker.start()
    assert entered.wait(2)
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is True
    release.set()
    worker.join(2)

    assert result_box[0].status is package.OperationStatus.COMPLETED
    assert len(harness.persistence.manual_calls) == 1
    assert harness.history.calls == []
    assert harness.scheduler.cancel_calls == []


@pytest.mark.parametrize("operation", ["manual", "reanalysis"])
def test_eviction_during_fact_load_retires_before_next_external_action(operation):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.persistence.hooks["facts"].entered = entered
    harness.persistence.hooks["facts"].release = release
    coordinator = harness.coordinator()
    result_box = []
    if operation == "manual":
        target = lambda: coordinator.apply_manual(manual_request())
    else:
        target = lambda: coordinator.start_reanalysis(reanalysis_request())
    worker = Thread(target=lambda: result_box.append(target()))

    worker.start()
    assert entered.wait(2)
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is True
    release.set()
    worker.join(2)

    assert result_box[0].status is package.OperationStatus.EVICTED
    assert harness.persistence.manual_calls == []
    assert harness.scheduler.reanalysis_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.history.calls == []


def test_reanalysis_validates_window_before_loading_facts():
    harness = Harness()
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(object())

    assert result.status is package.OperationStatus.INVALID_REQUEST
    assert harness.persistence.facts_calls == []


@pytest.mark.parametrize(
    ("facts", "status"),
    [
        (None, package.OperationStatus.NOT_FOUND),
        (result_facts(reanalysis=False), package.OperationStatus.NOT_ALLOWED),
        (result_facts(audio=False), package.OperationStatus.NOT_FOUND),
    ],
)
def test_reanalysis_requires_exact_capability_and_audio(facts, status):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = facts
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is status
    assert harness.scheduler.reanalysis_calls == []
    assert harness.history.calls == []


@pytest.mark.parametrize(
    ("phase", "admissible"),
    [
        (package.ResultProjectionPhase.RECORDING, False),
        (package.ResultProjectionPhase.WAITING_SAVE, False),
        (package.ResultProjectionPhase.WAITING_ANALYSIS, False),
        (package.ResultProjectionPhase.ANALYZING, False),
        (package.ResultProjectionPhase.REANALYZING, False),
        (package.ResultProjectionPhase.TERMINAL, True),
    ],
)
def test_reanalysis_prior_projection_phase_admission_is_exact(
    phase,
    admissible,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = dataclasses.replace(
        result_facts(),
        current_projection=projection_for_phase(phase),
    )
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    if admissible:
        assert result.status is package.OperationStatus.SUBMITTED
        assert len(harness.scheduler.reanalysis_calls) == 1
        assert len(harness.history.calls) == 1
        assert harness.diagnostics.calls == []
    else:
        assert result.status is package.OperationStatus.PERSISTENCE_REJECTED
        assert harness.scheduler.reanalysis_calls == []
        assert harness.projector.calls == []
        assert harness.history.calls == []
        assert coordinator.snapshot().result_operation_attempt_ids == ()
        assert len(harness.diagnostics.calls) == 1
    assert harness.persistence.manual_calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []


def test_reanalysis_forwards_exact_selected_window_and_binds_one_sink():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    request = reanalysis_request(start=0.123456789, end=9.876543211)

    result = coordinator.start_reanalysis(request)

    assert result.status is package.OperationStatus.SUBMITTED
    assert result.attempt_id == ATTEMPT_IDS[0]
    bridge_request, sink = harness.scheduler.reanalysis_calls[0]
    assert bridge_request.analysis_window_start_sec == request.start_sec
    assert bridge_request.analysis_window_end_sec == request.end_sec
    assert callable(sink)
    assert coordinator.snapshot().result_operation_attempt_ids == (ATTEMPT_IDS[0],)
    started = harness.history.calls[0]
    assert started.kind is package.HistoryFactKind.REANALYSIS_STARTED
    assert (started.expected_revision, started.proposed_revision) == (7, 8)
    assert harness.plc.register_calls == []
    assert harness.persistence.terminal_calls == []


@pytest.mark.parametrize("case", _MALFORMED_REANALYSIS_REPORT_CASES)
def test_malformed_held_report_is_quarantined_before_coherence_or_history(case):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.bridge_results.append(
        harness.scheduler.accepted_bridge_result(RUN_IDS[0], early=True)
    )
    harness.scheduler.synchronous_report = malformed_reanalysis_report(case)
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.projector.calls == []
    assert harness.history.calls == []
    assert harness.persistence.manual_calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []
    snapshot = coordinator.snapshot()
    assert snapshot.restart_required is True
    assert snapshot.result_operation_attempt_ids == ()
    assert len(harness.diagnostics.calls) == 1


@pytest.mark.parametrize("case", _MALFORMED_REANALYSIS_REPORT_CASES)
def test_malformed_async_report_quarantines_before_started_history(
    monkeypatch,
    case,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    before_started = Event()
    release_started = Event()
    original_dispatch = coordinator._dispatch_result_history

    def blocked_started_dispatch(context, fact, stage):
        if fact.kind is package.HistoryFactKind.REANALYSIS_STARTED:
            before_started.set()
            assert release_started.wait(2)
        return original_dispatch(context, fact, stage)

    monkeypatch.setattr(
        coordinator,
        "_dispatch_result_history",
        blocked_started_dispatch,
    )
    result_box = []
    worker = Thread(
        target=lambda: result_box.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )

    worker.start()
    assert before_started.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    try:
        sink(malformed_reanalysis_report(case))
    finally:
        release_started.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result_box[0].status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.projector.calls == []
    assert harness.history.calls == []
    assert harness.persistence.manual_calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []
    snapshot = coordinator.snapshot()
    assert snapshot.restart_required is True
    assert snapshot.result_operation_attempt_ids == ()
    assert len(harness.diagnostics.calls) == 1


def test_reanalysis_early_report_reconciles_once_after_acquired_identity():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.synchronous_report = reanalysis_report()
    harness.scheduler.bridge_results.append(
        Harness().scheduler.accepted_bridge_result(
            RUN_IDS[0], early=True
        )
    )
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is package.OperationStatus.SUBMITTED
    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED,
        package.HistoryFactKind.REANALYSIS_COMPLETED,
    ]
    terminal = harness.history.calls[1]
    assert (terminal.expected_revision, terminal.proposed_revision) == (8, 9)
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert harness.persistence.terminal_calls == []
    assert harness.plc.publish_calls == []


@pytest.mark.parametrize(
    ("case", "valid"),
    [
        ("accepted_committed", True),
        ("accepted_noncommitted", True),
        ("early_committed", True),
        ("early_noncommitted", False),
        ("rejected_matching", True),
        ("rejected_code_mismatch", False),
        ("rejected_summary_mismatch", False),
        ("rejected_noncommitted", False),
        ("unknown_matching", True),
        ("unknown_code_mismatch", False),
        ("unknown_summary_mismatch", False),
        ("unknown_committed", False),
    ],
)
def test_acquired_submission_report_pair_matrix_is_exact(case, valid):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    accepted = harness.scheduler.accepted_bridge_result(RUN_IDS[0])
    early = harness.scheduler.accepted_bridge_result(RUN_IDS[0], early=True)
    rejected = BridgeSubmissionResult(
        status=BridgeSubmissionStatus.REJECTED,
        authority=BridgeAuthorityStatus.ACQUIRED,
        run_id=RUN_IDS[0],
        analysis_attempt_id=ATTEMPT_IDS[0],
        scheduler_accepted=False,
        terminal_delivered_before_return=True,
        restart_required=False,
        code="ANALYSIS_SCHEDULER_SUBMIT_FAILED",
        reason=None,
        submit_receipt=None,
    )
    unknown = BridgeSubmissionResult(
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
    matching_rejected = committed_failed_reanalysis_report(
        "ANALYSIS_SCHEDULER_SUBMIT_FAILED",
        None,
    )
    matching_unknown = db_failed_reanalysis_report(
        "ATTEMPT_TERMINAL_DB_FAILED",
        "terminal database failed",
    )
    result, report = {
        "accepted_committed": (accepted, reanalysis_report()),
        "accepted_noncommitted": (
            accepted,
            db_failed_reanalysis_report(
                "ATTEMPT_TERMINAL_DB_FAILED",
                "terminal database failed",
            ),
        ),
        "early_committed": (early, reanalysis_report()),
        "early_noncommitted": (
            early,
            db_failed_reanalysis_report(
                "ATTEMPT_TERMINAL_DB_FAILED",
                "terminal database failed",
            ),
        ),
        "rejected_matching": (rejected, matching_rejected),
        "rejected_code_mismatch": (
            rejected,
            committed_failed_reanalysis_report(
                "ANALYSIS_QUEUE_FULL",
                None,
            ),
        ),
        "rejected_summary_mismatch": (
            rejected,
            committed_failed_reanalysis_report(
                "ANALYSIS_SCHEDULER_SUBMIT_FAILED",
                "unexpected summary",
            ),
        ),
        "rejected_noncommitted": (
            rejected,
            db_failed_reanalysis_report(
                "ANALYSIS_SCHEDULER_SUBMIT_FAILED",
                None,
            ),
        ),
        "unknown_matching": (unknown, matching_unknown),
        "unknown_code_mismatch": (
            unknown,
            db_failed_reanalysis_report(
                "ATTEMPT_TERMINAL_CALL_FAILED",
                "terminal database failed",
            ),
        ),
        "unknown_summary_mismatch": (
            unknown,
            db_failed_reanalysis_report(
                "ATTEMPT_TERMINAL_DB_FAILED",
                "different summary",
            ),
        ),
        "unknown_committed": (
            unknown,
            committed_failed_reanalysis_report(
                "ATTEMPT_TERMINAL_DB_FAILED",
                "terminal database failed",
            ),
        ),
    }[case]
    harness.scheduler.bridge_results.append(result)
    harness.scheduler.synchronous_report = report
    coordinator = harness.coordinator()

    operation = coordinator.start_reanalysis(reanalysis_request())

    if valid:
        assert operation.status is package.OperationStatus.SUBMITTED
        assert harness.history.calls
    else:
        assert operation.status is package.OperationStatus.PERSISTENCE_REJECTED
        assert harness.history.calls == []
        assert coordinator.snapshot().restart_required is True
        assert len(harness.diagnostics.calls) == 1
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.publish_calls == []


def test_duplicate_reports_before_submit_reconciliation_quarantine_without_history():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.scheduler.hook.entered = entered
    harness.scheduler.hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    worker = Thread(
        target=lambda: result_box.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )

    worker.start()
    assert entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    report = reanalysis_report()
    sink(report)
    sink(report)
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result_box[0].status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.history.calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert coordinator.snapshot().restart_required is True
    assert len(harness.diagnostics.calls) == 1


def test_duplicate_report_after_consumption_and_retirement_quarantines_bound_channel_once():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request())
    _, sink = harness.scheduler.reanalysis_calls[0]
    report = reanalysis_report()
    sink(report)
    history_count = len(harness.history.calls)
    diagnostic_count = len(harness.diagnostics.calls)

    sink(report)
    sink(report)

    assert len(harness.history.calls) == history_count
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert coordinator.snapshot().restart_required is True
    assert len(harness.diagnostics.calls) == diagnostic_count + 1


def test_duplicate_reports_during_started_projection_prevent_started_and_terminal_history():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    hook = harness.projector.hooks[package.HistoryFactKind.REANALYSIS_STARTED]
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    worker = Thread(
        target=lambda: result_box.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )

    worker.start()
    assert entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    report = reanalysis_report()
    sink(report)
    sink(report)
    release.set()
    worker.join(2)

    assert result_box[0].status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.history.calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert coordinator.snapshot().restart_required is True
    assert len(harness.diagnostics.calls) == 1


def test_duplicate_after_held_report_moves_local_cannot_reopen_delivery_slot(
    monkeypatch,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.synchronous_report = reanalysis_report()
    harness.scheduler.bridge_results.append(
        Harness().scheduler.accepted_bridge_result(
            RUN_IDS[0],
            early=True,
        )
    )
    coordinator = harness.coordinator()
    held_moved = Event()
    started_release = Event()
    original_dispatch = coordinator._dispatch_result_history

    def blocked_started_dispatch(context, fact, stage):
        if fact.kind is package.HistoryFactKind.REANALYSIS_STARTED:
            held_moved.set()
            started_release.wait()
        return original_dispatch(context, fact, stage)

    monkeypatch.setattr(
        coordinator,
        "_dispatch_result_history",
        blocked_started_dispatch,
    )
    result_box = []
    worker = Thread(
        target=lambda: result_box.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )
    worker.start()
    assert held_moved.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    sink(reanalysis_report())
    started_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result_box[0].status is package.OperationStatus.PERSISTENCE_REJECTED
    assert harness.history.calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.publish_calls == []
    assert coordinator.snapshot().restart_required is True
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert len(harness.diagnostics.calls) == 1


def test_duplicate_during_terminal_projection_suppresses_terminal_dispatch():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request())
    terminal_entered = Event()
    terminal_release = Event()
    hook = harness.projector.hooks[
        package.HistoryFactKind.REANALYSIS_COMPLETED
    ]
    hook.entered = terminal_entered
    hook.release = terminal_release
    _, sink = harness.scheduler.reanalysis_calls[0]
    report = reanalysis_report()
    first = Thread(target=lambda: sink(report))
    first.start()
    assert terminal_entered.wait(2)

    sink(report)
    terminal_release.set()
    first.join(2)

    assert not first.is_alive()
    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED
    ]
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.publish_calls == []
    assert coordinator.snapshot().restart_required is True
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert len(harness.diagnostics.calls) == 1


def test_duplicate_detection_and_quarantine_are_atomic_against_reconciliation(
    monkeypatch,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    submit_entered = Event()
    submit_release = Event()
    harness.scheduler.hook.entered = submit_entered
    harness.scheduler.hook.release = submit_release
    coordinator = harness.coordinator()
    result_box = []
    submit_done = Event()

    def submit():
        result_box.append(coordinator.start_reanalysis(reanalysis_request()))
        submit_done.set()

    submit_thread = Thread(
        target=submit
    )
    submit_thread.start()
    assert submit_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    report = reanalysis_report()
    sink(report)

    detection_entered = Event()
    quarantine_release = Event()
    original = coordinator._quarantine_result_report_channel_locked

    def blocked_locked_quarantine(context, stage, reason):
        detection_entered.set()
        quarantine_release.wait()
        return original(context, stage, reason)

    monkeypatch.setattr(
        coordinator,
        "_quarantine_result_report_channel_locked",
        blocked_locked_quarantine,
    )
    duplicate_thread = Thread(
        target=lambda: sink(report)
    )
    duplicate_thread.start()
    assert detection_entered.wait(2)
    submit_release.set()
    assert submit_done.wait(0.2) is False
    quarantine_release.set()
    duplicate_thread.join(2)
    submit_thread.join(2)

    assert not duplicate_thread.is_alive()
    assert not submit_thread.is_alive()
    assert harness.history.calls == []
    assert result_box[0].status is package.OperationStatus.PERSISTENCE_REJECTED
    assert coordinator.snapshot().restart_required is True
    assert len(harness.diagnostics.calls) == 1


@pytest.mark.parametrize("sink_raises", [False, True])
def test_manual_actual_sink_attempt_advances_operation_cursor(sink_raises):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    if sink_raises:
        harness.history.exception = RuntimeError("manual history failed")
    entered = Event()
    release = Event()
    hook = harness.projector.hooks[
        package.HistoryFactKind.TERMINAL_RESULT_REPLACED
    ]
    hook.entered = entered
    hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    worker = Thread(
        target=lambda: result_box.append(
            coordinator.apply_manual(manual_request(revision=7))
        )
    )
    worker.start()
    assert entered.wait(2)
    with coordinator._lock:
        context = coordinator._result_operations[RUN_IDS[0]]
        assert context.last_published_revision == 7
    release.set()
    worker.join(2)

    assert result_box[0].status is package.OperationStatus.COMPLETED
    assert context.last_published_revision == 8


@pytest.mark.parametrize("sink_raises", [False, True])
def test_reanalysis_start_and_terminal_sink_attempts_advance_exact_cursor(
    sink_raises,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    if sink_raises:
        harness.history.exception = RuntimeError("reanalysis history failed")
    coordinator = harness.coordinator()

    coordinator.start_reanalysis(reanalysis_request(revision=7))
    with coordinator._lock:
        context = coordinator._result_operations[RUN_IDS[0]]
    assert context.last_published_revision == 8

    harness.scheduler.complete_report(reanalysis_report())

    assert context.last_published_revision == 9
    assert coordinator.snapshot().result_operation_attempt_ids == ()


def test_projector_failure_and_suppressed_terminal_do_not_advance_cursor():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.projector.exception = RuntimeError("projection failed")
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request(revision=7))
    with coordinator._lock:
        context = coordinator._result_operations[RUN_IDS[0]]

    assert context.last_published_revision == 7
    harness.projector.exception = None
    assert coordinator.history_evicted(
        package.HistoryEvictionNotice(RUN_IDS[0])
    ) is True
    harness.scheduler.complete_report(reanalysis_report())

    assert context.last_published_revision == 7
    assert [event.kind for event in harness.history.calls] == []


def test_reanalysis_not_acquired_retires_and_keeps_prior_card():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=False,
            code="REANALYSIS_RUN_NOT_FOUND",
            reason=None,
            submit_receipt=None,
        )
    )
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is package.OperationStatus.SCHEDULER_REJECTED
    assert harness.history.calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()


@pytest.mark.parametrize(
    ("code", "restart_required"),
    [
        ("ANALYSIS_DB_CLOSED", True),
        ("REANALYSIS_RUN_NOT_FOUND", False),
    ],
)
def test_reanalysis_not_acquired_restart_matrix_is_fail_closed_without_writes(
    code,
    restart_required,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=restart_required,
            code=code,
            reason=None,
            submit_receipt=None,
        )
    )
    coordinator = harness.coordinator()
    diagnostic_lock_states = []
    original_record = harness.diagnostics.record

    def record(fact):
        diagnostic_lock_states.append(coordinator._lock._is_owned())
        original_record(fact)

    harness.diagnostics.record = record

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is package.OperationStatus.SCHEDULER_REJECTED
    assert coordinator.snapshot().restart_required is restart_required
    assert coordinator.snapshot().lifecycle is (
        package.CoordinatorLifecycle.CLOSED
        if restart_required
        else package.CoordinatorLifecycle.OPEN
    )
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert harness.history.calls == []
    assert harness.persistence.manual_calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.register_calls == []
    assert harness.plc.publish_calls == []
    assert harness.plc.reject_calls == []
    assert len(harness.diagnostics.calls) == int(restart_required)
    assert diagnostic_lock_states == ([False] if restart_required else [])
    if restart_required:
        assert code in harness.diagnostics.calls[0].message


def test_not_acquired_reconciliation_cannot_retire_across_callback_storage_gap(
    monkeypatch,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=False,
            code="REANALYSIS_RUN_NOT_FOUND",
            reason=None,
            submit_receipt=None,
        )
    )
    coordinator = harness.coordinator()
    retire_entered = Event()
    retire_release = Event()
    callback_done = Event()
    original_retire = coordinator._retire_result_operation

    def blocked_retire(context, status, reason):
        retire_entered.set()
        retire_release.wait()
        return original_retire(context, status, reason)

    monkeypatch.setattr(
        coordinator,
        "_retire_result_operation",
        blocked_retire,
    )
    result_box = []
    submit_thread = Thread(
        target=lambda: result_box.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )
    submit_thread.start()
    assert retire_entered.wait(2)
    _, sink = harness.scheduler.reanalysis_calls[0]
    callback_thread = Thread(
        target=lambda: (sink(reanalysis_report()), callback_done.set())
    )
    callback_thread.start()
    callback_crossed_gap = callback_done.wait(0.2)
    retire_release.set()
    submit_thread.join(2)
    callback_thread.join(2)

    assert callback_crossed_gap is False
    assert not submit_thread.is_alive()
    assert not callback_thread.is_alive()
    assert result_box[0].status is package.OperationStatus.SCHEDULER_REJECTED
    assert harness.history.calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert coordinator.snapshot().restart_required is True
    assert len(harness.diagnostics.calls) == 1


@pytest.mark.parametrize(
    ("authority", "restart_required", "code", "expected_status"),
    [
        (
            BridgeAuthorityStatus.NOT_ACQUIRED,
            False,
            "REANALYSIS_RUN_NOT_FOUND",
            package.OperationStatus.SCHEDULER_REJECTED,
        ),
        (
            BridgeAuthorityStatus.NOT_ACQUIRED,
            True,
            "ANALYSIS_DB_CLOSED",
            package.OperationStatus.SCHEDULER_REJECTED,
        ),
        (
            BridgeAuthorityStatus.UNKNOWN,
            True,
            "ATTEMPT_QUEUE_CALL_FAILED",
            package.OperationStatus.PERSISTENCE_REJECTED,
        ),
    ],
)
def test_nonacquired_authority_atomically_quarantines_current_held_report(
    authority,
    restart_required,
    code,
    expected_status,
):
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=(
                BridgeSubmissionStatus.REJECTED
                if authority is BridgeAuthorityStatus.NOT_ACQUIRED
                else BridgeSubmissionStatus.STATE_UNKNOWN
            ),
            authority=authority,
            run_id=RUN_IDS[0],
            analysis_attempt_id=(
                None
                if authority is BridgeAuthorityStatus.NOT_ACQUIRED
                else ATTEMPT_IDS[0]
            ),
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=restart_required,
            code=code,
            reason=(
                "queue call failed"
                if authority is BridgeAuthorityStatus.UNKNOWN
                else None
            ),
            submit_receipt=None,
        )
    )
    harness.scheduler.synchronous_report = reanalysis_report()
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is expected_status
    assert coordinator.snapshot().restart_required is True
    assert coordinator.snapshot().result_operation_attempt_ids == ()
    assert harness.history.calls == []
    assert harness.persistence.terminal_calls == []
    assert harness.scheduler.cancel_calls == []
    assert harness.plc.publish_calls == []
    assert len(harness.diagnostics.calls) == 1


def test_reanalysis_bridge_submission_status_restart_matrix_is_exactly_closed():
    accepted = Harness().scheduler.accepted_bridge_result(RUN_IDS[0])
    accepted_early = Harness().scheduler.accepted_bridge_result(
        RUN_IDS[0],
        early=True,
    )
    rows = [
        accepted,
        accepted_early,
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=False,
            code="REANALYSIS_RUN_NOT_FOUND",
            reason=None,
            submit_receipt=None,
        ),
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=True,
            code="ANALYSIS_DB_CLOSED",
            reason=None,
            submit_receipt=None,
        ),
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            run_id=RUN_IDS[0],
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=True,
            code="ATTEMPT_QUEUE_CALL_FAILED",
            reason="queue call failed",
            submit_receipt=None,
        ),
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=False,
            code="ANALYSIS_SCHEDULER_SUBMIT_FAILED",
            reason=None,
            submit_receipt=None,
        ),
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=RUN_IDS[0],
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=True,
            terminal_delivered_before_return=True,
            restart_required=True,
            code="ATTEMPT_TERMINAL_RESULT_INVALID",
            reason=None,
            submit_receipt=accepted.submit_receipt,
        ),
    ]

    assert {
        (row.status, row.authority, row.restart_required)
        for row in rows
    } == {
        (
            BridgeSubmissionStatus.ACCEPTED,
            BridgeAuthorityStatus.ACQUIRED,
            False,
        ),
        (
            BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL,
            BridgeAuthorityStatus.ACQUIRED,
            False,
        ),
        (
            BridgeSubmissionStatus.REJECTED,
            BridgeAuthorityStatus.NOT_ACQUIRED,
            False,
        ),
        (
            BridgeSubmissionStatus.REJECTED,
            BridgeAuthorityStatus.NOT_ACQUIRED,
            True,
        ),
        (
            BridgeSubmissionStatus.STATE_UNKNOWN,
            BridgeAuthorityStatus.UNKNOWN,
            True,
        ),
        (
            BridgeSubmissionStatus.REJECTED,
            BridgeAuthorityStatus.ACQUIRED,
            False,
        ),
        (
            BridgeSubmissionStatus.STATE_UNKNOWN,
            BridgeAuthorityStatus.ACQUIRED,
            True,
        ),
    }


def test_reanalysis_unknown_latches_restart_without_history_or_persistence_write():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    harness.scheduler.bridge_results.append(
        BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            run_id=RUN_IDS[0],
            analysis_attempt_id=ATTEMPT_IDS[0],
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=True,
            code="ATTEMPT_QUEUE_CALL_FAILED",
            reason="queue commit call failed",
            submit_receipt=None,
        )
    )
    coordinator = harness.coordinator()

    result = coordinator.start_reanalysis(reanalysis_request())

    assert result.status is package.OperationStatus.PERSISTENCE_REJECTED
    assert coordinator.snapshot().restart_required is True
    assert harness.history.calls == []
    assert harness.persistence.terminal_calls == []


def test_asynchronous_committed_reanalysis_uses_operation_local_terminal_revision():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    submitted = coordinator.start_reanalysis(reanalysis_request(revision=11))
    with coordinator._lock:
        context = coordinator._result_operations[RUN_IDS[0]]

    harness.scheduler.complete_report(reanalysis_report())

    assert submitted.attempt_id == ATTEMPT_IDS[0]
    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED,
        package.HistoryFactKind.REANALYSIS_COMPLETED,
    ]
    assert (
        harness.history.calls[1].expected_revision,
        harness.history.calls[1].proposed_revision,
    ) == (12, 13)
    assert context.facts is None
    assert context.audio is None
    assert context.early_report is None


def test_explicit_cancel_uses_user_stop_once_and_receipt_is_not_terminal():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    submitted = coordinator.start_reanalysis(reanalysis_request())

    result = coordinator.cancel_reanalysis(
        package.ReanalysisCancelRequest(
            run_id=RUN_IDS[0],
            attempt_id=submitted.attempt_id,
            expected_revision=8,
        )
    )

    assert result.status is package.OperationStatus.SUBMITTED
    assert len(harness.scheduler.cancel_requests) == 1
    assert harness.scheduler.cancel_requests[0].origin is AnalysisCancelOrigin.USER_STOP
    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED
    ]
    assert coordinator.snapshot().result_operation_attempt_ids == (ATTEMPT_IDS[0],)


def test_explicit_cancel_terminal_report_restores_prior_card_at_local_revision():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    submitted = coordinator.start_reanalysis(reanalysis_request())
    coordinator.cancel_reanalysis(
        package.ReanalysisCancelRequest(
            run_id=RUN_IDS[0],
            attempt_id=submitted.attempt_id,
            expected_revision=8,
        )
    )

    harness.scheduler.complete_report(
        reanalysis_report(
            state=AttemptState.CANCELED,
            judgment=None,
            result_json=None,
            code="REANALYSIS_CANCELED",
        )
    )

    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED,
        package.HistoryFactKind.REANALYSIS_CANCELLED,
    ]
    assert harness.history.calls[-1].projection == result_facts().current_projection
    assert harness.persistence.terminal_calls == []


def test_user_cancel_and_later_eviction_each_dispatch_their_exact_origin_once():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    submitted = coordinator.start_reanalysis(reanalysis_request())
    cancel = package.ReanalysisCancelRequest(
        run_id=RUN_IDS[0],
        attempt_id=submitted.attempt_id,
        expected_revision=8,
    )

    coordinator.cancel_reanalysis(cancel)
    coordinator.cancel_reanalysis(cancel)
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is True
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is False

    assert [request.origin for request in harness.scheduler.cancel_requests] == [
        AnalysisCancelOrigin.USER_STOP,
        AnalysisCancelOrigin.RESULT_EVICTION,
    ]


def test_eviction_during_bridge_submit_is_frozen_before_acquisition_and_cancels_once():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    entered = Event()
    release = Event()
    harness.scheduler.hook.entered = entered
    harness.scheduler.hook.release = release
    coordinator = harness.coordinator()
    result_box = []
    worker = Thread(
        target=lambda: result_box.append(
            coordinator.start_reanalysis(reanalysis_request())
        )
    )

    worker.start()
    assert entered.wait(2)
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is True
    release.set()
    worker.join(2)

    assert result_box[0].status is package.OperationStatus.SUBMITTED
    assert harness.history.calls == []
    assert len(harness.scheduler.cancel_requests) == 1
    assert harness.scheduler.cancel_requests[0].origin is AnalysisCancelOrigin.RESULT_EVICTION


def test_matching_eviction_suppresses_history_then_cancels_acquired_reanalysis_once():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request())

    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is True
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is False
    assert len(harness.scheduler.cancel_requests) == 1
    assert harness.scheduler.cancel_requests[0].origin is AnalysisCancelOrigin.RESULT_EVICTION

    harness.scheduler.complete_report(reanalysis_report())
    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED
    ]
    assert coordinator.snapshot().result_operation_attempt_ids == ()


def test_eviction_wins_between_terminal_projection_and_dispatch_without_late_cancel():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request())
    entered = Event()
    release = Event()
    hook = harness.projector.hooks[package.HistoryFactKind.REANALYSIS_COMPLETED]
    hook.entered = entered
    hook.release = release
    worker = Thread(
        target=lambda: harness.scheduler.complete_report(reanalysis_report())
    )

    worker.start()
    assert entered.wait(2)
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[0])) is True
    release.set()
    worker.join(2)

    assert [event.kind for event in harness.history.calls] == [
        package.HistoryFactKind.REANALYSIS_STARTED
    ]
    assert harness.scheduler.cancel_requests == []
    assert coordinator.snapshot().result_operation_attempt_ids == ()


def test_history_eviction_notice_requires_exact_canonical_uuid4():
    for run_id in (
        "",
        "00000000-0000-3000-8000-000000000001",
        "00000000-0000-4000-7000-000000000001",
        "00000000-0000-4000-8000-00000000000A",
        "{00000000-0000-4000-8000-000000000001}",
        "00000000000040008000000000000001",
    ):
        with pytest.raises(ValueError):
            package.HistoryEvictionNotice(run_id=run_id)


def test_unrelated_or_malformed_eviction_is_noop():
    harness = Harness()
    harness.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    coordinator = harness.coordinator()
    coordinator.start_reanalysis(reanalysis_request())

    assert coordinator.history_evicted(object()) is False
    assert coordinator.history_evicted(package.HistoryEvictionNotice(RUN_IDS[1])) is False
    assert harness.scheduler.cancel_calls == []
