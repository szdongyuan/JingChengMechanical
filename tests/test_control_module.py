from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.services_container import FactoryControllerState, FactoryStartBlockedPayload
from ui.modules.control import ControlModule
from ui.modules.workorder import WorkOrderModule


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
        self.factory_state_changed = _SignalStub()
        self.factory_start_blocked = _SignalStub()
        self.factory_selection_notice = _SignalStub()
        self.log = _SignalStub()
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


def _make_module(
    *,
    initial_state: FactoryControllerState | None = None,
):
    bus = _BusStub()
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(),
        factory_selection_settings=_FactorySelectionSettingsStub(),
        factory_mode=_FactoryModeStub(bus, initial_state),
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
    )
    return WorkOrderModule(services), ControlModule(services), services


def _make_module_with_message_box_spy(
    monkeypatch,
    *,
    initial_state: FactoryControllerState | None = None,
):
    module, services = _make_module(initial_state=initial_state)
    recorder = _MessageBoxSpy()
    monkeypatch.setattr(module, "_create_blocked_start_dialog", lambda: recorder.dialog)
    return module, services, recorder


def test_control_panel_renders_only_two_button_rows_with_updated_spacing_and_button_heights(
    qapp,
):
    module, _ = _make_module()

    assert hasattr(module, "lbl_start_reason") is False
    assert module.layout().count() == 2
    assert module.layout().spacing() == 8
    assert module.btn_start.minimumHeight() == 36
    assert module.btn_stop.minimumHeight() == 36
    assert module.btn_pass.minimumHeight() == 36
    assert module.btn_fail.minimumHeight() == 36


def test_start_uses_live_workorder_payload_from_current_workorder_module(qapp):
    workorder_module, control_module, services = _make_shared_modules()

    workorder_module.config_selector.set_checked(101, True)
    workorder_module.config_selector.set_checked(202, True)
    workorder_module.ed_sn.setText("SN-LIVE")
    workorder_module.ed_model.setText("MODEL-LIVE")

    control_module.btn_start.click()

    services.factory_mode.start_manual.assert_called_once_with(
        {
            "sn": "SN-LIVE",
            "model": "MODEL-LIVE",
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


def test_blocked_start_signal_opens_and_updates_single_modal(qapp, monkeypatch):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_state_changed.emit(_idle_blocked_state("配置已失效"))
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置已失效", trigger_source="manual")
    )
    services.bus.factory_state_changed.emit(_idle_blocked_state("配置加载失败"))
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置加载失败", trigger_source="plc")
    )

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
    assert recorder.latest_title == "自动开始已阻止"
    assert hasattr(module, "lbl_start_reason") is False


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

    assert recorder.open_count == 1
    assert recorder.latest_text == "请至少启用一个配置"
    assert hasattr(module, "lbl_start_reason") is False


def test_blocked_modal_closes_after_reason_clears(qapp, monkeypatch):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置已失效", trigger_source="manual")
    )
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

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
