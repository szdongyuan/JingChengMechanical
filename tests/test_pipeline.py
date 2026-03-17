"""Tests for AnalysisPipeline streaming analysis."""
from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.pipeline import AnalysisPipeline
from app.service.analysis.frames import StftFrame, SplFrame, OctaveFrame, FftFrame


def _make_config(**overrides) -> dict:
    """Create a channel config dict with defaults."""
    cfg = {
        "channel_index": 0,
        "stft_nfft": 1024,
        "stft_overlap": 0.5,
        "spl_weighting": "A",
        "spl_time_constant": 0.125,
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "fft_band_limits": [],
        "oct_weighting": "A",
        "oct_band_limits": [],
    }
    cfg.update(overrides)
    return cfg


class TestAnalysisPipeline:
    def test_init(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        assert p.channel_index == 0
        assert p.sample_rate == 44100.0

    def test_process_chunk_returns_all_frames(self):
        """Processing a chunk large enough for STFT should return all frame types."""
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        # Need at least nfft samples for one STFT frame
        chunk = np.random.randn(2048).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=0.5)
        assert "stft" in result
        assert "spl" in result
        assert "octave" in result
        assert "fft" in result

    def test_spl_frame_type(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        chunk = np.random.randn(2048).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=1.0)
        assert isinstance(result["spl"], SplFrame)
        assert result["spl"].weighting == "A"

    def test_stft_frame_when_enough_samples(self):
        cfg = _make_config(stft_nfft=512, stft_overlap=0.5)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg,
                             sample_rate=44100.0)
        chunk = np.random.randn(1024).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=0.0)
        stft = result["stft"]
        assert isinstance(stft, StftFrame)
        assert stft.frequencies.shape[0] == 512 // 2 + 1

    def test_stft_none_when_too_few_samples(self):
        """If chunk is smaller than nfft and no buffer, stft should be None."""
        cfg = _make_config(stft_nfft=4096)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg,
                             sample_rate=44100.0)
        chunk = np.random.randn(100).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=0.0)
        assert result["stft"] is None

    def test_mic_calibration_applied(self):
        """Non-zero sensitivity should change the SPL value."""
        cfg = _make_config()
        p_raw = AnalysisPipeline(channel_index=0, channel_config=cfg,
                                 sample_rate=44100.0, mic_sensitivity=0.0)
        p_cal = AnalysisPipeline(channel_index=0, channel_config=cfg,
                                 sample_rate=44100.0, mic_sensitivity=2.0)
        chunk = np.random.randn(2048).astype(np.float32)
        r_raw = p_raw.process_chunk(chunk.copy(), elapsed_time=0.0)
        r_cal = p_cal.process_chunk(chunk.copy(), elapsed_time=0.0)
        # Calibrated signal is 2x amplitude → ~6 dB higher SPL
        assert r_cal["spl"].spl_db > r_raw["spl"].spl_db

    def test_reset_clears_buffer(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        chunk = np.random.randn(2048).astype(np.float32)
        p.process_chunk(chunk, elapsed_time=0.0)
        p.reset()
        # After reset, internal buffer should be empty
        assert p._stft_buffer.size == 0 or len(p._stft_buffer) == 0
