from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.service.factory_mode_controller import FactoryModeController
from app.services_container import EventBus, FactoryControllerState
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
        self.factory_state_changed = _SignalStub()
        self.factory_selection_notice = _SignalStub()
        self.current_workorder_payload = {}


class _ConfigServiceStub:
    def __init__(
        self,
        configs: list[dict],
        *,
        runtime_errors_by_id: dict[int, list[str]] | None = None,
    ):
        self._configs = [dict(config) for config in configs]
        self._details = {config["config_id"]: dict(config) for config in configs}
        self._runtime_errors_by_id = {
            config_id: list(errors)
            for config_id, errors in (runtime_errors_by_id or {}).items()
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

    def validate_factory_runtime(self, config: dict):
        return list(self._runtime_errors_by_id.get(config["config_id"], ()))

    def delete_config(self, config_id: int) -> bool:
        if config_id not in self._details:
            return False
        self._details.pop(config_id, None)
        self._configs = [
            config for config in self._configs if config["config_id"] != config_id
        ]
        return True


class _SharedSettingsStub:
    def __init__(
        self,
        *,
        saved_selected_ids: tuple[int, ...] = (),
        save_errors: list[str | None] | None = None,
    ):
        self._saved_selected_ids = tuple(saved_selected_ids)
        self._save_errors = list(save_errors or [])
        self.save_calls: list[tuple[int, ...]] = []

    def load_selected_config_ids(self) -> tuple[int, ...]:
        return self._saved_selected_ids

    def save_selected_config_ids(self, config_ids: tuple[int, ...]) -> str | None:
        self.save_calls.append(tuple(config_ids))
        error = self._save_errors.pop(0) if self._save_errors else None
        if error:
            return error
        self._saved_selected_ids = tuple(config_ids)
        return None


class _FactoryModeStub:
    def __init__(self, initial_state: FactoryControllerState | None = None):
        self.state = initial_state or FactoryControllerState()
        self.restore_selection_calls: list[tuple[tuple[int, ...], bool]] = []
        self.set_selection_calls: list[tuple[int, ...]] = []

    def restore_selection(
        self,
        selected_config_ids: tuple[int, ...] | list[int],
        *,
        had_missing_configs: bool,
    ):
        selected = tuple(selected_config_ids)
        self.restore_selection_calls.append((selected, had_missing_configs))
        if had_missing_configs:
            self.state = FactoryControllerState(
                selected_config_ids=selected,
                run_phase="not_ready",
                selection_ready=False,
                selection_ready_reason="配置已失效",
                start_ready=False,
                start_ready_reason="配置已失效",
            )
        else:
            self.state = FactoryControllerState(
                selected_config_ids=selected,
                run_phase="armed" if selected else "not_ready",
            )
        return self.state

    def set_selection(self, selected_config_ids: tuple[int, ...] | list[int]):
        selected = tuple(selected_config_ids)
        self.set_selection_calls.append(selected)
        self.state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="armed" if selected else "not_ready",
        )
        return self.state


class _IdleTestManagerStub:
    @staticmethod
    def is_running():
        return False


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
    record_duration: float = 5.0,
    analysis_duration: float = 2.0,
    plc_device_key: str | None = None,
    plc_device_label: str | None = None,
    scan_triggers_test: bool = False,
    selected_channels: list[int] | None = None,
):
    return {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": record_duration,
        "analysis_duration": analysis_duration,
        "analysis_window_offset_ms": 0,
        "plc_device_key": plc_device_key,
        "plc_device_label": plc_device_label,
        "scan_triggers_test": scan_triggers_test,
        "selected_channels": list(selected_channels or []),
        "channels": [],
    }


def _make_module(
    *,
    saved_selected_ids: tuple[int, ...] = (),
    shared_settings: _SharedSettingsStub | None = None,
    initial_factory_state: FactoryControllerState | None = None,
    configs: list[dict] | None = None,
):
    bus = _BusStub()
    settings = shared_settings or _SharedSettingsStub(saved_selected_ids=saved_selected_ids)
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(
            configs
            or [
                _make_config(
                    101,
                    "Config Alpha",
                    plc_device_key="plc-a",
                    plc_device_label="PLC A",
                    selected_channels=[0, 1],
                ),
                _make_config(
                    202,
                    "Config Beta",
                    selected_channels=[1],
                ),
                _make_config(
                    303,
                    "Config Gamma",
                    scan_triggers_test=True,
                    selected_channels=[0],
                ),
            ]
        ),
        factory_selection_settings=settings,
        factory_mode=_FactoryModeStub(initial_factory_state),
    )
    return WorkOrderModule(services), services


def _make_real_controller_module(
    *,
    saved_selected_ids: tuple[int, ...] = (),
    shared_settings: _SharedSettingsStub | None = None,
    configs: list[dict] | None = None,
    runtime_errors_by_id: dict[int, list[str]] | None = None,
):
    bus = EventBus()
    settings = shared_settings or _SharedSettingsStub(saved_selected_ids=saved_selected_ids)
    services = SimpleNamespace(
        bus=bus,
        config_service=_ConfigServiceStub(
            configs
            or [
                _make_config(
                    101,
                    "Config Alpha",
                    plc_device_key="plc-a",
                    plc_device_label="PLC A",
                    selected_channels=[0, 1],
                ),
                _make_config(
                    202,
                    "Config Beta",
                    selected_channels=[1],
                ),
                _make_config(
                    303,
                    "Config Gamma",
                    scan_triggers_test=True,
                    selected_channels=[0],
                ),
            ],
            runtime_errors_by_id=runtime_errors_by_id,
        ),
        factory_selection_settings=settings,
        test=_IdleTestManagerStub(),
    )
    services.factory_mode = FactoryModeController(bus, services)
    return WorkOrderModule(services), services


def _workorder_rendered_text(module: WorkOrderModule) -> str:
    return "\n".join(
        (
            module.lbl_persistence_notice.text(),
            module.lbl_selection_status.text(),
            module.lbl_phase_summary.text(),
            module.lbl_trigger_summary.text(),
            module.lbl_factory_summary.text(),
        )
    )


def test_workorder_module_restores_saved_ids_prunes_missing_and_marks_invalid_selection(qapp):
    module, services = _make_module(saved_selected_ids=(303, 999, 101))

    assert module.value()["selected_config_ids"] == (303, 101)
    assert module.config_selector.visual_config_order() == (303, 101, 202)
    assert services.factory_mode.restore_selection_calls == [((303, 101), True)]
    assert services.factory_selection_settings.save_calls == []


def test_workorder_module_deduplicates_restored_saved_ids_before_syncing_controller(qapp):
    module, services = _make_module(saved_selected_ids=(101, 101, 202))

    assert module.value()["selected_config_ids"] == (101, 202)
    assert module.config_selector.visual_config_order() == (101, 202, 303)
    assert services.factory_mode.restore_selection_calls == [((101, 202), False)]


def test_workorder_module_only_renders_list_local_persistence_notice_not_controller_blockers(qapp):
    module, services = _make_module()

    assert module.lbl_factory_summary.text() == "Factory summary: 未启用工厂配置。"
    assert "请至少启用一个配置" not in _workorder_rendered_text(module)

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(),
            run_phase="not_ready",
            selection_ready=False,
            selection_ready_reason="请至少启用一个配置",
            start_ready=False,
            start_ready_reason="请至少启用一个配置",
        )
    )

    assert module.lbl_factory_summary.text() == "Factory summary: 未启用工厂配置。"
    assert "请至少启用一个配置" not in _workorder_rendered_text(module)

    module, services = _make_module(saved_selected_ids=(303, 999, 101))

    assert "配置已失效" not in _workorder_rendered_text(module)

    blocking_reason = "分析窗起始偏移加分析时长不能超过录音时长"
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(303, 101),
            run_phase="not_ready",
            selection_ready=False,
            selection_ready_reason=blocking_reason,
            start_ready=False,
            start_ready_reason=blocking_reason,
        )
    )

    rendered_text = _workorder_rendered_text(module)
    assert blocking_reason not in rendered_text
    assert module.lbl_persistence_notice.text() == ""

    services.bus.factory_selection_notice.emit("配置保存失败")

    assert module.lbl_persistence_notice.text() == "配置保存失败"


def test_first_user_edit_after_invalid_restore_clears_warning_and_overwrites_saved_selection(qapp):
    module, services = _make_module(saved_selected_ids=(303, 999, 101))

    module.config_selector.set_checked(202, True)

    assert services.factory_mode.set_selection_calls[-1] == (303, 101, 202)
    assert services.factory_selection_settings.save_calls[-1] == (303, 101, 202)
    assert services.factory_selection_settings.load_selected_config_ids() == (303, 101, 202)


def test_ordinary_user_edit_updates_controller_payload_and_persistence_immediately(qapp):
    module, services = _make_module()
    services.bus.workorder_changed.emissions.clear()

    module.config_selector.set_checked(101, True)

    assert services.factory_mode.set_selection_calls[-1] == (101,)
    assert services.factory_selection_settings.save_calls[-1] == (101,)
    payload = services.bus.workorder_changed.emissions[-1][0][0]
    assert payload == {
        "sn": "",
        "model": "",
        "selected_config_ids": (101,),
    }


def test_drag_reorder_user_edit_updates_controller_and_persistence_immediately(qapp):
    module, services = _make_module(saved_selected_ids=(101, 202, 303))
    services.factory_mode.set_selection_calls.clear()
    services.factory_selection_settings.save_calls.clear()

    module.config_selector.move_checked_config(303, before_config_id=101)

    assert module.value()["selected_config_ids"] == (303, 101, 202)
    assert services.factory_mode.set_selection_calls[-1] == (303, 101, 202)
    assert services.factory_selection_settings.save_calls[-1] == (303, 101, 202)


def test_controller_prune_sync_does_not_count_as_user_edit_or_repersist(qapp):
    module, services = _make_module(saved_selected_ids=(101, 202, 303))
    services.factory_mode.set_selection_calls.clear()
    services.factory_selection_settings.save_calls.clear()

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            selected_config_ids=(101, 303),
            run_phase="not_ready",
            selection_ready=False,
            selection_ready_reason="配置已失效",
            start_ready=False,
            start_ready_reason="配置已失效",
        )
    )

    assert module.value()["selected_config_ids"] == (101, 303)
    assert module.config_selector.visual_config_order() == (101, 303, 202)
    assert services.factory_mode.set_selection_calls == []
    assert services.factory_selection_settings.save_calls == []


def test_workorder_value_drops_manual_primary_and_apply_button(qapp):
    module, _ = _make_module()

    assert hasattr(module, "btn_apply") is False
    assert set(module.value()) == {"sn", "model", "selected_config_ids"}


def test_workorder_persistence_failure_keeps_live_selection_and_shows_non_blocking_feedback(qapp):
    settings = _SharedSettingsStub(save_errors=["配置保存失败"])
    module, services = _make_module(shared_settings=settings)

    module.config_selector.set_checked(101, True)

    assert module.value()["selected_config_ids"] == (101,)
    assert services.factory_selection_settings.load_selected_config_ids() == ()
    assert "配置保存失败" in module.lbl_persistence_notice.text()


def test_failed_save_after_prior_successful_selection_preserves_last_saved_restart_selection(qapp):
    settings = _SharedSettingsStub(
        saved_selected_ids=(303, 101),
        save_errors=["配置保存失败"],
    )
    first_module, first_services = _make_module(shared_settings=settings)

    first_module.config_selector.set_checked(202, True)

    assert first_module.value()["selected_config_ids"] == (303, 101, 202)
    assert first_services.factory_selection_settings.load_selected_config_ids() == (303, 101)

    second_module, second_services = _make_module(shared_settings=settings)

    assert second_module.value()["selected_config_ids"] == (303, 101)
    assert second_services.factory_mode.restore_selection_calls == [((303, 101), False)]


def test_successful_retry_clears_persistence_warning_after_prior_failure(qapp):
    settings = _SharedSettingsStub(save_errors=["配置保存失败", None])
    module, services = _make_module(shared_settings=settings)

    module.config_selector.set_checked(101, True)
    assert "配置保存失败" in module.lbl_persistence_notice.text()

    module.config_selector.set_checked(101, False)

    assert module.lbl_persistence_notice.text() == ""
    assert services.factory_selection_settings.load_selected_config_ids() == ()


def test_successful_user_save_clears_prior_controller_persistence_notice(qapp):
    module, _ = _make_module()

    module.services.bus.factory_selection_notice.emit("配置保存失败")
    assert "配置保存失败" in module.lbl_persistence_notice.text()

    module.config_selector.set_checked(101, True)

    assert module.lbl_persistence_notice.text() == ""
    assert module.services.bus.factory_selection_notice.emissions[-1][0][0] == ""


def test_real_controller_config_changed_prune_sync_updates_ui_without_second_persistence_write(qapp):
    module, services = _make_real_controller_module(saved_selected_ids=(101, 202, 303))
    services.factory_selection_settings.save_calls.clear()

    services.config_service.delete_config(202)
    services.bus.config_changed.emit()

    assert module.value()["selected_config_ids"] == (101, 303)
    assert module.config_selector.visual_config_order() == (101, 303)
    assert services.factory_mode.state.selected_config_ids == (101, 303)
    assert services.factory_mode.state.selection_ready_reason == "配置已失效"
    assert services.factory_selection_settings.save_calls == [(101, 303)]


def test_workorder_restore_keeps_sn_and_model_empty(qapp):
    module, _ = _make_module(saved_selected_ids=(303, 101))

    assert module.value() == {
        "sn": "",
        "model": "",
        "selected_config_ids": (303, 101),
    }
    assert module.ed_sn.text() == ""
    assert module.ed_model.text() == ""


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
def test_workorder_locks_checklist_during_busy_or_pending_start_states(qapp, state):
    module, services = _make_module(saved_selected_ids=(101,))

    services.bus.factory_state_changed.emit(state)

    assert module.config_selector.is_locked() is True
