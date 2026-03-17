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
