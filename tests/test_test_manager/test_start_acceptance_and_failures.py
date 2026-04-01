from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager

from ._support import (
    _DummySignal,
    _FakeSession,
    _mock_bus,
    _mock_services,
)


class TestTestManagerStartStop:
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
            mgr.stop(config_id=1)

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

    def test_start_can_be_canceled_by_request_id_after_backend_session_exists_but_before_recorder_start(
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
                    self._running = True

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

            mgr = TestManager(bus, services)
            cancel_pending_start = getattr(mgr, "cancel_pending_start", None)
            assert callable(cancel_pending_start)

            request_id = "req-1"

            def on_start_accepted(run_id):
                assert run_id
                assert mgr.is_running(config_id=1) is True
                assert cancel_pending_start(request_id=request_id, config_id=1) is True
                events.append("accepted")

            accepted = mgr.start(
                {
                    "config_id": 1,
                    "sn": "SN1",
                    "trigger_source": "manual",
                    "factory_start_request_id": request_id,
                },
                on_start_accepted=on_start_accepted,
            )

            assert accepted is False
            assert events == ["accepted"]
            assert mgr.is_running(config_id=1) is False
            assert 1 not in mgr._recorders
            assert FakeRecorder.instances[0].stop_calls == 1
            bus.test_started.emit.assert_not_called()

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
            events: list[str] = []

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
            bus.test_started.emit.side_effect = lambda config_id: events.append(
                f"test_started:{config_id}"
            )
            bus.status.emit.side_effect = lambda message: events.append(f"status:{message}")
            bus.log.emit.side_effect = lambda message: events.append(f"log:{message}")

            accepted = mgr.start({"config_id": 1, "sn": "SN1", "trigger_source": "manual"})

            assert accepted is True
            assert mgr.is_running(config_id=1) is False
            assert mgr._completed_sessions[1] is not prior_session
            assert mgr._completed_sessions[1]._workorder["sn"] == "SN1"
            assert mgr._completed_sessions[1]._workorder["trigger_source"] == "manual"
            assert "status:Analyzing..." in events
            assert "status:Idle" in events
            assert "test_started:1" not in events
            assert "status:Recording..." not in events
            assert not any(
                event.startswith("log:[TestManager] started config=1")
                for event in events
            )
            bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()

    def test_start_does_not_emit_started_or_recording_status_after_reentrant_recorder_error(
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
                "input_device_samplerate": 100,
                "output_device_name": None,
            }

            events: list[str] = []

            class ErrorRecorder:
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
                    ErrorRecorder.instances.append(self)

                def isRunning(self):
                    return self._running

                def start(self):
                    self._running = True
                    self.error_occurred.emit("device lost")

                def stop(self):
                    self._running = False

                def wait(self, timeout):
                    return True

                def attach_session(self, *, record_duration):
                    return MagicMock(chunk_index=0, sample_index=0)

                def get_buffer_from(self, snapshot):
                    return self._buffer

            monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", ErrorRecorder)
            monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

            prior_session = MagicMock(name="prior_session")
            mgr = TestManager(bus, services)
            mgr._completed_sessions[1] = prior_session
            bus.test_started.emit.side_effect = lambda config_id: events.append(
                f"test_started:{config_id}"
            )
            bus.status.emit.side_effect = lambda message: events.append(f"status:{message}")
            bus.log.emit.side_effect = lambda message: events.append(f"log:{message}")

            accepted = mgr.start({"config_id": 1, "sn": "SN1", "trigger_source": "manual"})

            assert accepted is True
            assert mgr.is_running(config_id=1) is False
            assert mgr._completed_sessions[1] is prior_session
            assert "status:Error: device lost" in events
            assert "test_started:1" not in events
            assert "status:Recording..." not in events
            assert not any(
                event.startswith("log:[TestManager] started config=1")
                for event in events
            )
            bus.factory_runtime_aborted.emit.assert_called_once()
