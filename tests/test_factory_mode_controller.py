from __future__ import annotations

import importlib
import importlib.util
from unittest.mock import MagicMock

import numpy as np
import pytest
import app.services_container as services_mod
from app.service.analysis.frames import JudgmentResult
from app.service.analysis.review_payloads import build_summary_payload
from app.service.analysis.test_manager import TestManager
from app.services_container import EventBus


_DEFAULT_JUDGMENT_PAYLOAD = object()


def _run_id_for(config_id: int, *, attempt: int = 1) -> str:
    return f"run-{config_id}-{attempt}"


def _load_factory_mode_controller_module():
    spec = importlib.util.find_spec("app.service.factory_mode_controller")
    assert spec is not None, "app.service.factory_mode_controller module is missing"
    return importlib.import_module("app.service.factory_mode_controller")


def _make_review_payload(
    *,
    displayed_config_id: int = 101,
    run_id: str | None = None,
    trigger_source: str = "manual",
    no_result: bool = False,
    error_message: str | None = None,
    judgment_payload: dict | None | object = _DEFAULT_JUDGMENT_PAYLOAD,
    summary_payload: dict | None = None,
    chart_payload: dict | None = None,
):
    review_window_cls = getattr(services_mod, "FactoryReviewWindow", None)
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    return review_payload_cls(
        displayed_config_id=displayed_config_id,
        run_id=run_id or _run_id_for(displayed_config_id),
        trigger_source=trigger_source,
        captured_duration_sec=5.0,
        default_window=review_window_cls(start_sec=1.0, end_sec=3.0),
        fallback_window=None,
        manual_reanalysis_allowed=True,
        no_result=no_result,
        error_message=error_message,
        judgment_payload=(
            {"overall": "PASS"}
            if judgment_payload is _DEFAULT_JUDGMENT_PAYLOAD
            else judgment_payload
        ),
        summary_payload=summary_payload,
        chart_payload=chart_payload,
    )


def _make_runtime_config_payload(*, config_id: int = 101, run_id: str | None = None):
    runtime_phase_cls = getattr(services_mod, "FactoryRuntimeConfigPayload", None)

    assert runtime_phase_cls is not None, "FactoryRuntimeConfigPayload is missing"

    return runtime_phase_cls(
        config_id=config_id,
        run_id=run_id or _run_id_for(config_id),
    )


def _full_no_result_summary(error_message: str | None) -> dict:
    return build_summary_payload(
        config={},
        judgment_payload=None,
        error_message=error_message,
    )


def _make_abort_payload(
    *,
    config_id: int = 101,
    run_id: str | None = None,
    trigger_source: str = "manual",
    error_message: str = "monitor disconnected",
):
    abort_payload_cls = getattr(services_mod, "FactoryRuntimeAbortPayload", None)

    assert abort_payload_cls is not None, "FactoryRuntimeAbortPayload is missing"

    return abort_payload_cls(
        config_id=config_id,
        run_id=run_id or _run_id_for(config_id),
        trigger_source=trigger_source,
        error_message=error_message,
    )


def _emit_owned_runtime_start(
    controller,
    bus,
    *,
    config_id: int,
    trigger_source: str,
    run_id: str | None = None,
):
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)

    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"

    active_run_id = run_id or _run_id_for(config_id)
    controller._pending_start = (config_id, trigger_source)
    controller._pending_start_request_id = f"request-{config_id}"
    controller.handle_runtime_started(
        runtime_start_cls(
            config_id=config_id,
            run_id=active_run_id,
            trigger_source=trigger_source,
        )
    )


def _enter_reviewing_state(
    controller,
    bus,
    *,
    config_id: int = 101,
    run_id: str | None = None,
    trigger_source: str = "manual",
):
    active_run_id = run_id or _run_id_for(config_id)
    review_payload = _make_review_payload(
        displayed_config_id=config_id,
        run_id=active_run_id,
        trigger_source=trigger_source,
    )

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=config_id,
        run_id=active_run_id,
        trigger_source=trigger_source,
    )
    bus.factory_runtime_analyzing_entered.emit(
        _make_runtime_config_payload(config_id=config_id, run_id=active_run_id)
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(review_payload)

    return review_payload


def _make_config(
    config_id: int,
    *,
    plc_device_key: str | None = None,
    scan_triggers_test: bool = False,
):
    return {
        "config_id": config_id,
        "config_name": f"Config {config_id}",
        "record_duration": 5.0,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 0,
        "plc_device_key": plc_device_key,
        "scan_triggers_test": scan_triggers_test,
    }


def _make_services(configs, *, validation_errors: dict[int, list[str]] | None = None):
    config_map = {config["config_id"]: dict(config) for config in configs}
    services = MagicMock()
    services.test = MagicMock()
    services.test.is_running.return_value = False
    services.config_service = MagicMock()
    services.config_service.load_config.side_effect = lambda config_id: dict(config_map[config_id])
    services.config_service.validate_factory_runtime.side_effect = (
        lambda config: list((validation_errors or {}).get(config["config_id"], ()))
    )
    return services


def _make_dynamic_services(config_map, *, validate_factory_runtime=None):
    services = MagicMock()
    services.test = MagicMock()
    services.test.is_running.return_value = False
    services.config_service = MagicMock()
    services.config_service.load_config.side_effect = lambda config_id: dict(config_map[config_id])
    services.config_service.validate_factory_runtime.side_effect = (
        validate_factory_runtime
        if validate_factory_runtime is not None
        else (lambda config: [])
    )
    return services


class _DummySignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def disconnect(self, callback):
        self._callbacks.remove(callback)

    def emit(self, *args, **kwargs):
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


def _accept_start(services):
    def _start(workorder, *, on_start_accepted=None):
        if callable(on_start_accepted):
            on_start_accepted(_run_id_for(workorder["config_id"]))
        return True

    services.test.start.side_effect = _start


def _assert_start_called_with(
    services,
    expected_workorder: dict,
    *,
    expected_trigger_source: str | None = None,
):
    services.test.start.assert_called_once()
    args, kwargs = services.test.start.call_args
    assert len(args) == 1
    actual_workorder = args[0]
    for key, value in expected_workorder.items():
        assert actual_workorder[key] == value
    if expected_trigger_source is not None:
        assert actual_workorder["trigger_source"] == expected_trigger_source
    assert callable(kwargs["on_start_accepted"])


def _assert_log_contains(messages: list[str], *substrings: str) -> None:
    assert any(
        all(substring in message for substring in substrings)
        for message in messages
    ), messages


def _finish_active_run_to_reviewing(
    controller,
    bus,
    *,
    config_id: int,
    trigger_source: str,
    run_id: str | None = None,
):
    active_run_id = run_id or _run_id_for(config_id)
    controller.handle_runtime_analyzing(
        _make_runtime_config_payload(config_id=config_id, run_id=active_run_id)
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(
            displayed_config_id=config_id,
            run_id=active_run_id,
            trigger_source=trigger_source,
        )
    )


def _make_real_manager_factory_context(monkeypatch, *, save_wav_error: Exception | None = None):
    controller_mod = _load_factory_mode_controller_module()

    class FakeRecorder:
        instances = []

        def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
            self._sample_rate = float(sample_rate)
            self._n_channels = n_channels
            self._record_duration = float(record_duration)
            self._buffer = np.zeros(
                (int(self._sample_rate * self._record_duration), n_channels),
                dtype=np.float32,
            )
            self._running = False
            self.stop_calls = 0
            self.wait_calls: list[int] = []
            self.chunk_ready = _DummySignal()
            self.recording_done = _DummySignal()
            self.error_occurred = _DummySignal()
            FakeRecorder.instances.append(self)

        def isRunning(self):
            return self._running

        def start(self):
            self._running = True

        def stop(self):
            self.stop_calls += 1
            self._running = False

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

        def attach_session(self, *, record_duration):
            return MagicMock(chunk_index=0, sample_index=0)

        def get_buffer_from(self, snapshot):
            return self._buffer

    class FakeSession:
        def __init__(self, config, bus, ai_service, mic_cal, workorder, audio_dir):
            self.config_id = config["config_id"]
            self._config = dict(config)
            self._workorder = dict(workorder)
            self._buffer = None
            self._sample_rate = None
            self._finished = False
            self._attach_snapshot = None
            self._audio_file_path = None
            self._audio_data_id = "audio-123"

        def _setup_pipelines(self):
            return None

        def prepare_for_attach(self):
            return None

        def attach_to_recorder(self, *, chunk_index, snapshot=None):
            self._attach_snapshot = snapshot

        def _on_chunk(self, *args, **kwargs):
            return None

        def _save_wav(self, *args, **kwargs):
            if save_wav_error is not None:
                raise save_wav_error
            self._audio_file_path = "captured.wav"
            return self._audio_file_path

        def _run_judgment(self):
            self._audio_file_path = "captured.wav"
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.91,
                analysis_window=(1.0, 3.0),
            )

        def rejudge(self, start_sec, end_sec):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.97,
                analysis_window=(start_sec, end_sec),
            )

    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
    monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = MagicMock()
    services.bus = bus
    services.config_service = MagicMock()
    services.config_service.load_config.side_effect = lambda config_id: dict(config_map[config_id])
    services.config_service.validate_factory_runtime.side_effect = lambda config: []
    services.ai_service = MagicMock()
    services.ai_service.resolve_binding.return_value = None
    services.hss_service = MagicMock()
    services.hss_service.load_io_devices.return_value = {
        "input": {
            "id": 0,
            "name": "Mic",
            "type": "MME",
            "samplerate": "100",
            "channel_count": "1",
        },
        "output": {
            "id": 1,
            "name": "Speaker",
            "type": "MME",
            "samplerate": "100",
            "channel_count": "1",
        },
        "mic_cal": {},
    }
    services.test = TestManager(bus, services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    return controller, services, reset_reasons, reviews, states, FakeRecorder


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
    assert controller.state.selection_ready is False
    assert controller.state.selection_ready_reason == "请选择两个不同配置"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "请选择两个不同配置"
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
    assert controller.state.selection_ready is True
    assert controller.state.selection_ready_reason is None
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert controller.state.is_ready is True
    assert states[-1] == controller.state


def test_factory_mode_controller_consumes_runtime_events_and_review_payload():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)
    runtime_phase_cls = getattr(services_mod, "FactoryRuntimeConfigPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"
    assert runtime_phase_cls is not None, "FactoryRuntimeConfigPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)

    _enter_reviewing_state(controller, bus)

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.review_payload == _make_review_payload()
    assert reviews[-1] == _make_review_payload()


def test_factory_mode_controller_ignores_out_of_band_factory_runtime_start_acceptance():
    controller_mod = _load_factory_mode_controller_module()
    runtime_start_cls = getattr(services_mod, "FactoryRuntimeStartPayload", None)

    assert runtime_start_cls is not None, "FactoryRuntimeStartPayload is missing"

    bus = EventBus()
    states: list[object] = []
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)

    bus.factory_runtime_start_accepted.emit(
        runtime_start_cls(
            config_id=202,
            run_id=_run_id_for(202),
            trigger_source="plc",
        )
    )

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.manual_primary_config_id == 101
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert states == [controller.state]


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


def test_set_selection_preserves_review_context_for_noop_resubmit():
    controller_mod = _load_factory_mode_controller_module()

    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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

    next_state = controller.set_selection((101, 202), manual_primary_config_id=101)

    assert next_state == original_state
    assert controller.state == original_state
    assert controller.review_payload == original_review_payload
    assert controller.state.run_phase == "reviewing"
    assert controller.state.displayed_config_id == 202
    assert controller.state.trigger_source == "plc"
    assert reset_reasons == []
    assert reviews == []
    assert states == []


def test_set_selection_from_review_to_invalid_primary_goes_directly_to_not_ready():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

    reset_reasons.clear()
    reviews.clear()
    states.clear()

    controller.set_selection((101, 202), manual_primary_config_id=None)

    assert controller.review_payload == review_payload_cls()
    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.manual_primary_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.trigger_source is None
    assert controller.state.run_phase == "not_ready"
    assert controller.state.is_ready is False
    assert reset_reasons == ["selection_changed"]
    assert reviews == [review_payload_cls()]
    assert states == [controller.state]
    assert states[0].run_phase == "not_ready"


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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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


def test_factory_mode_controller_rejects_start_when_backend_session_is_already_running():
    controller_mod = _load_factory_mode_controller_module()
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.is_running.return_value = True
    controller = controller_mod.FactoryModeController(EventBus(), services)

    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.state.run_phase == "armed"
    assert controller.state.is_ready is True
    assert controller.can_arm() is False
    assert controller.start_manual({"sn": "SN-1"}) is False
    services.test.is_running.assert_called()
    services.test.start.assert_not_called()


def test_factory_mode_controller_manual_start_resolves_to_primary_only():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=202)

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


@pytest.mark.parametrize(
    ("configs", "manual_primary_config_id", "plc_device_key", "expected_config_id"),
    [
        (
            (
                _make_config(101, plc_device_key="plc-a"),
                _make_config(202, plc_device_key="plc-b"),
            ),
            101,
            "plc-b",
            202,
        ),
        (
            (
                _make_config(101, plc_device_key="plc-a"),
                _make_config(202, plc_device_key="plc-a"),
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
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202), manual_primary_config_id=manual_primary_config_id)

    accepted = controller.handle_plc_event(services_mod.FactoryPlcIngressPayload(plc_device_key=plc_device_key))

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
    _accept_start(services)
    controller = controller_mod.FactoryModeController(EventBus(), services)
    controller.set_selection((101, 202), manual_primary_config_id=manual_primary_config_id)

    accepted = controller.handle_scan_event(services_mod.FactoryScanIngressPayload(scan_text="SN-1"))

    assert accepted is True
    _assert_start_called_with(
        services,
        {"config_id": expected_config_id},
        expected_trigger_source="scan",
    )
    assert controller.state.active_config_id == expected_config_id
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "scan"


@pytest.mark.parametrize("phase", ["running", "analyzing"])
def test_factory_mode_controller_applies_deferred_same_id_plc_refresh_after_active_phase(phase):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    services = _make_dynamic_services(config_map)
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.start_manual() is True
    services.test.start.reset_mock()

    if phase == "analyzing":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))

    config_map[202]["plc_device_key"] = "plc-b"
    bus.config_changed.emit()

    _finish_active_run_to_reviewing(
        controller,
        bus,
        config_id=101,
        trigger_source="manual",
    )

    accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_device_key="plc-b")
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
    controller.set_selection((101, 202), manual_primary_config_id=101)

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
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(EventBus(), services)

    plc_accepted = controller.handle_plc_event(services_mod.FactoryPlcIngressPayload(plc_device_key="plc-a"))
    scan_accepted = controller.handle_scan_event(services_mod.FactoryScanIngressPayload(scan_text="SN-1"))

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
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)

    plc_accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_device_key="plc-a")
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
def test_factory_mode_controller_logs_and_drops_plc_and_scan_during_active_phase(phase):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    log_messages: list[str] = []
    bus.log.connect(log_messages.append)
    services = _make_services(
        (
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)

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
        services_mod.FactoryPlcIngressPayload(plc_device_key="plc-b")
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

    assert controller.state.run_phase == "reviewing" if phase != "running" else controller.state.run_phase == "running"
    services.test.start.assert_not_called()


@pytest.mark.parametrize(
    ("configs", "plc_device_key"),
    [
        (
            (
                _make_config(101, plc_device_key="plc-a"),
                _make_config(202, plc_device_key="plc-b"),
            ),
            "plc-missing",
        ),
        (
            (
                _make_config(101),
                _make_config(202, plc_device_key=""),
            ),
            "plc-a",
        ),
    ],
)
def test_factory_mode_controller_logs_ignored_plc_no_match_for_current_pair(
    configs,
    plc_device_key,
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
    controller.set_selection((101, 202), manual_primary_config_id=101)

    accepted = controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_device_key=plc_device_key)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)

    accepted = controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-404")
    )

    assert controller.state.run_phase == "armed"
    assert accepted is False
    services.test.start.assert_not_called()
    _assert_log_contains(log_messages, "scan", "ignored", "no match")


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
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
            services_mod.FactoryPlcIngressPayload(plc_device_key="plc-b")
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
    states.clear()

    assert controller.start_manual({"sn": "first"}) is True
    controller.set_selection((303, 404), manual_primary_config_id=303)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
            _make_config(101, plc_device_key="plc-a", scan_triggers_test=True),
            _make_config(202, plc_device_key="plc-b", scan_triggers_test=True),
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    assert controller.handle_plc_event(
        services_mod.FactoryPlcIngressPayload(plc_device_key="plc-b")
    ) is False
    assert controller.handle_scan_event(
        services_mod.FactoryScanIngressPayload(scan_text="SN-1")
    ) is False
    assert len(callbacks) == 1
    assert services.test.start.call_count == 1

    callbacks[0](_run_id_for(101))

    assert len(states) == 2
    assert states[-1] == controller.state
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"


def test_factory_mode_controller_same_id_invalidating_refresh_stales_pending_manual_start():
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
    states.clear()

    assert controller.start_manual({"sn": "SN-1"}) is True
    assert controller._pending_start == (101, "manual")
    assert controller._pending_start_request_id

    config_map[202]["analysis_window_offset_ms"] = 4000
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.manual_primary_config_id == 101
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "not_ready"
    assert controller.state.trigger_source is None
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert controller.can_arm() is False
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].manual_primary_config_id == 101
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == controller.state
    assert accepted_payloads == []

    callbacks[0](_run_id_for(101))

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "not_ready"
    assert controller.state.trigger_source is None
    assert controller._pending_start is None
    assert controller._pending_start_request_id is None
    assert len(states) == 2
    assert states[1] == controller.state
    assert accepted_payloads == []


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
    controller.set_selection((101, 202), manual_primary_config_id=101)
    states.clear()

    assert controller.start_manual({"sn": "SN-1"}) is True
    pending_request_id = controller._pending_start_request_id
    assert pending_request_id

    config_map[202]["config_name"] = "Config 202 Updated"
    bus.config_changed.emit()

    assert controller.state.selected_config_ids == (101, 202)
    assert controller.state.manual_primary_config_id == 101
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller._pending_start == (101, "manual")
    assert controller._pending_start_request_id == pending_request_id
    assert controller.can_arm() is False
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].manual_primary_config_id == 101
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
    states.clear()

    assert controller.start_manual({"sn": "first"}) is True
    assert len(callbacks) == 1
    request_id = start_payloads[0].get("factory_start_request_id")
    assert isinstance(request_id, str)
    assert request_id

    next_state = controller.set_selection((303, 404), manual_primary_config_id=303)

    assert next_state == controller.state
    services.test.cancel_pending_start.assert_called_once_with(
        request_id=request_id,
        config_id=101,
    )
    assert controller.state.selected_config_ids == (303, 404)
    assert controller.state.manual_primary_config_id == 303
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller.can_arm() is True
    assert len(states) == 2
    assert states[0].selected_config_ids == (101, 202)
    assert states[0].manual_primary_config_id == 101
    assert states[0].active_config_id is None
    assert states[0].displayed_config_id is None
    assert states[0].run_phase == "armed"
    assert states[0].trigger_source is None
    assert states[0].start_ready is False
    assert states[0].start_ready_reason == "启动请求处理中"
    assert states[1] == controller.state

    callbacks[0](_run_id_for(101))

    assert controller.state.selected_config_ids == (303, 404)
    assert controller.state.manual_primary_config_id == 303
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert controller.can_arm() is True
    assert len(states) == 2
    assert states[1] == controller.state


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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    assert states[0].manual_primary_config_id == original_state.manual_primary_config_id
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
    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.start_manual({"sn": "first"}) is True
    first_request_id = start_payloads[0].get("factory_start_request_id")
    assert isinstance(first_request_id, str)
    assert first_request_id

    controller.set_selection((303, 404), manual_primary_config_id=303)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    assert states[0].manual_primary_config_id == 101
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
    controller.set_selection((101, 202), manual_primary_config_id=101)

    assert controller.start_manual({"sn": "first"}) is True
    controller.set_selection((303, 404), manual_primary_config_id=303)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
    original_review_payload = _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

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
    controller.set_selection((101, 202), manual_primary_config_id=101)
    original_review_payload = _enter_reviewing_state(controller, bus, config_id=202, trigger_source="plc")

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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
            _make_config(101, plc_device_key="plc-a"),
            _make_config(202, plc_device_key="plc-b"),
            _make_config(303),
            _make_config(404),
        )
    )
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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
    controller.set_selection((101, 202), manual_primary_config_id=101)
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


def test_factory_mode_controller_reanalysis_failure_returns_to_reviewing_without_result():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")

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
    assert controller.review_payload.summary_payload == _full_no_result_summary(
        "manual reanalysis failed"
    )
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None


def test_begin_reanalysis_clears_summary_payload_and_chart_payload_without_resetting_review_context():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    run_id = _run_id_for(101)
    review_payload = _make_review_payload(
        displayed_config_id=101,
        run_id=run_id,
        trigger_source="manual",
        judgment_payload={"overall": "合格"},
        summary_payload={"overall_status": "pass", "overall_label": "PASS", "rows": []},
        chart_payload={0: {"spl": {}, "fft": {}, "octave": {}}},
    )

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id=run_id,
        trigger_source="manual",
    )
    bus.factory_runtime_analyzing_entered.emit(
        _make_runtime_config_payload(config_id=101, run_id=run_id)
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(review_payload)
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reanalyzing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.run_id == run_id
    assert controller.review_payload.default_window == review_payload.default_window
    assert controller.review_payload.fallback_window == review_payload.fallback_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is False
    assert controller.review_payload.error_message is None
    assert controller.review_payload.summary_payload is None
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert reviews == [controller.review_payload]
    assert [state.run_phase for state in states] == ["reanalyzing"]


def test_factory_mode_controller_preserves_committed_partial_no_result_reanalysis_payload():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    original_review = _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")
    reviews.clear()
    states.clear()

    started = controller.begin_reanalysis(1.25, 3.25)
    partial_summary_payload = {
        "overall_status": "no_result",
        "overall_label": "无结果",
        "error_message": "AI timeout",
        "rows": [
            {
                "item_key": "spl",
                "label": "SPL",
                "deviation_value": -1.0,
                "deviation_display": "-1 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "fft",
                "label": "FFT",
                "deviation_value": -2.0,
                "deviation_display": "-2 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "octave",
                "label": "倍频程",
                "deviation_value": -3.0,
                "deviation_display": "-3 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "ai",
                "label": "AI分析",
                "deviation_value": None,
                "deviation_display": "--",
                "channel_index": None,
                "channel_label": "--",
                "status": "无结果",
                "constrained": True,
            },
        ],
    }
    partial_chart_payload = {
        0: {
            "spl": {"series": [70.0, 71.0]},
            "fft": {"series": [55.0, 48.0]},
            "octave": {"series": [57.0, 50.0]},
        }
    }
    committed_payload = _make_review_payload(
        displayed_config_id=101,
        run_id=original_review.run_id,
        trigger_source="manual",
        no_result=True,
        error_message="AI timeout",
        judgment_payload={"overall": "合格", "analysis_window": (1.25, 3.25)},
        summary_payload=partial_summary_payload,
        chart_payload=partial_chart_payload,
    )

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)

    controller.handle_runtime_reanalysis_completed(committed_payload)

    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "AI timeout"
    assert controller.review_payload.summary_payload is partial_summary_payload
    assert controller.review_payload.chart_payload is partial_chart_payload
    assert controller.review_payload.judgment_payload == {
        "overall": "合格",
        "analysis_window": (1.25, 3.25),
    }
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_reanalysis_missing_session_unwinds_to_reviewing_no_result():
    controller_mod = _load_factory_mode_controller_module()
    review_payload_cls = getattr(services_mod, "FactoryReviewPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    original_review = _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")
    reviews.clear()
    states.clear()
    summary_payload = _full_no_result_summary("no session for config 101")

    def _rejudge(config_id, start_sec, end_sec):
        error_message = f"no session for config {config_id}"
        bus.factory_runtime_reanalysis_completed.emit(
            review_payload_cls(
                displayed_config_id=config_id,
                run_id=None,
                trigger_source="manual",
                captured_duration_sec=None,
                default_window=None,
                fallback_window=None,
                manual_reanalysis_allowed=False,
                no_result=True,
                error_message=error_message,
                judgment_payload=None,
                summary_payload=summary_payload,
            )
        )

    services.test.rejudge.side_effect = _rejudge

    started = controller.begin_reanalysis(1.25, 3.25)

    assert started is True
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "no session for config 101"
    assert controller.review_payload.summary_payload is summary_payload
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_reanalysis_empty_exception_message_unwinds_to_reviewing_no_result():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    states: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    bus.factory_state_changed.connect(states.append)
    services = _make_services((_make_config(101), _make_config(202)))
    services.test.rejudge.side_effect = RuntimeError("")
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    original_review = _enter_reviewing_state(controller, bus, config_id=101, trigger_source="manual")
    reviews.clear()
    states.clear()

    try:
        started = controller.begin_reanalysis(1.25, 3.25)
    except RuntimeError as exc:
        started = exc

    assert started is False
    services.test.rejudge.assert_called_once_with(101, 1.25, 3.25)
    assert controller.state.run_phase == "reviewing"
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.review_payload.run_id == original_review.run_id
    assert controller.review_payload.default_window == original_review.default_window
    assert controller.review_payload.manual_reanalysis_allowed is True
    assert controller.review_payload.no_result is True
    assert controller.review_payload.error_message == "分析结果不可用"
    assert controller.review_payload.summary_payload == _full_no_result_summary("")
    assert controller.review_payload.chart_payload is None
    assert controller.review_payload.judgment_payload is None
    assert [state.run_phase for state in states] == ["reanalyzing", "reviewing"]
    assert reviews[-1] == controller.review_payload


def test_factory_mode_controller_ignores_mismatched_factory_runtime_review_payload():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()
    reviews.clear()

    controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(displayed_config_id=202, trigger_source="plc")
    )

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "analyzing"
    assert controller.state.trigger_source == "manual"
    assert reviews == []


@pytest.mark.parametrize(
    ("signal_name", "review_kwargs"),
    [
        ("factory_runtime_auto_analysis_succeeded", {}),
        (
            "factory_runtime_auto_analysis_no_result",
            {"no_result": True, "judgment_payload": None},
        ),
        (
            "factory_runtime_auto_analysis_error",
            {
                "no_result": True,
                "error_message": "analysis window missing",
                "judgment_payload": None,
            },
        ),
    ],
)
def test_factory_mode_controller_requires_analyzing_before_reviewing_for_auto_completion(
    signal_name,
    review_kwargs,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    run_id = _run_id_for(101)

    _emit_owned_runtime_start(
        controller,
        bus,
        config_id=101,
        run_id=run_id,
        trigger_source="manual",
    )
    reviews.clear()

    getattr(bus, signal_name).emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=run_id,
            trigger_source="manual",
            **review_kwargs,
        )
    )

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert reviews == []


@pytest.mark.parametrize(
    ("signal_name", "payload_kwargs"),
    [
        (
            "factory_runtime_auto_analysis_no_result",
            {"no_result": True, "judgment_payload": None},
        ),
        (
            "factory_runtime_auto_analysis_error",
            {
                "no_result": True,
                "error_message": "analysis window missing",
                "judgment_payload": None,
            },
        ),
    ],
)
def test_factory_mode_controller_ignores_out_of_band_auto_analysis_failure_payloads_while_armed(
    signal_name,
    payload_kwargs,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    reviews.clear()

    getattr(bus, signal_name).emit(_make_review_payload(**payload_kwargs))

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reviews == []


@pytest.mark.parametrize(
    ("reset_kind", "signal_name", "payload_kwargs"),
    [
        (
            "stop",
            "factory_runtime_auto_analysis_no_result",
            {"no_result": True, "judgment_payload": None},
        ),
        (
            "stop",
            "factory_runtime_auto_analysis_error",
            {
                "no_result": True,
                "error_message": "analysis window missing",
                "judgment_payload": None,
            },
        ),
        (
            "abort",
            "factory_runtime_auto_analysis_no_result",
            {"no_result": True, "judgment_payload": None},
        ),
        (
            "abort",
            "factory_runtime_auto_analysis_error",
            {
                "no_result": True,
                "error_message": "analysis window missing",
                "judgment_payload": None,
            },
        ),
    ],
)
def test_factory_mode_controller_ignores_out_of_band_auto_analysis_failure_payloads_after_stop_or_abort(
    reset_kind,
    signal_name,
    payload_kwargs,
):
    controller_mod = _load_factory_mode_controller_module()
    abort_payload_cls = getattr(services_mod, "FactoryRuntimeAbortPayload", None)

    assert abort_payload_cls is not None, "FactoryRuntimeAbortPayload is missing"

    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()

    if reset_kind == "stop":
        controller.handle_runtime_analyzing(_make_runtime_config_payload(config_id=101))
        reviews.clear()
        assert controller.stop_active_run() is True
    else:
        reviews.clear()
        bus.factory_runtime_aborted.emit(
            _make_abort_payload(
                config_id=101,
                trigger_source="manual",
                error_message="monitor disconnected",
            )
        )

    reviews.clear()

    getattr(bus, signal_name).emit(_make_review_payload(**payload_kwargs))

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reviews == []


def test_factory_mode_controller_ignores_late_same_config_run_id_events_after_stop_and_restart():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
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


def test_factory_mode_controller_recovers_from_factory_runtime_abort_mid_run():
    controller_mod = _load_factory_mode_controller_module()
    abort_payload_cls = getattr(services_mod, "FactoryRuntimeAbortPayload", None)

    assert abort_payload_cls is not None, "FactoryRuntimeAbortPayload is missing"

    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()
    reviews.clear()

    bus.factory_runtime_aborted.emit(
        _make_abort_payload(
            config_id=101,
            trigger_source="manual",
            error_message="monitor disconnected",
        )
    )

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reset_reasons == ["runtime_aborted"]
    assert reviews == [services_mod.FactoryReviewPayload()]


def test_factory_mode_controller_ignores_out_of_band_factory_runtime_abort():
    controller_mod = _load_factory_mode_controller_module()
    abort_payload_cls = getattr(services_mod, "FactoryRuntimeAbortPayload", None)

    assert abort_payload_cls is not None, "FactoryRuntimeAbortPayload is missing"

    bus = EventBus()
    reset_reasons: list[str] = []
    reviews: list[object] = []
    bus.factory_display_reset.connect(reset_reasons.append)
    bus.factory_review_changed.connect(reviews.append)
    services = _make_services((_make_config(101), _make_config(202)))
    _accept_start(services)
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202), manual_primary_config_id=101)
    controller.start_manual()
    reviews.clear()

    bus.factory_runtime_aborted.emit(
        _make_abort_payload(
            config_id=202,
            trigger_source="plc",
            error_message="recorder exploded",
        )
    )

    assert controller.state.active_config_id == 101
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "running"
    assert controller.state.trigger_source == "manual"
    assert reset_reasons == []
    assert reviews == []


def test_factory_mode_controller_consumes_real_test_manager_lifecycle_signals(monkeypatch):
    controller_mod = _load_factory_mode_controller_module()

    class FakeRecorder:
        def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
            self._sample_rate = float(sample_rate)
            self._n_channels = n_channels
            self._record_duration = float(record_duration)
            self._buffer = np.zeros(
                (int(self._sample_rate * self._record_duration), n_channels),
                dtype=np.float32,
            )
            self._running = False
            self.chunk_ready = _DummySignal()
            self.recording_done = _DummySignal()
            self.error_occurred = _DummySignal()

        def isRunning(self):
            return self._running

        def start(self):
            self._running = True

        def stop(self):
            self._running = False

        def wait(self, timeout):
            return True

        def attach_session(self, *, record_duration):
            return MagicMock(chunk_index=0, sample_index=0)

        def get_buffer_from(self, snapshot):
            return self._buffer

    class FakeSession:
        def __init__(self, config, bus, ai_service, mic_cal, workorder, audio_dir):
            self.config_id = config["config_id"]
            self._config = dict(config)
            self._workorder = dict(workorder)
            self._buffer = None
            self._sample_rate = None
            self._finished = False
            self._attach_snapshot = None
            self._audio_file_path = "captured.wav"
            self._audio_data_id = "audio-123"

        def _setup_pipelines(self):
            return None

        def prepare_for_attach(self):
            return None

        def attach_to_recorder(self, *, chunk_index, snapshot=None):
            self._attach_snapshot = snapshot

        def _on_chunk(self, *args, **kwargs):
            return None

        def _save_wav(self, *args, **kwargs):
            return "captured.wav"

        def _run_judgment(self):
            self._audio_file_path = "captured.wav"
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.91,
                analysis_window=(1.0, 3.0),
            )

        def rejudge(self, start_sec, end_sec):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.97,
                analysis_window=(start_sec, end_sec),
            )

    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
    monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

    config_map = {
        101: _make_config(101),
        202: _make_config(202),
    }
    bus = EventBus()
    services = MagicMock()
    services.bus = bus
    services.config_service = MagicMock()
    services.config_service.load_config.side_effect = lambda config_id: dict(config_map[config_id])
    services.config_service.validate_factory_runtime.side_effect = lambda config: []
    services.ai_service = MagicMock()
    services.ai_service.resolve_binding.return_value = None
    services.hss_service = MagicMock()
    services.hss_service.load_io_devices.return_value = {
        "input": {
            "id": 0,
            "name": "Mic",
            "type": "MME",
            "samplerate": "100",
            "channel_count": "1",
        },
        "output": {
            "id": 1,
            "name": "Speaker",
            "type": "MME",
            "samplerate": "100",
            "channel_count": "1",
        },
        "mic_cal": {},
    }
    services.test = TestManager(bus, services)
    states: list[object] = []
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus, services)

    controller.set_selection((101, 202), manual_primary_config_id=101)

    started = controller.start_manual({"sn": "SN-1"})

    assert started is True
    assert controller.state.run_phase == "running"
    services.test._on_session_done(101)

    assert controller.state.run_phase == "reviewing"
    assert controller.review_payload.displayed_config_id == 101
    assert controller.review_payload.trigger_source == "manual"
    assert controller.review_payload.judgment_payload["analysis_window"] == (1.0, 3.0)

    reanalyzing_started = controller.begin_reanalysis(1.25, 3.25)

    assert reanalyzing_started is True
    assert controller.state.run_phase == "reviewing"
    assert controller.review_payload.judgment_payload["analysis_window"] == (1.25, 3.25)
    assert [state.run_phase for state in states] == [
        "armed",
        "running",
        "analyzing",
        "reviewing",
        "reanalyzing",
        "reviewing",
    ]


def test_factory_mode_controller_direct_backend_stop_unwinds_real_manager_run(monkeypatch):
    controller, services, reset_reasons, reviews, _, recorder_cls = _make_real_manager_factory_context(
        monkeypatch
    )

    started = controller.start_manual({"sn": "SN-1"})

    assert started is True
    assert controller.state.run_phase == "running"

    reset_reasons.clear()
    reviews.clear()

    services.test.stop(config_id=101)

    assert services.test.is_running(config_id=101) is False
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reset_reasons == ["runtime_aborted"]
    assert reviews == [services_mod.FactoryReviewPayload()]
    assert recorder_cls.instances[0].stop_calls == 1
    assert recorder_cls.instances[0].wait_calls == [3000]


def test_factory_mode_controller_stop_active_run_remains_clean_when_partial_save_fails(
    monkeypatch,
):
    controller, services, reset_reasons, reviews, _, recorder_cls = _make_real_manager_factory_context(
        monkeypatch,
        save_wav_error=RuntimeError("disk full"),
    )

    started = controller.start_manual({"sn": "SN-1"})

    assert started is True
    assert controller.state.run_phase == "running"

    reset_reasons.clear()
    reviews.clear()

    stopped = controller.stop_active_run()

    assert stopped is True
    assert services.test.is_running(config_id=101) is False
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id is None
    assert controller.state.run_phase == "armed"
    assert controller.state.trigger_source is None
    assert reset_reasons == ["active_run_stopped"]
    assert reviews == [services_mod.FactoryReviewPayload()]
    assert recorder_cls.instances[0].stop_calls == 1
    assert recorder_cls.instances[0].wait_calls == [3000]
