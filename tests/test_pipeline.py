"""Tests for AnalysisPipeline streaming analysis."""
from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.pipeline import (
    _LIVE_LOUDNESS_THROTTLE_SEC,
    _LIVE_LOUDNESS_WINDOW_SEC,
    AnalysisPipeline,
)
from app.service.analysis.frames import FftFrame, LoudnessFrame, OctaveFrame, SplFrame, StftFrame


def _make_config(**overrides) -> dict:
    """Create a channel config dict with defaults."""
    cfg = {
        "channel_index": 0,
        "stft_nfft": 1024,
        "fft_nfft": 4096,
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
        assert "stft_frames" in result
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
        assert result["stft_frames"] == []

    def test_stft_accumulation_across_chunks(self):
        """Two sub-nfft chunks should accumulate and produce STFT on second call."""
        cfg = _make_config(stft_nfft=1024, stft_overlap=0.5)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg, sample_rate=44100.0)
        chunk1 = np.random.randn(600).astype(np.float32)
        chunk2 = np.random.randn(600).astype(np.float32)
        r1 = p.process_chunk(chunk1, elapsed_time=0.0)
        assert r1["stft"] is None
        assert r1["stft_frames"] == []
        r2 = p.process_chunk(chunk2, elapsed_time=0.05)
        assert r2["stft"] is not None
        assert len(r2["stft_frames"]) >= 1

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

    def test_live_fft_uses_fft_nfft_instead_of_stft_nfft(self):
        cfg = _make_config(stft_nfft=512, fft_nfft=4096)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg, sample_rate=48_000.0)

        first = p.process_chunk(np.ones(2048, dtype=np.float32), elapsed_time=0.05)
        second = p.process_chunk(np.ones(2048, dtype=np.float32), elapsed_time=0.10)

        assert first["fft"] is None
        assert isinstance(second["fft"], FftFrame)
        assert second["fft"].frequencies.shape[0] == 4096 // 2 + 1

    def test_live_fft_waits_for_rolling_buffer_then_emits_one_frame_per_chunk(
        self,
        monkeypatch,
    ):
        captured_fft_inputs: list[np.ndarray] = []

        def fake_compute_fft_spectrum(signal, sample_rate, nfft):
            captured_fft_inputs.append(np.asarray(signal, dtype=np.float64).copy())
            return (
                np.arange(nfft // 2 + 1, dtype=np.float64),
                np.zeros(nfft // 2 + 1, dtype=np.float64),
            )

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_fft_spectrum",
            fake_compute_fft_spectrum,
        )

        cfg = _make_config(stft_nfft=128, fft_nfft=8)
        p = AnalysisPipeline(channel_index=0, channel_config=cfg, sample_rate=100.0)

        first = p.process_chunk(np.array([1.0, 2.0, 3.0], dtype=np.float32), elapsed_time=0.03)
        second = p.process_chunk(np.array([4.0, 5.0, 6.0], dtype=np.float32), elapsed_time=0.06)
        third = p.process_chunk(np.array([7.0, 8.0], dtype=np.float32), elapsed_time=0.08)
        fourth = p.process_chunk(np.array([9.0], dtype=np.float32), elapsed_time=0.09)

        assert first["fft"] is None
        assert second["fft"] is None
        assert isinstance(third["fft"], FftFrame)
        assert isinstance(fourth["fft"], FftFrame)
        assert len(captured_fft_inputs) == 2
        np.testing.assert_array_equal(
            captured_fft_inputs[0],
            np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], dtype=np.float64),
        )
        np.testing.assert_array_equal(
            captured_fft_inputs[1],
            np.array([2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0], dtype=np.float64),
        )

    def test_process_chunk_returns_throttled_loudness_frame(self, monkeypatch):
        calls = []

        def fake_compute_loudness_trace(signal, sample_rate):
            calls.append(np.asarray(signal, dtype=np.float64).copy())
            return [0.0, 0.1], [1.25, 2.5], 2.5

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            fake_compute_loudness_trace,
        )

        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(),
            sample_rate=100.0,
        )

        first = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.25)
        second = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.40)
        third = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.50)
        fourth = p.process_chunk(np.ones(50, dtype=np.float32), elapsed_time=0.75)

        assert isinstance(first["loudness"], LoudnessFrame)
        assert first["loudness"].time == pytest.approx(0.25)
        assert first["loudness"].loudness_sone == pytest.approx(2.5)
        assert second["loudness"] is None
        assert isinstance(third["loudness"], LoudnessFrame)
        assert third["loudness"].time == pytest.approx(0.50)
        assert isinstance(fourth["loudness"], LoudnessFrame)
        assert fourth["loudness"].time == pytest.approx(0.75)
        assert len(calls) == 3

    def test_process_chunk_can_skip_live_loudness_backend_while_maintaining_buffer(
        self,
        monkeypatch,
    ):
        calls = []

        def fake_compute_loudness_trace(signal, sample_rate):
            calls.append(np.asarray(signal, dtype=np.float64).copy())
            return [0.0], [1.0], 1.0

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            fake_compute_loudness_trace,
        )

        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(),
            sample_rate=100.0,
        )

        skipped = p.process_chunk(
            np.ones(75, dtype=np.float32),
            elapsed_time=0.5,
            compute_loudness=False,
        )
        emitted = p.process_chunk(
            np.ones(25, dtype=np.float32),
            elapsed_time=1.0,
            compute_loudness=True,
        )

        assert skipped["loudness"] is None
        assert isinstance(emitted["loudness"], LoudnessFrame)
        assert len(calls) == 1
        assert p._loudness_buffer.size == 50
        np.testing.assert_array_equal(calls[0], np.ones(50, dtype=np.float64))

    def test_live_loudness_throttle_is_quarter_second_and_window_is_half_second(self):
        assert _LIVE_LOUDNESS_THROTTLE_SEC == pytest.approx(0.25)
        assert _LIVE_LOUDNESS_WINDOW_SEC == pytest.approx(0.5)

    def test_process_chunk_suppresses_live_loudness_backend_errors(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("backend down")),
        )
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(), sample_rate=100.0)
        result = p.process_chunk(np.ones(100, dtype=np.float32), elapsed_time=1.0)
        assert result["loudness"] is None

    def test_reset_clears_live_loudness_buffer_and_throttle(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda signal, sample_rate: ([0.0], [3.0], 3.0),
        )
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(), sample_rate=100.0)
        p.process_chunk(np.ones(100, dtype=np.float32), elapsed_time=1.0)
        p.reset()
        assert p._loudness_buffer.size == 0
        assert p._last_live_loudness_emit_time == 0.0

    def test_reset_clears_buffer(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        chunk = np.random.randn(2048).astype(np.float32)
        p.process_chunk(chunk, elapsed_time=0.0)
        p.reset()
        # After reset, internal buffer should be empty
        assert p._stft_buffer.size == 0 or len(p._stft_buffer) == 0

    def test_reset_clears_fft_buffer(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)

        p.process_chunk(np.random.randn(2048).astype(np.float32), elapsed_time=0.0)

        assert p._fft_buffer.size > 0

        p.reset()

        assert p._fft_buffer.size == 0

    def test_process_chunk_returns_stft_frames_starting_at_zero(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_stft",
            lambda signal, sample_rate, nfft, overlap: (
                np.array([0.0, 0.5, 1.0], dtype=np.float64),
                np.array([100.0, 200.0], dtype=np.float64),
                np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float64),
            ),
        )

        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(stft_nfft=8, stft_overlap=0.5),
            sample_rate=100.0,
        )

        result = p.process_chunk(np.ones(100), elapsed_time=1.0)

        assert [frame.time for frame in result["stft_frames"]] == [0.0, 0.5, 1.0]
        assert result["stft"] == result["stft_frames"][-1]

    def test_process_chunk_suppresses_duplicate_stft_frames_across_overlapping_recomputation(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_stft",
            lambda signal, sample_rate, nfft, overlap: (
                np.array([0.0, 0.5, 1.0], dtype=np.float64),
                np.array([100.0], dtype=np.float64),
                np.array([[1.0, 2.0, 3.0]], dtype=np.float64),
            ),
        )

        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(stft_nfft=100, stft_overlap=0.5),
            sample_rate=100.0,
        )

        first = p.process_chunk(np.ones(100), elapsed_time=1.0)
        second = p.process_chunk(np.ones(50), elapsed_time=1.5)

        assert [frame.time for frame in first["stft_frames"]] == [0.0, 0.5, 1.0]
        assert [frame.time for frame in second["stft_frames"]] == [1.5]

    def test_process_chunk_filters_future_padded_stft_frames(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_stft",
            lambda signal, sample_rate, nfft, overlap: (
                np.array([0.0, 0.5, 1.0, 1.5], dtype=np.float64),
                np.array([100.0], dtype=np.float64),
                np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float64),
            ),
        )

        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(stft_nfft=8, stft_overlap=0.5),
            sample_rate=100.0,
        )

        result = p.process_chunk(np.ones(100), elapsed_time=1.0)

        assert [frame.time for frame in result["stft_frames"]] == [0.0, 0.5, 1.0]

    def test_process_chunk_keeps_leading_stft_frame_at_zero_for_small_and_large_windows(self):
        chunk = np.random.randn(128).astype(np.float32)

        small = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(stft_nfft=8, stft_overlap=0.5),
            sample_rate=100.0,
        )
        large = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(stft_nfft=32, stft_overlap=0.5),
            sample_rate=100.0,
        )

        small_result = small.process_chunk(chunk, elapsed_time=1.28)
        large_result = large.process_chunk(chunk, elapsed_time=1.28)

        assert small_result["stft_frames"][0].time == pytest.approx(0.0)
        assert large_result["stft_frames"][0].time == pytest.approx(0.0)

    def test_reset_clears_stft_dedupe_state(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_stft",
            lambda signal, sample_rate, nfft, overlap: (
                np.array([0.0, 0.5, 1.0], dtype=np.float64),
                np.array([100.0], dtype=np.float64),
                np.array([[1.0, 2.0, 3.0]], dtype=np.float64),
            ),
        )

        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(stft_nfft=8, stft_overlap=0.5),
            sample_rate=100.0,
        )

        p.process_chunk(np.ones(100), elapsed_time=1.0)
        p.reset()
        second_after_reset = p.process_chunk(np.ones(100), elapsed_time=1.0)

        assert [frame.time for frame in second_after_reset["stft_frames"]] == [0.0, 0.5, 1.0]
