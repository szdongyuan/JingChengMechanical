"""Tests for TestManager (replaces fake TestService)."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager


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


class _FakeSession:
    def __init__(self, config, bus, ai_service, mic_cal, workorder, audio_dir):
        self.config_id = config["config_id"]
        self._config = config
        self._workorder = dict(workorder)
        self._buffer = None
        self._sample_rate = None
        self._finished = False
        self._attach_snapshot = None
        self._attach_chunk_index = 0
        self._on_session_done_cb = None

    def _setup_pipelines(self):
        return None

    def attach_to_recorder(self, *, chunk_index, snapshot=None):
        self._attach_chunk_index = chunk_index
        self._attach_snapshot = snapshot

    def _on_chunk(self, *args, **kwargs):
        return None

    def _run_judgment(self):
        raise AssertionError("not used in this test")

    def _save_wav(self, *args, **kwargs):
        return None


def _mock_bus():
    bus = MagicMock()
    for sig in (
        "log",
        "status",
        "result_changed",
        "stft_updated",
        "spl_updated",
        "octave_updated",
        "fft_updated",
        "recording_progress",
        "test_started",
        "test_finished",
        "factory_runtime_analyzing_entered",
        "factory_runtime_auto_analysis_succeeded",
        "factory_runtime_auto_analysis_no_result",
        "factory_runtime_auto_analysis_error",
        "factory_runtime_aborted",
        "factory_runtime_reanalysis_completed",
    ):
        setattr(bus, sig, MagicMock())
        getattr(bus, sig).emit = MagicMock()
    return bus


def _mock_services(bus):
    services = MagicMock()
    services.bus = bus
    services.config_service = MagicMock()
    services.ai_service = MagicMock()
    services.ai_service.resolve_binding.return_value = None
    services.hss_service = MagicMock()
    services.hss_service.load_io_devices.return_value = {
        "input": {
            "id": 0,
            "name": "Mic",
            "type": "MME",
            "samplerate": "44100",
            "channel_count": "2",
        },
        "output": {
            "id": 1,
            "name": "Speaker",
            "type": "MME",
            "samplerate": "44100",
            "channel_count": "2",
        },
        "mic_cal": {},
    }
    return services


def _expected_no_result_payload(error_summary: str) -> dict:
    return {
        "no_result": True,
        "has_result": False,
        "display_status": "no_result",
        "ai_status": "no_result",
        "ai_label": None,
        "ai_score": None,
        "ai_summary": None,
        "error_summary": error_summary,
    }


class TestTestManagerInit:
    def test_init(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        assert mgr.is_running() is False


class TestTestManagerStartStop:
    def test_start_passes_input_device_identity_to_recorder(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self.device_name = device_name
                self.hostapi = hostapi
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})

        recorder = FakeRecorder.instances[0]
        assert recorder.device_name == "InputMic"
        assert recorder.hostapi == "InputAPI"

    def test_start_without_config_id_logs_error(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        accepted = mgr.start({"sn": "123"})
        assert accepted is False
        bus.log.emit.assert_called()
        assert mgr.is_running() is False

    def test_start_with_config_id_creates_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        accepted = mgr.start({"config_id": 1, "sn": "SN1"})
        assert accepted is True
        assert mgr.is_running(config_id=1) is True
        bus.test_started.emit.assert_called_with(1)

    def test_start_returns_false_when_config_load_fails(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.side_effect = RuntimeError("missing config")
        mgr = TestManager(bus, services)

        accepted = mgr.start({"config_id": 1, "sn": "SN1"})

        assert accepted is False
        assert mgr.is_running(config_id=1) is False
        bus.log.emit.assert_called()

    def test_start_same_config_failed_restart_preserves_completed_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.side_effect = RuntimeError("missing config")
        mgr = TestManager(bus, services)
        prior_session = MagicMock()
        mgr._completed_sessions[1] = prior_session

        accepted = mgr.start({"config_id": 1, "sn": "SN1"})

        assert accepted is False
        assert mgr._completed_sessions[1] is prior_session

    def test_start_monitor_failure_before_acceptance_preserves_completed_session_and_cleans_up(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": "Speaker",
            "output_device_hostapi": "MME",
            "output_device_samplerate": 44100,
        }

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros(
                    (int(sample_rate * record_duration), n_channels),
                    dtype=np.float32,
                )
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self.stop_calls = 0
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                raise RuntimeError("recorder boom")

            def stop(self):
                self.stop_calls += 1
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(self._sample_rate * record_duration), self._n_channels),
                        dtype=np.float32,
                    )
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.error_occurred = _DummySignal()
                self.stop_calls = 0
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                return None

            def start(self):
                raise RuntimeError("monitor boom")

            def stop(self):
                self.stop_calls += 1

            def wait(self, timeout):
                return True

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        prior_session = MagicMock()
        mgr = TestManager(bus, services)
        mgr._completed_sessions[1] = prior_session
        accepted_run_ids: list[str] = []

        try:
            accepted = mgr.start(
                {"config_id": 1, "sn": "SN1"},
                on_start_accepted=accepted_run_ids.append,
            )
        except RuntimeError as exc:
            accepted = exc

        assert accepted is False
        assert accepted_run_ids == []
        assert mgr._completed_sessions[1] is prior_session
        assert mgr.is_running(config_id=1) is False
        assert 1 not in mgr._recorders
        assert mgr._shared_recorders == {}
        assert FakeRecorder.instances[0].stop_calls == 1
        assert FakeMonitor.instances[0].stop_calls == 1
        bus.factory_runtime_aborted.emit.assert_not_called()
        bus.test_started.emit.assert_not_called()

    def test_start_calls_on_start_accepted_with_factory_run_id_before_recorder_start_and_before_test_started(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }

        events: list[str] = []

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self.device_name = device_name
                self.hostapi = hostapi
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros(
                    (int(sample_rate * record_duration), n_channels),
                    dtype=np.float32,
                )
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                events.append("recorder.start")
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(self._sample_rate * record_duration), self._n_channels),
                        dtype=np.float32,
                    )
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        prior_session = MagicMock()
        mgr = TestManager(bus, services)
        mgr._completed_sessions[1] = prior_session
        bus.test_started.emit.side_effect = lambda config_id: events.append(
            f"test_started:{config_id}"
        )
        accepted_snapshots: list[dict[str, object]] = []

        def on_start_accepted(run_id):
            accepted_snapshots.append(
                {
                    "completed_session": mgr._completed_sessions.get(1),
                    "events": list(events),
                    "recorder_running": FakeRecorder.instances[0]._running,
                    "run_id": run_id,
                    "session_run_id": mgr._sessions[1]._workorder["factory_run_id"],
                }
            )
            events.append("accepted")

        accepted = mgr.start(
            {"config_id": 1, "sn": "SN1"},
            on_start_accepted=on_start_accepted,
        )

        assert accepted is True
        assert accepted_snapshots == [
            {
                "completed_session": prior_session,
                "events": [],
                "recorder_running": False,
                "run_id": accepted_snapshots[0]["run_id"],
                "session_run_id": accepted_snapshots[0]["session_run_id"],
            }
        ]
        assert isinstance(accepted_snapshots[0]["run_id"], str)
        assert accepted_snapshots[0]["run_id"]
        assert accepted_snapshots[0]["run_id"] == accepted_snapshots[0]["session_run_id"]
        assert events == ["accepted", "recorder.start", "test_started:1"]
        assert 1 not in mgr._completed_sessions
        assert FakeRecorder.instances[0]._running is True

    def test_start_recorder_failure_after_acceptance_emits_factory_runtime_aborted_and_restores_completed_session(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }

        events: list[str] = []

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros(
                    (int(sample_rate * record_duration), n_channels),
                    dtype=np.float32,
                )
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self.stop_calls = 0
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                events.append("recorder.start")
                raise RuntimeError("recorder boom")

            def stop(self):
                self.stop_calls += 1
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros(
                        (int(self._sample_rate * record_duration), self._n_channels),
                        dtype=np.float32,
                    )
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        prior_session = MagicMock()
        mgr = TestManager(bus, services)
        mgr._completed_sessions[1] = prior_session
        accepted_snapshots: list[dict[str, object]] = []

        def on_start_accepted(run_id):
            accepted_snapshots.append(
                {
                    "completed_session": mgr._completed_sessions.get(1),
                    "events": list(events),
                    "recorder_running": FakeRecorder.instances[0]._running,
                    "run_id": run_id,
                    "session_run_id": mgr._sessions[1]._workorder["factory_run_id"],
                }
            )
            events.append("accepted")

        accepted = mgr.start(
            {"config_id": 1, "sn": "SN1", "trigger_source": "manual"},
            on_start_accepted=on_start_accepted,
        )

        assert accepted is False
        assert accepted_snapshots == [
            {
                "completed_session": prior_session,
                "events": [],
                "recorder_running": False,
                "run_id": accepted_snapshots[0]["run_id"],
                "session_run_id": accepted_snapshots[0]["session_run_id"],
            }
        ]
        assert accepted_snapshots[0]["run_id"] == accepted_snapshots[0]["session_run_id"]
        assert events == ["accepted", "recorder.start"]
        abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
        assert abort_payload.config_id == 1
        assert abort_payload.run_id == accepted_snapshots[0]["run_id"]
        assert abort_payload.trigger_source == "manual"
        assert abort_payload.error_message == "recorder boom"
        assert mgr._completed_sessions[1] is prior_session
        assert mgr.is_running(config_id=1) is False
        assert 1 not in mgr._recorders
        assert mgr._shared_recorders == {}
        assert FakeRecorder.instances[0].stop_calls == 1
        bus.test_started.emit.assert_not_called()

    def test_start_keeps_newly_completed_session_when_recorder_finishes_before_start_returns(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 100,
            "output_device_name": None,
        }

        class FastRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = float(sample_rate)
                self._n_channels = n_channels
                self._record_duration = float(record_duration)
                self._buffer = np.ones(
                    (int(self._sample_rate * self._record_duration), n_channels),
                    dtype=np.float32,
                )
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                FastRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True
                self.recording_done.emit()

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FastSession(_FakeSession):
            def __init__(self, config, bus, ai_service, mic_cal, workorder, audio_dir):
                super().__init__(config, bus, ai_service, mic_cal, workorder, audio_dir)
                self._config = dict(config)
                self._audio_file_path = None

            def _run_judgment(self):
                self._audio_file_path = "captured.wav"
                return JudgmentResult(
                    overall="合格",
                    limit_pass=True,
                    ai_pass=True,
                    ai_score=0.95,
                    analysis_window=(0.0, 1.0),
                )

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FastRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FastSession)

        prior_session = MagicMock(name="prior_session")
        mgr = TestManager(bus, services)
        mgr._completed_sessions[1] = prior_session

        accepted = mgr.start({"config_id": 1, "sn": "SN1", "trigger_source": "manual"})

        assert accepted is True
        assert mgr.is_running(config_id=1) is False
        assert mgr._completed_sessions[1] is not prior_session
        assert mgr._completed_sessions[1]._workorder["sn"] == "SN1"
        assert mgr._completed_sessions[1]._workorder["trigger_source"] == "manual"
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()

    def test_stop_all(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr._recorders[1] = MagicMock()
        mgr.stop()
        assert mgr.is_running() is False

    def test_stop_by_config_id(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session1 = MagicMock()
        mock_session1.config_id = 1
        mock_session2 = MagicMock()
        mock_session2.config_id = 2
        mgr._sessions = {1: mock_session1, 2: mock_session2}
        mgr._recorders = {1: MagicMock(), 2: MagicMock()}
        mgr.stop(config_id=1)
        assert 1 not in mgr._sessions
        assert 2 in mgr._sessions

    def test_stop_with_bool_from_clicked_stops_all(self):
        """QPushButton.clicked emits bool. stop() should treat it as stop-all."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr._recorders[1] = MagicMock()
        mgr.stop(False)
        assert mgr.is_running() is False

    def test_stop_active_run_returns_true_when_factory_run_is_stopped(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        session = MagicMock()
        session.config_id = 1
        mgr._sessions[1] = session
        mgr._recorders[1] = MagicMock()

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        assert mgr.is_running(config_id=1) is False

    def test_stop_active_run_returns_false_when_factory_run_is_missing(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)

        stopped = mgr.stop_active_run(config_id=99)

        assert stopped is False

    def test_start_resolves_config_name(self):
        """WorkOrderModule sends config name, not config_id. Should resolve."""
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.list_configs.return_value = [
            {"config_id": 42, "config_name": "MyConfig"},
        ]
        services.config_service.load_config.return_value = {
            "config_id": 42,
            "config_name": "MyConfig",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        accepted = mgr.start({"config": "MyConfig", "sn": "SN1", "model": "M1"})
        assert accepted is True
        assert mgr.is_running(config_id=42) is True

    def test_start_passes_output_device_identity_to_monitor(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": "OutputSpeaker",
            "output_device_hostapi": "OutputAPI",
            "output_device_samplerate": 48000,
        }

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.device_name = device_name
                self.hostapi = hostapi
                self.sample_rate = sample_rate
                self.monitor_channel = monitor_channel
                self.error_occurred = _DummySignal()
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                return None

            def start(self):
                return None

            def stop(self):
                return None

            def wait(self, timeout):
                return True

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})

        monitor = FakeMonitor.instances[0]
        assert monitor.device_name == "OutputSpeaker"
        assert monitor.hostapi == "OutputAPI"
        assert monitor.sample_rate == 48000.0

    def test_start_uses_first_selected_channel_for_monitor(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [1],
            "channels": [{
                "channel_index": 1,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": "OutputSpeaker",
            "output_device_hostapi": "OutputAPI",
            "output_device_samplerate": 48000,
        }

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.monitor_channel = monitor_channel
                self.error_occurred = _DummySignal()
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                return None

            def start(self):
                return None

            def stop(self):
                return None

            def wait(self, timeout):
                return True

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})

        monitor = FakeMonitor.instances[0]
        assert monitor.monitor_channel == 1


class TestTestManagerRejudge:
    def test_rejudge_delegates_to_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)
        mock_session.rejudge.assert_called_with(1.0, 3.0)

    def test_rejudge_works_on_completed_session(self):
        """After recording completes, session moves to _completed_sessions.
        rejudge() should still find it there."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._completed_sessions[1] = mock_session
        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)
        mock_session.rejudge.assert_called_with(0.0, 2.0)

    def test_rejudge_emits_factory_runtime_reanalysis_completed_on_success(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {
            "trigger_source": "manual",
            "factory_run_id": "run-1",
        }
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
            analysis_window=(1.0, 3.0),
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.displayed_config_id == 1
        assert payload.run_id == "run-1"
        assert payload.trigger_source == "manual"
        assert payload.captured_duration_sec == 5.0
        assert payload.default_window.start_sec == 1.0
        assert payload.default_window.end_sec == 3.0
        assert payload.fallback_window is None
        assert payload.manual_reanalysis_allowed is True
        assert payload.no_result is False
        assert payload.error_message is None
        assert payload.judgment_payload == {
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": True,
            "ai_score": 0.88,
            "channel_results": [],
            "analysis_window": (1.0, 3.0),
        }

    def test_rejudge_without_session_emits_factory_runtime_reanalysis_completed(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)

        mgr.rejudge(config_id=99, start_sec=0.0, end_sec=2.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.displayed_config_id == 99
        assert payload.run_id is None
        assert payload.captured_duration_sec is None
        assert payload.default_window is None
        assert payload.fallback_window is None
        assert payload.manual_reanalysis_allowed is False
        assert payload.no_result is True
        assert payload.error_message == "no session for config 99"
        assert payload.judgment_payload is None
        bus.test_finished.emit.assert_not_called()
        bus.result_changed.emit.assert_not_called()

    def test_rejudge_required_window_failure_emits_no_result_payload_without_overwriting_trace(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        prior_result = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
        )
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {"factory_run_id": "run-1"}
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_result = prior_result
        mock_session.rejudge.side_effect = ValueError("analysis window not fully captured")
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)

        payload = _expected_no_result_payload("analysis window not fully captured")
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        factory_payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis window not fully captured"
        assert factory_payload.judgment_payload is None
        assert mock_session.last_result is prior_result

    def test_rejudge_none_result_emits_no_result_payload_without_overwriting_trace(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        prior_result = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
        )
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {"factory_run_id": "run-1"}
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_result = prior_result
        mock_session.rejudge.return_value = None
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)

        payload = _expected_no_result_payload("analysis result unavailable")
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        factory_payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis result unavailable"
        assert factory_payload.judgment_payload is None
        assert mock_session.last_result is prior_result


class TestSharedRecorderTeardown:
    def test_shared_recorder_not_stopped_while_other_session_uses_it(self):
        """When two sessions share a recorder, stopping one should NOT stop the recorder."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        shared_rec = MagicMock()
        mock_s1 = MagicMock()
        mock_s1._buffer = None
        mock_s2 = MagicMock()
        mock_s2._buffer = None
        mgr._sessions = {1: mock_s1, 2: mock_s2}
        mgr._recorders = {1: shared_rec, 2: shared_rec}
        mgr._shared_recorders = {"dev|MME|44100": shared_rec}
        mgr._stop_one(1)
        shared_rec.stop.assert_not_called()
        mgr._stop_one(2)
        shared_rec.stop.assert_called_once()


class TestSampleRateMismatch:
    def test_logs_warning_on_mismatch(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "48000",
                "channel_count": "2",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "48000",
                "channel_count": "2",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }
        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        calls = [str(c) for c in bus.log.emit.call_args_list]
        assert any("WARNING" in c and "sample rate" in c for c in calls)
        assert mgr.is_running(config_id=1) is True
        rec = mgr._recorders.get(1)
        assert rec is not None
        assert rec._sample_rate == 48000.0

    def test_start_builds_pipelines_with_resolved_device_sample_rate(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "48000",
                "channel_count": "2",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "48000",
                "channel_count": "2",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 44100,
            "output_device_name": None,
        }

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)

        mgr = TestManager(bus, services)

        mgr.start({"config_id": 1, "sn": "SN1"})

        session = mgr._sessions.get(1)
        assert session is not None
        assert len(session._pipelines) == 1
        assert session._pipelines[0].sample_rate == 48000.0

    def test_start_builds_pipelines_with_reused_recorder_sample_rate(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.side_effect = [
            {
                "input": {
                    "id": 0,
                    "name": "Mic",
                    "type": "MME",
                    "samplerate": "44100",
                    "channel_count": "2",
                },
                "output": {
                    "id": 1,
                    "name": "Speaker",
                    "type": "MME",
                    "samplerate": "44100",
                    "channel_count": "2",
                },
                "mic_cal": {},
            },
            {
                "input": {
                    "id": 0,
                    "name": "Mic",
                    "type": "MME",
                    "samplerate": "48000",
                    "channel_count": "2",
                },
                "output": {
                    "id": 1,
                    "name": "Speaker",
                    "type": "MME",
                    "samplerate": "48000",
                    "channel_count": "2",
                },
                "mic_cal": {},
            },
        ]
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Second",
                "record_duration": 2.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
        ]

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)

        mgr = TestManager(bus, services)

        mgr.start({"config_id": 1, "sn": "SN1"})
        mgr.start({"config_id": 2, "sn": "SN2"})

        first_session = mgr._sessions.get(1)
        second_session = mgr._sessions.get(2)
        shared_recorder = mgr._recorders.get(1)

        assert first_session is not None
        assert second_session is not None
        assert shared_recorder is not None
        assert shared_recorder is mgr._recorders.get(2)
        assert shared_recorder._sample_rate == 44100.0
        assert len(first_session._pipelines) == 1
        assert len(second_session._pipelines) == 1
        assert first_session._pipelines[0].sample_rate == 44100.0
        assert second_session._pipelines[0].sample_rate == 44100.0


class TestSharedRecorderMaxDuration:
    def test_shared_recorder_extends_duration_before_start(self, monkeypatch):
        """When a second config starts with longer duration before start, buffer extends."""
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "Short",
                "record_duration": 3.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Long",
                "record_duration": 5.0,
                "analysis_duration": 2.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
        ]

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self._chunk_index = 0
                FakeRecorder.instances.append(self)

            def extend_duration(self, record_duration):
                if record_duration <= self._record_duration:
                    return
                self._record_duration = record_duration
                self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)

            def isRunning(self):
                return self._running

            def start(self):
                return None

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        mgr.start({"config_id": 2, "sn": "SN2"})

        assert mgr._recorders[1] is mgr._recorders[2]
        shared_rec = mgr._recorders[1]
        assert shared_rec._record_duration == 5.0
        assert shared_rec._buffer.shape[0] == 44100 * 5

    def test_shared_recorder_extends_duration_while_running(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "Short",
                "record_duration": 3.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Long",
                "record_duration": 5.0,
                "analysis_duration": 2.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
        ]

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self._chunk_index = 0
                FakeRecorder.instances.append(self)

            def extend_duration(self, record_duration):
                if record_duration <= self._record_duration:
                    return
                self._record_duration = record_duration
                self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]

        mgr.start({"config_id": 2, "sn": "SN2"})

        assert mgr._recorders[1] is mgr._recorders[2]
        assert shared_rec._record_duration == 5.0
        assert shared_rec._buffer.shape == (44100 * 5, 2)
        calls = [call.args[0] for call in bus.log.emit.call_args_list]
        assert not any("WARNING" in msg and "cannot extend buffer" in msg for msg in calls)

    def test_shared_recorder_attaches_late_session_via_recorder_owned_snapshot(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 3.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Late",
                "record_duration": 5.0,
                "analysis_duration": 2.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 44100,
                "output_device_name": None,
            },
        ]

        class FakeAttachSnapshot:
            def __init__(self, chunk_index, sample_index):
                self.chunk_index = chunk_index
                self.sample_index = sample_index

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self.attach_calls = []
                self._next_chunk_index = 0
                self._next_sample_index = 0

            def attach_session(self, *, record_duration):
                self.attach_calls.append(record_duration)
                self._record_duration = float(record_duration) + (self._next_sample_index / self._sample_rate)
                self._buffer = np.zeros((int(self._sample_rate * self._record_duration), self._n_channels), dtype=np.float32)
                return FakeAttachSnapshot(self._next_chunk_index, self._next_sample_index)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def get_buffer_from(self, snapshot):
                raise AssertionError("not used in this test")

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]
        shared_rec._next_sample_index = int(shared_rec._sample_rate * 2.0)
        shared_rec._next_chunk_index = 40

        mgr.start({"config_id": 2, "sn": "SN2"})

        assert mgr._recorders[1] is mgr._recorders[2]
        assert shared_rec.attach_calls == [3.0, 5.0]
        assert shared_rec._record_duration == 7.0
        assert shared_rec._buffer.shape == (44100 * 7, 2)


class TestLateAttachBufferSlice:
    def test_on_session_done_uses_recorder_owned_post_attach_buffer_slice(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        session._run_judgment.assert_called_once_with()

    def test_on_session_done_emits_factory_run_id_on_runtime_payloads(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.93,
            analysis_window=(1.0, 3.0),
        )
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        phase_payload = bus.factory_runtime_analyzing_entered.emit.call_args.args[0]
        review_payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert phase_payload.config_id == 1
        assert phase_payload.run_id == "run-1"
        assert review_payload.displayed_config_id == 1
        assert review_payload.run_id == "run-1"
        assert review_payload.trigger_source == "scan"
        assert review_payload.captured_duration_sec == 5.0
        assert review_payload.default_window.start_sec == 1.0
        assert review_payload.default_window.end_sec == 3.0
        assert review_payload.fallback_window is None
        assert review_payload.manual_reanalysis_allowed is True
        assert review_payload.no_result is False
        assert review_payload.error_message is None
        assert review_payload.judgment_payload == {
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": True,
            "ai_score": 0.93,
            "channel_results": [],
            "analysis_window": (1.0, 3.0),
        }

    def test_on_session_done_required_window_failure_emits_no_result_payload_and_preserves_completed_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._audio_file_path = None
        session._audio_data_id = None
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
        }

        def raise_required_window_failure():
            session._audio_file_path = "captured.wav"
            session._audio_data_id = "audio-123"
            raise ValueError("analysis window not fully captured")

        session._run_judgment.side_effect = raise_required_window_failure
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        payload = _expected_no_result_payload("analysis window not fully captured")
        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        assert session._audio_file_path == "captured.wav"
        assert session._audio_data_id == "audio-123"
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        phase_payload = bus.factory_runtime_analyzing_entered.emit.call_args.args[0]
        factory_payload = bus.factory_runtime_auto_analysis_no_result.emit.call_args.args[0]
        assert phase_payload.run_id == "run-1"
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis window not fully captured"
        assert factory_payload.judgment_payload is None
        assert mgr._completed_sessions[1] is session

    def test_on_session_done_unexpected_analysis_failure_emits_no_result_payload_and_preserves_completed_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._audio_file_path = None
        session._audio_data_id = None
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
        }

        def raise_unexpected_failure():
            session._audio_file_path = "captured.wav"
            session._audio_data_id = "audio-123"
            raise RuntimeError("analysis crashed")

        session._run_judgment.side_effect = raise_unexpected_failure
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        payload = _expected_no_result_payload("analysis crashed")
        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        assert session._audio_file_path == "captured.wav"
        assert session._audio_data_id == "audio-123"
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        phase_payload = bus.factory_runtime_analyzing_entered.emit.call_args.args[0]
        factory_payload = bus.factory_runtime_auto_analysis_error.emit.call_args.args[0]
        assert phase_payload.run_id == "run-1"
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis crashed"
        assert factory_payload.judgment_payload is None
        assert mgr._completed_sessions[1] is session

    def test_on_error_emits_factory_runtime_aborted_for_active_factory_run(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        rec = MagicMock()
        session = MagicMock()
        session.config_id = 1
        session._workorder = {
            "trigger_source": "plc",
            "factory_run_id": "run-1",
        }
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_error(1, "monitor disconnected")

        abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
        assert abort_payload.config_id == 1
        assert abort_payload.run_id == "run-1"
        assert abort_payload.trigger_source == "plc"
        assert abort_payload.error_message == "monitor disconnected"
        assert mgr.is_running(config_id=1) is False

    def test_late_attached_session_processes_attach_window_chunk_consistently_with_final_buffer(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
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
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 1.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Late",
                "record_duration": 0.1,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def __init__(self, live_values):
                self._live_values = live_values

            def process_chunk(self, mono, elapsed):
                self._live_values.append(float(mono[0]))
                return {"stft": None, "spl": None, "octave": None, "fft": None}

        class FakeAttachSnapshot:
            def __init__(self, chunk_index, sample_index):
                self.chunk_index = chunk_index
                self.sample_index = sample_index

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = float(sample_rate)
                self._n_channels = n_channels
                self._record_duration = float(record_duration)
                self._buffer = np.zeros((int(self._sample_rate * self._record_duration), n_channels), dtype=np.float32)
                self._chunk_samples = int(self._sample_rate * 0.05)
                self._write_pos = 0
                self._emitted_chunk_index = 0
                self._running = False
                self._attach_emit_value = None
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()

            def attach_session(self, *, record_duration):
                snapshot = FakeAttachSnapshot(self._emitted_chunk_index, self._write_pos)
                if self._attach_emit_value is not None:
                    self.emit_chunk(self._attach_emit_value)
                    self._attach_emit_value = None
                return snapshot

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def get_buffer_from(self, snapshot):
                return self._buffer[snapshot.sample_index : self._write_pos].copy()

            def emit_chunk(self, fill_value):
                chunk = np.full((self._chunk_samples, self._n_channels), fill_value, dtype=np.float32)
                end = self._write_pos + self._chunk_samples
                self._buffer[self._write_pos : end] = chunk
                chunk_index = self._emitted_chunk_index
                self._write_pos = end
                self._emitted_chunk_index += 1
                self.chunk_ready.emit(chunk, chunk_index)

        def fake_setup_pipelines(self):
            self._live_chunk_values = []
            self._pipelines = [FakePipeline(self._live_chunk_values)]

        def fake_run_judgment(self):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
            )

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._setup_pipelines", fake_setup_pipelines)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._run_judgment", fake_run_judgment)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]
        shared_rec.emit_chunk(0.0)
        shared_rec.emit_chunk(1.0)
        shared_rec.emit_chunk(2.0)
        shared_rec.emit_chunk(3.0)

        shared_rec._attach_emit_value = 4.0
        mgr.start({"config_id": 2, "sn": "SN2"})
        shared_rec.emit_chunk(5.0)

        late_session = mgr._completed_sessions[2]
        assert late_session._live_chunk_values == [4.0, 5.0]
        expected = np.concatenate(
            [
                np.full((5, 1), 4.0, dtype=np.float32),
                np.full((5, 1), 5.0, dtype=np.float32),
            ],
            axis=0,
        )
        np.testing.assert_array_equal(late_session._buffer, expected)

    def test_late_attached_session_finishes_after_current_chunk_and_judges_exact_post_attach_audio(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
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
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 1.0,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Late",
                "record_duration": 0.1,
                "analysis_duration": 1.0,
                "selected_channels": [0],
                "channels": [{
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }],
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def process_chunk(self, mono, elapsed):
                return {"stft": None, "spl": None, "octave": None, "fft": None}

        class FakeAttachSnapshot:
            def __init__(self, chunk_index, sample_index):
                self.chunk_index = chunk_index
                self.sample_index = sample_index

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = float(sample_rate)
                self._n_channels = n_channels
                self._record_duration = float(record_duration)
                self._buffer = np.zeros((int(self._sample_rate * self._record_duration), n_channels), dtype=np.float32)
                self._chunk_samples = int(self._sample_rate * 0.05)
                self._write_pos = 0
                self._emitted_chunk_index = 0
                self._running = False
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()

            def attach_session(self, *, record_duration):
                return FakeAttachSnapshot(self._emitted_chunk_index, self._write_pos)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def get_buffer_from(self, snapshot):
                return self._buffer[snapshot.sample_index : self._write_pos].copy()

            def emit_chunk(self, fill_value):
                chunk = np.full((self._chunk_samples, self._n_channels), fill_value, dtype=np.float32)
                end = self._write_pos + self._chunk_samples
                self._buffer[self._write_pos : end] = chunk
                chunk_index = self._emitted_chunk_index
                self._write_pos = end
                self._emitted_chunk_index += 1
                self.chunk_ready.emit(chunk, chunk_index)

        def fake_setup_pipelines(self):
            self._pipelines = [FakePipeline()]

        def fake_run_judgment(self):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
            )

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._setup_pipelines", fake_setup_pipelines)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._run_judgment", fake_run_judgment)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]
        shared_rec.emit_chunk(0.0)
        shared_rec.emit_chunk(1.0)
        shared_rec.emit_chunk(2.0)
        shared_rec.emit_chunk(3.0)

        mgr.start({"config_id": 2, "sn": "SN2"})
        shared_rec.emit_chunk(4.0)
        shared_rec.emit_chunk(5.0)

        late_session = mgr._completed_sessions[2]
        expected = np.concatenate(
            [
                np.full((5, 1), 4.0, dtype=np.float32),
                np.full((5, 1), 5.0, dtype=np.float32),
            ],
            axis=0,
        )
        np.testing.assert_array_equal(late_session._buffer, expected)


class TestUserStopPartialSave:
    def test_user_stop_copies_buffer_and_saves(self):
        """User-initiated stop should copy recorder buffer and save partial WAV."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        partial_buf = np.random.randn(44100, 1).astype(np.float32)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = partial_buf
        rec._sample_rate = 44100.0
        session = MagicMock()
        session._buffer = None
        session.config_id = 1
        session._attach_snapshot = attach_snapshot
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._stop_one(1, silent=False)
        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, partial_buf)
        assert session._sample_rate == 44100.0
        saved_buf, saved_sr = session._save_wav.call_args.args
        np.testing.assert_array_equal(saved_buf, partial_buf)
        assert saved_sr == 44100.0

    def test_user_stop_saves_only_post_attach_buffer_slice(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(20, 30, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session._buffer = None
        session.config_id = 1
        session._attach_snapshot = attach_snapshot
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._stop_one(1, silent=False)

        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        saved_buf, saved_sr = session._save_wav.call_args.args
        np.testing.assert_array_equal(saved_buf, post_attach)
        assert saved_sr == 100.0


class TestSignalDisconnection:
    def test_stop_disconnects_session_and_monitor_callbacks(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [0],
            "channels": [{
                "channel_index": 0,
                "stft_nfft": 1024,
                "stft_overlap": 0.5,
                "spl_weighting": "Z",
                "spl_time_constant": 0.125,
                "spl_upper_limit": 85.0,
                "spl_lower_limit": 40.0,
                "fft_band_limits": [],
                "oct_weighting": "Z",
                "oct_band_limits": [],
            }],
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": "OutputSpeaker",
            "output_device_hostapi": "OutputAPI",
            "output_device_samplerate": 48000,
        }

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.error_occurred = _DummySignal()
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                return None

            def start(self):
                return None

            def stop(self):
                return None

            def wait(self, timeout):
                return True

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})

        recorder = FakeRecorder.instances[0]
        monitor = FakeMonitor.instances[0]
        session = mgr._sessions[1]
        done_lambda = getattr(session, "_done_lambda", None)
        error_lambda = getattr(session, "_error_lambda", None)
        monitor_lambda = getattr(session, "_monitor_lambda", None)
        monitor_error_lambda = getattr(session, "_monitor_error_lambda", None)

        assert session._on_chunk in recorder.chunk_ready._callbacks
        assert done_lambda in recorder.recording_done._callbacks
        assert error_lambda in recorder.error_occurred._callbacks
        assert monitor_lambda in recorder.chunk_ready._callbacks
        assert monitor_error_lambda in monitor.error_occurred._callbacks

        mgr.stop(config_id=1)

        assert session._on_chunk not in recorder.chunk_ready._callbacks
        assert done_lambda not in recorder.recording_done._callbacks
        assert error_lambda not in recorder.error_occurred._callbacks
        assert monitor_lambda not in recorder.chunk_ready._callbacks
        assert monitor_error_lambda not in monitor.error_occurred._callbacks
