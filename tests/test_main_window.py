from __future__ import annotations

import dataclasses
import gc
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.modules.setdefault("cv2", SimpleNamespace())
if "snap7" not in sys.modules:
    snap7_module = ModuleType("snap7")
    snap7_client_module = ModuleType("snap7.client")
    snap7_util_module = ModuleType("snap7.util")

    class _Client:
        def connect(self, *_args, **_kwargs):
            return None

        def disconnect(self):
            return None

    snap7_client_module.Client = _Client
    snap7_util_module.get_bool = lambda *_args, **_kwargs: False
    snap7_util_module.set_bool = lambda *_args, **_kwargs: None
    snap7_module.client = snap7_client_module
    snap7_module.util = snap7_util_module
    sys.modules["snap7"] = snap7_module
    sys.modules["snap7.client"] = snap7_client_module
    sys.modules["snap7.util"] = snap7_util_module

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

try:
    from pyqtgraph.widgets.GraphicsView import GraphicsView
except Exception:  # pragma: no cover - pyqtgraph import is environment-dependent
    GraphicsView = None
else:
    _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT = GraphicsView.paintEvent
    _NOOP_GRAPHICS_VIEW_PAINT_EVENT = lambda self, event: None

from app.service.analysis.frames import StftFrame
from app.service.result_dashboard_layout import DEFAULT_RESULT_DASHBOARD_LAYOUT
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
import app.main_window as main_window_mod
from consts.style import APP_QSS
from ui.widgets.cards import Card


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
        self.status = _SignalStub()
        self.log = _SignalStub()
        self.workorder_changed = _SignalStub()
        self.result_changed = _SignalStub()
        self.test_finished = _SignalStub()
        self.stft_updated = _SignalStub()
        self.fft_updated = _SignalStub()
        self.spl_updated = _SignalStub()
        self.loudness_updated = _SignalStub()
        self.octave_updated = _SignalStub()
        self.factory_start_blocked = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_channel_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()


class _StubModule(QWidget):
    def __init__(self, services=None):
        super().__init__()
        self.services = services


def _make_guarded_module(confirm_leave_result: bool):
    class _GuardedModule(_StubModule):
        def __init__(self, services=None):
            super().__init__(services)
            self.confirm_leave_calls = 0

        def confirm_leave(self) -> bool:
            self.confirm_leave_calls += 1
            return confirm_leave_result

    return _GuardedModule


def _make_guarded_ai_tool_management_module(confirm_leave_result: bool):
    return _make_guarded_module(confirm_leave_result)


class _ChannelProbeModule(_StubModule):
    def __init__(self, services=None):
        super().__init__(services)
        self.title_changed = _SignalStub()
        self.score_title_changed = _SignalStub()
        self.selected_display_channel_provider = None
        self.state_seen_channels = []
        self.review_seen_channels = []

        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)

    def _record_current_channel(self, bucket: list[int | None]) -> None:
        provider = self.selected_display_channel_provider
        bucket.append(provider() if callable(provider) else None)

    def _on_factory_state_changed(self, _state) -> None:
        self._record_current_channel(self.state_seen_channels)

    def _on_factory_review_changed(self, _review) -> None:
        self._record_current_channel(self.review_seen_channels)

    def current_title(self) -> str:
        return "响度"

    def current_score_title(self) -> str:
        return "得分：--"

class _RefreshProbeResultModule(_StubModule):
    def __init__(self, services=None):
        super().__init__(services)
        self.selected_display_channel_provider = None
        self.refresh_selected_display_channel_calls = 0

    def refresh_selected_display_channel(self) -> None:
        self.refresh_selected_display_channel_calls += 1

class _FakeLoginWindow:
    def __init__(
        self,
        *,
        on_login_success=None,
        on_login_cancel=None,
        window_title="登录",
    ):
        self.on_login_success = on_login_success
        self.on_login_cancel = on_login_cancel
        self.window_title = window_title
        self.shown = False

    def show(self):
        self.shown = True

    def trigger_success(self, username: str, role: str) -> None:
        if callable(self.on_login_success):
            self.on_login_success(username, role)

    def trigger_cancel(self) -> None:
        if callable(self.on_login_cancel):
            self.on_login_cancel()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _is_analysis_chart_widget(widget: QWidget) -> bool:
    return type(widget).__module__ == "ui.widgets.analysis_charts"


def _disable_updates_and_remove_events(widget: QWidget) -> None:
    widget.setUpdatesEnabled(False)
    widgets = [widget]
    try:
        widgets.extend(widget.findChildren(QWidget))
    except RuntimeError:
        pass
    for target in widgets:
        if not sip.isdeleted(target):
            target.setUpdatesEnabled(False)
            QCoreApplication.removePostedEvents(target)


def _close_plot_widget(chart_widget: QWidget) -> None:
    plot_widget = getattr(chart_widget, "plot_widget", None)
    if plot_widget is None or sip.isdeleted(plot_widget):
        return
    _disable_updates_and_remove_events(plot_widget)
    if getattr(plot_widget, "plotItem", None) is None:
        return
    try:
        plot_widget.close()
    except AttributeError:
        pass


@pytest.fixture(autouse=True)
def _close_top_level_widgets(qapp):
    if GraphicsView is not None:
        GraphicsView.paintEvent = _NOOP_GRAPHICS_VIEW_PAINT_EVENT
    yield
    chart_widgets = []
    for widget in list(qapp.topLevelWidgets()):
        if sip.isdeleted(widget):
            continue
        for child in widget.findChildren(QWidget):
            if _is_analysis_chart_widget(child):
                chart_widgets.append(child)
                _disable_updates_and_remove_events(child)
                _close_plot_widget(child)
        widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        try:
            widget.close()
        except AttributeError:
            pass
        if sip.isdeleted(widget):
            continue
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for widget in chart_widgets:
        if not sip.isdeleted(widget):
            QCoreApplication.removePostedEvents(widget)
    for _ in range(5):
        qapp.processEvents()
    gc.collect()
    for _ in range(5):
        qapp.processEvents()
    if GraphicsView is not None:
        GraphicsView.paintEvent = _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT


def _patch_non_target_modules(monkeypatch, *, use_real_control: bool = False):
    module_names = [
        "WorkOrderModule",
        "VideoModule",
        "TodayStatisticsModule",
        "HardwareModule",
        "InputCalibrationModule",
        "ConfigModule",
        "AiToolManagementModule",
        "ChannelSelectModule",
        "PlcSettingsModule",
        "DataManagementModule",
    ]
    if not use_real_control:
        module_names.append("ControlModule")
    for name in module_names:
        monkeypatch.setattr(main_window_mod, name, _StubModule, raising=False)


def _make_services(
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
):
    bus = _BusStub()
    shutdown = MagicMock()
    factory_mode = SimpleNamespace(
        state=initial_state or FactoryControllerState(),
        review_payload=initial_review_payload or FactoryReviewPayload(),
        begin_reanalysis=MagicMock(return_value=True),
        start_manual=MagicMock(),
        stop_active_run=MagicMock(),
    )
    return SimpleNamespace(
        bus=bus,
        shutdown=shutdown,
        db=SimpleNamespace(close=MagicMock()),
        config_service=SimpleNamespace(
            load_config=MagicMock(return_value={"selected_channels": []}),
            load_config_for_result_dashboard=MagicMock(
                return_value={
                    "config_id": 101,
                    "selected_channels": [0],
                    "vibration_view_channel_index": None,
                    "result_dashboard_layout": DEFAULT_RESULT_DASHBOARD_LAYOUT,
                }
            ),
        ),
        factory_mode=factory_mode,
    )


def _make_window(
    monkeypatch,
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
    use_real_control: bool = False,
    config_cls: type[QWidget] | None = None,
    ai_tool_management_cls: type[QWidget] | None = None,
):
    _patch_non_target_modules(monkeypatch, use_real_control=use_real_control)
    if config_cls is not None:
        monkeypatch.setattr(
            main_window_mod,
            "ConfigModule",
            config_cls,
            raising=False,
        )
    if ai_tool_management_cls is not None:
        monkeypatch.setattr(
            main_window_mod,
            "AiToolManagementModule",
            ai_tool_management_cls,
            raising=False,
        )
    services = _make_services(
        initial_state=initial_state,
        initial_review_payload=initial_review_payload,
    )
    return main_window_mod.MainWindow(services), services


def _show(window, qapp):
    window.resize(1440, 900)
    window.show()
    qapp.processEvents()


def test_show_change_password_window_when_logged_out_updates_visible_status_via_log(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)

    assert window.statusBar().currentMessage() == "[App] 启动程序"
    assert window.change_password_window is None

    window.show_change_password_window()
    qapp.processEvents()

    assert window.change_password_window is None
    assert services.bus.log.emissions[-1][0] == ("请先登录",)
    assert window.statusBar().currentMessage() == "请先登录"


def test_main_window_includes_ai_tool_management_tab_in_topbar(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    assert window.tab_names == ["测试", "数据管理", "配置", "AI管理", "硬件"]
    assert list(window.top_button_dict) == ["测试", "数据管理", "配置", "AI管理", "硬件"]


def test_engineer_role_can_access_ai_tool_management_tab(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "工程师"
    window.set_role()

    assert window.top_button_dict["AI管理"].isEnabled() is True


def test_admin_role_can_access_ai_tool_management_tab(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "管理员"
    window.set_role()

    assert window.top_button_dict["AI管理"].isEnabled() is True


def test_operator_role_cannot_access_ai_tool_management_tab(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "操作员"
    window.set_role()

    assert window.top_button_dict["AI管理"].isEnabled() is False


def test_no_role_programmatic_page_reentry_remains_allowed(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.tab.setCurrentIndex(1)
    qapp.processEvents()

    assert window.tab.currentIndex() == 1
    assert window.top_button_dict["数据管理"].isChecked() is True

    window.tab.setCurrentIndex(0)
    qapp.processEvents()

    assert window.tab.currentIndex() == 0
    assert window.top_button_dict["测试"].isChecked() is True


def test_operator_cannot_activate_ai_tool_management_via_programmatic_tab_change(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "操作员"
    window.set_role()

    ai_index = window.tab_names.index("AI管理")

    window.tab.setCurrentIndex(ai_index)
    qapp.processEvents()

    assert window.tab.currentIndex() == 0
    assert window.top_button_dict["测试"].isChecked() is True
    assert window.top_button_dict["AI管理"].isChecked() is False


def test_switching_away_from_ai_tool_management_is_blocked_when_confirm_leave_rejects(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(
        monkeypatch,
        ai_tool_management_cls=_make_guarded_ai_tool_management_module(False),
    )
    _show(window, qapp)

    window.current_user_role = "管理员"
    window.set_role()

    ai_index = window.tab_names.index("AI管理")
    window.top_button_dict["AI管理"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index

    window.top_button_dict["硬件"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index
    assert window.ai_tool_management_module.confirm_leave_calls == 1


def test_programmatic_tab_change_reverts_when_ai_tool_management_rejects_leave(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(
        monkeypatch,
        ai_tool_management_cls=_make_guarded_ai_tool_management_module(False),
    )
    _show(window, qapp)

    window.current_user_role = "管理员"
    window.set_role()

    ai_index = window.tab_names.index("AI管理")
    hardware_index = window.tab_names.index("硬件")

    window.top_button_dict["AI管理"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index

    window.tab.setCurrentIndex(hardware_index)
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index
    assert window.top_button_dict["AI管理"].isChecked() is True
    assert window.top_button_dict["硬件"].isChecked() is False
    assert window.ai_tool_management_module.confirm_leave_calls == 1


def test_switching_away_from_config_is_blocked_when_confirm_leave_rejects(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(
        monkeypatch,
        config_cls=_make_guarded_module(False),
    )
    _show(window, qapp)

    window.current_user_role = "管理员"
    window.set_role()

    config_index = window.tab_names.index("配置")
    window.top_button_dict["配置"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == config_index

    window.top_button_dict["硬件"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == config_index
    assert window.config_module.confirm_leave_calls == 1


def test_switching_away_from_config_is_allowed_when_confirm_leave_accepts(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(
        monkeypatch,
        config_cls=_make_guarded_module(True),
    )
    _show(window, qapp)

    window.current_user_role = "管理员"
    window.set_role()

    hardware_index = window.tab_names.index("硬件")
    window.top_button_dict["配置"].click()
    qapp.processEvents()

    window.top_button_dict["硬件"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == hardware_index
    assert window.top_button_dict["配置"].isChecked() is False
    assert window.top_button_dict["硬件"].isChecked() is True
    assert window.config_module.confirm_leave_calls == 1


def test_role_switch_from_admin_to_operator_stays_consistent_when_ai_leave_is_rejected(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(
        monkeypatch,
        ai_tool_management_cls=_make_guarded_ai_tool_management_module(False),
    )
    _show(window, qapp)

    window.current_user_name = "admin"
    window.current_user_role = "管理员"
    window.set_role()

    ai_index = window.tab_names.index("AI管理")
    window.top_button_dict["AI管理"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index

    window.current_user_name = "operator"
    window.current_user_role = "操作员"
    window.set_role()
    qapp.processEvents()

    assert window.current_user_name == "admin"
    assert window.current_user_role == "管理员"
    assert window.tab.currentIndex() == ai_index
    assert window.top_button_dict["AI管理"].isEnabled() is True
    assert window.top_button_dict["AI管理"].isChecked() is True
    assert window.top_button_dict["测试"].isChecked() is False
    assert window.ai_tool_management_module.confirm_leave_calls == 1


def test_closing_main_window_is_blocked_when_ai_tool_management_rejects_leave(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        ai_tool_management_cls=_make_guarded_ai_tool_management_module(False),
    )
    _show(window, qapp)
    window.current_user_role = "管理员"
    window.set_role()
    window.top_button_dict["AI管理"].click()
    qapp.processEvents()

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert close_event.isAccepted() is False
    services.shutdown.assert_not_called()
    services.db.close.assert_not_called()
    assert window.ai_tool_management_module.confirm_leave_calls == 1


def test_closing_main_window_is_blocked_when_config_rejects_leave(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        config_cls=_make_guarded_module(False),
    )
    _show(window, qapp)
    window.current_user_role = "管理员"
    window.set_role()
    window.top_button_dict["配置"].click()
    qapp.processEvents()

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert close_event.isAccepted() is False
    services.shutdown.assert_not_called()
    services.db.close.assert_not_called()
    assert window.config_module.confirm_leave_calls == 1


@pytest.mark.parametrize("role", ["管理员", "工程师"])
def test_admin_and_engineer_roles_can_use_plc_settings_card(qapp, monkeypatch, role):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_name = "user"
    window.current_user_role = role
    window.set_role()

    assert window.plc_settings_card.isEnabled() is True
    assert window.plc_settings_card.toolTip() == ""


def test_closing_main_window_stops_plc_cleanup_before_shared_shutdown_and_camera(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)

    cleanup_steps = []
    plc_service = object()
    plc_thread = MagicMock()
    plc_thread.quit.side_effect = lambda: cleanup_steps.append("thread_quit")
    plc_thread.wait.side_effect = (
        lambda timeout: cleanup_steps.append(("thread_wait", timeout))
    )
    services.plc_services = {101: plc_service}
    services.plc_threads = {101: plc_thread}
    services.shutdown.side_effect = lambda: cleanup_steps.append("shutdown")
    services.db.close = MagicMock()
    window.video.stop_camera = MagicMock(side_effect=lambda: cleanup_steps.append("camera"))

    monkeypatch.setattr(
        main_window_mod,
        "Qt",
        SimpleNamespace(
            ConnectionType=SimpleNamespace(BlockingQueuedConnection="blocking")
        ),
        raising=False,
    )

    def _invoke_method(target, method_name, connection_type):
        cleanup_steps.append(("plc_stop", target, method_name, connection_type))
        return True

    monkeypatch.setattr(
        main_window_mod,
        "QMetaObject",
        SimpleNamespace(invokeMethod=_invoke_method),
        raising=False,
    )

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert cleanup_steps == [
        ("plc_stop", plc_service, "stop_plc_service", "blocking"),
        "thread_quit",
        ("thread_wait", 2000),
        "shutdown",
        "camera",
    ]
    services.db.close.assert_not_called()


def test_closing_main_window_uses_shared_shutdown_boundary_and_stops_camera(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)
    window.video.stop_camera = MagicMock()

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    services.shutdown.assert_called_once_with()
    services.db.close.assert_not_called()
    window.video.stop_camera.assert_called_once_with()


def test_closing_main_window_still_uses_shared_shutdown_and_camera_when_plc_cleanup_raises(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)

    cleanup_steps = []
    services.plc_services = {101: object()}
    services.plc_threads = {}
    services.shutdown.side_effect = lambda: cleanup_steps.append("shutdown")
    services.db.close = MagicMock()
    window.video.stop_camera = MagicMock(side_effect=lambda: cleanup_steps.append("camera"))

    monkeypatch.setattr(
        main_window_mod,
        "Qt",
        SimpleNamespace(
            ConnectionType=SimpleNamespace(BlockingQueuedConnection="blocking")
        ),
        raising=False,
    )

    def _failing_invoke_method(*_args, **_kwargs):
        cleanup_steps.append("plc_stop")
        raise RuntimeError("plc cleanup failed")

    monkeypatch.setattr(
        main_window_mod,
        "QMetaObject",
        SimpleNamespace(invokeMethod=_failing_invoke_method),
        raising=False,
    )

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert cleanup_steps == ["plc_stop", "shutdown", "camera"]
    services.shutdown.assert_called_once_with()
    services.db.close.assert_not_called()
    window.video.stop_camera.assert_called_once_with()


def test_closing_main_window_swallows_camera_cleanup_errors_after_shared_shutdown(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)
    window.video.stop_camera = MagicMock(side_effect=RuntimeError("camera cleanup failed"))

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    services.shutdown.assert_called_once_with()
    services.db.close.assert_not_called()
    window.video.stop_camera.assert_called_once_with()


def test_closing_main_window_still_stops_camera_when_shared_shutdown_raises(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)
    services.shutdown.side_effect = RuntimeError("shutdown failed")
    window.video.stop_camera = MagicMock()

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    services.shutdown.assert_called_once_with()
    services.db.close.assert_not_called()
    window.video.stop_camera.assert_called_once_with()


def _armed_state(
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        run_phase="armed",
    )


def _running_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        active_config_id=config_id,
        displayed_config_id=config_id,
        run_phase="running",
        trigger_source="manual",
    )


def _reviewing_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        displayed_config_id=config_id,
        run_phase="reviewing",
        trigger_source="manual",
    )


def _stopped_state(
    config_id: int,
    *,
    selected_config_ids: tuple[int, ...] = (101, 202),
) -> FactoryControllerState:
    return FactoryControllerState(
        selected_config_ids=selected_config_ids,
        displayed_config_id=config_id,
        run_phase="stopped",
        trigger_source="manual",
    )


def _review_payload(config_id: int, *, run_id: str | None = None) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id or f"run-{config_id}",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=False,
        error_message=None,
        judgment_payload={
            "overall": "合格",
            "ai_label": "Normal",
            "ai_score": 0.93,
            "ai_summary": "stable waveform",
            "ai_status": "success",
            "toolchain_name": "Toolchain",
            "toolchain_version": "1.0",
            "error_summary": None,
            "artifacts_json": ["artifact.json"],
            "spl_db": 82.4,
        },
    )


def _chart_payload_for_channels(
    channels: tuple[int, ...],
    *,
    missing_loudness_channels: tuple[int, ...] = (),
) -> dict:
    payload: dict[int, dict] = {}
    for channel_index in channels:
        channel_payload = {
            "spl": {
                "times_sec": [0.0, 1.0],
                "values_db": [70.0 + channel_index, 71.0 + channel_index],
                "lower_limit_db": 60.0,
                "upper_limit_db": 80.0,
            },
            "fft": {
                "frequencies_hz": [125.0, 250.0],
                "levels_db": [40.0 + channel_index, 42.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 35.0,
                        "upper_db": 45.0,
                        "actual_db": 42.0 + channel_index,
                        "passed": True,
                    }
                ],
                "golden_mode": "off",
                "golden_overlay_levels_db": None,
                "show_zero_reference": False,
            },
            "octave": {
                "center_freqs_hz": [125.0, 250.0],
                "levels_db": [32.0 + channel_index, 34.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 30.0,
                        "upper_db": 40.0,
                        "actual_db": 34.0 + channel_index,
                        "passed": True,
                    }
                ],
                "golden_mode": "off",
                "golden_overlay_levels_db": None,
                "show_zero_reference": False,
            },
        }
        if channel_index not in missing_loudness_channels:
            channel_payload["loudness"] = {
                "times_sec": [0.0, 1.0],
                "values": [10.0 + channel_index, 12.0 + channel_index],
                "unit": "sone",
                "lower_limit": 0.0,
                "upper_limit": 60.0,
            }
        payload[channel_index] = channel_payload
    return payload


def _review_payload_with_chart_payload(
    config_id: int,
    *,
    run_id: str | None = None,
    channels: tuple[int, ...] = (0, 1),
    missing_loudness_channels: tuple[int, ...] = (),
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id or f"run-{config_id}",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=False,
        error_message=None,
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload=_chart_payload_for_channels(
            channels,
            missing_loudness_channels=missing_loudness_channels,
        ),
    )


def _review_payload_with_vibration(
    config_id: int = 101,
    *,
    vibration_channel: int = 1,
) -> FactoryReviewPayload:
    payload = _review_payload_with_chart_payload(config_id, channels=(0, 1))
    payload.chart_payload[vibration_channel]["vibration"] = {
        "channel_index": vibration_channel,
        "sample_rate": 1000.0,
        "overall_rms": 1.0,
        "overall_peak": 2.0,
        "overall_crest_factor": 2.0,
        "dominant_frequency_hz": 50.0,
        "times_sec": [0.05, 0.10],
        "rms_values": [0.8, 1.0],
        "peak_values": [1.5, 2.0],
        "crest_factors": [1.8, 2.0],
        "calibration_value": 1.0,
    }
    return dataclasses.replace(
        payload,
        config_payload={
            "config_id": config_id,
            "selected_channels": [0, 1],
            "vibration_view_channel_index": vibration_channel,
        },
    )


def _summary_payload(
    *,
    overall_status: str,
    error_message: str | None = None,
    rows: list[dict] | None = None,
) -> dict:
    overall_label = {
        "pass": "PASS",
        "fail": "FAIL",
        "no_result": "无结果",
    }[overall_status]
    return {
        "overall_status": overall_status,
        "overall_label": overall_label,
        "error_message": error_message,
        "rows": rows or [],
    }


def _partial_no_result_review_payload(
    config_id: int,
    *,
    run_id: str | None = None,
    channels: tuple[int, ...] = (0, 1),
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id or f"run-{config_id}",
        trigger_source="manual",
        captured_duration_sec=5.0,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=True,
        error_message="AI timeout",
        summary_payload=_summary_payload(
            overall_status="no_result",
            error_message="AI timeout",
            rows=[
                {
                    "item_key": "spl",
                    "label": "SPL",
                    "deviation_value": -1.0,
                    "deviation_display": "-1 dB",
                    "channel_index": 1,
                    "channel_label": "CH2",
                    "status": "OK",
                    "constrained": True,
                },
                {
                    "item_key": "loudness",
                    "label": "Loudness",
                    "deviation_value": -0.5,
                    "deviation_display": "-0.5 sone",
                    "channel_index": 1,
                    "channel_label": "CH2",
                    "status": "OK",
                    "constrained": True,
                },
                {
                    "item_key": "fft",
                    "label": "FFT",
                    "deviation_value": 2.0,
                    "deviation_display": "2 dB",
                    "channel_index": 0,
                    "channel_label": "CH1",
                    "status": "NG",
                    "constrained": True,
                },
                {
                    "item_key": "octave",
                    "label": "倍频程",
                    "deviation_value": -3.0,
                    "deviation_display": "-3 dB",
                    "channel_index": 1,
                    "channel_label": "CH2",
                    "status": "OK",
                    "constrained": True,
                },
                {
                    "item_key": "ai",
                    "label": "AI分析",
                    "deviation_value": None,
                    "deviation_display": "--",
                    "channel_index": None,
                    "channel_label": "--",
                    "status": "无结果",
                    "constrained": True,
                },
            ],
        ),
        chart_payload=_chart_payload_for_channels(channels),
    )


def _select_toolbar_channel(window, qapp, channel_index: int) -> None:
    combo = window.analysis_toolbar.channel_combo
    combo.setCurrentIndex(combo.findData(channel_index))
    qapp.processEvents()


def _toolbar_channel_options(window) -> list[int]:
    combo = window.analysis_toolbar.channel_combo
    return [combo.itemData(index) for index in range(combo.count())]


def _assert_run_specific_cards_cleared(window, *, result_badge: str = "-") -> None:
    assert window.result.lb_overall.text() == result_badge
    assert window.result.empty_state.isHidden() is True
    assert window.result.lb_empty_text.text() == ""
    assert window.stft.selection_box.isVisible() is False
    assert window.stft.analyze_button.isVisible() is False
    assert window.loudness.plot.isHidden() is False
    assert window.spectrum.plot.isHidden() is False
    assert window.spl.plot.isHidden() is False
    assert window.octave.plot.isHidden() is False
    assert window.loudness.has_committed_plot_data() is False
    assert window.spectrum.has_committed_plot_data() is False
    assert window.spl.has_committed_plot_data() is False
    assert window.octave.has_committed_plot_data() is False


def _assert_chart_cards_visible(window) -> None:
    assert window.loudness.plot.isHidden() is False
    assert window.spectrum.plot.isHidden() is False
    assert window.spl.plot.isHidden() is False
    assert window.octave.plot.isHidden() is False
    assert window.loudness.has_committed_plot_data() is True
    assert window.spectrum.has_committed_plot_data() is True
    assert window.spl.has_committed_plot_data() is True
    assert window.octave.has_committed_plot_data() is True


def _emit_runtime_started(services, *, config_id: int, run_id: str) -> None:
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=config_id,
            run_id=run_id,
            trigger_source="manual",
        )
    )
    services.bus.factory_state_changed.emit(_running_state(config_id))


def _emit_chart_frames(services, *, config_id: int, run_id: str) -> None:
    services.bus.fft_updated.emit(config_id, run_id, 0, object())
    services.bus.spl_updated.emit(config_id, run_id, 0, object())
    services.bus.octave_updated.emit(config_id, run_id, 0, object())


def _emit_stft_frames_for_review_channels(
    services,
    *,
    config_id: int,
    run_id: str,
    channels: tuple[int, ...],
) -> None:
    for channel_index in channels:
        for frame_index, time_sec in enumerate((0.0, 1.0, 2.0, 3.0)):
            magnitude_base = float((channel_index * 10) + (frame_index * 2) + 1)
            services.bus.stft_updated.emit(
                config_id,
                run_id,
                channel_index,
                _make_stft_frame(
                    time=time_sec,
                    magnitudes=(magnitude_base, magnitude_base + 1.0),
                ),
            )


def _emit_review_ready(
    services,
    *,
    config_id: int,
    run_id: str,
    channels: tuple[int, ...] = (0, 1),
) -> None:
    _emit_stft_frames_for_review_channels(
        services,
        config_id=config_id,
        run_id=run_id,
        channels=channels,
    )
    services.bus.factory_state_changed.emit(_reviewing_state(config_id))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            config_id,
            run_id=run_id,
            channels=channels,
        )
    )


def _assert_committed_chart_channel(window, *, channel_index: int) -> None:
    assert window.loudness.current_render_channel() == channel_index
    assert window.loudness.current_plot_payload()["values"] == [
        10.0 + channel_index,
        12.0 + channel_index,
    ]
    assert window.spl.current_render_channel() == channel_index
    assert window.spl.current_plot_payload()["y_values"] == [
        70.0 + channel_index,
        71.0 + channel_index,
    ]
    assert window.spectrum.current_render_channel() == channel_index
    assert window.spectrum.current_plot_payload()["levels_db"] == [
        40.0 + channel_index,
        42.0 + channel_index,
    ]
    assert window.spectrum.current_plot_payload()["golden_overlay_levels_db"] is None
    assert window.spectrum.current_plot_payload()["show_zero_reference"] is False
    assert window.octave.current_render_channel() == channel_index
    assert window.octave.current_plot_payload()["levels_db"] == [
        32.0 + channel_index,
        34.0 + channel_index,
    ]
    assert window.octave.current_plot_payload()["golden_overlay_levels_db"] is None
    assert window.octave.current_plot_payload()["show_zero_reference"] is False


def _result_table_value(window, row: int, column: int) -> str:
    item = window.result.table.item(row, column)
    assert item is not None
    return item.text()


def _card_title(card: QWidget) -> str:
    title = card.findChild(QLabel, "CardTitle")
    assert title is not None
    return title.text()


def _test_page(window) -> QWidget:
    return window.tab.widget(window._TEST_PAGE_INDEX)


def _test_page_left_slot_card(window, index: int) -> QWidget:
    page = _test_page(window)
    content_layout = page.layout()
    assert content_layout is not None
    left_column = content_layout.itemAt(0).widget()
    assert left_column is not None
    left_layout = left_column.layout()
    assert left_layout is not None
    card = left_layout.itemAt(index).widget()
    assert card is not None
    return card


def _test_page_grid_card(window, row: int, column: int) -> QWidget:
    page = _test_page(window)
    content_layout = page.layout()
    assert content_layout is not None
    main_panel = content_layout.itemAt(1).widget()
    assert main_panel is not None
    main_layout = main_panel.layout()
    assert main_layout is not None
    grid = main_layout.itemAt(0).layout()
    assert grid is not None
    item = grid.itemAtPosition(row, column)
    assert item is not None
    card = item.widget()
    assert card is not None
    return card


def _test_page_card_titles(window) -> list[str]:
    return [
        label.text()
        for label in _test_page(window).findChildren(QLabel, "CardTitle")
    ]


def _make_stft_frame(
    *,
    time: float,
    magnitudes: tuple[float, ...],
    frequencies: tuple[float, ...] = (100.0, 200.0),
) -> StftFrame:
    return StftFrame(
        time=time,
        frequencies=np.asarray(frequencies, dtype=float),
        magnitudes=np.asarray(magnitudes, dtype=float),
    )


def test_main_window_uses_result_dashboard_module_for_right_side(qapp, monkeypatch):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(initial_state=_reviewing_state(101))
    services.config_service.load_config_for_result_dashboard.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "result_dashboard_layout": {
            "version": 1,
            "columns": 1,
            "rows": 1,
            "panels": [
                {
                    "id": "summary",
                    "type": "result_summary",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "none",
                }
            ],
        },
    }

    window = main_window_mod.MainWindow(services)
    _show(window, qapp)

    assert hasattr(window, "result_dashboard")
    assert window.result_dashboard.module_id == "result_dashboard"
    assert list(window.result_dashboard.cards_by_panel_id()) == ["summary"]


def test_vibration_panel_titles_use_parentheses(qapp, monkeypatch):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(initial_state=_reviewing_state(101))
    services.config_service.load_config_for_result_dashboard.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
        "result_dashboard_layout": {
            "version": 1,
            "columns": 3,
            "rows": 1,
            "panels": [
                {
                    "id": "summary",
                    "type": "result_summary",
                    "row": 0,
                    "column": 0,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "none",
                },
                {
                    "id": "vib_stft",
                    "type": "vibration_stft",
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "vibration",
                },
                {
                    "id": "vib_octave",
                    "type": "vibration_octave",
                    "row": 0,
                    "column": 2,
                    "row_span": 1,
                    "column_span": 1,
                    "channel": "vibration",
                },
            ],
        },
    }

    window = main_window_mod.MainWindow(services)
    _show(window, qapp)

    assert [card.title() for card in window.result_dashboard.cards_by_panel_id().values()] == [
        "结果摘要",
        "时频图（振动）",
        "倍频程（振动）",
    ]


def test_main_window_keeps_fixed_left_column_modules_unchanged(qapp, monkeypatch):
    window, _services = _make_window(monkeypatch)
    _show(window, qapp)

    assert hasattr(window, "workorder")
    assert hasattr(window, "control")
    assert hasattr(window, "today_statistics")
    assert hasattr(window, "video")
    assert window.workorder.parentWidget() is not window.result_dashboard
    assert window.control.parentWidget() is not window.result_dashboard
    assert window.today_statistics.parentWidget() is not window.result_dashboard
    assert window.video.parentWidget() is not window.result_dashboard


def test_card_compact_dashboard_mode_reduces_layout_chrome(qapp):
    body = QWidget()
    card = Card("频谱", body)

    normal_margins = card.layout().contentsMargins()
    assert normal_margins.left() == 12
    assert normal_margins.top() == 10
    assert normal_margins.right() == 12
    assert normal_margins.bottom() == 12
    assert card.layout().spacing() == 8
    assert card.title_metric_value_label.minimumWidth() == 68
    assert card.title_metric_value_label.minimumHeight() == 26
    assert card.property("dashboardCompact") in (None, False)

    card.set_compact_dashboard_mode(True)

    compact_margins = card.layout().contentsMargins()
    assert compact_margins.left() == 4
    assert compact_margins.top() == 4
    assert compact_margins.right() == 4
    assert compact_margins.bottom() == 4
    assert card.layout().spacing() == 3
    assert card.title_metric_value_label.minimumWidth() <= 56
    assert card.title_metric_value_label.minimumHeight() <= 22
    assert card.property("dashboardCompact") is True


def test_card_title_suffix_does_not_overlap_title_at_narrow_width(qapp):
    body = QWidget()
    card = Card("时频图", body)
    card.set_title_metric("频谱最低得分：", "87.42")
    card.resize(220, 120)
    card.show()
    qapp.processEvents()

    assert card.title_label.geometry().right() < card.title_metric_widget.geometry().left()
    assert card.title_metric_widget.geometry().top() >= card.contentsRect().top()
    assert card.title_metric_widget.geometry().right() <= card.contentsRect().right()
    assert card.title_metric_widget.geometry().bottom() < body.geometry().top()
    assert card.title_metric_value_label.minimumWidth() == 68
    assert card.title_metric_value_label.minimumHeight() == 26


def test_card_title_metric_uses_score_value_box_style():
    label_style = APP_QSS.split("QLabel#CardTitleMetricLabel", 1)[1].split("}", 1)[0]
    value_style = APP_QSS.split("QLabel#CardTitleMetricValue", 1)[1].split("}", 1)[0]

    assert "font-size: 15px;" in label_style
    assert "font-size: 15px;" in value_style
    assert "border: 1px solid #cbd5e1;" in value_style


def test_dashboard_compact_card_qss_removes_card_chrome():
    style = APP_QSS.split('QFrame#Card[dashboardCompact="true"]', 1)[1].split(
        "}", 1
    )[0]
    metric_value_style = APP_QSS.split(
        'QFrame#Card[dashboardCompact="true"] QLabel#CardTitleMetricValue',
        1,
    )[1].split("}", 1)[0]

    assert "border: 0px solid transparent;" in style
    assert "border-radius: 0px;" in style
    assert "font-size: 12px;" in metric_value_style
    assert "padding: 1px 6px;" in metric_value_style


def test_card_empty_title_suffix_is_hidden_and_title_uses_row_width(qapp):
    body = QWidget()
    card = Card("时频图", body)
    card.resize(360, 120)
    card.show()
    qapp.processEvents()

    assert card.title_suffix() == ""
    assert card.title_suffix_label.isHidden()
    assert card.title_label.geometry().width() > body.geometry().width() * 0.70


def test_main_window_routes_latest_log_line_to_status_bar(qapp, monkeypatch):
    window, services = _make_window(monkeypatch)
    _show(window, qapp)

    assert window.statusBar().currentMessage() == "[App] 启动程序"

    window.current_user_name = "operator"
    window.current_user_role = "操作员"
    window.set_role()
    qapp.processEvents()

    assert window.statusBar().currentMessage() == "[App] 启动程序"

    services.bus.status.emit("Analyzing...")
    qapp.processEvents()

    assert window.statusBar().currentMessage() == "[App] 启动程序"

    services.bus.log.emit("line 1\n\n  line 2  \n")
    qapp.processEvents()

    assert window.statusBar().currentMessage() == "line 2"
