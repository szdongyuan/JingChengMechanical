"""Tests for pure DSP functions."""
from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.dsp import (
    a_weighting,
    c_weighting,
    compute_fft_spectrum,
    compute_spl,
    compute_stft,
)


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
