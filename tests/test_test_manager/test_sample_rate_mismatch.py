from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.test_manager import TestManager

from ._support import (
    _DummySignal,
    _mock_bus,
    _mock_services,
)


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
        mgr.stop(config_id=1)

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
