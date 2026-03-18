"""Tests for TestManager (replaces fake TestService)."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.test_manager import TestManager


class _DummySignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)


class _FakeSession:
    def __init__(self, config, bus, ai_service, mic_cal, workorder, audio_dir):
        self.config_id = config["config_id"]
        self._buffer = None
        self._sample_rate = None

    def _setup_pipelines(self):
        return None

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

            def get_buffer(self):
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
        mgr.start({"sn": "123"})
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
        mgr.start({"config_id": 1, "sn": "SN1"})
        assert mgr.is_running(config_id=1) is True
        bus.test_started.emit.assert_called_with(1)

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
        mgr.start({"config": "MyConfig", "sn": "SN1", "model": "M1"})
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

            def get_buffer(self):
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
                "default_samplerate": 48000.0,
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


class TestSharedRecorderMaxDuration:
    def test_shared_recorder_extends_duration(self):
        """When a second config starts with longer duration, recorder buffer extends."""
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
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
        }
        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        services.config_service.load_config.return_value = {
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
        }
        mgr.start({"config_id": 2, "sn": "SN2"})
        assert mgr._recorders[1] is mgr._recorders[2]
        shared_rec = mgr._recorders[1]
        assert shared_rec._record_duration == 5.0


class TestUserStopPartialSave:
    def test_user_stop_copies_buffer_and_saves(self):
        """User-initiated stop should copy recorder buffer and save partial WAV."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        partial_buf = np.random.randn(44100, 1).astype(np.float32)
        rec = MagicMock()
        rec.get_buffer.return_value = partial_buf
        rec._sample_rate = 44100.0
        session = MagicMock()
        session._buffer = None
        session.config_id = 1
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._stop_one(1, silent=False)
        rec.get_buffer.assert_called_once()
        assert session._buffer is partial_buf
        assert session._sample_rate == 44100.0
        session._save_wav.assert_called_once_with(partial_buf, 44100.0)
