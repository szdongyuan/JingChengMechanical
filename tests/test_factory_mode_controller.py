from __future__ import annotations

import importlib
import importlib.util
from unittest.mock import MagicMock

import pytest
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


def _make_config(
    config_id: int,
    *,
    trigger_type: str = "button",
    plc_device_key: str | None = None,
    scan_triggers_test: bool = False,
):
    return {
        "config_id": config_id,
        "config_name": f"Config {config_id}",
        "record_duration": 5.0,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 0,
        "trigger_type": trigger_type,
        "plc_device_key": plc_device_key,
        "scan_triggers_test": scan_triggers_test,
    }


def _make_services(configs, *, validation_errors: dict[int, list[str]] | None = None):
    config_map = {config["config_id"]: dict(config) for config in configs}
    services = MagicMock()
    services.test = MagicMock()
    services.config_service = MagicMock()
    services.config_service.load_config.side_effect = lambda config_id: dict(config_map[config_id])
    services.config_service.validate_factory_runtime.side_effect = (
        lambda config: list((validation_errors or {}).get(config["config_id"], ()))
    )
    return services


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


def test_factory_mode_controller_stays_not_ready_without_manual_primary():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202))

    assert controller.state.run_phase == "not_ready"
    assert controller.state.is_ready is False
    assert controller.can_arm() is False


def test_factory_mode_controller_arms_only_after_runtime_validation_succeeds():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (_make_config(101), _make_config(202)),
        validation_errors={202: ["bad runtime config"]},
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.state.run_phase == "not_ready"
    assert controller.state.is_ready is False

    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.state.run_phase == "armed"
    assert controller.state.is_ready is True
    assert controller.can_arm() is True


def test_factory_mode_controller_manual_start_resolves_to_primary_only():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=202)

    accepted = controller.start_manual({"sn": "SN-1", "model": "M-1"})

    assert accepted is True
    services.test.start.assert_called_once_with({"sn": "SN-1", "model": "M-1", "config_id": 202})
    assert controller.state.active_config_id == 202
    assert controller.state.displayed_config_id == 202
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


@pytest.mark.parametrize(
    ("configs", "manual_primary_config_id", "plc_device_key", "expected_config_id"),
    [
        (
            (
                _make_config(101, trigger_type="plc", plc_device_key="plc-a"),
                _make_config(202, trigger_type="plc", plc_device_key="plc-b"),
            ),
            101,
            "plc-b",
            202,
        ),
        (
            (
                _make_config(101, trigger_type="plc", plc_device_key="plc-a"),
                _make_config(202, trigger_type="plc", plc_device_key="plc-a"),
            ),
            101,
            "plc-a",
            101,
        ),
    ],
)
def test_factory_mode_controller_routes_plc_events(configs, manual_primary_config_id, plc_device_key, expected_config_id):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(configs)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202), manual_primary_config_id=manual_primary_config_id)

    accepted = controller.handle_plc_event(services_mod.FactoryPlcIngressPayload(plc_device_key=plc_device_key))

    assert accepted is True
    services.test.start.assert_called_once_with({"config_id": expected_config_id})
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "plc"


@pytest.mark.parametrize(
    ("configs", "manual_primary_config_id", "expected_config_id"),
    [
        (
            (
                _make_config(101, scan_triggers_test=False),
                _make_config(202, scan_triggers_test=True),
            ),
            101,
            202,
        ),
        (
            (
                _make_config(101, scan_triggers_test=True),
                _make_config(202, scan_triggers_test=True),
            ),
            101,
            101,
        ),
    ],
)
def test_factory_mode_controller_routes_scan_events(configs, manual_primary_config_id, expected_config_id):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(configs)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202), manual_primary_config_id=manual_primary_config_id)

    accepted = controller.handle_scan_event(services_mod.FactoryScanIngressPayload(scan_text="SN-1"))

    assert accepted is True
    services.test.start.assert_called_once_with({"config_id": expected_config_id})
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "scan"


def test_factory_mode_controller_ignores_plc_and_scan_before_armed():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101, trigger_type="plc", plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, trigger_type="plc", plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    plc_accepted = controller.handle_plc_event(services_mod.FactoryPlcIngressPayload(plc_device_key="plc-a"))
    scan_accepted = controller.handle_scan_event(services_mod.FactoryScanIngressPayload(scan_text="SN-1"))

    assert plc_accepted is False
    assert scan_accepted is False
    services.test.start.assert_not_called()
    assert controller.state.run_phase == "not_ready"


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
            _make_config(101, trigger_type="plc", plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, trigger_type="plc", plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

    reset_reasons.clear()
    reviews.clear()
    services.test.start.reset_mock()

    if start_kind == "manual":
        accepted = controller.start_manual()
        expected_config_id = 101
    elif start_kind == "plc":
        accepted = controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_device_key="plc-b")
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
    services.test.start.assert_called_once_with({"config_id": expected_config_id})
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.displayed_config_id == expected_config_id
    assert controller.state.run_phase == "running"


@pytest.mark.parametrize("start_kind", ["manual", "plc", "scan"])
def test_factory_mode_controller_accepted_starts_preclear_review_context_before_test_manager_start(
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
            _make_config(101, trigger_type="plc", plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, trigger_type="plc", plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

    start_snapshots: list[dict[str, object]] = []

    def _capture_start(payload):
        start_snapshots.append(
            {
                "payload": dict(payload),
                "review_payload": controller.review_payload,
                "state": controller.state,
                "reset_reasons": list(reset_reasons),
                "reviews": list(reviews),
            }
        )

    services.test.start.side_effect = _capture_start
    reset_reasons.clear()
    reviews.clear()

    if start_kind == "manual":
        accepted = controller.start_manual()
        expected_config_id = 101
    elif start_kind == "plc":
        accepted = controller.handle_plc_event(
            services_mod.FactoryPlcIngressPayload(plc_device_key="plc-b")
        )
        expected_config_id = 202
    else:
        accepted = controller.handle_scan_event(
            services_mod.FactoryScanIngressPayload(scan_text="SN-1")
        )
        expected_config_id = 101

    assert accepted is True
    assert len(start_snapshots) == 1
    assert start_snapshots[0]["payload"]["config_id"] == expected_config_id
    assert start_snapshots[0]["review_payload"] == services_mod.FactoryReviewPayload()
    assert start_snapshots[0]["reset_reasons"] == ["next_run_accepted"]
    assert start_snapshots[0]["reviews"] == [services_mod.FactoryReviewPayload()]
    assert start_snapshots[0]["state"].active_config_id is None
    assert start_snapshots[0]["state"].displayed_config_id is None
    assert start_snapshots[0]["state"].run_phase == "armed"
    assert start_snapshots[0]["state"].trigger_source is None


@pytest.mark.parametrize("phase", ["running", "analyzing", "reanalyzing"])
def test_factory_mode_controller_blocks_new_starts_during_active_or_reanalysis_phase(phase):
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services(
        (
            _make_config(101, trigger_type="plc", plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, trigger_type="plc", plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()
    services.test.start.reset_mock()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(services_mod.FactoryRuntimeConfigPayload(config_id=101))
    elif phase == "reanalyzing":
        controller.handle_runtime_review_ready(_make_review_payload(displayed_config_id=101))
        controller.begin_reanalysis(1.0, 3.0)
        services.test.rejudge.reset_mock()

    assert controller.start_manual() is False
    assert controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_device_key="plc-a")
    ) is False
    assert controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-1")
    ) is False
    services.test.start.assert_not_called()


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
            _make_config(101, trigger_type="plc", plc_device_key="plc-a"),
            _make_config(202, trigger_type="plc", plc_device_key="plc-b"),
            _make_config(303, trigger_type="button"),
            _make_config(404, trigger_type="button"),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(services_mod.FactoryRuntimeConfigPayload(config_id=101))
    elif phase == "reanalyzing":
        controller.handle_runtime_review_ready(_make_review_payload(displayed_config_id=101))
        controller.begin_reanalysis(1.0, 3.0)

    original_state = controller.state
    original_review_payload = controller.review_payload
    reset_reasons.clear()
    reviews.clear()
    states.clear()

    next_state = controller.set_selection((303, 404), manual_primary_config_id=303)

    assert next_state == original_state
    assert controller.state == original_state
    assert controller.review_payload == original_review_payload
    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.manual_primary_config_id == 101
    assert controller.state.displayed_config_id == original_state.displayed_config_id
    assert controller.state.run_phase == phase
    assert controller.can_arm() is False
    assert reset_reasons == []
    assert reviews == []
    assert states == []


@pytest.mark.parametrize("phase", ["running", "analyzing"])
def test_factory_mode_controller_stop_clears_active_state_and_display_ownership(phase):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()
    reviews.clear()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(services_mod.FactoryRuntimeConfigPayload(config_id=101))

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


def test_factory_mode_controller_reanalysis_failure_returns_to_reviewing_without_result():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.handle_runtime_review_ready(_make_review_payload(displayed_config_id=101))

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reanalyzing"
    assert controller.state.displayed_config_id == 101

    controller.finish_reanalysis_failure("manual reanalysis failed")

    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "manual reanalysis failed"
    assert controller.review_payload.judgment_payload is None
