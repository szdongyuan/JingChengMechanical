from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QScrollArea,
    QTableWidget,
    QWidget,
)

from app.service.analysis.imported_audio import (
    ImportedAudioAnalysisRequest,
    ImportedChannelAssignment,
)
from app.service.multi_run_coordinator_mvp import OperationResult, OperationStatus
from app.service.result_history import (
    FrozenConfigSummary,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultHistorySnapshot,
    ResultPhase,
)
from app.service.result_history_contracts import ManualJudgmentTarget

from app.services_container import (
    FactoryControllerState,
    FactoryStartBlockedPayload,
)
from ui.modules.control import ControlModule
from ui.modules.result_history import ResultHistoryModule
from ui.modules.workorder import WorkOrderModule
from ui.widgets.result_history_card import ResultHistoryCard


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.workorder_changed = _SignalStub()
        self.config_changed = _SignalStub()
        self.plc_changed = _SignalStub()
        self.plc_workorder_changed = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_start_blocked = _SignalStub()
        self.factory_runtime_aborted = _SignalStub()
        self.factory_selection_notice = _SignalStub()
        self.log = _SignalStub()
        self.status = _SignalStub()
        self.current_workorder_payload = {}


class _ConfigServiceStub:
    def __init__(self):
        self._configs = [
            {
                "config_id": 101,
                "config_name": "Config Alpha",
                "analysis_window_offset_ms": 0,
                "analysis_duration": 2.0,
                "record_duration": 5.0,
                "plc_device_key": "plc-a",
                "plc_device_label": "PLC A",
                "scan_triggers_test": False,
                "selected_channels": [0, 1],
                "fault_types": ["aa", "bb", "cc"],
            },
            {
                "config_id": 202,
                "config_name": "Config Beta",
                "analysis_window_offset_ms": 0,
                "analysis_duration": 2.0,
                "record_duration": 5.0,
                "plc_device_key": None,
                "plc_device_label": None,
                "scan_triggers_test": True,
                "selected_channels": [1],
            },
        ]
        self._details = {
            config["config_id"]: dict(config)
            for config in self._configs
        }

    def list_configs(self):
        return [
            {
                "config_id": config["config_id"],
                "config_name": config["config_name"],
            }
            for config in self._configs
        ]

    def load_config(self, config_id: int):
        return dict(self._details[config_id])


class _FactorySelectionSettingsStub:
    def __init__(self, saved_selected_ids: tuple[int, ...] = ()):
        self._saved_selected_ids = tuple(saved_selected_ids)

    def load_selected_config_ids(self) -> tuple[int, ...]:
        return self._saved_selected_ids

    def save_selected_config_ids(self, config_ids: tuple[int, ...]) -> str | None:
        self._saved_selected_ids = tuple(config_ids)
        return None


class _FactoryModeStub:
    def __init__(self, bus, initial_state: FactoryControllerState | None = None):
        self._bus = bus
        self.state = initial_state or FactoryControllerState()
        self.start_manual = MagicMock(return_value=True)
        self.stop_active_run = MagicMock(return_value=True)
        self.analyze_imported_audio = MagicMock(return_value=True)
        self.apply_manual_judgment = MagicMock(
            return_value=SimpleNamespace(success=True, message="")
        )

    def restore_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        *,
        had_missing_configs: bool,
    ):
        selected = tuple(selected_config_ids)
        reason = "配置已失效" if had_missing_configs else None
        self.state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if selected and reason is None else "not_ready",
            selection_ready=(reason is None and bool(selected)),
            selection_ready_reason=reason,
            start_ready=(reason is None and bool(selected)),
            start_ready_reason=reason,
        )
        return self.state

    def set_selection(self, selected_config_ids: tuple[int, ...] | list[int]):
        selected = tuple(selected_config_ids)
        self.state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if selected else "not_ready",
        )
        return self.state

    def confirm_selection(self, selected_config_ids: tuple[int, ...] | list[int]):
        self.set_selection(selected_config_ids)
        self._bus.factory_state_changed.emit(self.state)
        return SimpleNamespace(
            status="applied",
            selected_config_ids=self.state.selected_config_ids,
            message="",
        )


class _MessageBoxSpy:
    def __init__(self):
        self.open_count = 0
        self.close_count = 0
        self.latest_text = ""
        self.latest_title = ""
        self.was_closed = False
        self.dialog = _FakeMessageBox(self)


class _FakeMessageBox:
    def __init__(self, recorder: _MessageBoxSpy):
        self._recorder = recorder
        self.finished = _SignalStub()

    def setWindowTitle(self, title: str) -> None:
        self._recorder.latest_title = title

    def setText(self, text: str) -> None:
        self._recorder.latest_text = text

    def setIcon(self, _icon) -> None:
        return None

    def setStandardButtons(self, _buttons) -> None:
        return None

    def open(self) -> None:
        self._recorder.open_count += 1

    def close(self) -> None:
        self._recorder.close_count += 1
        self._recorder.was_closed = True
        self.finished.emit(0)

    def raise_(self) -> None:
        return None

    def activateWindow(self) -> None:
        return None


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _idle_blocked_state(
    reason: str,
    *,
    selected_config_ids: tuple[int, ...] = (101,),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        run_phase="not_ready",
        selection_ready=False,
        selection_ready_reason=reason,
        start_ready=False,
        start_ready_reason=reason,
    )


def _ready_state() -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=(101,),
        run_phase="armed",
        selection_ready=True,
        start_ready=True,
    )


def _manual_target(
    run_id: str = "run-1",
    *,
    revision: int = 1,
    fault_types: tuple[str, ...] = ("异响", "漏装"),
    allowed: bool = True,
) -> ManualJudgmentTarget:
    return ManualJudgmentTarget(
        run_id=run_id,
        revision=revision,
        fault_types=fault_types,
        manual_judgment_allowed=allowed,
    )


def _make_module(
    *,
    initial_state: FactoryControllerState | None = None,
    production_unavailable_reason: str | None = None,
):
    bus = _BusStub()
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(),
        factory_selection_settings=_FactorySelectionSettingsStub(),
        factory_mode=_FactoryModeStub(bus, initial_state),
        plc_services={},
        plc_threads={},
        plc_state={},
        production_unavailable_reason=production_unavailable_reason,
    )
    return ControlModule(services), services


def _make_shared_modules(
    *,
    initial_state: FactoryControllerState | None = None,
):
    bus = _BusStub()
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(),
        factory_selection_settings=_FactorySelectionSettingsStub(),
        factory_mode=_FactoryModeStub(bus, initial_state),
        plc_services={},
        plc_threads={},
        plc_state={},
    )
    return WorkOrderModule(services), ControlModule(services), services


def _import_request() -> ImportedAudioAnalysisRequest:
    return ImportedAudioAnalysisRequest(
        config_id=101,
        wav_path="import.wav",
        assignments=(ImportedChannelAssignment(0, "config", 0),),
    )


class _FakeDialogReturningRequest:
    def __init__(self, *, configs, default_config_id, parent=None):
        self.configs = configs
        self.default_config_id = default_config_id
        self.parent = parent

    def exec(self):
        return int(QDialog.DialogCode.Accepted)

    def current_request(self):
        return _import_request()


def _make_module_with_message_box_spy(
    monkeypatch,
    *,
    initial_state: FactoryControllerState | None = None,
):
    module, services = _make_module(initial_state=initial_state)
    recorder = _MessageBoxSpy()
    monkeypatch.setattr(module, "_create_blocked_start_dialog", lambda: recorder.dialog)
    return module, services, recorder


def test_control_panel_renders_three_button_rows_with_updated_spacing_and_button_heights(
    qapp,
):
    module, _ = _make_module()

    assert hasattr(module, "lbl_start_reason") is False
    assert module.layout().count() == 3
    assert module.layout().spacing() == 8
    assert module.btn_start.minimumHeight() == 36
    assert module.btn_stop.minimumHeight() == 36
    assert module.btn_stop.text() == "停止测试"
    assert module.btn_pass.minimumHeight() == 36
    assert module.btn_fail.minimumHeight() == 36
    assert module.btn_import_audio.minimumHeight() == 36


def test_manual_actions_are_one_shared_widget_outside_left_control_layout(qapp):
    module, _services = _make_module()

    actions = module.manual_judgment_widget

    assert isinstance(actions, QWidget)
    assert actions.objectName() == "manual_judgment_actions"
    assert module.layout().indexOf(actions) == -1
    assert actions.isAncestorOf(module.btn_pass)
    assert actions.isAncestorOf(module.btn_fail)
    assert all(actions.isAncestorOf(button) for button in module.manual_fault_buttons)
    assert module.btn_start.parentWidget() is module
    assert module.btn_stop.parentWidget() is module
    assert module.btn_import_audio.parentWidget() is module


def test_real_five_fault_ng_submission_keeps_history_geometry_and_focus(qapp):
    bus = _BusStub()
    config_service = _ConfigServiceStub()
    fault_types = ("异响", "漏装", "松动", "破损", "电流异常")
    config_service._details[101]["fault_types"] = list(fault_types)
    services = SimpleNamespace(
        bus=bus,
        config_service=config_service,
        factory_selection_settings=_FactorySelectionSettingsStub(),
        factory_mode=_FactoryModeStub(bus, _ready_state()),
    )
    control = ControlModule(services)
    history = ResultHistoryModule(
        manual_judgment_widget=control.manual_judgment_widget
    )
    started_at = datetime(2026, 7, 30, 9, 0)
    cards = tuple(
        ResultHistoryCardSnapshot(
            run_id=f"run-{index}",
            config=FrozenConfigSummary(
                config_id=101,
                name="Config Alpha",
                fault_types=fault_types,
            ),
            sn=f"SN-{index}",
            trigger_source="manual",
            started_at=started_at + timedelta(minutes=index),
            terminated_at=started_at + timedelta(minutes=index, seconds=5),
            phase=ResultPhase.TERMINAL,
            conclusion=ResultConclusion.OK,
            reason=None,
            is_active_run=False,
            manual_judgment_allowed=True,
            manual_reanalysis_allowed=True,
            revision=1,
        )
        for index in range(6)
    )
    selected_run_id = "run-4"
    snapshot = ResultHistorySnapshot(
        revision=1,
        follow_latest=False,
        selected_run_id=selected_run_id,
        active_cards=(),
        terminal_cards=cards,
    )
    history.render_snapshot(snapshot)
    control.set_manual_judgment_target(
        ManualJudgmentTarget(
            run_id=selected_run_id,
            revision=1,
            fault_types=fault_types,
            manual_judgment_allowed=True,
        )
    )
    history.resize(360, 260)
    history.show()
    QTest.qWait(180)
    qapp.processEvents()

    scroll = history.findChild(QScrollArea, "result_history_scroll")
    assert scroll is not None
    scrollbar = scroll.verticalScrollBar()
    card_widgets = {
        str(card.property("run_id")): card
        for card in history.findChildren(ResultHistoryCard)
    }
    selected_card = card_widgets[selected_run_id]
    scroll.ensureWidgetVisible(selected_card, 0, 0)
    qapp.processEvents()
    scrollbar.setValue(scrollbar.maximum() // 2)
    qapp.processEvents()

    def geometry_signature():
        current_cards = {
            str(card.property("run_id")): card
            for card in history.findChildren(ResultHistoryCard)
            if card.isVisible()
        }
        selected = current_cards[selected_run_id]
        table = selected.findChild(
            QTableWidget, "result_history_card_summary_table"
        )
        assert table is not None
        return (
            tuple(
                (
                    run_id,
                    card.height(),
                    card.mapTo(history._scroll_content, QPoint(0, 0)).y(),
                )
                for run_id, card in sorted(current_cards.items())
            ),
            table.height(),
            selected.manual_judgment_slot.height(),
            history._scroll_content.height(),
            scrollbar.maximum(),
            scrollbar.value(),
        )

    initial_signature = geometry_signature()
    initial_cards = dict(card_widgets)
    action_slot = selected_card.manual_judgment_slot
    actions = control.manual_judgment_widget
    assert action_slot.height() >= actions.sizeHint().height()
    assert actions.parentWidget() is action_slot
    assert not actions.findChildren(QScrollArea)
    assert all(button.isVisible() for button in control.manual_fault_buttons)
    assert control.btn_pass.objectName() == "Success"
    assert all(
        button.objectName() == "Fail"
        for button in control.manual_fault_buttons
    )
    assert [
        control.manual_fault_button_layout.getItemPosition(
            control.manual_fault_button_layout.indexOf(widget)
        )[:2]
        for widget in (control.btn_pass, *control.manual_fault_buttons)
    ] == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]

    signatures_during_submission = []

    def submit_ng(_label, fault_type):
        nonlocal snapshot
        signatures_during_submission.append(geometry_signature())
        updated_cards = tuple(
            replace(
                card,
                conclusion=ResultConclusion.NG,
                fault_type=fault_type,
                revision=2,
                manual_judgment_revision=1,
            )
            if card.run_id == selected_run_id
            else card
            for card in cards
        )
        snapshot = ResultHistorySnapshot(
            revision=2,
            follow_latest=False,
            selected_run_id=selected_run_id,
            active_cards=(),
            terminal_cards=updated_cards,
        )
        history.render_snapshot(snapshot)
        qapp.processEvents()

    control.manual_judgment_intent.connect(submit_ng)
    control.manual_fault_buttons[-1].click()
    qapp.processEvents()

    final_cards = {
        str(card.property("run_id")): card
        for card in history.findChildren(ResultHistoryCard)
        if card.isVisible()
    }
    assert signatures_during_submission == [initial_signature]
    assert geometry_signature() == initial_signature
    assert final_cards == initial_cards
    assert selected_card.manual_judgment_widget is actions
    assert actions.parentWidget() is action_slot
    assert control.manual_fault_buttons[-1].isChecked()


def test_unchanged_manual_target_setter_is_a_complete_no_op(qapp, monkeypatch):
    module, _services = _make_module()
    target = _manual_target()
    module.set_manual_judgment_target(target)
    sync_calls = []
    monkeypatch.setattr(
        module,
        "_sync_manual_judgment_controls",
        lambda: sync_calls.append(True),
    )

    module.set_manual_judgment_target(target)

    assert sync_calls == []


def test_control_module_has_import_audio_button(qapp):
    module, _ = _make_module()

    assert module.btn_import_audio.text() == "导入音频分析"


def test_restart_control_is_always_available_and_emits_only_one_pending_intent(qapp):
    module, _ = _make_module(
        production_unavailable_reason="生产功能不可用，请修复设备或配置后重启软件。"
    )
    intents = []
    module.restart_intent.connect(lambda: intents.append("restart"))

    assert module.btn_restart.text() == "重启软件"
    assert module.btn_restart.isEnabled() is True

    module.btn_restart.click()
    module.btn_restart.click()

    assert intents == ["restart"]
    assert module.btn_restart.isEnabled() is False

    module.set_restart_pending(False)

    assert module.btn_restart.isEnabled() is True
    module.btn_restart.click()
    assert intents == ["restart", "restart"]


def test_control_connects_start_blocked_without_legacy_runtime_abort(qapp):
    module, services = _make_module()

    assert module._on_factory_state_changed in (
        services.bus.factory_state_changed._callbacks
    )
    assert module._on_factory_start_blocked in (
        services.bus.factory_start_blocked._callbacks
    )
    assert services.bus.factory_runtime_aborted._callbacks == []
    assert not hasattr(module, "_on_factory_runtime_aborted")


@pytest.mark.parametrize("run_phase", ["running", "analyzing"])
def test_stop_button_exposes_one_intent_without_legacy_write(qapp, run_phase):
    module, services = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase=run_phase,
        )
    )
    intents = []
    module.stop_intent.connect(lambda: intents.append("stop"))

    module.btn_stop.click()

    assert module.btn_stop.text() == "停止测试"
    assert intents == ["stop"]
    services.factory_mode.stop_active_run.assert_not_called()


@pytest.mark.parametrize("run_phase", ["running", "analyzing", "reanalyzing"])
def test_import_audio_button_is_disabled_for_busy_run_phases(qapp, run_phase):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase=run_phase,
        )
    )

    assert module.btn_import_audio.isEnabled() is False


@pytest.mark.parametrize("busy_reason", ["当前测试运行中", "启动请求处理中"])
def test_import_audio_button_is_disabled_for_controller_busy_start_reasons(
    qapp,
    monkeypatch,
    busy_reason,
):
    module, services = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            selection_ready=True,
            start_ready=False,
            start_ready_reason=busy_reason,
        )
    )
    dialog_created = []
    warnings = []

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            dialog_created.append((args, kwargs))

    monkeypatch.setattr("ui.modules.control.ImportAudioAnalysisDialog", FakeDialog)
    monkeypatch.setattr(
        "ui.modules.control.QMessageBox.warning",
        lambda parent, title, text: warnings.append((title, text)),
    )

    assert module.btn_import_audio.isEnabled() is False

    module.btn_import_audio.click()

    assert dialog_created == []
    assert warnings == []
    services.factory_mode.analyze_imported_audio.assert_not_called()


def test_import_audio_requires_selected_config(qapp, monkeypatch):
    module, services = _make_module(
        initial_state=FactoryControllerState(selected_config_ids=())
    )
    warnings = []
    monkeypatch.setattr(
        "ui.modules.control.QMessageBox.warning",
        lambda parent, title, text: warnings.append((title, text)),
    )

    module.btn_import_audio.click()

    services.factory_mode.analyze_imported_audio.assert_not_called()
    assert warnings == [("导入音频分析", "请先启用一个配置")]


def test_import_audio_no_selection_prompt_still_works_when_not_busy(
    qapp,
    monkeypatch,
):
    module, services = _make_module(
        initial_state=FactoryControllerState(selected_config_ids=())
    )
    warnings = []
    monkeypatch.setattr(
        "ui.modules.control.QMessageBox.warning",
        lambda parent, title, text: warnings.append((title, text)),
    )

    assert module.btn_import_audio.isEnabled() is True

    module.btn_import_audio.click()

    services.factory_mode.analyze_imported_audio.assert_not_called()
    assert warnings == [("导入音频分析", "请先启用一个配置")]


def test_import_audio_config_load_failure_shows_warning(qapp, monkeypatch):
    module, services = _make_module(
        initial_state=FactoryControllerState(selected_config_ids=(101,))
    )
    services.config_service.load_config = MagicMock(side_effect=RuntimeError("missing"))
    dialog_created = []
    warnings = []

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            dialog_created.append((args, kwargs))

    monkeypatch.setattr("ui.modules.control.ImportAudioAnalysisDialog", FakeDialog)
    monkeypatch.setattr(
        "ui.modules.control.QMessageBox.warning",
        lambda parent, title, text: warnings.append((title, text)),
    )

    module.btn_import_audio.click()

    assert dialog_created == []
    services.factory_mode.analyze_imported_audio.assert_not_called()
    assert warnings == [("导入音频分析", "配置加载失败，无法导入音频分析")]


def test_import_audio_with_multiple_configs_requires_dialog_choice(qapp, monkeypatch):
    module, services = _make_module(
        initial_state=FactoryControllerState(selected_config_ids=(101, 202))
    )
    created = {}

    class FakeDialog:
        def __init__(self, *, configs, default_config_id, parent=None):
            created["configs"] = configs
            created["default_config_id"] = default_config_id
            created["parent"] = parent

        def exec(self):
            return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr("ui.modules.control.ImportAudioAnalysisDialog", FakeDialog)

    module.btn_import_audio.click()

    assert [cfg["config_id"] for cfg in created["configs"]] == [101, 202]
    assert created["default_config_id"] is None
    assert created["parent"] is module


def test_import_audio_dialog_success_calls_controller(qapp, monkeypatch):
    module, services = _make_module(
        initial_state=FactoryControllerState(selected_config_ids=(101,))
    )
    monkeypatch.setattr(
        "ui.modules.control.ImportAudioAnalysisDialog",
        _FakeDialogReturningRequest,
    )

    module.btn_import_audio.click()

    services.factory_mode.analyze_imported_audio.assert_called_once_with(
        _import_request()
    )


def test_import_audio_dialog_controller_rejection_shows_warning(qapp, monkeypatch):
    module, services = _make_module(
        initial_state=FactoryControllerState(selected_config_ids=(101,))
    )
    services.factory_mode.analyze_imported_audio.return_value = False
    warnings = []
    monkeypatch.setattr(
        "ui.modules.control.ImportAudioAnalysisDialog",
        _FakeDialogReturningRequest,
    )
    monkeypatch.setattr(
        "ui.modules.control.QMessageBox.warning",
        lambda parent, title, text: warnings.append((title, text)),
    )

    module.btn_import_audio.click()

    assert warnings == [("导入音频分析", "导入音频分析失败，请查看结果区域或日志")]


def test_manual_fault_button_click_submits_typed_request_and_manual_ng_retries(qapp):
    module, services = _make_module()
    run_id = "00000000-0000-4000-8000-000000000001"
    intents = []
    module.manual_judgment_intent.connect(
        lambda label, fault_type: intents.append((label, fault_type))
    )

    module.set_manual_judgment_target(_manual_target(run_id))

    assert module.manual_fault_button_group.exclusive() is True
    assert [button.text() for button in module.manual_fault_buttons] == [
        "异响",
        "漏装",
    ]
    assert all(button.isCheckable() for button in module.manual_fault_buttons)
    assert module.manual_fault_button_group.checkedButton() is None
    assert module.btn_fail.isEnabled() is False
    assert module.btn_fail.isHidden()
    assert module.btn_pass.text() == "√ 合格"
    assert module.manual_fault_button_layout.getItemPosition(
        module.manual_fault_button_layout.indexOf(module.btn_pass)
    )[:2] == (0, 0)
    assert [
        module.manual_fault_button_layout.getItemPosition(
            module.manual_fault_button_layout.indexOf(button)
        )[:2]
        for button in module.manual_fault_buttons
    ] == [(0, 1), (0, 2)]

    module.manual_fault_buttons[0].click()

    assert module.manual_fault_buttons[0].isChecked() is True
    assert intents == [("NG", "异响")]
    services.factory_mode.apply_manual_judgment.assert_not_called()
    services.factory_mode.start_manual.assert_not_called()
    services.factory_mode.stop_active_run.assert_not_called()
    services.factory_mode.analyze_imported_audio.assert_not_called()
    assert services.bus.log.emissions == []

    module.manual_fault_buttons[1].click()

    assert module.manual_fault_buttons[0].isChecked() is False
    assert module.manual_fault_buttons[1].isChecked() is True
    assert intents == [("NG", "异响"), ("NG", "漏装")]

    module.handle_manual_judgment_result(
        OperationResult(
            run_id=run_id,
            status=OperationStatus.PERSISTENCE_REJECTED,
            attempt_id=None,
            reason="persistence rejected",
        )
    )

    assert module.manual_fault_buttons[1].isChecked() is True
    assert module.btn_fail.isEnabled() is False

    module.manual_fault_buttons[1].click()

    assert intents == [("NG", "异响"), ("NG", "漏装"), ("NG", "漏装")]

    module.handle_manual_judgment_result(
        OperationResult(
            run_id=run_id,
            status=OperationStatus.COMPLETED,
            attempt_id=None,
            reason="",
        )
    )

    assert module.manual_fault_button_group.checkedButton() is None
    services.factory_mode.apply_manual_judgment.assert_not_called()


@pytest.mark.parametrize(
    "next_target",
    (
        _manual_target("00000000-0000-4000-8000-000000000002"),
        _manual_target(
            "00000000-0000-4000-8000-000000000001",
            revision=2,
        ),
    ),
)
def test_retired_fault_button_release_never_labels_a_new_target(
    qapp,
    next_target,
):
    module, _ = _make_module()
    intents = []
    module.manual_judgment_intent.connect(
        lambda label, fault_type: intents.append((label, fault_type))
    )
    module.set_manual_judgment_target(
        _manual_target("00000000-0000-4000-8000-000000000001")
    )
    old_button = module.manual_fault_buttons[0]
    module.show()
    qapp.processEvents()

    QTest.mousePress(old_button, Qt.MouseButton.LeftButton)
    module.set_manual_judgment_target(next_target)
    QTest.mouseRelease(old_button, Qt.MouseButton.LeftButton)

    assert intents == []


def test_configured_buttons_stay_prepared_but_hidden_without_terminal_target(qapp):
    module, services = _make_module(initial_state=_ready_state())
    intents = []
    module.manual_judgment_intent.connect(
        lambda label, fault_type: intents.append((label, fault_type))
    )

    assert [button.text() for button in module.manual_fault_buttons] == [
        "aa",
        "bb",
        "cc",
    ]
    assert all(
        button.isEnabled() is False for button in module.manual_fault_buttons
    )
    assert module.btn_pass.isEnabled() is False
    assert module.btn_fail.isEnabled() is False

    assert intents == []

    services.config_service._details[101]["fault_types"] = ["new"]
    next_state = FactoryControllerState(
        selected_config_ids=(101,),
        run_phase="running",
        selection_ready=True,
        start_ready=True,
    )
    services.bus.factory_state_changed.emit(next_state)

    assert [button.text() for button in module.manual_fault_buttons] == ["new"]


def test_manual_fault_selection_clears_on_target_change_success_or_shutdown(qapp):
    module, _ = _make_module()
    target = _manual_target()
    run_two = "00000000-0000-4000-8000-000000000002"
    module.set_manual_judgment_target(target)
    module.manual_fault_buttons[0].click()

    module.set_manual_judgment_target(target)
    assert module.manual_fault_buttons[0].isChecked() is True

    module.set_manual_judgment_target(_manual_target(run_two))
    assert module.manual_fault_button_group.checkedButton() is None
    module.manual_fault_buttons[0].click()

    module.set_manual_judgment_target(_manual_target(run_two, revision=2))
    assert module.manual_fault_button_group.checkedButton() is None
    module.manual_fault_buttons[0].click()

    module.set_manual_judgment_target(
        _manual_target(run_two, revision=2, fault_types=("松动", "异响"))
    )
    assert [button.text() for button in module.manual_fault_buttons] == [
        "松动",
        "异响",
    ]
    assert module.manual_fault_button_group.checkedButton() is None
    module.manual_fault_buttons[1].click()

    module.set_manual_judgment_target(
        _manual_target(
            run_two,
            revision=2,
            fault_types=("松动", "异响"),
            allowed=False,
        )
    )
    assert module.manual_fault_button_group.checkedButton() is None
    module.set_manual_judgment_target(
        _manual_target(
            run_two,
            revision=2,
            fault_types=("松动", "异响"),
        )
    )
    module.manual_fault_buttons[1].click()
    module.btn_fail.click()

    module.handle_manual_judgment_result(
        OperationResult(
            run_id=run_two,
            status=OperationStatus.PERSISTENCE_REJECTED,
            attempt_id=None,
            reason="persistence rejected",
        )
    )
    assert module.manual_fault_buttons[1].isChecked() is True

    module.handle_manual_judgment_result(
        OperationResult(
            run_id=run_two,
            status=OperationStatus.COMPLETED,
            attempt_id=None,
            reason="",
        )
    )
    assert module.manual_fault_button_group.checkedButton() is None
    module.manual_fault_buttons[0].click()

    module.shutdown()
    module.shutdown()

    assert module.manual_fault_buttons == ()
    assert module.btn_pass.isEnabled() is False
    assert module.btn_fail.isEnabled() is False


def test_late_success_for_prior_key_never_clears_current_selection(qapp):
    module, _ = _make_module()
    first_run = "00000000-0000-4000-8000-000000000001"
    second_run = "00000000-0000-4000-8000-000000000002"
    module.set_manual_judgment_target(_manual_target(first_run))
    module.manual_fault_buttons[0].click()
    module.btn_fail.click()

    module.set_manual_judgment_target(_manual_target(second_run))
    module.manual_fault_buttons[1].click()
    module.handle_manual_judgment_result(
        OperationResult(
            run_id=first_run,
            status=OperationStatus.COMPLETED,
            attempt_id=None,
            reason="",
        )
    )

    assert module.manual_fault_buttons[1].isChecked() is True


def test_manual_not_found_emits_one_chinese_status_and_keeps_different_target(qapp):
    module, services = _make_module()
    first_run = "00000000-0000-4000-8000-000000000001"
    second_run = "00000000-0000-4000-8000-000000000002"
    module.set_manual_judgment_target(_manual_target(second_run))
    module.manual_fault_buttons[1].click()

    module.handle_manual_judgment_result(
        OperationResult(
            run_id=first_run,
            status=OperationStatus.NOT_FOUND,
            attempt_id=None,
            reason="result-control facts were not found",
        )
    )

    assert services.bus.status.emissions == [
        (("人工判定失败：所选运行记录不存在，可能已被删除",), {})
    ]
    assert module.manual_fault_buttons[1].isChecked() is True


def test_manual_fault_controls_disable_without_capability_and_emit_nothing(qapp):
    module, services = _make_module()
    intents = []
    module.manual_judgment_intent.connect(
        lambda label, fault_type: intents.append((label, fault_type))
    )
    module.set_manual_judgment_target(_manual_target(allowed=False))

    assert [button.text() for button in module.manual_fault_buttons] == [
        "异响",
        "漏装",
    ]
    assert all(
        button.isEnabled() is False for button in module.manual_fault_buttons
    )
    assert module.btn_pass.isEnabled() is False
    assert module.btn_fail.isEnabled() is False

    module.manual_fault_buttons[0].click()
    module.btn_pass.click()
    module.btn_fail.click()

    assert intents == []
    services.factory_mode.apply_manual_judgment.assert_not_called()


def test_ok_always_submits_none_without_changing_fault_selection(qapp):
    module, _ = _make_module()
    intents = []
    module.manual_judgment_intent.connect(
        lambda label, fault_type: intents.append((label, fault_type))
    )
    module.set_manual_judgment_target(_manual_target())
    module.manual_fault_buttons[1].click()

    module.btn_pass.click()

    assert intents == [("NG", "漏装"), ("OK", None)]
    assert module.manual_fault_buttons[1].isChecked() is True


def test_empty_fault_list_submits_generic_ng_and_ok_always_submits_none(qapp):
    module, services = _make_module()
    intents = []
    module.manual_judgment_intent.connect(
        lambda label, fault_type: intents.append((label, fault_type))
    )
    module.set_manual_judgment_target(_manual_target(fault_types=()))

    assert module.manual_fault_buttons == ()
    assert module.btn_pass.isEnabled() is True
    assert module.btn_fail.isEnabled() is True
    assert not module.btn_pass.isHidden()
    assert not module.btn_fail.isHidden()
    assert module.btn_pass.text() == "√ 合格"
    assert module.btn_fail.text() == "× 不合格"

    module.btn_fail.click()
    module.btn_pass.click()

    assert intents == [("NG", None), ("OK", None)]
    services.factory_mode.apply_manual_judgment.assert_not_called()


def test_target_update_clears_rejection_retention_and_never_preselects_persisted_type(
    qapp,
):
    module, _ = _make_module()
    module.set_manual_judgment_target(_manual_target(revision=8))
    module.manual_fault_buttons[0].click()

    module.handle_manual_judgment_result(
        OperationResult(
            run_id="00000000-0000-4000-8000-000000000001",
            status=OperationStatus.INVALID_REQUEST,
            attempt_id=None,
            reason="validation rejected",
        )
    )
    assert module.manual_fault_buttons[0].isChecked() is True

    module.set_manual_judgment_target(_manual_target(revision=9))

    assert module.manual_fault_button_group.checkedButton() is None
    assert [button.isChecked() for button in module.manual_fault_buttons] == [
        False,
        False,
    ]

    module.set_manual_judgment_target(None)
    assert module.manual_fault_buttons == ()
    assert module.btn_pass.isEnabled() is False
    assert module.btn_fail.isEnabled() is False


def test_start_uses_live_workorder_payload_from_current_workorder_module(qapp):
    workorder_module, control_module, services = _make_shared_modules()

    result = services.factory_mode.confirm_selection((101, 202))

    control_module.btn_start.click()

    assert result.status == "applied"
    services.factory_mode.start_manual.assert_called_once_with(
        {
            "sn": "",
            "model": "",
            "selected_config_ids": (101, 202),
        }
    )


@pytest.mark.parametrize(
    "state",
    [
        _idle_blocked_state("配置已失效"),
        _idle_blocked_state("配置加载失败"),
        _idle_blocked_state("请至少启用一个配置", selected_config_ids=()),
        _idle_blocked_state("analysis window invalid"),
    ],
)
def test_start_button_stays_enabled_for_idle_blocking_reasons_and_click_attempts_manual_start(
    qapp,
    state,
):
    module, services = _make_module(initial_state=state)

    assert module.btn_start.isEnabled() is True
    assert hasattr(module, "lbl_start_reason") is False

    module.btn_start.click()

    services.factory_mode.start_manual.assert_called_once()


@pytest.mark.parametrize(
    "state",
    [
        FactoryControllerState(selected_config_ids=(101,), run_phase="running"),
        FactoryControllerState(selected_config_ids=(101,), run_phase="analyzing"),
        FactoryControllerState(selected_config_ids=(101,), run_phase="reanalyzing"),
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            selection_ready=True,
            start_ready=False,
            start_ready_reason="启动请求处理中",
        ),
    ],
)
def test_start_button_is_disabled_only_for_busy_or_pending_states(qapp, state):
    module, _ = _make_module(initial_state=state)

    assert module.btn_start.isEnabled() is False
    assert hasattr(module, "lbl_start_reason") is False


def test_configuration_restart_required_disables_start_and_import(qapp):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(202,),
            run_phase="not_ready",
            selection_ready=True,
            start_ready=False,
            start_ready_reason="配置已变更，请重启软件后生效。",
        )
    )

    assert module.btn_start.isEnabled() is False
    assert module.btn_import_audio.isEnabled() is False


def test_production_unavailable_reason_is_shown_once_and_disables_only_ingress(
    qapp,
):
    reason = "生产功能不可用，请修复设备或配置后重启软件。"
    module, services = _make_module(initial_state=_ready_state())

    assert module.btn_start.isEnabled() is True
    assert module.lbl_production_unavailable_reason.text() == ""

    services.production_unavailable_reason = reason
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="not_ready",
            selection_ready=True,
            start_ready=False,
            start_ready_reason=reason,
        )
    )

    assert module.btn_start.isEnabled() is False
    assert module.btn_import_audio.isEnabled() is False
    assert module.btn_stop.isEnabled() is False
    assert module.btn_restart.isEnabled() is True
    assert module.lbl_production_unavailable_reason.text() == reason
    assert module.lbl_production_unavailable_reason.isVisible() is False
    module.show()
    qapp.processEvents()
    assert module.lbl_production_unavailable_reason.isVisible() is True

    module.btn_start.click()
    services.factory_mode.start_manual.assert_not_called()


def test_start_button_is_disabled_for_review_cleanup_busy_state(qapp):
    module, services = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="reviewing",
            selection_ready=True,
            start_ready=False,
            start_ready_reason="当前测试运行中",
        )
    )

    assert module.btn_start.isEnabled() is False
    assert hasattr(module, "lbl_start_reason") is False

    module.btn_start.click()

    services.factory_mode.start_manual.assert_not_called()


def test_start_button_is_disabled_for_controller_owned_busy_reason_in_armed_state(qapp):
    module, services = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            selection_ready=True,
            start_ready=False,
            start_ready_reason="当前测试运行中",
        )
    )

    assert module.btn_start.isEnabled() is False
    assert hasattr(module, "lbl_start_reason") is False

    module.btn_start.click()

    services.factory_mode.start_manual.assert_not_called()


def test_control_start_button_remains_enabled_in_stopped_state(qapp):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="stopped",
            selection_ready=True,
            start_ready=True,
        )
    )

    assert module.btn_start.isEnabled() is True


def test_control_stop_button_is_disabled_in_stopped_state(qapp):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="stopped",
        )
    )

    assert module.btn_stop.isEnabled() is False


def test_blocked_start_signal_opens_and_updates_single_modal(qapp, monkeypatch):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_state_changed.emit(_idle_blocked_state("配置已失效"))
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置已失效", trigger_source="manual")
    )
    module._open_pending_blocked_start_dialog()
    services.bus.factory_state_changed.emit(_idle_blocked_state("配置加载失败"))
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置加载失败", trigger_source="plc")
    )

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
    assert recorder.latest_title == "自动开始已阻止"
    assert hasattr(module, "lbl_start_reason") is False


@pytest.mark.parametrize("reason", ["当前测试运行中", "启动请求处理中"])
def test_busy_blocked_start_reasons_do_not_open_modal(qapp, monkeypatch, reason):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason=reason, trigger_source="manual")
    )

    assert recorder.open_count == 0
    assert recorder.latest_text == ""
    assert module._blocked_start_dialog is None


@pytest.mark.parametrize("reason", ["当前测试运行中", "启动请求处理中"])
def test_busy_blocked_start_reason_does_not_replace_existing_failure_modal(
    qapp,
    monkeypatch,
    reason,
):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置加载失败", trigger_source="manual")
    )
    module._open_pending_blocked_start_dialog()
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason=reason, trigger_source="manual")
    )

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
    assert module._blocked_start_dialog is not None


@pytest.mark.parametrize(
    "next_state",
    [
        _ready_state(),
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            selection_ready=True,
            start_ready=False,
            start_ready_reason="启动请求处理中",
        ),
        FactoryControllerState(selected_config_ids=(101,), run_phase="running"),
    ],
)
def test_transient_blocked_start_cleared_before_delay_does_not_open_modal(
    qapp,
    monkeypatch,
    next_state,
):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_state_changed.emit(_idle_blocked_state("配置加载失败"))
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置加载失败", trigger_source="manual")
    )

    assert recorder.open_count == 0
    assert module._pending_blocked_start_reason == "配置加载失败"

    services.bus.factory_state_changed.emit(next_state)
    module._open_pending_blocked_start_dialog()

    assert recorder.open_count == 0
    assert module._blocked_start_dialog is None


def test_blocked_start_signal_does_not_require_inline_reason_without_state_change(
    qapp,
    monkeypatch,
):
    module, services, recorder = _make_module_with_message_box_spy(
        monkeypatch,
        initial_state=FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            selection_ready=True,
            start_ready=True,
        ),
    )

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="请至少启用一个配置", trigger_source="manual")
    )
    module._open_pending_blocked_start_dialog()

    assert recorder.open_count == 1
    assert recorder.latest_text == "请至少启用一个配置"
    assert hasattr(module, "lbl_start_reason") is False


def test_blocked_modal_closes_after_reason_clears(qapp, monkeypatch):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置已失效", trigger_source="manual")
    )
    module._open_pending_blocked_start_dialog()
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101,),
            run_phase="armed",
            selection_ready=True,
            start_ready=True,
        )
    )

    assert recorder.open_count == 1
    assert recorder.was_closed is True
    assert recorder.close_count == 1


def test_open_blocked_modal_updates_when_background_state_refresh_changes_reason(
    qapp,
    monkeypatch,
):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置已失效", trigger_source="manual")
    )
    module._open_pending_blocked_start_dialog()
    services.bus.factory_state_changed.emit(_idle_blocked_state("配置加载失败"))

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
    assert hasattr(module, "lbl_start_reason") is False


@pytest.mark.parametrize("trigger_source", ["plc", "scan"])
def test_blocked_external_trigger_signal_uses_same_modal_flow(qapp, monkeypatch, trigger_source):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置加载失败", trigger_source=trigger_source)
    )
    module._open_pending_blocked_start_dialog()

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
