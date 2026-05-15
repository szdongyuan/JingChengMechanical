from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.selected_channels import project_selected_channels
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
        self._audio_file_path = None
        self._audio_data_id = None
        self._source_channels = ()
        self._real_to_logical_channel = {}
        self.last_ai_review = None
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

    def capture_recorder_buffer(self, hardware_buffer, sample_rate):
        view = project_selected_channels(
            hardware_buffer,
            self._config.get("selected_channels", []),
        )
        self._buffer = view.buffer
        self._source_channels = view.source_channels
        self._real_to_logical_channel = view.real_to_logical
        self._sample_rate = sample_rate
        return self._buffer

    def _save_wav(self, *args, **kwargs):
        self._audio_file_path = "partial.wav"
        self._audio_data_id = "audio-123"
        return self._audio_file_path

def _factory_runtime_config(
    config_id: int = 1,
    *,
    analysis_duration: float = 1.0,
    analysis_window_offset_ms: int = 0,
    allow_incomplete_recording_analysis: bool = False,
) -> dict:
    return {
        "config_id": config_id,
        "config_name": "Test",
        "record_duration": 2.0,
        "analysis_duration": analysis_duration,
        "analysis_window_offset_ms": analysis_window_offset_ms,
        "allow_incomplete_recording_analysis": allow_incomplete_recording_analysis,
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
            "loudness_unit": "sone",
            "loudness_lower_limit": 0.0,
            "loudness_upper_limit": 10.0,
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

def _make_running_factory_manager(
    monkeypatch,
    *,
    save_wav_error: Exception | None = None,
    analysis_duration: float = 1.0,
    analysis_window_offset_ms: int = 0,
    allow_incomplete_recording_analysis: bool = False,
    trigger_source: str = "manual",
    partial_buf: np.ndarray | None = None,
    sample_rate: float = 44100.0,
):
    bus = _mock_bus()
    services = _mock_services(bus)
    services.config_service.load_config.return_value = _factory_runtime_config(
        analysis_duration=analysis_duration,
        analysis_window_offset_ms=analysis_window_offset_ms,
        allow_incomplete_recording_analysis=allow_incomplete_recording_analysis,
    )

    if partial_buf is None:
        partial_buf = np.arange(20, dtype=np.float32).reshape(20, 1)

    class FakeRecorder:
        instances = []

        def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
            self.device_name = device_name
            self.hostapi = hostapi
            self._sample_rate = sample_rate
            self._n_channels = n_channels
            self._record_duration = record_duration
            self._buffer = partial_buf
            self.chunk_ready = _DummySignal()
            self.recording_done = _DummySignal()
            self.error_occurred = _DummySignal()
            self._running = False
            self.stop_calls = 0
            self.wait_calls = []
            self.attach_calls = []
            self.get_buffer_from_calls = []
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
            self.attach_calls.append(record_duration)
            return MagicMock(chunk_index=0, sample_index=0)

        def get_buffer_from(self, snapshot):
            self.get_buffer_from_calls.append(snapshot)
            return self._buffer

    class FakeMonitor:
        instances = []

        def __init__(
            self,
            *,
            device_name,
            hostapi,
            sample_rate,
            monitor_channel,
            gain_db=0.0,
        ):
            self.device_name = device_name
            self.hostapi = hostapi
            self.sample_rate = sample_rate
            self.monitor_channel = monitor_channel
            self.gain_db = gain_db
            self.error_occurred = _DummySignal()
            self.stop_calls = 0
            self.wait_calls = []
            self.start_calls = 0
            FakeMonitor.instances.append(self)

        def feed(self, chunk):
            return None

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    class FakeSession(_FakeSession):
        instances = []

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saved_wavs = []
            FakeSession.instances.append(self)

        def _save_wav(self, buf, sample_rate):
            self.saved_wavs.append((buf.copy(), sample_rate))
            if save_wav_error is not None:
                raise save_wav_error
            self._audio_file_path = "partial.wav"
            self._audio_data_id = "audio-123"
            return self._audio_file_path

    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
    monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

    mgr = TestManager(bus, services)

    started = mgr.start({"config_id": 1, "sn": "SN1", "trigger_source": trigger_source})

    assert started is True
    assert mgr.is_running(config_id=1) is True
    assert len(FakeSession.instances) == 1
    assert len(FakeRecorder.instances) == 1
    assert len(FakeMonitor.instances) == 1

    FakeRecorder.instances[0]._sample_rate = sample_rate

    return (
        bus,
        services,
        mgr,
        FakeSession.instances[0],
        FakeRecorder.instances[0],
        FakeMonitor.instances[0],
        partial_buf,
    )

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
        "factory_runtime_auto_analysis_cleanup_settled",
        "factory_runtime_stopped",
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

def _expected_summary_payload(*, overall_status: str, error_message: str | None = None) -> dict:
    overall_label = {
        "pass": "PASS",
        "fail": "FAIL",
        "no_result": "无结果",
    }[overall_status]
    ai_status = "无结果" if overall_status == "no_result" else "OK"
    return {
        "overall_status": overall_status,
        "overall_label": overall_label,
        "error_message": error_message,
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
                "item_key": "loudness",
                "label": "Loudness",
                "deviation_value": -2.0,
                "deviation_display": "-2 sone",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "fft",
                "label": "FFT",
                "deviation_value": -3.0,
                "deviation_display": "-3 dB",
                "channel_index": 0,
                "channel_label": "CH1",
                "status": "OK",
                "constrained": True,
            },
            {
                "item_key": "octave",
                "label": "倍频程",
                "deviation_value": -4.0,
                "deviation_display": "-4 dB",
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
                "status": ai_status,
                "constrained": ai_status != "OK",
            },
        ],
    }

def _expected_chart_payload() -> dict:
    return {
        0: {
            "loudness": {
                "times_sec": [0.0, 1.0],
                "values": [2.0, 3.0],
                "unit": "sone",
                "lower_limit": 0.0,
                "upper_limit": 10.0,
            },
            "spl": {
                "times_sec": [0.05, 0.1],
                "values_db": [71.0, 72.0],
                "lower_limit_db": 40.0,
                "upper_limit_db": 80.0,
            },
            "fft": {
                "frequencies_hz": [1000.0],
                "levels_db": [67.0],
                "golden_mode": "off",
                "limit_bands": [
                    {
                        "freq_hz": 1000.0,
                        "lower_db": 40.0,
                        "upper_db": 60.0,
                        "actual_db": 67.0,
                        "passed": False,
                    }
                ],
            },
            "octave": {
                "center_freqs_hz": [500.0],
                "levels_db": [57.0],
                "golden_mode": "off",
                "limit_bands": [
                    {
                        "freq_hz": 500.0,
                        "lower_db": 35.0,
                        "upper_db": 55.0,
                        "actual_db": 57.0,
                        "passed": False,
                    }
                ],
            },
        }
    }
