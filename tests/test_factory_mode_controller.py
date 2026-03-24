from __future__ import annotations

import importlib
import importlib.util

import app.services_container as services_mod
from app.services_container import EventBus


def _load_factory_mode_controller_module():
    spec = importlib.util.find_spec("app.service.factory_mode_controller")
    assert spec is not None, "app.service.factory_mode_controller module is missing"
    return importlib.import_module("app.service.factory_mode_controller")


def _make_review_payload(
    *,
    displayed_config_id: int = 101,
    trigger_source: str = "manual",
):
    review_window_cls = getattr(services_mod, "FactoryReviewWindow", None)
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    return review_payload_cls(
        displayed_config_id=displayed_config_id,
        trigger_source=trigger_source,
        captured_duration_sec=5.0,
        default_window=review_window_cls(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=False,
        error_message=None,
        judgment_payload={"overall": "PASS"},
    )


def _enter_reviewing_state(controller, bus, *, config_id: int = 101, trigger_source: str = "manual"):
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)
    runtime_phase_cls = getattr(services_mod, "FactoryRuntimeConfigPayload", None)

    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"
    assert runtime_phase_cls is not None, "FactoryRuntimeConfigPayload is missing"

    review_payload = _make_review_payload(
        displayed_config_id=config_id,
        trigger_source=trigger_source,
    )

    bus.factory_runtime_start_accepted.emit(
        runtime_start_cls(config_id=config_id, trigger_source=trigger_source)
    )
    bus.factory_runtime_analyzing_entered.emit(runtime_phase_cls(config_id=config_id))
    bus.factory_runtime_auto_analysis_succeeded.emit(review_payload)

    return review_payload


def test_factory_mode_controller_starts_not_ready():
    controller_mod = _load_factory_mode_controller_module()
    state_cls = getattr(services_mod, "FactoryControllerState", None)

    assert state_cls is not None, "FactoryControllerState is missing"

    controller = controller_mod.FactoryModeController(EventBus())

    assert isinstance(controller.state, state_cls)
    assert controller.state.selected_config_ids == ()
    assert controller.state.manual_primary_config_id is None
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "not_ready"
    assert controller.state.trigger_source is None
    assert controller.state.is_ready is False


def test_factory_mode_controller_owns_pair_selection_state():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    states: list[object] = []
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)

    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.manual_primary_config_id == 101
    assert controller.state.run_phase == "armed"
    assert controller.state.is_ready is True
    assert states[-1] == controller.state


def test_factory_mode_controller_consumes_runtime_events_and_review_payload():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)
    runtime_phase_cls = getattr(services_mod, "FactoryRuntimeConfigPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"
    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"
    assert runtime_phase_cls is not None, "FactoryRuntimeConfigPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)

    bus.factory_runtime_start_accepted.emit(
        runtime_start_cls(config_id=101, trigger_source="manual")
    )

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"

    bus.factory_runtime_analyzing_entered.emit(runtime_phase_cls(config_id=101))

    assert controller.state.run_phase == "analyzing"

    review_payload = _make_review_payload()

    bus.factory_runtime_auto_analysis_succeeded.emit(review_payload)

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.review_payload == review_payload
    assert reviews[-1] == review_payload


def test_factory_mode_controller_tracks_explicit_plc_ingress_and_display_reset():
    controller_mod = _load_factory_mode_controller_module()
    plc_payload_cls = getattr(services_mod, "FactoryPlcIngressPayload", None)

    assert plc_payload_cls is not None, "FactoryPlcIngressPayload is missing"

    bus = EventBus()
    reset_reasons: list[str] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    controller = controller_mod.FactoryModeController(bus)

    bus.factory_plc_event.emit(plc_payload_cls(plc_device_key="plc-a"))
    controller.clear_display_state("selection_changed")

    assert controller.last_plc_ingress.plc_device_key == "plc-a"
    assert reset_reasons == ["selection_changed"]


def test_set_selection_clears_review_payload_and_emits_cleared_review_contract():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    reset_reasons: list[str] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_display_reset.connect(reset_reasons.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    _enter_reviewing_state(controller, bus)

    reviews.clear()
    reset_reasons.clear()

    controller.set_selection((303, 404), manual_primary_config_id=303)

    assert controller.review_payload == review_payload_cls()
    assert controller.state.selected_config_ids == (303, 404)
    assert controller.state.manual_primary_config_id == 303
    assert controller.state.displayed_config_id is None
    assert controller.state.trigger_source is None
    assert reviews == [review_payload_cls()]
    assert reset_reasons == ["selection_changed"]


def test_runtime_start_accepted_clears_review_context_before_running():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"
    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    reset_reasons: list[str] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    _enter_reviewing_state(controller, bus)

    reviews.clear()
    reset_reasons.clear()
    states.clear()

    bus.factory_runtime_start_accepted.emit(
        runtime_start_cls(config_id=202, trigger_source="plc")
    )

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
