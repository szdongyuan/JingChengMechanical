from __future__ import annotations

import gc
import os
import weakref
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.service.factory_review_payloads import FactoryReviewPayload
from app.service.result_history import (
    ActiveRunChanged,
    ActiveRunRemoved,
    ConfigSummaryField,
    ErrorDisplayPayload,
    FrozenConfigSummary,
    ReanalysisCancelled,
    ReanalysisCompleted,
    ReanalysisStarted,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultHistoryError,
    ResultHistorySnapshot,
    ResultHistoryStore,
    ResultHistoryTransition,
    ResultPhase,
    ReviewDisplayPayload,
    RunTerminated,
    TerminalResultReplaced,
)
from app.service.result_history_contracts import (
    HistoryProjectionDiagnostic,
    HistoryProjectionDiagnosticCode,
    ManualJudgmentRequest,
    ManualJudgmentTarget,
    ReanalysisCancelRequest,
    ReanalysisRequest,
    ResultOperationAccepted,
    ResultOperationRejected,
    ResultOperationRejectionCode,
)
from app.services_container import FactoryControllerState
from ui.modules.result_dashboard import ResultDashboardModule
from ui.modules.result_history import ResultHistoryModule
from ui.widgets.result_history_card import ResultHistoryCard


class SignalStub:
    def __init__(self):
        self.callbacks = []
        self.emissions = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        self.emissions.append(args)
        for callback in tuple(self.callbacks):
            callback(*args)


class GlobalBusStub:
    def __init__(self):
        for name in (
            "config_changed", "factory_state_changed", "factory_review_changed",
            "factory_display_reset", "factory_runtime_start_accepted",
            "factory_runtime_analyzing_entered", "stft_updated", "fft_updated",
            "spl_updated", "loudness_updated", "octave_updated",
            "current_trace_updated",
        ):
            setattr(self, name, SignalStub())


def _flush_deferred_deletes_safely(qapp):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    gc.collect()


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def config(*, panel_type="spectrum"):
    return {
        "config_id": 7,
        "selected_channels": [0],
        "result_dashboard_layout": {
            "version": 2,
            "rows": 1,
            "columns": 1,
            "panels": [
                {
                "id": "chart", "type": panel_type, "row": 0, "column": 0,
                "row_span": 1, "column_span": 1, "channel": "selected_1",
                },
            ],
        },
    }


def services():
    return SimpleNamespace(
        bus=GlobalBusStub(),
        config_service=SimpleNamespace(
            load_config=MagicMock(side_effect=AssertionError("must use frozen config")),
            load_config_for_result_dashboard=MagicMock(side_effect=AssertionError("must use frozen config")),
        ),
        factory_selection_settings=SimpleNamespace(
            load_selected_config_ids=MagicMock(return_value=())
        ),
        factory_mode=SimpleNamespace(
            state=FactoryControllerState(
                displayed_config_id=999, run_phase="reviewing"
            ),
            review_payload=FactoryReviewPayload(
                displayed_config_id=999, run_id="legacy"
            ),
            begin_reanalysis=MagicMock(),
        ),
    )


def card(
    run_id: str, *, active=False, revision=0, reanalyzing=False,
    operation_id=None, fault_types=(), fault_type=None,
    manual_judgment_buttons_visible=True,
):
    return ResultHistoryCardSnapshot(
        run_id=run_id,
        config=FrozenConfigSummary(
            config_id=7,
            name="Frozen config",
            details=(ConfigSummaryField("Channel", "0"),),
            fault_types=tuple(fault_types),
            manual_judgment_buttons_visible=(
                manual_judgment_buttons_visible
            ),
        ),
        sn="SN",
        trigger_source="test",
        started_at=datetime(2026, 7, 26, 1, 0),
        terminated_at=None if active else datetime(2026, 7, 26, 1, 1),
        phase=(
            ResultPhase.RECORDING if active else
            ResultPhase.REANALYZING if reanalyzing else ResultPhase.TERMINAL
        ),
        conclusion=None if active else ResultConclusion.OK,
        reason=None,
        is_active_run=active,
        manual_judgment_allowed=not active and not reanalyzing,
        manual_reanalysis_allowed=not active and not reanalyzing,
        revision=revision,
        operation_id=operation_id,
        fault_type=fault_type,
    )


def review(run_id: str, level=1.0):
    return ReviewDisplayPayload(
        FactoryReviewPayload(
            displayed_config_id=7,
            run_id=run_id,
            chart_payload={0: {"fft": {
                "frequencies_hz": [100.0, 200.0],
                "levels_db": [level, level + 1.0],
            }}},
        ),
        config(),
    )


def terminal(run_id: str, *, level=1.0):
    return RunTerminated(card(run_id), review_payload=review(run_id, level))


def test_manual_target_uses_only_frozen_history_labels(qapp):
    module = ResultDashboardModule(services())
    targets = []
    module.manual_judgment_target_changed.connect(targets.append)
    module.apply_history_event(
        RunTerminated(
            card("run-1", revision=4, fault_types=("异响", "漏装")),
            review_payload=review("run-1"),
        )
    )

    assert targets[-1] == ManualJudgmentTarget(
        run_id="run-1",
        revision=4,
        fault_types=("异响", "漏装"),
        manual_judgment_allowed=True,
    )
    module.services.config_service.load_config.assert_not_called()
    module.services.config_service.load_config_for_result_dashboard.assert_not_called()

    emitted = []
    module.manual_judgment_requested.connect(emitted.append)
    accepted = module.request_manual_judgment("NG", "漏装")
    assert accepted.request.fault_type == "漏装"
    assert emitted == [accepted.request]


def test_manual_judgment_buttons_false_publishes_no_homepage_target(qapp):
    module = ResultDashboardModule(services())
    targets = []
    module.manual_judgment_target_changed.connect(targets.append)
    module.apply_history_event(
        RunTerminated(
            card("visible", revision=1),
            review_payload=review("visible"),
        )
    )
    module.apply_history_event(
        RunTerminated(
            card(
                "hidden",
                revision=1,
                manual_judgment_buttons_visible=False,
            ),
            review_payload=review("hidden"),
        )
    )

    assert targets[-1] is None
    assert module._manual_judgment_target is None


def test_manual_judgment_buttons_visibility_is_frozen_per_run(qapp):
    service_bundle = services()
    current_config = {"manual_judgment_buttons_visible": False}
    service_bundle.config_service.load_config.side_effect = None
    service_bundle.config_service.load_config.return_value = dict(current_config)
    service_bundle.config_service.load_config_for_result_dashboard.side_effect = None
    service_bundle.config_service.load_config_for_result_dashboard.return_value = (
        dict(current_config)
    )
    module = ResultDashboardModule(service_bundle)
    targets = []
    module.manual_judgment_target_changed.connect(targets.append)
    module.apply_history_event(
        RunTerminated(
            card(
                "old-hidden",
                revision=1,
                manual_judgment_buttons_visible=False,
            ),
            review_payload=review("old-hidden"),
        )
    )

    current_config["manual_judgment_buttons_visible"] = True
    service_bundle.config_service.load_config.return_value = dict(current_config)
    service_bundle.config_service.load_config_for_result_dashboard.return_value = (
        dict(current_config)
    )
    module.set_follow_latest(False)
    module.select_run("old-hidden")

    assert module._manual_judgment_target is None

    module.apply_history_event(
        RunTerminated(
            card(
                "new-visible",
                revision=1,
                manual_judgment_buttons_visible=True,
            ),
            review_payload=review("new-visible"),
        )
    )
    module.select_run("new-visible")

    assert targets[-1] == ManualJudgmentTarget(
        "new-visible",
        1,
        (),
        True,
    )
    service_bundle.config_service.load_config.assert_not_called()
    service_bundle.config_service.load_config_for_result_dashboard.assert_not_called()


def test_dashboard_manual_actions_keep_viewport_host_and_overlay_focused_slot(qapp):
    actions = QWidget()
    actions_layout = QVBoxLayout(actions)
    action_button = QPushButton("manual OK", actions)
    actions_layout.addWidget(action_button)
    module = ResultDashboardModule(
        services(),
        manual_judgment_widget=actions,
    )
    requested = []
    module.manual_judgment_requested.connect(requested.append)
    action_button.clicked.connect(
        lambda: module.request_manual_judgment("OK")
    )
    for index in range(6):
        module.apply_history_event(terminal(f"run-{index}"))
    module.resize(900, 260)
    module.show()
    QTest.qWait(20)
    qapp.processEvents()

    history = module._history_module
    scroll = history.findChild(QScrollArea, "result_history_scroll")
    assert scroll is not None
    scrollbar = scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    fixed_parent = actions.parentWidget()

    module.set_follow_latest(False)
    module.select_run("run-1")
    scrollbar.setValue(0)
    qapp.processEvents()

    first_selected = next(
        card_widget
        for card_widget in module.findChildren(ResultHistoryCard)
        if card_widget.property("run_id") == "run-1"
    )
    scroll.ensureWidgetVisible(first_selected, 0, 0)
    qapp.processEvents()
    first_slot = first_selected.manual_judgment_slot
    first_slot_pos = first_slot.mapTo(scroll.viewport(), QPoint(0, 0))
    assert fixed_parent is scroll.viewport()
    assert actions.parentWidget() is fixed_parent
    assert first_selected.manual_judgment_widget is actions
    assert actions.pos() == first_slot_pos
    assert actions.size() == first_slot.size()
    assert first_slot_pos.y() < scroll.viewport().height()
    assert first_slot_pos.y() + first_slot.height() > 0

    action_button.click()
    assert requested[-1].run_id == "run-1"

    module.select_run("run-4")
    scrollbar.setValue(scrollbar.maximum())
    qapp.processEvents()

    second_selected = next(
        card_widget
        for card_widget in module.findChildren(ResultHistoryCard)
        if card_widget.property("run_id") == "run-4"
    )
    scroll.ensureWidgetVisible(second_selected, 0, 0)
    qapp.processEvents()
    second_slot = second_selected.manual_judgment_slot
    second_slot_pos = second_slot.mapTo(scroll.viewport(), QPoint(0, 0))
    assert actions.parentWidget() is fixed_parent
    assert second_selected.manual_judgment_widget is actions
    assert actions.pos() == second_slot_pos
    assert actions.size() == second_slot.size()
    assert second_slot_pos.y() < scroll.viewport().height()
    assert second_slot_pos.y() + second_slot.height() > 0

    action_button.click()
    assert requested[-1].run_id == "run-4"


def test_manual_request_rereads_authoritative_store_exactly_once(qapp):
    class CountingStore(ResultHistoryStore):
        def __init__(self):
            super().__init__()
            self.snapshot_calls = 0

        def snapshot(self):
            self.snapshot_calls += 1
            return super().snapshot()

    store = CountingStore()
    module = ResultDashboardModule(services(), store)
    module.apply_history_event(
        RunTerminated(
            card("run-1", revision=7, fault_types=("异响",)),
            review_payload=review("run-1"),
        )
    )
    store.snapshot_calls = 0

    result = module.request_manual_judgment("NG", "异响")

    assert type(result) is ResultOperationAccepted
    assert result.request == ManualJudgmentRequest(
        "run-1", "NG", 7, "异响"
    )
    assert store.snapshot_calls == 1


def test_auto_follow_allows_old_manual_target_then_returns_on_new_result(qapp):
    module = ResultDashboardModule(services())
    targets = []
    module.manual_judgment_target_changed.connect(targets.append)
    module.apply_history_event(
        RunTerminated(
            card("run-1", revision=1, fault_types=("异响",)),
            review_payload=review("run-1"),
        )
    )
    module.apply_history_event(
        RunTerminated(
            card("run-2", revision=1, fault_types=("漏装",)),
            review_payload=review("run-2"),
        )
    )

    selected = module.select_run("run-1")

    assert selected.snapshot.follow_latest is True
    assert selected.snapshot.selected_run_id == "run-1"
    assert targets[-1] == ManualJudgmentTarget(
        "run-1",
        1,
        ("异响",),
        True,
    )

    module.apply_history_event(
        TerminalResultReplaced(
            "run-1",
            1,
            replace(
                card("run-1", revision=2, fault_types=("异响",)),
                conclusion=ResultConclusion.NG,
                fault_type="异响",
            ),
            review_payload=review("run-1", 1.5),
        )
    )

    assert module.history_snapshot().selected_run_id == "run-1"
    assert targets[-1] == ManualJudgmentTarget(
        "run-1",
        2,
        ("异响",),
        True,
    )

    module.apply_history_event(
        RunTerminated(
            card("run-3", revision=1, fault_types=("松动",)),
            review_payload=review("run-3"),
        )
    )

    assert module.history_snapshot().follow_latest is True
    assert module.history_snapshot().selected_run_id == "run-3"
    assert targets[-1] == ManualJudgmentTarget(
        "run-3",
        1,
        ("松动",),
        True,
    )


def test_manual_target_clears_on_selection_revision_tuple_capability_or_eviction(qapp):
    module = ResultDashboardModule(services())
    targets = []
    module.manual_judgment_target_changed.connect(targets.append)
    module.apply_history_event(
        RunTerminated(
            card("run-1", revision=1, fault_types=("异响",)),
            review_payload=review("run-1"),
        )
    )
    assert targets[-1].run_id == "run-1"

    module.apply_history_event(
        RunTerminated(
            card("run-2", revision=1, fault_types=("漏装",)),
            review_payload=review("run-2"),
        )
    )
    assert targets[-1].run_id == "run-2"

    module.apply_history_event(ReanalysisStarted("run-2", "operation-2", 1))
    assert targets[-1] == ManualJudgmentTarget("run-2", 2, ("漏装",), False)

    module.apply_history_event(ReanalysisCancelled("run-2", "operation-2", 2))
    assert targets[-1] == ManualJudgmentTarget("run-2", 3, ("漏装",), True)

    module.apply_history_event(
        TerminalResultReplaced(
            "run-2",
            3,
            card("run-2", revision=4, fault_types=("电流异常",)),
            review_payload=review("run-2"),
        )
    )
    assert targets[-1] == ManualJudgmentTarget(
        "run-2", 4, ("电流异常",), True
    )

    module.set_follow_latest(False)
    module.select_run("run-1")
    for index in range(3, 9):
        module.apply_history_event(
            RunTerminated(
                card(f"run-{index}", revision=1),
                review_payload=review(f"run-{index}"),
            )
        )
    assert all(
        item.run_id != "run-1"
        for item in module.history_snapshot().terminal_cards
    )
    assert targets[-1].run_id != "run-1"
    module.close()
    assert targets[-1] is None


def test_dashboard_result_subregion_orders_history_before_elastic_display(qapp):
    module = ResultDashboardModule(services())
    layout = module.layout()
    assert type(layout) is QHBoxLayout
    assert isinstance(layout.itemAt(0).widget(), ResultHistoryModule)
    assert layout.itemAt(0).widget().width() == 360
    assert isinstance(layout.itemAt(1).widget(), QStackedWidget)
    assert layout.stretch(1) == 1


def test_dashboard_starts_locked_with_no_results_and_waiting_display(qapp):
    module = ResultDashboardModule(services())
    assert module.history_snapshot().follow_latest is True
    assert module.history_snapshot().selected_run_id is None
    assert module.findChild(QLabel, "result_dashboard_waiting_label").text() == "等待测试结果"
    assert module.display_stack().currentWidget().objectName() == "result_dashboard_waiting_page"


def test_prepared_config_keeps_empty_graph_visible_and_forwards_active_live_stft(
    qapp,
):
    svc = services()
    svc.factory_mode.state = FactoryControllerState(
        selected_config_ids=(7,),
        displayed_config_id=7,
        run_phase="idle",
    )
    svc.factory_selection_settings.load_selected_config_ids.return_value = (7,)
    svc.config_service.load_config_for_result_dashboard.side_effect = None
    svc.config_service.load_config_for_result_dashboard.return_value = config(
        panel_type="stft"
    )
    module = ResultDashboardModule(svc)

    assert (
        module.display_stack().currentWidget().objectName()
        == "result_dashboard_review_page"
    )
    assert module.panel_widget("chart") is not None

    forwarded = []
    module.projection_bus().stft_updated.connect(
        lambda *args: forwarded.append(args)
    )
    module.apply_history_event(ActiveRunChanged(card("active", active=True)))
    frame = object()
    svc.bus.stft_updated.emit(7, "active", 0, frame)

    assert forwarded == [(7, "active", 0, frame)]


def test_locked_dashboard_switches_from_terminal_to_next_active_live_stft(qapp):
    svc = services()
    svc.config_service.load_config_for_result_dashboard.side_effect = None
    svc.config_service.load_config_for_result_dashboard.return_value = config(
        panel_type="stft"
    )
    module = ResultDashboardModule(svc)
    module.apply_history_event(terminal("completed"))

    forwarded = []
    module.projection_bus().stft_updated.connect(
        lambda *args: forwarded.append(args)
    )
    module.apply_history_event(ActiveRunChanged(card("next", active=True)))
    frame = object()
    svc.bus.stft_updated.emit(7, "next", 0, frame)

    assert module.history_snapshot().selected_run_id == "next"
    assert module.projected_run_id() == "next"
    assert forwarded == [(7, "next", 0, frame)]


def test_live_spl_routes_to_all_history_cards_before_projection_filter(qapp):
    svc = services()
    live_config = config(panel_type="spl")
    live_config["channels"] = [
        {"channel_index": 0, "loudness_unit": "sone"}
    ]
    svc.config_service.load_config_for_result_dashboard.side_effect = None
    svc.config_service.load_config_for_result_dashboard.return_value = live_config
    module = ResultDashboardModule(svc)
    module.apply_history_event(ActiveRunChanged(card("run-a", active=True)))
    module.apply_history_event(ActiveRunChanged(card("run-b", active=True)))
    forwarded = []
    module.projection_bus().spl_updated.connect(
        lambda *args: forwarded.append(args)
    )

    frame_a = SimpleNamespace(spl_db=61.25)
    frame_b = SimpleNamespace(spl_db=72.5)
    svc.bus.spl_updated.emit(7, "run-a", 0, frame_a)
    svc.bus.spl_updated.emit(7, "run-b", 0, frame_b)

    cards = {
        item.property("run_id"): item
        for item in module.findChildren(ResultHistoryCard)
    }
    assert cards["run-a"].findChild(
        QLabel, "result_history_card_metric_spl"
    ).text() == "61.2 dB"
    assert cards["run-b"].findChild(
        QLabel, "result_history_card_metric_spl"
    ).text() == "72.5 dB"
    assert forwarded == [(7, "run-b", 0, frame_b)]


def test_apply_event_renders_active_and_terminal_cards_from_store(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(ActiveRunChanged(card("active", active=True)))
    module.apply_history_event(terminal("terminal"))
    snapshot = module.history_snapshot()
    assert [item.run_id for item in snapshot.active_cards] == ["active"]
    assert [item.run_id for item in snapshot.terminal_cards] == ["terminal"]


def test_apply_history_event_calls_store_once_and_projects_returned_transition(qapp):
    class CountingStore(ResultHistoryStore):
        def __init__(self):
            super().__init__()
            self.calls = 0
        def apply(self, event):
            self.calls += 1
            return super().apply(event)

    store = CountingStore()
    module = ResultDashboardModule(services(), store)
    transition = module.apply_history_event(terminal("run-1"))
    assert store.calls == 1
    assert transition.snapshot.revision == 1
    assert module.projected_snapshot() == transition.snapshot


def test_project_history_transition_never_reapplies_an_upstream_commit(qapp):
    store = ResultHistoryStore()
    transition = store.apply(terminal("run-1"))
    store.apply = MagicMock(side_effect=AssertionError("must not apply"))
    module = ResultDashboardModule(services(), store)
    module.project_history_transition(transition)
    store.apply.assert_not_called()


def test_run_terminated_projection_reads_identity_only_from_event_card(qapp):
    event = terminal("run-only-on-card")
    with pytest.raises(AttributeError):
        getattr(event, "run_id")
    module = ResultDashboardModule(services())
    module.apply_history_event(event)
    assert module.history_snapshot().selected_run_id == "run-only-on-card"


def test_raw_domain_phase_outcome_has_no_dashboard_default_mapping(qapp):
    with pytest.raises((TypeError, ValueError)):
        ResultDashboardModule(services()).apply_history_event(
            SimpleNamespace(phase="mystery", outcome="ok")
        )


def test_projection_rejects_stale_foreign_and_payload_race_transitions(qapp):
    store = ResultHistoryStore()
    first = store.apply(terminal("run-1"))
    second = store.apply(terminal("run-2"))
    module = ResultDashboardModule(services(), store)
    diagnostics = []
    module.history_diagnostic.connect(diagnostics.append)
    module.project_history_transition(first)
    assert diagnostics[-1].code is HistoryProjectionDiagnosticCode.STALE_TRANSITION
    foreign = ResultHistoryTransition(
        replace(
            second.snapshot,
            selected_run_id="run-1",
            terminal_cards=tuple(reversed(second.snapshot.terminal_cards)),
        ),
        changed=True,
    )
    module.project_history_transition(foreign)
    assert diagnostics[-1].code is HistoryProjectionDiagnosticCode.FOREIGN_TRANSITION

    original = store.display_payload
    def racing(run_id):
        value = original(run_id)
        store.set_follow_latest(False)
        return value
    store.display_payload = racing
    module.project_history_transition(second)
    assert diagnostics[-1].code is HistoryProjectionDiagnosticCode.CONCURRENT_STORE_CHANGE


def test_dashboard_history_entrypoints_require_own_qt_thread(qapp):
    module = ResultDashboardModule(services())
    module.thread = lambda: object()
    diagnostics = []
    module.history_diagnostic.connect(diagnostics.append)
    with pytest.raises(RuntimeError):
        module.history_snapshot()
    assert diagnostics[-1].code is HistoryProjectionDiagnosticCode.WRONG_QT_THREAD


def test_projection_diagnostic_values_are_exact_typed_and_bounded():
    assert type(HistoryProjectionDiagnostic(
        HistoryProjectionDiagnosticCode.STALE_TRANSITION,
        "run-1", 1, 2, "stale",
    )) is HistoryProjectionDiagnostic
    with pytest.raises(ValueError):
        HistoryProjectionDiagnostic("stale_transition", "run-1", 1, 2, "stale")
    with pytest.raises(ValueError):
        HistoryProjectionDiagnostic(
            HistoryProjectionDiagnosticCode.STALE_TRANSITION,
            "", 1, 2, "stale",
        )
    with pytest.raises(ValueError):
        HistoryProjectionDiagnostic(
            HistoryProjectionDiagnosticCode.STALE_TRANSITION,
            None, -1, 2, "stale",
        )


def test_locked_terminal_auto_projects_latest_review(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1", level=2))
    module.apply_history_event(terminal("run-2", level=9))
    assert module.projected_run_id() == "run-2"
    assert module.display_stack().currentWidget().objectName() == "result_dashboard_review_page"


def test_unlocked_card_click_projects_selected_review_without_new_result_steal(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    module.set_follow_latest(False)
    module.apply_history_event(terminal("run-2"))
    module.select_run("run-1")
    module.apply_history_event(terminal("run-3"))
    assert module.projected_run_id() == "run-1"


def test_relock_immediately_projects_latest(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    module.set_follow_latest(False)
    module.apply_history_event(terminal("run-2"))
    module.select_run("run-1")
    module.set_follow_latest(True)
    assert module.projected_run_id() == "run-2"


def test_error_payload_switches_to_error_page_and_clears_old_chart(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    module.apply_history_event(RunTerminated(
        replace(card("run-2"), conclusion=ResultConclusion.ERROR, reason="boom"),
        error_payload=ErrorDisplayPayload("analysis", "boom"),
    ))
    assert module.cards_by_panel_id() == {}
    assert module.display_stack().currentWidget().objectName() == "result_dashboard_error_page"
    assert "boom" in module.findChild(QLabel, "result_dashboard_error_reason").text()


def test_selected_eviction_projects_latest_and_shows_notice(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    module.set_follow_latest(False)
    module.select_run("run-1")
    for index in range(2, 8):
        transition = module.apply_history_event(terminal(f"run-{index}"))
    assert transition.evicted_run_id == "run-1"
    assert module.projected_run_id() == "run-7"
    assert module.findChild(QLabel, "result_history_notice").text() == "所查看结果已移出缓存"


def test_review_projection_uses_frozen_config_and_never_reloads_current_config(qapp):
    svc = services()
    module = ResultDashboardModule(svc)
    module.apply_history_event(terminal("run-1"))
    assert module.displayed_config()["config_id"] == 7
    svc.config_service.load_config.assert_not_called()
    svc.config_service.load_config_for_result_dashboard.assert_not_called()


def test_private_projection_bus_does_not_emit_global_factory_review(qapp):
    svc = services()
    ResultDashboardModule(svc).apply_history_event(terminal("run-1"))
    assert svc.bus.factory_review_changed.emissions == []


def test_synthetic_review_repaints_existing_chart_widget(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1", level=1))
    panel = module.panel_widget("chart")
    first = panel.current_plot_payload()
    module.apply_history_event(terminal("run-2", level=10))
    assert module.panel_widget("chart") is panel
    assert panel.current_plot_payload() != first


def test_preview_accepts_only_unlocked_selected_active_run(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(ActiveRunChanged(card("active", active=True)))
    assert module.project_preview("active", object()) is False
    module.set_follow_latest(False)
    module.select_run("active")
    seen = []
    module.preview_projected.connect(lambda run_id, payload: seen.append((run_id, payload)))
    payload = object()
    assert module.project_preview("other", payload) is False
    assert module.project_preview("active", payload) is True
    assert seen == [("active", payload)]


def test_selected_active_removal_repairs_selection_and_clears_preview(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(ActiveRunChanged(card("active", active=True)))
    module.set_follow_latest(False)
    module.select_run("active")
    module.apply_history_event(ActiveRunRemoved("active"))
    assert module.preview_target_run_id() is None
    assert module.history_snapshot().selected_run_id is None


def test_manual_judgment_signal_delivers_selected_run_label_and_revision(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    seen = []
    module.manual_judgment_requested.connect(seen.append)
    result = module.request_manual_judgment("NG")
    assert type(result) is ResultOperationAccepted
    assert seen == [ManualJudgmentRequest("run-1", "NG", 0)]


def test_reanalysis_signal_delivers_selected_run_revision_and_window_without_operation_id(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    seen = []
    module.reanalysis_requested.connect(seen.append)
    result = module.request_reanalysis(0, 1)
    assert type(result) is ResultOperationAccepted
    assert seen == [ReanalysisRequest("run-1", 0, 0.0, 1.0)]
    assert not hasattr(seen[0], "operation_id")


def test_reanalysis_cancel_signal_reuses_current_operation_id(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    module.apply_history_event(ReanalysisStarted("run-1", "coordinator-op", 0))
    seen = []
    module.reanalysis_cancel_requested.connect(seen.append)
    result = module.request_reanalysis_cancel()
    assert type(result) is ResultOperationAccepted
    assert seen == [ReanalysisCancelRequest("run-1", "coordinator-op", 1)]


def test_coordinator_minted_reanalysis_started_operation_is_only_recorded_from_event(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    module.request_reanalysis(0, 1)
    assert module.history_snapshot().terminal_cards[0].operation_id is None
    module.apply_history_event(ReanalysisStarted("run-1", "minted", 0))
    assert module.history_snapshot().terminal_cards[0].operation_id == "minted"


@pytest.mark.parametrize(
    ("method", "args", "code"),
    [
        ("request_manual_judgment", ("",), ResultOperationRejectionCode.INVALID_ARGUMENT),
        ("request_manual_judgment", ("OK",), ResultOperationRejectionCode.NO_SELECTION),
        ("request_reanalysis", (0, 1), ResultOperationRejectionCode.NO_SELECTION),
        ("request_reanalysis_cancel", (), ResultOperationRejectionCode.NO_SELECTION),
    ],
)
def test_rejected_operation_emits_no_adapter_signal_and_returns_visible_reason(qapp, method, args, code):
    module = ResultDashboardModule(services())
    seen = []
    module.manual_judgment_requested.connect(seen.append)
    module.reanalysis_requested.connect(seen.append)
    module.reanalysis_cancel_requested.connect(seen.append)
    result = getattr(module, method)(*args)
    assert type(result) is ResultOperationRejected
    assert result.code is code
    assert result.reason.strip()
    assert seen == []


def _capture_operation_request_signals(module):
    emitted = {
        "manual": [],
        "reanalysis": [],
        "cancel": [],
    }
    module.manual_judgment_requested.connect(emitted["manual"].append)
    module.reanalysis_requested.connect(emitted["reanalysis"].append)
    module.reanalysis_cancel_requested.connect(emitted["cancel"].append)
    return emitted


def _assert_exact_operation_rejection(result, code, emitted):
    assert type(result) is ResultOperationRejected
    assert result.code is code
    assert 1 <= len(result.reason) <= 240
    assert result.reason.strip()
    assert emitted == {
        "manual": [],
        "reanalysis": [],
        "cancel": [],
    }


def test_active_selection_rejects_manual_judgment_as_target_not_terminal(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(ActiveRunChanged(card("active", active=True)))
    module.set_follow_latest(False)
    module.select_run("active")
    emitted = _capture_operation_request_signals(module)

    result = module.request_manual_judgment("NG")

    _assert_exact_operation_rejection(
        result,
        ResultOperationRejectionCode.TARGET_NOT_TERMINAL,
        emitted,
    )


def test_forbidden_terminal_rejects_reanalysis_as_operation_not_allowed(qapp):
    module = ResultDashboardModule(services())
    forbidden_card = replace(
        card("forbidden"),
        manual_judgment_allowed=False,
        manual_reanalysis_allowed=False,
    )
    module.apply_history_event(
        RunTerminated(
            forbidden_card,
            review_payload=review("forbidden"),
        )
    )
    emitted = _capture_operation_request_signals(module)

    result = module.request_reanalysis(0, 1)

    _assert_exact_operation_rejection(
        result,
        ResultOperationRejectionCode.OPERATION_NOT_ALLOWED,
        emitted,
    )


def test_terminal_selection_rejects_cancel_as_not_reanalyzing(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1"))
    emitted = _capture_operation_request_signals(module)

    result = module.request_reanalysis_cancel()

    _assert_exact_operation_rejection(
        result,
        ResultOperationRejectionCode.NOT_REANALYZING,
        emitted,
    )


def test_corrupt_reanalyzing_selection_rejects_cancel_as_missing_operation_id(qapp):
    class MissingOperationStore(ResultHistoryStore):
        expose_corruption = False

        def snapshot(self):
            snapshot = super().snapshot()
            if not self.expose_corruption:
                return snapshot
            malformed_card = SimpleNamespace(
                run_id="run-corrupt",
                is_active_run=False,
                phase=ResultPhase.REANALYZING,
                operation_id=None,
                revision=1,
            )
            return SimpleNamespace(
                revision=snapshot.revision,
                follow_latest=True,
                selected_run_id="run-corrupt",
                active_cards=(),
                terminal_cards=(malformed_card,),
            )

    store = MissingOperationStore()
    module = ResultDashboardModule(services(), store)
    store.expose_corruption = True
    emitted = _capture_operation_request_signals(module)

    result = module.request_reanalysis_cancel()

    _assert_exact_operation_rejection(
        result,
        ResultOperationRejectionCode.MISSING_OPERATION_ID,
        emitted,
    )


def test_reanalysis_start_success_and_cancel_project_correctly(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1", level=1))
    started = module.apply_history_event(ReanalysisStarted("run-1", "op", 0))
    assert started.snapshot.terminal_cards[0].phase is ResultPhase.REANALYZING
    module.apply_history_event(ReanalysisCompleted(
        "run-1", "op", 1,
        replace(card("run-1"), conclusion=ResultConclusion.NG),
        review_payload=review("run-1", 8),
    ))
    assert module.history_snapshot().terminal_cards[0].conclusion is ResultConclusion.NG
    module.apply_history_event(ReanalysisStarted("run-1", "cancel-op", 2))
    module.apply_history_event(ReanalysisCancelled("run-1", "cancel-op", 3))
    assert module.history_snapshot().terminal_cards[0].phase is ResultPhase.TERMINAL


def test_reanalysis_error_completion_switches_to_structured_error_and_clears_chart(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1", level=3))
    old_chart = module.panel_widget("chart")
    module.apply_history_event(
        ReanalysisStarted("run-1", "error-operation", 0)
    )

    transition = module.apply_history_event(
        ReanalysisCompleted(
            "run-1",
            "error-operation",
            1,
            replace(
                card("run-1"),
                conclusion=ResultConclusion.ERROR,
                reason="reanalysis failed",
            ),
            error_payload=ErrorDisplayPayload(
                "reanalysis",
                "reanalysis failed",
                (ConfigSummaryField("run_id", "run-1"),),
            ),
        )
    )

    selected = transition.snapshot.terminal_cards[0]
    assert selected.run_id == "run-1"
    assert selected.revision == 2
    assert selected.conclusion is ResultConclusion.ERROR
    assert module.display_stack().currentWidget().objectName() == (
        "result_dashboard_error_page"
    )
    assert module.cards_by_panel_id() == {}
    assert module.panel_widget("chart") is None
    assert old_chart is not module.panel_widget("chart")
    assert module.findChild(
        QLabel,
        "result_dashboard_error_category",
    ).text() == "错误类别：reanalysis"
    assert module.findChild(
        QLabel,
        "result_dashboard_error_reason",
    ).text() == "reanalysis failed"
    assert "run-1" in module.findChild(
        QLabel,
        "result_dashboard_error_context",
    ).text()
    assert module._factory_display.review_payload == FactoryReviewPayload()
    assert module._factory_display.state == FactoryControllerState()
    assert module._projection_services.config_service.load_config(7) == {}
    assert old_chart.isVisible() is False


def test_waiting_projection_tears_down_private_review_then_later_review_repopulates(qapp):
    module = ResultDashboardModule(services())
    module.apply_history_event(terminal("run-1", level=4))
    old_chart = module.panel_widget("chart")
    old_chart_ref = weakref.ref(old_chart)
    assert module._factory_display.review_payload.run_id == "run-1"
    assert module._projection_services.config_service.load_config(7)

    module.apply_history_event(
        ActiveRunChanged(card("active", active=True))
    )
    module.set_follow_latest(False)
    module.select_run("active")

    assert module.display_stack().currentWidget().objectName() == (
        "result_dashboard_waiting_page"
    )
    assert module.cards_by_panel_id() == {}
    assert module._factory_display.review_payload == FactoryReviewPayload()
    assert module._factory_display.state == FactoryControllerState()
    assert module._projection_services.config_service.load_config(7) == {}
    assert old_chart.isVisible() is False

    module.apply_history_event(terminal("run-2", level=8))
    module.select_run("run-2")

    assert module.projected_run_id() == "run-2"
    assert module._factory_display.review_payload.run_id == "run-2"
    assert module._factory_display.state.displayed_config_id == 7
    assert module._projection_services.config_service.load_config(7)
    assert module.panel_widget("chart") is not None


def test_reanalyzing_eviction_rejects_late_completion_without_restoring_run_or_chart(qapp):
    store = ResultHistoryStore()
    module = ResultDashboardModule(services(), store)
    module.apply_history_event(terminal("run-1", level=1))
    module.apply_history_event(
        ReanalysisStarted("run-1", "evicted-operation", 0)
    )
    assert module.history_snapshot().terminal_cards[0].revision == 1

    for index in range(2, 8):
        transition = module.apply_history_event(
            terminal(f"run-{index}", level=float(index))
        )

    assert transition.evicted_run_id == "run-1"
    before = module.history_snapshot()
    before_chart_payload = module.panel_widget("chart").current_plot_payload()
    assert before.selected_run_id == "run-7"
    assert module.projected_run_id() == "run-7"
    assert all(item.run_id != "run-1" for item in before.terminal_cards)
    assert store.display_payload("run-1") is None
    assert module._factory_display.review_payload.run_id == "run-7"

    with pytest.raises(ResultHistoryError, match="not cached"):
        module.apply_history_event(
            ReanalysisCompleted(
                "run-1",
                "evicted-operation",
                1,
                replace(
                    card("run-1"),
                    conclusion=ResultConclusion.NG,
                ),
                review_payload=review("run-1", 99),
            )
        )

    assert module.history_snapshot() == before
    assert module.projected_run_id() == "run-7"
    assert module.panel_widget("chart").current_plot_payload() == (
        before_chart_payload
    )
    assert all(
        item.run_id != "run-1"
        for item in module.history_snapshot().terminal_cards
    )


def test_missing_payload_for_selected_terminal_shows_safe_error_clears_chart_and_diagnoses(qapp, caplog):
    snapshot = ResultHistorySnapshot(1, True, "run-1", (), (card("run-1"),))
    class CorruptStore:
        def snapshot(self):
            return snapshot
        def display_payload(self, run_id):
            return None

    module = ResultDashboardModule(services(), CorruptStore())
    diagnostics = []
    module.history_diagnostic.connect(diagnostics.append)
    module.refresh_history_projection()
    assert module.cards_by_panel_id() == {}
    assert module.display_stack().currentWidget().objectName() == "result_dashboard_error_page"
    assert diagnostics[-1].code is HistoryProjectionDiagnosticCode.MISSING_SELECTED_PAYLOAD
    assert "run-1" in caplog.text


def test_dashboard_destruction_does_not_retain_chart_or_callback_references(qapp):
    class CallbackCapture:
        pass

    module = ResultDashboardModule(services())
    private_bus = module.projection_bus()
    chart = QWidget(module)
    chart.setObjectName("callback_owned_chart_probe")
    capture = CallbackCapture()
    private_bus.factory_review_changed.connect(
        lambda _review, retained=capture: retained
    )
    module_ref = weakref.ref(module)
    bus_ref = weakref.ref(private_bus)
    chart_ref = weakref.ref(chart)
    capture_ref = weakref.ref(capture)

    module.close()
    module.deleteLater()
    del capture
    del chart
    del private_bus
    del module
    _flush_deferred_deletes_safely(qapp)

    released_module = module_ref()
    released_bus = bus_ref()
    released_chart = chart_ref()
    assert released_module is None or sip.isdeleted(released_module)
    assert released_bus is None or sip.isdeleted(released_bus)
    assert released_chart is None or sip.isdeleted(released_chart)
    assert capture_ref() is None
