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
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

try:
    from pyqtgraph.widgets.GraphicsView import GraphicsView
except Exception:  # pragma: no cover - pyqtgraph import is environment-dependent
    GraphicsView = None
else:
    _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT = GraphicsView.paintEvent
    _NOOP_GRAPHICS_VIEW_PAINT_EVENT = lambda self, event: None

from app.service.analysis.frames import StftFrame
from app.service.multi_run_integration.shutdown import ApplicationShutdownReport
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
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




class _StubModule(QWidget):
    def __init__(self, services=None):
        super().__init__()
        self.services = services






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


def test_main_window_source_wires_dashboard_to_the_one_history_adapter():
    import inspect
    import app.main_window as main_window

    source = inspect.getsource(main_window.MainWindow)
    assert "result_history_store" in source
    assert "attach_result_dashboard" in source
    assert "self.control.manual_judgment_widget" in source


def test_main_window_close_uses_only_cached_application_shutdown_and_preserves_stop_text():
    import inspect
    import app.main_window as main_window

    source = inspect.getsource(main_window.MainWindow.closeEvent)
    class_source = inspect.getsource(main_window.MainWindow)

    assert "self.services.shutdown()" in source
    assert "plc_services" not in source
    assert "plc_threads" not in source
    assert "BlockingQueuedConnection" not in source
    assert 'setText("停止测试")' in class_source
    assert "self.video.stop_camera" in class_source
    assert source.index("self.control.shutdown()") < source.index(
        "self.services.shutdown()"
    )


def test_main_window_source_wires_only_run_scoped_result_operation_intents():
    import inspect
    import app.main_window as main_window

    source = inspect.getsource(main_window.MainWindow)

    assert "manual_judgment_target_changed.connect" in source
    assert "manual_judgment_intent.connect" in source
    assert "manual_judgment_requested.connect" in source
    assert "reanalysis_requested.connect" in source
    assert "reanalysis_cancel_requested.connect" in source
    assert "stop_intent.connect" in source
    assert "attach_committed_history_observer" in source
    assert "factory_mode.apply_manual_judgment" not in source
    assert "factory_review_changed" not in source
    assert "FactoryReviewPayload" not in source
    assert "_review_payload" not in source
    assert "displayed_config_id" not in source


def test_reanalysis_not_found_emits_one_chinese_status_without_retry():
    from unittest.mock import MagicMock

    from app.main_window import MainWindow
    from app.service.multi_run_coordinator_mvp import OperationResult, OperationStatus
    from app.service.result_history_contracts import ReanalysisRequest

    run_id = "00000000-0000-4000-8000-000000000001"
    coordinator = SimpleNamespace(
        start_reanalysis=MagicMock(
            return_value=OperationResult(
                run_id=run_id,
                status=OperationStatus.NOT_FOUND,
                attempt_id=None,
                reason="result-control facts were not found",
            )
        )
    )
    status = _SignalStub()
    window = SimpleNamespace(
        services=SimpleNamespace(
            multi_run_coordinator=coordinator,
            bus=SimpleNamespace(status=status),
        )
    )

    MainWindow._start_reanalysis_request(
        window,
        ReanalysisRequest(run_id, 7, 0.25, 1.75),
    )

    coordinator.start_reanalysis.assert_called_once()
    assert status.emissions == [
        (("重新分析失败：所选运行记录不存在，可能已被删除",), {})
    ]


def test_main_window_registers_process_plc_shutdown_after_module_construction():
    import inspect
    import app.main_window as main_window

    source = inspect.getsource(main_window.MainWindow.build_hardware_page)
    construction = source.index(
        "self.plc_settings_module = PlcSettingsModule(self.services)"
    )
    registration = source.index(
        "self.plc_settings_module.shutdown_process_param_service"
    )

    assert construction < registration
    assert "register_independent_shutdown_resource" in source


class _RestartControlStub:
    def __init__(self, order, shutdown_error=None):
        self._order = order
        self._shutdown_error = shutdown_error
        self.pending = []

    def set_restart_pending(self, pending):
        self.pending.append(pending)

    def shutdown(self):
        self._order.append("control")
        if self._shutdown_error is not None:
            raise self._shutdown_error


def _application_shutdown_report(*, clean=True):
    return ApplicationShutdownReport(
        clean=clean,
        restart_required=not clean,
        deadline_monotonic=1.0,
        stages=(),
        errors=(),
    )


_DEFAULT_SHUTDOWN_RESULT = object()


def _restart_window(
    *,
    can_leave=True,
    control_error=None,
    service_error=None,
    service_result=_DEFAULT_SHUTDOWN_RESULT,
):
    from app.main_window import MainWindow

    order = []
    if service_result is _DEFAULT_SHUTDOWN_RESULT:
        service_result = _application_shutdown_report()
    window = MainWindow.__new__(MainWindow)
    window._restart_in_progress = False
    window.tab = SimpleNamespace(currentIndex=lambda: 2)
    window._can_leave_main_tab = MagicMock(return_value=can_leave)
    window.control = _RestartControlStub(order, control_error)

    def shutdown():
        order.append("services")
        if service_error is not None:
            raise service_error
        return service_result

    window.services = SimpleNamespace(
        shutdown=MagicMock(side_effect=shutdown),
        bus=SimpleNamespace(log=_SignalStub()),
    )
    return window, order


def test_restart_cancel_clears_guard_and_reenables_control(monkeypatch):
    import app.main_window as main_window

    window, order = _restart_window(can_leave=False)
    detached = MagicMock(return_value=True)
    monkeypatch.setattr(main_window.QProcess, "startDetached", detached)

    window._restart_application()

    assert order == []
    detached.assert_not_called()
    assert window._restart_in_progress is False
    assert window.control.pending == [True, False]


def test_restart_shutdown_is_ordered_and_launches_once_only_after_clean_shutdown(
    monkeypatch,
):
    import app.main_window as main_window

    window, order = _restart_window()
    detached = MagicMock(return_value=True)
    app = SimpleNamespace(quit=MagicMock())
    monkeypatch.setattr(main_window.QProcess, "startDetached", detached)
    monkeypatch.setattr(
        main_window,
        "QApplication",
        SimpleNamespace(instance=lambda: app),
    )
    monkeypatch.setattr(
        window,
        "_restart_command",
        lambda: ("python.exe", ["C:\\app\\main.py", "--flag"]),
    )

    window._restart_application()
    window._restart_application()

    assert order == ["control", "services"]
    detached.assert_called_once_with(
        "python.exe",
        ["C:\\app\\main.py", "--flag"],
    )
    app.quit.assert_called_once_with()
    assert window._restart_in_progress is True
    assert window.control.pending == [True]


@pytest.mark.parametrize(
    ("control_error", "service_error"),
    [
        (RuntimeError("control close failed"), None),
        (None, RuntimeError("service close failed")),
    ],
)
def test_restart_unclean_shutdown_never_relaunches_and_allows_retry(
    monkeypatch,
    control_error,
    service_error,
):
    import app.main_window as main_window

    window, order = _restart_window(
        control_error=control_error,
        service_error=service_error,
    )
    detached = MagicMock(return_value=True)
    critical = MagicMock()
    monkeypatch.setattr(main_window.QProcess, "startDetached", detached)
    monkeypatch.setattr(
        main_window,
        "QMessageBox",
        SimpleNamespace(critical=critical),
    )

    window._restart_application()

    assert order == ["control", "services"]
    detached.assert_not_called()
    assert window._restart_in_progress is False
    assert window.control.pending == [True, False]
    assert "关闭失败" in critical.call_args.args[2]
    assert "未启动新实例" in critical.call_args.args[2]


@pytest.mark.parametrize(
    "shutdown_result",
    [
        _application_shutdown_report(clean=False),
        None,
        SimpleNamespace(clean=True),
    ],
)
def test_restart_requires_an_exact_clean_application_shutdown_report(
    monkeypatch,
    shutdown_result,
):
    import app.main_window as main_window

    window, order = _restart_window(service_result=shutdown_result)
    detached = MagicMock(return_value=True)
    critical = MagicMock()
    monkeypatch.setattr(main_window.QProcess, "startDetached", detached)
    monkeypatch.setattr(
        main_window,
        "QMessageBox",
        SimpleNamespace(critical=critical),
    )

    window._restart_application()

    assert order == ["control", "services"]
    detached.assert_not_called()
    assert window._restart_in_progress is False
    assert window.control.pending == [True, False]
    assert "关闭失败" in critical.call_args.args[2]
    assert "未启动新实例" in critical.call_args.args[2]


@pytest.mark.parametrize(
    "detached_result",
    [False, (False, 0)],
)
def test_restart_launch_failure_shows_chinese_error_and_allows_retry(
    monkeypatch,
    detached_result,
):
    import app.main_window as main_window

    window, order = _restart_window()
    detached = MagicMock(return_value=detached_result)
    critical = MagicMock()
    app = SimpleNamespace(quit=MagicMock())
    monkeypatch.setattr(main_window.QProcess, "startDetached", detached)
    monkeypatch.setattr(
        main_window,
        "QMessageBox",
        SimpleNamespace(critical=critical),
    )
    monkeypatch.setattr(
        main_window,
        "QApplication",
        SimpleNamespace(instance=lambda: app),
    )

    window._restart_application()

    assert order == ["control", "services"]
    detached.assert_called_once()
    app.quit.assert_not_called()
    assert window._restart_in_progress is False
    assert window.control.pending == [True, False]
    assert "重启失败" in critical.call_args.args[2]
    assert "未启动新实例" in critical.call_args.args[2]


def test_restart_command_reconstructs_frozen_argv(monkeypatch):
    import app.main_window as main_window

    monkeypatch.setattr(main_window.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        main_window.sys,
        "argv",
        ["C:\\dist\\jingcheng.exe", "--station", "A"],
    )
    monkeypatch.setattr(main_window.sys, "executable", "C:\\dist\\jingcheng.exe")

    assert main_window.MainWindow._restart_command() == (
        "C:\\dist\\jingcheng.exe",
        ["--station", "A"],
    )


def test_restart_command_reconstructs_source_argv(monkeypatch):
    import app.main_window as main_window

    monkeypatch.delattr(main_window.sys, "frozen", raising=False)
    monkeypatch.setattr(
        main_window.sys,
        "argv",
        ["main.py", "--station", "A"],
    )
    monkeypatch.setattr(
        main_window.sys,
        "executable",
        "C:\\Python\\python.exe",
    )

    program, arguments = main_window.MainWindow._restart_command()

    assert program == "C:\\Python\\python.exe"
    assert arguments[0] == os.path.abspath(
        os.path.join(os.path.dirname(main_window.__file__), os.pardir, "main.py")
    )
    assert arguments[1:] == ["--station", "A"]


def test_degraded_production_still_builds_configuration_and_hardware_pages(
    qapp,
    monkeypatch,
):
    import app.main_window as main_window

    built_pages = []
    monkeypatch.setattr(
        main_window.MainWindow,
        "build_topbar",
        lambda _self: QWidget(),
    )
    monkeypatch.setattr(
        main_window.MainWindow,
        "_connect_bus_signals",
        lambda _self: None,
    )
    monkeypatch.setattr(
        main_window.MainWindow,
        "_reload_selected_plc_bindings",
        lambda _self: None,
    )
    monkeypatch.setattr(
        main_window.MainWindow,
        "_refresh_plc_state_labels",
        lambda _self: None,
    )
    monkeypatch.setattr(main_window.os.path, "exists", lambda _path: False)
    services = SimpleNamespace(
        bus=SimpleNamespace(log=_SignalStub()),
        factory_mode=None,
        startup_failure=None,
        production_unavailable_reason=(
            "生产功能不可用，请修复设备或配置后重启软件。"
        ),
    )
    for method_name, page_name in (
        ("build_test_page", "test"),
        ("build_data_page", "data"),
        ("build_config_page", "config"),
        ("build_ai_tool_management_page", "ai"),
        ("build_hardware_page", "hardware"),
    ):
        monkeypatch.setattr(
            main_window.MainWindow,
            method_name,
            lambda _self, name=page_name: (
                built_pages.append(name) or QWidget()
            ),
        )
    monkeypatch.setattr(main_window.MainWindow, "set_role", lambda _self: None)

    window = main_window.MainWindow(services)

    assert built_pages == ["test", "data", "config", "ai", "hardware"]
    assert window.tab.count() == 5


def test_operator_can_activate_data_management_but_not_admin_pages():
    import app.main_window as main_window

    window = main_window.MainWindow.__new__(main_window.MainWindow)
    window.current_user_role = "操作员"
    window.tab_names = ["测试", "数据管理", "配置", "AI管理", "硬件"]

    assert window._can_activate_main_tab(0)
    assert window._can_activate_main_tab(1)
    assert not window._can_activate_main_tab(2)
    assert not window._can_activate_main_tab(3)
    assert not window._can_activate_main_tab(4)


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
