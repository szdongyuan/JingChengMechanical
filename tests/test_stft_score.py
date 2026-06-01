from __future__ import annotations

import builtins
import time

import numpy as np
import pytest

from app.service.analysis import stft_score


def _tone(sample_rate: float, duration: float, frequency: float, amplitude: float = 0.01):
    t = np.arange(int(sample_rate * duration), dtype=float) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * frequency * t)


def _synthetic_fm_signal(*, sample_rate=25600.0, duration=4.0, base_hz=45.0, depth=0.001):
    times = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    frequency = base_hz * (1.0 + depth * np.sin(2.0 * np.pi * 0.7 * times))
    phase = 2.0 * np.pi * np.cumsum(frequency) / sample_rate
    signal = 0.05 * np.sin(phase)
    signal += 0.0175 * np.sin(2.0 * phase + 0.2)
    return signal, frequency


def _default_config(**overrides):
    config = stft_score.default_score_config()
    config.update(overrides)
    return config


def _loudness_trace_entry(times_sec, values_sone):
    values = np.asarray(values_sone, dtype=np.float64)
    return {
        "times_sec": np.asarray(times_sec, dtype=np.float64).tolist(),
        "values_sone": values.tolist(),
        "max_sone": float(np.max(values)),
        "algorithm": stft_score.dsp.LOUDNESS_TRACE_ALGORITHM,
        "internal_rate_hz": stft_score.dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
    }


def test_dominant_speed_frequency_hz_detects_known_line():
    sample_rate = 25600.0
    duration = 4.0
    times = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    signal = 0.05 * np.sin(2.0 * np.pi * 45.0 * times)
    signal += 0.01 * np.sin(2.0 * np.pi * 90.0 * times)

    frequency = stft_score._dominant_speed_frequency_hz(signal, sample_rate)

    assert frequency == pytest.approx(45.0, abs=0.25)


def test_dominant_speed_frequency_hz_rejects_noise_only():
    rng = np.random.default_rng(20260526)
    signal = rng.normal(0.0, 0.01, size=25600 * 4)

    assert stft_score._dominant_speed_frequency_hz(signal, 25600.0) is None


def test_dominant_speed_frequency_hz_rejects_weak_peak():
    sample_rate = 25600.0
    duration = 4.0
    rng = np.random.default_rng(20260527)
    times = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    signal = np.zeros(times.shape, dtype=np.float64)
    for frequency_hz, phase in zip(
        np.arange(30.0, 150.1, 0.5),
        rng.uniform(0.0, 2.0 * np.pi, size=241),
        strict=False,
    ):
        signal += 0.0001 * np.sin(2.0 * np.pi * frequency_hz * times + phase)
    signal += 0.0001 * np.sin(2.0 * np.pi * 45.0 * times)

    assert stft_score._dominant_speed_frequency_hz(signal, sample_rate) is None


def test_estimate_speed_frequency_track_tracks_small_fm():
    signal, true_frequency = _synthetic_fm_signal(depth=0.001)
    score_times = np.arange(0.0, 4.0 + 1e-12, 0.04)
    track = stft_score._estimate_speed_frequency_track(
        signal,
        25600.0,
        score_times_sec=score_times,
    )
    true_track = np.interp(score_times, np.arange(signal.size) / 25600.0, true_frequency)
    usable = np.isfinite(track) & (score_times >= 0.2) & (score_times <= 3.8)

    assert np.mean(usable) > 0.8
    tracked_dev = np.percentile(
        np.abs(track[usable] - np.median(track[usable])) / np.median(track[usable]),
        95,
    )
    true_dev = np.percentile(
        np.abs(true_track[usable] - np.median(true_track[usable]))
        / np.median(true_track[usable]),
        95,
    )
    assert tracked_dev == pytest.approx(true_dev, abs=0.00035)


def test_speed_track_unavailable_when_dominant_line_rejected():
    rng = np.random.default_rng(20260528)
    signal = rng.normal(0.0, 0.01, size=25600 * 4)
    score_times = np.arange(0.0, 4.0 + 1e-12, 0.04)

    track = stft_score._estimate_speed_frequency_track(
        signal,
        25600.0,
        score_times_sec=score_times,
    )
    factors = stft_score._speed_stability_factors(
        score_times,
        track,
        reference=0.001,
        absolute_start_sec=0.0,
        window_duration_sec=4.0,
    )

    assert not np.any(np.isfinite(track))
    assert factors == pytest.approx([0.0] * score_times.size)


def test_estimate_speed_frequency_track_does_not_allocate_full_stft(monkeypatch):
    signal, _true_frequency = _synthetic_fm_signal(depth=0.001)
    score_times = np.arange(0.0, 4.0 + 1e-12, 0.04)

    def fail_stft(*_args, **_kwargs):
        raise AssertionError("speed tracker must not allocate full STFT matrix")

    monkeypatch.setattr(stft_score.dsp.scipy_signal, "stft", fail_stft)

    track = stft_score._estimate_speed_frequency_track(
        signal,
        25600.0,
        score_times_sec=score_times,
    )

    usable = np.isfinite(track) & (score_times >= 0.2) & (score_times <= 3.8)
    assert np.mean(usable) > 0.8


def test_compute_stft_score_returns_summary_and_frame_scores_for_stable_signal():
    sample_rate = 2000.0
    signal = _tone(sample_rate, 3.0, 60.0, amplitude=0.02)

    payload = stft_score.compute_stft_score_payload(
        signal=signal,
        sample_rate=sample_rate,
        stft_nfft=512,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=1.0,
    )

    assert payload is not None
    assert payload["enabled"] is True
    assert 0.0 <= payload["summary_score"] <= 100.0
    assert payload["times_sec"]
    assert payload["scores"]
    assert len(payload["times_sec"]) == len(payload["scores"])
    assert min(payload["times_sec"]) >= 1.0


def test_stft_score_interpolates_from_internal_loudness_trace_without_display_downsampling(monkeypatch):
    internal_times = (np.arange(7, dtype=np.float64) / 150.0).tolist()
    internal_values = [1.0, 10.0, 2.0, 8.0, 3.0, 7.0, 4.0]
    captured = {}
    real_interpolate = stft_score._interpolate_loudness_to_score_times

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_stft",
        lambda *_args, **_kwargs: (
            np.array([1.0 / 150.0], dtype=np.float64),
            np.array([60.0], dtype=np.float64),
            np.array([[-120.0]], dtype=np.float64),
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (internal_times, internal_values, 10.0),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "downsample_loudness_trace_for_display",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("display trace helper must not be used for score interpolation")
        ),
    )

    def spy_interpolate(loudness_times, loudness_values, score_times, *, absolute_start):
        captured["times"] = list(loudness_times)
        captured["values"] = list(loudness_values)
        return real_interpolate(
            loudness_times,
            loudness_values,
            score_times,
            absolute_start=absolute_start,
        )

    monkeypatch.setattr(stft_score, "_interpolate_loudness_to_score_times", spy_interpolate)

    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(32, dtype=np.float64),
        sample_rate=2000.0,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=_default_config(stft_score_loudness_reference_sone=10.0),
        absolute_start_sec=0.0,
    )

    assert payload is not None
    assert captured == {"times": internal_times, "values": internal_values}


def test_stft_score_uses_provided_loudness_trace_without_recompute(monkeypatch):
    internal_times = (np.arange(7, dtype=np.float64) / 150.0).tolist()
    internal_values = [1.0, 10.0, 2.0, 8.0, 3.0, 7.0, 4.0]
    captured = {}
    real_interpolate = stft_score._interpolate_loudness_to_score_times

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_stft",
        lambda *_args, **_kwargs: (
            np.array([1.0 / 150.0], dtype=np.float64),
            np.array([60.0], dtype=np.float64),
            np.array([[-120.0]], dtype=np.float64),
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached loudness trace expected")
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace_for_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached contextual loudness trace expected")
        ),
    )

    def spy_interpolate(loudness_times, loudness_values, score_times, *, absolute_start):
        captured["times"] = list(loudness_times)
        captured["values"] = list(loudness_values)
        return real_interpolate(
            loudness_times,
            loudness_values,
            score_times,
            absolute_start=absolute_start,
        )

    monkeypatch.setattr(stft_score, "_interpolate_loudness_to_score_times", spy_interpolate)

    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(32, dtype=np.float64),
        sample_rate=2000.0,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=_default_config(stft_score_loudness_reference_sone=10.0),
        absolute_start_sec=0.5,
        loudness_context_signal=np.full(200, 0.5, dtype=np.float64),
        loudness_trace=_loudness_trace_entry(internal_times, internal_values),
    )

    assert payload is not None
    assert captured == {"times": internal_times, "values": internal_values}


def test_stft_score_accepts_non_grid_aligned_internal_loudness_trace(monkeypatch):
    duration_sec = 1.011
    sample_rate = 1000.0
    sample_count = int(round(duration_sec * sample_rate))
    assert sample_count / sample_rate == pytest.approx(duration_sec)
    internal_times = (
        np.arange(int(np.floor(duration_sec * 150.0 + 1e-12)) + 1, dtype=np.float64)
        / 150.0
    )
    internal_values = np.linspace(2.0, 4.0, internal_times.size, dtype=np.float64)
    captured = {}

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_stft",
        lambda *_args, **_kwargs: (
            np.array([0.1], dtype=np.float64),
            np.array([60.0], dtype=np.float64),
            np.array([[-120.0]], dtype=np.float64),
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-grid aligned internal cache should be accepted")
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace_for_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-grid aligned contextual cache should be accepted")
        ),
    )

    def spy_interpolate(loudness_times, loudness_values, score_times, *, absolute_start):
        captured["last_time"] = float(np.asarray(loudness_times, dtype=np.float64)[-1])
        captured["count"] = len(loudness_times)
        return np.ones_like(np.asarray(score_times, dtype=np.float64))

    monkeypatch.setattr(stft_score, "_interpolate_loudness_to_score_times", spy_interpolate)

    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(sample_count, dtype=np.float64),
        sample_rate=sample_rate,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=_default_config(stft_score_loudness_reference_sone=10.0),
        absolute_start_sec=0.0,
        loudness_trace=_loudness_trace_entry(internal_times, internal_values),
    )

    assert payload is not None
    assert captured["count"] == internal_times.size
    assert captured["last_time"] == pytest.approx(1.0066666667)


def test_stft_score_rejects_display_loudness_trace_and_falls_back(monkeypatch):
    fallback_times = (np.arange(7, dtype=np.float64) / 150.0).tolist()
    fallback_values = [5.0, 5.5, 6.0, 5.5, 5.0, 4.5, 4.0]
    captured = {}
    real_interpolate = stft_score._interpolate_loudness_to_score_times

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_stft",
        lambda *_args, **_kwargs: (
            np.array([1.0 / 150.0], dtype=np.float64),
            np.array([60.0], dtype=np.float64),
            np.array([[-120.0]], dtype=np.float64),
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (fallback_times, fallback_values, max(fallback_values)),
    )

    def spy_interpolate(loudness_times, loudness_values, score_times, *, absolute_start):
        captured["times"] = list(loudness_times)
        captured["values"] = list(loudness_values)
        return real_interpolate(
            loudness_times,
            loudness_values,
            score_times,
            absolute_start=absolute_start,
        )

    monkeypatch.setattr(stft_score, "_interpolate_loudness_to_score_times", spy_interpolate)

    display_trace = _loudness_trace_entry([0.0, 0.04], [99.0, 99.0])
    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(32, dtype=np.float64),
        sample_rate=2000.0,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=_default_config(stft_score_loudness_reference_sone=10.0),
        absolute_start_sec=0.0,
        loudness_trace=display_trace,
    )

    assert payload is not None
    assert captured == {"times": fallback_times, "values": fallback_values}


def test_stft_score_cached_loudness_trace_matches_recomputed_score(monkeypatch):
    sample_rate = 2000.0
    signal = _tone(sample_rate, 0.5, 60.0, amplitude=0.02)
    trace_times = np.arange(76, dtype=np.float64) / 150.0
    trace_values = 5.0 + 0.5 * np.sin(2.0 * np.pi * 4.0 * trace_times)
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (
            trace_times.tolist(),
            trace_values.tolist(),
            float(np.max(trace_values)),
        ),
    )

    recomputed = stft_score.compute_stft_score_payload(
        signal=signal,
        sample_rate=sample_rate,
        stft_nfft=128,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=0.0,
    )
    cached = stft_score.compute_stft_score_payload(
        signal=signal,
        sample_rate=sample_rate,
        stft_nfft=128,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=0.0,
        loudness_trace=_loudness_trace_entry(trace_times, trace_values),
    )

    assert cached is not None
    assert recomputed is not None
    assert cached["summary_score"] == pytest.approx(recomputed["summary_score"])
    assert cached["times_sec"] == pytest.approx(recomputed["times_sec"])
    assert cached["scores"] == pytest.approx(recomputed["scores"])


def test_stft_score_uses_contextual_loudness_for_mid_file_window(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_stft",
        lambda *_args, **_kwargs: (
            np.array([0.1], dtype=np.float64),
            np.array([60.0], dtype=np.float64),
            np.array([[-120.0]], dtype=np.float64),
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("selected-window loudness must not be used")
        ),
    )

    def fake_loudness_for_window(signal, sample_rate, start_sec, end_sec):
        captured["signal"] = np.asarray(signal, dtype=np.float64).copy()
        captured["sample_rate"] = sample_rate
        captured["window"] = (start_sec, end_sec)
        return [0.0, 0.1], [8.0, 8.0], 8.0

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace_for_window",
        fake_loudness_for_window,
    )

    context_signal = np.full(200, 0.5, dtype=np.float64)
    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(32, dtype=np.float64),
        sample_rate=2000.0,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=_default_config(stft_score_loudness_reference_sone=10.0),
        absolute_start_sec=0.5,
        loudness_context_signal=context_signal,
    )

    assert payload is not None
    np.testing.assert_allclose(captured["signal"], context_signal)
    assert captured["sample_rate"] == 2000.0
    assert captured["window"] == pytest.approx((0.5, 0.516))
    assert payload["times_sec"] == pytest.approx([0.6])


def test_speed_stability_affects_score_independent_of_loudness(monkeypatch):
    relative_times = np.arange(0.0, 0.30 + 1e-12, 0.025)
    absolute_start = 2.0
    constant_signal = np.ones(4096, dtype=np.float64)
    drifting_signal = np.full(4096, 2.0, dtype=np.float64)
    speed_calls = []
    constant_track = np.full(relative_times.size, 60.0, dtype=np.float64)
    drifting_track = np.array(
        [60.0, 60.4, 60.8, 60.4, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0],
        dtype=np.float64,
    )

    monkeypatch.setattr(
        stft_score.dsp,
        "compute_stft",
        lambda *_args, **_kwargs: (
            relative_times,
            np.array([60.0], dtype=np.float64),
            np.full((1, relative_times.size), -120.0, dtype=np.float64),
        ),
    )
    monkeypatch.setattr(
        stft_score.dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: (
            relative_times.tolist(),
            [0.001] * relative_times.size,
            0.001,
        ),
    )

    def fake_speed_track(signal, sample_rate, *, score_times_sec):
        speed_calls.append(np.asarray(score_times_sec, dtype=np.float64).copy())
        assert sample_rate == pytest.approx(2000.0)
        np.testing.assert_allclose(score_times_sec, relative_times)
        if float(np.max(signal)) > 1.5:
            return drifting_track
        return constant_track

    def fake_f0_values(signal, sample_rate, *, score_times_sec, absolute_start_sec):
        assert sample_rate == pytest.approx(2000.0)
        assert absolute_start_sec == pytest.approx(absolute_start)
        np.testing.assert_allclose(score_times_sec, relative_times + absolute_start)
        return [None] * relative_times.size

    monkeypatch.setattr(stft_score, "_estimate_speed_frequency_track", fake_speed_track)
    monkeypatch.setattr(stft_score, "_estimate_frame_f0_values", fake_f0_values)

    cfg = _default_config(
        stft_score_loudness_reference_sone=10.0,
        stft_score_frequency_instability_reference=0.001,
    )
    stable_payload = stft_score.compute_stft_score_payload(
        signal=constant_signal,
        sample_rate=2000.0,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=cfg,
        absolute_start_sec=absolute_start,
    )
    drifting_payload = stft_score.compute_stft_score_payload(
        signal=drifting_signal,
        sample_rate=2000.0,
        stft_nfft=16,
        stft_overlap=0.5,
        score_config=cfg,
        absolute_start_sec=absolute_start,
    )

    assert stable_payload is not None
    assert drifting_payload is not None
    assert stable_payload["times_sec"] == pytest.approx((relative_times + absolute_start).tolist())
    assert len(speed_calls) == 2
    assert drifting_payload["summary_score"] <= stable_payload["summary_score"] - 50.0


def test_strong_modulation_and_harmonics_score_lower_than_stable_tone(monkeypatch):
    sample_rate = 4000.0
    t = np.arange(int(sample_rate * 4.0), dtype=float) / sample_rate
    stable = 0.03 * np.sin(2.0 * np.pi * 60.0 * t)
    modulation = 1.0 + 0.75 * np.sin(2.0 * np.pi * 4.0 * t)
    harmonic_rich = modulation * (
        0.02 * np.sin(2.0 * np.pi * 60.0 * t)
        + 0.015 * np.sin(2.0 * np.pi * 600.0 * t)
        + 0.015 * np.sin(2.0 * np.pi * 1200.0 * t)
    )

    def fake_loudness_trace(signal, sample_rate):
        times = np.arange(0.25, 4.0, 0.25)
        if np.max(np.abs(signal)) > np.max(np.abs(stable)):
            values = 0.02 + 0.015 * (1.0 + np.sin(2.0 * np.pi * times))
        else:
            values = np.full(times.shape, 0.02)
        return times.tolist(), values.tolist(), float(np.max(values))

    monkeypatch.setattr(stft_score.dsp, "compute_loudness_trace", fake_loudness_trace)

    cfg = _default_config(
        stft_score_loudness_reference_sone=0.01,
        stft_score_modulation_reference=0.05,
        stft_score_harmonic_ratio_reference=0.05,
        stft_score_frequency_instability_reference=0.02,
    )
    stable_payload = stft_score.compute_stft_score_payload(
        signal=stable,
        sample_rate=sample_rate,
        stft_nfft=512,
        stft_overlap=0.5,
        score_config=cfg,
        absolute_start_sec=0.0,
    )
    bad_payload = stft_score.compute_stft_score_payload(
        signal=harmonic_rich,
        sample_rate=sample_rate,
        stft_nfft=512,
        stft_overlap=0.5,
        score_config=cfg,
        absolute_start_sec=0.0,
    )

    assert bad_payload["summary_score"] < stable_payload["summary_score"]


def test_periodic_f0_estimation_accepts_periodicity_inside_fixed_range():
    sample_rate = 2000.0
    signal = _tone(sample_rate, 1.0, 60.0)

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 == pytest.approx(60.0, rel=0.03)


def test_periodic_f0_estimation_chooses_strongest_in_range_autocorrelation_peak():
    sample_rate = 2000.0
    duration = 1.0
    t = np.arange(int(sample_rate * duration), dtype=float) / sample_rate
    signal = (
        0.01 * np.sin(2.0 * np.pi * 35.0 * t)
        + 0.04 * np.sin(2.0 * np.pi * 70.0 * t)
    )

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 == pytest.approx(35.0, rel=0.03)


def test_periodic_f0_estimation_invalid_signal_returns_none_without_exception():
    f0 = stft_score.estimate_periodic_f0_hz(["bad"], sample_rate=2000.0)

    assert f0 is None


def test_oversized_numeric_inputs_return_unavailable_or_defaults_without_exception():
    oversized = 10**10000

    f0 = stft_score.estimate_periodic_f0_hz(np.ones(512), sample_rate=oversized)
    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(512),
        sample_rate=oversized,
        stft_nfft=256,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=0.0,
    )
    normalized = stft_score.normalize_score_config(
        {"stft_score_loudness_reference_sone": oversized}
    )

    assert f0 is None
    assert payload is None
    assert (
        normalized["stft_score_loudness_reference_sone"]
        == stft_score.default_score_config()["stft_score_loudness_reference_sone"]
    )


@pytest.mark.parametrize(
    ("frequencies_hz", "frame_power"),
    [
        ([0.0, 10**10000], [1.0, 1.0]),
        ([0.0, 10.0], [1.0, 10**10000]),
        ([0.0, "bad"], [1.0, 1.0]),
        ([0.0, 10.0], [1.0, "bad"]),
    ],
)
def test_harmonic_ratio_invalid_or_overflowing_arrays_returns_zero_without_exception(
    frequencies_hz,
    frame_power,
):
    ratio = stft_score.harmonic_energy_ratio(
        frequencies_hz=frequencies_hz,
        frame_power=frame_power,
        f0_hz=1.0,
        start_order=2,
        end_order=2,
    )

    assert ratio == 0.0


def test_summary_score_overflowing_values_returns_none_without_exception():
    assert stft_score.summary_score([10**10000], mode="mean", percentile=10.0) is None


def test_normalize_score_config_keeps_harmonic_end_order_at_or_above_start_order():
    normalized = stft_score.normalize_score_config(
        {"stft_score_harmonic_start_order": 200}
    )

    assert (
        normalized["stft_score_harmonic_end_order"]
        >= normalized["stft_score_harmonic_start_order"]
    )


def test_periodic_f0_estimation_accepts_periodicity_at_lower_boundary():
    sample_rate = 2000.0
    signal = _tone(sample_rate, 1.0, 30.0)

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 == pytest.approx(30.0, rel=0.03)


def test_periodic_f0_estimation_accepts_periodicity_at_upper_boundary():
    sample_rate = 3000.0
    signal = _tone(sample_rate, 1.0, 150.0)

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 == pytest.approx(150.0, rel=0.03)


def test_periodic_f0_estimation_accepts_upper_boundary_at_2000hz_sample_rate():
    sample_rate = 2000.0
    signal = _tone(sample_rate, 1.0, 150.0)

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 == pytest.approx(150.0, rel=0.03)


def test_periodic_f0_estimation_is_fast_at_realistic_sample_rate():
    sample_rate = 44100.0
    signal = _tone(sample_rate, 0.5, 60.0)

    started = time.perf_counter()
    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )
    elapsed = time.perf_counter() - started

    assert f0 == pytest.approx(60.0, rel=0.03)
    assert elapsed < 0.5


def test_periodic_f0_estimation_rejects_periodicity_below_fixed_range():
    sample_rate = 2000.0
    signal = _tone(sample_rate, 1.0, 20.0)

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 is None


@pytest.mark.parametrize("frequency_hz", [25.0, 29.0])
def test_periodic_f0_estimation_rejects_near_below_range_edge_tones(frequency_hz):
    sample_rate = 2000.0
    signal = _tone(sample_rate, 1.0, frequency_hz)

    f0 = stft_score.estimate_periodic_f0_hz(
        signal,
        sample_rate=sample_rate,
    )

    assert f0 is None


def test_frequency_instability_is_zero_when_current_frame_f0_unavailable():
    factors = stft_score._frequency_instability_factors(
        np.array([0.0, 0.25, 0.5]),
        [60.0, None, 90.0],
        reference=0.05,
    )

    assert factors[1] == 0.0


def test_frequency_instability_detects_smooth_local_f0_drift():
    factors = stft_score._frequency_instability_factors(
        np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
        [60.0, 60.5, 61.0, 61.5, 62.0],
        reference=0.02,
    )

    assert factors[2] == pytest.approx(1.0)


def test_frequency_instability_is_zero_for_constant_local_f0():
    factors = stft_score._frequency_instability_factors(
        np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
        [60.0, 60.0, 60.0, 60.0, 60.0],
        reference=0.02,
    )

    assert factors[2] == pytest.approx(0.0)


def test_speed_stability_is_zero_for_constant_speed_track():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10, 0.15, 0.20],
        [120.0, 120.0, 120.0, 120.0, 120.0],
        reference=0.001,
        absolute_start_sec=0.0,
        window_duration_sec=0.20,
    )
    assert factors == pytest.approx([0.0, 0.0, 0.0, 0.0, 0.0])


def test_speed_stability_uses_quarter_second_local_window():
    assert stft_score.SPEED_LOCAL_RIPPLE_SPAN_SEC == pytest.approx(0.25)


def test_speed_stability_detects_local_ripple_on_ramp():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.225, 0.25, 0.275, 0.30],
        [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.5, 104.0, 104.5, 105.0, 105.5, 106.0],
        reference=0.05,
        absolute_start_sec=0.0,
        window_duration_sec=0.30,
    )

    assert factors[0] == pytest.approx(0.0)
    assert factors[-1] == pytest.approx(0.0)
    assert factors[5] == pytest.approx(4.0 / 102.5 / 0.05)
    assert factors[6] == pytest.approx(4.0 / 103.0 / 0.05)
    assert factors[7] == pytest.approx(4.0 / 103.5 / 0.05)


def test_speed_stability_saturates_local_ramp():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30],
        [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
        reference=0.001,
        absolute_start_sec=0.0,
        window_duration_sec=0.30,
    )

    assert factors[3] == pytest.approx(1.0)


def test_speed_stability_detects_rise_then_fall_local_ripple():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.225, 0.25, 0.275, 0.30],
        [100.0, 102.0, 104.0, 102.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
        reference=0.02,
        absolute_start_sec=0.0,
        window_duration_sec=0.30,
    )

    assert factors[5] > 0.95


def test_speed_stability_detects_alternating_local_ripple():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.225, 0.25, 0.275, 0.30],
        [102.0, 98.0, 102.0, 98.0, 102.0, 98.0, 102.0, 98.0, 102.0, 98.0, 102.0, 98.0, 102.0],
        reference=0.02,
        absolute_start_sec=0.0,
        window_duration_sec=0.30,
    )

    assert factors[5] > 0.95
    assert factors[6] > 0.95
    assert factors[7] > 0.95


def test_speed_stability_requires_three_real_local_samples():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.20],
        [100.0, 104.0, 100.0],
        reference=0.02,
        absolute_start_sec=0.0,
        window_duration_sec=0.20,
    )

    assert factors == pytest.approx([0.0, 0.0, 0.0])


def test_speed_stability_uses_full_range_for_three_or_four_local_samples():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.10, 0.125, 0.25],
        [100.0, 104.0, 101.0, 100.0],
        reference=0.04,
        absolute_start_sec=0.0,
        window_duration_sec=0.25,
    )

    assert factors[2] == pytest.approx(4.0 / 100.5 / 0.04)


def test_speed_stability_returns_zero_for_unavailable_and_unbracketed_frames():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10, 0.15, 0.20],
        [100.0, None, 102.0, 103.0, 104.0],
        reference=0.01,
        absolute_start_sec=0.0,
        window_duration_sec=0.20,
    )

    assert factors[0] == pytest.approx(0.0)
    assert factors[1] == pytest.approx(0.0)
    assert factors[-1] == pytest.approx(0.0)


def test_speed_stability_sorts_absolute_times_before_interpolation():
    factors = stft_score._speed_stability_factors(
        [10.15, 10.00, 10.30, 10.05, 10.10, 10.20, 10.25],
        [103.0, 100.0, 106.0, 101.0, 102.0, 104.0, 105.0],
        reference=0.02,
        absolute_start_sec=10.0,
        window_duration_sec=0.31,
    )

    assert factors[0] == pytest.approx(1.0)


def test_speed_stability_duplicate_valid_times_return_zero():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.05, 0.10],
        [100.0, 101.0, 102.0, 103.0],
        reference=0.01,
        absolute_start_sec=0.0,
        window_duration_sec=0.10,
    )

    assert factors == pytest.approx([0.0, 0.0, 0.0, 0.0])


def test_speed_stability_mismatched_lengths_return_zero():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10],
        [100.0, 101.0],
        reference=0.01,
        absolute_start_sec=0.0,
        window_duration_sec=0.10,
    )

    assert factors == pytest.approx([0.0, 0.0, 0.0])


def test_speed_stability_insufficient_valid_samples_return_zero():
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10],
        [None, 101.0, None],
        reference=0.01,
        absolute_start_sec=0.0,
        window_duration_sec=0.10,
    )

    assert factors == pytest.approx([0.0, 0.0, 0.0])


@pytest.mark.parametrize("window_duration_sec", [0.0, -0.1, float("nan"), float("inf")])
def test_speed_stability_invalid_window_duration_returns_zero(window_duration_sec):
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10],
        [100.0, 101.0, 102.0],
        reference=0.01,
        absolute_start_sec=0.0,
        window_duration_sec=window_duration_sec,
    )

    assert factors == pytest.approx([0.0, 0.0, 0.0])


@pytest.mark.parametrize("reference", [0.0, -0.1, float("nan"), float("inf")])
def test_speed_stability_invalid_reference_returns_zero(reference):
    factors = stft_score._speed_stability_factors(
        [0.0, 0.05, 0.10],
        [100.0, 101.0, 102.0],
        reference=reference,
        absolute_start_sec=0.0,
        window_duration_sec=0.10,
    )
    assert factors == pytest.approx([0.0, 0.0, 0.0])


def test_harmonic_ratio_uses_total_frame_energy_and_high_orders():
    frequencies = np.arange(0.0, 2001.0, 1.0)
    power = np.zeros_like(frequencies)
    f0_hz = 30.0
    power[int(10.0)] = 100.0
    for order in (20, 40, 60):
        index = int(np.argmin(np.abs(frequencies - f0_hz * order)))
        power[index] = 100.0

    ratio = stft_score.harmonic_energy_ratio(
        frequencies_hz=frequencies,
        frame_power=power,
        f0_hz=f0_hz,
        start_order=2,
        end_order=100,
    )

    assert ratio == pytest.approx(300.0 / 400.0)


def test_harmonic_ratio_unions_overlapping_high_order_bands():
    frequencies = np.arange(0.0, 1001.0, 1.0)
    power = np.ones_like(frequencies)

    ratio = stft_score.harmonic_energy_ratio(
        frequencies_hz=frequencies,
        frame_power=power,
        f0_hz=5.0,
        start_order=100,
        end_order=101,
    )

    expected_mask = (
        (np.abs(frequencies - 500.0) <= max(1.0, 0.02 * 500.0))
        | (np.abs(frequencies - 505.0) <= max(1.0, 0.02 * 505.0))
    )
    assert ratio == pytest.approx(float(expected_mask.sum()) / float(power.sum()))


def test_harmonic_ratio_bounds_large_end_order_to_usable_frequency_range(monkeypatch):
    frequencies = np.arange(0.0, 101.0, 1.0)
    power = np.ones_like(frequencies)
    f0_hz = 20.0
    usable_last_order = int(np.floor(float(np.max(frequencies)) / f0_hz))
    expected = stft_score.harmonic_energy_ratio(
        frequencies_hz=frequencies,
        frame_power=power,
        f0_hz=f0_hz,
        start_order=2,
        end_order=usable_last_order,
    )

    def bounded_range(*args):
        stop = args[0] if len(args) == 1 else args[1]
        assert stop <= usable_last_order + 1
        return builtins.range(*args)

    monkeypatch.setattr(stft_score, "range", bounded_range, raising=False)

    ratio = stft_score.harmonic_energy_ratio(
        frequencies_hz=frequencies,
        frame_power=power,
        f0_hz=f0_hz,
        start_order=2,
        end_order=10**9,
    )

    assert ratio == pytest.approx(expected)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("percentile", 17.5), ("mean", 50.0), ("min", 10.0)],
)
def test_summary_modes(mode, expected):
    values = np.array([10.0, 20.0, 80.0, 90.0])

    result = stft_score.summary_score(values, mode=mode, percentile=25.0)

    assert result == pytest.approx(expected)


def test_disabled_score_config_returns_disabled_payload():
    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(512),
        sample_rate=2000.0,
        stft_nfft=256,
        stft_overlap=0.5,
        score_config=_default_config(stft_score_enabled=False),
        absolute_start_sec=0.0,
    )

    assert payload == {"enabled": False}


@pytest.mark.parametrize("score_config", ["bad", 123, ["not", "a", "mapping"]])
def test_malformed_truthy_score_config_normalizes_to_defaults_without_exception(score_config):
    assert stft_score.normalize_score_config(score_config) == stft_score.default_score_config()

    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(32),
        sample_rate=2000.0,
        stft_nfft=256,
        stft_overlap=0.5,
        score_config=score_config,
        absolute_start_sec=0.0,
    )

    assert payload is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"signal": np.array([], dtype=float), "sample_rate": 2000.0},
        {"signal": np.ones(32), "sample_rate": 2000.0},
        {"signal": np.ones(512), "sample_rate": 0.0},
        {"signal": np.ones(512), "sample_rate": float("nan")},
        {"signal": ["bad"], "sample_rate": 2000.0},
    ],
)
def test_invalid_or_insufficient_input_returns_unavailable_without_exception(kwargs):
    payload = stft_score.compute_stft_score_payload(
        signal=kwargs["signal"],
        sample_rate=kwargs["sample_rate"],
        stft_nfft=256,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=0.0,
    )

    assert payload is None


def test_loudness_failure_returns_unavailable_without_exception(monkeypatch):
    def fail_loudness(*_args, **_kwargs):
        raise RuntimeError("loudness backend unavailable")

    monkeypatch.setattr(stft_score.dsp, "compute_loudness_trace", fail_loudness)

    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(1024),
        sample_rate=2000.0,
        stft_nfft=256,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=0.0,
    )

    assert payload is None


def test_stft_nonfinite_magnitude_to_power_returns_unavailable(monkeypatch):
    def fake_stft(*_args, **_kwargs):
        return (
            np.array([0.25, 0.5]),
            np.array([0.0, 60.0]),
            np.full((2, 2), 4000.0),
        )

    def fake_loudness_trace(*_args, **_kwargs):
        return [0.25, 0.5], [0.01, 0.01], 0.01

    monkeypatch.setattr(stft_score.dsp, "compute_stft", fake_stft)
    monkeypatch.setattr(stft_score.dsp, "compute_loudness_trace", fake_loudness_trace)

    payload = stft_score.compute_stft_score_payload(
        signal=np.ones(512),
        sample_rate=2000.0,
        stft_nfft=256,
        stft_overlap=0.5,
        score_config=_default_config(),
        absolute_start_sec=0.0,
    )

    assert payload is None
