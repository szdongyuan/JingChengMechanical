from __future__ import annotations

import pytest
import app.services_container as services_mod
from app.services_container import EventBus

from ._support import (
    _accept_start,
    _assert_log_contains,
    _assert_start_called_with,
    _emit_owned_runtime_start,
    _finish_active_run_to_reviewing,
    _load_factory_mode_controller_module,
    _make_config,
    _make_dynamic_services,
    _make_review_payload,
    _make_runtime_config_payload,
    _make_services,
    _run_id_for,
)


def _make_stopped_payload(
    *,
    config_id: int = 101,
    run_id: str | None = None,
    trigger_source: str = "manual",
):
    stopped_payload_cls = getattr(services_mod, "FactoryRuntimeStoppedPayload", None)

    assert stopped_payload_cls is not None, "FactoryRuntimeStoppedPayload is missing"

    return stopped_payload_cls(
        config_id=config_id,
        run_id=run_id or _run_id_for(config_id),
        trigger_source=trigger_source,
    )


def test_factory_mode_controller_tracks_explicit_plc_ingress_and_display_reset():
    controller_mod = _load_factory_mode_controller_module()
    plc_payload_cls = getattr(services_mod, "FactoryPlcIngressPayload", None)

    assert plc_payload_cls is not None, "FactoryPlcIngressPayload is missing"

    bus = EventBus()
    reset_reasons: list[str] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    controller = controller_mod.FactoryModeController(bus)

    bus.factory_plc_event.emit(plc_payload_cls(plc_id=11))
    controller.clear_display_state("selection_changed")

    assert controller.last_plc_ingress.plc_id == 11
    assert reset_reasons == ["selection_changed"]


@pytest.mark.parametrize(
    ("configs", "plc_id", "expected_config_id"),
    [
        (
            (
                _make_config(101, plc_id=11),
                _make_config(202, plc_id=22),
            ),
            22,
            202,
        ),
        (
            (
                _make_config(101, plc_id=11),
                _make_config(202, plc_id=11),
            ),
            11,
            101,
        ),
    ],
)
def test_factory_mode_controller_routes_plc_events_by_plc_id(
    configs,
    plc_id,
    expected_config_id,
):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(configs)
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202))

    accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=plc_id)
    )

    assert accepted is True
    _assert_start_called_with(
        services,
        {"config_id": expected_config_id},
        expected_trigger_source="plc",
    )
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "plc"


@pytest.mark.parametrize(
    ("configs", "expected_config_id"),
    [
        (
            (
                _make_config(101, scan_triggers_test=False),
                _make_config(202, scan_triggers_test=True),
            ),
            202,
        ),
        (
            (
                _make_config(101, scan_triggers_test=True),
                _make_config(202, scan_triggers_test=True),
            ),
            101,
        ),
    ],
)
def test_factory_mode_controller_routes_scan_events(configs, expected_config_id):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(configs)
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202))

    accepted = controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-1")
    )

    assert accepted is True
    _assert_start_called_with(
        services,
        {"config_id": expected_config_id},
        expected_trigger_source="scan",
    )
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "scan"


@pytest.mark.parametrize(
    ("trigger_kind", "expected_config_id"),
    [
        ("plc", 202),
        ("scan", 202),
    ],
)
def test_factory_mode_controller_routes_external_triggers_from_stopped_state(
    trigger_kind,
    expected_config_id,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=False),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id="run-1",
        trigger_source="manual",
    )
    controller.handle_runtime_stopped(
        _make_stopped_payload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    reset_reasons.clear()
    _accept_start(services)

    if trigger_kind == "plc":
        accepted = controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=22)
        )
    else:
        accepted = controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )

    assert accepted is True
    _assert_start_called_with(
        services,
        {"config_id": expected_config_id},
        expected_trigger_source=trigger_kind,
    )
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.displayed_config_id == expected_config_id
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == trigger_kind
    assert reset_reasons == ["next_run_accepted"]


def test_blocked_plc_and_scan_start_emit_factory_start_blocked_while_idle():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection(())

    assert (
        controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=11)
        )
        is False
    )
    assert (
        controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        is False
    )

    assert [payload.reason for payload in blocked_payloads] == [
        "请至少启用一个配置",
        "请至少启用一个配置",
    ]
    assert [payload.trigger_source for payload in blocked_payloads] == ["plc", "scan"]


def test_busy_external_triggers_do_not_emit_factory_start_blocked():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.restore_selection((101,), had_missing_configs=False)
    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )

    assert (
        controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=11)
        )
        is False
    )
    assert (
        controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        is False
    )

    assert blocked_payloads == []


def test_plc_trigger_uses_highest_priority_matching_config():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101, plc_id=11),
            _make_config(202, plc_id=11),
            _make_config(303, plc_id=22),
        )
    )
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((303, 202, 101))

    assert (
        controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=11)
        )
        is True
    )

    _assert_start_called_with(
        services,
        {"config_id": 202},
        expected_trigger_source="plc",
    )


def test_scan_trigger_uses_first_enabled_scan_triggers_test_config():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101, scan_triggers_test=True),
            _make_config(202, scan_triggers_test=False),
            _make_config(303, scan_triggers_test=True),
        )
    )
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((303, 202, 101))

    assert (
        controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        is True
    )

    _assert_start_called_with(
        services,
        {"config_id": 303},
        expected_trigger_source="scan",
    )


@pytest.mark.parametrize("phase", ["running", "analyzing"])
def test_factory_mode_controller_refreshes_selected_config_cache_after_plc_changed(
    phase,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    services = _make_dynamic_services(config_map)
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    assert controller.start_manual() is True
    services.test.start.reset_mock()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))

    config_map[202]["plc_id"] = 22
    bus.plc_changed.emit()

    _finish_active_run_to_reviewing(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )

    accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=22)
    )

    assert accepted is True
    _assert_start_called_with(
        services,
        {"config_id": 202},
        expected_trigger_source="plc",
    )
    assert controller.state.active_config_id == 202
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "plc"


@pytest.mark.parametrize("phase", ["running", "analyzing"])
def test_factory_mode_controller_applies_deferred_runtime_revalidation_after_active_phase(
    phase,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    services = _make_dynamic_services(
        config_map,
        validate_factory_runtime=lambda config: (
            ["analysis window invalid"]
            if config["analysis_window_offset_ms"] > 0
            else []
        ),
    )
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    assert controller.start_manual() is True
    services.test.start.reset_mock()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))

    config_map[202]["analysis_window_offset_ms"] = 4000
    bus.config_changed.emit()

    _finish_active_run_to_reviewing(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "analysis window invalid"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "analysis window invalid"
    assert controller.can_arm() is False
    assert controller.start_manual() is False
    services.test.start.assert_not_called()


def test_factory_mode_controller_ignores_plc_and_scan_before_armed():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    plc_accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=11)
    )
    scan_accepted = controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-1")
    )

    assert plc_accepted is False
    assert scan_accepted is False
    services.test.start.assert_not_called()
    assert controller.state.run_phase == "not_ready"


def test_factory_mode_controller_logs_ignored_plc_and_scan_before_armed():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    log_messages: list[str] = []
    bus.log.connect(log_messages.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)

    plc_accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=11)
    )
    scan_accepted = controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-1")
    )

    assert plc_accepted is False
    assert scan_accepted is False
    services.test.start.assert_not_called()
    _assert_log_contains(log_messages, "PLC", "ignored", "not armed")
    _assert_log_contains(log_messages, "scan", "ignored", "not armed")


@pytest.mark.parametrize("phase", ["running", "analyzing", "reanalyzing"])
def test_factory_mode_controller_logs_and_drops_plc_and_scan_during_active_phase(
    phase,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    log_messages: list[str] = []
    bus.log.connect(log_messages.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    assert controller.start_manual({"sn": "SN-1"}) is True
    active_run_id = _run_id_for(101)
    if phase == "analyzing":
        controller.handle_runtime_analyzing(
            _make_runtime_config_payload(config_id=101, run_id=active_run_id)
        )
    elif phase == "reanalyzing":
        _finish_active_run_to_reviewing(
            controller,
            bus,
            config_id=101,
            trigger_source="manual",
            run_id=active_run_id,
        )
        assert controller.begin_reanalysis(1.0, 3.0) is True

    services.test.start.reset_mock()
    log_messages.clear()

    plc_accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=22)
    )
    scan_accepted = controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-2")
    )

    assert plc_accepted is False
    assert scan_accepted is False
    services.test.start.assert_not_called()
    _assert_log_contains(log_messages, "PLC", "ignored", phase)
    _assert_log_contains(log_messages, "scan", "ignored", phase)

    if phase == "reanalyzing":
        controller.finish_reanalysis_success(
            _make_review_payload(
                displayed_config_id=101,
                run_id=active_run_id,
                trigger_source="manual",
            )
        )
    elif phase == "analyzing":
        _finish_active_run_to_reviewing(
            controller,
            bus,
            config_id=101,
            trigger_source="manual",
            run_id=active_run_id,
        )

    assert (
        controller.state.run_phase == "reviewing"
        if phase != "running"
        else controller.state.run_phase == "running"
    )
    services.test.start.assert_not_called()


@pytest.mark.parametrize(
    ("configs", "plc_id"),
    [
        (
            (
                _make_config(101, plc_id=11),
                _make_config(202, plc_id=22),
            ),
            999,
        ),
        (
            (
                _make_config(101),
                _make_config(202),
            ),
            11,
        ),
    ],
)
def test_factory_mode_controller_logs_ignored_plc_no_match_for_current_pair(
    configs,
    plc_id,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    log_messages: list[str] = []
    bus.log.connect(log_messages.append)
    services = _make_dynamic_services(
        {config["config_id"]: dict(config) for config in configs},
        validate_factory_runtime=lambda config: [],
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=plc_id)
    )

    assert controller.state.run_phase == "armed"
    assert accepted is False
    services.test.start.assert_not_called()
    _assert_log_contains(log_messages, "PLC", "ignored", "no match")


def test_factory_mode_controller_logs_ignored_scan_no_match_for_current_pair():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    log_messages: list[str] = []
    bus.log.connect(log_messages.append)
    services = _make_services(
        (
            _make_config(101, scan_triggers_test=False),
            _make_config(202, scan_triggers_test=False),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    accepted = controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-404")
    )

    assert controller.state.run_phase == "armed"
    assert accepted is False
    services.test.start.assert_not_called()
    _assert_log_contains(log_messages, "scan", "ignored", "no match")
