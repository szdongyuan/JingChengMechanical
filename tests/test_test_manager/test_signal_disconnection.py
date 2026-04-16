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
                "fft_nfft": 4096,
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
