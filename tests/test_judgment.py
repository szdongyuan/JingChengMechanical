"""Tests for JudgmentEngine limit checks and verdict logic."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
from unittest.mock import MagicMock
from scipy.io import wavfile

from app.service.analysis import dsp
from app.service.analysis import golden_reference as golden_reference_module
from app.service.analysis.judgment import JudgmentEngine
from app.service.analysis.frames import JudgmentResult, ChannelJudgment, BandResult
from app.service.golden_sample_service import GoldenSampleService


_REF_DB = 20.0 * np.log10(20e-6)


@pytest.fixture(autouse=True)
def _stub_loudness_trace(monkeypatch):
    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace",
        lambda *args, **kwargs: ([0.0, 0.5], [2.0, 4.0], 4.0),
    )


def _make_config(channels=None, **overrides) -> dict:
    """Minimal config dict for judgment testing."""
    cfg = {
        "config_id": 1,
        "config_name": "Test",
        "input_device_samplerate": 44_100.0,
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "analysis_window_offset_ms": 0,
        "selected_channels": [0],
        "channels": channels or [_make_channel_config(0)],
    }
    cfg.update(overrides)
    return cfg


def _make_channel_config(idx, **overrides) -> dict:
    cfg = {
        "channel_index": idx,
        "stft_nfft": 1024,
        "stft_overlap": 0.5,
        "spl_weighting": "Z",
        "spl_time_constant": 0.125,
        "spl_upper_limit": 85.0,
        "spl_lower_limit": 40.0,
        "loudness_unit": "sone",
        "loudness_lower_limit": 0.0,
        "loudness_upper_limit": 60.0,
        "fft_band_limits": [],
        "golden_sample_path": None,
        "golden_sample_label": None,
        "golden_sample_source_channel": None,
        "golden_sample_start_sample": None,
        "golden_sample_num_samples": None,
        "golden_sample_start_ms": None,
        "golden_sample_duration_ms": None,
        "golden_sample_sample_rate": None,
        "fft_golden_mode": "off",
        "fft_show_golden_overlay": False,
        "oct_weighting": "Z",
        "oct_band_limits": [],
        "oct_golden_mode": "off",
        "oct_show_golden_overlay": False,
    }
    cfg.update(overrides)
    return cfg


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    samples: int = 144_000,
    fill_value: float = 0.5,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, sample_rate, np.full(samples, fill_value, dtype=np.float32))
    return path


def _golden_binding(path: Path | str, *, sample_rate: float = 48_000.0) -> dict:
    return {
        "golden_sample_path": str(path),
        "golden_sample_label": "gold.wav",
        "golden_sample_source_channel": 0,
        "golden_sample_start_sample": 0,
        "golden_sample_num_samples": 144_000,
        "golden_sample_start_ms": 0,
        "golden_sample_duration_ms": 3000,
        "golden_sample_sample_rate": sample_rate,
    }


class TestLimitChecks:
    def _make_engine(self, *, golden_sample_service=None):
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None  # no AI configured
        return JudgmentEngine(
            mock_ai,
            golden_sample_service=golden_sample_service,
        )

    def test_spl_within_limits_passes(self):
        engine = self._make_engine()
        # Generate a signal that produces SPL within [40, 85] dB
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        # ~60 dB SPL signal (Z-weighted)
        amplitude = 20e-6 * 10 ** (60 / 20)  # 0.02 Pa
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)  # single channel
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert isinstance(result, JudgmentResult)
        assert len(result.channel_results) == 1
        assert result.channel_results[0].spl_pass is True

    def test_judgment_uses_loudness_max_sone_for_pass_fail(self, monkeypatch):
        engine = self._make_engine()
        sample_rate = 44100.0
        buffer = np.zeros((int(sample_rate * 3.0), 1))
        config = _make_config(
            channels=[
                _make_channel_config(
                    0,
                    loudness_unit="sone",
                    loudness_lower_limit=5.0,
                    loudness_upper_limit=10.0,
                )
            ]
        )
        monkeypatch.setattr(
            dsp,
            "compute_loudness_trace",
            lambda *args, **kwargs: ([0.0, 1.0], [5.0, 8.0], 8.0),
        )

        result = engine.judge(config, buffer, sample_rate, mic_cal={}, audio_file_path="/tmp/test.wav")

        assert result.channel_results[0].loudness_max_sone == 8.0
        assert result.channel_results[0].loudness_pass is True

    def test_judgment_uses_phon_limits_via_conversion_helpers(self, monkeypatch):
        engine = self._make_engine()
        sample_rate = 44100.0
        buffer = np.zeros((int(sample_rate * 3.0), 1))
        config = _make_config(
            channels=[
                _make_channel_config(
                    0,
                    loudness_unit="phon",
                    loudness_lower_limit=65.0,
                    loudness_upper_limit=75.0,
                )
            ]
        )
        monkeypatch.setattr(
            dsp,
            "compute_loudness_trace",
            lambda *args, **kwargs: ([0.0, 1.0], [4.0, 8.0], 8.0),
        )

        result = engine.judge(config, buffer, sample_rate, mic_cal={}, audio_file_path="/tmp/test.wav")

        assert result.channel_results[0].loudness_pass is True

    def test_judgment_marks_loudness_unavailable_when_backend_errors(self, monkeypatch):
        engine = self._make_engine()
        sample_rate = 44100.0
        buffer = np.zeros((int(sample_rate * 3.0), 1))
        config = _make_config()

        def _raising_backend_error(*args, **kwargs):
            raise RuntimeError("loudness backend unavailable")

        monkeypatch.setattr(dsp, "compute_loudness_trace", _raising_backend_error)

        result = engine.judge(config, buffer, sample_rate, mic_cal={}, audio_file_path="/tmp/test.wav")

        assert result.channel_results[0].loudness_available is False
        assert result.channel_results[0].loudness_error_message == "loudness backend unavailable"
        assert result.limit_pass is False

    def test_judgment_preserves_loudness_failure_contract_when_segmented_backend_errors(self, monkeypatch):
        engine = self._make_engine()
        sample_rate = 48000.0
        buffer = np.zeros((int(sample_rate * 3.0), 1))
        config = _make_config()

        def _raising_backend_error(*args, **kwargs):
            raise RuntimeError("backend failed")

        monkeypatch.setattr(dsp, "compute_loudness_trace", _raising_backend_error)

        result = engine.judge(config, buffer, sample_rate, mic_cal={}, audio_file_path="/tmp/test.wav")

        assert result.channel_results[0].loudness_available is False
        assert result.channel_results[0].loudness_error_message == "backend failed"

    def test_spl_above_upper_limit_fails(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        # Very loud signal → SPL well above 85 dB
        sig = 10.0 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.channel_results[0].spl_pass is False
        assert result.limit_pass is False

    def test_spl_below_lower_limit_fails(self):
        engine = self._make_engine()
        sr = 44100.0
        # Very quiet signal → SPL below 40 dB
        buf = np.full((int(sr * 3), 1), 1e-10)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.channel_results[0].spl_pass is False

    def test_fft_band_limit_pass(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        sig = 0.01 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        ch_cfg = _make_channel_config(0, fft_band_limits=[
            {"freq": 1000.0, "lower": -100.0, "upper": 100.0},
        ])
        config = _make_config(channels=[ch_cfg])
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert len(result.channel_results[0].fft_band_results) == 1
        assert result.channel_results[0].fft_band_results[0].passed is True

    def test_fft_band_limit_fail(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        sig = 10.0 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        ch_cfg = _make_channel_config(0, fft_band_limits=[
            {"freq": 1000.0, "lower": -200.0, "upper": -100.0},  # impossibly low
        ])
        config = _make_config(channels=[ch_cfg])
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.channel_results[0].fft_band_results[0].passed is False

    def test_fft_off_mode_uses_full_window_fft_and_preserves_absolute_band_space(
        self,
        monkeypatch,
    ):
        engine = self._make_engine()
        sr = 48_000.0
        window_samples = int(sr * 3.0)
        buf = np.full((window_samples, 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    stft_nfft=512,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": 9.0, "upper": 13.0},
                    ],
                    fft_golden_mode="off",
                )
            ],
        )
        fft_calls: list[int] = []

        def fake_fft(signal, sample_rate, nfft):
            fft_calls.append(int(nfft))
            return np.array([1000.0], dtype=np.float64), np.array(
                [11.0 + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_fft_spectrum", fake_fft)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        band = result.channel_results[0].fft_band_results[0]

        assert fft_calls == [window_samples]
        assert band.level == pytest.approx(11.0)
        assert band.lower == pytest.approx(9.0)
        assert band.upper == pytest.approx(13.0)
        assert result.channel_results[0].fft_available is True

    def test_fft_absolute_mode_uses_golden_offset_bounds(self, tmp_path, monkeypatch):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                    ],
                    fft_golden_mode="absolute",
                    **_golden_binding(golden_path),
                )
            ],
        )

        def fake_fft(signal, sample_rate, nfft):
            if np.isclose(float(signal[0]), 0.25):
                level = 11.0
            elif np.isclose(float(signal[0]), 0.5):
                level = 10.0
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0], dtype=np.float64), np.array(
                [level + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_fft_spectrum", fake_fft)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        band = result.channel_results[0].fft_band_results[0]

        assert band.level == pytest.approx(11.0)
        assert band.lower == pytest.approx(9.0)
        assert band.upper == pytest.approx(11.0)
        assert band.passed is True
        assert result.channel_results[0].fft_available is True

    def test_fft_absolute_mode_uses_selected_window_sample_count_for_measured_and_golden_fft(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        window_samples = int(sr * 3.0)
        buf = np.full((window_samples, 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    stft_nfft=512,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                    ],
                    fft_golden_mode="absolute",
                    **_golden_binding(golden_path),
                )
            ],
        )
        fft_calls: list[dict[str, int | str]] = []

        def fake_fft(signal, sample_rate, nfft):
            if np.isclose(float(signal[0]), 0.25):
                source = "measured"
                level = 11.0
            elif np.isclose(float(signal[0]), 0.5):
                source = "golden"
                level = 10.0
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            fft_calls.append({"source": source, "nfft": int(nfft)})
            return np.array([1000.0], dtype=np.float64), np.array(
                [level + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_fft_spectrum", fake_fft)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        assert fft_calls == [
            {"source": "golden", "nfft": window_samples},
            {"source": "measured", "nfft": window_samples},
        ]
        assert result.channel_results[0].fft_available is True

    def test_fft_delta_mode_serializes_band_result_in_delta_space(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": -3.0, "upper": 3.0},
                    ],
                    fft_golden_mode="delta",
                    **_golden_binding(golden_path),
                )
            ],
        )

        def fake_fft(signal, sample_rate, nfft):
            if np.isclose(float(signal[0]), 0.25):
                level = 7.0
            elif np.isclose(float(signal[0]), 0.5):
                level = 10.0
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0], dtype=np.float64), np.array(
                [level + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_fft_spectrum", fake_fft)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        band = result.channel_results[0].fft_band_results[0]

        assert band.level == pytest.approx(-3.0)
        assert band.lower == pytest.approx(-3.0)
        assert band.upper == pytest.approx(3.0)
        assert band.passed is True
        assert result.channel_results[0].fft_available is True

    def test_fft_empty_band_limits_stay_unconstrained_in_absolute_mode_with_missing_golden(
        self,
        tmp_path,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        sr = 48_000.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        missing_path = tmp_path / "managed" / "config_1" / "missing.wav"
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    fft_golden_mode="absolute",
                    **_golden_binding(missing_path, sample_rate=sr),
                )
            ],
        )

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        channel_result = result.channel_results[0]

        assert channel_result.fft_band_results == []
        assert channel_result.fft_available is True
        assert channel_result.fft_error_message is None
        assert result.limit_pass is True

    def test_octave_empty_band_limits_stay_unconstrained_in_delta_mode_with_corrupt_golden(
        self,
        tmp_path,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        sr = 48_000.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        corrupt_path = tmp_path / "managed" / "config_1" / "corrupt.wav"
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_bytes(b"not a wav")
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    oct_golden_mode="delta",
                    **_golden_binding(corrupt_path, sample_rate=sr),
                )
            ],
        )

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        channel_result = result.channel_results[0]

        assert channel_result.oct_band_results == []
        assert channel_result.octave_available is True
        assert channel_result.octave_error_message is None
        assert result.limit_pass is True

    def test_fft_golden_derive_failure_becomes_unavailable_instead_of_raising(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                    ],
                    fft_golden_mode="absolute",
                    **_golden_binding(golden_path, sample_rate=sr),
                )
            ],
        )

        def _raising_fft(*args, **kwargs):
            raise RuntimeError("fft backend exploded")

        monkeypatch.setattr(dsp, "compute_fft_spectrum", _raising_fft)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        channel_result = result.channel_results[0]

        assert channel_result.fft_available is False
        assert channel_result.fft_band_results == []
        assert "fft backend exploded" in (channel_result.fft_error_message or "")
        assert result.limit_pass is False

    def test_octave_absolute_mode_uses_golden_offset_bounds(self, tmp_path, monkeypatch):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    oct_band_limits=[
                        {"freq": 1000.0, "lower": -2.0, "upper": 2.0},
                    ],
                    oct_golden_mode="absolute",
                    **_golden_binding(golden_path),
                )
            ],
        )

        def fake_octave(signal, sample_rate, weighting):
            if np.isclose(float(signal[0]), 0.25):
                level = 14.0
            elif np.isclose(float(signal[0]), 0.5):
                level = 12.0
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0], dtype=np.float64), np.array(
                [level + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_octave_bands", fake_octave)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        band = result.channel_results[0].oct_band_results[0]

        assert band.level == pytest.approx(14.0)
        assert band.lower == pytest.approx(10.0)
        assert band.upper == pytest.approx(14.0)
        assert band.passed is True
        assert result.channel_results[0].octave_available is True

    def test_octave_delta_mode_serializes_band_result_in_delta_space(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    oct_band_limits=[
                        {"freq": 1000.0, "lower": 1.0, "upper": 3.0},
                    ],
                    oct_golden_mode="delta",
                    **_golden_binding(golden_path),
                )
            ],
        )

        def fake_octave(signal, sample_rate, weighting):
            if np.isclose(float(signal[0]), 0.25):
                level = 14.0
            elif np.isclose(float(signal[0]), 0.5):
                level = 12.0
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0], dtype=np.float64), np.array(
                [level + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_octave_bands", fake_octave)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        band = result.channel_results[0].oct_band_results[0]

        assert band.level == pytest.approx(2.0)
        assert band.lower == pytest.approx(1.0)
        assert band.upper == pytest.approx(3.0)
        assert band.passed is True
        assert result.channel_results[0].octave_available is True

    def test_octave_golden_derive_failure_becomes_unavailable_instead_of_raising(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    oct_band_limits=[
                        {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                    ],
                    oct_golden_mode="absolute",
                    **_golden_binding(golden_path, sample_rate=sr),
                )
            ],
        )

        def _raising_octave(*args, **kwargs):
            raise RuntimeError("octave backend exploded")

        monkeypatch.setattr(dsp, "compute_octave_bands", _raising_octave)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        channel_result = result.channel_results[0]

        assert channel_result.octave_available is False
        assert channel_result.oct_band_results == []
        assert "octave backend exploded" in (channel_result.octave_error_message or "")
        assert result.limit_pass is False

    def test_wrong_length_golden_file_becomes_unavailable_instead_of_producing_result(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            samples=120_000,
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    oct_band_limits=[
                        {"freq": 1000.0, "lower": -2.0, "upper": 2.0},
                    ],
                    oct_golden_mode="absolute",
                    **_golden_binding(golden_path, sample_rate=sr),
                )
            ],
        )

        def fake_octave(signal, sample_rate, weighting):
            if np.isclose(float(signal[0]), 0.25):
                level = 14.0
            elif np.isclose(float(signal[0]), 0.5):
                level = 12.0
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0], dtype=np.float64), np.array(
                [level + _REF_DB],
                dtype=np.float64,
            )

        monkeypatch.setattr(dsp, "compute_octave_bands", fake_octave)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        channel_result = result.channel_results[0]

        assert channel_result.octave_available is False
        assert channel_result.oct_band_results == []
        assert "长度" in (channel_result.octave_error_message or "")
        assert result.limit_pass is False

    def test_golden_enabled_judgment_marks_fft_and_octave_unavailable_on_runtime_sample_rate_mismatch(
        self,
        tmp_path,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "gold.wav")
        sr = 44_100.0
        buf = np.zeros((int(sr * 3.0), 1))
        config = _make_config(
            input_device_samplerate=48_000.0,
            channels=[
                _make_channel_config(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": -3.0, "upper": 3.0},
                    ],
                    oct_band_limits=[
                        {"freq": 1000.0, "lower": -3.0, "upper": 3.0},
                    ],
                    fft_golden_mode="absolute",
                    oct_golden_mode="delta",
                    **_golden_binding(golden_path, sample_rate=48_000.0),
                )
            ],
        )

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        channel_result = result.channel_results[0]

        assert channel_result.fft_available is False
        assert "采样率" in (channel_result.fft_error_message or "")
        assert channel_result.octave_available is False
        assert "采样率" in (channel_result.octave_error_message or "")
        assert result.limit_pass is False

    def test_overlay_only_mode_off_logs_warning_without_failing_pass_fail(
        self,
        tmp_path,
        caplog,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        missing_path = tmp_path / "managed" / "config_1" / "missing.wav"
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": 9.0, "upper": 13.0},
                    ],
                    fft_show_golden_overlay=True,
                    **_golden_binding(missing_path),
                )
            ],
        )

        monkeypatch.setattr(
            dsp,
            "compute_fft_spectrum",
            lambda signal, sample_rate, nfft: (
                np.array([1000.0], dtype=np.float64),
                np.array([11.0 + _REF_DB], dtype=np.float64),
            ),
        )

        with caplog.at_level(logging.WARNING):
            result = engine.judge(
                config,
                buf,
                sr,
                mic_cal={},
                audio_file_path="/tmp/test.wav",
            )

        assert result.limit_pass is True
        assert result.channel_results[0].fft_available is True
        assert any("黄金样本" in record.message for record in caplog.records)

    def test_golden_reference_io_and_computation_run_once_per_channel_per_judgment_pass(
        self,
        tmp_path,
        monkeypatch,
    ):
        service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
        engine = self._make_engine(golden_sample_service=service)
        golden_path = _write_temp_wav(
            tmp_path / "managed" / "config_1" / "gold.wav",
            fill_value=0.5,
        )
        sr = 48_000.0
        buf = np.full((int(sr * 3.0), 1), 0.25, dtype=np.float32)
        config = _make_config(
            input_device_samplerate=sr,
            channels=[
                _make_channel_config(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": -3.0, "upper": 3.0},
                        {"freq": 2000.0, "lower": -3.0, "upper": 3.0},
                    ],
                    oct_band_limits=[
                        {"freq": 1000.0, "lower": -3.0, "upper": 3.0},
                        {"freq": 2000.0, "lower": -3.0, "upper": 3.0},
                    ],
                    fft_golden_mode="delta",
                    oct_golden_mode="delta",
                    **_golden_binding(golden_path),
                )
            ],
        )
        read_calls: list[str] = []
        real_read = golden_reference_module.wavfile.read
        fft_calls: list[float] = []
        octave_calls: list[float] = []

        def fake_read(path):
            read_calls.append(str(path))
            return real_read(path)

        def fake_fft(signal, sample_rate, nfft):
            fft_calls.append(float(signal[0]))
            if np.isclose(float(signal[0]), 0.25):
                levels = [7.0 + _REF_DB, 8.0 + _REF_DB]
            elif np.isclose(float(signal[0]), 0.5):
                levels = [9.0 + _REF_DB, 10.0 + _REF_DB]
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0, 2000.0], dtype=np.float64), np.array(
                levels,
                dtype=np.float64,
            )

        def fake_octave(signal, sample_rate, weighting):
            octave_calls.append(float(signal[0]))
            if np.isclose(float(signal[0]), 0.25):
                levels = [11.0 + _REF_DB, 12.0 + _REF_DB]
            elif np.isclose(float(signal[0]), 0.5):
                levels = [9.0 + _REF_DB, 10.0 + _REF_DB]
            else:  # pragma: no cover - makes failures easier to read
                raise AssertionError(f"unexpected signal marker {float(signal[0])}")
            return np.array([1000.0, 2000.0], dtype=np.float64), np.array(
                levels,
                dtype=np.float64,
            )

        monkeypatch.setattr(golden_reference_module.wavfile, "read", fake_read)
        monkeypatch.setattr(dsp, "compute_fft_spectrum", fake_fft)
        monkeypatch.setattr(dsp, "compute_octave_bands", fake_octave)

        result = engine.judge(
            config,
            buf,
            sr,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )

        assert len(read_calls) == 1
        assert fft_calls.count(0.25) == 1
        assert fft_calls.count(0.5) == 1
        assert octave_calls.count(0.25) == 1
        assert octave_calls.count(0.5) == 1
        assert result.channel_results[0].fft_available is True
        assert result.channel_results[0].octave_available is True

    def test_missing_channel_in_buffer_fails(self):
        """Selected channel not in buffer should produce a fail verdict."""
        engine = self._make_engine()
        sr = 44100.0
        buf = np.zeros((int(sr * 3), 1))
        config = _make_config(selected_channels=[1])
        result = engine.judge(config, buf, sr, mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.limit_pass is False
        assert result.overall == "不合格"

    def test_mic_calibration_changes_spl(self):
        """Mic calibration should affect the SPL calculation."""
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        sig = 0.001 * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        r_raw = engine.judge(config, buf, sr, mic_cal={}, audio_file_path="/tmp/test.wav")
        mic_cal = {"In1": {"deviation_value": "2.0"}}
        r_cal = engine.judge(config, buf, sr, mic_cal=mic_cal, audio_file_path="/tmp/test.wav")
        assert r_cal.channel_results[0].spl_db > r_raw.channel_results[0].spl_db

    def test_no_ai_configured(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None

    def test_all_pass_verdict(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        config = _make_config()
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        # With no AI and SPL within limits → pass
        assert result.overall == "合格"

    def test_octave_band_limit_fail(self):
        """A signal exceeding an octave band limit should produce a fail verdict."""
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        # Strong 1 kHz tone — should produce high energy in the 1 kHz band
        amplitude = 20e-6 * 10 ** (80 / 20)  # ~80 dB
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        # Set octave band limit: 1 kHz upper = 50 dB (will be exceeded)
        config = _make_config(
            channels=[_make_channel_config(0,
                oct_band_limits=[{"freq": 1000, "lower": -100, "upper": 50}],
            )],
        )
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.limit_pass is False
        assert result.overall == "不合格"
        # Verify the band result details
        ch_result = result.channel_results[0]
        assert any(not br.passed for br in ch_result.oct_band_results)

    def test_fft_band_limit_fail_high_amplitude(self):
        """A signal exceeding an FFT band limit should produce a fail verdict."""
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (80 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        # Set FFT band limit: 1 kHz upper = 50 dB (will be exceeded)
        config = _make_config(
            channels=[_make_channel_config(0,
                fft_band_limits=[{"freq": 1000, "lower": -100, "upper": 50}],
            )],
        )
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.limit_pass is False

    def test_judge_uses_analysis_window_offset_and_duration(self):
        """Automatic judgment should use the configured offset-based window."""
        engine = self._make_engine()
        sr = 44100.0
        config = _make_config(
            record_duration=5.0,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
        )
        # 5 seconds of audio, single channel
        buf = np.random.randn(int(sr * 5), 1) * 0.001
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert result.analysis_window == pytest.approx((1.0, 3.0), abs=0.01)

    def test_judge_raises_required_window_error_when_buffer_too_short(self):
        """Automatic judgment should fail when the required window was not captured."""
        engine = self._make_engine()
        sr = 44100.0
        config = _make_config(
            record_duration=5.0,
            analysis_duration=3.0,
            analysis_window_offset_ms=3000,
        )
        buf = np.random.randn(int(sr * 4), 1) * 0.001

        with pytest.raises(ValueError, match="analysis window"):
            engine.judge(config, buf, sr, mic_cal={},
                         audio_file_path="/tmp/test.wav")

    def test_multi_channel(self):
        engine = self._make_engine()
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        ch0 = amplitude * np.sin(2 * np.pi * 1000 * t)
        ch1 = amplitude * np.sin(2 * np.pi * 500 * t)
        buf = np.column_stack([ch0, ch1])
        config = _make_config(
            selected_channels=[0, 1],
            channels=[_make_channel_config(0), _make_channel_config(1)],
        )
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        assert len(result.channel_results) == 2


class TestRejudge:
    def test_custom_window(self):
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        buf = np.random.randn(int(sr * 5), 1) * 0.001
        config = _make_config()
        result = engine.rejudge(config, buf, sr, mic_cal={},
                                audio_file_path="/tmp/test.wav",
                                start_sec=1.0, end_sec=4.0)
        assert result.analysis_window == pytest.approx((1.0, 4.0), abs=0.01)

    def test_reversed_window_clamped(self):
        """start_sec > end_sec should be clamped, not crash."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        buf = np.random.randn(int(sr * 5), 1) * 0.001
        config = _make_config()
        result = engine.rejudge(config, buf, sr, mic_cal={},
                                audio_file_path="/tmp/test.wav",
                                start_sec=4.0, end_sec=1.0)
        # end_sec gets clamped to start_sec (empty window), should not crash
        assert isinstance(result, JudgmentResult)

    def test_out_of_bounds_clamped(self):
        """Window beyond buffer length should be clamped."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        buf = np.random.randn(int(sr * 2), 1) * 0.001
        config = _make_config()
        result = engine.rejudge(config, buf, sr, mic_cal={},
                                audio_file_path="/tmp/test.wav",
                                start_sec=0.0, end_sec=100.0)
        assert result.analysis_window[1] == pytest.approx(2.0, abs=0.1)


class TestMissingChannelConfig:
    def test_missing_config_uses_defaults(self):
        """Selected channel with no config should use defaults, not skip."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = None
        engine = JudgmentEngine(mock_ai)
        sr = 44100.0
        t = np.arange(int(sr * 3.0)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        buf = sig.reshape(-1, 1)
        # Config says channel 0 selected, but no channel configs provided
        config = _make_config(selected_channels=[0], channels=[])
        result = engine.judge(config, buf, sr, mic_cal={},
                              audio_file_path="/tmp/test.wav")
        # Should still analyze channel 0 using defaults, not skip it
        assert len(result.channel_results) == 1


class TestRunAiDeferral:
    """_run_ai() intentionally returns (None, None) in JudgmentEngine.

    AI integration requires full context (test_run_id, segments, etc.) that only
    TestSession has. JudgmentEngine delegates AI to TestSession via the deferred
    pattern — _run_ai() validates binding exists but does not call AiService.run_ai_analysis().
    """

    def test_ai_returns_none_when_no_config_id(self):
        mock_ai = MagicMock()
        engine = JudgmentEngine(mock_ai)
        config = _make_config(config_id=None)
        result = engine.judge(config, np.zeros((44100 * 3, 1)), 44100.0,
                              mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None

    def test_ai_returns_none_when_binding_disabled(self):
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = {"enabled": False}
        engine = JudgmentEngine(mock_ai)
        config = _make_config()
        result = engine.judge(config, np.zeros((44100 * 3, 1)), 44100.0,
                              mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None
        # Confirm resolve_binding was called (integration validated)
        mock_ai.resolve_binding.assert_called_once_with(config["config_id"])

    def test_ai_returns_none_when_binding_enabled(self):
        """Even with a valid binding, _run_ai defers to TestSession."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.return_value = {"enabled": True}
        engine = JudgmentEngine(mock_ai)
        config = _make_config()
        result = engine.judge(config, np.zeros((44100 * 3, 1)), 44100.0,
                              mic_cal={}, audio_file_path="/tmp/test.wav")
        assert result.ai_pass is None
        assert result.ai_score is None
        # Confirm run_ai_analysis was NOT called (deferred to TestSession)
        mock_ai.run_ai_analysis.assert_not_called()

    def test_ai_returns_none_when_resolve_binding_throws(self):
        """If resolve_binding raises, judgment should still produce a result."""
        mock_ai = MagicMock()
        mock_ai.resolve_binding.side_effect = RuntimeError("db unavailable")
        engine = JudgmentEngine(mock_ai)
        config = _make_config()
        result = engine.judge(
            config,
            np.zeros((44100 * 3, 1)),
            44100.0,
            mic_cal={},
            audio_file_path="/tmp/test.wav",
        )
        assert result.ai_pass is None
        assert result.ai_score is None
        assert isinstance(result, JudgmentResult)
