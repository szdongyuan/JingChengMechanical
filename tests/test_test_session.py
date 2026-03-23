"""Tests for TestSession lifecycle."""
from __future__ import annotations

import pytest
import os
import re
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
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

        chunk = np.random.randn(2048, 1).astype(np.float32)

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

        chunk = np.random.randn(2048, 1).astype(np.float32)

        session._on_chunk(chunk, 3)
        bus.recording_progress.emit.assert_not_called()
        session._on_chunk(chunk, 4)
        session._on_chunk(chunk, 5)
        bus.recording_progress.emit.assert_any_call(1, 0.05)
        bus.recording_progress.emit.assert_any_call(1, 0.1)
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
    """_run_ai() score threshold decision logic."""

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

    def test_score_above_threshold(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": 0.9, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.overall == "合格"

    def test_score_below_threshold_overrides_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": 0.3, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.overall == "不合格"

    def test_score_at_threshold_passes(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "FAIL",
            "score": 0.8, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.overall == "合格"

    def test_score_present_no_threshold_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": 0.9, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": None,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True

    def test_no_score_threshold_set_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "FAIL",
            "score": None, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False

    def test_no_score_no_threshold_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": None, "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": None,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True

    def test_nan_score_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "PASS",
            "score": float("nan"), "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score is None

    def test_inf_score_falls_back_to_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = {
            "module_status": "success", "ai_label": "FAIL",
            "score": float("inf"), "error_summary": None,
        }
        session = self._make_session(tmp_path, ai, {
            "enabled": True, "score_threshold": 0.8,
        })
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score is None
