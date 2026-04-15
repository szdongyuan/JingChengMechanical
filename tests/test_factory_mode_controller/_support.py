from __future__ import annotations

import importlib
import importlib.util
from unittest.mock import MagicMock

import numpy as np
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
    plc_id: int | None = None,
    scan_triggers_test: bool = False,
):
    return {
        "config_id": config_id,
        "config_name": f"Config {config_id}",
        "record_duration": 5.0,
        "analysis_duration": 2.0,
        "analysis_window_offset_ms": 0,
        "plc_id": plc_id,
        "scan_triggers_test": scan_triggers_test,
    }


def _make_services(configs, *, validation_errors: dict[int, list[str]] | None = None):
    config_map = {config["config_id"]: dict(config) for config in configs}
    services = MagicMock()
    services.test = MagicMock()
    services.test.is_running.return_value = False
    services.config_service = MagicMock()
    services.config_service.list_configs.side_effect = lambda: [
        {"config_id": config_id, "config_name": config["config_name"]}
        for config_id, config in sorted(config_map.items())
    ]
    services.config_service.load_config.side_effect = lambda config_id: dict(
        config_map[config_id]
    )
    services.config_service.validate_factory_runtime.side_effect = (
        lambda config: list((validation_errors or {}).get(config["config_id"], ()))
    )
    services.config_service.delete_config.side_effect = (
        lambda config_id: config_map.pop(config_id, None) is not None
    )
    services.factory_selection_settings = _SelectionSettingsStub(
        saved_selected_ids=tuple(config_map)
    )
    return services


def _make_dynamic_services(config_map, *, validate_factory_runtime=None):
    services = MagicMock()
    services.test = MagicMock()
    services.test.is_running.return_value = False
    services.config_service = MagicMock()
    services.config_service.list_configs.side_effect = lambda: [
        {"config_id": config_id, "config_name": config["config_name"]}
        for config_id, config in sorted(config_map.items())
    ]
    services.config_service.load_config.side_effect = lambda config_id: dict(
        config_map[config_id]
    )
    services.config_service.validate_factory_runtime.side_effect = (
        validate_factory_runtime
        if validate_factory_runtime is not None
        else (lambda config: [])
    )
    services.config_service.delete_config.side_effect = (
        lambda config_id: config_map.pop(config_id, None) is not None
    )
    services.factory_selection_settings = _SelectionSettingsStub(
        saved_selected_ids=tuple(config_map)
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


class _SelectionSettingsStub:
    def __init__(
        self,
        *,
        saved_selected_ids: tuple[int, ...] = (),
        save_results: list[str | None] | None = None,
    ):
        self._saved_selected_ids = tuple(saved_selected_ids)
        self._save_results = list(save_results or [])
        self.save_selected_config_ids = MagicMock(side_effect=self._save)
        self.load_selected_config_ids = MagicMock(side_effect=self._load)

    def _load(self) -> tuple[int, ...]:
        return self._saved_selected_ids

    def _save(self, config_ids: tuple[int, ...]) -> str | None:
        if self._save_results:
            result = self._save_results.pop(0)
        else:
            result = None
        if result is None:
            self._saved_selected_ids = tuple(config_ids)
        return result


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


def _make_real_manager_factory_context(
    monkeypatch,
    *,
    save_wav_error: Exception | None = None,
):
    controller_mod = _load_factory_mode_controller_module()

    class FakeRecorder:
        instances = []

        def __init__(
            self,
            *,
            device_name,
            hostapi,
            sample_rate,
            n_channels,
            record_duration,
        ):
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
    services.config_service.load_config.side_effect = lambda config_id: dict(
        config_map[config_id]
    )
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
    controller.set_selection((101, 202))
    return controller, services, reset_reasons, reviews, states, FakeRecorder
