from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager

from ._support import (
    _DummySignal,
    _expected_no_result_payload,
    _mock_bus,
    _mock_services,
)


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

    @pytest.mark.parametrize(
        ("judgment_outcome", "review_signal_name"),
        [
            ("success", "factory_runtime_auto_analysis_succeeded"),
            ("no_result", "factory_runtime_auto_analysis_no_result"),
            ("error", "factory_runtime_auto_analysis_error"),
        ],
    )
    def test_on_session_done_emits_cleanup_settled_after_auto_analysis_review_signal(
        self,
        judgment_outcome,
        review_signal_name,
    ):
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
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        session._workorder = {
            "trigger_source": "manual",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        if judgment_outcome == "success":
            session._run_judgment.return_value = JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.93,
                analysis_window=(1.0, 3.0),
            )
        elif judgment_outcome == "no_result":
            session._run_judgment.side_effect = ValueError("analysis window not fully captured")
        else:
            session._run_judgment.side_effect = RuntimeError("analysis crashed")
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        emission_order: list[tuple[str, bool, bool, object]] = []

        def _capture_emission(label, payload):
            emission_order.append((label, bool(mgr._sessions), mgr.is_running(), payload))

        getattr(bus, review_signal_name).emit.side_effect = (
            lambda payload: _capture_emission("review", payload)
        )
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.side_effect = (
            lambda payload: _capture_emission("cleanup", payload)
        )

        mgr._on_session_done(1)

        assert [entry[0] for entry in emission_order] == ["review", "cleanup"]
        assert emission_order[0][1:3] == (True, True)
        assert emission_order[1][1:3] == (False, False)
        cleanup_payload = emission_order[1][3]
        assert cleanup_payload.config_id == 1
        assert cleanup_payload.run_id == "run-1"
        assert mgr._completed_sessions[1] is session

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

    def test_stop_during_inflight_analysis_suppresses_completion_signals_and_discards_completed_session(self):
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
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }

        def stop_then_return_result():
            mgr.stop(config_id=1)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.93,
            )

        session._run_judgment.side_effect = stop_then_return_result
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        bus.test_finished.emit.assert_not_called()
        bus.result_changed.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

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
                return {"stft_frames": [], "stft": None, "spl": None, "octave": None, "fft": None}

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
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def process_chunk(self, mono, elapsed):
                return {"stft_frames": [], "stft": None, "spl": None, "octave": None, "fft": None}

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

    def test_late_attached_session_clamps_tail_elapsed_to_partial_final_write_boundary(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "22050",
                "channel_count": "1",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "22050",
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
                "input_device_samplerate": 22050,
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
                "input_device_samplerate": 22050,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def __init__(self, elapsed_values):
                self._elapsed_values = elapsed_values

            def process_chunk(self, mono, elapsed):
                self._elapsed_values.append(elapsed)
                return {"stft_frames": [], "stft": None, "spl": None, "octave": None, "fft": None}

        class FakeAttachSnapshot:
            def __init__(self, chunk_index, sample_index):
                self.chunk_index = chunk_index
                self.sample_index = sample_index

        class FakeRecorder:
            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = float(sample_rate)
                self._n_channels = n_channels
                self._record_duration = float(record_duration)
                self._buffer = np.zeros(
                    (int(self._sample_rate * self._record_duration), n_channels),
                    dtype=np.float32,
                )
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

            def emit_chunk(self, fill_value, *, new_samples=None):
                n_write = self._chunk_samples if new_samples is None else int(new_samples)
                end = self._write_pos + n_write
                self._buffer[self._write_pos : end] = fill_value
                self._write_pos = end
                start = max(0, self._write_pos - self._chunk_samples)
                chunk = self._buffer[start : self._write_pos].copy()
                chunk_index = self._emitted_chunk_index
                self._emitted_chunk_index += 1
                self.chunk_ready.emit(chunk, chunk_index)

        def fake_setup_pipelines(self):
            self._elapsed_values = []
            self._pipelines = [FakePipeline(self._elapsed_values)]

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

        assert 2 not in mgr._completed_sessions

        shared_rec.emit_chunk(6.0, new_samples=1)

        late_session = mgr._completed_sessions[2]
        expected_elapsed_values = [1102 / 22050.0, 2204 / 22050.0, 2205 / 22050.0]
        progress_elapsed_values = [
            call.args[1]
            for call in bus.recording_progress.emit.call_args_list
            if call.args[0] == 2
        ]
        expected_buffer = np.concatenate(
            [
                np.full((1102, 1), 4.0, dtype=np.float32),
                np.full((1102, 1), 5.0, dtype=np.float32),
                np.full((1, 1), 6.0, dtype=np.float32),
            ],
            axis=0,
        )

        assert late_session._elapsed_values == pytest.approx(expected_elapsed_values)
        assert progress_elapsed_values == pytest.approx(expected_elapsed_values)
        np.testing.assert_array_equal(late_session._buffer, expected_buffer)
