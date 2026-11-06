from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QSettings

from app.service.factory_mode_controller import FactoryModeController
from app.service.factory_selection_settings import FactorySelectionSettings
from app.service.multi_run_coordinator_mvp.contracts import (
    CapturedStartRequest,
    FrozenPayload,
    StartStatus,
    TriggerSource,
)
from app.services_container import (
    EventBus,
    FactoryPlcIngressPayload,
    FactoryScanIngressPayload,
)
from tests.test_factory_mode_controller._support import (
    _make_config,
    _make_dynamic_services,
)


_PAYLOAD = FrozenPayload("payload", b"{}")


class _Coordinator:
    def __init__(self, events):
        self.events = events
        self.requests = []
        self.imported_requests = []

    def start_captured(self, request):
        self.events.append("coordinator")
        self.requests.append(request)
        return SimpleNamespace(status=StartStatus.ACCEPTED_ACTIVE)

    def start_imported(self, request):
        self.imported_requests.append(request)
        return SimpleNamespace(status=StartStatus.ACCEPTED_ACTIVE)


class _ConfigService:
    @staticmethod
    def load_config(config_id):
        return {
            "config_id": config_id,
            "plc_id": {101: 7, 202: 8}.get(config_id),
        }

    @staticmethod
    def validate_factory_runtime(_config):
        return ()

    @staticmethod
    def list_configs():
        return [{"config_id": 101}, {"config_id": 202}]


def _services(events):
    coordinator = _Coordinator(events)
    services = SimpleNamespace(
        config_service=_ConfigService(),
        multi_run_coordinator=coordinator,
        built_requests=[],
    )

    def build(**kwargs):
        events.append("build")
        services.built_requests.append(dict(kwargs))
        return CapturedStartRequest(
            run_id="12345678-1234-4234-8234-123456789abc",
            trigger_monotonic_ns=kwargs["trigger_monotonic_ns"],
            source=kwargs["source"],
            ingress_payload=_PAYLOAD,
            fallback_history_labels=_PAYLOAD,
            plc_id=kwargs["plc_id"],
        )

    services.build_captured_start_request = build
    return services, coordinator


class _SelectionSettingsSpy:
    def __init__(self, selected=(101,), pending=None):
        self.selected = tuple(selected)
        self.pending = pending
        self.persist_error = None
        self.pending_error = None
        self.persist_calls = []
        self.pending_calls = []
        self.reconcile_calls = []
        self.operation_order = []

    def load_selected_config_ids(self):
        return self.selected

    def load_pending_selected_config_ids(self):
        return self.pending

    def save_selected_config_ids(self, selected):
        selected = tuple(selected)
        self.persist_calls.append(selected)
        self.operation_order.append(("persist", selected))
        if self.persist_error is None:
            self.selected = selected
        return self.persist_error

    def save_pending_selected_config_ids(self, selected):
        selected = tuple(selected)
        self.pending_calls.append(selected)
        self.operation_order.append(("persist_pending", selected))
        if self.pending_error is None:
            self.pending = selected
        return self.pending_error

    def reconcile_for_startup(self, existing):
        existing = tuple(existing)
        self.reconcile_calls.append(existing)
        candidate = self.selected if self.pending is None else tuple(self.pending)
        seen = set()
        reconciled = tuple(
            config_id
            for config_id in candidate
            if config_id in existing
            and config_id not in seen
            and not seen.add(config_id)
        )
        if not reconciled and existing:
            reconciled = (existing[0],)
        if reconciled != self.selected:
            self.save_selected_config_ids(reconciled)
        self.pending = None
        return reconciled


class _DurablePendingSaveSettings(_SelectionSettingsSpy):
    def __init__(self, *, post_save_load_result):
        super().__init__()
        self.post_save_load_result = post_save_load_result
        self.durable_pending = None
        self.pending_load_calls = 0

    def load_pending_selected_config_ids(self):
        self.pending_load_calls += 1
        if self.durable_pending is None:
            return self.pending
        if isinstance(self.post_save_load_result, Exception):
            raise self.post_save_load_result
        return self.post_save_load_result

    def save_pending_selected_config_ids(self, selected):
        selected = tuple(selected)
        self.pending_calls.append(selected)
        self.operation_order.append(("persist_pending", selected))
        self.durable_pending = selected
        return None


class _PendingRollbackFailureSettings:
    Status = QSettings.Status

    def __init__(self, *, corrupt_after_first=False):
        self._key = FactorySelectionSettings.PENDING_SETTINGS_KEY
        self._backing = {self._key: '[101]'}
        self._pending = dict(self._backing)
        self._sync_calls = 0
        self._status = self.Status.NoError
        self._corrupt_after_first = corrupt_after_first

    def value(self, key, default=None):
        return self._backing.get(key, default)

    def setValue(self, key, value):
        self._pending[key] = value

    def remove(self, key):
        self._pending.pop(key, None)

    def sync(self):
        self._sync_calls += 1
        self._status = self.Status.AccessError
        if self._sync_calls == 1:
            self._backing.clear()
            self._backing.update(self._pending)
        elif self._corrupt_after_first:
            self._backing[self._key] = "not-json"

    def status(self):
        return self._status


def _selection_controller(*, locked=False, prepared=(101,)):
    bus = EventBus()
    settings = _SelectionSettingsSpy()
    reset_calls = []

    def request_resets(selected):
        selected = tuple(selected)
        reset_calls.append(selected)
        settings.operation_order.append(("reset", selected))

    services = SimpleNamespace(
        production_selection_locked=locked,
        config_service=_ConfigService(),
        factory_selection_settings=settings,
        request_plc_start_signal_resets_for_configs=request_resets,
    )
    controller = FactoryModeController(
        bus,
        services,
        prepared_selected_config_ids=prepared if locked else (),
        prepared_config_routes=(
            tuple((config_id, None) for config_id in prepared)
            if locked
            else ()
        ),
    )
    if not locked:
        controller.set_selection(prepared)
    return controller, services, settings, reset_calls, bus


def test_confirm_selection_persists_before_runtime_state_and_resets_only_added_configs():
    controller, _services, settings, reset_calls, bus = _selection_controller()
    state_events = []
    bus.factory_state_changed.connect(
        lambda state: (
            settings.operation_order.append(("state", state.selected_config_ids)),
            state_events.append(state),
        )
    )

    result = controller.confirm_selection((101, 202))

    assert result.status == "applied"
    assert result.selected_config_ids == (101, 202)
    assert reset_calls == [(202,)]
    assert settings.operation_order == [
        ("persist", (101, 202)),
        ("reset", (202,)),
        ("state", (101, 202)),
    ]
    assert len(state_events) == 1


def test_confirm_selection_rejects_persistence_failure_without_runtime_side_effects():
    controller, _services, settings, reset_calls, bus = _selection_controller()
    settings.persist_error = "配置保存失败"
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection((202,))

    assert result.status == "rejected"
    assert result.message == "配置保存失败"
    assert controller.state.selected_config_ids == (101,)
    assert reset_calls == []
    assert state_events == []


def test_confirm_selection_outside_prepared_routes_returns_restart_required():
    controller, _services, settings, reset_calls, _bus = _selection_controller(
        locked=True
    )

    result = controller.confirm_selection((202,))

    assert result.status == "restart_required"
    assert settings.load_pending_selected_config_ids() == (202,)
    assert controller.state.selected_config_ids == (202,)
    assert controller.state.start_ready is False
    assert reset_calls == [(202,)]


def test_confirm_selection_pending_restart_resets_before_state_publication():
    controller, _services, settings, _reset_calls, bus = _selection_controller(
        locked=True
    )
    bus.factory_state_changed.connect(
        lambda state: settings.operation_order.append(
            ("state", state.selected_config_ids)
        )
    )

    result = controller.confirm_selection((202,))

    assert result.status == "restart_required"
    assert settings.operation_order == [
        ("persist_pending", (202,)),
        ("reset", (202,)),
        ("state", (202,)),
    ]


@pytest.mark.parametrize(
    "post_save_load_result",
    [RuntimeError("readback failed"), (101,)],
    ids=["readback-raises", "readback-mismatches"],
)
def test_confirm_selection_trusts_verified_pending_save_without_readback(
    post_save_load_result,
):
    controller, services, _settings, reset_calls, bus = _selection_controller(
        locked=True
    )
    settings = _DurablePendingSaveSettings(
        post_save_load_result=post_save_load_result
    )
    services.factory_selection_settings = settings
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection((202,))

    assert settings.durable_pending == (202,)
    assert settings.pending_load_calls == 0
    assert result.status == "restart_required"
    assert result.selected_config_ids == (202,)
    assert controller.state.selected_config_ids == (202,)
    assert reset_calls == [(202,)]
    assert len(state_events) == 1


def test_confirm_selection_pending_write_failure_has_no_runtime_or_workorder_side_effects():
    controller, _services, settings, reset_calls, bus = _selection_controller(
        locked=True
    )
    settings.pending_error = "下次启动配置保存失败"
    previous_state = controller.state
    previous_pending = settings.load_pending_selected_config_ids()
    state_events = []
    workorder_events = []
    bus.factory_state_changed.connect(state_events.append)
    bus.workorder_changed.connect(workorder_events.append)

    result = controller.confirm_selection((202,))

    assert result.status == "rejected"
    assert result.message == "下次启动配置保存失败"
    assert controller.state is previous_state
    assert settings.load_pending_selected_config_ids() == previous_pending
    assert state_events == []
    assert workorder_events == []
    assert reset_calls == []


def test_confirm_selection_does_not_reject_durable_pending_after_failed_rollback():
    bus = EventBus()
    raw = _PendingRollbackFailureSettings()
    settings = FactorySelectionSettings(settings=raw)
    reset_calls = []
    state_events = []
    services = SimpleNamespace(
        production_selection_locked=True,
        config_service=_ConfigService(),
        factory_selection_settings=settings,
        request_plc_start_signal_resets_for_configs=lambda selected: (
            reset_calls.append(tuple(selected))
        ),
    )
    controller = FactoryModeController(
        bus,
        services,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, None),),
    )
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection((202,))

    assert result.status != "rejected"
    assert result.status == "restart_required"
    assert settings.load_pending_selected_config_ids() == (202,)
    assert controller.state.selected_config_ids == (202,)
    assert reset_calls == [(202,)]
    assert len(state_events) == 1


def test_confirm_selection_blocks_factory_when_pending_durability_is_indeterminate():
    bus = EventBus()
    settings = FactorySelectionSettings(
        settings=_PendingRollbackFailureSettings(corrupt_after_first=True)
    )
    notices = []
    state_events = []
    blocked = []
    reset_calls = []
    bus.factory_selection_notice.connect(notices.append)
    bus.factory_state_changed.connect(state_events.append)
    bus.factory_start_blocked.connect(blocked.append)
    services = SimpleNamespace(
        production_selection_locked=True,
        config_service=_ConfigService(),
        factory_selection_settings=settings,
        request_plc_start_signal_resets_for_configs=lambda selected: (
            reset_calls.append(tuple(selected))
        ),
    )
    controller = FactoryModeController(
        bus,
        services,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, None),),
    )

    result = controller.confirm_selection((202,))

    assert result.status == "indeterminate"
    assert "状态不确定" in result.message
    assert controller.selection_persistence_indeterminate is True
    assert controller.state.selected_config_ids == (101,)
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == result.message
    assert notices == [result.message]
    assert len(state_events) == 1
    assert reset_calls == []

    assert controller.start_manual() is False
    assert blocked[-1].reason == result.message
    rejected = controller.confirm_selection((101,))
    assert rejected.status == "rejected"
    assert rejected.message == result.message


def test_internal_pending_selection_blocks_factory_on_indeterminate_write():
    bus = EventBus()
    raw = _PendingRollbackFailureSettings(corrupt_after_first=True)
    settings = FactorySelectionSettings(settings=raw)
    notices = []
    state_events = []
    bus.factory_selection_notice.connect(notices.append)
    bus.factory_state_changed.connect(state_events.append)
    services = SimpleNamespace(
        production_selection_locked=True,
        config_service=_ConfigService(),
        factory_selection_settings=settings,
        request_plc_start_signal_resets_for_configs=lambda _selected: None,
    )
    controller = FactoryModeController(
        bus,
        services,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, None),),
    )

    state = controller.set_selection((202,))

    assert state is controller.state
    assert state.selected_config_ids == (101,)
    assert state.start_ready is False
    assert "状态不确定" in state.start_ready_reason
    assert controller.selection_persistence_indeterminate is True
    assert notices == [state.start_ready_reason]
    assert len(state_events) == 1
    assert controller.can_arm() is False

    blocked_state = controller.set_selection((101,))
    assert blocked_state is state
    assert notices[-1] == state.start_ready_reason

    sync_calls = raw._sync_calls
    refreshed_state = controller.handle_configuration_changed()
    assert refreshed_state is state
    assert raw._sync_calls == sync_calls
    assert notices[-1] == state.start_ready_reason


def test_confirm_selection_unchanged_has_no_side_effects():
    controller, _services, settings, reset_calls, bus = _selection_controller()
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection((101,))

    assert result.status == "unchanged"
    assert settings.persist_calls == []
    assert reset_calls == []
    assert state_events == []


@pytest.mark.parametrize(
    "selected",
    [(), (101, 101), (0,), (-1,), (303,), (True,)],
)
def test_confirm_selection_rejects_invalid_selection(selected):
    controller, _services, settings, reset_calls, bus = _selection_controller()
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection(selected)

    assert result.status == "rejected"
    assert result.message == "配置选择无效"
    assert result.selected_config_ids == (101,)
    assert settings.persist_calls == []
    assert reset_calls == []
    assert state_events == []


@pytest.mark.parametrize("phase", ["running", "analyzing", "reanalyzing"])
def test_confirm_selection_rejects_busy_phases(phase):
    controller, _services, settings, reset_calls, bus = _selection_controller()
    controller._state = replace(controller.state, run_phase=phase)
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection((202,))

    assert result.status == "rejected"
    assert result.message
    assert settings.persist_calls == []
    assert reset_calls == []
    assert state_events == []


def test_confirm_selection_rejects_while_start_request_is_pending():
    controller, _services, settings, reset_calls, bus = _selection_controller()
    controller._state = replace(
        controller.state,
        start_ready=False,
        start_ready_reason="启动请求处理中",
    )
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    result = controller.confirm_selection((202,))

    assert result.status == "rejected"
    assert result.message == "启动请求处理中"
    assert settings.persist_calls == []
    assert reset_calls == []
    assert state_events == []


def test_initialize_selection_once_resets_prepared_selection_once():
    controller, _services, settings, reset_calls, bus = _selection_controller(
        locked=True,
        prepared=(101, 202),
    )
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    first = controller.initialize_selection()
    second = controller.initialize_selection()

    assert first.selected_config_ids == (101, 202)
    assert second is first
    assert reset_calls == [(101, 202)]
    assert settings.persist_calls == []
    assert state_events == []


def test_initialize_selection_once_reconciles_unprepared_selection_once():
    controller, services, settings, reset_calls, bus = _selection_controller(
        prepared=()
    )
    settings.selected = (202, 202, 303)
    services.config_service = _ConfigService()
    state_events = []
    bus.factory_state_changed.connect(state_events.append)

    first = controller.initialize_selection()
    second = controller.initialize_selection()

    assert first.selected_config_ids == (202,)
    assert second is first
    assert settings.reconcile_calls == [(101, 202)]
    assert settings.persist_calls == [(202,)]
    assert reset_calls == [(202,)]
    assert len(state_events) == 1


def test_manual_start_samples_once_as_first_valid_action():
    events = []
    services, coordinator = _services(events)

    def monotonic_ns():
        events.append("sample")
        return 444_555

    controller = FactoryModeController(
        EventBus(),
        services,
        monotonic_ns=monotonic_ns,
    )
    controller.set_selection((101,))

    assert controller.start_manual({"sn": "SN-1"}) is True
    assert events == ["sample", "build", "coordinator"]
    assert coordinator.requests[0].trigger_monotonic_ns == 444_555
    assert coordinator.requests[0].source is TriggerSource.MANUAL


def test_manual_start_uses_nonblocking_service_submission_when_available():
    events = []
    services, coordinator = _services(events)
    submitted = []

    def submit(**kwargs):
        submitted.append(kwargs)
        return True

    services.submit_captured_start = submit
    controller = FactoryModeController(
        EventBus(),
        services,
        monotonic_ns=lambda: 555_666,
    )
    controller.set_selection((101,))

    assert controller.start_manual({"sn": "SN-ASYNC"}) is True
    assert coordinator.requests == []
    assert services.built_requests == []
    assert submitted == [
        {
            "source": TriggerSource.MANUAL,
            "trigger_monotonic_ns": 555_666,
            "workorder": {"sn": "SN-ASYNC"},
            "plc_id": None,
            "config_id": 101,
        }
    ]


def test_prepared_selection_initializes_armed_without_live_reread_and_stays_leased():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True

    class NoReread:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected live config reread: {name}")

    services.config_service = NoReread()
    bus = EventBus()
    notices = []
    bus.factory_selection_notice.connect(notices.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, 7),),
    )

    assert controller.state.selected_config_ids == (101,)
    assert controller.state.run_phase == "armed"
    assert controller.start_manual() is True
    assert controller.handle_scan_event(
        FactoryScanIngressPayload("SN", 701)
    ) is True
    assert controller.handle_plc_event(
        FactoryPlcIngressPayload(plc_id=7, trigger_monotonic_ns=702)
    ) is True
    original = controller.state
    assert controller.set_selection((202,)) is original
    assert controller.restore_selection((202,), had_missing_configs=False) is original
    assert controller.refresh_selection() is original
    assert notices == [
        "当前测试使用的配置选择已固定，暂时无法更改。",
        "当前测试使用的配置选择已固定，暂时无法更改。",
    ]
    assert len(coordinator.requests) == 3
    assert [
        item["config_id"] for item in services.built_requests
    ] == [101, 101, 101]
    assert coordinator.requests[0].trigger_monotonic_ns == 700
    assert coordinator.requests[0].source is TriggerSource.MANUAL


def test_multi_config_manual_uses_first_prepared_route():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    logs = []
    bus.factory_selection_notice.connect(notices.append)
    bus.log.connect(logs.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(202, 101),
        prepared_config_routes=((202, 8), (101, 7)),
    )

    assert controller.start_manual() is True

    assert notices == []
    assert logs == []
    assert [item["config_id"] for item in services.built_requests] == [202]
    assert len(coordinator.requests) == 1


def test_locked_manual_uses_first_live_selected_route_after_reorder():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    controller = FactoryModeController(
        EventBus(),
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101, 202),
        prepared_config_routes=((101, 7), (202, 8)),
    )

    controller.set_selection((202, 101))

    assert controller.start_manual() is True
    assert [item["config_id"] for item in services.built_requests] == [202]
    assert len(coordinator.requests) == 1


def test_locked_shared_plc_uses_first_live_selected_matching_route():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    controller = FactoryModeController(
        EventBus(),
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101, 202),
        prepared_config_routes=((101, 7), (202, 7)),
    )

    controller.set_selection((202, 101))

    assert controller.handle_plc_event(
        FactoryPlcIngressPayload(plc_id=7, trigger_monotonic_ns=702)
    ) is True
    assert [item["config_id"] for item in services.built_requests] == [202]
    assert len(coordinator.requests) == 1


def test_locked_missing_first_live_route_is_unavailable_without_fallback():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    logs = []
    bus.factory_selection_notice.connect(notices.append)
    bus.log.connect(logs.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101, 202),
        prepared_config_routes=((101, 7), (202, 8)),
    )
    controller._prepared_config_routes = ((202, 8),)

    assert controller.start_manual() is False
    assert len(notices) == 1
    assert logs == notices
    assert services.built_requests == []
    assert coordinator.requests == []


def test_locked_prepared_selection_can_toggle_and_routes_selected_subset():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    bus.factory_selection_notice.connect(notices.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101, 202),
        prepared_config_routes=((101, 7), (202, 8)),
    )

    state = controller.set_selection((202,))
    assert state.selected_config_ids == (202,)
    assert state.start_ready is True
    assert controller.start_manual() is True
    assert services.built_requests[-1]["config_id"] == 202

    state = controller.set_selection(())
    assert state.selected_config_ids == ()
    assert state.start_ready is False
    assert controller.start_manual() is False

    state = controller.set_selection((101, 202))
    assert state.start_ready is True
    assert controller.start_manual() is True
    assert services.built_requests[-1]["config_id"] == 101
    assert notices == []
    assert len(coordinator.requests) == 2


def test_locked_config_change_stages_first_existing_config_and_blocks_start():
    config_map = {
        101: _make_config(101, plc_id=7),
    }
    services = _make_dynamic_services(config_map)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    blocked = []
    bus.factory_selection_notice.connect(notices.append)
    bus.factory_start_blocked.connect(blocked.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, 7),),
    )

    config_map.clear()
    config_map[202] = _make_config(202, plc_id=8)
    config_map[303] = _make_config(303, plc_id=9)
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (202,)
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置已变更，请重启软件后生效。"
    assert controller.selection_restart_required is True
    assert (
        services.factory_selection_settings.load_pending_selected_config_ids()
        == (202,)
    )
    assert controller.start_manual() is False
    assert blocked[-1].reason == "配置已变更，请重启软件后生效。"
    assert notices[-1] == "配置已变更，请重启软件后生效。"


def test_locked_selection_outside_prepared_routes_is_saved_for_next_start():
    config_map = {
        101: _make_config(101, plc_id=7),
        202: _make_config(202, plc_id=8),
    }
    services = _make_dynamic_services(config_map)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    bus.factory_selection_notice.connect(notices.append)
    controller = FactoryModeController(
        bus,
        services,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, 7),),
    )

    state = controller.set_selection((202,))

    assert state.selected_config_ids == (202,)
    assert state.start_ready is False
    assert state.start_ready_reason == "配置已变更，请重启软件后生效。"
    assert (
        services.factory_selection_settings.load_pending_selected_config_ids()
        == (202,)
    )
    assert notices == ["配置已变更，请重启软件后生效。"]


def test_multi_config_scanner_rejects_once_without_guess_or_coordinator():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    logs = []
    bus.factory_selection_notice.connect(notices.append)
    bus.log.connect(logs.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101, 202),
        prepared_config_routes=((101, 7), (202, 8)),
    )

    assert controller.handle_scan_event(
        FactoryScanIngressPayload("SN", 701)
    ) is False

    assert len(notices) == 1
    assert logs == notices
    assert services.built_requests == []
    assert coordinator.requests == []


def test_unbound_positive_plc_ingress_is_silently_ignored():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    logs = []
    bus.factory_selection_notice.connect(notices.append)
    bus.log.connect(logs.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101, 202),
        prepared_config_routes=((101, 7), (202, 8)),
    )

    assert controller.handle_plc_event(
        FactoryPlcIngressPayload(
            plc_id=9,
            trigger_monotonic_ns=702,
        )
    ) is False
    assert notices == []
    assert logs == []
    assert services.built_requests == []
    assert coordinator.requests == []


def test_invalid_plc_identifier_is_rejected_without_building_a_request():
    events = []
    services, coordinator = _services(events)
    services.production_selection_locked = True
    bus = EventBus()
    notices = []
    logs = []
    bus.factory_selection_notice.connect(notices.append)
    bus.log.connect(logs.append)
    controller = FactoryModeController(
        bus,
        services,
        monotonic_ns=lambda: 700,
        prepared_selected_config_ids=(101,),
        prepared_config_routes=((101, 1),),
    )

    assert controller.handle_plc_event(
        FactoryPlcIngressPayload(
            plc_id=True,
            trigger_monotonic_ns=702,
        )
    ) is False
    assert len(notices) == 1
    assert logs == notices
    assert services.built_requests == []
    assert coordinator.requests == []


def test_plc_and_scanner_preserve_producer_timestamp_without_resampling():
    events = []
    services, coordinator = _services(events)
    controller = FactoryModeController(
        EventBus(),
        services,
        monotonic_ns=lambda: (_ for _ in ()).throw(
            AssertionError("non-manual ingress must not resample")
        ),
    )
    controller.set_selection((101,))

    assert controller.handle_plc_event(
        FactoryPlcIngressPayload(
            plc_id=7,
            trigger_monotonic_ns=111,
            sn="PLC-SN",
        )
    )
    assert controller.handle_scan_event(
        FactoryScanIngressPayload(
            scan_text="SCAN-SN",
            trigger_monotonic_ns=222,
        )
    )

    assert [request.trigger_monotonic_ns for request in coordinator.requests] == [
        111,
        222,
    ]
    assert [request.source for request in coordinator.requests] == [
        TriggerSource.PLC,
        TriggerSource.SCANNER,
    ]


def test_invalid_manual_timestamp_fails_closed_before_request_build():
    events = []
    services, coordinator = _services(events)
    controller = FactoryModeController(
        EventBus(),
        services,
        monotonic_ns=lambda: True,
    )
    controller.set_selection((101,))

    assert controller.start_manual() is False
    assert events == []
    assert coordinator.requests == []


def test_selection_readiness_is_ui_only_and_has_no_run_ownership():
    services, _coordinator = _services([])
    controller = FactoryModeController(EventBus(), services)

    assert controller.set_selection((101, 202)).start_ready is True
    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.run_phase == "armed"
    assert controller.set_selection(()).start_ready is False
    assert controller.state.run_phase == "not_ready"


def test_services_test_pending_and_single_active_config_authorities_are_absent():
    source = Path(FactoryModeController.__module__.replace(".", "/") + ".py")
    repo_source = Path(__file__).resolve().parents[2] / source
    text = repo_source.read_text(encoding="utf-8")

    assert "services.test" not in text
    assert "_pending_start" not in text
    assert "_owned_runtime_config_id" not in text
    assert "_owned_runtime_run_id" not in text
    assert "_review_payload" not in text


def test_prepare_holds_selection_lease_without_warning_on_background_refresh():
    events = []
    services, _coordinator = _services(events)
    services.production_selection_locked = True
    lease_notice = "当前测试使用的配置选择已固定，暂时无法更改。"
    services.factory_selection_settings = SimpleNamespace(
        save_selected_config_ids=lambda _value: lease_notice
    )
    bus = EventBus()
    notices = []
    bus.factory_selection_notice.connect(notices.append)
    controller = FactoryModeController(bus, services)

    original = controller.state
    assert controller.set_selection((101,)) is original
    assert controller.restore_selection(
        (101,),
        had_missing_configs=False,
    ) is original
    assert controller.refresh_selection() is original
    bus.config_changed.emit()
    assert services.factory_selection_settings.save_selected_config_ids((101,))
    assert notices == [lease_notice] * 2


def test_controller_starts_not_ready_without_selection():
    services, _coordinator = _services([])
    controller = FactoryModeController(EventBus(), services)

    assert controller.state.selected_config_ids == ()
    assert controller.state.selection_ready is False
    assert controller.state.start_ready is False
    assert controller.state.run_phase == "not_ready"


def test_zero_selection_blocks_and_emits_one_reason():
    services, coordinator = _services([])
    bus = EventBus()
    blocked = []
    bus.factory_start_blocked.connect(blocked.append)
    controller = FactoryModeController(bus, services)

    assert controller.start_manual() is False
    assert len(blocked) == 1
    assert blocked[0].trigger_source == "manual"
    assert blocked[0].reason
    assert coordinator.requests == []


def test_validation_uses_first_error_in_ordered_selection():
    events = []
    services, _coordinator = _services(events)
    services.config_service.validate_factory_runtime = (
        lambda config: [f"invalid-{config['config_id']}"]
    )
    controller = FactoryModeController(EventBus(), services)

    state = controller.set_selection((202, 101))

    assert state.start_ready is False
    assert state.start_ready_reason == "invalid-202"


def test_config_load_failure_blocks_without_partial_selection():
    services, _coordinator = _services([])
    services.config_service.load_config = lambda config_id: (
        (_ for _ in ()).throw(RuntimeError("missing"))
        if config_id == 202
        else {"config_id": config_id}
    )
    controller = FactoryModeController(EventBus(), services)

    state = controller.set_selection((101, 202))

    assert state.selected_config_ids == (101, 202)
    assert state.start_ready is False
    assert state.start_ready_reason == "配置加载失败"


def test_restore_with_missing_config_stays_blocked_until_user_selection():
    services, _coordinator = _services([])
    controller = FactoryModeController(EventBus(), services)

    restored = controller.restore_selection(
        (101,),
        had_missing_configs=True,
    )
    unchanged = controller.refresh_selection()
    edited = controller.set_selection((101,))

    assert restored.start_ready is False
    assert unchanged.start_ready is False
    assert edited.start_ready is True


def test_config_change_prunes_missing_selection_and_persists():
    services, _coordinator = _services([])
    saved = []
    services.factory_selection_settings = SimpleNamespace(
        save_selected_config_ids=lambda value: saved.append(value)
    )
    services.config_service.list_configs = lambda: [{"config_id": 101}]
    controller = FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202))

    state = controller.refresh_selection()

    assert state.selected_config_ids == (101,)
    assert state.start_ready is False
    assert saved == [(101,)]


def test_config_change_reload_recovers_previous_load_failure():
    services, _coordinator = _services([])
    failing = True

    def load(config_id):
        if failing:
            raise RuntimeError("temporary")
        return {"config_id": config_id}

    services.config_service.load_config = load
    controller = FactoryModeController(EventBus(), services)
    assert controller.set_selection((101,)).start_ready is False
    failing = False

    assert controller.refresh_selection().start_ready is True


def test_blocked_plc_and_scanner_do_not_submit_requests():
    services, coordinator = _services([])
    controller = FactoryModeController(EventBus(), services)

    assert controller.handle_plc_event(
        FactoryPlcIngressPayload(
            plc_id=1,
            trigger_monotonic_ns=10,
        )
    ) is False
    assert controller.handle_scan_event(
        FactoryScanIngressPayload(
            scan_text="SN",
            trigger_monotonic_ns=20,
        )
    ) is False
    assert coordinator.requests == []


def test_import_route_is_owned_by_offline_service():
    events = []
    services, coordinator = _services(events)
    controller = FactoryModeController(EventBus(), services)

    assert not hasattr(controller, "analyze_imported_audio")
    assert coordinator.requests == []
    assert coordinator.imported_requests == []


def test_result_operations_are_not_reacquired_by_ui_readiness_adapter():
    services, _coordinator = _services([])
    controller = FactoryModeController(EventBus(), services)

    assert controller.apply_manual_judgment("OK").success is False
    assert controller.begin_reanalysis(0.0, 1.0) is False
    assert controller.stop_active_run() is False
    assert controller.review_payload == __import__(
        "app.services_container",
        fromlist=["FactoryReviewPayload"],
    ).FactoryReviewPayload()
