"""Tests for TestSession lifecycle."""
from __future__ import annotations

import pytest
import os
import re
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import FftFrame, JudgmentResult, SplFrame, StftFrame
from app.service.analysis.test_session import TestSession
from app.service.config_service import _CHANNEL_DEFAULTS
from model.audio_data_table import AudioDataTable


def _make_mock_bus():
    bus = MagicMock()
    bus.log = MagicMock()
    bus.log.emit = MagicMock()
    bus.status = MagicMock()
    bus.status.emit = MagicMock()
    bus.result_changed = MagicMock()
    bus.result_changed.emit = MagicMock()
    bus.stft_updated = MagicMock()
    bus.stft_updated.emit = MagicMock()
    bus.spl_updated = MagicMock()
    bus.spl_updated.emit = MagicMock()
    bus.octave_updated = MagicMock()
    bus.octave_updated.emit = MagicMock()
    bus.fft_updated = MagicMock()
    bus.fft_updated.emit = MagicMock()
    bus.recording_progress = MagicMock()
    bus.recording_progress.emit = MagicMock()
    bus.test_started = MagicMock()
    bus.test_started.emit = MagicMock()
    bus.test_finished = MagicMock()
    bus.test_finished.emit = MagicMock()
    return bus


def _make_config() -> dict:
    return {
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
        "input_device_name": "Test Mic",
        "input_device_hostapi": "MME",
        "input_device_samplerate": 44100,
        "output_device_name": None,
    }


class TestTestSessionInit:
    def test_init(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "123", "model": "X"},
            audio_dir="/tmp/audio",
        )
        assert session.config_id == 1
        assert session._record_duration == 2.0


class TestTestSessionProcessing:
    def test_setup_pipelines_uses_channel_defaults_when_selected_channel_config_missing(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["selected_channels"] = [1]
        config["channels"] = []
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )

        session._setup_pipelines()

        assert len(session._pipelines) == 1
        pipeline = session._pipelines[0]
        assert pipeline.channel_index == 1
        assert pipeline._cfg["stft_nfft"] == _CHANNEL_DEFAULTS["stft_nfft"]
        assert pipeline._cfg["spl_weighting"] == _CHANNEL_DEFAULTS["spl_weighting"]

    def test_on_chunk_emits_analysis_signals(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        chunk = np.random.randn(2048, 1).astype(np.float32)
        session._on_chunk(chunk, 0)
        bus.spl_updated.emit.assert_called()

    def test_on_chunk_emits_analysis_signals_with_factory_run_id(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"factory_run_id": "factory-run-1"},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()

        chunk = np.random.randn(2048, 1).astype(np.float32)

        session._on_chunk(chunk, 0)

        assert bus.spl_updated.emit.call_args.args[0] == 1
        assert bus.spl_updated.emit.call_args.args[1] == "factory-run-1"
        assert bus.spl_updated.emit.call_args.args[2] == 0

    def test_on_chunk_emits_one_stft_signal_per_stft_frame(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"factory_run_id": "factory-run-1"},
            audio_dir="/tmp/audio",
        )
        frame_a = StftFrame(
            time=0.0,
            frequencies=np.array([100.0]),
            magnitudes=np.array([1.0]),
        )
        frame_b = StftFrame(
            time=0.5,
            frequencies=np.array([100.0]),
            magnitudes=np.array([2.0]),
        )
        fake_pipeline = MagicMock()
        fake_pipeline.channel_index = 0
        fake_pipeline.process_chunk.return_value = {
            "stft_frames": [frame_a, frame_b],
            "stft": frame_b,
            "spl": SplFrame(time=0.05, spl_db=60.0, weighting="Z"),
            "octave": None,
            "fft": None,
        }
        session._pipelines = [fake_pipeline]

        session._on_chunk(np.ones((2048, 1), dtype=np.float32), 0)

        assert bus.stft_updated.emit.call_count == 2
        assert bus.stft_updated.emit.call_args_list[0].args == (1, "factory-run-1", 0, frame_a)
        assert bus.stft_updated.emit.call_args_list[1].args == (1, "factory-run-1", 0, frame_b)

    def test_on_chunk_re_emits_fft_frame_with_factory_run_id(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"factory_run_id": "factory-run-1"},
            audio_dir="/tmp/audio",
        )
        fft_frame = FftFrame(
            time=0.10,
            frequencies=np.array([0.0, 100.0], dtype=np.float64),
            magnitudes=np.array([1.0, 2.0], dtype=np.float64),
        )
        fake_pipeline = MagicMock()
        fake_pipeline.channel_index = 0
        fake_pipeline.process_chunk.return_value = {
            "stft_frames": [],
            "stft": None,
            "spl": SplFrame(time=0.05, spl_db=60.0, weighting="Z"),
            "octave": None,
            "fft": fft_frame,
        }
        session._pipelines = [fake_pipeline]

        session._on_chunk(np.ones((2048, 1), dtype=np.float32), 0)

        assert bus.fft_updated.emit.call_args.args == (1, "factory-run-1", 0, fft_frame)

    def test_on_chunk_notifies_when_session_duration_expires(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["record_duration"] = 0.1
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        session._on_session_done_cb = MagicMock()

        chunk = np.random.randn(int(session._sample_rate * 0.05), 1).astype(np.float32)

        session._on_chunk(chunk, 2)
        session._on_chunk(chunk, 3)

        session._on_session_done_cb.assert_called_once_with(1)

    def test_on_chunk_ignores_pre_attach_chunks_and_uses_attach_boundary_elapsed_time(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["record_duration"] = 0.1
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        session._on_session_done_cb = MagicMock()
        session.attach_to_recorder(chunk_index=4)

        chunk = np.random.randn(int(session._sample_rate * 0.05), 1).astype(np.float32)

        session._on_chunk(chunk, 3)
        bus.recording_progress.emit.assert_not_called()
        session._on_chunk(chunk, 4)
        session._on_chunk(chunk, 5)
        bus.recording_progress.emit.assert_any_call(1, 0.05)
        bus.recording_progress.emit.assert_any_call(1, 0.1)
        session._on_session_done_cb.assert_called_once_with(1)

    def test_on_chunk_uses_sample_derived_elapsed_for_pipeline_and_progress_at_non_round_sample_rate(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["input_device_samplerate"] = 22050
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        fake_pipeline = MagicMock()
        fake_pipeline.channel_index = 0
        fake_pipeline.process_chunk.return_value = {
            "stft_frames": [],
            "stft": None,
            "spl": None,
            "octave": None,
            "fft": None,
        }
        session._pipelines = [fake_pipeline]
        chunk = np.ones((int(22050 * 0.05), 1), dtype=np.float32)
        expected_elapsed = chunk.shape[0] / 22050.0

        session._on_chunk(chunk, 0)

        assert fake_pipeline.process_chunk.call_args.args[1] == pytest.approx(expected_elapsed)
        assert bus.recording_progress.emit.call_args.args == (1, pytest.approx(expected_elapsed))

    def test_on_chunk_completion_clamps_elapsed_to_session_target_after_attach_at_non_round_sample_rate(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["input_device_samplerate"] = 22050
        config["record_duration"] = 0.1
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        fake_pipeline = MagicMock()
        fake_pipeline.channel_index = 0
        fake_pipeline.process_chunk.return_value = {
            "stft_frames": [],
            "stft": None,
            "spl": None,
            "octave": None,
            "fft": None,
        }
        session._pipelines = [fake_pipeline]
        session._on_session_done_cb = MagicMock()
        session.attach_to_recorder(chunk_index=4)
        chunk_window_samples = int(22050 * 0.05)
        total_target_samples = int(22050 * config["record_duration"])
        chunk = np.ones((chunk_window_samples, 1), dtype=np.float32)
        expected_elapsed_values = [
            samples / 22050.0
            for samples in (chunk_window_samples, chunk_window_samples * 2, total_target_samples)
        ]

        session._on_chunk(chunk, 4)
        session._on_session_done_cb.assert_not_called()
        session._on_chunk(chunk, 5)
        session._on_session_done_cb.assert_not_called()
        session._on_chunk(chunk, 6)

        observed_elapsed_values = [
            call.args[1]
            for call in fake_pipeline.process_chunk.call_args_list
        ]
        assert observed_elapsed_values == pytest.approx(expected_elapsed_values)
        progress_elapsed_values = [
            call.args[1]
            for call in bus.recording_progress.emit.call_args_list
        ]
        assert progress_elapsed_values == pytest.approx(expected_elapsed_values)
        assert observed_elapsed_values[-1] == pytest.approx(config["record_duration"])
        session._on_session_done_cb.assert_called_once_with(1)


class TestTestSessionJudgment:
    def test_run_judgment_produces_result(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._setup_pipelines()
        sr = 44100.0
        duration = 2.0
        t = np.arange(int(sr * duration)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        session._buffer = sig.reshape(-1, 1)
        session._sample_rate = sr
        result = session._run_judgment()
        assert isinstance(result, JudgmentResult)
        assert result.overall in ("合格", "不合格")

    def test_run_judgment_required_window_failure_preserves_no_result_session_state(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.side_effect = ValueError("analysis window not fully captured")

        with pytest.raises(ValueError, match="analysis window not fully captured"):
            session._run_judgment()

        assert session.buffer is not None
        assert session._audio_file_path is not None
        assert os.path.isfile(session._audio_file_path)
        assert session._audio_data_id is not None
        assert session.last_result is None

    def test_run_judgment_missing_buffer_returns_none_for_manager_no_result_path(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )

        result = session._run_judgment()

        assert result is None
        assert session.last_result is None
        assert session.last_ai_review is None
        assert session._audio_file_path is None
        assert session._audio_data_id is None

    def test_run_judgment_default_window_offset_ms_uses_config_analysis_window_offset(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["analysis_window_offset_ms"] = 1500
        ai.resolve_binding.return_value = {
            "enabled": True,
            "analysis_window_offset_ms": 250,
        }
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.91,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.5, 2.5),
        )

        result = session._run_judgment()

        ai.run_ai_analysis.assert_called_once()
        kwargs = ai.run_ai_analysis.call_args.kwargs
        assert kwargs["config_id"] == session.config_id
        assert re.fullmatch(r"run_1_\d+", kwargs["test_run_id"])
        assert kwargs["context"] == {"config_id": session.config_id, "workorder": session._workorder}
        assert os.path.normpath(kwargs["ai_runs_dir"]).endswith(os.path.normpath("ai_runs"))
        assert len(kwargs["segments"]) == 1
        segment = kwargs["segments"][0]
        assert segment["segment_id"] == session._audio_data_id
        assert segment["role"] == "device_1"
        assert segment["audio_path"] == session._audio_file_path
        assert segment["channels"] == [0]
        assert segment["duration_ms"] == 1000
        assert segment["sample_rate"] == 44100
        assert kwargs["window_info"] == {
            "window_overridden": False,
            "default_window_offset_ms": 1500,
            "default_window_duration_ms": 1000,
            "final_window_offset_ms": 1500,
            "final_window_duration_ms": 1000,
        }
        assert result.ai_pass is True
        assert result.ai_score == 0.91
        assert result.overall == "合格"

    def test_run_judgment_marks_result_failed_when_ai_label_is_fail(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "FAIL",
            "score": 0.12,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.ai_pass is False
        assert result.ai_score == 0.12
        assert result.overall == "不合格"

    def test_run_judgment_logs_warning_and_preserves_limit_result_when_ai_envelope_not_success(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "timeout",
            "ai_label": None,
            "score": None,
            "error_summary": "Process timed out",
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        bus.log.emit.assert_called_with(
            "[TestSession] WARNING: AI analysis did not succeed for config=1: "
            "module_status=timeout ai_label=None error=Process timed out"
        )

    def test_run_judgment_preserves_ai_review_details_separately_from_judgment_result_on_timeout(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {
            "enabled": True,
        }
        ai.run_ai_analysis.return_value = {
            "module_status": "timeout",
            "ai_label": None,
            "score": None,
            "error_summary": "Process timed out",
            "channel_index": 0,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "timeout"
        assert session.last_ai_review["threshold"] is None
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["channel_index"] == 0
        assert session.last_ai_review["error_message"] == "Process timed out"

    def test_run_judgment_keeps_limit_result_when_ai_analysis_fails(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.side_effect = RuntimeError("AI unavailable")
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        ai.run_ai_analysis.assert_called_once()
        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        bus.log.emit.assert_called_with("[TestSession] WARNING: AI analysis failed for config=1: AI unavailable")

    def test_run_judgment_logs_warning_when_ai_binding_resolution_fails(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.side_effect = RuntimeError("binding lookup failed")
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        bus.log.emit.assert_called_with(
            "[TestSession] WARNING: failed to resolve AI binding for config=1: binding lookup failed"
        )

    def test_rejudge_reruns_ai_and_patches_result(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "FAIL",
            "score": 0.12,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session.rejudge(0.1, 0.5)

        session._judgment.rejudge.assert_called_once_with(
            session._config,
            session._buffer,
            session._sample_rate,
            session._mic_cal,
            session._audio_file_path,
            0.1,
            0.5,
        )
        ai.run_ai_analysis.assert_called_once()
        assert result is not None
        assert result is session.last_result
        assert result.ai_pass is False
        assert result.ai_score == 0.12
        assert result.overall == "不合格"

    def test_rejudge_uses_same_ai_envelope_normalization_as_run_judgment(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.88,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session.rejudge(0.1, 0.5)

        assert result is not None
        assert result.ai_pass is True
        assert result.ai_score == 0.88
        assert result.overall == "合格"

    def test_rejudge_required_window_failure_preserves_prior_no_result_trace(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        prior_result = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
        )
        session._last_result = prior_result
        session._judgment = MagicMock()
        session._judgment.rejudge.side_effect = ValueError("analysis window not fully captured")

        with pytest.raises(ValueError, match="analysis window not fully captured"):
            session.rejudge(0.1, 0.5)

        assert session.last_result is prior_result
        assert session._audio_file_path == str(tmp_path / "existing.wav")
        assert session._audio_data_id == "audio-123"

    def test_rejudge_passes_selected_window_to_ai_analysis_with_config_default_window_offset(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["analysis_window_offset_ms"] = 1500
        ai.resolve_binding.return_value = {
            "enabled": True,
            "analysis_window_offset_ms": 250,
        }
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.88,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.1, 0.5),
        )

        session.rejudge(0.1, 0.5)

        kwargs = ai.run_ai_analysis.call_args.kwargs
        assert kwargs["segments"][0]["duration_ms"] == 400
        assert kwargs["window_info"] == {
            "window_overridden": True,
            "default_window_offset_ms": 1500,
            "default_window_duration_ms": 1000,
            "final_window_offset_ms": 100,
            "final_window_duration_ms": 400,
        }

    def test_repeated_ai_attempts_in_same_session_reuse_test_run_id(self, tmp_path, monkeypatch):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.91,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 2.0),
        )
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.1, 0.5),
        )

        base = datetime(2026, 3, 18, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        timestamps = iter([
            base,
            base + timedelta(milliseconds=1),
            base + timedelta(milliseconds=2),
            base + timedelta(milliseconds=25),
        ])

        class _FixedDateTime:
            @staticmethod
            def now(tz=None):
                current = next(timestamps)
                if tz is None:
                    return current.replace(tzinfo=None)
                return current.astimezone(tz)

        monkeypatch.setattr("app.service.analysis.test_session.datetime", _FixedDateTime)

        session._run_judgment()
        first_run_id = ai.run_ai_analysis.call_args.kwargs["test_run_id"]
        session.rejudge(0.1, 0.5)
        second_run_id = ai.run_ai_analysis.call_args.kwargs["test_run_id"]

        assert re.fullmatch(r"run_1_\d+", first_run_id)
        assert second_run_id == first_run_id


class TestTestSessionWavSave:
    def test_save_wav(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        sr = 44100.0
        buf = np.random.randn(int(sr * 1), 1).astype(np.float32)
        path = session._save_wav(buf, sr)
        assert os.path.isfile(path)
        assert path.endswith(".wav")
        records = list(AudioDataTable.select())
        assert len(records) == 1
        rec = records[0]
        assert rec.file_path == path
        assert rec.barcode == "SN001"
        assert rec.product_model == "M1"
        assert rec.sample_rate == 44100
        assert rec.audio_data_id
        assert rec.record_date
        assert rec.labels is None

    def test_save_wav_returns_absolute_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "SN001", "model": "M1"},
            audio_dir="audio",
        )
        sr = 44100.0
        buf = np.random.randn(int(sr * 1), 1).astype(np.float32)

        path = session._save_wav(buf, sr)

        assert os.path.isabs(path)
        assert os.path.isfile(path)


class TestScoreThresholdDecision:
    """_run_ai() runtime threshold decision logic."""

    def _make_session(self, tmp_path, ai_mock, binding):
        ai_mock.resolve_binding.return_value = binding
        session = TestSession(
            config=_make_config(), bus=_make_mock_bus(), ai_service=ai_mock,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "test.wav")
        session._audio_data_id = "audio-test"
        return session

    def _make_base_result(self):
        return JudgmentResult(
            overall="合格", limit_pass=True, ai_pass=None, ai_score=None,
        )

    def _make_success_envelope(self, **overrides):
        envelope = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.9,
            "decision_threshold": 0.8,
            "error_summary": None,
        }
        envelope.update(overrides)
        return envelope

    def test_score_above_envelope_threshold_overrides_fail_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="FAIL",
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score == 0.9
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "success"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["score"] == 0.9

    def test_score_below_envelope_threshold_fails_even_if_ai_label_is_pass(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(score=0.3)
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score == 0.3
        assert result.overall == "不合格"

    def test_score_uses_resolved_request_threshold_from_envelope(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="FAIL",
            score=0.6,
            decision_threshold=0.6,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score == 0.6
        assert result.overall == "合格"
        assert session.last_ai_review["threshold"] == 0.6

    def test_score_uses_default_threshold_from_envelope_when_upstream_falls_back_to_point_five(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            score=0.4,
            decision_threshold=0.5,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score == 0.4
        assert result.overall == "不合格"
        assert session.last_ai_review["threshold"] == 0.5

    def test_missing_score_ai_label_fail_falls_back_to_false(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="FAIL",
            score=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score is None
        assert result.overall == "不合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["error_message"] == "AI score unavailable"

    def test_missing_score_ai_label_pass_falls_back_to_true(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(score=None)
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["error_message"] == "AI score unavailable"

    def test_missing_score_unknown_ai_label_degrades_to_no_result(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="MAYBE",
            score=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["error_message"] == "AI label is not a recognized binary label"

    @pytest.mark.parametrize("ai_label", [None, 123])
    def test_missing_score_without_usable_ai_label_degrades_to_no_result_with_explicit_error(
        self, tmp_path, ai_label
    ):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label=ai_label,
            score=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})

        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["error_message"] == "AI score and usable AI label are unavailable"

    def test_run_ai_malformed_output_none_marks_invalid_output_and_preserves_limit_result(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = None
        session = self._make_session(tmp_path, ai, {"enabled": True})

        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "invalid_output"
        assert session.last_ai_review["threshold"] is None
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["channel_index"] is None
        assert session.last_ai_review["error_message"] == "AI analysis returned invalid output"

    @pytest.mark.parametrize(
        ("score", "ai_label", "expected_ai_pass"),
        [
            (float("nan"), "PASS", True),
            (float("inf"), "FAIL", False),
            ("0.7", "PASS", True),
        ],
    )
    def test_unusable_score_enters_ai_label_fallback_and_preserves_missing_score_review(
        self, tmp_path, score, ai_label, expected_ai_pass
    ):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label=ai_label,
            score=score,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is expected_ai_pass
        assert result.ai_score is None
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["error_message"] == "AI score unavailable"

    def test_score_without_envelope_threshold_degrades_to_no_result_instead_of_label_fallback(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="PASS",
            decision_threshold=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})

        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is None
        assert result.ai_score == 0.9
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "success"
        assert session.last_ai_review["threshold"] is None
        assert session.last_ai_review["score"] == 0.9
        assert session.last_ai_review["error_message"] == "AI decision threshold unavailable"
