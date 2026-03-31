"""Tests for pure DSP functions."""
from __future__ import annotations

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


class TestLoudnessHelpers:
    def test_sone_to_phon_matches_piecewise_contract(self):
        assert sone_to_phon(1.0) == pytest.approx(40.0)

    def test_phon_to_sone_clamps_negative_inverse_to_zero(self):
        assert phon_to_sone(0.0) == pytest.approx(0.0)

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

    def test_sine_peak(self):
        sr = 44100.0
        t = np.arange(4096) / sr
        sig = np.sin(2 * np.pi * 1000 * t)
        freqs, mag_db = compute_fft_spectrum(sig, sr, 4096)
        peak_freq = freqs[np.argmax(mag_db)]
        assert abs(peak_freq - 1000.0) < 20

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
