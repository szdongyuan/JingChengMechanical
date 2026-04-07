from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.modules.setdefault("cv2", SimpleNamespace())

from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from app.service.analysis.frames import StftFrame
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
import app.main_window as main_window_mod


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
        self.stft_updated = _SignalStub()
        self.fft_updated = _SignalStub()
        self.spl_updated = _SignalStub()
        self.octave_updated = _SignalStub()
        self.factory_start_blocked = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()


class _StubModule(QWidget):
    def __init__(self, services=None):
        super().__init__()
        self.services = services


def _make_guarded_ai_tool_management_module(confirm_leave_result: bool):
    class _GuardedAiToolManagementModule(_StubModule):
        def __init__(self, services=None):
            super().__init__(services)
            self.confirm_leave_calls = 0

        def confirm_leave(self) -> bool:
            self.confirm_leave_calls += 1
            return confirm_leave_result

    return _GuardedAiToolManagementModule


class _ChannelProbeModule(_StubModule):
    def __init__(self, services=None):
        super().__init__(services)
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


def _patch_non_target_modules(monkeypatch, *, use_real_control: bool = False):
    module_names = [
        "WorkOrderModule",
        "VideoModule",
        "HardwareModule",
        "InputCalibrationModule",
        "ConfigModule",
        "AiToolManagementModule",
        "ChannelSelectModule",
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
    factory_mode = SimpleNamespace(
        state=initial_state or FactoryControllerState(),
        review_payload=initial_review_payload or FactoryReviewPayload(),
        begin_reanalysis=MagicMock(return_value=True),
        start_manual=MagicMock(),
        stop_active_run=MagicMock(),
    )
    return SimpleNamespace(
        bus=bus,
        db=SimpleNamespace(close=MagicMock()),
        config_service=SimpleNamespace(
            load_config=MagicMock(return_value={"selected_channels": []})
        ),
        factory_mode=factory_mode,
    )


def _make_window(
    monkeypatch,
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
    use_real_control: bool = False,
    ai_tool_management_cls: type[QWidget] | None = None,
):
    _patch_non_target_modules(monkeypatch, use_real_control=use_real_control)
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
    window.login_shown = True
    window.resize(1440, 900)
    window.show()
    qapp.processEvents()


def test_main_window_login_refreshes_control_owned_start_button_state_without_enabling_busy_start(
    qapp,
    monkeypatch,
):
    busy_state = FactoryControllerState(
        selected_config_ids=(101,),
        active_config_id=101,
        displayed_config_id=101,
        run_phase="running",
        trigger_source="manual",
        start_ready=False,
        start_ready_reason="当前测试运行中",
    )
    window, _ = _make_window(
        monkeypatch,
        initial_state=busy_state,
        use_real_control=True,
    )
    _show(window, qapp)

    assert window.control.btn_start.isEnabled() is False
    assert window.control.btn_stop.isEnabled() is False

    window.current_user_name = "operator"
    window.current_user_role = "操作员"
    window.set_role()
    qapp.processEvents()

    assert window.control.btn_start.isEnabled() is False
    assert window.control.lbl_start_reason.text() == "当前测试运行中"
    assert window.control.btn_stop.isEnabled() is True


def test_show_login_window_disables_main_window_until_login_success(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    monkeypatch.setattr(main_window_mod, "LoginWindow", _FakeLoginWindow)

    window, _ = _make_window(monkeypatch)

    assert window.isEnabled() is True

    window.show_login_window(close_main_window=False, window_title="切换用户")
    qapp.processEvents()

    assert window.isEnabled() is False
    assert isinstance(window.login_window, _FakeLoginWindow)
    assert window.login_window.window_title == "切换用户"
    assert window.login_window.shown is True

    window.login_window.trigger_success("operator", "操作员")
    qapp.processEvents()

    assert window.isEnabled() is True
    assert window.current_user_name == "operator"
    assert window.current_user_role == "操作员"


def test_show_login_window_cancel_reenables_main_window_without_closing_for_switch_user(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    monkeypatch.setattr(main_window_mod, "LoginWindow", _FakeLoginWindow)

    window, _ = _make_window(monkeypatch)
    close_spy = MagicMock()
    monkeypatch.setattr(window, "close", close_spy)

    window.show_login_window(close_main_window=False, window_title="切换用户")
    qapp.processEvents()

    assert window.isEnabled() is False

    window.login_window.trigger_cancel()
    qapp.processEvents()

    assert window.isEnabled() is True
    close_spy.assert_not_called()


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


def test_show_login_window_cancel_reenables_main_window_before_closing_initial_login(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    monkeypatch.setattr(main_window_mod, "LoginWindow", _FakeLoginWindow)

    window, _ = _make_window(monkeypatch)
    enabled_state_when_close_called = []

    def _close_spy():
        enabled_state_when_close_called.append(window.isEnabled())

    monkeypatch.setattr(window, "close", _close_spy)

    window.show_login_window(close_main_window=True, window_title="登录")
    qapp.processEvents()

    assert window.isEnabled() is False

    window.login_window.trigger_cancel()
    qapp.processEvents()

    assert enabled_state_when_close_called == [True]
    assert window.isEnabled() is True


def test_main_window_includes_ai_tool_management_tab_in_topbar(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    assert window.tab_names == ["测试", "数据管理", "配置", "AI工具管理", "硬件"]
    assert list(window.top_button_dict) == ["测试", "数据管理", "配置", "AI工具管理", "硬件"]


def test_engineer_role_can_access_ai_tool_management_tab(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "工程师"
    window.set_role()

    assert window.top_button_dict["AI工具管理"].isEnabled() is True


def test_admin_role_can_access_ai_tool_management_tab(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "管理员"
    window.set_role()

    assert window.top_button_dict["AI工具管理"].isEnabled() is True


def test_operator_role_cannot_access_ai_tool_management_tab(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "操作员"
    window.set_role()

    assert window.top_button_dict["AI工具管理"].isEnabled() is False


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

    ai_index = window.tab_names.index("AI工具管理")

    window.tab.setCurrentIndex(ai_index)
    qapp.processEvents()

    assert window.tab.currentIndex() == 0
    assert window.top_button_dict["测试"].isChecked() is True
    assert window.top_button_dict["AI工具管理"].isChecked() is False


def test_engineer_cannot_activate_data_management_via_programmatic_tab_change(
    qapp,
    monkeypatch,
):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    window.current_user_role = "工程师"
    window.set_role()

    data_management_index = window.tab_names.index("数据管理")

    window.tab.setCurrentIndex(data_management_index)
    qapp.processEvents()

    assert window.tab.currentIndex() == 0
    assert window.top_button_dict["测试"].isChecked() is True
    assert window.top_button_dict["数据管理"].isChecked() is False


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

    ai_index = window.tab_names.index("AI工具管理")
    window.top_button_dict["AI工具管理"].click()
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

    ai_index = window.tab_names.index("AI工具管理")
    hardware_index = window.tab_names.index("硬件")

    window.top_button_dict["AI工具管理"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index

    window.tab.setCurrentIndex(hardware_index)
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index
    assert window.top_button_dict["AI工具管理"].isChecked() is True
    assert window.top_button_dict["硬件"].isChecked() is False
    assert window.ai_tool_management_module.confirm_leave_calls == 1


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

    ai_index = window.tab_names.index("AI工具管理")
    window.top_button_dict["AI工具管理"].click()
    qapp.processEvents()

    assert window.tab.currentIndex() == ai_index

    window.current_user_name = "operator"
    window.current_user_role = "操作员"
    window.set_role()
    qapp.processEvents()

    assert window.current_user_name == "admin"
    assert window.current_user_role == "管理员"
    assert window.tab.currentIndex() == ai_index
    assert window.top_button_dict["AI工具管理"].isEnabled() is True
    assert window.top_button_dict["AI工具管理"].isChecked() is True
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

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert close_event.isAccepted() is False
    services.db.close.assert_not_called()
    assert window.ai_tool_management_module.confirm_leave_calls == 1


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


def test_armed_page_keeps_chart_cards_empty_until_committed_review_payload_arrives(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_armed_state(),
    )
    _show(window, qapp)

    _assert_run_specific_cards_cleared(window)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_next_run_reset_clears_result_stft_and_chart_cards_before_next_review_payload_renders(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    assert window.result.empty_state.isHidden() is True
    assert window.stft.selection_box.isVisible() is True
    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("next_run_accepted")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(_armed_state())
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

    _emit_runtime_started(services, config_id=202, run_id="run-202-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_chart_frames(services, config_id=202, run_id="run-202-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_review_ready(services, config_id=202, run_id="run-202-1")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_same_config_rerun_keeps_chart_cards_empty_until_new_review_payload_arrives(
    qapp,
    monkeypatch,
):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("next_run_accepted")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(_armed_state())
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

    _emit_runtime_started(services, config_id=101, run_id="run-101-2")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    _emit_chart_frames(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window, result_badge="分析中")

    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, run_id="run-101-1")
    )
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)

    _emit_review_ready(services, config_id=101, run_id="run-101-2")
    qapp.processEvents()

    _assert_chart_cards_visible(window)


def test_selection_changed_reset_clears_result_stft_and_chart_cards(qapp, monkeypatch):
    window, services = _make_window(monkeypatch, initial_state=_armed_state())
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    _emit_review_ready(services, config_id=101, run_id="run-101-1")
    qapp.processEvents()

    assert window.result.empty_state.isHidden() is True
    assert window.stft.selection_box.isVisible() is True
    _assert_chart_cards_visible(window)

    services.bus.factory_display_reset.emit("selection_changed")
    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(
        _armed_state(
            selected_config_ids=(303, 404),
        )
    )
    qapp.processEvents()

    _assert_run_specific_cards_cleared(window)


def test_main_window_reflows_test_page_for_loudness_and_video(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    assert "系统日志" not in _test_page_card_titles(window)
    assert _card_title(_test_page_left_slot_card(window, 2)) == "视频预览"
    assert window.video.parentWidget() is _test_page_left_slot_card(window, 2)
    assert _card_title(_test_page_grid_card(window, 2, 0)) == "Loudness"
    assert window.loudness.parentWidget() is _test_page_grid_card(window, 2, 0)


def test_test_page_uses_renamed_spl_card_title(qapp, monkeypatch):
    window, _ = _make_window(monkeypatch)
    _show(window, qapp)

    assert _card_title(_test_page_grid_card(window, 2, 1)) == "声压级 (SPL)"


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


def test_main_window_channel_toolbar_shows_for_multi_channel_running_config(qapp, monkeypatch):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.analysis_toolbar.isVisible() is True
    assert window.analysis_toolbar.channel_combo.isVisible() is True
    assert window.analysis_toolbar.channel_combo.isEnabled() is True
    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id == {101: 0}
    assert callable(window.stft.selected_display_channel_provider)
    assert callable(window.loudness.selected_display_channel_provider)
    assert callable(window.spl.selected_display_channel_provider)
    assert callable(window.spectrum.selected_display_channel_provider)
    assert callable(window.octave.selected_display_channel_provider)
    assert window.stft.selected_display_channel_provider() == 0


def test_main_window_toolbar_selection_immediately_refreshes_stft_cached_channel(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=101,
            run_id="run-101",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.0, magnitudes=(9.0, 10.0)),
    )
    qapp.processEvents()

    assert window.stft.current_render_cache_key() == (101, "run-101", 0)
    assert np.array_equal(
        window.stft.current_image_payload()["magnitude_db"],
        np.array([[1.0], [2.0]]),
    )

    _select_toolbar_channel(window, qapp, 1)

    assert window.stft.current_render_cache_key() == (101, "run-101", 1)
    assert np.array_equal(
        window.stft.current_image_payload()["magnitude_db"],
        np.array([[9.0], [10.0]]),
    )


def test_main_window_page_reentry_does_not_redraw_visible_unchanged_stft(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=101,
            run_id="run-101",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.0, magnitudes=(9.0, 10.0)),
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    redraw_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        window.stft.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    window.tab.setCurrentIndex(1)
    qapp.processEvents()
    window.tab.setCurrentIndex(0)
    qapp.processEvents()

    assert redraw_calls == []
    assert window.stft.current_render_cache_key() == (101, "run-101", 1)


def test_main_window_page_reentry_refreshes_stft_from_existing_cache(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=101,
            run_id="run-101",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.0, magnitudes=(9.0, 10.0)),
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    assert window.stft.current_render_cache_key() == (101, "run-101", 1)
    assert np.array_equal(
        window.stft.canvas.heatmap.image_payload()["magnitude_db"],
        np.array([[9.0], [10.0]]),
    )

    window.tab.setCurrentIndex(1)
    qapp.processEvents()
    window.stft.canvas.heatmap.clear_data()
    window.stft._render_cache_key = None

    assert window.stft.current_image_payload() is None

    window.tab.setCurrentIndex(0)
    qapp.processEvents()

    assert window.stft.current_render_cache_key() == (101, "run-101", 1)
    assert np.array_equal(
        window.stft.canvas.heatmap.image_payload()["magnitude_db"],
        np.array([[9.0], [10.0]]),
    )


def test_main_window_page_reentry_clears_stft_when_selected_channel_has_no_cache(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    services.bus.factory_runtime_start_accepted.emit(
        SimpleNamespace(
            config_id=101,
            run_id="run-101",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    qapp.processEvents()

    assert window.stft.current_render_cache_key() == (101, "run-101", 0)
    assert window.stft.current_image_payload() is not None

    window.tab.setCurrentIndex(1)
    qapp.processEvents()
    window._sync_selected_display_channel(1, refresh_modules=False)

    assert window.stft.current_render_cache_key() == (101, "run-101", 0)
    assert np.array_equal(
        window.stft.canvas.heatmap.image_payload()["magnitude_db"],
        np.array([[1.0], [2.0]]),
    )

    window.tab.setCurrentIndex(0)
    qapp.processEvents()

    assert window.stft.current_render_cache_key() is None
    assert window.stft.current_image_payload() is None


def test_main_window_toolbar_selection_immediately_refreshes_committed_chart_modules(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    _assert_committed_chart_channel(window, channel_index=0)

    _select_toolbar_channel(window, qapp, 1)

    _assert_committed_chart_channel(window, channel_index=1)


def test_main_window_review_loudness_card_uses_data_focused_y_range(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0,)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0,))
    )
    qapp.processEvents()

    assert window.loudness.plot.plot_item.vb.viewRange()[1] == pytest.approx((9.8, 12.2))


def test_main_window_ai_partial_no_result_keeps_rule_rows_and_committed_charts_visible(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _partial_no_result_review_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert window.result.lb_overall.text() == "无结果"
    assert window.result.error_label.isHidden() is False
    assert window.result.error_label.text() == "AI timeout"
    assert _result_table_value(window, 0, 0) == "SPL"
    assert _result_table_value(window, 0, 1) == "-1 dB"
    assert _result_table_value(window, 0, 2) == "CH2"
    assert _result_table_value(window, 0, 3) == "OK"
    assert _result_table_value(window, 1, 0) == "Loudness"
    assert _result_table_value(window, 1, 1) == "-0.5 sone"
    assert _result_table_value(window, 1, 2) == "CH2"
    assert _result_table_value(window, 1, 3) == "OK"
    assert _result_table_value(window, 2, 0) == "FFT"
    assert _result_table_value(window, 2, 1) == "2 dB"
    assert _result_table_value(window, 2, 2) == "CH1"
    assert _result_table_value(window, 2, 3) == "NG"
    assert _result_table_value(window, 3, 0) == "倍频程"
    assert _result_table_value(window, 3, 1) == "-3 dB"
    assert _result_table_value(window, 3, 2) == "CH2"
    assert _result_table_value(window, 3, 3) == "OK"
    assert _result_table_value(window, 4, 0) == "AI分析"
    assert _result_table_value(window, 4, 2) == "--"
    assert _result_table_value(window, 4, 3) == "无结果"
    _assert_chart_cards_visible(window)
    _assert_committed_chart_channel(window, channel_index=1)


def test_main_window_resolves_display_channel_before_analysis_modules_handle_factory_signals(
    qapp,
    monkeypatch,
):
    for name in ("ResultModule",):
        monkeypatch.setattr(main_window_mod, name, _StubModule)
    for name in (
        "SpectrumModule",
        "StftModule",
        "SplModule",
        "OctaveModule",
        "LoudnessModule",
    ):
        monkeypatch.setattr(main_window_mod, name, _ChannelProbeModule)

    window, services = _make_window(
        monkeypatch,
        initial_state=_armed_state(),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.stft.state_seen_channels[-1] == 0
    assert window.loudness.state_seen_channels[-1] == 0
    assert window.spl.state_seen_channels[-1] == 0

    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 2))
    )
    qapp.processEvents()

    assert window.stft.review_seen_channels[-1] == 0
    assert window.loudness.review_seen_channels[-1] == 0
    assert window.octave.review_seen_channels[-1] == 0


def test_main_window_review_selected_channel_without_loudness_payload_keeps_other_cards_on_channel(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(
            101,
            channels=(0, 1),
            missing_loudness_channels=(1,),
        ),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            101,
            channels=(0, 1),
            missing_loudness_channels=(1,),
        )
    )
    qapp.processEvents()

    _select_toolbar_channel(window, qapp, 1)

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id[101] == 1
    assert window.spl.current_render_channel() == 1
    assert window.spectrum.current_render_channel() == 1
    assert window.octave.current_render_channel() == 1
    assert window.loudness.has_committed_plot_data() is False
    assert window.loudness.current_render_channel() is None
    assert window.loudness.current_plot_payload() is None


def test_main_window_review_selected_channel_missing_fft_and_octave_payloads_keeps_channel_selected(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    review_payload = _review_payload_with_chart_payload(101, channels=(0, 1))
    del review_payload.chart_payload[1]["fft"]
    del review_payload.chart_payload[1]["octave"]

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id[101] == 1
    assert window.spl.current_render_channel() == 1
    assert window.loudness.current_render_channel() == 1
    assert window.spectrum.has_committed_plot_data() is False
    assert window.spectrum.current_render_channel() == 1
    assert window.spectrum.current_plot_payload() is None
    assert window.octave.has_committed_plot_data() is False
    assert window.octave.current_render_channel() == 1
    assert window.octave.current_plot_payload() is None


def test_main_window_channel_toolbar_hides_or_disables_selector_for_single_channel_displayed_config(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.analysis_toolbar.isVisible() is True
    assert window.analysis_toolbar.current_channel_index() == 0
    assert (
        window.analysis_toolbar.channel_combo.isHidden()
        or not window.analysis_toolbar.channel_combo.isEnabled()
    )
    assert window.display_channel_by_config_id == {101: 0}


def test_main_window_remembers_selected_channel_per_config_when_switching_displayed_configs(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.side_effect = lambda config_id: {
        101: {"config_id": 101, "selected_channels": [0, 1]},
        202: {"config_id": 202, "selected_channels": [0, 2]},
    }[config_id]
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_running_state(202))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 2)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id[101] == 1
    assert window.spl.selected_display_channel_provider() == 1

    services.bus.factory_state_changed.emit(_running_state(202))
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 2
    assert window.display_channel_by_config_id[202] == 2
    assert window.spectrum.selected_display_channel_provider() == 2


def test_main_window_review_switching_displayed_configs_restores_last_selected_channel_for_charts(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.side_effect = lambda config_id: {
        101: {"config_id": 101, "selected_channels": [0, 1]},
        202: {"config_id": 202, "selected_channels": [0, 2]},
    }[config_id]
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)
    _assert_committed_chart_channel(window, channel_index=1)

    services.bus.factory_state_changed.emit(_reviewing_state(202))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(202, channels=(0, 2))
    )
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 2)
    _assert_committed_chart_channel(window, channel_index=2)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id[101] == 1
    _assert_committed_chart_channel(window, channel_index=1)

    services.bus.factory_state_changed.emit(_reviewing_state(202))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(202, channels=(0, 2))
    )
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 2
    assert window.display_channel_by_config_id[202] == 2
    _assert_committed_chart_channel(window, channel_index=2)


def test_main_window_startup_in_review_prefers_first_selected_config_channel_when_none_is_remembered(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [1, 0],
    }

    window = main_window_mod.MainWindow(services)
    _show(window, qapp)

    assert _toolbar_channel_options(window) == [1, 0]
    assert window.analysis_toolbar.current_channel_index() == 1
    assert window.display_channel_by_config_id == {101: 1}
    assert window.stft.selected_display_channel_provider() == 1


def test_main_window_startup_in_review_renders_committed_chart_modules_on_resolved_shared_channel(
    qapp,
    monkeypatch,
):
    _patch_non_target_modules(monkeypatch)
    services = _make_services(
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 1)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [1, 0],
    }

    window = main_window_mod.MainWindow(services)
    _show(window, qapp)

    assert window.analysis_toolbar.current_channel_index() == 1
    _assert_committed_chart_channel(window, channel_index=1)


def test_main_window_review_time_selected_channel_fallback_picks_first_committed_chart_payload_channel(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 2))
    )
    qapp.processEvents()

    assert _toolbar_channel_options(window) == [0, 1]
    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id[101] == 0
    assert window.octave.selected_display_channel_provider() == 0


def test_main_window_toolbar_selection_missing_committed_channel_resynchronizes_to_fallback(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_reviewing_state(101),
        initial_review_payload=_review_payload_with_chart_payload(101, channels=(0, 2)),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_reviewing_state(101))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 2))
    )
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 0
    _assert_committed_chart_channel(window, channel_index=0)

    _select_toolbar_channel(window, qapp, 1)
    qapp.processEvents()

    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.display_channel_by_config_id[101] == 0
    _assert_committed_chart_channel(window, channel_index=0)


def test_main_window_channel_toolbar_hides_when_returning_to_armed_without_displayed_config(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    assert window.analysis_toolbar.isVisible() is True

    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    services.bus.factory_state_changed.emit(_armed_state())
    qapp.processEvents()

    assert window.analysis_toolbar.isHidden() is True
