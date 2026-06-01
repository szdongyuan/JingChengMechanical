from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication, QEventLoop
import app.services_container as services_mod
from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager
from app.services_container import EventBus

from ._support import (
    _DummySignal,
    _accept_start,
    _emit_owned_runtime_start,
    _load_factory_mode_controller_module,
    _make_abort_payload,
    _make_config,
    _make_real_manager_factory_context,
    _make_review_payload,
    _make_runtime_config_payload,
    _make_services,
    _run_id_for,
)


_QT_APP = None


def _process_qt_events():
    global _QT_APP
    app = QCoreApplication.instance()
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        app = QApplication([])
        _QT_APP = app
    for _ in range(3):
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)


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
    controller.set_selection((101, 202))
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
    controller.set_selection((101, 202))
    controller.start_manual()
    run_id = _run_id_for(101)

    if reset_kind == "stop":
        controller.handle_runtime_analyzing(
            _make_runtime_config_payload(config_id=101, run_id=run_id)
        )
        reviews.clear()
        assert controller.stop_active_run() is True
        bus.factory_runtime_stopped.emit(
            _make_stopped_payload(
                config_id=101,
                run_id=run_id,
                trigger_source="manual",
            )
        )
        expected_displayed_config_id = 101
        expected_phase = "stopped"
        expected_trigger_source = "manual"
    else:
        reviews.clear()
        bus.factory_runtime_aborted.emit(
            _make_abort_payload(
                config_id=101,
                trigger_source="manual",
                error_message="monitor disconnected",
            )
        )
        expected_displayed_config_id = None
        expected_phase = "armed"
        expected_trigger_source = None

    reviews.clear()

    getattr(bus, signal_name).emit(_make_review_payload(**payload_kwargs))

    assert controller.review_payload == services_mod.FactoryReviewPayload()
    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == expected_displayed_config_id
    assert controller.state.run_phase == expected_phase
    assert controller.state.trigger_source == expected_trigger_source
    assert reviews == []


def test_factory_mode_controller_ignores_late_auto_analysis_completion_after_runtime_stopped():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    reviews: list[object] = []
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
    controller.handle_runtime_analyzing(
        _make_runtime_config_payload(config_id=101, run_id="run-1")
    )
    controller.handle_runtime_stopped(
        _make_stopped_payload(config_id=101, run_id="run-1", trigger_source="manual")
    )
    reviews.clear()

    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id="run-1",
            trigger_source="manual",
        )
    )

    assert controller.state.run_phase == "stopped"
    assert controller.review_payload == services_mod.FactoryReviewPayload()
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
    controller.set_selection((101, 202))
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
    controller.set_selection((101, 202))
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


def test_factory_mode_controller_real_manager_auto_completion_gates_start_readiness_until_cleanup_settles(
    monkeypatch,
):
    controller, services, _, _, states, _ = _make_real_manager_factory_context(monkeypatch)
    review_signal_snapshots: list[tuple[object, bool]] = []

    bus = services.bus
    bus.factory_review_changed.connect(
        lambda *_: review_signal_snapshots.append(
            (controller.state, services.test.is_running())
        )
    )

    started = controller.start_manual({"sn": "SN-1"})

    assert started is True
    assert controller.state.run_phase == "running"

    state_count_before_completion = len(states)

    services.test._on_session_done(101)

    completion_states = states[state_count_before_completion:]
    assert [state.run_phase for state in completion_states] == [
        "analyzing",
        "reviewing",
        "reviewing",
    ]
    assert review_signal_snapshots[-1][0].run_phase == "reviewing"
    assert review_signal_snapshots[-1][0].start_ready is False
    assert review_signal_snapshots[-1][0].start_ready_reason == "当前测试运行中"
    assert review_signal_snapshots[-1][1] is True
    assert completion_states[-1].run_phase == "reviewing"
    assert completion_states[-1].start_ready is True
    assert completion_states[-1].start_ready_reason is None
    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert services.test.is_running() is False
    assert services.test._sessions == {}
    assert 101 in services.test._completed_sessions


def test_factory_mode_controller_restores_start_ready_after_real_manager_monitor_startup_abort(
    monkeypatch,
):
    controller, services, reset_reasons, reviews, states, _ = (
        _make_real_manager_factory_context(monkeypatch, with_monitor=True)
    )

    started = controller.start_manual({"sn": "SN-1"})

    assert started is True
    assert controller.state.run_phase == "running"
    assert services.test.is_running(config_id=101) is True
    monitor = services.test._monitors[101]
    assert monitor.start_calls == 1

    monitor.error_occurred.emit("Device not found: Speaker (MME)")
    _process_qt_events()

    assert services.test.is_running(config_id=101) is False
    assert services.test.is_running() is False
    assert controller.state.run_phase == "armed"
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None
    assert reset_reasons[-1] == "runtime_aborted"
    assert reviews[-1] == services_mod.FactoryReviewPayload()
    assert states[-1].run_phase == "armed"
    assert states[-1].start_ready is True
    assert states[-1].start_ready_reason is None
    assert monitor.stop_calls == 1
    assert monitor.wait_calls == [1000]

    restarted = controller.start_manual({"sn": "SN-2"})

    assert restarted is True
    assert controller.state.run_phase == "running"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "当前测试运行中"


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
def test_factory_mode_controller_rechecks_start_readiness_after_cleanup_settled_signal(
    signal_name,
    review_kwargs,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((202, 101))
    run_id = _run_id_for(101)
    backend_running = False

    def _is_running(config_id=None):
        return backend_running

    services.test.is_running.side_effect = _is_running

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
    backend_running = True

    review_signal = getattr(bus, signal_name)
    review_signal.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=run_id,
            trigger_source="manual",
            **review_kwargs,
        )
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "当前测试运行中"

    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "当前测试运行中"

    backend_running = False
    bus.factory_runtime_auto_analysis_cleanup_settled.emit(
        _make_runtime_config_payload(config_id=101, run_id=run_id)
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None


def test_factory_mode_controller_cleanup_settled_recomputes_review_readiness_instead_of_trusting_stale_reason_text():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    services.test.is_running.return_value = False
    run_id = _run_id_for(101)
    controller._review_payload = _make_review_payload(
        displayed_config_id=101,
        run_id=run_id,
        trigger_source="manual",
    )

    controller._state = replace(
        controller.state,
        selected_config_ids=(),
        active_config_id=None,
        displayed_config_id=101,
        run_phase="reviewing",
        trigger_source="manual",
        selection_ready=False,
        selection_ready_reason="请至少启用一个配置",
        start_ready=False,
        start_ready_reason="当前测试运行中",
    )

    bus.factory_runtime_auto_analysis_cleanup_settled.emit(
        _make_runtime_config_payload(config_id=101, run_id=run_id)
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "请至少启用一个配置"


def test_factory_mode_controller_cleanup_settled_does_not_enable_start_while_backend_still_running():
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    run_id = _run_id_for(101)

    services.test.is_running.return_value = True

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
    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=run_id,
            trigger_source="manual",
        )
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "当前测试运行中"

    bus.factory_runtime_auto_analysis_cleanup_settled.emit(
        _make_runtime_config_payload(config_id=101, run_id=run_id)
    )

    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is False
    assert controller.state.start_ready_reason == "当前测试运行中"


@pytest.mark.parametrize(
    ("signal_name", "review_kwargs"),
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
def test_factory_mode_controller_marks_start_ready_after_terminal_auto_analysis_failures(
    signal_name,
    review_kwargs,
):
    controller_mod = _load_factory_mode_controller_module()
    bus = EventBus()
    services = _make_services((_make_config(101), _make_config(202)))
    controller = controller_mod.FactoryModeController(bus, services)
    controller.set_selection((101, 202))
    run_id = _run_id_for(101)

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

    getattr(bus, signal_name).emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=run_id,
            trigger_source="manual",
            **review_kwargs,
        )
    )

    assert controller.state.active_config_id is None
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "reviewing"
    assert controller.state.start_ready is True
    assert controller.state.start_ready_reason is None


def test_factory_mode_controller_consumes_real_test_manager_lifecycle_signals(monkeypatch):
    controller_mod = _load_factory_mode_controller_module()

    class FakeRecorder:
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
    states: list[object] = []
    bus.factory_state_changed.connect(states.append)
    controller = controller_mod.FactoryModeController(bus, services)

    controller.set_selection((101, 202))

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
    assert controller.review_payload.judgment_payload["analysis_window"] == (
        1.25,
        3.25,
    )
    assert [state.run_phase for state in states] == [
        "armed",
        "running",
        "analyzing",
        "reviewing",
        "reviewing",
        "reanalyzing",
        "reviewing",
    ]


def test_factory_mode_controller_direct_backend_stop_unwinds_real_manager_run(
    monkeypatch,
):
    controller, services, reset_reasons, reviews, _, recorder_cls = (
        _make_real_manager_factory_context(monkeypatch)
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


def test_factory_mode_controller_stop_active_run_consumes_real_manager_stopped_signal(
    monkeypatch,
):
    controller, services, reset_reasons, reviews, _, recorder_cls = (
        _make_real_manager_factory_context(
            monkeypatch,
            save_wav_error=RuntimeError("disk full"),
        )
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
    assert controller.state.displayed_config_id == 101
    assert controller.state.run_phase == "stopped"
    assert controller.state.trigger_source == "manual"
    assert reset_reasons == []
    assert reviews == []
    assert recorder_cls.instances[0].stop_calls == 1
    assert recorder_cls.instances[0].wait_calls == [3000]
