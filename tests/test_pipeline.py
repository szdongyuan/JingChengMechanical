"""Tests for AnalysisPipeline streaming analysis."""
from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.pipeline import (
    _LIVE_LOUDNESS_THROTTLE_SEC,
    _LIVE_LOUDNESS_WINDOW_BACKEND_SAMPLES,
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


def _stable_calibrator_signal(
    *,
    sample_rate: float = 44100.0,
    duration_sec: float = 3.0,
    freq_hz: float = 1000.0,
    spl_db: float = 94.0,
) -> tuple[np.ndarray, float]:
    amplitude = np.sqrt(2.0) * 20e-6 * (10.0 ** (spl_db / 20.0))
    times = np.arange(int(sample_rate * duration_sec), dtype=np.float64) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * freq_hz * times), sample_rate


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

    def test_process_chunk_skips_octave_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_octave_bands",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("live processing should not compute octave bands by default")
            ),
        )
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(), sample_rate=1000.0)

        result = p.process_chunk(np.ones(512, dtype=np.float32), elapsed_time=0.512)

        assert result["octave"] is None

    def test_process_chunk_can_compute_octave_when_requested(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_octave_bands",
            lambda signal, sample_rate, weighting: (
                np.array([1000.0], dtype=np.float64),
                np.array([42.0], dtype=np.float64),
            ),
        )
        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(oct_weighting="Z"),
            sample_rate=1000.0,
        )

        result = p.process_chunk(
            np.ones(512, dtype=np.float32),
            elapsed_time=0.512,
            compute_octave=True,
        )

        assert isinstance(result["octave"], OctaveFrame)
        np.testing.assert_array_equal(result["octave"].center_freqs, np.array([1000.0]))
        np.testing.assert_array_equal(result["octave"].levels, np.array([42.0]))
        assert result["octave"].weighting == "Z"

    def test_spl_frame_type(self):
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(),
                             sample_rate=44100.0)
        chunk = np.random.randn(2048).astype(np.float32)
        result = p.process_chunk(chunk, elapsed_time=1.0)
        assert isinstance(result["spl"], SplFrame)
        assert result["spl"].weighting == "A"

    @pytest.mark.parametrize("weighting", ["A", "C", "Z"])
    def test_process_chunk_live_spl_is_stable_for_1khz_calibrator(self, weighting):
        sample_rate = 44100.0
        signal, _ = _stable_calibrator_signal(sample_rate=sample_rate)
        cfg = _make_config(
            spl_weighting=weighting,
            spl_time_constant=0.125,
            stft_nfft=1024,
            fft_nfft=4096,
        )
        pipeline = AnalysisPipeline(
            channel_index=0,
            channel_config=cfg,
            sample_rate=sample_rate,
        )

        values = []
        chunk_size = 4096
        for start in range(0, len(signal) - chunk_size + 1, chunk_size):
            elapsed = (start + chunk_size) / sample_rate
            result = pipeline.process_chunk(
                signal[start : start + chunk_size],
                elapsed_time=elapsed,
                compute_loudness=False,
            )
            values.append(result["spl"].spl_db)

        stable_values = np.asarray(values[1:], dtype=np.float64)
        assert float(stable_values.max() - stable_values.min()) <= 0.10

    def test_live_stft_stable_tone_has_no_periodic_broadband_boundary_spikes(self):
        sample_rate = 44100.0
        nfft = 1024
        chunk_size = 4096
        signal, _ = _stable_calibrator_signal(
            sample_rate=sample_rate,
            duration_sec=4.5,
        )
        pipeline = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(
                stft_nfft=nfft,
                stft_overlap=0.5,
                fft_nfft=4096,
                spl_weighting="Z",
                oct_weighting="Z",
            ),
            sample_rate=sample_rate,
        )

        frames = []
        for start in range(0, len(signal) - chunk_size + 1, chunk_size):
            result = pipeline.process_chunk(
                signal[start : start + chunk_size],
                elapsed_time=(start + chunk_size) / sample_rate,
                compute_loudness=False,
            )
            frames.extend(result["stft_frames"])

        assert len(frames) > 20
        frame_times = [float(frame.time) for frame in frames]
        assert frame_times == sorted(frame_times)
        assert len(set(frame_times)) == len(frame_times)

        high_frequency_means = []
        tone_peaks = []
        for frame in frames:
            freqs = np.asarray(frame.frequencies, dtype=np.float64)
            mags = np.asarray(frame.magnitudes, dtype=np.float64)
            high_frequency_means.append(float(np.mean(mags[freqs > 5000.0])))
            tone_peaks.append(float(np.max(mags[np.abs(freqs - 1000.0) < 100.0])))

        assert max(high_frequency_means) < max(tone_peaks) - 80.0

    def test_live_stft_preserves_unprocessed_tail_for_non_hop_aligned_chunks(self):
        sample_rate = 44100.0
        nfft = 1024
        overlap = 0.5
        chunk_size = int(sample_rate * 0.05)
        signal, _ = _stable_calibrator_signal(
            sample_rate=sample_rate,
            duration_sec=1.0,
        )
        pipeline = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(
                stft_nfft=nfft,
                stft_overlap=overlap,
                fft_nfft=4096,
                spl_weighting="Z",
                oct_weighting="Z",
            ),
            sample_rate=sample_rate,
        )

        frames = []
        for start in range(0, len(signal) - chunk_size + 1, chunk_size):
            result = pipeline.process_chunk(
                signal[start : start + chunk_size],
                elapsed_time=(start + chunk_size) / sample_rate,
                compute_loudness=False,
            )
            frames.extend(result["stft_frames"])

        frame_samples = [int(round(float(frame.time) * sample_rate)) for frame in frames]
        assert len(frame_samples) > 20
        assert frame_samples == sorted(frame_samples)
        assert len(set(frame_samples)) == len(frame_samples)

        hop_samples = nfft - int(nfft * overlap)
        frame_deltas = np.diff(frame_samples)
        assert frame_deltas.size > 0
        assert np.all(frame_deltas == hop_samples)

    def test_process_chunk_reuses_one_live_spl_detector(self, monkeypatch):
        instances = []

        class FakeDetector:
            def __init__(self, sample_rate, weighting, time_constant):
                self.calls = []
                self.reset_count = 0
                instances.append(self)

            def process(self, signal):
                self.calls.append(np.asarray(signal).copy())
                return float(len(self.calls))

            def reset(self):
                self.reset_count += 1

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.SplDetector",
            FakeDetector,
        )
        pipeline = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(spl_weighting="Z"),
            sample_rate=100.0,
        )

        first = pipeline.process_chunk(
            np.array([1.0, 2.0], dtype=np.float64),
            elapsed_time=0.02,
            compute_loudness=False,
        )
        second = pipeline.process_chunk(
            np.array([3.0, 4.0], dtype=np.float64),
            elapsed_time=0.04,
            compute_loudness=False,
        )

        assert len(instances) == 1
        assert len(instances[0].calls) == 2
        assert first["spl"].spl_db == pytest.approx(1.0)
        assert second["spl"].spl_db == pytest.approx(2.0)

    def test_reset_clears_live_spl_detector_state(self):
        sample_rate = 44100.0
        chunk_size = 4096
        high_signal, _ = _stable_calibrator_signal(
            sample_rate=sample_rate,
            duration_sec=chunk_size / sample_rate,
            spl_db=94.0,
        )
        low_signal, _ = _stable_calibrator_signal(
            sample_rate=sample_rate,
            duration_sec=chunk_size / sample_rate,
            spl_db=80.0,
        )
        cfg = _make_config(spl_weighting="Z", spl_time_constant=1.0)
        pipeline = AnalysisPipeline(
            channel_index=0,
            channel_config=cfg,
            sample_rate=sample_rate,
        )

        first = pipeline.process_chunk(
            high_signal,
            elapsed_time=chunk_size / sample_rate,
            compute_loudness=False,
        )
        pipeline.reset()
        second = pipeline.process_chunk(
            low_signal,
            elapsed_time=chunk_size / sample_rate,
            compute_loudness=False,
        )

        assert first["spl"].spl_db == pytest.approx(94.0, abs=0.10)
        assert second["spl"].spl_db == pytest.approx(80.0, abs=0.10)

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

    def test_live_fft_uses_channel_fft_weighting(self, monkeypatch):
        calls = []

        def fake_fft(signal, sample_rate, nfft, weighting="Z"):
            calls.append(weighting)
            return np.array([100.0]), np.array([42.0])

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_fft_spectrum",
            fake_fft,
        )
        pipeline = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(fft_nfft=4, fft_weighting="A"),
            sample_rate=1000.0,
        )

        result = pipeline.process_chunk(np.ones(4, dtype=np.float32), elapsed_time=0.004)

        assert calls == ["A"]
        assert isinstance(result["fft"], FftFrame)
        assert result["fft"].weighting == "A"

    def test_live_fft_waits_for_rolling_buffer_then_emits_one_frame_per_chunk(
        self,
        monkeypatch,
    ):
        captured_fft_inputs: list[np.ndarray] = []

        def fake_compute_fft_spectrum(signal, sample_rate, nfft, weighting="Z"):
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

        def fake_compute_loudness_window_sone(signal, sample_rate):
            calls.append(np.asarray(signal, dtype=np.float64).copy())
            return 2.5

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("final trace helper must not be used live")
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.downsample_loudness_trace_for_display",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("display trace helper must not be used live")
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_window_sone",
            fake_compute_loudness_window_sone,
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

        def fake_compute_loudness_window_sone(signal, sample_rate):
            calls.append(np.asarray(signal, dtype=np.float64).copy())
            return 1.0

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("final trace helper must not be used live")
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_window_sone",
            fake_compute_loudness_window_sone,
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
        assert p._loudness_buffer.size == 17
        np.testing.assert_array_equal(calls[0], np.ones(17, dtype=np.float64))

    def test_prepare_live_loudness_input_is_throttled_copy_without_backend_compute(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("backend should not run during preparation")
            ),
        )
        p = AnalysisPipeline(
            channel_index=0,
            channel_config=_make_config(),
            sample_rate=100.0,
        )

        p.process_chunk(
            np.arange(75, dtype=np.float32),
            elapsed_time=0.5,
            compute_loudness=False,
        )

        prepared = p.prepare_live_loudness_input(0.5)
        throttled = p.prepare_live_loudness_input(0.6)
        prepared[0] = -1.0

        assert throttled is None
        np.testing.assert_array_equal(
            prepared[1:],
            np.arange(59, 75, dtype=np.float64),
        )
        assert p._loudness_buffer[0] == pytest.approx(58.0)
        assert p._last_live_loudness_emit_time == pytest.approx(0.5)

    def test_live_loudness_throttle_is_quarter_second_and_window_is_8192_backend_samples(self):
        assert _LIVE_LOUDNESS_THROTTLE_SEC == pytest.approx(0.25)
        assert _LIVE_LOUDNESS_WINDOW_BACKEND_SAMPLES == 8192

    def test_process_chunk_suppresses_live_loudness_backend_errors(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("final trace helper must not be used live")
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_window_sone",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("fast backend failed")
            ),
        )
        p = AnalysisPipeline(channel_index=0, channel_config=_make_config(), sample_rate=1000.0)
        result = p.process_chunk(np.ones(100, dtype=np.float32), elapsed_time=1.0)

        assert result["loudness"] is None
        assert isinstance(result["spl"], SplFrame)
        assert set(result) == {"stft_frames", "stft", "spl", "loudness", "octave", "fft"}

    def test_reset_clears_live_loudness_buffer_and_throttle(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("final trace helper must not be used live")
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_window_sone",
            lambda signal, sample_rate: 3.0,
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

    def test_process_chunk_uses_complete_window_center_times_for_real_stft(self):
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

        assert small_result["stft_frames"][0].time == pytest.approx(8 / (2 * 100.0))
        assert large_result["stft_frames"][0].time == pytest.approx(32 / (2 * 100.0))

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
