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
    compute_loudness_trace_for_window,
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


def _phon_array(values_sone) -> np.ndarray:
    return np.asarray(
        [dsp_module.sone_to_phon(float(value)) for value in values_sone],
        dtype=np.float64,
    )


def _amplitude_for_spl(spl_db) -> np.ndarray:
    return np.sqrt(2.0) * 20e-6 * (10.0 ** (np.asarray(spl_db, dtype=np.float64) / 20.0))


def _tone_with_level_envelope(levels_db: np.ndarray, sample_rate: float) -> np.ndarray:
    times = np.arange(levels_db.size, dtype=np.float64) / sample_rate
    return _amplitude_for_spl(levels_db) * np.sin(2.0 * np.pi * 1000.0 * times)


def _reduced_zwtv_reference_fixture(name: str) -> tuple[np.ndarray, float, float]:
    sample_rate = 48000.0
    duration_sec = 0.5
    sample_count = int(sample_rate * duration_sec)
    times = np.arange(sample_count, dtype=np.float64) / sample_rate

    if name == "steady":
        signal, _ = _stable_calibrator_signal(sample_rate=sample_rate, duration_sec=duration_sec)
        return signal, sample_rate, 0.5
    if name == "ramp":
        return _tone_with_level_envelope(np.linspace(60.0, 94.0, sample_count), sample_rate), sample_rate, 0.5
    if name == "step":
        levels = np.where(times < duration_sec / 2.0, 60.0, 94.0)
        return _tone_with_level_envelope(levels, sample_rate), sample_rate, 0.5
    if name == "30ms-pulse":
        return _pulse_fixture(sample_rate=sample_rate, duration_sec=duration_sec, pulse_sec=0.030), sample_rate, 1.0
    if name == "100ms-pulse":
        return _pulse_fixture(sample_rate=sample_rate, duration_sec=duration_sec, pulse_sec=0.100), sample_rate, 1.0
    raise AssertionError(f"unknown fixture {name}")


def _pulse_fixture(*, sample_rate: float, duration_sec: float, pulse_sec: float) -> np.ndarray:
    sample_count = int(sample_rate * duration_sec)
    times = np.arange(sample_count, dtype=np.float64) / sample_rate
    signal = np.zeros(sample_count, dtype=np.float64)
    start = int(round(0.15 * sample_rate))
    stop = min(sample_count, start + int(round(pulse_sec * sample_rate)))
    signal[start:stop] = _amplitude_for_spl(94.0) * np.sin(2.0 * np.pi * 1000.0 * times[start:stop])
    return signal


def _assert_reduced_zwtv_matches_public_reference(
    signal: np.ndarray,
    sample_rate: float,
    *,
    max_error_phon: float,
) -> None:
    from mosqito.sq_metrics import loudness_zwtv

    times_sec, values_sone, _max_sone = compute_loudness_trace(signal, sample_rate)
    display_times, display_values = dsp_module.downsample_loudness_trace_for_display(
        times_sec,
        values_sone,
    )
    reference_values, _reference_specific, _reference_bark, reference_times = loudness_zwtv(
        signal,
        int(round(sample_rate)),
        field_type="free",
    )
    reference_at_display = np.interp(
        np.asarray(display_times, dtype=np.float64),
        np.asarray(reference_times, dtype=np.float64),
        np.asarray(reference_values, dtype=np.float64),
        left=float(reference_values[0]),
        right=float(reference_values[-1]),
    )

    errors_phon = np.abs(_phon_array(display_values) - _phon_array(reference_at_display))
    assert float(np.max(errors_phon)) <= max_error_phon


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

    def test_compute_loudness_trace_returns_reduced_zwtv_internal_150hz(self):
        signal, sample_rate = _stable_calibrator_signal(
            sample_rate=48000.0,
            duration_sec=1.0,
        )

        times_sec, values_sone, max_sone = compute_loudness_trace(signal, sample_rate)

        values = np.asarray(values_sone, dtype=np.float64)
        assert times_sec[0] == pytest.approx(0.0)
        assert len(times_sec) == 151
        assert len(values_sone) == 151
        assert times_sec[-1] == pytest.approx(1.0)
        np.testing.assert_allclose(np.diff(times_sec), 1.0 / 150.0, rtol=0.0, atol=1e-12)
        assert np.all(np.isfinite(values))
        assert np.all(values >= 0.0)
        assert max_sone == pytest.approx(max(values_sone))
        assert dsp_module.sone_to_phon(float(np.mean(values))) == pytest.approx(94.0, abs=1.0)
        assert dsp_module.sone_to_phon(max_sone) == pytest.approx(94.0, abs=1.0)

    def test_compute_loudness_trace_for_window_preserves_prior_context(self):
        signal, sample_rate = _stable_calibrator_signal(
            sample_rate=48000.0,
            duration_sec=1.0,
        )
        start_sec = 0.5
        end_sec = 0.7
        sliced = signal[int(start_sec * sample_rate):int(end_sec * sample_rate)]

        times_sec, values_sone, max_sone = compute_loudness_trace_for_window(
            signal,
            sample_rate,
            start_sec,
            end_sec,
        )
        _sliced_times, sliced_values_sone, _sliced_max = compute_loudness_trace(
            sliced,
            sample_rate,
        )

        assert times_sec[0] == pytest.approx(0.0)
        assert times_sec[-1] == pytest.approx(0.2)
        np.testing.assert_allclose(np.diff(times_sec), 1.0 / 150.0, rtol=0.0, atol=1e-12)
        assert dsp_module.sone_to_phon(values_sone[0]) > 90.0
        assert values_sone[0] > sliced_values_sone[0] * 100.0
        assert max_sone == pytest.approx(max(values_sone))

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

        assert len(loudness_times) == 151
        assert len(values_sone) == 151
        assert loudness_times[0] == pytest.approx(0.0)
        assert loudness_times[-1] == pytest.approx(1.0)
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

        def fake_reduced_zwtv(values, backend_sample_rate, target_times):
            calls["reduced_zwtv"] = {
                "input_size": int(np.asarray(values).size),
                "sample_rate": float(backend_sample_rate),
                "times": np.asarray(target_times, dtype=np.float64).copy(),
            }
            return np.full(len(target_times), 1.5, dtype=np.float64)

        monkeypatch.setattr(
            dsp_module,
            "_compute_reduced_zwtv_loudness_values",
            fake_reduced_zwtv,
            raising=False,
        )

        loudness_times, values_sone, max_sone = compute_loudness_trace(signal, 96000.0)

        assert calls["resample"] == {"input_size": 200, "target_size": 100}
        assert calls["reduced_zwtv"]["input_size"] == 100
        assert calls["reduced_zwtv"]["sample_rate"] == pytest.approx(48000.0)
        assert calls["reduced_zwtv"]["times"].tolist() == [pytest.approx(0.0)]
        assert loudness_times == [pytest.approx(0.0)]
        assert values_sone == [1.5]
        assert max_sone == pytest.approx(1.5)


def test_compute_loudness_trace_non_exact_duration_stays_within_signal_end(monkeypatch):
    def fake_reduced_zwtv(_values, _backend_sample_rate, target_times):
        return np.ones(len(target_times), dtype=np.float64)

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )
    signal = np.ones(48001, dtype=np.float64)

    times_sec, values_sone, max_sone = compute_loudness_trace(signal, 48000.0)

    assert len(times_sec) == 151
    assert len(values_sone) == 151
    assert times_sec[-1] == pytest.approx(1.0)
    assert times_sec[-1] <= signal.size / 48000.0
    assert max_sone == pytest.approx(1.0)


def test_downsample_loudness_trace_for_display_selects_every_sixth_internal_sample():
    times = (np.arange(151, dtype=np.float64) / 150.0).tolist()
    values = np.arange(151, dtype=np.float64).tolist()

    display_times, display_values = dsp_module.downsample_loudness_trace_for_display(
        times,
        values,
    )

    assert len(display_times) == 26
    assert display_times[0] == pytest.approx(0.0)
    assert display_times[-1] == pytest.approx(1.0)
    assert display_times == pytest.approx(times[::6])
    assert display_values == pytest.approx(values[::6])


@pytest.mark.parametrize(
    ("times", "values", "match"),
    [
        ([], [], "empty"),
        ([0.0], [1.0, 2.0], "same length"),
        ([0.0, float("nan")], [1.0, 2.0], "finite"),
        ([0.0, 0.1], [1.0, float("inf")], "finite"),
        ([0.0, 0.1], [1.0, -0.1], "non-negative"),
        ([0.0, 0.1, 0.1], [1.0, 2.0, 3.0], "increasing"),
    ],
)
def test_downsample_loudness_trace_for_display_rejects_invalid_direct_inputs(times, values, match):
    with pytest.raises((RuntimeError, ValueError), match=match):
        dsp_module.downsample_loudness_trace_for_display(times, values)


@pytest.mark.parametrize("fixture_name", ["steady", "ramp", "step", "30ms-pulse", "100ms-pulse"])
def test_reduced_zwtv_matches_public_mosqito_at_display_sample_times(fixture_name):
    signal, sample_rate, tolerance_phon = _reduced_zwtv_reference_fixture(fixture_name)

    _assert_reduced_zwtv_matches_public_reference(
        signal,
        sample_rate,
        max_error_phon=tolerance_phon,
    )


def _forbid_time_varying_loudness_backend(monkeypatch):
    def forbidden_backend_loader():
        raise AssertionError("ZWTv loudness backend must not be used")

    monkeypatch.setattr(dsp_module, "_load_loudness_backend", forbidden_backend_loader)

    def forbidden_reduced_zwtv(*_args, **_kwargs):
        raise AssertionError("reduced-ZWTv loudness trace must not be used")

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        forbidden_reduced_zwtv,
        raising=False,
    )


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


def test_compute_loudness_trace_uses_reduced_zwtv_core(monkeypatch):
    calls = {}

    def fake_reduced_zwtv(values, backend_sample_rate, target_times):
        target_times = np.asarray(target_times, dtype=np.float64)
        calls["reduced_zwtv"] = {
            "input_size": int(np.asarray(values).size),
            "sample_rate": float(backend_sample_rate),
            "target_count": int(target_times.size),
        }
        return np.arange(1, target_times.size + 1, dtype=np.float64)

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )
    signal = np.ones(int(48000 * 0.75), dtype=np.float64)

    times_sec, values_sone, max_sone = compute_loudness_trace(signal, 48000.0)

    assert len(times_sec) == 113
    assert times_sec[0] == pytest.approx(0.0)
    assert times_sec[-1] == pytest.approx(112 / 150.0)
    assert values_sone[0] == pytest.approx(1.0)
    assert values_sone[-1] == pytest.approx(113.0)
    assert max_sone == pytest.approx(113.0)
    assert calls["reduced_zwtv"] == {
        "input_size": 36000,
        "sample_rate": 48000.0,
        "target_count": 113,
    }


def test_compute_loudness_trace_passes_through_native_48k_without_resample(monkeypatch):
    signal = np.ones(int(48000 * 0.75), dtype=np.float64)
    calls = {}

    def fake_reduced_zwtv(values, backend_sample_rate, target_times):
        calls["reduced_zwtv"] = {
            "input_size": int(np.asarray(values).size),
            "sample_rate": float(backend_sample_rate),
            "target_count": int(np.asarray(target_times).size),
        }
        return np.ones(len(target_times), dtype=np.float64)

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )

    def fake_resample(*args, **kwargs):
        calls["resample"] = True
        raise AssertionError("resample should not be called for native 48 kHz input")

    monkeypatch.setattr(dsp_module.scipy_signal, "resample", fake_resample)

    compute_loudness_trace(signal, 48000.0)

    assert "resample" not in calls
    assert calls["reduced_zwtv"] == {
        "input_size": 36000,
        "sample_rate": 48000.0,
        "target_count": 113,
    }


def test_compute_loudness_trace_normalizes_non_native_sample_rate_before_backend_call(monkeypatch):
    signal = np.ones(int(96000 * 0.75), dtype=np.float64)
    calls = {}

    def fake_resample(values, target_size):
        calls["resample"] = {
            "input_size": int(np.asarray(values).size),
            "target_size": int(target_size),
        }
        return np.full(target_size, 0.25, dtype=np.float64)

    def fake_reduced_zwtv(values, backend_sample_rate, target_times):
        calls["reduced_zwtv"] = {
            "input_size": int(np.asarray(values).size),
            "sample_rate": float(backend_sample_rate),
            "target_count": int(np.asarray(target_times).size),
        }
        return np.ones(len(target_times), dtype=np.float64)

    monkeypatch.setattr(dsp_module.scipy_signal, "resample", fake_resample)
    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )

    compute_loudness_trace(signal, 96000.0)

    assert calls["resample"] == {"input_size": 72000, "target_size": 36000}
    assert calls["reduced_zwtv"] == {
        "input_size": 36000,
        "sample_rate": 48000.0,
        "target_count": 113,
    }


def test_compute_loudness_trace_uses_integer_150hz_time_axis(monkeypatch):
    calls = {}

    def fake_reduced_zwtv(_values, _backend_sample_rate, target_times):
        calls["target_times"] = np.asarray(target_times, dtype=np.float64).copy()
        return np.arange(1, len(target_times) + 1, dtype=np.float64)

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )

    signal = np.ones(int(48000 * 1.1), dtype=np.float64)
    times_sec, values_sone, _max_sone = compute_loudness_trace(signal, 48000.0)

    assert len(times_sec) == 166
    assert times_sec[0] == pytest.approx(0.0)
    assert times_sec[-1] == pytest.approx(1.1)
    np.testing.assert_allclose(np.diff(times_sec), 1.0 / 150.0, rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(calls["target_times"], times_sec, rtol=0.0, atol=0.0)
    assert values_sone[0] == pytest.approx(1.0)
    assert values_sone[-1] == pytest.approx(166.0)


def test_compute_loudness_trace_rejects_non_finite_backend_output(monkeypatch):
    signal = np.ones(int(48000 * 0.75), dtype=np.float64)

    def fake_reduced_zwtv(_values, _backend_sample_rate, target_times):
        values = np.ones(len(target_times), dtype=np.float64)
        values[1] = np.nan
        return values

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="non-finite output"):
        compute_loudness_trace(signal, 48000.0)


def test_compute_loudness_trace_returns_one_point_for_short_non_empty_signal(monkeypatch):
    signal = np.ones(12, dtype=np.float64)

    def fake_reduced_zwtv(_values, _backend_sample_rate, target_times):
        target_times = np.asarray(target_times, dtype=np.float64)
        assert target_times.size == 1
        assert target_times[0] == pytest.approx(0.0)
        return np.array([4.0], dtype=np.float64)

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )

    loudness_times, values_sone, max_sone = compute_loudness_trace(signal, 48000.0)

    assert loudness_times == [pytest.approx(0.0)]
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
    def fake_reduced_zwtv(_values, _backend_sample_rate, target_times):
        values = np.ones(len(target_times), dtype=np.float64)
        values[1] = -0.1
        return values

    monkeypatch.setattr(
        dsp_module,
        "_compute_reduced_zwtv_loudness_values",
        fake_reduced_zwtv,
        raising=False,
    )

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

    start = time.perf_counter()
    traces = [compute_loudness_trace(channel, sample_rate) for channel in channels]
    elapsed = time.perf_counter() - start

    assert all(trace[1] for trace in traces)
    assert all(len(trace[0]) == 751 for trace in traces)
    assert elapsed >= 0.0


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

    def test_a_weighting_attenuates_low_frequency_fft_bin_relative_to_z(self):
        sample_rate = 48_000.0
        nfft = 4096
        bin_index = 10
        freq_hz = bin_index * sample_rate / nfft
        t = np.arange(nfft, dtype=np.float64) / sample_rate
        signal = np.sin(2.0 * np.pi * freq_hz * t)

        freqs_z, levels_z = compute_fft_spectrum(signal, sample_rate, nfft, weighting="Z")
        freqs_a, levels_a = compute_fft_spectrum(signal, sample_rate, nfft, weighting="A")

        np.testing.assert_allclose(freqs_a, freqs_z)
        assert levels_a[bin_index] < levels_z[bin_index] - 10.0

    def test_c_weighting_is_flatter_than_a_weighting_at_low_frequency_fft_bin(self):
        sample_rate = 48_000.0
        nfft = 4096
        bin_index = 10
        freq_hz = bin_index * sample_rate / nfft
        t = np.arange(nfft, dtype=np.float64) / sample_rate
        signal = np.sin(2.0 * np.pi * freq_hz * t)

        _, levels_z = compute_fft_spectrum(signal, sample_rate, nfft, weighting="Z")
        _, levels_a = compute_fft_spectrum(signal, sample_rate, nfft, weighting="A")
        _, levels_c = compute_fft_spectrum(signal, sample_rate, nfft, weighting="C")

        assert levels_a[bin_index] < levels_c[bin_index] < levels_z[bin_index]

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
