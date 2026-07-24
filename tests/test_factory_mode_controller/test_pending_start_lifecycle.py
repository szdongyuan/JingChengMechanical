from __future__ import annotations

import pytest
import app.services_container as services_mod
from app.services_container import EventBus

from ._support import (
    _accept_start,
    _assert_start_called_with,
    _emit_owned_runtime_start,
    _enter_reviewing_state,
    _finish_active_run_to_reviewing,
    _load_factory_mode_controller_module,
    _make_abort_payload,
    _make_config,
    _make_dynamic_services,
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


def test_plc_testing_state_is_true_only_after_backend_accepts_plc_start():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    testing_states: list[tuple[int, bool]] = []
    callbacks: list[object] = []
    bus.write_plc_testing_state_sign.connect(
        lambda plc_id, testing: testing_states.append((plc_id, testing))
    )
    services = _make_services((_make_config(101, plc_id=22),))

    def _capture_start(_payload, *, on_start_accepted=None):
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101,))

    assert controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=22)
    ) is True
    assert testing_states == []

    callbacks[0](_run_id_for(101))

    assert testing_states == [(22, True)]


@pytest.mark.parametrize(
    "failure_mode",
    ["no_selected_config", "validation", "backend_false", "backend_error"],
)
def test_plc_testing_state_is_not_set_for_rejected_plc_start(failure_mode):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    testing_states: list[tuple[int, bool]] = []
    bus.write_plc_testing_state_sign.connect(
        lambda plc_id, testing: testing_states.append((plc_id, testing))
    )
    services = _make_services(
        (_make_config(101, plc_id=22),),
        validation_errors={101: ["invalid"]} if failure_mode == "validation" else None,
    )
    if failure_mode == "backend_false":
        services.test.start.return_value = False
    elif failure_mode == "backend_error":
        services.test.start.side_effect = RuntimeError("backend exploded")
    controller = controller_mod.FactoryModeController(bus, services)
    if failure_mode != "no_selected_config":
        controller.set_selection((101,))

    assert controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_id=22)
    ) is False
    assert testing_states == []


@pytest.mark.parametrize("trigger_source", ["manual", "scan"])
def test_plc_testing_state_is_not_set_for_accepted_non_plc_start(trigger_source):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    testing_states: list[tuple[int, bool]] = []
    bus.write_plc_testing_state_sign.connect(
        lambda plc_id, testing: testing_states.append((plc_id, testing))
    )
    services = _make_services((_make_config(101, scan_triggers_test=True),))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101,))

    if trigger_source == "manual":
        accepted = controller.start_manual()
    else:
        accepted = controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )

    assert accepted is True
    assert testing_states == []


def test_factory_mode_controller_ignores_out_of_band_factory_runtime_start_acceptance():
    controller_mod = _load_factory_mode_controller_module()
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)

    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"

    bus = EventBus()
    states: list[object] = []
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202))

    bus.factory_runtime_start_accepted.emit(
        runtime_start_cls(
            config_id=202,
            run_id=_run_id_for(202),
            trigger_source="plc",
        )
    )

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert states == [controller.state]


def test_runtime_start_accepted_clears_review_context_before_running():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    reset_reasons: list[str] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus)

    reviews.clear()
    reset_reasons.clear()
    states.clear()

    _emit_owned_runtime_start(controller, bus, config_id=202, trigger_source="plc")

    assert controller.review_payload == review_payload_cls()
    assert len(reset_reasons) == 1
    assert reviews == [review_payload_cls()]
    assert len(states) == 2
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[1].active_config_id == 202
    assert states[1].displayed_config_id == 202
    assert states[1].run_phase == "running"
    assert states[1].trigger_source == "plc"


def test_newly_accepted_run_clears_stopped_display_context_via_next_run_accepted():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
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
    reviews.clear()

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=202,
        run_id="run-2",
        trigger_source="plc",
    )

    assert controller.state.active_config_id == 202
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "plc"
    assert reset_reasons == ["next_run_accepted"]
    assert reviews == [services_mod.FactoryReviewPayload()]


def test_start_manual_from_stopped_accepts_next_run_and_clears_preserved_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    services = _make_services((_make_config(101), _make_config(202)))
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

    started = controller.start_manual()

    assert started is True
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert reset_reasons == ["next_run_accepted"]


def test_manual_start_uses_first_enabled_config_id():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202), _make_config(303)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((303, 202, 101))

    accepted = controller.start_manual({"sn": "SN-1", "model": "MODEL-1"})

    assert accepted is True
    _assert_start_called_with(
        services,
        {"sn": "SN-1", "model": "MODEL-1", "config_id": 303},
        expected_trigger_source="manual",
    )
    assert controller.state.active_config_id == 303
    assert controller.state.displayed_config_id == 303
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


def test_factory_mode_controller_manual_start_resolves_to_first_enabled_config():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((202, 101))

    accepted = controller.start_manual({"sn": "SN-1", "model": "M-1"})

    assert accepted is True
    _assert_start_called_with(
        services,
        {"sn": "SN-1", "model": "M-1", "config_id": 202},
        expected_trigger_source="manual",
    )
    assert controller.state.active_config_id == 202
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


@pytest.mark.parametrize("start_kind", ["manual", "plc", "scan"])
def test_factory_mode_controller_accepted_starts_clear_stale_review_context(start_kind):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

    reset_reasons.clear()
    reviews.clear()
    _accept_start(services)
    services.test.start.reset_mock()

    if start_kind == "manual":
        accepted = controller.start_manual()
        expected_config_id = 101
    elif start_kind == "plc":
        accepted = controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=22)
        )
        expected_config_id = 202
    else:
        accepted = controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        expected_config_id = 101

    assert accepted is True
    assert reset_reasons == ["next_run_accepted"]
    assert reviews == [services_mod.FactoryReviewPayload()]
    assert controller.review_payload == services_mod.FactoryReviewPayload()
    _assert_start_called_with(
        services,
        {"config_id": expected_config_id},
        expected_trigger_source=start_kind,
    )
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.displayed_config_id == expected_config_id
    assert controller.state.run_phase == "running"


@pytest.mark.parametrize("start_kind", ["manual", "plc", "scan"])
def test_factory_mode_controller_accepted_starts_preserve_review_context_until_start_acceptance(
    start_kind,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

    start_snapshots: list[dict[str, object]] = []

    def _capture_start(payload, *, on_start_accepted=None):
        start_snapshots.append(
            {
                "stage": "before_acceptance",
                "payload": dict(payload),
                "review_payload": controller.review_payload,
                "state": controller.state,
                "reset_reasons": list(reset_reasons),
                "reviews": list(reviews),
            }
        )
        assert callable(on_start_accepted)
        on_start_accepted(_run_id_for(payload["config_id"]))
        start_snapshots.append(
            {
                "stage": "after_acceptance",
                "payload": dict(payload),
                "review_payload": controller.review_payload,
                "state": controller.state,
                "reset_reasons": list(reset_reasons),
                "reviews": list(reviews),
            }
        )
        return True

    services.test.start.side_effect = _capture_start
    reset_reasons.clear()
    reviews.clear()

    if start_kind == "manual":
        accepted = controller.start_manual()
        expected_config_id = 101
    elif start_kind == "plc":
        accepted = controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=22)
        )
        expected_config_id = 202
    else:
        accepted = controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        expected_config_id = 101

    assert accepted is True
    assert len(start_snapshots) == 2
    assert start_snapshots[0]["payload"]["config_id"] == expected_config_id
    assert start_snapshots[0]["payload"]["trigger_source"] == start_kind
    assert start_snapshots[0]["stage"] == "before_acceptance"
    assert start_snapshots[0]["review_payload"].displayed_config_id == 202
    assert start_snapshots[0]["reset_reasons"] == []
    assert start_snapshots[0]["reviews"] == []
    assert start_snapshots[0]["state"].active_config_id is None
    assert start_snapshots[0]["state"].displayed_config_id == 202
    assert start_snapshots[0]["state"].run_phase == "reviewing"
    assert start_snapshots[0]["state"].trigger_source == "plc"
    assert start_snapshots[1]["stage"] == "after_acceptance"
    assert start_snapshots[1]["review_payload"] == services_mod.FactoryReviewPayload()
    assert start_snapshots[1]["reset_reasons"] == ["next_run_accepted"]
    assert start_snapshots[1]["reviews"] == [services_mod.FactoryReviewPayload()]
    assert start_snapshots[1]["state"].active_config_id == expected_config_id
    assert start_snapshots[1]["state"].displayed_config_id == expected_config_id
    assert start_snapshots[1]["state"].run_phase == "running"
    assert start_snapshots[1]["state"].trigger_source == start_kind


def test_factory_mode_controller_ignores_stale_same_config_start_acceptance_callbacks_after_pending_reset():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    callbacks: list[object] = []
    bus.factory_state_changed.connect(states.append)
    services = _make_services(
        (_make_config(101), _make_config(202), _make_config(303), _make_config(404))
    )

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "first"}) is True
    controller.set_selection((303, 404))
    controller.set_selection((101, 202))
    assert controller.start_manual({"sn": "second"}) is True
    assert len(callbacks) == 2
    states.clear()

    callbacks[0](_run_id_for(101, attempt=1))

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert states == []

    callbacks[1](_run_id_for(101, attempt=2))

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert len(states) == 1
    assert states[0] == controller.state


def test_factory_mode_controller_treats_pending_start_as_busy_until_acceptance():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    services = _make_services(
        (
            _make_config(101, plc_id=11, scan_triggers_test=True),
            _make_config(202, plc_id=22, scan_triggers_test=True),
        )
    )
    callbacks: list[object] = []

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "first"}) is True
    assert len(states) == 1
    assert states[0] == controller.state
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "启动请求处理中"
    assert controller.can_arm() is False
    assert controller.start_manual({"sn": "second"}) is False
    assert (
        controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_id=22)
        )
        is False
    )
    assert (
        controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        is False
    )
    assert len(callbacks) == 1
    assert services.test.start.call_count == 1

    callbacks[0](_run_id_for(101))

    assert len(states) == 2
    assert states[-1] == controller.state
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


@pytest.mark.parametrize(
    ("method_name", "reason"),
    [
        ("clear_display_state", "selection_changed"),
        ("clear_for_next_run", "next_run_accepted"),
    ],
)
def test_factory_mode_controller_clear_calls_ignore_pending_start_and_allow_acceptance(
    method_name,
    reason,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    states: list[object] = []
    callbacks: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "pending"}) is True
    assert len(callbacks) == 1

    reset_reasons.clear()
    states.clear()

    getattr(controller, method_name)(reason)

    assert controller._pending_start == (101, "manual")
    assert controller.can_arm() is False
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reset_reasons == []
    assert states == []

    callbacks[0](_run_id_for(101))

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


def test_factory_mode_controller_same_id_invalidating_refresh_keeps_pending_reason_until_idle():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    callbacks: list[object] = []
    accepted_payloads: list[object] = []
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

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    bus.factory_state_changed.connect(states.append)
    bus.factory_runtime_start_accepted.connect(accepted_payloads.append)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "SN-1"}) is True
    assert controller._pending_start == (101, "manual")
    assert controller._pending_start_request_id

    config_map[202]["analysis_window_offset_ms"] = 4000
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "analysis window invalid"
    assert controller._pending_start == (101, "manual")
    assert controller._pending_start_request_id
    assert controller.can_arm() is False
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == controller.state
    assert accepted_payloads == []

    callbacks[0](_run_id_for(101))

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert len(states) == 3
    assert states[2] == controller.state
    assert len(accepted_payloads) == 1
    assert accepted_payloads[0].config_id == 101
    assert accepted_payloads[0].run_id == _run_id_for(101)
    assert accepted_payloads[0].trigger_source == "manual"

    _finish_active_run_to_reviewing(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "manual"
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "analysis window invalid"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "analysis window invalid"


def test_factory_mode_controller_same_id_valid_refresh_keeps_pending_manual_start():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    callbacks: list[object] = []
    accepted_payloads: list[object] = []
    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    services = _make_dynamic_services(config_map)

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    bus.factory_state_changed.connect(states.append)
    bus.factory_runtime_start_accepted.connect(accepted_payloads.append)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "SN-1"}) is True
    pending_request_id = controller._pending_start_request_id
    assert pending_request_id

    config_map[202]["config_name"] = "Config 202 Updated"
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller._pending_start == (101, "manual")
    assert controller._pending_start_request_id == pending_request_id
    assert controller.can_arm() is False
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == controller.state
    assert accepted_payloads == []
    services.test.cancel_pending_start.assert_not_called()

    callbacks[0](_run_id_for(101))

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert len(states) == 3
    assert states[2] == controller.state
    assert len(accepted_payloads) == 1
    assert accepted_payloads[0].config_id == 101
    assert accepted_payloads[0].run_id == _run_id_for(101)
    assert accepted_payloads[0].trigger_source == "manual"


def test_factory_mode_controller_selection_change_cancels_pending_start_acceptance():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    callbacks: list[object] = []
    start_payloads: list[dict[str, object]] = []
    bus.factory_state_changed.connect(states.append)
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
            _make_config(404),
        )
    )

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        start_payloads.append(dict(payload))
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "first"}) is True
    assert len(callbacks) == 1
    request_id = start_payloads[0].get("factory_start_request_id")
    assert isinstance(request_id, str)
    assert request_id

    next_state = controller.set_selection((303, 404))

    assert next_state == controller.state
    services.test.cancel_pending_start.assert_called_once_with(
        request_id=request_id,
        config_id=101,
    )
    assert controller.state.selected_config_ids == (303, 404)
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller.can_arm() is True
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == controller.state

    callbacks[0](_run_id_for(101))

    assert controller.state.selected_config_ids == (303, 404)
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller.can_arm() is True
    assert len(states) == 2
    assert states[1] == controller.state


def test_factory_mode_controller_selection_change_cancels_pending_start_rejection_keeps_invalid_selection_warning():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    callbacks: list[object] = []
    start_payloads: list[dict[str, object]] = []
    services = _make_services(
        (
            _make_config(101),
            _make_config(202),
            _make_config(303),
        )
    )

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        start_payloads.append(dict(payload))
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    services.test.cancel_pending_start.return_value = False
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202, 303))

    assert controller.start_manual({"sn": "first"}) is True
    assert len(callbacks) == 1
    request_id = start_payloads[0].get("factory_start_request_id")
    assert isinstance(request_id, str)
    assert request_id

    assert services.config_service.delete_config(202) is True
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 303)
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "启动请求处理中"

    next_state = controller.set_selection((303, 101))

    assert next_state == controller.state
    services.test.cancel_pending_start.assert_called_once_with(
        request_id=request_id,
        config_id=101,
    )
    assert controller.state.selected_config_ids == (101, 303)
    assert controller._invalid_selection_warning_active is True
    assert controller._pending_start == (101, "manual")

    callbacks[0](_run_id_for(101))
    bus.factory_runtime_aborted.emit(
        _make_abort_payload(
            config_id=101,
            trigger_source="manual",
        )
    )

    assert controller.state.selected_config_ids == (101, 303)
    assert controller.state.run_phase == "not_ready"
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "配置已失效"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "配置已失效"


def test_factory_mode_controller_stop_cancels_pending_start_and_preserves_review_state():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    accepted_payloads: list[object] = []
    callbacks: list[object] = []
    start_payloads: list[dict[str, object]] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    bus.factory_runtime_start_accepted.connect(accepted_payloads.append)
    services = _make_services((_make_config(101), _make_config(202)))

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        start_payloads.append(dict(payload))
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review_payload = _enter_reviewing_state(
        controller,
        bus,
        config_id=202,
        trigger_source="plc",
    )
    original_state = controller.state

    reset_reasons.clear()
    reviews.clear()
    states.clear()
    accepted_payloads.clear()

    assert controller.start_manual({"sn": "SN-2"}) is True
    assert len(callbacks) == 1
    request_id = start_payloads[0].get("factory_start_request_id")
    assert isinstance(request_id, str)
    assert request_id

    stopped = controller.stop_active_run()

    assert stopped is True
    services.test.cancel_pending_start.assert_called_once_with(
        request_id=request_id,
        config_id=101,
    )
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert controller.state == original_state
    assert controller.review_payload == original_review_payload
    assert controller.can_arm() is True
    assert reset_reasons == []
    assert reviews == []
    assert len(states) == 2
    assert states[0].selected_config_ids == original_state.selected_config_ids
    assert states[0].active_config_id == original_state.active_config_id
    assert states[0].displayed_config_id == original_state.displayed_config_id
    assert states[0].run_phase == original_state.run_phase
    assert states[0].trigger_source == original_state.trigger_source
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == original_state
    assert accepted_payloads == []

    callbacks[0](_run_id_for(101))

    assert controller.state == original_state
    assert controller.review_payload == original_review_payload
    assert reset_reasons == []
    assert reviews == []
    assert len(states) == 2
    assert states[1] == original_state
    assert accepted_payloads == []


def test_factory_mode_controller_ignores_stale_start_callback_for_same_config_new_request():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    callbacks: list[object] = []
    start_payloads: list[dict[str, object]] = []
    accepted_payloads: list[object] = []
    bus.factory_state_changed.connect(states.append)
    bus.factory_runtime_start_accepted.connect(accepted_payloads.append)
    services = _make_services(
        (_make_config(101), _make_config(202), _make_config(303), _make_config(404))
    )

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        start_payloads.append(dict(payload))
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    assert controller.start_manual({"sn": "first"}) is True
    first_request_id = start_payloads[0].get("factory_start_request_id")
    assert isinstance(first_request_id, str)
    assert first_request_id

    controller.set_selection((303, 404))
    controller.set_selection((101, 202))
    assert controller.start_manual({"sn": "second"}) is True
    second_request_id = start_payloads[1].get("factory_start_request_id")
    assert isinstance(second_request_id, str)
    assert second_request_id
    assert second_request_id != first_request_id
    states.clear()
    accepted_payloads.clear()

    callbacks[0](_run_id_for(101, attempt=1))

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert states == []
    assert accepted_payloads == []

    callbacks[1](_run_id_for(101, attempt=2))

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert states == [controller.state]
    assert len(accepted_payloads) == 1
    assert accepted_payloads[0].config_id == 101
    assert accepted_payloads[0].run_id == _run_id_for(101, attempt=2)
    assert accepted_payloads[0].trigger_source == "manual"
    assert not hasattr(accepted_payloads[0], "request_id")


def test_factory_mode_controller_consumes_validated_start_acceptance_via_event_bus_dispatch():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    callbacks: list[object] = []
    accepted_snapshots: list[dict[str, object]] = []
    states: list[object] = []
    services = _make_services((_make_config(101), _make_config(202)))
    controller = None

    def _observe_accepted(payload):
        accepted_snapshots.append(
            {
                "run_id": payload.run_id,
                "active_config_id": controller.state.active_config_id,
                "displayed_config_id": controller.state.displayed_config_id,
                "run_phase": controller.state.run_phase,
                "trigger_source": controller.state.trigger_source,
            }
        )

    bus.factory_runtime_start_accepted.connect(_observe_accepted)

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    bus.factory_state_changed.connect(states.append)
    controller.set_selection((101, 202))
    states.clear()

    assert controller.start_manual({"sn": "SN-1"}) is True
    assert len(callbacks) == 1

    callbacks[0](_run_id_for(101))

    assert accepted_snapshots == [
        {
            "run_id": _run_id_for(101),
            "active_config_id": None,
            "displayed_config_id": None,
            "run_phase": "armed",
            "trigger_source": None,
        }
    ]
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == controller.state


def test_factory_mode_controller_ignores_stale_public_start_accepted_emit_for_same_config_new_request():
    controller_mod = _load_factory_mode_controller_module()
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)

    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"

    bus = EventBus()
    states: list[object] = []
    callbacks: list[object] = []
    accepted_payloads: list[object] = []
    bus.factory_state_changed.connect(states.append)
    bus.factory_runtime_start_accepted.connect(accepted_payloads.append)
    services = _make_services(
        (_make_config(101), _make_config(202), _make_config(303), _make_config(404))
    )

    def _capture_start(payload, *, on_start_accepted=None):
        assert payload["config_id"] == 101
        assert payload["trigger_source"] == "manual"
        assert callable(on_start_accepted)
        callbacks.append(on_start_accepted)
        return True

    services.test.start.side_effect = _capture_start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    assert controller.start_manual({"sn": "first"}) is True
    controller.set_selection((303, 404))
    controller.set_selection((101, 202))
    assert controller.start_manual({"sn": "second"}) is True
    assert len(callbacks) == 2
    states.clear()
    accepted_payloads.clear()

    bus.factory_runtime_start_accepted.emit(
        runtime_start_cls(
            config_id=101,
            run_id=_run_id_for(101, attempt=1),
            trigger_source="manual",
        )
    )

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert states == []
    assert len(accepted_payloads) == 1
    assert accepted_payloads[0].run_id == _run_id_for(101, attempt=1)

    callbacks[1](_run_id_for(101, attempt=2))

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert states == [controller.state]
    assert len(accepted_payloads) == 2
    assert accepted_payloads[1].run_id == _run_id_for(101, attempt=2)


def test_blocked_manual_start_emits_factory_start_blocked_reason():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection(())

    assert controller.start_manual({"sn": "SN-1"}) is False

    assert len(blocked_payloads) == 1
    assert blocked_payloads[0].reason == "请至少启用一个配置"
    assert blocked_payloads[0].trigger_source == "manual"


def test_blocked_manual_start_emits_updated_state_before_blocked_feedback_when_readiness_changes():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    events: list[tuple[str, str | None]] = []
    services = _make_services((_make_config(101),))
    controller = controller_mod.FactoryModeController(bus, services)
    bus.factory_state_changed.connect(
        lambda state: events.append(("state", state.start_ready_reason))
    )
    bus.factory_start_blocked.connect(
        lambda payload: events.append(("blocked", payload.reason))
    )
    controller.set_selection((101,))
    events.clear()
    services.config_service.validate_factory_runtime.side_effect = (
        lambda config: ["analysis window invalid"]
    )

    assert controller.start_manual({"sn": "SN-1"}) is False

    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "analysis window invalid"
    assert events == [
        ("state", "analysis window invalid"),
        ("blocked", "analysis window invalid"),
    ]


def test_factory_mode_controller_failed_start_preserves_review_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.start.side_effect = lambda workorder, *, on_start_accepted=None: False
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review_payload = _enter_reviewing_state(
        controller,
        bus,
        config_id=202,
        trigger_source="plc",
    )

    reset_reasons.clear()
    reviews.clear()
    states.clear()

    try:
        accepted = controller.start_manual({"sn": "SN-1"})
    except RuntimeError as exc:
        accepted = exc

    assert accepted is False
    _assert_start_called_with(
        services,
        {"sn": "SN-1", "config_id": 101},
        expected_trigger_source="manual",
    )
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "plc"
    assert controller.review_payload == original_review_payload
    assert reset_reasons == []
    assert reviews == []
    assert states == []


def test_factory_mode_controller_failed_start_emits_backend_reason_and_preserves_review_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.start.return_value = False
    services.test.last_start_error.return_value = "未配置输入硬件，请先在「硬件」页面保存输入设备。"
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review_payload = _enter_reviewing_state(
        controller,
        bus,
        config_id=202,
        trigger_source="plc",
    )

    blocked_payloads.clear()
    reset_reasons.clear()
    reviews.clear()
    states.clear()

    accepted = controller.start_manual({"sn": "SN-1"})

    assert accepted is False
    assert len(blocked_payloads) == 1
    assert blocked_payloads[0].reason == "未配置输入硬件，请先在「硬件」页面保存输入设备。"
    assert blocked_payloads[0].trigger_source == "manual"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "plc"
    assert controller.review_payload == original_review_payload
    assert reset_reasons == []
    assert reviews == []
    assert states == []


def test_factory_mode_controller_start_exception_preserves_review_context_and_clears_pending_start():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.start.side_effect = RuntimeError("start exploded")
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    original_review_payload = _enter_reviewing_state(
        controller,
        bus,
        config_id=202,
        trigger_source="plc",
    )

    reset_reasons.clear()
    reviews.clear()
    states.clear()

    try:
        accepted = controller.start_manual({"sn": "SN-1"})
    except RuntimeError as exc:
        accepted = exc

    assert accepted is False
    services.test.start.assert_called_once()
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert controller.can_arm() is True
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "plc"
    assert controller.review_payload == original_review_payload
    assert reset_reasons == []
    assert reviews == []
    assert states == []


def test_factory_mode_controller_start_exception_emits_blocked_reason_and_clears_pending_start():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.start.side_effect = RuntimeError("backend exploded")
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    accepted = controller.start_manual({"sn": "SN-1"})

    assert accepted is False
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert len(blocked_payloads) == 1
    assert blocked_payloads[0].reason == "启动失败：backend exploded"
    assert blocked_payloads[0].trigger_source == "manual"


def test_factory_mode_controller_failed_start_after_start_acceptance_signal_error_emits_blocked_reason():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services((_make_config(101), _make_config(202)))

    class FailingAcceptedSignal:
        def connect(self, _callback):
            return None

        def emit(self, _payload):
            raise RuntimeError("accept failed")

    def _start(payload, *, on_start_accepted=None):
        assert callable(on_start_accepted)
        try:
            on_start_accepted(_run_id_for(payload["config_id"]))
        except RuntimeError as exc:
            services.test.last_start_error.return_value = f"启动失败：{exc}"
            return False
        return True

    bus.factory_runtime_start_accepted = FailingAcceptedSignal()
    services.test.start.side_effect = _start
    services.test.last_start_error.return_value = None
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    accepted = controller.start_manual({"sn": "SN-1"})

    assert accepted is False
    assert len(blocked_payloads) == 1
    assert blocked_payloads[0].reason == "启动失败：accept failed"
    assert blocked_payloads[0].trigger_source == "manual"
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None


def test_factory_mode_controller_backend_false_after_runtime_acceptance_does_not_emit_blocked_start():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services((_make_config(101), _make_config(202)))

    def _start(payload, *, on_start_accepted=None):
        assert callable(on_start_accepted)
        on_start_accepted(_run_id_for(payload["config_id"]))
        return False

    services.test.start.side_effect = _start
    services.test.last_start_error.return_value = None
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    accepted = controller.start_manual({"sn": "SN-1"})

    assert accepted is False
    assert blocked_payloads == []
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


def test_factory_mode_controller_intentional_pending_start_cancellation_does_not_emit_blocked_start():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    blocked_payloads: list[object] = []
    bus.factory_start_blocked.connect(blocked_payloads.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.cancel_pending_start.return_value = True
    services.test.last_start_error.return_value = None
    controller = None

    def _start(_payload, *, on_start_accepted=None):
        assert controller.stop_active_run() is True
        return False

    services.test.start.side_effect = _start
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))

    accepted = controller.start_manual({"sn": "SN-1"})

    assert accepted is False
    assert blocked_payloads == []
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
