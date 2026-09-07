from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from threading import Event

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QPaintEvent
from PyQt6.QtWidgets import QApplication, QLabel, QTableWidget, QWidget

from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter
from app.services_container import _PostDurableHistorySink
from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    FrozenPayload,
    ProjectedHistoryEvent,
    ResultProjection,
    ResultProjectionPhase,
    TerminalConclusion,
)
from ui.modules.result_dashboard import ResultDashboardModule
from ui.widgets.result_history_card import ResultHistoryCard
from tests.multi_run_integration.fakes import history_labels
from tests.test_result_dashboard_module import _config, _panel, _services

from app.service.analysis.scheduler import AnalysisBundleIdentity
from app.service.multi_run_coordinator_mvp import (
    HistoryFactKind,
    ResultEnrichmentProjection,
    TriggerSource,
)
from app.service.run_persistence import (
    AttemptKind,
    AttemptState,
    BridgeTerminalReport,
    BridgeTerminalStatus,
    EnrichmentCommittedEvent,
    Judgment,
    TransactionResult,
    TransactionState,
)
from app.service.plc_result_sequencer import (
    PlcAttemptOutcome,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcResultSequencer,
)
from app.service.result_history import (
    ActiveRunRemoved,
    ResultHistorySnapshot,
    ResultHistoryStore,
    ReviewDisplayPayload,
)
from app.service.result_history_contracts import ResultOperationAccepted
from tests.multi_run_coordinator_mvp._support import (
    ATTEMPT_IDS,
    Harness,
    captured_request,
    completed_capture,
    saved_outcome,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _canonical_result(*, charts: dict[str, object]) -> str:
    return json.dumps(
        {
            "judgment": "PASS",
            "result": {
                "chart_payload": charts,
                "config_payload": {
                    "config_id": 7,
                    "config_name": "配置 A",
                    "selected_channels": [0],
                    "result_dashboard_layout": {
                        "version": 2,
                        "rows": 1,
                        "columns": 1,
                        "panels": [
                            {
                                "id": "spectrum",
                                "type": "spectrum",
                                "row": 0,
                                "column": 0,
                                "row_span": 1,
                                "column_span": 1,
                                "channel": "selected_1",
                            }
                        ],
                    },
                },
                "judgment_payload": {"overall_pass": True},
                "summary_payload": {
                    "overall_status": "pass",
                    "overall_label": "通过",
                    "rows": [],
                },
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _production_shaped_result(
    *,
    charts: dict[str, object],
    complete: bool,
    summary_valid: bool = True,
) -> str:
    result = json.loads(_canonical_result(charts=charts))
    if not summary_valid:
        result["result"]["summary_payload"] = ["invalid"]
        return json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    measurement_displays = (
        {
            "spl": "58.14 dB",
            "spl_average": "53.71 dB",
            "loudness": "11.10 sone",
            "loudness_average": "8.08 sone",
            "stft_score": "0.83",
        }
        if complete
        else {
            "spl": "--",
            "spl_average": "--",
            "loudness": "--",
            "loudness_average": "--",
            "stft_score": "--",
        }
    )
    rows = [
        {
            "item_key": key,
            "actual_display": display,
            "current_display": "CH0",
            "status": "pass",
            "result_display": "PASS",
        }
        for key, display in measurement_displays.items()
    ]
    rows.extend(
        (
            {
                "item_key": "ai",
                "actual_display": "1.00 %" if complete else "97.50 %",
                "current_display": "CH0",
                "status": "fail" if complete else "pass",
                "result_display": "FAIL" if complete else "PASS",
            },
            {
                "item_key": "fft_score",
                "actual_display": "0.91",
                "current_display": "CH0",
                "status": "pass",
                "result_display": "PASS",
            },
        )
    )
    result["result"]["summary_payload"]["rows"] = rows
    return json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _history_card(dashboard: ResultDashboardModule, run_id: str):
    return next(
        card
        for card in dashboard._history_module.findChildren(ResultHistoryCard)
        if card.property("run_id") == run_id and not card.isHidden()
    )


def _card_table_values(card: ResultHistoryCard) -> tuple[str, ...]:
    table = card.findChild(QTableWidget, "result_history_card_summary_table")
    assert table is not None
    return tuple(table.item(row, 1).text() for row in range(table.rowCount()))


def _assert_complete_card(card: ResultHistoryCard) -> None:
    assert _card_table_values(card) == (
        "58.14 dB",
        "53.71 dB",
        "11.10 sone",
        "8.08 sone",
        "0.83",
        "97.50 %",
    )
    assert card.findChild(
        QLabel, "result_history_card_metric_spl"
    ).text() == "53.71 dB"
    assert card.findChild(
        QLabel, "result_history_card_metric_loudness"
    ).text() == "8.08 sone"


def _bridge_payload(attempt_id: str, value: str) -> FrozenPayload:
    return FrozenPayload(
        value_id=f"bridge-result:{attempt_id}",
        canonical_bytes=value.encode("utf-8"),
    )


def _decision_history_event(
    *,
    run_id: str,
    attempt_id: str,
    decision_json: str,
    terminal_revision: str,
    revision: int = 4,
    fault_types: tuple[str, ...] = (),
    conclusion: TerminalConclusion = TerminalConclusion.OK,
) -> ProjectedHistoryEvent:
    return ProjectedHistoryEvent(
        kind=HistoryFactKind.RUN_TERMINATED,
        run_id=run_id,
        expected_revision=revision - 1,
        operation_id=attempt_id,
        proposed_revision=revision,
        projection=ResultProjection(
            phase=ResultProjectionPhase.TERMINAL,
            conclusion=conclusion,
            error_category=None,
            reason="",
            history_labels=history_labels(
                config_id=7,
                fault_types=fault_types,
            ),
            result_payload=_bridge_payload(attempt_id, decision_json),
            terminal_revision=terminal_revision,
        ),
    )


def _manual_history_event(
    *,
    run_id: str,
    attempt_id: str,
    decision_json: str,
    expected_revision: int,
    fault_type: str,
) -> ProjectedHistoryEvent:
    return ProjectedHistoryEvent(
        kind=HistoryFactKind.TERMINAL_RESULT_REPLACED,
        run_id=run_id,
        expected_revision=expected_revision,
        operation_id=None,
        proposed_revision=expected_revision + 1,
        projection=ResultProjection(
            phase=ResultProjectionPhase.TERMINAL,
            conclusion=TerminalConclusion.NG,
            error_category=None,
            reason="",
            history_labels=history_labels(
                config_id=7,
                fault_types=("异响", "漏装"),
            ),
            result_payload=_bridge_payload(attempt_id, decision_json),
            fault_type=fault_type,
            manual_judgment_revision=expected_revision + 1,
        ),
    )


def _active_recording_event(*, run_id: str) -> ProjectedHistoryEvent:
    return ProjectedHistoryEvent(
        kind=HistoryFactKind.RECORDING,
        run_id=run_id,
        expected_revision=ABSENT,
        operation_id=None,
        proposed_revision=1,
        projection=ResultProjection(
            phase=ResultProjectionPhase.RECORDING,
            conclusion=None,
            error_category=None,
            reason="",
            history_labels=history_labels(config_id=7),
            result_payload=None,
        ),
    )


def _reanalysis_event(
    *,
    kind: HistoryFactKind,
    run_id: str,
    operation_id: str,
    expected_revision: int,
    conclusion: TerminalConclusion = TerminalConclusion.NG,
    result_payload: FrozenPayload | None = None,
    terminal_revision: str | None = None,
) -> ProjectedHistoryEvent:
    phase = (
        ResultProjectionPhase.REANALYZING
        if kind is HistoryFactKind.REANALYSIS_STARTED
        else ResultProjectionPhase.TERMINAL
    )
    return ProjectedHistoryEvent(
        kind=kind,
        run_id=run_id,
        expected_revision=expected_revision,
        operation_id=operation_id,
        proposed_revision=expected_revision + 1,
        projection=ResultProjection(
            phase=phase,
            conclusion=conclusion,
            error_category=None,
            reason="",
            history_labels=history_labels(
                config_id=7,
                fault_types=("异响", "漏装"),
            ),
            result_payload=result_payload,
            terminal_revision=terminal_revision,
        ),
    )


def _drain(qapp, attempts: int = 10) -> None:
    for _ in range(attempts):
        qapp.processEvents()


def _drive_decision(harness: Harness, decision_json: str):
    coordinator = harness.coordinator()
    request = captured_request(source=TriggerSource.PLC)
    coordinator.start_captured(request)
    harness.resident.complete(completed_capture(harness.resident.register_calls[0]))
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
            terminal_at="2026-08-31T09:00:00.000000Z",
            persistence_result=TransactionResult(
                TransactionState.COMMITTED,
                True,
            ),
            restart_required=False,
        )
    )
    return coordinator, request.run_id


def _enrichment_event(
    *,
    run_id: str,
    terminal_revision: str,
    result_json: str,
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
        result_payload_json=result_json,
        terminal_revision=terminal_revision,
        committed_at="2026-08-31T09:00:00.100000Z",
    )


def test_matching_enrichment_is_exact_revision_ui_only_and_idempotent() -> None:
    harness = Harness()
    decision_json = _canonical_result(charts={})
    coordinator, run_id = _drive_decision(harness, decision_json)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    full_json = _canonical_result(
        charts={"0": {"fft": {"frequencies_hz": [10.0], "levels_db": [-3.0]}}}
    )
    terminal_history = [
        event
        for event in harness.history.calls
        if event.kind is HistoryFactKind.RUN_TERMINATED
    ]
    history_count = len(harness.history.calls)
    plc_count = len(harness.plc.publish_calls)
    terminal_history_trace = harness.trace.index("history:RUN_TERMINATED")
    plc_trace = harness.trace.index("plc_publish")

    # The bridge report supplied above is already durably COMMITTED.  The
    # coordinator must not create a second terminal and must publish UI/history
    # before the ordered PLC settlement.
    assert harness.persistence.terminal_calls == []
    assert terminal_history_trace < plc_trace

    projected = coordinator.project_enrichment(
        _enrichment_event(
            run_id=run_id,
            terminal_revision=terminal_revision,
            result_json=full_json,
        )
    )

    assert type(projected) is ResultEnrichmentProjection
    assert projected.run_id == run_id
    assert projected.attempt_id == ATTEMPT_IDS[0]
    assert projected.terminal_revision == terminal_revision
    assert projected.expected_revision == terminal_history[0].proposed_revision
    assert projected.result_payload.canonical_bytes == full_json.encode("utf-8")
    assert len(harness.plc.publish_calls) == plc_count
    assert len(harness.history.calls) == history_count
    assert coordinator.project_enrichment(
        _enrichment_event(
            run_id=run_id,
            terminal_revision=terminal_revision,
            result_json=full_json,
        )
    ) is None


def test_stale_enrichment_revision_is_ignored_without_terminal_or_plc_action() -> None:
    harness = Harness()
    decision_json = _canonical_result(charts={})
    coordinator, run_id = _drive_decision(harness, decision_json)
    history_count = len(harness.history.calls)
    plc_count = len(harness.plc.publish_calls)

    projected = coordinator.project_enrichment(
        _enrichment_event(
            run_id=run_id,
            terminal_revision="0" * 64,
            result_json=_canonical_result(charts={"0": {"fft": {}}}),
        )
    )

    assert projected is None
    assert len(harness.history.calls) == history_count
    assert len(harness.plc.publish_calls) == plc_count


def test_dashboard_paints_summary_then_fills_matching_charts_without_card_change(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _canonical_result(charts={})
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    full_json = _canonical_result(
        charts={
            "0": {
                "fft": {
                    "frequencies_hz": [100.0, 200.0],
                    "levels_db": [-3.0, -4.0],
                }
            }
        }
    )
    config = _config([_panel("spectrum", "spectrum")], selected_channels=(0,))
    services = _services()
    services.config_service.load_config_for_result_dashboard.return_value = config
    dashboard = ResultDashboardModule(services)
    adapter = HistoryQtAdapter(dashboard)
    painted = []
    dashboard.decisionPainted.connect(
        lambda revision, painted_at: painted.append((revision, painted_at))
    )
    dashboard.show()
    mapped_decision = adapter._map_event(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
        )
    )
    dashboard.apply_history_event(mapped_decision)

    decision_snapshot = dashboard.history_snapshot()
    spectrum = dashboard.panel_widget("spectrum")
    summary = dashboard._history_module.findChild(
        QLabel,
        "result_history_card_summary_overall",
    )
    caption = dashboard._history_module.findChild(
        QLabel,
        "result_history_card_summary_caption",
    )
    assert summary is not None
    assert caption is not None
    assert summary.text() == "通过"
    assert caption.text() == "结果摘要"
    assert painted == []
    QWidget.paintEvent(dashboard, QPaintEvent(dashboard.rect()))
    assert painted == []
    _drain(qapp)
    assert painted and painted[-1][0] == terminal_revision
    assert decision_snapshot.selected_run_id == run_id
    assert decision_snapshot.terminal_cards[0].revision == 4
    assert (
        dashboard._factory_display.review_payload.summary_payload[
            "overall_status"
        ]
        == "pass"
    )
    assert spectrum is not None
    assert spectrum.has_committed_plot_data() is False

    observer_calls = []
    post_durable_sink = _PostDurableHistorySink(
        adapter,
        observer_calls.append,
        lambda _message: None,
    )
    post_durable_sink.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(attempt_id, full_json),
        )
    )

    assert spectrum.has_committed_plot_data() is False
    _drain(qapp)
    after = dashboard.history_snapshot()
    assert after.selected_run_id == decision_snapshot.selected_run_id
    assert after.terminal_cards[0] == decision_snapshot.terminal_cards[0]
    assert spectrum.has_committed_plot_data() is True
    assert observer_calls == []
    assert len(painted) == 1
    dashboard._history_module.decisionPainted.emit(
        ("stale-run", attempt_id, terminal_revision, 4),
        1,
    )
    assert len(painted) == 1
    dashboard.refresh_history_projection()
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True


def test_selected_card_refreshes_complete_enrichment_summary_in_place(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    full_json = _production_shaped_result(
        charts={
            "0": {
                "fft": {
                    "frequencies_hz": [100.0, 200.0],
                    "levels_db": [-3.0, -4.0],
                }
            }
        },
        complete=True,
    )
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    painted = []
    dashboard.decisionPainted.connect(lambda *args: painted.append(args))
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
        )
    )
    _drain(qapp)

    before_snapshot = dashboard.history_snapshot()
    before_card = _history_card(dashboard, run_id)
    before_terminal = before_snapshot.terminal_cards[0]
    assert _card_table_values(before_card) == (
        "--",
        "--",
        "--",
        "--",
        "--",
        "97.50 %",
    )
    assert before_card.findChild(
        QLabel, "result_history_card_metric_spl"
    ).text() == "--"
    assert before_card.findChild(
        QLabel, "result_history_card_metric_loudness"
    ).text() == "--"
    assert len(painted) == 1

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(attempt_id, full_json),
        )
    )
    _drain(qapp)

    after_snapshot = dashboard.history_snapshot()
    after_card = _history_card(dashboard, run_id)
    assert after_card is before_card
    _assert_complete_card(after_card)
    assert after_card.findChild(
        QLabel, "result_history_card_summary_overall"
    ).text() == "通过"
    assert after_snapshot.revision == before_snapshot.revision
    assert after_snapshot.selected_run_id == before_snapshot.selected_run_id
    assert after_snapshot.terminal_cards == before_snapshot.terminal_cards
    assert after_snapshot.terminal_cards[0].terminated_at == before_terminal.terminated_at
    assert tuple(card.run_id for card in after_snapshot.terminal_cards) == tuple(
        card.run_id for card in before_snapshot.terminal_cards
    )
    assert len(painted) == 1
    dashboard.close()
    _drain(qapp)


def test_pending_manual_preview_survives_enrichment_and_refresh(qapp) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    fault_type = "异响"
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
            fault_types=(fault_type, "漏装"),
        )
    )
    _drain(qapp)
    card = _history_card(dashboard, run_id)
    request = dashboard.request_manual_judgment("NG", fault_type)
    assert type(request) is ResultOperationAccepted
    preview = (
        card.findChild(QLabel, "result_history_card_summary_overall").text(),
        card.property("resultState"),
        card.accessibleDescription(),
    )
    assert preview == ("不通过：异响", "ng", "结果状态：不通过")
    assert "#d93025" in card.findChild(
        QLabel, "result_history_card_summary_overall"
    ).styleSheet().lower()

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(
                attempt_id,
                _production_shaped_result(charts={}, complete=True),
            ),
        )
    )
    _drain(qapp)
    dashboard.refresh_history_projection()

    assert _history_card(dashboard, run_id) is card
    _assert_complete_card(card)
    assert (
        card.findChild(QLabel, "result_history_card_summary_overall").text(),
        card.property("resultState"),
        card.accessibleDescription(),
    ) == preview
    assert card._snapshot.conclusion.name == "NG"
    assert card._snapshot.fault_type == fault_type

    adapter.publish(
        _manual_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            expected_revision=4,
            fault_type=fault_type,
        )
    )
    _drain(qapp)

    committed = dashboard.history_snapshot().terminal_cards[0]
    assert committed.conclusion.name == "NG"
    assert committed.fault_type == fault_type
    assert committed.manual_judgment_revision == committed.revision == 5
    _assert_complete_card(card)
    assert card.findChild(
        QLabel, "result_history_card_summary_overall"
    ).text() == "不通过：异响"
    assert card.property("resultState") == "ng"
    dashboard.close()
    _drain(qapp)


def test_valid_enrichment_chart_with_invalid_summary_preserves_card_text(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
        )
    )
    _drain(qapp)
    card = _history_card(dashboard, run_id)
    before = (
        _card_table_values(card),
        card.findChild(QLabel, "result_history_card_metric_spl").text(),
        card.findChild(QLabel, "result_history_card_metric_loudness").text(),
    )

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(
                attempt_id,
                _production_shaped_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    },
                    complete=True,
                    summary_valid=False,
                ),
            ),
        )
    )
    _drain(qapp)

    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    assert (
        _card_table_values(card),
        card.findChild(QLabel, "result_history_card_metric_spl").text(),
        card.findChild(QLabel, "result_history_card_metric_loudness").text(),
    ) == before
    dashboard.close()
    _drain(qapp)


def test_partial_enrichment_overlay_preserves_missing_current_rows_and_order():
    current = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_status": "fail",
                "overall_label": "不通过（异响）",
                "error_message": "decision-error",
                "manual_fault": "异响",
                "rows": [
                    {"item_key": "spl", "actual_display": "--"},
                    {"item_key": "spl_average", "actual_display": "40.00 dB"},
                    {"item_key": "loudness", "actual_display": "--"},
                    {
                        "item_key": "loudness_average",
                        "actual_display": "4.00 sone",
                    },
                    {"item_key": "stft_score", "actual_display": "0.40"},
                    {
                        "item_key": "ai",
                        "actual_display": "97.50 %",
                        "status": "pass",
                        "result_display": "PASS",
                    },
                    {"item_key": "octave_score", "actual_display": "0.75"},
                ],
            },
            "chart_payload": {"old": {"value": 1}},
            "decision_only": "preserved",
        },
        dashboard_config={"config_id": 7, "layout": "decision"},
    )
    enrichment = ReviewDisplayPayload(
        review={
            "summary_payload": {
                "overall_status": "pass",
                "overall_label": "通过",
                "error_message": "enrichment-error",
                "manual_fault": "overwritten",
                "rows": [
                    {"item_key": "spl", "actual_display": "58.14 dB"},
                    {
                        "item_key": "loudness_average",
                        "actual_display": "8.08 sone",
                    },
                    {
                        "item_key": "ai",
                        "actual_display": "1.00 %",
                        "status": "fail",
                        "result_display": "FAIL",
                    },
                    {"item_key": "fft_score", "actual_display": "0.91"},
                ],
            },
            "chart_payload": {"0": {"fft": {"frequencies_hz": [100.0]}}},
        },
        dashboard_config={"config_id": 99, "layout": "enrichment"},
    )
    current_before = deepcopy(current)
    enrichment_before = deepcopy(enrichment)

    overlay = ResultDashboardModule._merge_enrichment_display_overlay(
        current,
        enrichment,
    )

    assert overlay.dashboard_config == current.dashboard_config
    assert overlay.review["decision_only"] == "preserved"
    assert overlay.review["chart_payload"] == enrichment.review["chart_payload"]
    summary = overlay.review["summary_payload"]
    assert {
        key: summary[key]
        for key in (
            "overall_status",
            "overall_label",
            "error_message",
            "manual_fault",
        )
    } == {
        "overall_status": "fail",
        "overall_label": "不通过（异响）",
        "error_message": "decision-error",
        "manual_fault": "异响",
    }
    assert [row["item_key"] for row in summary["rows"]] == [
        "spl",
        "spl_average",
        "loudness",
        "loudness_average",
        "stft_score",
        "ai",
        "octave_score",
        "fft_score",
    ]
    rows = {row["item_key"]: row for row in summary["rows"]}
    assert rows["spl"]["actual_display"] == "58.14 dB"
    assert rows["spl_average"]["actual_display"] == "40.00 dB"
    assert rows["loudness"]["actual_display"] == "--"
    assert rows["loudness_average"]["actual_display"] == "8.08 sone"
    assert rows["stft_score"]["actual_display"] == "0.40"
    assert rows["octave_score"]["actual_display"] == "0.75"
    assert rows["fft_score"]["actual_display"] == "0.91"
    assert rows["ai"]["actual_display"] == "97.50 %"
    assert rows["ai"]["result_display"] == "PASS"
    assert current == current_before
    assert enrichment == enrichment_before


def test_cached_enrichment_recovers_after_initial_card_refresh_miss(qapp) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
        )
    )
    _drain(qapp)
    card = _history_card(dashboard, run_id)
    refresh_terminal_payload = dashboard._history_module.refresh_terminal_payload
    missed = []

    def miss_first_refresh(*args):
        missed.append(args[0])
        return False

    dashboard._history_module.refresh_terminal_payload = miss_first_refresh
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(
                attempt_id,
                _production_shaped_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    },
                    complete=True,
                ),
            ),
        )
    )
    _drain(qapp)

    assert missed == [run_id]
    assert _card_table_values(card)[:5] == ("--", "--", "--", "--", "--")
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    assert dashboard._applied_enrichment_keys == {
        (run_id, attempt_id, terminal_revision)
    }

    dashboard._history_module.refresh_terminal_payload = refresh_terminal_payload
    dashboard.refresh_history_projection()
    _assert_complete_card(card)
    dashboard.close()
    _drain(qapp)


def test_mismatched_enrichment_keeps_existing_overlay_and_does_not_refresh(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    painted = []
    dashboard.decisionPainted.connect(lambda *args: painted.append(args))
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
        )
    )
    _drain(qapp)
    full_json = _production_shaped_result(charts={}, complete=True)
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(attempt_id, full_json),
        )
    )
    _drain(qapp)
    _assert_complete_card(_history_card(dashboard, run_id))
    refresh_calls = []
    refresh_terminal_payload = dashboard._history_module.refresh_terminal_payload

    def count_refresh(*args):
        refresh_calls.append(args[0])
        return refresh_terminal_payload(*args)

    dashboard._history_module.refresh_terminal_payload = count_refresh
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision="b" * 64,
            expected_revision=4,
            result_payload=_bridge_payload(attempt_id, full_json),
        )
    )
    _drain(qapp)

    _assert_complete_card(_history_card(dashboard, run_id))
    assert refresh_calls == []
    assert dashboard._applied_enrichment_keys == {
        (run_id, attempt_id, terminal_revision)
    }
    assert len(painted) == 1
    dashboard.close()
    _drain(qapp)


def test_dashboard_ignores_enrichment_after_selection_changes(qapp) -> None:
    first_run = captured_request().run_id
    second_run = "00000000-0000-4000-8000-000000000002"
    decision_json = _canonical_result(charts={})
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    config = _config([_panel("spectrum", "spectrum")], selected_channels=(0,))
    services = _services()
    services.config_service.load_config_for_result_dashboard.return_value = config
    dashboard = ResultDashboardModule(services)
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    for run_id, attempt_id, revision in (
        (first_run, ATTEMPT_IDS[0], 4),
        (second_run, "10000000-0000-4000-8000-000000000002", 2),
    ):
        adapter.publish(
            _decision_history_event(
                run_id=run_id,
                attempt_id=attempt_id,
                decision_json=decision_json,
                terminal_revision=terminal_revision,
                revision=revision,
            )
        )
        _drain(qapp)
    assert dashboard.selected_run_id() == second_run

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=first_run,
            attempt_id=ATTEMPT_IDS[0],
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(
                ATTEMPT_IDS[0],
                _canonical_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    }
                ),
            ),
        )
    )
    _drain(qapp)

    assert dashboard.selected_run_id() == second_run
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is False


def test_dashboard_ignores_enrichment_after_reanalysis_starts(qapp) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _canonical_result(charts={})
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    painted = []
    dashboard.decisionPainted.connect(
        lambda revision, painted_at: painted.append((revision, painted_at))
    )
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
        )
    )
    _drain(qapp)
    adapter.publish(
        ProjectedHistoryEvent(
            kind=HistoryFactKind.REANALYSIS_STARTED,
            run_id=run_id,
            expected_revision=4,
            operation_id="10000000-0000-4000-8000-000000000002",
            proposed_revision=5,
            projection=ResultProjection(
                phase=ResultProjectionPhase.REANALYZING,
                conclusion=TerminalConclusion.OK,
                error_category=None,
                reason="",
                history_labels=history_labels(config_id=7),
                result_payload=_bridge_payload(attempt_id, decision_json),
            ),
        )
    )
    _drain(qapp)
    painted_count = len(painted)
    dashboard._history_module.decisionPainted.emit(
        (run_id, attempt_id, terminal_revision, 4),
        1,
    )
    assert len(painted) == painted_count
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(
                attempt_id,
                _canonical_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    }
                ),
            ),
        )
    )
    _drain(qapp)

    card = dashboard.history_snapshot().terminal_cards[0]
    assert card.phase.name == "REANALYZING"
    assert card.revision == 5
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is False


def test_plc_transient_retry_reuses_one_identical_decision() -> None:
    writes = []
    reports = []

    def write_result(plc_id, payload):
        writes.append((plc_id, dict(payload)))
        if len(writes) == 1:
            raise RuntimeError("transient PLC transport failure")

    sequencer = PlcResultSequencer(
        write_result=write_result,
        report_attempt=reports.append,
    )
    registration = sequencer.register(
        run_id=captured_request().run_id,
        trigger_source="plc",
        operation_kind=PlcOperationKind.INITIAL,
        plc_id=1,
    )
    ticket = registration.ticket
    assert ticket is not None
    assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
    payload = {"finished": True, "test_result": True}

    assert (
        sequencer.publish_terminal(ticket, payload)
        is PlcMutationDisposition.APPLIED
    )

    assert writes == [(1, payload), (1, payload)]
    assert len(reports) == 1
    assert reports[0].outcome is PlcAttemptOutcome.BOUNDARY_RETURNED
    assert (
        sequencer.publish_terminal(ticket, payload)
        is PlcMutationDisposition.DUPLICATE_OR_STALE
    )
    assert len(writes) == 2


@pytest.mark.parametrize(
    ("physical_failures", "expected_attempts", "expected_outcome"),
    (
        (0, 1, PlcAttemptOutcome.BOUNDARY_RETURNED),
        (1, 2, PlcAttemptOutcome.BOUNDARY_RETURNED),
        (2, 2, PlcAttemptOutcome.BOUNDARY_FAILED),
    ),
)
def test_real_qt_plc_dispatch_acknowledges_physical_result_write(
    qapp,
    physical_failures,
    expected_attempts,
    expected_outcome,
) -> None:
    from app.service.multi_run_integration.plc_dispatch import PlcDispatchPorts
    from app.service.plc_service import PlcService
    from app.services_container import EventBus

    offsets = {
        "res_error_code_offset": "0",
        "res_sn_code_offset": "10",
        "res_result_offset": "20.0",
        "res_score_offset": "24",
        "res_spl_offset": "28",
        "res_loudness_offset": "32",
        "res_vibrate_data_offset": "36",
        "res_ai_score_offset": "40",
        "res_finish_signal_offset": "44.0",
        "res_testing_signal_offset": "45.0",
    }
    service = PlcService(
        EventBus(),
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict=offsets,
        plc_id=1,
    )
    service.plc_client = object()
    physical_attempt_threads = []
    payload_ids = []
    dispatched_payloads = []

    def write_value(offset, _value_type, _value):
        if offset == "0":
            physical_attempt_threads.append(QThread.currentThread())
            if len(physical_attempt_threads) <= physical_failures:
                raise RuntimeError("physical PLC write failed")
        return True

    service.write_plc_value = write_value
    original_write_result = service.write_result

    def observed_write_result(plc_id, payload):
        payload_ids.append(id(payload))
        dispatched_payloads.append(payload)
        return original_write_result(plc_id, payload)

    service.write_result = observed_write_result
    thread = QThread()
    service.moveToThread(thread)
    thread.start()
    try:
        ports = PlcDispatchPorts(
            plc_services={1: service},
            plc_threads={1: thread},
            dispatch_timeout_seconds=1.0,
        )
        reports = []
        sequencer = PlcResultSequencer(
            write_result=ports.write_result,
            report_attempt=reports.append,
        )
        registration = sequencer.register(
            run_id=captured_request().run_id,
            trigger_source="plc",
            operation_kind=PlcOperationKind.INITIAL,
            plc_id=1,
        )
        ticket = registration.ticket
        assert ticket is not None
        assert sequencer.mark_accepted(ticket) is PlcMutationDisposition.APPLIED
        payload = {
            "finished": True,
            "error_code": 0,
            "sn_code": "SN-1",
            "test_result": True,
            "score": 1.0,
            "spl": 2.0,
            "loudness": 3.0,
            "vibrate": 4.0,
            "ai_score": 5.0,
        }

        assert (
            sequencer.publish_terminal(ticket, payload)
            is PlcMutationDisposition.APPLIED
        )

        assert len(physical_attempt_threads) == expected_attempts
        assert all(item is thread for item in physical_attempt_threads)
        assert len(set(payload_ids)) == 1
        with pytest.raises(TypeError):
            dispatched_payloads[0]["score"] = 99.0
        assert len(reports) == 1
        assert reports[0].outcome is expected_outcome
    finally:
        thread.quit()
        assert thread.wait(2_000)


def test_plc_dispatch_on_target_thread_does_not_wait_for_itself(qapp) -> None:
    from app.service.multi_run_integration.plc_dispatch import PlcDispatchPorts
    from app.service.plc_service import PlcService
    from app.services_container import EventBus

    service = PlcService(
        EventBus(),
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict={"res_error_code_offset": "0"},
        plc_id=1,
    )
    service.plc_client = object()
    writes = []
    service.write_plc_value = lambda *args: writes.append(args)
    thread = QThread()
    service.moveToThread(thread)
    thread.start()
    ports = PlcDispatchPorts(
        plc_services={1: service},
        plc_threads={1: thread},
        dispatch_timeout_seconds=0.2,
    )

    class CallFromTargetThread:
        def __init__(self):
            self.completed = Event()
            self.error = None
            self.executing_thread = None

        def execute(self, _service):
            self.executing_thread = QThread.currentThread()
            try:
                ports.write_result(1, {"finished": False, "error_code": 0})
            except Exception as exc:  # pragma: no cover - asserted below
                self.error = exc
            finally:
                self.completed.set()

    command = CallFromTargetThread()
    try:
        service.plc_dispatch_requested.emit(command)
        assert command.completed.wait(1.0)
        assert command.executing_thread is thread
        assert command.error is None
        assert writes == [("0", "string", 0)]
    finally:
        thread.quit()
        assert thread.wait(2_000)


def test_unselected_matching_enrichment_is_retained_until_selection(qapp) -> None:
    first_run = captured_request().run_id
    second_run = "00000000-0000-4000-8000-000000000002"
    first_attempt = ATTEMPT_IDS[0]
    second_attempt = "10000000-0000-4000-8000-000000000002"
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    for run_id, attempt_id, revision in (
        (first_run, first_attempt, 4),
        (second_run, second_attempt, 2),
    ):
        adapter.publish(
            _decision_history_event(
                run_id=run_id,
                attempt_id=attempt_id,
                decision_json=decision_json,
                terminal_revision=terminal_revision,
                revision=revision,
            )
        )
        _drain(qapp)
    full_json = _production_shaped_result(
        charts={
            "0": {
                "fft": {
                    "frequencies_hz": [100.0],
                    "levels_db": [-3.0],
                }
            }
        },
        complete=True,
    )
    painted = []
    dashboard.decisionPainted.connect(lambda *args: painted.append(args))

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=first_run,
            attempt_id=first_attempt,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(first_attempt, full_json),
        )
    )
    _drain(qapp)

    assert dashboard.selected_run_id() == second_run
    assert painted == []
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is False
    _assert_complete_card(_history_card(dashboard, first_run))
    dashboard.set_follow_latest(False)
    dashboard.select_run(first_run)
    _assert_complete_card(_history_card(dashboard, first_run))
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True


def test_manual_fault_then_new_recording_keeps_late_charts_on_one_reselection(
    qapp,
) -> None:
    old_run = captured_request().run_id
    new_run = "00000000-0000-4000-8000-000000000002"
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    full_json = _production_shaped_result(
        charts={
            "0": {
                "fft": {
                    "frequencies_hz": [100.0],
                    "levels_db": [-3.0],
                }
            }
        },
        complete=True,
    )
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    painted = []
    dashboard.decisionPainted.connect(lambda *args: painted.append(args))
    dashboard.show()

    adapter.publish(
        _decision_history_event(
            run_id=old_run,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
            fault_types=("异响", "漏装"),
        )
    )
    _drain(qapp)
    assert len(painted) == 1
    adapter.publish(
        _manual_history_event(
            run_id=old_run,
            attempt_id=attempt_id,
            decision_json=decision_json,
            expected_revision=4,
            fault_type="异响",
        )
    )
    adapter.publish(_active_recording_event(run_id=new_run))
    _drain(qapp)
    assert dashboard.selected_run_id() == new_run

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=old_run,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(attempt_id, full_json),
        )
    )
    _drain(qapp)
    _assert_complete_card(_history_card(dashboard, old_run))
    dashboard.select_run(old_run)

    card = dashboard.history_snapshot().terminal_cards[0]
    assert card.run_id == old_run
    assert card.conclusion.name == "NG"
    assert card.fault_type == "异响"
    assert card.manual_judgment_revision == card.revision == 5
    _assert_complete_card(_history_card(dashboard, old_run))
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    assert dashboard._factory_display.review_payload.summary_payload[
        "overall_status"
    ] == "fail"
    assert len(painted) == 1
    assert dashboard._applied_enrichment_keys == {
        (old_run, attempt_id, terminal_revision)
    }
    dashboard.close()
    _drain(qapp)


def test_manual_before_enrichment_updates_selected_charts_without_summary_loss(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
            fault_types=("异响", "漏装"),
        )
    )
    adapter.publish(
        _manual_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            expected_revision=4,
            fault_type="漏装",
        )
    )
    _drain(qapp)

    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=5,
            result_payload=_bridge_payload(
                attempt_id,
                _production_shaped_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    },
                    complete=True,
                ),
            ),
        )
    )
    _drain(qapp)

    card = dashboard.history_snapshot().terminal_cards[0]
    assert card.fault_type == "漏装"
    assert card.manual_judgment_revision == 5
    _assert_complete_card(_history_card(dashboard, run_id))
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    assert dashboard._factory_display.review_payload.summary_payload[
        "overall_label"
    ] == "不通过（漏装）"
    dashboard.close()
    _drain(qapp)


def test_enrichment_before_manual_keeps_charts_and_manual_result_authoritative(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
            fault_types=("异响", "漏装"),
        )
    )
    _drain(qapp)
    enrichment_key = (run_id, attempt_id, terminal_revision)
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=attempt_id,
            terminal_revision=terminal_revision,
            expected_revision=4,
            result_payload=_bridge_payload(
                attempt_id,
                _production_shaped_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    },
                    complete=True,
                ),
            ),
        )
    )
    _drain(qapp)
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    _assert_complete_card(_history_card(dashboard, run_id))
    assert dashboard._applied_enrichment_keys == {enrichment_key}
    assert enrichment_key in dashboard._enriched_payload_by_binding

    adapter.publish(
        _manual_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            expected_revision=4,
            fault_type="异响",
        )
    )
    _drain(qapp)

    card = dashboard.history_snapshot().terminal_cards[0]
    assert card.conclusion.name == "NG"
    assert card.fault_type == "异响"
    assert card.manual_judgment_revision == card.revision == 5
    _assert_complete_card(_history_card(dashboard, run_id))
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    assert dashboard._applied_enrichment_keys == {enrichment_key}
    assert enrichment_key in dashboard._enriched_payload_by_binding
    assert dashboard._factory_display.review_payload.summary_payload[
        "overall_status"
    ] == "fail"
    assert dashboard._factory_display.review_payload.summary_payload[
        "overall_label"
    ] == "不通过（异响）"
    dashboard.refresh_history_projection()
    dashboard.set_follow_latest(False)
    dashboard.select_run(run_id)
    _assert_complete_card(_history_card(dashboard, run_id))

    before_rebuild = _history_card(dashboard, run_id)
    dashboard._history_module.render_snapshot(
        ResultHistorySnapshot(
            revision=dashboard.history_snapshot().revision,
            follow_latest=False,
            selected_run_id=None,
            active_cards=(),
            terminal_cards=(),
        )
    )
    dashboard.refresh_history_projection()
    after_rebuild = _history_card(dashboard, run_id)
    assert after_rebuild is not before_rebuild
    _assert_complete_card(after_rebuild)
    dashboard.close()
    _drain(qapp)


def test_repeated_manual_changes_before_enrichment_keep_latest_fault_and_apply_once(
    qapp,
) -> None:
    run_id = captured_request().run_id
    attempt_id = ATTEMPT_IDS[0]
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
            fault_types=("异响", "漏装"),
        )
    )
    _drain(qapp)
    adapter.publish(
        _manual_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            expected_revision=4,
            fault_type="异响",
        )
    )
    adapter.publish(
        _manual_history_event(
            run_id=run_id,
            attempt_id=attempt_id,
            decision_json=decision_json,
            expected_revision=5,
            fault_type="漏装",
        )
    )
    _drain(qapp)
    enrichment = ResultEnrichmentProjection(
        run_id=run_id,
        attempt_id=attempt_id,
        terminal_revision=terminal_revision,
        expected_revision=6,
        result_payload=_bridge_payload(
            attempt_id,
            _production_shaped_result(
                charts={
                    "0": {
                        "fft": {
                            "frequencies_hz": [100.0],
                            "levels_db": [-3.0],
                        }
                    }
                },
                complete=True,
            ),
        ),
    )
    refresh_calls = []
    refresh_terminal_payload = dashboard._history_module.refresh_terminal_payload

    def count_refresh(*args):
        refresh_calls.append(args[0])
        return refresh_terminal_payload(*args)

    dashboard._history_module.refresh_terminal_payload = count_refresh
    adapter.publish_enrichment(enrichment)
    adapter.publish_enrichment(enrichment)
    _drain(qapp)

    card = dashboard.history_snapshot().terminal_cards[0]
    assert card.fault_type == "漏装"
    assert card.manual_judgment_revision == card.revision == 6
    _assert_complete_card(_history_card(dashboard, run_id))
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    assert dashboard._factory_display.review_payload.summary_payload[
        "overall_label"
    ] == "不通过（漏装）"
    assert len(dashboard._applied_enrichment_keys) == 1
    assert refresh_calls == [run_id]
    dashboard.close()
    _drain(qapp)


def test_cancelled_reanalysis_does_not_revive_old_enrichment_but_new_attempt_does(
    qapp,
) -> None:
    run_id = captured_request().run_id
    old_attempt = ATTEMPT_IDS[0]
    operation_id = "10000000-0000-4000-8000-000000000002"
    new_attempt = operation_id
    decision_json = _production_shaped_result(charts={}, complete=False)
    old_terminal = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    adapter.publish(
        _decision_history_event(
            run_id=run_id,
            attempt_id=old_attempt,
            decision_json=decision_json,
            terminal_revision=old_terminal,
            fault_types=("异响", "漏装"),
        )
    )
    adapter.publish(
        _manual_history_event(
            run_id=run_id,
            attempt_id=old_attempt,
            decision_json=decision_json,
            expected_revision=4,
            fault_type="异响",
        )
    )
    _drain(qapp)
    old_enrichment = ResultEnrichmentProjection(
        run_id=run_id,
        attempt_id=old_attempt,
        terminal_revision=old_terminal,
        expected_revision=5,
        result_payload=_bridge_payload(
            old_attempt,
            _production_shaped_result(
                charts={
                    "0": {
                        "fft": {
                            "frequencies_hz": [1],
                            "levels_db": [-3.0],
                        }
                    }
                },
                complete=True,
            ),
        ),
    )
    adapter.publish_enrichment(old_enrichment)
    _drain(qapp)
    _assert_complete_card(_history_card(dashboard, run_id))
    assert dashboard._applied_enrichment_keys == {
        (run_id, old_attempt, old_terminal)
    }

    adapter.publish(
        _reanalysis_event(
            kind=HistoryFactKind.REANALYSIS_STARTED,
            run_id=run_id,
            operation_id=operation_id,
            expected_revision=5,
            result_payload=_bridge_payload(old_attempt, decision_json),
        )
    )
    adapter.publish(
        _reanalysis_event(
            kind=HistoryFactKind.REANALYSIS_CANCELLED,
            run_id=run_id,
            operation_id=operation_id,
            expected_revision=6,
            result_payload=_bridge_payload(old_attempt, decision_json),
        )
    )
    _drain(qapp)
    assert _card_table_values(_history_card(dashboard, run_id))[:5] == (
        "--",
        "--",
        "--",
        "--",
        "--",
    )
    assert dashboard._applied_enrichment_keys == set()
    adapter.publish_enrichment(old_enrichment)
    _drain(qapp)
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is False
    assert dashboard._applied_enrichment_keys == set()

    adapter.publish(
        _reanalysis_event(
            kind=HistoryFactKind.REANALYSIS_STARTED,
            run_id=run_id,
            operation_id=operation_id,
            expected_revision=7,
            result_payload=_bridge_payload(old_attempt, decision_json),
        )
    )
    new_terminal = "a" * 64
    adapter.publish(
        _reanalysis_event(
            kind=HistoryFactKind.REANALYSIS_COMPLETED,
            run_id=run_id,
            operation_id=operation_id,
            expected_revision=8,
            conclusion=TerminalConclusion.OK,
            result_payload=_bridge_payload(new_attempt, decision_json),
            terminal_revision=new_terminal,
        )
    )
    _drain(qapp)
    adapter.publish_enrichment(old_enrichment)
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=run_id,
            attempt_id=new_attempt,
            terminal_revision=new_terminal,
            expected_revision=9,
            result_payload=_bridge_payload(
                new_attempt,
                _production_shaped_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [2],
                                "levels_db": [-3.0],
                            }
                        }
                    },
                    complete=True,
                ),
            ),
        )
    )
    _drain(qapp)

    assert dashboard._applied_enrichment_keys == {
        (run_id, new_attempt, new_terminal)
    }
    _assert_complete_card(_history_card(dashboard, run_id))
    assert dashboard.panel_widget("spectrum").has_committed_plot_data() is True
    dashboard.close()
    _drain(qapp)


def test_eviction_and_close_purge_all_staged_projection_payloads(qapp) -> None:
    dashboard = ResultDashboardModule(_services())
    adapter = HistoryQtAdapter(dashboard)
    dashboard.show()
    decision_json = _production_shaped_result(charts={}, complete=False)
    terminal_revision = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
    first_run = captured_request().run_id
    first_attempt = ATTEMPT_IDS[0]
    adapter.publish(
        _decision_history_event(
            run_id=first_run,
            attempt_id=first_attempt,
            decision_json=decision_json,
            terminal_revision=terminal_revision,
            revision=2,
        )
    )
    _drain(qapp)
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=first_run,
            attempt_id=first_attempt,
            terminal_revision=terminal_revision,
            expected_revision=2,
            result_payload=_bridge_payload(
                first_attempt,
                _production_shaped_result(
                    charts={
                        "0": {
                            "fft": {
                                "frequencies_hz": [100.0],
                                "levels_db": [-3.0],
                            }
                        }
                    },
                    complete=True,
                ),
            ),
        )
    )
    _drain(qapp)
    _assert_complete_card(_history_card(dashboard, first_run))
    for index in range(2, 5):
        active_run = f"00000000-0000-4000-8000-{index:012d}"
        adapter.publish(_active_recording_event(run_id=active_run))
        _drain(qapp)
        assert dashboard.selected_run_id() == active_run
        dashboard.set_follow_latest(False)
        dashboard.select_run(first_run)
        assert dashboard.selected_run_id() == first_run
        dashboard.set_follow_latest(True)
        assert dashboard.selected_run_id() == active_run
        dashboard.apply_history_event(ActiveRunRemoved(active_run))
        assert dashboard.selected_run_id() == first_run
    for index in range(2, ResultHistoryStore.MAX_TERMINAL_RESULTS + 2):
        latest_run = f"00000000-0000-4000-8000-{index:012d}"
        latest_attempt = f"10000000-0000-4000-8000-{index:012d}"
        adapter.publish(
            _decision_history_event(
                run_id=latest_run,
                attempt_id=latest_attempt,
                decision_json=decision_json,
                terminal_revision=terminal_revision,
                revision=2,
            )
        )
        _drain(qapp)

    assert first_run not in dashboard._decision_binding_by_run
    assert first_run not in dashboard._decision_origin_revision_by_run
    assert all(key[0] != first_run for key in dashboard._applied_enrichment_keys)
    assert all(
        key[0] != first_run for key in dashboard._enriched_payload_by_binding
    )
    assert (
        dashboard._current_decision_key is None
        or dashboard._current_decision_key[0] != first_run
    )
    assert first_run not in dashboard._last_acknowledged_decision_by_run
    assert first_run not in dashboard._history_module._card_entries
    dashboard.close()
    _drain(qapp)
    adapter.publish_enrichment(
        ResultEnrichmentProjection(
            run_id=latest_run,
            attempt_id=latest_attempt,
            terminal_revision=terminal_revision,
            expected_revision=2,
            result_payload=_bridge_payload(
                latest_attempt,
                _production_shaped_result(charts={}, complete=True),
            ),
        )
    )
    _drain(qapp)
    assert dashboard._decision_binding_by_run == {}
    assert dashboard._decision_origin_revision_by_run == {}
    assert dashboard._applied_enrichment_keys == set()
    assert dashboard._enriched_payload_by_binding == {}
    assert dashboard._current_decision_key is None
    assert dashboard._last_acknowledged_decision_by_run == {}
