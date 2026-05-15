"""Tests for pure DSP functions."""
from __future__ import annotations

import time

import numpy as np
import pytest

import app.service.analysis.dsp as dsp_module
from app.service.analysis.dsp import (
    OCTAVE_CENTER_FREQS,
    a_weighting,
    apply_mic_calibration,
    c_weighting,
    compute_fft_spectrum,
    compute_loudness_trace,
    compute_octave_bands,
    compute_spl,
    compute_spl_leq,
    compute_spl_trace,
    compute_stft,
    phon_to_sone,
    sone_to_phon,
)


def _level_for_center(
    centers: np.ndarray,
    levels: np.ndarray,
    target_freq: float,
) -> float:
    """Return the level for a specific IEC center frequency."""
    idx = np.where(np.isclose(centers, target_freq))[0]
    assert idx.size == 1
    return float(levels[idx[0]])


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


def _calibrated_sine_signal(
    *,
    frequency_hz: float,
    spl_db: float = 94.0,
    sample_rate: float = 48000.0,
    duration_sec: float = 3.0,
) -> tuple[np.ndarray, float]:
    times = np.arange(int(sample_rate * duration_sec), dtype=np.float64) / sample_rate
    amplitude = np.sqrt(2.0) * 20e-6 * (10.0 ** (spl_db / 20.0))
    return amplitude * np.sin(2.0 * np.pi * frequency_hz * times), sample_rate


def _deterministic_multitone_signal(
    *,
    sample_rate: float = 48000.0,
    duration_sec: float = 0.75,
) -> tuple[np.ndarray, float]:
    times = np.arange(int(sample_rate * duration_sec), dtype=np.float64) / sample_rate
    rms = 20e-6 * 10.0 ** (74.0 / 20.0)
    signal = (
        0.62 * np.sin(2.0 * np.pi * 125.0 * times)
        + 0.41 * np.sin(2.0 * np.pi * 1000.0 * times)
        + 0.29 * np.sin(2.0 * np.pi * 3150.0 * times)
        + 0.17 * np.sin(2.0 * np.pi * 8000.0 * times)
    )
    signal_rms = float(np.sqrt(np.mean(signal ** 2)))
    return signal * (rms / signal_rms), sample_rate


def _loudness_spectrum_for_signal(
    signal: np.ndarray,
    sample_rate: float,
) -> tuple[np.ndarray, np.ndarray]:
    window_samples = dsp_module._loudness_window_samples(sample_rate)
    hop_samples = dsp_module._loudness_hop_samples(sample_rate)
    starts = dsp_module._loudness_window_starts(signal.size, window_samples, hop_samples)
    windows = dsp_module._loudness_windows(signal, starts, window_samples)
    return dsp_module._one_sided_rms_spectrum_matrix(windows, sample_rate)


def test_loudness_spectrum_helper_returns_one_sided_matrix_shape():
    sample_rate = 48000.0
    signal, _ = _calibrated_sine_signal(
        frequency_hz=998.6,
        sample_rate=sample_rate,
        duration_sec=0.75,
    )
    window_samples = dsp_module._loudness_window_samples(sample_rate)
    hop_samples = dsp_module._loudness_hop_samples(sample_rate)
    starts = dsp_module._loudness_window_starts(signal.size, window_samples, hop_samples)
    windows = dsp_module._loudness_windows(signal, starts, window_samples)

    spectrum, freqs = dsp_module._one_sided_rms_spectrum_matrix(windows, sample_rate)

    assert spectrum.shape == (window_samples // 2, starts.size)
    assert freqs.shape == (window_samples // 2,)
    assert freqs[0] == pytest.approx(sample_rate / window_samples)
    assert freqs[-1] == pytest.approx(sample_rate / 2.0)
    assert np.all(np.isfinite(spectrum))
    assert np.all(spectrum >= 0.0)


class TestAWeighting:
    def test_1khz_is_zero(self):
        """A-weighting at 1 kHz should be ~0 dB (reference point)."""
        freqs = np.array([1000.0])
        gains = a_weighting(freqs)
        assert abs(gains[0]) < 0.1

    def test_low_freq_attenuated(self):
        """A-weighting strongly attenuates low frequencies."""
        freqs = np.array([100.0])
        gains = a_weighting(freqs)
        assert gains[0] < -15

    def test_high_freq_attenuated(self):
        """A-weighting attenuates very high frequencies."""
        freqs = np.array([10000.0])
        gains = a_weighting(freqs)
        assert gains[0] < 0

    def test_known_values(self):
        """Check against IEC 61672-1 reference values."""
        test_cases = [
            (31.5, -39.4),
            (63.0, -26.2),
            (125.0, -16.1),
            (250.0, -8.6),
            (500.0, -3.2),
            (1000.0, 0.0),
            (2000.0, 1.2),
            (4000.0, 1.0),
            (8000.0, -1.1),
        ]
        for freq, expected in test_cases:
            gains = a_weighting(np.array([freq]))
            assert abs(gains[0] - expected) < 0.5, (
                f"A-weight at {freq} Hz: got {gains[0]:.1f}, expected {expected}"
            )

    def test_zero_freq_returns_neg_inf(self):
        """DC component (0 Hz) should be -inf or very large negative."""
        freqs = np.array([0.0])
        gains = a_weighting(freqs)
        assert gains[0] < -100


class TestCWeighting:
    def test_1khz_is_zero(self):
        freqs = np.array([1000.0])
        gains = c_weighting(freqs)
        assert abs(gains[0]) < 0.1

    def test_flatter_than_a_at_low_freq(self):
        """C-weighting is flatter than A-weighting at low frequencies."""
        freqs = np.array([100.0])
        a_gain = a_weighting(freqs)[0]
        c_gain = c_weighting(freqs)[0]
        assert c_gain > a_gain

    def test_known_values(self):
        test_cases = [
            (31.5, -3.0),
            (63.0, -0.8),
            (125.0, -0.2),
            (250.0, 0.0),
            (500.0, 0.0),
            (1000.0, 0.0),
            (4000.0, -0.8),
            (8000.0, -3.0),
        ]
        for freq, expected in test_cases:
            gains = c_weighting(np.array([freq]))
            assert abs(gains[0] - expected) < 0.5, (
                f"C-weight at {freq} Hz: got {gains[0]:.1f}, expected {expected}"
            )


class TestComputeSpl:
    def test_sine_wave_z_weight(self):
        """A full-scale sine wave should give ~91 dB SPL (Z-weighted, ref 20 µPa)."""
        sr = 44100.0
        t = np.arange(int(sr * 1.0)) / sr
        sig = np.sin(2 * np.pi * 1000 * t)
        spl = compute_spl(sig, sr, weighting="Z", time_constant=0.125)
        assert isinstance(spl, float)
        assert np.isfinite(spl)

    def test_z_weighting_is_flat(self):
        """Z-weighted SPL should equal unweighted RMS-based SPL."""
        sr = 44100.0
        t = np.arange(int(sr * 0.5)) / sr
        sig = 0.5 * np.sin(2 * np.pi * 1000 * t)
        spl_z = compute_spl(sig, sr, weighting="Z", time_constant=0.125)
        rms = 0.5 / np.sqrt(2)
        expected = 20 * np.log10(rms / 20e-6)
        assert abs(spl_z - expected) < 1.0

    def test_a_weighting_attenuates_low_freq(self):
        """A-weighted SPL of a low-freq tone should be less than Z-weighted."""
        sr = 44100.0
        t = np.arange(int(sr * 1.0)) / sr
        sig = 0.5 * np.sin(2 * np.pi * 100 * t)
        spl_z = compute_spl(sig, sr, weighting="Z", time_constant=0.125)
        spl_a = compute_spl(sig, sr, weighting="A", time_constant=0.125)
        assert spl_a < spl_z - 10

    def test_silence_returns_very_low(self):
        """Silent signal should return a very low SPL."""
        sig = np.zeros(44100)
        spl = compute_spl(sig, 44100.0, weighting="Z", time_constant=0.125)
        assert spl < -50

    def test_compute_spl_leq_matches_rms_contract(self):
        sr = 44100.0
        t = np.arange(int(sr * 1.0)) / sr
        sig = 0.5 * np.sqrt(2.0) * np.sin(2 * np.pi * 1000 * t)

        spl = compute_spl_leq(sig, sr, weighting="Z")

        expected = 20 * np.log10(0.5 / 20e-6)
        assert spl == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("weighting", ["A", "C", "Z"])
def test_compute_spl_trace_is_stable_for_1khz_calibrator(weighting):
    signal, sample_rate = _stable_calibrator_signal()

    times_sec, values_db = compute_spl_trace(
        signal,
        sample_rate,
        weighting=weighting,
        time_constant=0.125,
        chunk_duration=4096 / sample_rate,
    )

    assert len(times_sec) >= 10
    stable_values = np.asarray(values_db[1:], dtype=np.float64)
    assert float(np.max(stable_values) - np.min(stable_values)) <= 0.10
    assert float(np.mean(stable_values)) == pytest.approx(94.0, abs=0.10)


def test_compute_spl_trace_does_not_widen_when_time_constant_increases():
    signal, sample_rate = _stable_calibrator_signal()

    spans = []
    for time_constant in (0.035, 0.125, 1.0):
        _times, values = compute_spl_trace(
            signal,
            sample_rate,
            weighting="Z",
            time_constant=time_constant,
            chunk_duration=4096 / sample_rate,
        )
        stable = np.asarray(values[1:], dtype=np.float64)
        spans.append(float(stable.max() - stable.min()))

    assert max(spans) <= 0.10


def test_compute_spl_trace_uses_one_continuous_detector(monkeypatch):
    instances = []

    class FakeDetector:
        def __init__(self, sample_rate, weighting, time_constant):
            self.calls = []
            instances.append(self)

        def process(self, signal):
            self.calls.append(np.asarray(signal).copy())
            return float(len(self.calls))

    monkeypatch.setattr("app.service.analysis.dsp.SplDetector", FakeDetector)

    signal = np.ones(1000, dtype=np.float64)
    _times, values = compute_spl_trace(
        signal,
        sample_rate=1000.0,
        weighting="Z",
        time_constant=0.125,
        chunk_duration=0.1,
    )

    assert len(instances) == 1
    assert len(instances[0].calls) == len(values)
    assert values == pytest.approx([float(i) for i in range(1, len(values) + 1)])


class TestLoudnessHelpers:
    def test_sone_to_phon_matches_piecewise_contract(self):
        assert sone_to_phon(1.0) == pytest.approx(40.0)

    def test_phon_to_sone_clamps_negative_inverse_to_zero(self):
        assert phon_to_sone(0.0) == pytest.approx(0.0)

    def test_compute_loudness_trace_matches_1khz_94db_reference(self):
        signal, sample_rate = _stable_calibrator_signal(sample_rate=48000.0)

        times_sec, values_sone, max_sone = compute_loudness_trace(signal, sample_rate)

        values = np.asarray(values_sone, dtype=np.float64)
        assert len(times_sec) == 12
        assert times_sec[0] == pytest.approx(4096 / 48000.0)
        assert times_sec[-1] == pytest.approx((132000 + 4096) / 48000.0)
        assert dsp_module.sone_to_phon(float(np.mean(values))) == pytest.approx(94.0, abs=0.75)
        assert dsp_module.sone_to_phon(max_sone) == pytest.approx(94.0, abs=0.75)
        assert dsp_module.sone_to_phon(float(np.mean(values))) < 95.0
        assert dsp_module.sone_to_phon(max_sone) < 95.0
        assert float(np.mean(values)) < 45.0
        assert max_sone < 45.5

    def test_compute_loudness_trace_handles_non_bin_centered_1khz_calibrator(self):
        signal, sample_rate = _calibrated_sine_signal(frequency_hz=998.6)

        _times_sec, values_sone, max_sone = compute_loudness_trace(signal, sample_rate)

        mean_sone = float(np.mean(np.asarray(values_sone, dtype=np.float64)))
        mean_phon = dsp_module.sone_to_phon(mean_sone)
        max_phon = dsp_module.sone_to_phon(max_sone)
        assert mean_phon == pytest.approx(94.0, abs=0.75)
        assert max_phon == pytest.approx(94.0, abs=0.75)
        assert mean_sone < 45.0
        assert max_sone < 45.5

    def test_compute_loudness_trace_limits_residual_inflation_for_calibrator(self):
        main, sample_rate = _calibrated_sine_signal(frequency_hz=998.6, spl_db=94.0)
        residual, _ = _calibrated_sine_signal(
            frequency_hz=100.0,
            spl_db=62.0,
            sample_rate=sample_rate,
        )
        signal = main + residual

        _times_sec, values_sone, max_sone = compute_loudness_trace(signal, sample_rate)

        mean_sone = float(np.mean(np.asarray(values_sone, dtype=np.float64)))
        mean_phon = dsp_module.sone_to_phon(mean_sone)
        max_phon = dsp_module.sone_to_phon(max_sone)
        assert mean_phon < 95.5
        assert max_phon < 95.8
        assert mean_sone < 47.0
        assert max_sone < 48.0

    def test_compute_loudness_trace_handles_non_native_sample_rate(self):
        sample_rate = 44100.0
        duration_sec = 1.0
        times = np.arange(int(sample_rate * duration_sec)) / sample_rate
        rms = 20e-6 * 10 ** (60.0 / 20.0)
        amplitude = rms * np.sqrt(2.0)
        signal = amplitude * np.sin(2 * np.pi * 1000.0 * times)

        loudness_times, values_sone, max_sone = compute_loudness_trace(signal, sample_rate)

        assert loudness_times
        assert values_sone
        assert max_sone >= 0.0
        assert max_sone == pytest.approx(max(values_sone))

    def test_compute_loudness_trace_resamples_high_sample_rate_before_backend_call(
        self,
        monkeypatch,
    ):
        signal = np.linspace(-1.0, 1.0, 200, dtype=np.float64)
        calls = {}

        def fake_resample(values, target_size):
            calls["resample"] = {
                "input_size": int(np.asarray(values).size),
                "target_size": int(target_size),
            }
            return np.full(target_size, 0.25, dtype=np.float64)

        monkeypatch.setattr(dsp_module.scipy_signal, "resample", fake_resample)
        _install_fake_frequency_backend(
            monkeypatch,
            calls,
            values=np.array([1.5], dtype=np.float64),
        )

        loudness_times, values_sone, max_sone = compute_loudness_trace(signal, 96000.0)

        assert calls["resample"] == {"input_size": 200, "target_size": 100}
        assert calls["backend"] == {
            "spectrum_shape": (4096, 1),
            "freqs_shape": (4096,),
            "field_type": "free",
        }
        assert loudness_times == [pytest.approx(50 / 48000.0)]
        assert values_sone == [1.5]
        assert max_sone == pytest.approx(1.5)


def _forbid_time_varying_loudness_backend(monkeypatch):
    def forbidden_backend_loader():
        raise AssertionError("ZWTv loudness backend must not be used")

    monkeypatch.setattr(dsp_module, "_load_loudness_backend", forbidden_backend_loader)


def _install_fake_frequency_backend(monkeypatch, calls, *, values):
    def fake_backend(spectrum, freqs, *, field_type="free"):
        calls["backend"] = {
            "spectrum_shape": tuple(np.asarray(spectrum).shape),
            "freqs_shape": tuple(np.asarray(freqs).shape),
            "field_type": field_type,
        }
        return values, None, None

    monkeypatch.setattr(dsp_module, "_load_loudness_frequency_backend", lambda: fake_backend, raising=False)
    _forbid_time_varying_loudness_backend(monkeypatch)


def test_compute_loudness_trace_uses_frequency_backend(monkeypatch):
    calls = {}

    def fake_backend(spectrum, freqs, *, field_type="free"):
        calls["backend"] = {
            "spectrum_shape": tuple(np.asarray(spectrum).shape),
            "freqs_shape": tuple(np.asarray(freqs).shape),
            "field_type": field_type,
        }
        return np.array([2.0, 3.0, 4.0]), None, None

    monkeypatch.setattr(dsp_module, "_load_loudness_frequency_backend", lambda: fake_backend, raising=False)
    _forbid_time_varying_loudness_backend(monkeypatch)
    signal = np.ones(int(48000 * 0.75), dtype=np.float64)

    times_sec, values_sone, max_sone = compute_loudness_trace(signal, 48000.0)

    assert times_sec == [
        pytest.approx(4096 / 48000.0),
        pytest.approx((12000 + 4096) / 48000.0),
        pytest.approx((24000 + 4096) / 48000.0),
    ]
    assert values_sone == [2.0, 3.0, 4.0]
    assert max_sone == pytest.approx(4.0)
    assert calls["backend"]["field_type"] == "free"
    assert calls["backend"]["spectrum_shape"][1] == 3


def test_compute_loudness_trace_passes_through_native_48k_without_resample(monkeypatch):
    signal = np.ones(int(48000 * 0.75), dtype=np.float64)
    calls = {}
    _install_fake_frequency_backend(
        monkeypatch,
        calls,
        values=np.array([1.0, 2.0, 3.0], dtype=np.float64),
    )

    def fake_resample(*args, **kwargs):
        calls["resample"] = True
        raise AssertionError("resample should not be called for native 48 kHz input")

    monkeypatch.setattr(dsp_module.scipy_signal, "resample", fake_resample)

    compute_loudness_trace(signal, 48000.0)

    assert "resample" not in calls
    assert calls["backend"]["spectrum_shape"] == (4096, 3)


def test_compute_loudness_trace_normalizes_non_native_sample_rate_before_backend_call(monkeypatch):
    signal = np.ones(int(96000 * 0.75), dtype=np.float64)
    calls = {}
    _install_fake_frequency_backend(
        monkeypatch,
        calls,
        values=np.array([1.0, 2.0, 3.0], dtype=np.float64),
    )

    def fake_resample(values, target_size):
        calls["resample"] = {
            "input_size": int(np.asarray(values).size),
            "target_size": int(target_size),
        }
        return np.full(target_size, 0.25, dtype=np.float64)

    monkeypatch.setattr(dsp_module.scipy_signal, "resample", fake_resample)

    compute_loudness_trace(signal, 96000.0)

    assert calls["resample"] == {"input_size": 72000, "target_size": 36000}
    assert calls["backend"]["spectrum_shape"] == (4096, 3)


def test_compute_loudness_trace_uses_8192_sample_windows_with_250ms_hop(monkeypatch):
    calls = {}

    def fake_backend(spectrum, freqs, *, field_type="free"):
        spectrum = np.asarray(spectrum)
        calls["backend"] = {
            "spectrum_shape": tuple(spectrum.shape),
            "freqs_shape": tuple(np.asarray(freqs).shape),
            "field_type": field_type,
        }
        return np.arange(1, spectrum.shape[1] + 1, dtype=np.float64), None, None

    monkeypatch.setattr(dsp_module, "_load_loudness_frequency_backend", lambda: fake_backend, raising=False)
    _forbid_time_varying_loudness_backend(monkeypatch)

    signal = np.ones(int(48000 * 1.1), dtype=np.float64)
    times_sec, values_sone, _max_sone = compute_loudness_trace(signal, 48000.0)

    assert times_sec == pytest.approx([
        4096 / 48000.0,
        (12000 + 4096) / 48000.0,
        (24000 + 4096) / 48000.0,
        (36000 + 4096) / 48000.0,
    ])
    assert values_sone == [1.0, 2.0, 3.0, 4.0]
    assert calls["backend"]["spectrum_shape"] == (4096, 4)


def test_compute_loudness_trace_rejects_non_finite_backend_output(monkeypatch):
    signal = np.ones(int(48000 * 0.75), dtype=np.float64)
    calls = {}
    _install_fake_frequency_backend(
        monkeypatch,
        calls,
        values=np.array([1.0, np.nan, 1.0], dtype=np.float64),
    )

    with pytest.raises(RuntimeError, match="non-finite output"):
        compute_loudness_trace(signal, 48000.0)


def test_compute_loudness_trace_pads_short_signal_to_one_frequency_window(monkeypatch):
    signal = np.ones(int(48000 * 0.1), dtype=np.float64)
    calls = {}
    _install_fake_frequency_backend(
        monkeypatch,
        calls,
        values=np.array([4.0], dtype=np.float64),
    )

    loudness_times, values_sone, max_sone = compute_loudness_trace(signal, 48000.0)

    assert calls["backend"]["spectrum_shape"] == (4096, 1)
    assert loudness_times == [pytest.approx(0.05)]
    assert values_sone == [4.0]
    assert max_sone == pytest.approx(4.0)


def test_compute_loudness_window_sone_matches_1khz_94db_reference():
    signal, sample_rate = _stable_calibrator_signal(sample_rate=48000.0, duration_sec=0.5)

    value_sone = dsp_module.compute_loudness_window_sone(signal, sample_rate)

    assert dsp_module.sone_to_phon(value_sone) == pytest.approx(94.0, abs=0.75)


def test_compute_loudness_window_sone_pads_short_signal(monkeypatch):
    signal = np.ones(int(48000 * 0.1), dtype=np.float64)
    calls = {}
    _install_fake_frequency_backend(
        monkeypatch,
        calls,
        values=np.array([7.0], dtype=np.float64),
    )

    value_sone = dsp_module.compute_loudness_window_sone(signal, 48000.0)

    assert calls["backend"]["spectrum_shape"] == (4096, 1)
    assert value_sone == pytest.approx(7.0)


def test_compute_loudness_trace_rejects_negative_backend_values(monkeypatch):
    def fake_backend(_spectrum, _freqs, *, field_type="free"):
        return np.array([1.0, -0.1, 1.0]), None, None

    monkeypatch.setattr(dsp_module, "_load_loudness_frequency_backend", lambda: fake_backend, raising=False)
    _forbid_time_varying_loudness_backend(monkeypatch)

    with pytest.raises(RuntimeError, match="non-negative"):
        compute_loudness_trace(np.ones(int(48000 * 0.75)), 48000.0)


def test_default_frequency_backend_matches_public_mosqito_for_supported_spectrum():
    from mosqito.sq_metrics import loudness_zwst_freq

    sample_rate = 48000.0
    signal, _ = _deterministic_multitone_signal(sample_rate=sample_rate, duration_sec=0.75)
    spectrum, freqs = _loudness_spectrum_for_signal(signal, sample_rate)

    optimized_values, optimized_specific, optimized_bark = dsp_module._compute_loudness_zwst_freq_cached(
        spectrum,
        freqs,
        field_type="free",
    )
    public_values, public_specific, public_bark = loudness_zwst_freq(
        spectrum,
        freqs,
        field_type="free",
    )

    np.testing.assert_allclose(optimized_values, public_values, rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(optimized_specific, public_specific, rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(optimized_bark, public_bark, rtol=0.0, atol=1e-12)


def test_cached_frequency_backend_matches_public_mosqito_for_one_window_1d_spectrum():
    from mosqito.sq_metrics import loudness_zwst_freq

    sample_rate = 48000.0
    signal, _ = _deterministic_multitone_signal(sample_rate=sample_rate, duration_sec=0.5)
    spectrum, freqs = _loudness_spectrum_for_signal(signal, sample_rate)
    spectrum_1d = spectrum[:, 0]

    optimized_values, optimized_specific, optimized_bark = dsp_module._compute_loudness_zwst_freq_cached(
        spectrum_1d,
        freqs,
        field_type="free",
    )
    public_values, public_specific, public_bark = loudness_zwst_freq(
        spectrum_1d,
        freqs,
        field_type="free",
    )

    assert float(optimized_values) == pytest.approx(float(public_values), abs=1e-12)
    np.testing.assert_allclose(optimized_specific, public_specific, rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(optimized_bark, public_bark, rtol=0.0, atol=1e-12)


def test_cached_frequency_backend_reuses_synthesis_weights(monkeypatch):
    sample_rate = 48000.0
    signal, _ = _deterministic_multitone_signal(sample_rate=sample_rate, duration_sec=0.75)
    spectrum, freqs = _loudness_spectrum_for_signal(signal, sample_rate)
    original_sosfreqz = dsp_module.scipy_signal.sosfreqz
    calls = {"sosfreqz": 0}

    def counting_sosfreqz(*args, **kwargs):
        calls["sosfreqz"] += 1
        return original_sosfreqz(*args, **kwargs)

    dsp_module._loudness_third_octave_synthesis_weights.cache_clear()
    monkeypatch.setattr(dsp_module.scipy_signal, "sosfreqz", counting_sosfreqz)

    dsp_module._compute_loudness_zwst_freq_cached(spectrum, freqs, field_type="free")
    first_count = calls["sosfreqz"]
    dsp_module._compute_loudness_zwst_freq_cached(spectrum, freqs, field_type="free")
    second_count = calls["sosfreqz"]

    assert first_count > 0
    assert second_count == first_count


def test_compute_loudness_trace_four_channel_five_second_performance():
    sample_rate = 48000.0
    signal, _ = _stable_calibrator_signal(sample_rate=sample_rate, duration_sec=5.0)
    channels = [signal, signal * 0.5, signal * 0.25, signal * 0.125]

    # Measure steady-state trace generation, not one-time MoSQITo backend/cache setup.
    compute_loudness_trace(channels[0], sample_rate)

    elapsed_runs = []
    traces = []
    for _ in range(3):
        start = time.perf_counter()
        traces = [compute_loudness_trace(channel, sample_rate) for channel in channels]
        elapsed_runs.append(time.perf_counter() - start)

    assert all(trace[1] for trace in traces)
    assert float(np.median(elapsed_runs)) < 0.75


def _stft_high_frequency_mean_db(mag_db, freqs, *, min_freq=5000.0):
    high = mag_db[np.asarray(freqs) > min_freq, :]
    return np.mean(high, axis=0)


class TestComputeStft:
    def test_output_shapes(self):
        """STFT output arrays should have correct shapes."""
        sr = 44100.0
        n = int(sr * 1.0)
        sig = np.random.randn(n)
        nfft = 1024
        overlap = 0.5
        times, freqs, mag_db = compute_stft(sig, sr, nfft, overlap)
        assert freqs.shape[0] == nfft // 2 + 1
        assert times.ndim == 1
        assert mag_db.shape == (freqs.shape[0], times.shape[0])

    def test_frequency_axis(self):
        """Frequency axis should range from 0 to Nyquist."""
        sr = 44100.0
        sig = np.zeros(int(sr * 0.5))
        times, freqs, mag_db = compute_stft(sig, sr, 1024, 0.5)
        assert freqs[0] == pytest.approx(0.0)
        assert freqs[-1] == pytest.approx(sr / 2)

    def test_sine_peak(self):
        """A 1 kHz sine should produce a peak near 1 kHz in the STFT."""
        sr = 44100.0
        t = np.arange(int(sr * 0.5)) / sr
        sig = np.sin(2 * np.pi * 1000 * t)
        times, freqs, mag_db = compute_stft(sig, sr, 1024, 0.5)
        avg_mag = mag_db.mean(axis=1)
        peak_freq = freqs[np.argmax(avg_mag)]
        assert abs(peak_freq - 1000.0) < 50

    def test_compute_stft_does_not_zero_pad_streaming_boundaries_for_stable_tone(self):
        sample_rate = 44100.0
        nfft = 1024
        overlap = 0.5
        signal, _ = _stable_calibrator_signal(
            sample_rate=sample_rate,
            duration_sec=0.25,
        )

        times, freqs, mag_db = compute_stft(signal, sample_rate, nfft, overlap)

        high_frequency_mean = _stft_high_frequency_mean_db(mag_db, freqs)
        tone_bin = int(np.argmin(np.abs(freqs - 1000.0)))
        tone_peak = np.max(mag_db[tone_bin, :])
        assert times.size > 0
        assert float(np.max(high_frequency_mean)) < float(tone_peak) - 80.0

    def test_short_signal_does_not_crash(self):
        """Signal shorter than nfft should not raise."""
        sig = np.random.randn(128)
        times, freqs, mag_db = compute_stft(sig, 44100.0, 1024, 0.5)
        assert times.ndim == 1
        assert freqs.ndim == 1


class TestComputeFftSpectrum:
    def test_output_shapes(self):
        sig = np.random.randn(4096)
        freqs, mag_db = compute_fft_spectrum(sig, 44100.0, 4096)
        assert freqs.shape[0] == 4096 // 2 + 1
        assert mag_db.shape == freqs.shape

    @pytest.mark.parametrize(
        ("signal", "sample_rate", "nfft"),
        [
            (np.array([], dtype=np.float64), 44100.0, 4096),
            (np.ones(8, dtype=np.float64), 0.0, 4096),
            (np.ones(8, dtype=np.float64), float("nan"), 4096),
            (np.ones(8, dtype=np.float64), 44100.0, 1024.5),
            (np.ones(8, dtype=np.float64), 44100.0, 0),
        ],
    )
    def test_invalid_inputs_raise_value_error(self, signal, sample_rate, nfft):
        with pytest.raises(ValueError):
            compute_fft_spectrum(signal, sample_rate, nfft)

    def test_short_signal_zero_pads_to_nfft(self, monkeypatch):
        captured_windows: list[np.ndarray] = []

        monkeypatch.setattr(dsp_module.np, "hanning", lambda n: np.ones(n, dtype=np.float64))

        def fake_rfft(values):
            captured_windows.append(np.asarray(values, dtype=np.float64).copy())
            return np.array([1.0 + 0j, 2.0 + 0j, 3.0 + 0j], dtype=np.complex128)

        monkeypatch.setattr(dsp_module.np.fft, "rfft", fake_rfft)

        compute_fft_spectrum(np.array([1.0, 2.0], dtype=np.float64), 1.0, 4)

        assert [window.tolist() for window in captured_windows] == [
            [1.0, 2.0, 0.0, 0.0],
        ]

    def test_sine_peak(self):
        sr = 44100.0
        t = np.arange(4096) / sr
        sig = np.sin(2 * np.pi * 1000 * t)
        freqs, mag_db = compute_fft_spectrum(sig, sr, 4096)
        peak_freq = freqs[np.argmax(mag_db)]
        assert abs(peak_freq - 1000.0) < 20

    def test_long_signal_uses_full_signal_with_bounded_axis(self):
        sr = 48_000.0
        nfft = 4096
        lead_bin = 17
        tail_bin = 171
        lead_freq = lead_bin * sr / nfft
        tail_freq = tail_bin * sr / nfft
        t = np.arange(nfft, dtype=np.float64) / sr
        lead = np.sin(2 * np.pi * lead_freq * t)
        tail = np.sin(2 * np.pi * tail_freq * t)

        freqs, mag_db = compute_fft_spectrum(
            np.concatenate([lead, tail]),
            sr,
            nfft,
        )

        assert len(freqs) == nfft // 2 + 1
        assert mag_db[lead_bin] > -20.0
        assert mag_db[tail_bin] > -20.0

    def test_long_signal_appends_tail_window_and_averages_linear_magnitudes(self, monkeypatch):
        captured_windows: list[np.ndarray] = []

        monkeypatch.setattr(dsp_module.np, "hanning", lambda n: np.ones(n, dtype=np.float64))

        def fake_rfft(values):
            window = np.asarray(values, dtype=np.float64).copy()
            captured_windows.append(window)
            if float(window[0]) == 0.0:
                return np.array([1.0 + 0j, 2.0 + 0j, 3.0 + 0j], dtype=np.complex128)
            return np.array([5.0 + 0j, 7.0 + 0j, 11.0 + 0j], dtype=np.complex128)

        monkeypatch.setattr(dsp_module.np.fft, "rfft", fake_rfft)

        _, mag_db = compute_fft_spectrum(np.arange(5, dtype=np.float64), 1.0, 4)

        assert [window.tolist() for window in captured_windows] == [
            [0.0, 1.0, 2.0, 3.0],
            [1.0, 2.0, 3.0, 4.0],
        ]
        np.testing.assert_allclose(
            10 ** (mag_db / 20.0),
            np.array([1.5, 2.25, 3.5], dtype=np.float64),
        )

    def test_long_signal_deduplicates_tail_window_when_last_hop_matches_tail(self, monkeypatch):
        captured_windows: list[np.ndarray] = []

        monkeypatch.setattr(dsp_module.np, "hanning", lambda n: np.ones(n, dtype=np.float64))
        monkeypatch.setattr(
            dsp_module.np.fft,
            "rfft",
            lambda values: (
                captured_windows.append(np.asarray(values, dtype=np.float64).copy())
                or np.array([1.0 + 0j, 2.0 + 0j, 3.0 + 0j], dtype=np.complex128)
            ),
        )

        compute_fft_spectrum(np.arange(6, dtype=np.float64), 1.0, 4)

        assert [window.tolist() for window in captured_windows] == [
            [0.0, 1.0, 2.0, 3.0],
            [2.0, 3.0, 4.0, 5.0],
        ]

    def test_frequency_range(self):
        freqs, _ = compute_fft_spectrum(np.zeros(1024), 44100.0, 1024)
        assert freqs[0] == pytest.approx(0.0)
        assert freqs[-1] == pytest.approx(44100.0 / 2)


class TestComputeOctaveBands:
    def test_output_shapes(self):
        sr = 44100.0
        sig = np.random.randn(int(sr * 0.5))
        centers, levels = compute_octave_bands(sig, sr, weighting="Z")
        assert centers.ndim == 1
        assert levels.shape == centers.shape
        assert len(centers) > 0
        assert centers[-1] <= sr / 2

    def test_z_weighting_white_noise_rises_with_bandwidth(self):
        """A filter bank should report higher broadband levels for wider bands."""
        sr = 44100.0
        rng = np.random.default_rng(42)
        sig = rng.standard_normal(int(sr * 5.0))
        centers, levels = compute_octave_bands(sig, sr, weighting="Z")

        level_100 = _level_for_center(centers, levels, 100.0)
        level_1000 = _level_for_center(centers, levels, 1000.0)
        level_5000 = _level_for_center(centers, levels, 5000.0)

        assert level_1000 > level_100 + 6.0
        assert level_5000 > level_1000 + 4.0

    def test_a_weighting_applied(self):
        """A-weighted octave levels at low freq should be less than Z-weighted."""
        sr = 44100.0
        rng = np.random.default_rng(42)
        sig = rng.standard_normal(int(sr * 1.0))
        _, levels_z = compute_octave_bands(sig, sr, weighting="Z")
        _, levels_a = compute_octave_bands(sig, sr, weighting="A")
        assert levels_a[0] < levels_z[0] - 5

    def test_center_freqs_are_standard(self):
        """Center frequencies should follow IEC 61260 standard values."""
        assert 1000.0 in OCTAVE_CENTER_FREQS
        assert 31.5 in OCTAVE_CENTER_FREQS

    def test_empty_signal_returns_empty_arrays(self):
        """Empty input should return empty octave band outputs instead of failing."""
        centers, levels = compute_octave_bands(np.array([]), 44100.0, weighting="Z")
        assert centers.size == 0
        assert levels.size == 0

    @pytest.mark.parametrize("n_samples", [8, 16])
    def test_short_non_empty_signal_does_not_crash(self, n_samples):
        """Very short non-empty inputs should return octave levels without pad errors."""
        sample_rate = 48000.0
        rng = np.random.default_rng(123)
        sig = rng.standard_normal(n_samples)

        try:
            centers, levels = compute_octave_bands(sig, sample_rate, weighting="Z")
        except ValueError as exc:
            pytest.fail(f"compute_octave_bands raised for n={n_samples}: {exc}")

        factor = 2.0 ** (1.0 / 6.0)
        expected_centers = OCTAVE_CENTER_FREQS[OCTAVE_CENTER_FREQS * factor < sample_rate / 2.0]
        np.testing.assert_array_equal(centers, expected_centers)
        assert levels.shape == centers.shape
        assert np.all(np.isfinite(levels) | np.isneginf(levels))

    def test_reuses_filter_bank_for_same_sample_rate(self, monkeypatch):
        """Repeated calls at the same sample rate should reuse precomputed SOS filters."""
        original_butter = dsp_module.scipy_signal.butter
        butter_calls = 0

        def counting_butter(*args, **kwargs):
            nonlocal butter_calls
            butter_calls += 1
            return original_butter(*args, **kwargs)

        monkeypatch.setattr(dsp_module.scipy_signal, "butter", counting_butter)

        sample_rate_a = 47987.0
        sample_rate_b = 32001.0
        sig = np.random.default_rng(456).standard_normal(2400)

        centers_a, _ = compute_octave_bands(sig, sample_rate_a, weighting="Z")
        compute_octave_bands(sig, sample_rate_a, weighting="A")
        centers_b, _ = compute_octave_bands(sig, sample_rate_b, weighting="Z")

        assert butter_calls == len(centers_a) + len(centers_b)


class TestApplyMicCalibration:
    def test_scales_signal(self):
        sig = np.array([1.0, 2.0, -1.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=0.5)
        np.testing.assert_array_almost_equal(result, [0.5, 1.0, -0.5])

    def test_zero_sensitivity_returns_raw(self):
        sig = np.array([1.0, 2.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=0.0)
        np.testing.assert_array_equal(result, sig)

    def test_none_sensitivity_returns_raw(self):
        sig = np.array([1.0, 2.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=None)
        np.testing.assert_array_equal(result, sig)

    def test_unity_sensitivity(self):
        sig = np.array([3.0, -3.0])
        result = apply_mic_calibration(sig, sensitivity_pa_per_v=1.0)
        np.testing.assert_array_equal(result, sig)
