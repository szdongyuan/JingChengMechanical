from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.test_manager import TestManager

from ._support import (
    _DummySignal,
    _FakeSession,
    _mock_bus,
    _mock_services,
)


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
            mgr.stop(config_id=42)

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
