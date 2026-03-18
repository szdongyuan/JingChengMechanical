"""Tests for TestSession lifecycle."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_session import TestSession
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

    def test_run_judgment_runs_ai_analysis_when_binding_enabled(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {"pass": True, "score": 0.91}
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
        kwargs = ai.run_ai_analysis.call_args.kwargs
        assert kwargs["config_id"] == session.config_id
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
        assert result.ai_pass is True
        assert result.ai_score == 0.91
        assert result.overall == "合格"

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
