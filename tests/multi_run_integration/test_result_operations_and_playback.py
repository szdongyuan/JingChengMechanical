from __future__ import annotations

import os
from types import MethodType
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

import app.services_container as services_module
from app.main_window import MainWindow
from app.service.multi_run_coordinator_mvp import (
    HistoryFactKind,
    ManualJudgmentRequest as CompositeManualJudgmentRequest,
    OperationResult,
    OperationStatus,
    ReanalysisCancelRequest as CompositeReanalysisCancelRequest,
    ReanalysisRequest as CompositeReanalysisRequest,
    TerminalConclusion,
)
from app.service.result_history_contracts import (
    ManualJudgmentRequest as UiManualJudgmentRequest,
    ManualJudgmentTarget,
    ReanalysisCancelRequest as UiReanalysisCancelRequest,
    ReanalysisRequest as UiReanalysisRequest,
)
from app.service.run_scoped_playback.contracts import (
    PlaybackCode,
    PlaybackLifecycleCode,
    PlaybackLifecycleStatus,
    PlaybackStatus,
)
from tests.multi_run_integration.fakes import projected_event
from ui.modules.control import ControlModule
from ui.modules.stft import (
    _PLAYBACK_DIAGNOSTICS,
    _PLAYBACK_LIFECYCLE_DIAGNOSTICS,
)


RUN_ID = "00000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "10000000-0000-4000-8000-000000000001"


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []
        self.emissions = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        self.emissions.append((args, {}))
        for callback in tuple(self._callbacks):
            callback(*args)


class _OperationWindow:
    pass


def _operation_window():
    coordinator = SimpleNamespace(
        apply_manual=MagicMock(
            return_value=OperationResult(
                run_id=RUN_ID,
                status=OperationStatus.COMPLETED,
                attempt_id=None,
                reason="",
            )
        ),
        start_reanalysis=MagicMock(
            return_value=OperationResult(
                run_id=RUN_ID,
                status=OperationStatus.SUBMITTED,
                attempt_id=ATTEMPT_ID,
                reason="",
            )
        ),
        cancel_reanalysis=MagicMock(return_value=object()),
        stop_all=MagicMock(return_value=object()),
    )
    plc = SimpleNamespace(
        publish_terminal=MagicMock(),
        publish_rejection=MagicMock(),
        write_result=MagicMock(),
    )
    control = SimpleNamespace(
        manual_judgment_intent=_Signal(),
        stop_intent=_Signal(),
        set_manual_judgment_target=MagicMock(),
        handle_manual_judgment_result=MagicMock(),
    )
    dashboard = SimpleNamespace(
        manual_judgment_target_changed=_Signal(),
        manual_judgment_requested=_Signal(),
        reanalysis_requested=_Signal(),
        reanalysis_cancel_requested=_Signal(),
        request_manual_judgment=MagicMock(return_value=object()),
    )
    window = _OperationWindow()
    window.services = SimpleNamespace(
        multi_run_coordinator=coordinator,
        final_composition=SimpleNamespace(plc_result_sequencer=plc),
        bus=SimpleNamespace(status=_Signal()),
    )
    window.control = control
    window.result_dashboard = dashboard
    for method_name in (
        "_apply_manual_judgment_request",
        "_start_reanalysis_request",
        "_cancel_reanalysis_request",
        "_stop_all_active_runs",
    ):
        setattr(
            window,
            method_name,
            MethodType(getattr(MainWindow, method_name), window),
        )
    return window, coordinator, plc, control, dashboard


def test_selected_result_operations_and_playback_are_run_revision_scoped():
    window, coordinator, plc, control, dashboard = _operation_window()
    MainWindow._wire_run_scoped_result_operations(window)
    target = ManualJudgmentTarget(RUN_ID, 7, ("异响",), True)

    dashboard.manual_judgment_target_changed.emit(target)
    control.manual_judgment_intent.emit("NG", "异响")
    dashboard.manual_judgment_requested.emit(
        UiManualJudgmentRequest(RUN_ID, "NG", 7, "异响")
    )
    dashboard.reanalysis_requested.emit(
        UiReanalysisRequest(RUN_ID, 7, 0.125, 1.875)
    )
    dashboard.reanalysis_cancel_requested.emit(
        UiReanalysisCancelRequest(RUN_ID, ATTEMPT_ID, 8)
    )
    control.stop_intent.emit()

    control.set_manual_judgment_target.assert_called_once_with(target)
    dashboard.request_manual_judgment.assert_called_once_with("NG", "异响")
    coordinator.apply_manual.assert_called_once_with(
        CompositeManualJudgmentRequest(
            run_id=RUN_ID,
            expected_revision=7,
            conclusion=TerminalConclusion.NG,
            fault_type="异响",
            bounded_note="",
        )
    )
    control.handle_manual_judgment_result.assert_called_once_with(
        coordinator.apply_manual.return_value
    )
    coordinator.start_reanalysis.assert_called_once_with(
        CompositeReanalysisRequest(
            run_id=RUN_ID,
            expected_revision=7,
            start_sec=0.125,
            end_sec=1.875,
        )
    )
    coordinator.cancel_reanalysis.assert_called_once_with(
        CompositeReanalysisCancelRequest(
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            expected_revision=8,
        )
    )
    coordinator.stop_all.assert_called_once_with(5.0)
    plc.publish_terminal.assert_not_called()
    plc.publish_rejection.assert_not_called()
    plc.write_result.assert_not_called()


def test_reanalysis_stop_button_calls_coordinator_once(qapp):
    bus = SimpleNamespace(
        workorder_changed=_Signal(),
        factory_state_changed=_Signal(),
        factory_start_blocked=_Signal(),
        factory_runtime_aborted=_Signal(),
        current_workorder_payload={},
    )
    control = ControlModule(
        SimpleNamespace(
            bus=bus,
            factory_mode=SimpleNamespace(
                state=services_module.FactoryControllerState(
                    selected_config_ids=(101,),
                    run_phase="reanalyzing",
                )
            ),
        )
    )
    window, coordinator, _plc, _placeholder_control, _dashboard = _operation_window()
    window.control = control
    MainWindow._wire_run_scoped_result_operations(window)
    control.show()
    qapp.processEvents()

    stop_buttons = [
        button
        for button in control.findChildren(QPushButton)
        if button.text() == "停止测试" and button.isVisible()
    ]

    assert len(stop_buttons) == 1
    assert stop_buttons[0].isEnabled() is True

    stop_buttons[0].click()

    coordinator.stop_all.assert_called_once_with(5.0)
    control.close()


@pytest.mark.parametrize(
    ("label", "fault_type", "conclusion"),
    [
        ("OK", None, TerminalConclusion.OK),
        ("NG", "漏装", TerminalConclusion.NG),
    ],
)
def test_manual_mapping_calls_one_coordinator_and_zero_plc(
    label,
    fault_type,
    conclusion,
):
    window, coordinator, plc, control, _dashboard = _operation_window()
    request = UiManualJudgmentRequest(RUN_ID, label, 11, fault_type)

    MainWindow._apply_manual_judgment_request(window, request)

    coordinator.apply_manual.assert_called_once_with(
        CompositeManualJudgmentRequest(
            run_id=RUN_ID,
            expected_revision=11,
            conclusion=conclusion,
            fault_type=fault_type,
            bounded_note="",
        )
    )
    control.handle_manual_judgment_result.assert_called_once_with(
        coordinator.apply_manual.return_value
    )
    plc.publish_terminal.assert_not_called()
    plc.publish_rejection.assert_not_called()
    plc.write_result.assert_not_called()


def test_deleted_run_reanalysis_preserves_not_found_contract_and_localizes_once():
    window, coordinator, _plc, _control, _dashboard = _operation_window()
    low_level_result = OperationResult(
        run_id=RUN_ID,
        status=OperationStatus.NOT_FOUND,
        attempt_id=None,
        reason="result-control facts were not found",
    )
    coordinator.start_reanalysis.return_value = low_level_result

    MainWindow._start_reanalysis_request(
        window,
        UiReanalysisRequest(RUN_ID, 11, 0.125, 1.875),
    )

    coordinator.start_reanalysis.assert_called_once()
    assert coordinator.start_reanalysis.return_value is low_level_result
    assert low_level_result.reason == "result-control facts were not found"
    assert window.services.bus.status.emissions == [
        (("重新分析失败：所选运行记录不存在，可能已被删除",), {})
    ]


def test_dynamic_stft_uses_dashboard_selected_run_and_shared_playback(qapp):
    from tests.test_result_dashboard_module import (
        _config,
        _event,
        _panel,
        _services,
    )
    from ui.modules.result_dashboard import ResultDashboardModule

    playback = object()
    service_container = _services()
    service_container.final_composition = SimpleNamespace(playback=playback)
    dashboard = ResultDashboardModule(service_container)
    dashboard.apply_history_event(
        _event(
            RUN_ID,
            _config([_panel("stft", "stft", channel="selected_1")]),
            chart_payload={},
        )
    )

    stft = dashboard.panel_widget("stft")
    assert stft.run_scoped_playback is playback
    assert stft.selected_run_id_provider() == RUN_ID
    assert stft.selected_display_channel_provider() == 0


def test_playback_maps_every_status_and_code_without_truthiness():
    mapped = set(_PLAYBACK_DIAGNOSTICS)

    assert {status for status, _code in mapped} == set(PlaybackStatus)
    assert {code for _status, code in mapped} == set(PlaybackCode)
    assert _PLAYBACK_DIAGNOSTICS[
        (
            PlaybackStatus.UNAVAILABLE,
            PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE,
        )
    ] == "播放失败：所选运行记录的音频不存在，可能已被删除"


def test_playback_lifecycle_maps_every_status_and_code_without_truthiness():
    mapped = set(_PLAYBACK_LIFECYCLE_DIAGNOSTICS)

    assert {status for status, _code in mapped} == set(
        PlaybackLifecycleStatus
    )
    assert {code for _status, code in mapped} == set(
        PlaybackLifecycleCode
    )


def test_committed_statistics_observer_runs_once_and_never_gates_history():
    trace = []
    diagnostics = []

    class Primary:
        def publish(self, event) -> None:
            trace.append(("dashboard", event))

    def observer(event) -> None:
        trace.append(("statistics", event))
        raise RuntimeError("statistics refresh exploded")

    sink = services_module._PostDurableHistorySink(
        Primary(),
        observer,
        diagnostics.append,
    )
    event = projected_event(
        HistoryFactKind.RUN_TERMINATED,
        expected_revision=1,
        proposed_revision=2,
    )

    sink.publish(event)

    assert trace == [("dashboard", event), ("statistics", event)]
    assert len(diagnostics) == 1
    assert "statistics refresh exploded" in diagnostics[0]
    assert len(diagnostics[0].encode("utf-8")) <= 512

    trace.clear()
    diagnostics.clear()
    replacement = projected_event(
        HistoryFactKind.TERMINAL_RESULT_REPLACED,
        expected_revision=2,
        proposed_revision=3,
    )
    preaccept = projected_event(HistoryFactKind.RUN_TERMINATED)

    sink.publish(replacement)
    sink.publish(preaccept)

    assert trace == [
        ("dashboard", replacement),
        ("statistics", replacement),
        ("dashboard", preaccept),
    ]
    assert len(diagnostics) == 1
    assert "statistics refresh exploded" in diagnostics[0]


def test_committed_enrichment_forwards_only_to_primary_history_adapter():
    primary_calls = []
    observer_calls = []

    class Primary:
        def publish(self, _event) -> None:
            raise AssertionError("terminal publish path was used")

        def publish_enrichment(self, event) -> None:
            primary_calls.append(event)

    sink = services_module._PostDurableHistorySink(
        Primary(),
        observer_calls.append,
        lambda _message: None,
    )
    enrichment = object()

    sink.publish_enrichment(enrichment)

    assert primary_calls == [enrichment]
    assert observer_calls == []
