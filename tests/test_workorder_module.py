from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.service.factory_mode_controller import FactoryModeController
from app.services_container import FactoryControllerState, FactoryPlcIngressPayload
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
        self.log = _SignalStub()
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


class _ConfigServiceStub:
    def __init__(
        self,
        configs: list[dict],
        *,
        runtime_errors_by_id: dict[int, list[str]] | None = None,
    ):
        self._configs = {config["config_id"]: dict(config) for config in configs}
        self._listed_config_ids = list(self._configs)
        self._runtime_errors_by_id = runtime_errors_by_id or {}

    def list_configs(self):
        return [
            {
                "config_id": config["config_id"],
                "config_name": config["config_name"],
            }
            for config_id, config in self._configs.items()
            if config_id in self._listed_config_ids
        ]

    def load_config(self, config_id: int):
        return dict(self._configs[config_id])

    def delete_config(self, config_id: int):
        self._configs.pop(config_id, None)
        self._listed_config_ids = [
            listed_config_id
            for listed_config_id in self._listed_config_ids
            if listed_config_id != config_id
        ]

    def set_listed_config_ids(self, config_ids: list[int]):
        self._listed_config_ids = list(config_ids)

    def update_config(self, config_id: int, **changes):
        self._configs[config_id].update(changes)

    def validate_factory_runtime(self, config: dict) -> list[str]:
        return list(self._runtime_errors_by_id.get(config["config_id"], []))

    def set_runtime_errors(self, config_id: int, errors: list[str]) -> None:
        self._runtime_errors_by_id[config_id] = list(errors)


class _FactoryModeStub:
    def __init__(self, initial_state: FactoryControllerState | None = None):
        self.state = initial_state or FactoryControllerState()
        self.selection_calls: list[tuple[tuple[int, ...], int | None]] = []

    def set_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        manual_primary_config_id: int | None = None,
    ):
        selected = tuple(selected_config_ids)
        self.selection_calls.append((selected, manual_primary_config_id))
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
        return self.state


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_config(
    config_id: int,
    config_name: str,
    *,
    plc_device_label: str | None,
    selected_channels: list[int],
    record_duration: float,
    analysis_duration: float,
    plc_device_key: str | None = None,
    scan_triggers_test: bool = False,
):
    return {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": record_duration,
        "analysis_duration": analysis_duration,
        "analysis_window_offset_ms": 0,
        "scan_triggers_test": scan_triggers_test,
        "plc_device_key": plc_device_key if plc_device_key is not None else (
            f"plc-{config_id}" if plc_device_label else None
        ),
        "plc_device_label": plc_device_label,
        "selected_channels": selected_channels,
        "channels": [],
    }


def _make_module(
    configs: list[dict] | None = None,
    *,
    initial_factory_state: FactoryControllerState | None = None,
    runtime_errors_by_id: dict[int, list[str]] | None = None,
):
    bus = _BusStub()
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(
            configs
            or [
                _make_config(
                    101,
                    "Config Alpha",
                    plc_device_label="PLC A",
                    selected_channels=[0, 1],
                    record_duration=5.0,
                    analysis_duration=2.0,
                ),
                _make_config(
                    202,
                    "Config Beta",
                    plc_device_label=None,
                    selected_channels=[1],
                    record_duration=6.0,
                    analysis_duration=2.5,
                ),
                _make_config(
                    303,
                    "Config Gamma",
                    plc_device_label=None,
                    selected_channels=[0],
                    record_duration=7.0,
                    analysis_duration=3.0,
                    scan_triggers_test=True,
                ),
            ],
            runtime_errors_by_id=runtime_errors_by_id,
        ),
        factory_mode=_FactoryModeStub(initial_factory_state),
    )
    return WorkOrderModule(services), services


def _make_module_with_real_factory_controller(
    configs: list[dict] | None = None,
    *,
    runtime_errors_by_id: dict[int, list[str]] | None = None,
):
    bus = _BusStub()
    config_service = _ConfigServiceStub(
        configs
        or [
            _make_config(
                101,
                "Config Alpha",
                plc_device_label="PLC A",
                selected_channels=[0, 1],
                record_duration=5.0,
                analysis_duration=2.0,
            ),
            _make_config(
                202,
                "Config Beta",
                plc_device_label=None,
                selected_channels=[1],
                record_duration=6.0,
                analysis_duration=2.5,
            ),
        ],
        runtime_errors_by_id=runtime_errors_by_id,
    )
    services = SimpleNamespace(
        bus=bus,
        config_service=config_service,
        test=SimpleNamespace(
            start=MagicMock(return_value=True),
            is_running=MagicMock(return_value=False),
            cancel_pending_start=MagicMock(return_value=True),
        ),
    )
    services.factory_mode = FactoryModeController(bus, services)
    return WorkOrderModule(services), services


def _set_combo_to_data(combo, data):
    index = combo.findData(data)
    assert index != -1
    combo.setCurrentIndex(index)


def _select_pair_with_primary(module, *, config_a: int, config_b: int, primary: int):
    _set_combo_to_data(module.combo_config_a, config_a)
    _set_combo_to_data(module.combo_config_b, config_b)
    _set_combo_to_data(module.combo_primary, primary)


def _promote_controller_to_reviewing(services, *, displayed_config_id: int, trigger_source: str):
    controller = services.factory_mode
    controller._state = replace(
        controller.state,
        displayed_config_id=displayed_config_id,
        run_phase="reviewing",
        trigger_source=trigger_source,
    )
    services.bus.factory_state_changed.emit(controller.state)


def test_two_distinct_config_selectors_exist(qapp):
    module, _ = _make_module()

    assert hasattr(module, "combo_config_a")
    assert hasattr(module, "combo_config_b")
    assert hasattr(module, "combo_primary")
    assert module.combo_config_a.findData(101) != -1
    assert module.combo_config_b.findData(202) != -1


def test_duplicate_config_selection_is_prevented(qapp):
    module, _ = _make_module()

    _set_combo_to_data(module.combo_config_a, 101)

    assert module.combo_config_b.findData(101) == -1


def test_primary_required_when_pair_selected(qapp):
    module, services = _make_module()

    _set_combo_to_data(module.combo_config_a, 101)
    _set_combo_to_data(module.combo_config_b, 202)

    assert module.combo_primary.currentData() is None
    assert services.factory_mode.selection_calls[-1] == ((101, 202), None)
    assert module.value()["manual_primary_config_id"] is None


def test_invalidating_selected_primary_forces_reconfirmation(qapp):
    module, services = _make_module()

    _select_pair_with_primary(module, config_a=101, config_b=202, primary=101)
    _set_combo_to_data(module.combo_config_a, 303)

    assert module.combo_primary.currentData() is None
    assert services.factory_mode.selection_calls[-1] == ((303, 202), None)


@pytest.mark.parametrize("run_phase", ["running", "analyzing", "reanalyzing"])
def test_selectors_lock_during_active_factory_phases(qapp, run_phase):
    module, services = _make_module()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            run_phase=run_phase,
            trigger_source="manual",
        )
    )

    assert module.combo_config_a.isEnabled() is False
    assert module.combo_config_b.isEnabled() is False
    assert module.combo_primary.isEnabled() is False


def test_phase_trigger_summary_text_is_visible(qapp):
    module, services = _make_module()

    _select_pair_with_primary(module, config_a=101, config_b=202, primary=101)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            run_phase="reviewing",
            trigger_source="plc",
        )
    )

    assert "reviewing" in module.lbl_phase_summary.text().lower()
    assert "plc" in module.lbl_trigger_summary.text().lower()
    assert "Config Alpha" in module.lbl_factory_summary.text()
    assert "PLC A" in module.lbl_factory_summary.text()
    assert "Trigger:" not in module.lbl_factory_summary.text()
    assert "Scan:" in module.lbl_factory_summary.text()
    assert "5.0s" in module.lbl_factory_summary.text()


def test_free_text_config_field_is_no_longer_authoritative(qapp):
    module, _ = _make_module()

    module.ed_cfg.setText("Legacy Free Text")
    _select_pair_with_primary(module, config_a=101, config_b=202, primary=202)

    payload = module.value()

    assert module.ed_cfg.isEnabled() is False
    assert "config" not in payload
    assert payload["selected_config_ids"] == (101, 202)
    assert payload["manual_primary_config_id"] == 202


def test_apply_emits_structured_workorder_payload(qapp):
    module, services = _make_module()

    module.ed_sn.setText("SN-42")
    module.ed_model.setText("MODEL-X")
    _select_pair_with_primary(module, config_a=101, config_b=202, primary=202)

    module.apply()

    assert services.bus.workorder_changed.emissions
    payload = services.bus.workorder_changed.emissions[-1][0][0]
    assert payload["sn"] == "SN-42"
    assert payload["model"] == "MODEL-X"
    assert payload["selected_config_ids"] == (101, 202)
    assert payload["manual_primary_config_id"] == 202


def test_live_sn_model_edits_emit_structured_workorder_payload_without_apply(qapp):
    module, services = _make_module()

    _select_pair_with_primary(module, config_a=101, config_b=202, primary=202)
    services.bus.workorder_changed.emissions.clear()

    module.ed_sn.setText("SN-LIVE")
    module.ed_model.setText("MODEL-LIVE")

    assert services.bus.workorder_changed.emissions
    payload = services.bus.workorder_changed.emissions[-1][0][0]
    assert payload["sn"] == "SN-LIVE"
    assert payload["model"] == "MODEL-LIVE"
    assert payload["selected_config_ids"] == (101, 202)
    assert payload["manual_primary_config_id"] == 202


def test_init_preserves_controller_selection_without_emitting_selection_intent(qapp):
    initial_state = FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=202,
        run_phase="armed",
    )

    module, services = _make_module(initial_factory_state=initial_state)

    assert services.factory_mode.selection_calls == []
    assert module.combo_config_a.currentData() == 101
    assert module.combo_config_b.currentData() == 202
    assert module.combo_primary.currentData() == 202
    assert module.value()["selected_config_ids"] == (101, 202)
    assert module.value()["manual_primary_config_id"] == 202


def test_init_seeds_structured_workorder_payload_from_prearmed_controller_state(qapp):
    initial_state = FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=202,
        run_phase="armed",
    )

    _, services = _make_module(initial_factory_state=initial_state)

    assert getattr(services.bus, "current_workorder_payload", None) == {
        "sn": "",
        "model": "",
        "selected_config_ids": (101, 202),
        "manual_primary_config_id": 202,
    }


def test_reload_config_options_preserves_controller_selection_without_emitting_selection_intent(qapp):
    initial_state = FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=101,
        run_phase="armed",
    )
    module, services = _make_module(initial_factory_state=initial_state)
    services.factory_mode.selection_calls.clear()

    services.bus.config_changed.emit()

    assert services.factory_mode.selection_calls == []
    assert module.combo_config_a.currentData() == 101
    assert module.combo_config_b.currentData() == 202
    assert module.combo_primary.currentData() == 101
    assert module.value()["selected_config_ids"] == (101, 202)
    assert module.value()["manual_primary_config_id"] == 101


@pytest.mark.parametrize("run_phase", ["running", "analyzing", "reanalyzing"])
def test_reload_config_options_keeps_locked_controller_owned_selection_visible(qapp, run_phase):
    initial_state = FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=101,
        active_config_id=101,
        displayed_config_id=101,
        run_phase=run_phase,
        trigger_source="manual",
    )
    module, services = _make_module(initial_factory_state=initial_state)
    services.factory_mode.selection_calls.clear()
    services.config_service.set_listed_config_ids([202, 303])

    services.bus.config_changed.emit()
    module.apply()

    payload = services.bus.workorder_changed.emissions[-1][0][0]
    assert services.factory_mode.selection_calls == []
    assert module.combo_config_a.currentData() == 101
    assert module.combo_config_b.currentData() == 202
    assert module.combo_primary.currentData() == 101
    assert payload["selected_config_ids"] == (101, 202)
    assert payload["manual_primary_config_id"] == 101
    assert "Config Alpha" in module.lbl_factory_summary.text()


def test_deleted_locked_selection_is_reconciled_when_controller_unlocks_with_stale_state(qapp):
    initial_state = FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=101,
        active_config_id=101,
        displayed_config_id=101,
        run_phase="running",
        trigger_source="manual",
    )
    module, services = _make_module(initial_factory_state=initial_state)
    services.factory_mode.selection_calls.clear()
    services.config_service.delete_config(101)

    services.bus.config_changed.emit()

    assert services.factory_mode.selection_calls == []
    assert module.combo_config_a.currentData() == 101
    assert module.combo_config_b.currentData() == 202

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101, 202),
            manual_primary_config_id=101,
            run_phase="armed",
        )
    )

    assert services.factory_mode.selection_calls == [((202,), None)]
    assert module.combo_config_a.currentData() == 202
    assert module.combo_config_b.currentData() is None
    assert module.combo_primary.currentData() is None
    assert module.value()["selected_config_ids"] == (202,)
    assert module.value()["manual_primary_config_id"] is None
    assert "Config Alpha" not in module.lbl_factory_summary.text()


def test_reload_config_options_resubmits_selection_when_selected_config_disappears(qapp):
    initial_state = FactoryControllerState(
        selected_config_ids=(101, 202),
        manual_primary_config_id=101,
        run_phase="armed",
    )
    module, services = _make_module(initial_factory_state=initial_state)
    services.factory_mode.selection_calls.clear()
    services.config_service.delete_config(101)

    services.bus.config_changed.emit()

    assert services.factory_mode.selection_calls == [((202,), None)]
    assert module.combo_config_a.currentData() == 202
    assert module.combo_config_b.currentData() is None
    assert module.combo_primary.currentData() is None
    assert module.value()["selected_config_ids"] == (202,)
    assert module.value()["manual_primary_config_id"] is None


def test_same_id_config_refresh_reloads_controller_plc_metadata_in_reviewing(qapp):
    module, services = _make_module_with_real_factory_controller()
    _select_pair_with_primary(module, config_a=101, config_b=202, primary=101)
    _promote_controller_to_reviewing(
        services,
        displayed_config_id=101,
        trigger_source="manual",
    )

    services.config_service.update_config(
        202,
        plc_device_key="plc-b",
        plc_device_label="PLC B",
    )
    services.bus.config_changed.emit()

    accepted = services.factory_mode.handle_plc_event(
        FactoryPlcIngressPayload(plc_device_key="plc-b")
    )

    assert accepted is True
    services.test.start.assert_called_once()
    payload = services.test.start.call_args.args[0]
    assert payload["config_id"] == 202
    assert payload["trigger_source"] == "plc"


def test_same_id_config_refresh_updates_selection_status_in_reviewing(qapp):
    module, services = _make_module_with_real_factory_controller()
    _select_pair_with_primary(module, config_a=101, config_b=202, primary=101)
    _promote_controller_to_reviewing(
        services,
        displayed_config_id=101,
        trigger_source="manual",
    )

    assert "已就绪" in module.lbl_selection_status.text()

    services.config_service.set_runtime_errors(
        202,
        ["分析窗起始偏移加分析时长不能超过录音时长"],
    )
    services.bus.config_changed.emit()

    assert "已就绪" not in module.lbl_selection_status.text()
    assert "分析窗起始偏移加分析时长不能超过录音时长" in module.lbl_selection_status.text()
    assert "reviewing" in module.lbl_phase_summary.text().lower()


def test_factory_state_changed_syncs_selectors_without_emitting_selection_intent(qapp):
    module, services = _make_module()
    services.factory_mode.selection_calls.clear()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(202, 303),
            manual_primary_config_id=303,
            run_phase="armed",
            trigger_source="plc",
        )
    )

    assert services.factory_mode.selection_calls == []
    assert module.combo_config_a.currentData() == 202
    assert module.combo_config_b.currentData() == 303
    assert module.combo_primary.currentData() == 303
    assert module.value()["selected_config_ids"] == (202, 303)
    assert module.value()["manual_primary_config_id"] == 303


def test_factory_state_clear_resets_selectors_and_derived_selection_state(qapp):
    module, services = _make_module()
    _select_pair_with_primary(module, config_a=101, config_b=202, primary=101)
    services.factory_mode.selection_calls.clear()

    services.bus.factory_state_changed.emit(FactoryControllerState())

    assert services.factory_mode.selection_calls == []
    assert module.combo_config_a.currentData() is None
    assert module.combo_config_b.currentData() is None
    assert module.combo_primary.currentData() is None
    assert module.value()["selected_config_ids"] == ()
    assert module.value()["manual_primary_config_id"] is None
    assert module.ed_cfg.text() == "Factory mode selection controls"
    assert module.lbl_factory_summary.text() == "Factory summary: select Config A and Config B."
