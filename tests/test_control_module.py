from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.services_container import FactoryControllerState
from app.service.factory_mode_controller import FactoryModeController
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
        self.factory_plc_event = _SignalStub()
        self.factory_scan_event = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.factory_runtime_auto_analysis_succeeded = _SignalStub()
        self.factory_runtime_auto_analysis_no_result = _SignalStub()
        self.factory_runtime_auto_analysis_error = _SignalStub()
        self.factory_runtime_aborted = _SignalStub()
        self.factory_runtime_reanalysis_completed = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.log = _SignalStub()


class _ConfigServiceStub:
    def __init__(self, runtime_errors_by_id: dict[int, list[str]] | None = None):
        self._runtime_errors_by_id = runtime_errors_by_id or {}
        self._configs = {
            101: {
                "config_id": 101,
                "config_name": "Config Alpha",
                "analysis_window_offset_ms": 0,
                "analysis_duration": 2.0,
                "record_duration": 5.0,
                "plc_device_key": None,
                "plc_device_label": None,
                "selected_channels": [0],
            },
            202: {
                "config_id": 202,
                "config_name": "Config Beta",
                "analysis_window_offset_ms": 0,
                "analysis_duration": 2.0,
                "record_duration": 5.0,
                "plc_device_key": None,
                "plc_device_label": None,
                "selected_channels": [1],
            },
        }

    def list_configs(self):
        return [
            {
                "config_id": config["config_id"],
                "config_name": config["config_name"],
            }
            for config in self._configs.values()
        ]

    def load_config(self, config_id: int):
        return dict(self._configs[config_id])

    def validate_factory_runtime(self, config: dict) -> list[str]:
        return list(self._runtime_errors_by_id.get(config["config_id"], []))

    def set_runtime_errors(self, config_id: int, errors: list[str]) -> None:
        self._runtime_errors_by_id[config_id] = list(errors)


class _FactoryModeStub:
    def __init__(self, bus, initial_state: FactoryControllerState | None = None):
        self._bus = bus
        self.state = initial_state or FactoryControllerState()
        self.start_manual = MagicMock(return_value=True)
        self.stop_active_run = MagicMock(return_value=True)

    def can_arm(self) -> bool:
        return self.state.is_ready

    def set_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        manual_primary_config_id: int | None = None,
    ):
        selected = tuple(selected_config_ids)
        self.state = FactoryControllerState(
            selected_config_ids=selected,
            manual_primary_config_id=manual_primary_config_id,
            run_phase=(
                "armed"
                if len(selected) == 2
                and len(set(selected)) == 2
                and manual_primary_config_id in selected
                else "not_ready"
            ),
        )
        self._bus.factory_state_changed.emit(self.state)
        return self.state


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_module(
    *,
    initial_state: FactoryControllerState | None = None,
    runtime_errors_by_id: dict[int, list[str]] | None = None,
):
    bus = _BusStub()
    factory_mode = _FactoryModeStub(bus, initial_state)
    services = SimpleNamespace(
        bus=bus,
        factory_mode=factory_mode,
        config_service=_ConfigServiceStub(runtime_errors_by_id),
        test=SimpleNamespace(
            start=MagicMock(return_value=True),
            stop=MagicMock(return_value=True),
        ),
    )
    return ControlModule(services), services


def _make_shared_modules(*, initial_state: FactoryControllerState | None = None):
    bus = _BusStub()
    factory_mode = _FactoryModeStub(bus, initial_state)
    services = SimpleNamespace(
        bus=bus,
        factory_mode=factory_mode,
        config_service=_ConfigServiceStub(),
        test=SimpleNamespace(
            start=MagicMock(return_value=True),
            stop=MagicMock(return_value=True),
        ),
    )
    return WorkOrderModule(services), ControlModule(services), services


def _make_real_factory_modules():
    bus = _BusStub()
    config_service = _ConfigServiceStub()
    services = SimpleNamespace(
        bus=bus,
        config_service=config_service,
        test=SimpleNamespace(
            start=MagicMock(return_value=True),
            stop=MagicMock(return_value=True),
            is_running=MagicMock(return_value=False),
            cancel_pending_start=MagicMock(return_value=True),
        ),
    )
    services.factory_mode = FactoryModeController(bus, services)
    return WorkOrderModule(services), ControlModule(services), services


def _promote_controller_to_reviewing(services, *, displayed_config_id: int, trigger_source: str):
    controller = services.factory_mode
    controller._state = replace(
        controller.state,
        displayed_config_id=displayed_config_id,
        run_phase="reviewing",
        trigger_source=trigger_source,
    )
    services.bus.factory_state_changed.emit(controller.state)


def _set_combo_to_data(combo, data):
    index = combo.findData(data)
    assert index != -1
    combo.setCurrentIndex(index)


def test_start_delegates_to_factory_mode_controller_not_test_manager(qapp):
    module, services = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            run_phase="armed",
        )
    )
    workorder = {
        "sn": "SN-42",
        "model": "MODEL-X",
        "selected_config_ids": (101, 202),
        "manual_primary_config_id": 101,
    }
    services.bus.workorder_changed.emit(workorder)

    module.btn_start.click()

    services.factory_mode.start_manual.assert_called_once_with(workorder)
    services.test.start.assert_not_called()


def test_start_uses_live_workorder_metadata_without_prior_apply(qapp):
    workorder_module, control_module, services = _make_shared_modules()

    _set_combo_to_data(workorder_module.combo_config_a, 101)
    _set_combo_to_data(workorder_module.combo_config_b, 202)
    _set_combo_to_data(workorder_module.combo_primary, 101)
    workorder_module.ed_sn.setText("SN-LIVE")
    workorder_module.ed_model.setText("MODEL-LIVE")

    control_module.btn_start.click()

    services.factory_mode.start_manual.assert_called_once_with(
        {
            "sn": "SN-LIVE",
            "model": "MODEL-LIVE",
            "selected_config_ids": (101, 202),
            "manual_primary_config_id": 101,
        }
    )


def test_prearmed_page_construction_seeds_structured_workorder_before_first_start(qapp):
    _, control_module, services = _make_shared_modules(
        initial_state=FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=202,
            run_phase="armed",
        )
    )

    assert control_module.btn_start.isEnabled() is True

    control_module.btn_start.click()

    services.factory_mode.start_manual.assert_called_once_with(
        {
            "sn": "",
            "model": "",
            "selected_config_ids": (101, 202),
            "manual_primary_config_id": 202,
        }
    )


def test_start_uses_latest_workorder_metadata_after_editing_post_apply(qapp):
    workorder_module, control_module, services = _make_shared_modules()

    _set_combo_to_data(workorder_module.combo_config_a, 101)
    _set_combo_to_data(workorder_module.combo_config_b, 202)
    _set_combo_to_data(workorder_module.combo_primary, 101)
    workorder_module.ed_sn.setText("SN-OLD")
    workorder_module.ed_model.setText("MODEL-OLD")
    workorder_module.apply()
    workorder_module.ed_sn.setText("SN-NEW")
    workorder_module.ed_model.setText("MODEL-NEW")

    control_module.btn_start.click()

    services.factory_mode.start_manual.assert_called_once_with(
        {
            "sn": "SN-NEW",
            "model": "MODEL-NEW",
            "selected_config_ids": (101, 202),
            "manual_primary_config_id": 101,
        }
    )


def test_pending_start_disables_start_button_until_runtime_acceptance(qapp):
    workorder_module, control_module, services = _make_real_factory_modules()

    _set_combo_to_data(workorder_module.combo_config_a, 101)
    _set_combo_to_data(workorder_module.combo_config_b, 202)
    _set_combo_to_data(workorder_module.combo_primary, 101)
    workorder_module.ed_sn.setText("SN-PENDING")

    assert control_module.btn_start.isEnabled() is True

    control_module.btn_start.click()

    services.test.start.assert_called_once()
    assert control_module.btn_start.isEnabled() is False
    assert "启动请求处理中" in control_module.lbl_start_reason.text()


def test_stop_delegates_to_factory_mode_controller_stop_flow(qapp):
    module, services = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            active_config_id=101,
            displayed_config_id=101,
            run_phase="running",
            trigger_source="manual",
        )
    )

    module.btn_stop.click()

    services.factory_mode.stop_active_run.assert_called_once_with()
    services.test.stop.assert_not_called()


def test_start_is_disabled_when_factory_mode_is_not_ready(qapp):
    module, _ = _make_module(initial_state=FactoryControllerState())

    assert module.btn_start.isEnabled() is False
    assert "请选择两个不同配置" in module.lbl_start_reason.text()


def test_start_is_disabled_when_manual_primary_is_missing(qapp):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=None,
            run_phase="not_ready",
        )
    )

    assert module.btn_start.isEnabled() is False
    assert "主配置" in module.lbl_start_reason.text()


def test_start_is_disabled_when_runtime_validation_fails(qapp):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            run_phase="not_ready",
            selection_ready=False,
            selection_ready_reason="分析窗起始偏移加分析时长不能超过录音时长",
            start_ready=False,
            start_ready_reason="分析窗起始偏移加分析时长不能超过录音时长",
        ),
        runtime_errors_by_id={202: ["分析窗起始偏移加分析时长不能超过录音时长"]},
    )

    assert module.btn_start.isEnabled() is False
    assert "分析窗起始偏移加分析时长不能超过录音时长" in module.lbl_start_reason.text()


def test_same_id_config_refresh_disables_start_after_runtime_validation_changes_in_reviewing(qapp):
    workorder_module, control_module, services = _make_real_factory_modules()

    _set_combo_to_data(workorder_module.combo_config_a, 101)
    _set_combo_to_data(workorder_module.combo_config_b, 202)
    _set_combo_to_data(workorder_module.combo_primary, 101)
    _promote_controller_to_reviewing(
        services,
        displayed_config_id=101,
        trigger_source="manual",
    )

    assert control_module.btn_start.isEnabled() is True

    services.config_service.set_runtime_errors(
        202,
        ["分析窗起始偏移加分析时长不能超过录音时长"],
    )
    services.bus.config_changed.emit()

    assert control_module.btn_start.isEnabled() is False
    assert "分析窗起始偏移加分析时长不能超过录音时长" in control_module.lbl_start_reason.text()
    assert "已就绪" not in workorder_module.lbl_selection_status.text()
    assert "分析窗起始偏移加分析时长不能超过录音时长" in workorder_module.lbl_selection_status.text()
    assert "reviewing" in workorder_module.lbl_phase_summary.text().lower()


@pytest.mark.parametrize(
    ("run_phase", "reason_fragment"),
    [
        ("running", "运行中"),
        ("analyzing", "分析中"),
        ("reanalyzing", "重新分析"),
    ],
)
def test_start_is_disabled_during_blocking_factory_phases(qapp, run_phase, reason_fragment):
    module, _ = _make_module(
        initial_state=FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            active_config_id=101,
            displayed_config_id=101,
            run_phase=run_phase,
            trigger_source="manual",
        )
    )

    assert module.btn_start.isEnabled() is False
    assert reason_fragment in module.lbl_start_reason.text()
