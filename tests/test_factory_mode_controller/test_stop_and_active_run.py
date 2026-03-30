from __future__ import annotations

import pytest
import app.services_container as services_mod
from app.services_container import EventBus

from ._support import (
    _accept_start,
    _emit_owned_runtime_start,
    _enter_reviewing_state,
    _load_factory_mode_controller_module,
    _make_config,
    _make_review_payload,
    _make_runtime_config_payload,
    _make_services,
    _run_id_for,
)


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


@pytest.mark.parametrize("phase", ["running", "analyzing", "reanalyzing"])
def test_factory_mode_controller_blocks_new_starts_during_active_or_reanalysis_phase(phase):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202))
    _accept_start(services)
    controller.start_manual()
    services.test.start.reset_mock()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
    elif phase == "reanalyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
        controller.handle_runtime_review_ready(_make_review_payload(displayed_config_id=101))
        controller.begin_reanalysis(1.0, 3.0)
        services.test.rejudge.reset_mock()

    assert controller.start_manual() is False
    assert (
        controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_device_key="plc-a")
        )
        is False
    )
    assert (
        controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        is False
    )
    services.test.start.assert_not_called()


@pytest.mark.parametrize(
    ("phase", "expected_reason"),
    [
        ("running", "当前测试运行中"),
        ("analyzing", "当前测试分析中"),
        ("reanalyzing", "当前正在重新分析"),
    ],
)
def test_factory_mode_controller_keeps_start_ready_false_during_blocking_analysis_phases(
    phase,
    expected_reason,
):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((202, 101))
    _emit_owned_runtime_start(
        controller,
        controller.bus,
        config_id=101,
        trigger_source="manual",
    )

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
    elif phase == "reanalyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
        controller.handle_runtime_review_ready(_make_review_payload(displayed_config_id=101))
        controller.begin_reanalysis(1.0, 3.0)

    assert controller.state.run_phase == phase
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == expected_reason


@pytest.mark.parametrize("phase", ["running", "analyzing", "reanalyzing"])
def test_factory_mode_controller_ignores_selection_changes_during_active_or_reanalysis_phase(
    phase,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services(
        (
            _make_config(101, plc_device_key="plc-a"),
            _make_config(202, plc_device_key="plc-b"),
            _make_config(303),
            _make_config(404),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    _accept_start(services)
    controller.start_manual()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
    elif phase == "reanalyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
        controller.handle_runtime_review_ready(_make_review_payload(displayed_config_id=101))
        controller.begin_reanalysis(1.0, 3.0)

    original_state = controller.state
    original_review_payload = controller.review_payload
    reset_reasons.clear()
    reviews.clear()
    states.clear()

    next_state = controller.set_selection((303, 404))

    assert next_state == original_state
    assert controller.state == original_state
    assert controller.review_payload == original_review_payload
    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.displayed_config_id == original_state.displayed_config_id
    assert controller.state.run_phase == phase
    assert controller.can_arm() is False
    assert reset_reasons == []
    assert reviews == []
    assert states == []


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


@pytest.mark.parametrize(
    ("method_name", "reason"),
    [
        ("clear_display_state", "selection_changed"),
        ("clear_for_next_run", "next_run_accepted"),
    ],
)
def test_factory_mode_controller_clear_calls_ignore_active_analysis_and_preserve_runtime_ownership(
    method_name,
    reason,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    controller.start_manual()
    run_id = _run_id_for(101)
    controller.handle_runtime_analyzing(
        _make_runtime_config_payload(config_id=101, run_id=run_id)
    )
    original_state = controller.state

    reset_reasons.clear()
    reviews.clear()
    states.clear()

    getattr(controller, method_name)(reason)

    assert controller.state == original_state
    assert reset_reasons == []
    assert reviews == []
    assert states == []

    review_payload = _make_review_payload(
        displayed_config_id=101,
        run_id=run_id,
        trigger_source="manual",
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(review_payload)

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.state.trigger_source == "manual"
    assert controller.review_payload == review_payload


@pytest.mark.parametrize("phase", ["running", "analyzing"])
def test_factory_mode_controller_stop_clears_active_state_and_display_ownership(phase):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    controller.start_manual()
    reviews.clear()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))

    stopped = controller.stop_active_run()

    assert stopped is True
    services.test.stop_active_run.assert_called_once_with(config_id=101)
    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reset_reasons == ["active_run_stopped"]
    assert reviews == [services_mod.FactoryReviewPayload()]


@pytest.mark.parametrize("phase", ["running", "analyzing"])
def test_factory_mode_controller_failed_stop_keeps_active_run_state(phase):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    services.test.stop_active_run.return_value = False
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    controller.start_manual()
    reviews.clear()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))

    stopped = controller.stop_active_run()

    assert stopped is False
    services.test.stop_active_run.assert_called_once_with(config_id=101)
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == phase
    assert controller.state.trigger_source == "manual"
    assert reset_reasons == []
    assert reviews == []


@pytest.mark.parametrize("backend_stop_api", ["stop_active_run", "stop"])
def test_factory_mode_controller_stop_backend_exception_is_contained_and_keeps_active_run_state(
    backend_stop_api,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    if backend_stop_api == "stop_active_run":
        services.test.stop_active_run.side_effect = RuntimeError("stop exploded")
    else:
        services.test.stop_active_run = None
        services.test.stop.side_effect = RuntimeError("stop exploded")
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    controller.start_manual()
    reviews.clear()

    try:
        stopped = controller.stop_active_run()
    except RuntimeError as exc:
        pytest.fail(f"stop backend exception escaped controller boundary: {exc}")

    assert stopped is False
    if backend_stop_api == "stop_active_run":
        services.test.stop_active_run.assert_called_once_with(config_id=101)
    else:
        services.test.stop.assert_called_once_with(config_id=101)
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert reset_reasons == []
    assert reviews == []


def test_factory_mode_controller_ignores_late_same_config_run_id_events_after_stop_and_restart():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    first_run_id = _run_id_for(101, attempt=1)
    second_run_id = _run_id_for(101, attempt=2)

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id=first_run_id,
        trigger_source="manual",
    )
    controller.handle_runtime_analyzing(
        _make_runtime_config_payload(config_id=101, run_id=first_run_id)
    )
    assert controller.stop_active_run() is True
    reviews.clear()

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id=second_run_id,
        trigger_source="manual",
    )
    reviews.clear()

    controller.handle_runtime_analyzing(
        _make_runtime_config_payload(config_id=101, run_id=first_run_id)
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=first_run_id,
            trigger_source="manual",
        )
    )

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert reviews == []
