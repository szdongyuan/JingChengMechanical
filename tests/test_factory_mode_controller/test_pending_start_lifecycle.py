from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.service.factory_mode_controller import FactoryModeController
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


def test_imported_request_delegates_directly_without_review_state():
    events = []
    services, coordinator = _services(events)
    request = SimpleNamespace(run_id="imported")
    controller = FactoryModeController(EventBus(), services)

    assert controller.analyze_imported_audio(request) is True
    assert coordinator.requests == []
    assert coordinator.imported_requests == [request]


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
