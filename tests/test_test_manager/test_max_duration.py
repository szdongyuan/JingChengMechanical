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
