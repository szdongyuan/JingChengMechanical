import math

import numpy as np
import pytest

from app.service.analysis import dsp, stft_score
from app.service.analysis.golden_reference import GoldenReferenceError
from app.service.analysis.review_payloads import (
    attach_stft_score_summary,
    build_chart_payload,
    build_summary_payload,
    canonical_channel_label,
)


def _make_config(*, fft_limits=True, octave_limits=True):
    def _channel(channel_index: int):
        return {
            "channel_index": channel_index,
            "fft_nfft": 4096,
            "spl_lower_enabled": True,
            "spl_lower_limit": 40.0,
            "spl_upper_enabled": True,
            "spl_upper_limit": 80.0,
            "spl_average_lower_enabled": True,
            "spl_average_lower_limit": 40.0,
            "spl_average_upper_enabled": True,
            "spl_average_upper_limit": 80.0,
            "loudness_unit": "sone",
            "loudness_lower_enabled": True,
            "loudness_lower_limit": 0.0,
            "loudness_upper_enabled": True,
            "loudness_upper_limit": 10.0,
            "loudness_average_lower_enabled": True,
            "loudness_average_lower_limit": 0.0,
            "loudness_average_upper_enabled": True,
            "loudness_average_upper_limit": 10.0,
            "fft_band_limits": (
                [
                    {"freq": 1000.0, "lower": 40.0, "upper": 60.0},
                    {"freq": 2000.0, "lower": 40.0, "upper": 60.0},
                ]
                if fft_limits
                else []
            ),
            "oct_band_limits": (
                [
                    {"freq": 500.0, "lower": 35.0, "upper": 55.0},
                    {"freq": 1000.0, "lower": 35.0, "upper": 55.0},
                ]
                if octave_limits
                else []
            ),
        }

    return {
        "config_id": 101,
        "selected_channels": [0, 1],
        "channels": [_channel(0), _channel(1)],
    }


def _make_judgment_payload():
    return {
        "overall": "不合格",
        "limit_pass": False,
        "ai_pass": True,
        "ai_score": 0.93,
        "analysis_window": (1.0, 3.0),
        "channel_results": [
            {
                "channel_index": 0,
                "spl_db": 77.0,
                "spl_pass": True,
                "spl_min_db": 45.0,
                "spl_max_db": 77.0,
                "spl_average_db": 60.0,
                "spl_average_pass": True,
                "spl_trace_available": True,
                "spl_trace_error_message": None,
                "spl_average_available": True,
                "spl_average_error_message": None,
                "loudness_min_sone": 2.0,
                "loudness_max_sone": 7.0,
                "loudness_average_sone": 4.0,
                "loudness_pass": True,
                "loudness_average_pass": True,
                "loudness_available": True,
                "loudness_error_message": None,
                "fft_available": True,
                "fft_error_message": None,
                "octave_available": True,
                "octave_error_message": None,
                "fft_band_results": [
                    {
                        "freq": 1000.0,
                        "level": 57.0,
                        "lower": 40.0,
                        "upper": 60.0,
                        "passed": True,
                    }
                ],
                "oct_band_results": [
                    {
                        "freq": 500.0,
                        "level": 55.0,
                        "lower": 35.0,
                        "upper": 55.0,
                        "passed": True,
                    }
                ],
            },
            {
                "channel_index": 1,
                "spl_db": 86.0,
                "spl_pass": False,
                "spl_min_db": 45.0,
                "spl_max_db": 86.0,
                "spl_average_db": 84.0,
                "spl_average_pass": False,
                "spl_trace_available": True,
                "spl_trace_error_message": None,
                "spl_average_available": True,
                "spl_average_error_message": None,
                "loudness_min_sone": 2.0,
                "loudness_max_sone": 12.0,
                "loudness_average_sone": 9.0,
                "loudness_pass": False,
                "loudness_average_pass": False,
                "loudness_available": True,
                "loudness_error_message": None,
                "fft_available": True,
                "fft_error_message": None,
                "octave_available": True,
                "octave_error_message": None,
                "fft_band_results": [
                    {
                        "freq": 1000.0,
                        "level": 50.0,
                        "lower": 40.0,
                        "upper": 60.0,
                        "passed": True,
                    }
                ],
                "oct_band_results": [
                    {
                        "freq": 500.0,
                        "level": 45.0,
                        "lower": 35.0,
                        "upper": 55.0,
                        "passed": True,
                    }
                ],
            },
        ],
    }


def _make_single_channel_config(**channel_overrides):
    config = _make_config()
    channel = dict(config["channels"][0])
    channel.update(channel_overrides)
    config["selected_channels"] = [0]
    config["channels"] = [channel]
    return config


def _make_single_channel_judgment_payload(**channel_overrides):
    payload = _make_judgment_payload()
    channel_result = dict(payload["channel_results"][0])
    channel_result.update(channel_overrides)
    payload["channel_results"] = [channel_result]
    return payload


def _make_ai_only_config():
    return {
        "config_id": 101,
        "selected_channels": [],
        "channels": [],
    }


def _make_ai_only_judgment_payload(*, ai_pass, ai_score):
    return {
        "overall": "合格" if ai_pass is not False else "不合格",
        "limit_pass": True,
        "ai_pass": ai_pass,
        "ai_score": ai_score,
        "analysis_window": (1.0, 3.0),
        "channel_results": [],
    }


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


class _GoldenCacheStub:
    def __init__(
        self,
        *,
        fft_axis=None,
        fft_levels=None,
        octave_axis=None,
        octave_levels=None,
        bundle_error: Exception | None = None,
        fft_error: Exception | None = None,
        octave_error: Exception | None = None,
    ):
        self.bundle_calls = 0
        self.ensure_fft_calls = 0
        self.ensure_fft_nffts: list[int] = []
        self.ensure_fft_weightings: list[str] = []
        self.ensure_octave_calls = 0
        self.fft_axis = np.asarray(fft_axis or [], dtype=np.float64)
        self.fft_levels = np.asarray(fft_levels or [], dtype=np.float64)
        self.octave_axis = np.asarray(octave_axis or [], dtype=np.float64)
        self.octave_levels = np.asarray(octave_levels or [], dtype=np.float64)
        self.bundle_error = bundle_error
        self.fft_error = fft_error
        self.octave_error = octave_error

    def bundle_for_channel(self, **kwargs):
        self.bundle_calls += 1
        if self.bundle_error is not None:
            raise self.bundle_error
        return object()

    def ensure_fft(self, bundle, *, nfft, weighting="Z"):
        self.ensure_fft_calls += 1
        self.ensure_fft_nffts.append(int(nfft))
        self.ensure_fft_weightings.append(str(weighting))
        if self.fft_error is not None:
            raise self.fft_error
        return self.fft_axis, self.fft_levels

    def ensure_octave(self, bundle, *, weighting):
        self.ensure_octave_calls += 1
        if self.octave_error is not None:
            raise self.octave_error
        return self.octave_axis, self.octave_levels


def _build_single_channel_band_summary(*, config_key, result_key, band_results):
    configured_bands = [
        {"freq": 500.0, "lower": 35.0, "upper": 55.0},
        {"freq": 1000.0, "lower": 35.0, "upper": 55.0},
    ]

    return build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "fft_band_limits": configured_bands if config_key == "fft_band_limits" else [],
                    "oct_band_limits": configured_bands if config_key == "oct_band_limits" else [],
                }
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "loudness_max_sone": 5.0,
                    "loudness_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": band_results if result_key == "fft_band_results" else [],
                    "oct_band_results": band_results if result_key == "oct_band_results" else [],
                }
            ],
        },
        ai_review={"status": "not_enabled"},
    )


def _internal_loudness_trace(values_sone):
    values = np.asarray(values_sone, dtype=np.float64)
    times = np.arange(values.size, dtype=np.float64) / dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ
    return times.tolist(), values.tolist(), float(np.max(values))


def _valid_loudness_cache_entry(values_sone):
    times, values, max_sone = _internal_loudness_trace(values_sone)
    return {
        "times_sec": times,
        "values_sone": values,
        "max_sone": max_sone,
        "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
        "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
    }


def _two_display_point_values(start: float, end: float) -> np.ndarray:
    return np.linspace(start, end, 7, dtype=np.float64)


def _patch_basic_chart_backends(
    monkeypatch,
    *,
    fft_axis=(1000.0, 2000.0),
    fft_levels=(67.0, 45.0),
    octave_axis=(500.0, 1000.0),
    octave_levels=(57.0, 43.0),
):
    ref_db = 20.0 * math.log10(20e-6)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.asarray(fft_axis, dtype=np.float64),
            np.asarray(fft_levels, dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.asarray(octave_axis, dtype=np.float64),
            np.asarray(octave_levels, dtype=np.float64) + ref_db,
        ),
    )


def _patch_basic_loudness_backend(monkeypatch):
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: ([0.0, 1.0], [2.0, 3.0], 3.0),
    )


def test_build_chart_payload_adds_vibration_for_configured_channel(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    _patch_basic_loudness_backend(monkeypatch)
    sample_rate = 1000.0
    times = np.arange(0, 1.0, 1.0 / sample_rate)
    buffer = np.column_stack(
        [
            np.ones_like(times),
            np.sin(2.0 * np.pi * 12.0 * times),
        ]
    )
    config = _make_config()
    config["selected_channels"] = [0, 1]
    config["vibration_view_channel_index"] = 1

    payload = build_chart_payload(
        buffer=buffer,
        config=config,
        sample_rate=sample_rate,
        mic_cal={"In2": {"deviation_value": "2.0"}},
        analysis_window=(0.0, 1.0),
    )

    assert "vibration" not in payload[0]
    assert {"fft", "octave", "vibration"} <= set(payload[1])
    assert "spl" not in payload[1]
    assert "loudness" not in payload[1]
    assert "stft_score" not in payload[1]
    vibration = payload[1]["vibration"]
    assert vibration["channel_index"] == 1
    assert vibration["calibration_value"] == pytest.approx(2.0)
    assert vibration["overall_peak"] == pytest.approx(2.0, rel=0.02)
    assert vibration["overall_rms"] == pytest.approx(math.sqrt(2.0), rel=0.02)
    assert vibration["overall_crest_factor"] == pytest.approx(math.sqrt(2.0), rel=0.02)
    assert vibration["low_frequency_energy_share"] == pytest.approx(1.0, abs=0.02)
    assert vibration["dominant_frequency_hz"] == pytest.approx(12.0, rel=0.03)


def test_build_chart_payload_offsets_selected_window_vibration_times(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace_for_window",
        lambda _signal, _sample_rate, _start_sec, _end_sec: _internal_loudness_trace(
            _two_display_point_values(2.0, 3.0)
        ),
    )
    config = _make_single_channel_config()
    config["vibration_view_channel_index"] = 0
    sample_rate = 1000.0
    times = np.arange(0.0, 1.0, 1.0 / sample_rate)
    buffer = np.sin(2.0 * math.pi * 60.0 * times).reshape(-1, 1)

    payload = build_chart_payload(
        buffer=buffer,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.5, 0.9),
    )

    vibration = payload[0]["vibration"]
    assert "spl" not in payload[0]
    assert "loudness" not in payload[0]
    assert "stft_score" not in payload[0]
    assert vibration["times_sec"][0] == pytest.approx(0.55)
    assert vibration["frequency_times_sec"][0] == pytest.approx(0.55)


def test_build_chart_payload_adds_selected_window_axis_range_to_acoustic_time_series(monkeypatch):
    sample_rate = 1000.0
    times = np.arange(0, 2.0, 1.0 / sample_rate)
    signal = (0.02 * np.sin(2.0 * np.pi * 100.0 * times)).reshape(-1, 1)

    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace_for_window",
        lambda *_args, **_kwargs: ([0.0, 0.04, 1.0], [1.0, 1.1, 1.2], 1.2),
    )

    config = _make_single_channel_config()
    config["channels"][0]["fft_band_limits"] = []
    config["channels"][0]["oct_band_limits"] = []

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert payload[0]["spl"]["x_axis_range_sec"] == pytest.approx([0.5, 1.5])
    assert payload[0]["loudness"]["x_axis_range_sec"] == pytest.approx([0.5, 1.5])
    assert "x_axis_range_sec" not in payload[0].get("fft", {})
    assert "x_axis_range_sec" not in payload[0].get("octave", {})
    assert "x_axis_range_sec" not in payload[0].get("stft_score", {})
    assert "x_axis_range_sec" not in payload[0].get("current_trace", {})


def test_build_chart_payload_adds_selected_window_axis_range_to_vibration_payload():
    sample_rate = 1000.0
    times = np.arange(0, 2.0, 1.0 / sample_rate)
    signal = (0.5 * np.sin(2.0 * np.pi * 42.0 * times)).reshape(-1, 1)
    config = _make_single_channel_config()
    config["channels"][0]["fft_band_limits"] = []
    config["channels"][0]["oct_band_limits"] = []
    config["vibration_view_channel_index"] = 0

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert payload[0]["vibration"]["x_axis_range_sec"] == pytest.approx([0.5, 1.5])


def test_build_chart_payload_does_not_add_axis_range_to_stft_score_or_current_trace(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)

    def fake_score(**_kwargs):
        return {
            "enabled": True,
            "summary_score": 87.0,
            "times_sec": [0.5, 1.0],
            "scores": [90.0, 80.0],
            "summary_mode": "percentile",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)
    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace_for_window",
        lambda *_args, **_kwargs: ([0.0, 0.04, 1.0], [1.0, 1.1, 1.2], 1.2),
    )

    payload = build_chart_payload(
        buffer=np.ones((2000, 1), dtype=float),
        config=_make_single_channel_config(stft_score_enabled=True, stft_nfft=256),
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert "stft_score" in payload[0]
    assert "current_trace" not in payload[0]
    assert "x_axis_range_sec" not in payload[0]["stft_score"]
    assert "x_axis_range_sec" not in payload[0].get("current_trace", {})


def test_build_chart_payload_omits_axis_range_for_full_duration_window(monkeypatch):
    sample_rate = 1000.0
    signal = np.ones((1000, 1), dtype=float) * 0.01
    monkeypatch.setattr(
        dsp,
        "compute_loudness_trace",
        lambda *_args, **_kwargs: ([0.0, 0.04, 1.0], [1.0, 1.1, 1.2], 1.2),
    )

    config = _make_single_channel_config()
    config["channels"][0]["fft_band_limits"] = []
    config["channels"][0]["oct_band_limits"] = []

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert "x_axis_range_sec" not in payload[0]["spl"]
    assert "x_axis_range_sec" not in payload[0]["loudness"]


def test_build_chart_payload_omits_axis_range_for_full_duration_vibration_window():
    sample_rate = 1000.0
    times = np.arange(0, 1.0, 1.0 / sample_rate)
    signal = (0.5 * np.sin(2.0 * np.pi * 42.0 * times)).reshape(-1, 1)
    config = _make_single_channel_config()
    config["channels"][0]["fft_band_limits"] = []
    config["channels"][0]["oct_band_limits"] = []
    config["vibration_view_channel_index"] = 0

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert "x_axis_range_sec" not in payload[0]["vibration"]


def test_build_chart_payload_skips_vibration_when_configured_channel_is_not_selected(
    monkeypatch,
):
    _patch_basic_chart_backends(monkeypatch)
    _patch_basic_loudness_backend(monkeypatch)
    config = _make_single_channel_config()
    config["vibration_view_channel_index"] = 1

    payload = build_chart_payload(
        buffer=np.ones((1000, 1), dtype=float),
        config=config,
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(0.0, 1.0),
    )

    assert "vibration" not in payload[0]


def test_build_chart_payload_keeps_other_charts_when_vibration_fails(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    _patch_basic_loudness_backend(monkeypatch)
    config = _make_single_channel_config()
    config["vibration_view_channel_index"] = 0

    def fail_vibration_payload(**_kwargs):
        raise RuntimeError("vibration unavailable")

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.build_vibration_payload",
        fail_vibration_payload,
    )

    payload = build_chart_payload(
        buffer=np.ones((1000, 1), dtype=float),
        config=config,
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(0.0, 1.0),
    )

    assert "vibration" not in payload[0]
    assert {"fft", "octave"} <= set(payload[0])
    assert "spl" not in payload[0]
    assert "loudness" not in payload[0]
    assert "stft_score" not in payload[0]


def test_build_chart_payload_skips_acoustic_and_score_computation_for_vibration_channel(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    calls = {"spl_trace": 0, "loudness": 0, "score": 0}

    def fake_spl_trace(*_args, **_kwargs):
        calls["spl_trace"] += 1
        return [0.05], [70.0]

    def fake_loudness_trace(*_args, **_kwargs):
        calls["loudness"] += 1
        return _internal_loudness_trace(_two_display_point_values(2.0, 3.0))

    def fake_score(**_kwargs):
        calls["score"] += 1
        return {"enabled": True, "summary_score": 88.0}

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl_trace",
        fake_spl_trace,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        fake_loudness_trace,
    )
    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)

    config = _make_single_channel_config()
    config["vibration_view_channel_index"] = 0
    sample_rate = 1000.0
    times = np.arange(0.0, 1.0, 1.0 / sample_rate)

    payload = build_chart_payload(
        buffer=np.sin(2.0 * math.pi * 40.0 * times).reshape(-1, 1),
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, 1.0),
    )

    assert {"fft", "octave", "vibration"} <= set(payload[0])
    assert "spl" not in payload[0]
    assert "loudness" not in payload[0]
    assert "stft_score" not in payload[0]
    assert calls == {"spl_trace": 0, "loudness": 0, "score": 0}


def test_build_chart_payload_includes_stft_score_with_absolute_times(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    captured = {}

    def fake_score(**kwargs):
        captured.update(kwargs)
        return {
            "enabled": True,
            "summary_score": 87.42,
            "times_sec": [1.0, 1.5],
            "scores": [90.0, 80.0],
            "summary_mode": "percentile",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)
    buffer = np.ones((4000, 1), dtype=float)

    payload = build_chart_payload(
        buffer=buffer,
        config=_make_single_channel_config(
            stft_score_enabled=True,
            stft_nfft=256,
            stft_overlap=0.5,
        ),
        sample_rate=1000.0,
        mic_cal={"In1": {"deviation_value": "2.5"}},
        analysis_window=(1.0, 3.0),
    )

    assert payload[0]["stft_score"]["summary_score"] == 87.42
    assert captured["absolute_start_sec"] == pytest.approx(1.0)
    assert captured["sample_rate"] == 1000.0
    assert captured["stft_nfft"] == 256
    assert captured["stft_overlap"] == 0.5
    assert captured["score_config"]["stft_score_enabled"] is True
    assert captured["signal"].shape == (2000,)
    np.testing.assert_allclose(captured["signal"], np.full(2000, 2.5))
    assert captured["loudness_context_signal"].shape == (4000,)
    np.testing.assert_allclose(
        captured["loudness_context_signal"],
        np.full(4000, 2.5),
    )


def test_build_chart_payload_passes_loudness_trace_cache_to_stft_score(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    captured = {}

    def fake_score(**kwargs):
        captured.update(kwargs)
        return {
            "enabled": True,
            "summary_score": 88.0,
            "times_sec": [1.0],
            "scores": [88.0],
            "summary_mode": "percentile",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)
    cache_entry = _valid_loudness_cache_entry(
        np.linspace(2.0, 4.0, 301, dtype=np.float64)
    )

    payload = build_chart_payload(
        buffer=np.ones((4000, 1), dtype=float),
        config=_make_single_channel_config(
            stft_score_enabled=True,
            stft_nfft=256,
            stft_overlap=0.5,
        ),
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(1.0, 3.0),
        loudness_trace_cache={0: cache_entry},
    )

    assert payload[0]["stft_score"]["summary_score"] == 88.0
    assert captured["loudness_trace"] == cache_entry


def test_build_chart_payload_passes_non_grid_aligned_loudness_cache_to_stft_score(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    captured = {}

    def fake_score(**kwargs):
        captured.update(kwargs)
        return {
            "enabled": True,
            "summary_score": 88.0,
            "times_sec": [1.0],
            "scores": [88.0],
            "summary_mode": "percentile",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)
    cache_entry = _valid_loudness_cache_entry(
        np.linspace(2.0, 4.0, 152, dtype=np.float64)
    )

    payload = build_chart_payload(
        buffer=np.ones((3000, 1), dtype=float),
        config=_make_single_channel_config(
            stft_score_enabled=True,
            stft_nfft=256,
            stft_overlap=0.5,
        ),
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(1.0, 2.011),
        loudness_trace_cache={0: cache_entry},
    )

    assert payload[0]["stft_score"]["summary_score"] == 88.0
    assert captured["loudness_trace"] == cache_entry
    assert captured["loudness_trace"]["times_sec"][-1] == pytest.approx(1.0066666667)


def test_build_chart_payload_disabled_stft_score_uses_disabled_payload(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    captured = {}

    def fake_score(**kwargs):
        captured.update(kwargs)
        return {"enabled": False}

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)

    payload = build_chart_payload(
        buffer=np.ones((4000, 1), dtype=float),
        config=_make_single_channel_config(
            stft_score_enabled=False,
            stft_nfft=256,
        ),
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(1.0, 3.0),
    )

    assert captured["score_config"]["stft_score_enabled"] is False
    assert payload[0]["stft_score"] == {"enabled": False}


def test_build_chart_payload_score_failure_keeps_other_chart_payloads(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    captured = {"called": False}

    def fail_score(**kwargs):
        captured["called"] = True
        raise RuntimeError("score unavailable")

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fail_score)

    payload = build_chart_payload(
        buffer=np.ones((4000, 1), dtype=float),
        config=_make_single_channel_config(
            stft_score_enabled=True,
            stft_nfft=256,
        ),
        sample_rate=1000.0,
        mic_cal={},
        analysis_window=(1.0, 3.0),
    )

    assert captured["called"] is True
    assert "stft_score" not in payload[0]
    assert {"spl", "fft", "octave", "loudness"} <= set(payload[0])


def test_build_chart_payload_uses_reanalysis_window_start_for_stft_score(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    captured = {}

    def fake_score(**kwargs):
        captured.update(kwargs)
        start_sec = float(kwargs["absolute_start_sec"])
        return {
            "enabled": True,
            "summary_score": 91.0,
            "times_sec": [start_sec, start_sec + 0.5],
            "scores": [91.0, 90.0],
            "summary_mode": "percentile",
            "percentile": 10.0,
        }

    monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)

    payload = build_chart_payload(
        buffer=np.ones((500, 1), dtype=float),
        config=_make_single_channel_config(
            stft_score_enabled=True,
            stft_nfft=128,
            stft_overlap=0.5,
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(1.25, 3.25),
    )

    assert captured["absolute_start_sec"] == pytest.approx(1.25)
    assert payload[0]["stft_score"]["times_sec"][0] == pytest.approx(1.25)


def test_build_summary_payload_aggregates_selected_acoustic_channels_for_summary_rows():
    judgment_payload = _make_judgment_payload()
    judgment_payload["channel_results"][1]["fft_band_results"] = [
        {
            "freq": 1000.0,
            "level": 65.0,
            "lower": 40.0,
            "upper": 60.0,
            "passed": False,
        }
    ]

    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=judgment_payload,
        ai_review={"status": "success", "threshold": 0.8, "score": 0.93},
    )

    assert summary["overall_status"] == "fail"
    assert summary["overall_label"] == "FAIL"
    assert [row["label"] for row in summary["rows"]] == [
        "SPL",
        "平均SPL",
        "Loudness",
        "平均Loudness",
        "FFT",
        "倍频程",
        "频谱最低得分",
        "AI分析",
    ]
    assert summary["rows"][0]["label"] == "SPL"
    assert summary["rows"][0]["item_key"] == "spl"
    assert summary["rows"][0]["deviation_value"] == 6.0
    assert summary["rows"][0]["deviation_display"] == "6 dB"
    assert summary["rows"][0]["actual_display"] == "86.00 dB"
    assert summary["rows"][0]["channel_index"] == 1
    assert summary["rows"][0]["channel_label"] == "--"
    assert summary["rows"][0]["status"] == "NG"
    assert summary["rows"][0]["constrained"] is True
    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["channel_index"] == 1
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["status"] == "NG"
    assert summary["rows"][4]["label"] == "FFT"
    assert summary["rows"][4]["deviation_value"] == 5.0
    assert summary["rows"][4]["deviation_display"] == "5 dB"
    assert summary["rows"][4]["actual_display"] == "65.00 dB @ 1000 Hz"
    assert summary["rows"][4]["channel_index"] == 1
    assert summary["rows"][4]["channel_label"] == "CH2"
    assert summary["rows"][4]["status"] == "NG"


def test_build_summary_payload_reports_primary_mic_when_vibration_channel_is_worse():
    config = _make_config()
    config["vibration_view_channel_index"] = 1

    summary = build_summary_payload(
        config=config,
        judgment_payload=_make_judgment_payload(),
        ai_review={"status": "success", "threshold": 0.8, "score": 0.93},
    )

    assert summary["overall_status"] == "pass"
    assert [row["item_key"] for row in summary["rows"]] == [
        "spl",
        "spl_average",
        "loudness",
        "loudness_average",
        "fft",
        "octave",
        "stft_score",
        "ai",
    ]
    assert summary["rows"][0]["channel_label"] == "--"
    assert summary["rows"][0]["status"] == "OK"
    assert summary["rows"][0]["actual_value"] == pytest.approx(77.0)
    assert summary["rows"][0]["actual_display"] == "77.00 dB"
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["actual_value"] == pytest.approx(2.0)
    assert summary["rows"][2]["actual_display"] == "2.00 sone"
    assert summary["rows"][4]["channel_label"] == "CH1"
    assert summary["rows"][4]["actual_value"] == pytest.approx(57.0)
    assert summary["rows"][4]["actual_frequency_hz"] == pytest.approx(1000.0)
    assert summary["rows"][4]["actual_display"] == "57.00 dB @ 1000 Hz"
    assert summary["rows"][5]["actual_display"] == "55.00 dB @ 500 Hz"


def test_build_summary_payload_sets_peak_rows_channel_label_to_current_at_event():
    config = _make_single_channel_config(
        spl_lower_limit=40.0,
        spl_upper_limit=80.0,
        loudness_lower_limit=1.0,
        loudness_upper_limit=10.0,
    )
    judgment_payload = _make_single_channel_judgment_payload(
        spl_db=70.0,
        spl_min_db=60.0,
        spl_max_db=77.0,
        loudness_max_sone=7.0,
    )
    chart_payload = {
        0: {
            "spl": {"times_sec": [0.0, 1.0, 2.0], "values_db": [60.0, 77.0, 70.0]},
            "loudness": {
                "times_sec": [0.0, 1.0, 2.0],
                "values": [2.0, 7.0, 5.0],
                "unit": "sone",
            },
            "current_trace": {
                "times_sec": [0.0, 2.0],
                "values_a": [1.0, 3.0],
                "average_a": 2.0,
                "max_a": 3.0,
                "latest_a": 3.0,
                "unit": "mA",
            },
        }
    }

    summary = build_summary_payload(
        config=config,
        judgment_payload=judgment_payload,
        chart_payload=chart_payload,
    )

    assert summary["rows"][0]["item_key"] == "spl"
    assert summary["rows"][0]["channel_label"] == "2.000 mA"
    assert summary["rows"][0]["channel_index"] == 0
    assert summary["rows"][2]["item_key"] == "loudness"
    assert summary["rows"][2]["channel_label"] == "2.000 mA"
    assert summary["rows"][2]["channel_index"] == 0
    assert summary["rows"][1]["channel_label"] == "CH1"

    chart_payload[0].pop("current_trace")

    summary = build_summary_payload(
        config=config,
        judgment_payload=judgment_payload,
        chart_payload=chart_payload,
    )

    assert summary["rows"][0]["channel_label"] == "--"


def test_attach_stft_score_summary_sets_channel_label_to_current_at_min_score():
    config = _make_config()
    config["vibration_view_channel_index"] = 1
    summary = build_summary_payload(
        config=config,
        judgment_payload=_make_judgment_payload(),
    )

    attach_stft_score_summary(
        summary,
        config=config,
        judgment_payload=_make_judgment_payload(),
        chart_payload={
            0: {
                "stft_score": {
                    "enabled": True,
                    "summary_score": 72.0,
                    "times_sec": [0.0, 1.0, 2.0],
                    "scores": [90.0, 72.0, 80.0],
                },
                "current_trace": {
                    "times_sec": [0.0, 2.0],
                    "values_a": [1.0, 3.0],
                    "average_a": 2.0,
                    "max_a": 3.0,
                    "latest_a": 3.0,
                    "unit": "mA",
                },
            },
            1: {
                "stft_score": {"enabled": True, "summary_score": 12.0},
            },
        },
    )

    score_row = summary["rows"][6]
    assert score_row["item_key"] == "stft_score"
    assert score_row["channel_label"] == "2.000 mA"
    assert score_row["channel_index"] == 0
    assert score_row["actual_value"] == pytest.approx(72.0)
    assert score_row["actual_display"] == "72.00"
    assert score_row["status"] == "OK"
    assert score_row["constrained"] is False


def test_attach_stft_score_summary_uses_primary_mic_and_ignores_vibration_score():
    config = _make_config()
    config["vibration_view_channel_index"] = 1
    summary = build_summary_payload(
        config=config,
        judgment_payload=_make_judgment_payload(),
    )

    attach_stft_score_summary(
        summary,
        config=config,
        chart_payload={
            0: {"stft_score": {"enabled": True, "summary_score": 86.456}},
            1: {"stft_score": {"enabled": True, "summary_score": 12.0}},
        },
    )

    score_row = summary["rows"][6]
    assert score_row["item_key"] == "stft_score"
    assert score_row["label"] == "频谱最低得分"
    assert score_row["channel_label"] == "--"
    assert score_row["actual_value"] == pytest.approx(86.456)
    assert score_row["actual_display"] == "86.46"
    assert score_row["status"] == "OK"
    assert score_row["constrained"] is False


def test_build_summary_payload_formats_actual_values_with_two_decimals():
    judgment_payload = _make_judgment_payload()
    channel_result = judgment_payload["channel_results"][0]
    channel_result["spl_max_db"] = -55.8638
    channel_result["spl_min_db"] = -55.8638
    channel_result["spl_pass"] = False
    channel_result["fft_band_results"][0]["level"] = -55.8638
    channel_result["fft_band_results"][0]["passed"] = False

    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=judgment_payload,
    )

    assert summary["rows"][0]["actual_display"] == "-55.86 dB"
    assert summary["rows"][4]["actual_display"] == "-55.86 dB @ 1000 Hz"


def test_build_summary_payload_uses_spl_min_for_lower_limit_deviation():
    judgment_payload = _make_single_channel_judgment_payload(
        spl_db=60.0,
        spl_min_db=35.0,
        spl_max_db=62.0,
        spl_pass=False,
    )

    summary = build_summary_payload(
        config=_make_single_channel_config(spl_lower_limit=40.0, spl_upper_limit=80.0),
        judgment_payload=judgment_payload,
    )

    spl_row = summary["rows"][0]
    assert summary["overall_status"] == "fail"
    assert spl_row["deviation_value"] == 5.0
    assert spl_row["deviation_display"] == "5 dB"
    assert spl_row["status"] == "NG"


def test_build_summary_payload_handles_bounded_pass_margin_and_exact_boundary():
    summary = build_summary_payload(
        config=_make_single_channel_config(),
        judgment_payload=_make_single_channel_judgment_payload(),
        ai_review={"status": "success", "threshold": 0.8, "score": 0.93},
    )

    assert summary["rows"][1]["label"] == "平均SPL"
    assert summary["rows"][1]["deviation_value"] == -20.0
    assert summary["rows"][1]["deviation_display"] == "-20 dB"
    assert summary["rows"][1]["actual_display"] == "60.00 dB"
    assert summary["rows"][1]["channel_label"] == "CH1"
    assert summary["rows"][1]["status"] == "OK"
    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["deviation_value"] == -2.0
    assert summary["rows"][2]["deviation_display"] == "-2 sone"
    assert summary["rows"][2]["actual_display"] == "2.00 sone"
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["status"] == "OK"
    assert summary["rows"][3]["label"] == "平均Loudness"
    assert summary["rows"][3]["deviation_value"] == -4.0
    assert summary["rows"][3]["deviation_display"] == "-4 sone"
    assert summary["rows"][3]["actual_display"] == "4.00 sone"
    assert summary["rows"][3]["channel_label"] == "CH1"
    assert summary["rows"][3]["status"] == "OK"
    assert summary["rows"][4]["label"] == "FFT"
    assert summary["rows"][4]["deviation_value"] == -3.0
    assert summary["rows"][4]["deviation_display"] == "-3 dB"
    assert summary["rows"][4]["channel_label"] == "CH1"
    assert summary["rows"][4]["status"] == "OK"
    assert summary["rows"][5]["label"] == "倍频程"
    assert summary["rows"][5]["deviation_value"] == 0.0
    assert summary["rows"][5]["deviation_display"] == "0 dB"
    assert summary["rows"][5]["channel_label"] == "CH1"
    assert summary["rows"][5]["status"] == "OK"


def test_build_summary_payload_uses_ai_threshold_for_numeric_success():
    summary = build_summary_payload(
        config=_make_ai_only_config(),
        judgment_payload=_make_ai_only_judgment_payload(ai_pass=False, ai_score=0.48),
        ai_review={"status": "success", "threshold": 0.5, "score": 0.48},
    )

    assert summary["overall_status"] == "fail"
    assert summary["rows"][-1]["label"] == "AI分析"
    assert summary["rows"][-1]["deviation_value"] == 0.02
    assert summary["rows"][-1]["deviation_display"] == "0.02"
    assert summary["rows"][-1]["status"] == "NG"
    assert summary["rows"][-1]["constrained"] is True


def test_build_summary_payload_displays_chinese_ai_defect_label_with_score():
    summary = build_summary_payload(
        config=_make_ai_only_config(),
        judgment_payload=_make_ai_only_judgment_payload(ai_pass=False, ai_score=0.91),
        ai_review={
            "status": "success",
            "threshold": 0.5,
            "score": 0.91,
            "passed": False,
            "ai_label": "嗡嗡音",
        },
    )

    ai_row = summary["rows"][-1]
    assert summary["overall_status"] == "fail"
    assert ai_row["label"] == "AI分析"
    assert ai_row["actual_display"] == "0.91"
    assert ai_row["status"] == "NG"
    assert ai_row["failure_label"] == "嗡嗡音"
    assert ai_row["result_display"] == "NG（嗡嗡音）"
    assert ai_row["constrained"] is True


def test_build_summary_payload_keeps_scoreless_success_with_ai_threshold_as_no_result_even_when_judgment_used_ai_label():
    summary = build_summary_payload(
        config=_make_ai_only_config(),
        judgment_payload=_make_ai_only_judgment_payload(ai_pass=True, ai_score=None),
        ai_review={"status": "success", "threshold": 0.5, "score": None},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["rows"][-1]["label"] == "AI分析"
    assert summary["rows"][-1]["deviation_value"] is None
    assert summary["rows"][-1]["deviation_display"] == "--"
    assert summary["rows"][-1]["status"] == "无结果"
    assert summary["rows"][-1]["constrained"] is True


def test_build_summary_payload_marks_successful_numeric_ai_with_missing_ai_threshold_as_no_result():
    summary = build_summary_payload(
        config=_make_ai_only_config(),
        judgment_payload=_make_ai_only_judgment_payload(ai_pass=True, ai_score=0.81),
        ai_review={
            "status": "success",
            "threshold": None,
            "score": 0.81,
            "error_message": "missing threshold",
        },
    )

    assert summary["overall_status"] == "no_result"
    assert summary["error_message"] == "AI decision threshold unavailable"
    assert summary["rows"][-1]["label"] == "AI分析"
    assert summary["rows"][-1]["deviation_value"] is None
    assert summary["rows"][-1]["deviation_display"] == "--"
    assert summary["rows"][-1]["status"] == "无结果"
    assert summary["rows"][-1]["constrained"] is True


def test_build_summary_payload_renders_unconstrained_rows_and_scoreless_success_ai_as_no_result():
    summary = build_summary_payload(
        config=_make_config(fft_limits=False, octave_limits=False),
        judgment_payload={
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_min_db": 45.0,
                    "spl_max_db": 70.0,
                    "spl_average_db": 60.0,
                    "spl_average_pass": True,
                    "spl_trace_available": True,
                    "spl_average_available": True,
                    "loudness_min_sone": 2.0,
                    "loudness_max_sone": 5.0,
                    "loudness_average_sone": 4.0,
                    "loudness_pass": True,
                    "loudness_average_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 71.0,
                    "spl_pass": True,
                    "spl_min_db": 45.0,
                    "spl_max_db": 71.0,
                    "spl_average_db": 61.0,
                    "spl_average_pass": True,
                    "spl_trace_available": True,
                    "spl_average_available": True,
                    "loudness_min_sone": 2.0,
                    "loudness_max_sone": 6.0,
                    "loudness_average_sone": 5.0,
                    "loudness_pass": True,
                    "loudness_average_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "success", "channel_index": None},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["status"] == "OK"
    assert summary["rows"][2]["constrained"] is True
    assert summary["rows"][4]["label"] == "FFT"
    assert summary["rows"][4]["deviation_value"] is None
    assert summary["rows"][4]["deviation_display"] == "--"
    assert summary["rows"][4]["channel_index"] is None
    assert summary["rows"][4]["channel_label"] == "--"
    assert summary["rows"][4]["status"] == "OK"
    assert summary["rows"][4]["constrained"] is False
    assert summary["rows"][5]["label"] == "倍频程"
    assert summary["rows"][5]["status"] == "OK"
    assert summary["rows"][5]["deviation_display"] == "--"
    assert summary["rows"][6]["label"] == "频谱最低得分"
    assert summary["rows"][6]["status"] == "OK"
    assert summary["rows"][6]["constrained"] is False
    assert summary["rows"][7]["label"] == "AI分析"
    assert summary["rows"][7]["channel_label"] == "--"
    assert summary["rows"][7]["status"] == "无结果"
    assert summary["rows"][7]["constrained"] is True


@pytest.mark.parametrize("status", ["not_enabled", "not_configured", "disabled", "skipped"])
def test_build_summary_payload_treats_not_enabled_ai_statuses_as_unconstrained_even_with_threshold(status):
    summary = build_summary_payload(
        config=_make_single_channel_config(),
        judgment_payload=_make_single_channel_judgment_payload(),
        ai_review={
            "status": status,
            "threshold": 0.8,
            "score": None,
            "channel_index": 1,
            "error_message": "should be ignored",
        },
    )

    assert summary["overall_status"] == "pass"
    assert summary["overall_label"] == "PASS"
    assert summary["error_message"] is None
    assert summary["rows"][-1]["label"] == "AI分析"
    assert summary["rows"][-1]["deviation_value"] is None
    assert summary["rows"][-1]["deviation_display"] == "--"
    assert summary["rows"][-1]["channel_index"] is None
    assert summary["rows"][-1]["channel_label"] == "--"
    assert summary["rows"][-1]["status"] == "OK"
    assert summary["rows"][-1]["constrained"] is False


def test_build_summary_payload_marks_enabled_ai_failure_as_partial_no_result():
    summary = build_summary_payload(
        config=_make_single_channel_config(),
        judgment_payload=_make_single_channel_judgment_payload(),
        ai_review={"status": "timeout", "error_message": "AI timeout"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "AI timeout"
    assert summary["rows"][0]["label"] == "SPL"
    assert summary["rows"][0]["status"] == "OK"
    assert summary["rows"][-1]["label"] == "AI分析"
    assert summary["rows"][-1]["deviation_display"] == "--"
    assert summary["rows"][-1]["channel_label"] == "--"
    assert summary["rows"][-1]["status"] == "无结果"
    assert summary["rows"][-1]["constrained"] is True


def test_build_summary_payload_preserves_ai_channel_label_on_ai_failure():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=_make_judgment_payload(),
        ai_review={
            "status": "timeout",
            "error_message": "AI timeout",
            "channel_index": 1,
        },
    )

    assert summary["overall_status"] == "no_result"
    assert summary["rows"][-1]["label"] == "AI分析"
    assert summary["rows"][-1]["channel_index"] == 1
    assert summary["rows"][-1]["channel_label"] == "CH2"
    assert summary["rows"][-1]["status"] == "无结果"


def test_build_summary_payload_marks_configured_octave_with_non_finite_actual_as_no_result():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [
                        {"freq": 1000.0, "lower": 35.0, "upper": 55.0},
                        {"freq": 2000.0, "lower": 35.0, "upper": 55.0},
                    ],
                }
            ],
        },
        judgment_payload={
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "loudness_max_sone": 5.0,
                    "loudness_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [
                        {
                            "freq": 1000.0,
                            "level": math.nan,
                            "lower": 35.0,
                            "upper": 55.0,
                            "passed": False,
                        }
                    ],
                }
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "分析结果不可用"
    assert summary["rows"][5]["label"] == "倍频程"
    assert summary["rows"][5]["deviation_value"] is None
    assert summary["rows"][5]["deviation_display"] == "--"
    assert summary["rows"][5]["channel_index"] is None
    assert summary["rows"][5]["channel_label"] == "--"
    assert summary["rows"][5]["status"] == "无结果"
    assert summary["rows"][5]["constrained"] is True


def test_build_summary_payload_treats_empty_fft_envelope_results_as_constrained_ok():
    summary = build_summary_payload(
        config=_make_single_channel_config(
            fft_band_limits=[
                {"freq": 500.0, "lower": 35.0, "upper": 55.0},
                {"freq": 1000.0, "lower": 40.0, "upper": 60.0},
            ],
            oct_band_limits=[],
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
        ai_review={"status": "not_enabled"},
    )

    fft_row = summary["rows"][4]
    assert summary["overall_status"] == "pass"
    assert summary["overall_label"] == "PASS"
    assert summary["error_message"] is None
    assert fft_row["label"] == "FFT"
    assert fft_row["deviation_value"] is None
    assert fft_row["deviation_display"] == "--"
    assert fft_row["status"] == "OK"
    assert fft_row["constrained"] is True


@pytest.mark.parametrize(
    (
        "config_key",
        "result_key",
        "available_key",
        "error_key",
        "row_index",
        "label",
        "bad_limits",
    ),
    [
        (
            "fft_band_limits",
            "fft_band_results",
            "fft_available",
            "fft_error_message",
            4,
            "FFT",
            {"bad": "shape"},
        ),
        (
            "oct_band_limits",
            "oct_band_results",
            "octave_available",
            "octave_error_message",
            5,
            "倍频程",
            {"bad": "shape"},
        ),
    ],
)
def test_build_summary_payload_marks_malformed_non_list_band_limits_as_no_result(
    config_key,
    result_key,
    available_key,
    error_key,
    row_index,
    label,
    bad_limits,
):
    summary = build_summary_payload(
        config=_make_single_channel_config(
            fft_band_limits=bad_limits if config_key == "fft_band_limits" else [],
            oct_band_limits=bad_limits if config_key == "oct_band_limits" else [],
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            **{
                available_key: False,
                error_key: None,
                result_key: [],
            },
        ),
        ai_review={"status": "not_enabled"},
    )

    band_row = summary["rows"][row_index]
    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] is not None
    assert "列表" in summary["error_message"]
    assert band_row["label"] == label
    assert band_row["deviation_value"] is None
    assert band_row["deviation_display"] == "--"
    assert band_row["status"] == "无结果"
    assert band_row["constrained"] is True


@pytest.mark.parametrize(
    ("config_key", "result_key", "row_index", "label", "empty_limits"),
    [
        (
            "fft_band_limits",
            "fft_band_results",
            4,
            "FFT",
            [{"kind": "upper", "points": []}],
        ),
        (
            "oct_band_limits",
            "oct_band_results",
            5,
            "倍频程",
            [{"kind": "upper", "points": []}],
        ),
    ],
)
def test_build_summary_payload_treats_empty_grouped_band_limits_as_unconstrained(
    config_key,
    result_key,
    row_index,
    label,
    empty_limits,
):
    summary = build_summary_payload(
        config=_make_single_channel_config(
            fft_band_limits=empty_limits if config_key == "fft_band_limits" else [],
            oct_band_limits=empty_limits if config_key == "oct_band_limits" else [],
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            octave_available=True,
            **{result_key: []},
        ),
        ai_review={"status": "not_enabled"},
    )

    band_row = summary["rows"][row_index]
    assert summary["overall_status"] == "pass"
    assert summary["overall_label"] == "PASS"
    assert summary["error_message"] is None
    assert band_row["label"] == label
    assert band_row["deviation_value"] is None
    assert band_row["deviation_display"] == "--"
    assert band_row["channel_index"] is None
    assert band_row["channel_label"] == "--"
    assert band_row["status"] == "OK"
    assert band_row["constrained"] is False


def test_build_summary_payload_uses_failed_only_fft_envelope_result_for_worst_deviation():
    summary = build_summary_payload(
        config=_make_single_channel_config(
            fft_band_limits=[
                {"freq": 500.0, "lower": 35.0, "upper": 55.0},
                {"freq": 1000.0, "lower": 40.0, "upper": 60.0},
            ],
            oct_band_limits=[],
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[
                {
                    "freq": 750.0,
                    "level": 66.0,
                    "lower": 37.5,
                    "upper": 57.5,
                    "passed": False,
                }
            ],
            oct_band_results=[],
        ),
        ai_review={"status": "not_enabled"},
    )

    fft_row = summary["rows"][4]
    assert summary["overall_status"] == "fail"
    assert summary["overall_label"] == "FAIL"
    assert summary["error_message"] is None
    assert fft_row["label"] == "FFT"
    assert fft_row["deviation_value"] == 8.5
    assert fft_row["deviation_display"] == "8.5 dB"
    assert fft_row["channel_index"] == 0
    assert fft_row["channel_label"] == "CH1"
    assert fft_row["status"] == "NG"
    assert fft_row["constrained"] is True


def test_build_summary_payload_uses_measured_center_octave_results_for_grouped_upper_only_limit():
    summary = build_summary_payload(
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 50.0},
                        {"freq": 2000.0, "limit": 50.0},
                    ],
                }
            ],
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_band_results=[],
            octave_available=True,
            octave_error_message=None,
            oct_band_results=[
                {
                    "freq": 1000.0,
                    "level": 45.0,
                    "lower": None,
                    "upper": 50.0,
                    "passed": True,
                },
                {
                    "freq": 1500.0,
                    "level": 56.5,
                    "lower": None,
                    "upper": 50.0,
                    "passed": False,
                },
                {
                    "freq": 2000.0,
                    "level": 48.0,
                    "lower": None,
                    "upper": 50.0,
                    "passed": True,
                },
            ],
        ),
        ai_review={"status": "not_enabled"},
    )

    octave_row = summary["rows"][5]
    assert summary["overall_status"] == "fail"
    assert octave_row["label"] == "倍频程"
    assert octave_row["deviation_value"] == 6.5
    assert octave_row["actual_value"] == 56.5
    assert octave_row["actual_frequency_hz"] == 1500.0
    assert octave_row["channel_label"] == "CH1"
    assert octave_row["status"] == "NG"
    assert octave_row["constrained"] is True


@pytest.mark.parametrize(
    ("config_key", "result_key", "row_index", "label"),
    [
        ("fft_band_limits", "fft_band_results", 4, "FFT"),
        ("oct_band_limits", "oct_band_results", 5, "倍频程"),
    ],
)
def test_build_summary_payload_marks_mixed_usable_and_non_finite_band_rows_as_no_result(
    config_key,
    result_key,
    row_index,
    label,
):
    summary = _build_single_channel_band_summary(
        config_key=config_key,
        result_key=result_key,
        band_results=[
            {
                "freq": 500.0,
                "level": 45.0,
                "lower": 35.0,
                "upper": 55.0,
                "passed": True,
            },
            {
                "freq": 1000.0,
                "level": math.nan,
                "lower": 35.0,
                "upper": 55.0,
                "passed": False,
            },
        ],
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "分析结果不可用"
    assert summary["rows"][row_index]["label"] == label
    assert summary["rows"][row_index]["deviation_value"] is None
    assert summary["rows"][row_index]["deviation_display"] == "--"
    assert summary["rows"][row_index]["channel_index"] is None
    assert summary["rows"][row_index]["channel_label"] == "--"
    assert summary["rows"][row_index]["status"] == "无结果"
    assert summary["rows"][row_index]["constrained"] is True


@pytest.mark.parametrize(
    ("config_key", "result_key", "row_index", "label"),
    [
        ("oct_band_limits", "oct_band_results", 5, "倍频程"),
    ],
)
def test_build_summary_payload_marks_missing_band_bounds_as_no_result(
    config_key,
    result_key,
    row_index,
    label,
):
    summary = _build_single_channel_band_summary(
        config_key=config_key,
        result_key=result_key,
        band_results=[
            {
                "freq": 500.0,
                "level": 45.0,
                "passed": True,
            }
        ],
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "分析结果不可用"
    assert summary["rows"][row_index]["label"] == label
    assert summary["rows"][row_index]["deviation_value"] is None
    assert summary["rows"][row_index]["deviation_display"] == "--"
    assert summary["rows"][row_index]["channel_index"] is None
    assert summary["rows"][row_index]["channel_label"] == "--"
    assert summary["rows"][row_index]["status"] == "无结果"
    assert summary["rows"][row_index]["constrained"] is True


def test_build_summary_payload_keeps_nearest_bin_fft_band_results_as_normal_row():
    summary = _build_single_channel_band_summary(
        config_key="fft_band_limits",
        result_key="fft_band_results",
        band_results=[
            {
                "freq": 492.1875,
                "level": 45.0,
                "lower": 35.0,
                "upper": 55.0,
                "passed": True,
            },
            {
                "freq": 984.375,
                "level": 54.0,
                "lower": 35.0,
                "upper": 55.0,
                "passed": True,
            },
        ],
    )

    assert summary["overall_status"] == "pass"
    assert summary["overall_label"] == "PASS"
    assert summary["error_message"] is None
    assert summary["rows"][4]["label"] == "FFT"
    assert summary["rows"][4]["deviation_value"] == -1.0
    assert summary["rows"][4]["deviation_display"] == "-1 dB"
    assert summary["rows"][4]["channel_index"] == 0
    assert summary["rows"][4]["channel_label"] == "CH1"
    assert summary["rows"][4]["status"] == "OK"
    assert summary["rows"][4]["constrained"] is True


def test_build_summary_payload_displays_unconstrained_acoustic_values_as_unjudged():
    summary = build_summary_payload(
        config=_make_single_channel_config(
            spl_lower_enabled=False,
            spl_upper_enabled=False,
            spl_average_lower_enabled=False,
            spl_average_upper_enabled=False,
            loudness_lower_enabled=False,
            loudness_upper_enabled=False,
            loudness_average_lower_enabled=False,
            loudness_average_upper_enabled=False,
            fft_band_limits=[],
            oct_band_limits=[],
        ),
        judgment_payload=_make_single_channel_judgment_payload(),
        ai_review={"status": "not_enabled"},
    )

    acoustic_rows = summary["rows"][:4]
    assert [row["item_key"] for row in acoustic_rows] == [
        "spl",
        "spl_average",
        "loudness",
        "loudness_average",
    ]
    assert [row["actual_display"] for row in acoustic_rows] == [
        "77.00 dB",
        "60.00 dB",
        "7.00 sone",
        "4.00 sone",
    ]
    assert all(row["status"] == "未判定" for row in acoustic_rows)
    assert all(row["constrained"] is False for row in acoustic_rows)
    assert all(row["deviation_display"] == "--" for row in acoustic_rows)
    assert summary["overall_status"] == "pass"


def test_build_summary_payload_marks_failed_unconstrained_average_measurements_no_result():
    summary = build_summary_payload(
        config=_make_single_channel_config(
            spl_average_lower_enabled=False,
            spl_average_upper_enabled=False,
            loudness_average_lower_enabled=False,
            loudness_average_upper_enabled=False,
            fft_band_limits=[],
            oct_band_limits=[],
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            spl_db=70.0,
            spl_min_db=45.0,
            spl_max_db=70.0,
            spl_pass=True,
            spl_trace_available=True,
            spl_average_db=None,
            spl_average_pass=True,
            spl_average_available=False,
            spl_average_error_message="average spl unavailable",
            loudness_min_sone=2.0,
            loudness_max_sone=5.0,
            loudness_average_sone=None,
            loudness_pass=True,
            loudness_average_pass=True,
            loudness_available=True,
            loudness_error_message="loudness backend unavailable",
            fft_band_results=[],
            oct_band_results=[],
        ),
        ai_review={"status": "not_enabled"},
    )

    assert [row["item_key"] for row in summary["rows"]] == [
        "spl",
        "spl_average",
        "loudness",
        "loudness_average",
        "fft",
        "octave",
        "stft_score",
        "ai",
    ]
    assert summary["rows"][1]["label"] == "平均SPL"
    assert summary["rows"][1]["status"] == "无结果"
    assert summary["rows"][1]["deviation_display"] == "--"
    assert summary["rows"][1]["channel_label"] == "CH1"
    assert summary["rows"][3]["label"] == "平均Loudness"
    assert summary["rows"][3]["status"] == "无结果"
    assert summary["rows"][5]["deviation_display"] == "--"
    assert summary["rows"][5]["channel_label"] == "--"
    assert summary["overall_status"] == "no_result"


def test_build_summary_payload_marks_first_selected_blocking_average_row_no_result():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [1, 0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": True,
                    "spl_average_lower_limit": 40.0,
                    "spl_average_upper_enabled": True,
                    "spl_average_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": True,
                    "spl_average_lower_limit": 40.0,
                    "spl_average_upper_enabled": True,
                    "spl_average_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": None,
                    "spl_average_pass": None,
                    "spl_average_available": False,
                    "spl_average_error_message": "ch0 average spl missing",
                    "loudness_available": True,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": None,
                    "spl_average_pass": None,
                    "spl_average_available": False,
                    "spl_average_error_message": "ch1 average spl missing",
                    "loudness_available": True,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["error_message"] == "ch1 average spl missing"
    assert summary["rows"][1]["item_key"] == "spl_average"
    assert summary["rows"][1]["status"] == "无结果"
    assert summary["rows"][1]["deviation_display"] == "--"
    assert summary["rows"][1]["channel_index"] == 1
    assert summary["rows"][1]["channel_label"] == "CH2"


def test_build_summary_payload_uses_enabled_bounds_for_average_deviation_rules():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [3, 0, 1, 2, 4],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": True,
                    "spl_average_lower_limit": 50.0,
                    "spl_average_upper_enabled": False,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": False,
                    "spl_average_upper_enabled": True,
                    "spl_average_upper_limit": 60.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 2,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": True,
                    "spl_average_lower_limit": 45.0,
                    "spl_average_upper_enabled": True,
                    "spl_average_upper_limit": 55.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 3,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": True,
                    "spl_average_lower_limit": 45.0,
                    "spl_average_upper_enabled": True,
                    "spl_average_upper_limit": 55.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 4,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": True,
                    "spl_average_lower_limit": 45.0,
                    "spl_average_upper_enabled": True,
                    "spl_average_upper_limit": 55.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": False,
                    "loudness_average_upper_enabled": False,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {"channel_index": 0, "spl_average_db": 48.0, "spl_average_pass": False, "spl_average_available": True, "loudness_available": True, "fft_band_results": [], "oct_band_results": []},
                {"channel_index": 1, "spl_average_db": 63.0, "spl_average_pass": False, "spl_average_available": True, "loudness_available": True, "fft_band_results": [], "oct_band_results": []},
                {"channel_index": 2, "spl_average_db": 45.0, "spl_average_pass": True, "spl_average_available": True, "loudness_available": True, "fft_band_results": [], "oct_band_results": []},
                {"channel_index": 3, "spl_average_db": 61.0, "spl_average_pass": False, "spl_average_available": True, "loudness_available": True, "fft_band_results": [], "oct_band_results": []},
                {"channel_index": 4, "spl_average_db": 50.0, "spl_average_pass": True, "spl_average_available": True, "loudness_available": True, "fft_band_results": [], "oct_band_results": []},
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["rows"][1]["item_key"] == "spl_average"
    assert summary["rows"][1]["status"] == "NG"
    assert summary["rows"][1]["channel_index"] == 3
    assert summary["rows"][1]["deviation_value"] == 6.0
    assert summary["rows"][1]["deviation_display"] == "6 dB"


def test_build_summary_payload_uses_canonical_sone_ranking_for_loudness_but_displays_in_selected_channel_unit():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [1, 0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": True,
                    "spl_lower_limit": 40.0,
                    "spl_upper_enabled": True,
                    "spl_upper_limit": 80.0,
                    "loudness_lower_enabled": True,
                    "loudness_unit": "sone",
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_enabled": True,
                    "loudness_upper_limit": 6.0,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 0.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 6.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_enabled": True,
                    "spl_lower_limit": 40.0,
                    "spl_upper_enabled": True,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "phon",
                    "loudness_lower_enabled": True,
                    "loudness_lower_limit": 20.0,
                    "loudness_upper_enabled": True,
                    "loudness_upper_limit": 40.0,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 20.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 6.5,
                    "loudness_average_sone": 6.5,
                    "loudness_pass": False,
                    "loudness_average_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 2.0,
                    "loudness_average_sone": 2.0,
                    "loudness_pass": False,
                    "loudness_average_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["channel_index"] == 1
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["status"] == "NG"
    assert summary["rows"][2]["deviation_value"] == pytest.approx(10.0)
    assert summary["rows"][2]["deviation_display"] == "10 phon"


def test_build_summary_payload_uses_loudness_min_for_lower_limit_deviation():
    summary = build_summary_payload(
        config=_make_single_channel_config(
            loudness_unit="sone",
            loudness_lower_limit=5.0,
            loudness_upper_limit=10.0,
        ),
        judgment_payload=_make_single_channel_judgment_payload(
            loudness_min_sone=1.0,
            loudness_max_sone=8.0,
            loudness_pass=False,
            loudness_available=True,
        ),
        ai_review={"status": "not_enabled"},
    )

    loudness_row = summary["rows"][2]
    assert summary["overall_status"] == "fail"
    assert loudness_row["deviation_value"] == 4.0
    assert loudness_row["deviation_display"] == "4 sone"
    assert loudness_row["status"] == "NG"


def test_build_summary_payload_marks_loudness_row_no_result_from_serialized_judgment():
    config = _make_config()
    config["selected_channels"] = [1, 0]
    summary = build_summary_payload(
        config=config,
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 60.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 5.0,
                    "loudness_average_sone": 4.0,
                    "loudness_pass": True,
                    "loudness_average_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 72.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 61.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": None,
                    "loudness_average_sone": None,
                    "loudness_pass": None,
                    "loudness_average_pass": None,
                    "loudness_available": False,
                    "loudness_error_message": "MoSQITo unavailable",
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "MoSQITo unavailable"
    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["deviation_display"] == "--"
    assert summary["rows"][2]["status"] == "无结果"
    assert summary["rows"][4]["constrained"] is True


def test_build_summary_payload_preserves_loudness_partial_no_result_after_backend_failure_serialization():
    config = _make_config()
    config["selected_channels"] = [1, 0]
    summary = build_summary_payload(
        config=config,
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 60.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 5.0,
                    "loudness_average_sone": 4.0,
                    "loudness_pass": True,
                    "loudness_average_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 72.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 61.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": None,
                    "loudness_average_sone": None,
                    "loudness_pass": None,
                    "loudness_average_pass": None,
                    "loudness_available": False,
                    "loudness_error_message": "backend failed",
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["rows"][2]["status"] == "无结果"
    assert summary["error_message"] == "backend failed"


def test_build_summary_payload_keeps_loudness_row_ng_when_serialized_loudness_pass_is_false_even_if_scalar_is_in_range():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": True,
                    "spl_lower_limit": 40.0,
                    "spl_upper_enabled": True,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": True,
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_enabled": True,
                    "loudness_upper_limit": 10.0,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 0.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 10.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                }
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 60.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 5.0,
                    "loudness_average_sone": 5.0,
                    "loudness_pass": False,
                    "loudness_average_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                }
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "fail"
    assert summary["overall_label"] == "FAIL"
    assert summary["error_message"] is None
    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["deviation_value"] == -5.0
    assert summary["rows"][2]["deviation_display"] == "-5 sone"
    assert summary["rows"][2]["status"] == "NG"
    assert summary["rows"][2]["channel_label"] == "--"


def test_build_summary_payload_ranks_loudness_using_unrounded_raw_canonical_deviation():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [1, 0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": True,
                    "spl_lower_limit": 40.0,
                    "spl_upper_enabled": True,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": True,
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_enabled": True,
                    "loudness_upper_limit": 10.0,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 0.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 10.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_enabled": True,
                    "spl_lower_limit": 40.0,
                    "spl_upper_enabled": True,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": True,
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_enabled": True,
                    "loudness_upper_limit": 10.0,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 0.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 10.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
            ],
        },
        judgment_payload={
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 60.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 9.9999994,
                    "loudness_average_sone": 9.9999994,
                    "loudness_pass": True,
                    "loudness_average_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "spl_trace_available": True,
                    "spl_average_db": 60.0,
                    "spl_average_pass": True,
                    "spl_average_available": True,
                    "loudness_max_sone": 9.99999949,
                    "loudness_average_sone": 9.99999949,
                    "loudness_pass": True,
                    "loudness_average_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "pass"
    assert summary["rows"][2]["label"] == "Loudness"
    assert summary["rows"][2]["channel_index"] == 1
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["deviation_value"] == -1e-06
    assert summary["rows"][2]["deviation_display"] == "-1e-06 sone"
    assert summary["rows"][4]["status"] == "OK"


def test_build_summary_payload_uses_canonical_sone_ranking_for_average_loudness_but_displays_in_selected_channel_unit():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [1, 0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": False,
                    "spl_average_upper_enabled": False,
                    "loudness_unit": "sone",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 0.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 6.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_enabled": False,
                    "spl_upper_enabled": False,
                    "spl_average_lower_enabled": False,
                    "spl_average_upper_enabled": False,
                    "loudness_unit": "phon",
                    "loudness_lower_enabled": False,
                    "loudness_upper_enabled": False,
                    "loudness_average_lower_enabled": True,
                    "loudness_average_lower_limit": 20.0,
                    "loudness_average_upper_enabled": True,
                    "loudness_average_upper_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_trace_available": True,
                    "spl_average_available": True,
                    "loudness_average_sone": 6.5,
                    "loudness_average_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_trace_available": True,
                    "spl_average_available": True,
                    "loudness_average_sone": 2.0,
                    "loudness_average_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["rows"][3]["label"] == "平均Loudness"
    assert summary["rows"][3]["channel_index"] == 1
    assert summary["rows"][3]["channel_label"] == "CH2"
    assert summary["rows"][3]["status"] == "NG"
    assert summary["rows"][3]["deviation_value"] == pytest.approx(10.0)
    assert summary["rows"][3]["deviation_display"] == "10 phon"


def test_build_summary_payload_renders_eight_no_result_rows_for_full_no_result():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=None,
        error_message="analysis window not fully captured",
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "analysis window not fully captured"
    assert [row["item_key"] for row in summary["rows"]] == [
        "spl",
        "spl_average",
        "loudness",
        "loudness_average",
        "fft",
        "octave",
        "stft_score",
        "ai",
    ]
    assert [row["label"] for row in summary["rows"]] == [
        "SPL",
        "平均SPL",
        "Loudness",
        "平均Loudness",
        "FFT",
        "倍频程",
        "频谱最低得分",
        "AI分析",
    ]
    assert len(summary["rows"]) == 8
    assert all(row["status"] == "无结果" for row in summary["rows"])
    assert all(row["deviation_display"] == "--" for row in summary["rows"])
    assert all(row["channel_label"] == "--" for row in summary["rows"])


@pytest.mark.parametrize(
    ("config_key", "result_key", "available_key", "error_key", "row_index", "label"),
    [
        ("fft_band_limits", "fft_band_results", "fft_available", "fft_error_message", 4, "FFT"),
        ("oct_band_limits", "oct_band_results", "octave_available", "octave_error_message", 5, "倍频程"),
    ],
)
def test_build_summary_payload_treats_serialized_band_availability_as_authoritative_no_result(
    config_key,
    result_key,
    available_key,
    error_key,
    row_index,
    label,
):
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "fft_band_limits": (
                        [
                            {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                            {"freq": 2000.0, "lower": -1.0, "upper": 1.0},
                        ]
                        if config_key == "fft_band_limits"
                        else []
                    ),
                    "oct_band_limits": (
                        [
                            {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                            {"freq": 2000.0, "lower": -1.0, "upper": 1.0},
                        ]
                        if config_key == "oct_band_limits"
                        else []
                    ),
                }
            ],
        },
        judgment_payload={
            "overall": "不合格",
            "limit_pass": False,
            "ai_pass": None,
            "ai_score": None,
            "analysis_window": (1.0, 3.0),
            "channel_results": [
                {
                    "channel_index": 0,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "loudness_max_sone": 5.0,
                    "loudness_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_available": available_key != "fft_available",
                    "fft_error_message": (
                        "fft golden reference unavailable"
                        if error_key == "fft_error_message"
                        else None
                    ),
                    "octave_available": available_key != "octave_available",
                    "octave_error_message": (
                        "octave golden reference unavailable"
                        if error_key == "octave_error_message"
                        else None
                    ),
                    "fft_band_results": [
                        {
                            "freq": 1000.0,
                            "level": 0.5,
                            "lower": -1.0,
                            "upper": 1.0,
                            "passed": True,
                        }
                    ],
                    "oct_band_results": [
                        {
                            "freq": 1000.0,
                            "level": 0.5,
                            "lower": -1.0,
                            "upper": 1.0,
                            "passed": True,
                        }
                    ],
                }
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] in {
        "fft golden reference unavailable",
        "octave golden reference unavailable",
    }
    assert summary["rows"][row_index]["label"] == label
    assert summary["rows"][row_index]["deviation_value"] is None
    assert summary["rows"][row_index]["deviation_display"] == "--"
    assert summary["rows"][row_index]["channel_index"] == 0
    assert summary["rows"][row_index]["channel_label"] == "CH1"
    assert summary["rows"][row_index]["status"] == "无结果"
    assert summary["rows"][row_index]["constrained"] is True


def test_build_chart_payload_returns_render_ready_series_per_channel(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    def fake_compute_spl(signal, sample_rate, weighting, time_constant):
        return float(np.mean(signal) * 10.0)

    def fake_compute_fft_spectrum(signal, sample_rate, nfft, weighting="Z"):
        if float(signal[0]) < 0.15:
            levels_db = np.array([67.0, 45.0], dtype=np.float64) + ref_db
        else:
            levels_db = np.array([58.0, 49.0], dtype=np.float64) + ref_db
        return np.array([1000.0, 2000.0], dtype=np.float64), levels_db

    def fake_compute_octave_bands(signal, sample_rate, weighting):
        if float(signal[0]) < 0.15:
            levels_db = np.array([57.0, 43.0], dtype=np.float64) + ref_db
        else:
            levels_db = np.array([53.0, 50.0], dtype=np.float64) + ref_db
        return np.array([500.0, 1000.0], dtype=np.float64), levels_db

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        fake_compute_spl,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        fake_compute_fft_spectrum,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        fake_compute_octave_bands,
    )
    def fake_loudness_trace(signal, *_args):
        return _internal_loudness_trace(
            _two_display_point_values(
                2.0 + (10.0 if float(signal[0]) > 0.15 else 0.0),
                3.0 + (10.0 if float(signal[0]) > 0.15 else 0.0),
            )
        )

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        fake_loudness_trace,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace_for_window",
        fake_loudness_trace,
    )

    config = _make_config()
    config["channels"][1]["loudness_unit"] = "phon"
    config["channels"][1]["loudness_lower_limit"] = 40.0
    config["channels"][1]["loudness_upper_limit"] = 70.0
    buffer = np.column_stack(
        [
            np.full(200, 0.1, dtype=np.float32),
            np.full(200, 0.2, dtype=np.float32),
        ]
    )

    payload = build_chart_payload(
        buffer=buffer,
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert sorted(payload) == [0, 1]
    assert set(payload[0]) == {"loudness", "spl", "fft", "octave"}
    assert "stft" not in payload[0]
    assert payload[0]["loudness"] == {
        "times_sec": [0.5, 0.54],
        "values": [2.0, 3.0],
        "unit": "sone",
        "lower_limit": 0.0,
        "upper_limit": 10.0,
        "x_axis_range_sec": [0.5, 1.5],
    }
    assert payload[1]["loudness"]["times_sec"] == [0.5, 0.54]
    assert payload[1]["loudness"]["unit"] == "phon"
    assert payload[1]["loudness"]["lower_limit"] == 40.0
    assert payload[1]["loudness"]["upper_limit"] == 70.0
    assert payload[1]["loudness"]["x_axis_range_sec"] == pytest.approx([0.5, 1.5])
    assert payload[1]["loudness"]["values"] == pytest.approx(
        [dsp.sone_to_phon(12.0), dsp.sone_to_phon(13.0)]
    )
    assert payload[0]["spl"]["times_sec"]
    assert payload[0]["spl"]["times_sec"][:3] == pytest.approx([0.55, 0.6, 0.65])
    assert payload[0]["spl"]["values_db"]
    assert payload[0]["spl"]["lower_limit_db"] == 40.0
    assert payload[0]["spl"]["upper_limit_db"] == 80.0
    assert payload[0]["fft"]["frequencies_hz"] == [1000.0, 2000.0]
    assert payload[0]["fft"]["levels_db"] == [67.0, 45.0]
    assert payload[0]["fft"]["golden_mode"] == "off"
    assert payload[0]["fft"]["limit_bands"][0] == {
        "freq_hz": 1000.0,
        "lower_db": 40.0,
        "upper_db": 60.0,
        "actual_db": 67.0,
        "passed": False,
    }
    assert payload[0]["octave"]["center_freqs_hz"] == [500.0, 1000.0]
    assert payload[0]["octave"]["levels_db"] == [57.0, 43.0]
    assert payload[0]["octave"]["golden_mode"] == "off"
    assert payload[0]["octave"]["limit_bands"][0] == {
        "freq_hz": 500.0,
        "lower_db": 35.0,
        "upper_db": 55.0,
        "actual_db": 57.0,
        "passed": False,
    }


def test_build_chart_payload_includes_fft_x_axis_scale_from_channel_config(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    config = _make_config(fft_limits=False, octave_limits=False)
    config["channels"][0]["fft_x_axis_scale"] = "log"
    config["channels"][1]["fft_x_axis_scale"] = "linear"

    payload = build_chart_payload(
        buffer=np.full((200, 2), 0.1, dtype=np.float32),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    assert payload[0]["fft"]["x_axis_scale"] == "log"
    assert payload[1]["fft"]["x_axis_scale"] == "linear"


def test_build_chart_payload_attaches_current_trace_at_channel_level(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    current_trace = {
        "times_sec": [0.0, 0.2],
        "values_a": [1.25, 1.5],
        "average_a": 1.375,
        "max_a": 1.5,
        "latest_a": 1.5,
    }

    payload = build_chart_payload(
        buffer=np.full((200, 2), 0.1, dtype=np.float32),
        config=_make_config(fft_limits=False, octave_limits=False),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        current_trace_payload=current_trace,
    )
    current_trace["average_a"] = 9.0
    current_trace["values_a"].append(9.0)

    assert payload[0]["current_trace"]["average_a"] == pytest.approx(1.375)
    assert payload[1]["current_trace"]["values_a"] == [1.25, 1.5]
    assert payload[0]["current_trace"] is not payload[1]["current_trace"]
    assert "current_trace" not in payload[0]["fft"]
    assert "current_trace" not in payload[1]["fft"]


def test_factory_chart_payload_includes_fft_limit_envelope_control_points_and_failed_points(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(100.0, 200.0, 300.0),
        fft_levels=(50.0, 95.0, 60.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {"freq": 300.0, "lower": 60.0, "upper": 90.0},
                {"freq": 100.0, "lower": 30.0, "upper": 80.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[
                {
                    "freq": 200.0,
                    "level": 95.0,
                    "lower": 45.0,
                    "upper": 85.0,
                    "passed": False,
                }
            ],
            oct_band_results=[],
        ),
    )
    fft_payload = payload[0]["fft"]

    assert fft_payload["limit_envelope"]["control_points"] == [
        {"freq_hz": 100.0, "lower_db": 30.0, "upper_db": 80.0},
        {"freq_hz": 300.0, "lower_db": 60.0, "upper_db": 90.0},
    ]
    assert fft_payload["limit_envelope"]["failed_points"] == [
        {"freq_hz": 200.0, "actual_db": 95.0, "lower_db": 45.0, "upper_db": 85.0}
    ]


def test_chart_payload_fft_limit_lines_preserve_vertical_step(monkeypatch):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0, 2500.0, 3000.0),
        fft_levels=(10.0, 10.0, 10.0, 10.0, 10.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 100.0},
                        {"freq": 2000.0, "limit": 100.0},
                        {"freq": 2000.0, "limit": 50.0},
                        {"freq": 3000.0, "limit": 50.0},
                    ],
                }
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    envelope = payload[0]["fft"]["limit_envelope"]
    assert envelope["limit_lines"] == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 100.0},
                {"freq_hz": 2000.0, "limit_db": 100.0},
                {"freq_hz": 2000.0, "limit_db": 50.0},
                {"freq_hz": 3000.0, "limit_db": 50.0},
            ],
        }
    ]
    assert envelope["control_points"] == []
    assert "line_points" not in envelope


def test_chart_payload_grouped_fft_two_sided_limits_populate_legacy_envelope_fields(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0),
        fft_levels=(50.0, 52.0, 54.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {
                    "kind": "lower",
                    "points": [
                        {"freq": 1000.0, "limit": 40.0},
                        {"freq": 2000.0, "limit": 44.0},
                    ],
                },
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 80.0},
                        {"freq": 2000.0, "limit": 84.0},
                    ],
                },
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    envelope = payload[0]["fft"]["limit_envelope"]
    assert envelope["limit_lines"] == [
        {
            "kind": "lower",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 40.0},
                {"freq_hz": 2000.0, "limit_db": 44.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 80.0},
                {"freq_hz": 2000.0, "limit_db": 84.0},
            ],
        },
    ]
    assert envelope["control_points"] == [
        {"freq_hz": 1000.0, "lower_db": 40.0, "upper_db": 80.0},
        {"freq_hz": 2000.0, "lower_db": 44.0, "upper_db": 84.0},
    ]
    assert envelope["line_points"] == [
        {"freq_hz": 1000.0, "lower_db": 40.0, "upper_db": 80.0},
        {"freq_hz": 2000.0, "lower_db": 44.0, "upper_db": 84.0},
    ]


def test_chart_payload_grouped_fft_crossing_same_kind_tables_suppress_legacy_envelope(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0),
        fft_levels=(50.0, 52.0, 54.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {
                    "kind": "lower",
                    "points": [
                        {"freq": 1000.0, "limit": 40.0},
                        {"freq": 2000.0, "limit": 60.0},
                    ],
                },
                {
                    "kind": "lower",
                    "points": [
                        {"freq": 1000.0, "limit": 50.0},
                        {"freq": 2000.0, "limit": 45.0},
                    ],
                },
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 80.0},
                        {"freq": 2000.0, "limit": 90.0},
                    ],
                },
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    envelope = payload[0]["fft"]["limit_envelope"]
    assert envelope["limit_lines"] == [
        {
            "kind": "lower",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 40.0},
                {"freq_hz": 2000.0, "limit_db": 60.0},
            ],
        },
        {
            "kind": "lower",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 50.0},
                {"freq_hz": 2000.0, "limit_db": 45.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 80.0},
                {"freq_hz": 2000.0, "limit_db": 90.0},
            ],
        },
    ]
    assert envelope["control_points"] == []
    assert "line_points" not in envelope


def test_chart_payload_grouped_fft_duplicate_step_table_suppresses_legacy_envelope(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0, 2500.0, 3000.0),
        fft_levels=(50.0, 52.0, 54.0, 56.0, 58.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {
                    "kind": "lower",
                    "points": [
                        {"freq": 1000.0, "limit": 30.0},
                        {"freq": 3000.0, "limit": 30.0},
                    ],
                },
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 70.0},
                        {"freq": 2000.0, "limit": 70.0},
                        {"freq": 2000.0, "limit": 60.0},
                        {"freq": 3000.0, "limit": 60.0},
                    ],
                },
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    envelope = payload[0]["fft"]["limit_envelope"]
    assert envelope["limit_lines"] == [
        {
            "kind": "lower",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 30.0},
                {"freq_hz": 3000.0, "limit_db": 30.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 70.0},
                {"freq_hz": 2000.0, "limit_db": 70.0},
                {"freq_hz": 2000.0, "limit_db": 60.0},
                {"freq_hz": 3000.0, "limit_db": 60.0},
            ],
        },
    ]
    assert envelope["control_points"] == []
    assert "line_points" not in envelope


def test_chart_payload_fft_empty_serialized_results_sample_grouped_limit_bands(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0, 2500.0),
        fft_levels=(50.0, 52.0, 54.0, 56.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 60.0},
                        {"freq": 2000.0, "limit": 64.0},
                    ],
                }
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_bands"] == [
        {
            "freq_hz": 1000.0,
            "lower_db": None,
            "upper_db": 60.0,
            "actual_db": 50.0,
            "passed": True,
        },
        {
            "freq_hz": 1500.0,
            "lower_db": None,
            "upper_db": 62.0,
            "actual_db": 52.0,
            "passed": True,
        },
        {
            "freq_hz": 2000.0,
            "lower_db": None,
            "upper_db": 64.0,
            "actual_db": 54.0,
            "passed": True,
        },
    ]


def test_chart_payload_fft_mixed_serialized_failures_sample_non_legacy_grouped_limit_bands(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2500.0),
        fft_levels=(50.0, 70.0, 45.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 60.0},
                        {"freq": 2000.0, "limit": 60.0},
                    ],
                }
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[
                {
                    "freq": 1500.0,
                    "level": 70.0,
                    "lower": None,
                    "upper": 60.0,
                    "passed": False,
                }
            ],
            oct_band_results=[],
        ),
    )

    fft_payload = payload[0]["fft"]
    assert fft_payload["limit_bands"] == [
        {
            "freq_hz": 1000.0,
            "lower_db": None,
            "upper_db": 60.0,
            "actual_db": 50.0,
            "passed": True,
        },
        {
            "freq_hz": 1500.0,
            "lower_db": None,
            "upper_db": 60.0,
            "actual_db": 70.0,
            "passed": False,
        },
    ]
    assert fft_payload["limit_envelope"]["limit_lines"] == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 60.0},
                {"freq_hz": 2000.0, "limit_db": 60.0},
            ],
        }
    ]
    assert fft_payload["limit_envelope"]["control_points"] == []
    assert "line_points" not in fft_payload["limit_envelope"]


def test_chart_payload_fft_failed_points_allow_one_sided_bounds(monkeypatch):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0),
        fft_levels=(45.0, 65.0, 35.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[
                {
                    "freq": 1500.0,
                    "level": 65.0,
                    "lower": None,
                    "upper": 60.0,
                    "passed": False,
                },
                {
                    "freq": 2000.0,
                    "level": 35.0,
                    "lower": 40.0,
                    "upper": None,
                    "passed": False,
                },
            ],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_envelope"]["failed_points"] == [
        {
            "freq_hz": 1500.0,
            "actual_db": 65.0,
            "lower_db": None,
            "upper_db": 60.0,
        },
        {
            "freq_hz": 2000.0,
            "actual_db": 35.0,
            "lower_db": 40.0,
            "upper_db": None,
        },
    ]


def test_chart_payload_omits_fft_when_grouped_limits_are_malformed(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_band_limits=[
                    {
                        "kind": "ceiling",
                        "points": [
                            {"freq": 1000.0, "limit": 60.0},
                            {"freq": 2000.0, "limit": 60.0},
                        ],
                    }
                ],
                oct_band_limits=[],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert "fft" not in payload[0]
    assert "FFT chart payload omitted" in caplog.text
    assert "FFT限制表第 1 项类型必须是 upper 或 lower" in caplog.text


def test_chart_payload_omits_fft_when_non_empty_legacy_limits_are_malformed(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_band_limits=[{"freq": 1000.0, "lower": 40.0}],
                oct_band_limits=[],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert "fft" not in payload[0]
    assert "FFT chart payload omitted" in caplog.text
    assert "FFT限制表第 1 项格式无效" in caplog.text


def test_factory_chart_payload_keeps_duplicate_fft_control_points_in_stable_sorted_order(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(100.0, 300.0, 400.0),
        fft_levels=(50.0, 60.0, 70.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {"freq": 400.0, "lower": 80.0, "upper": 110.0},
                {"freq": 300.0, "lower": 60.0, "upper": 90.0},
                {"freq": 100.0, "lower": 30.0, "upper": 70.0},
                {"freq": 300.0, "lower": 80.0, "upper": 100.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )
    fft_payload = payload[0]["fft"]

    assert fft_payload["limit_envelope"]["control_points"] == [
        {"freq_hz": 100.0, "lower_db": 30.0, "upper_db": 70.0},
        {"freq_hz": 300.0, "lower_db": 60.0, "upper_db": 90.0},
        {"freq_hz": 300.0, "lower_db": 80.0, "upper_db": 100.0},
        {"freq_hz": 400.0, "lower_db": 80.0, "upper_db": 110.0},
    ]


def test_factory_chart_payload_fft_off_line_points_use_collapsed_duplicate_envelope(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(100.0, 200.0, 300.0),
        fft_levels=(50.0, 12.0, 40.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[
                {"freq": 300.0, "lower": 10.0, "upper": 30.0},
                {"freq": 200.0, "lower": 8.0, "upper": 9.0},
                {"freq": 100.0, "lower": 0.0, "upper": 20.0},
                {"freq": 200.0, "lower": 1.0, "upper": 20.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    envelope = payload[0]["fft"]["limit_envelope"]
    assert envelope["control_points"] == [
        {"freq_hz": 100.0, "lower_db": 0.0, "upper_db": 20.0},
        {"freq_hz": 200.0, "lower_db": 8.0, "upper_db": 9.0},
        {"freq_hz": 200.0, "lower_db": 1.0, "upper_db": 20.0},
        {"freq_hz": 300.0, "lower_db": 10.0, "upper_db": 30.0},
    ]
    assert envelope["line_points"] == [
        {"freq_hz": 100.0, "lower_db": 0.0, "upper_db": 20.0},
        {"freq_hz": 200.0, "lower_db": 8.0, "upper_db": 9.0},
        {"freq_hz": 300.0, "lower_db": 10.0, "upper_db": 30.0},
    ]


def test_factory_chart_payload_fft_absolute_limit_envelope_is_in_chart_space(monkeypatch):
    _patch_basic_chart_backends(monkeypatch, fft_levels=(64.0, 45.0))
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            fft_band_limits=[
                {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                {"freq": 2000.0, "lower": -2.0, "upper": 2.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_envelope"]["control_points"] == [
        {"freq_hz": 1000.0, "lower_db": 59.0, "upper_db": 61.0},
        {"freq_hz": 2000.0, "lower_db": 39.0, "upper_db": 43.0},
    ]


def test_chart_payload_fft_absolute_limit_lines_apply_golden_offsets(monkeypatch):
    _patch_basic_chart_backends(monkeypatch, fft_levels=(64.0, 45.0))
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            fft_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 1.0},
                        {"freq": 2000.0, "limit": 2.0},
                    ],
                }
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_envelope"]["limit_lines"] == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 1000.0, "limit_db": 61.0},
                {"freq_hz": 2000.0, "limit_db": 43.0},
            ],
        }
    ]


def test_factory_chart_payload_fft_absolute_limit_envelope_line_points_follow_bin_sampled_golden(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(1000.0, 1500.0, 2000.0),
        fft_levels=(64.0, 82.0, 45.0),
    )
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 1500.0, 2000.0),
        fft_levels=(60.0, 80.0, 40.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            fft_band_limits=[
                {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                {"freq": 2000.0, "lower": -2.0, "upper": 2.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    envelope = payload[0]["fft"]["limit_envelope"]
    assert envelope["control_points"] == [
        {"freq_hz": 1000.0, "lower_db": 59.0, "upper_db": 61.0},
        {"freq_hz": 2000.0, "lower_db": 38.0, "upper_db": 42.0},
    ]
    assert envelope["line_points"] == [
        {"freq_hz": 1000.0, "lower_db": 59.0, "upper_db": 61.0},
        {"freq_hz": 1500.0, "lower_db": 78.5, "upper_db": 81.5},
        {"freq_hz": 2000.0, "lower_db": 38.0, "upper_db": 42.0},
    ]


def test_factory_chart_payload_fft_absolute_line_points_keep_non_bin_endpoints(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(100.0, 200.0, 300.0, 400.0),
        fft_levels=(11.0, 21.0, 31.0, 41.0),
    )
    cache = _GoldenCacheStub(
        fft_axis=(100.0, 200.0, 300.0, 400.0),
        fft_levels=(10.0, 20.0, 30.0, 40.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            fft_band_limits=[
                {"freq": 150.0, "lower": -1.0, "upper": 1.0},
                {"freq": 350.0, "lower": -3.0, "upper": 3.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_envelope"]["line_points"] == [
        {"freq_hz": 150.0, "lower_db": 14.0, "upper_db": 16.0},
        {"freq_hz": 200.0, "lower_db": 18.5, "upper_db": 21.5},
        {"freq_hz": 300.0, "lower_db": 27.5, "upper_db": 32.5},
        {"freq_hz": 350.0, "lower_db": 32.0, "upper_db": 38.0},
    ]


def test_factory_chart_payload_fft_absolute_line_points_use_collapsed_duplicate_envelope(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        fft_axis=(100.0, 150.0, 200.0, 250.0, 300.0, 350.0, 400.0),
        fft_levels=(11.0, 16.0, 21.0, 26.0, 31.0, 36.0, 41.0),
    )
    cache = _GoldenCacheStub(
        fft_axis=(100.0, 150.0, 200.0, 250.0, 300.0, 350.0, 400.0),
        fft_levels=(10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            fft_band_limits=[
                {"freq": 400.0, "lower": -8.0, "upper": 8.0},
                {"freq": 200.0, "lower": -2.0, "upper": 2.0},
                {"freq": 100.0, "lower": -1.0, "upper": 1.0},
                {"freq": 200.0, "lower": -4.0, "upper": 4.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_envelope"]["line_points"] == [
        {"freq_hz": 100.0, "lower_db": 9.0, "upper_db": 11.0},
        {"freq_hz": 150.0, "lower_db": 13.5, "upper_db": 16.5},
        {"freq_hz": 200.0, "lower_db": 18.0, "upper_db": 22.0},
        {"freq_hz": 250.0, "lower_db": 21.5, "upper_db": 28.5},
        {"freq_hz": 300.0, "lower_db": 25.0, "upper_db": 35.0},
        {"freq_hz": 350.0, "lower_db": 28.5, "upper_db": 41.5},
        {"freq_hz": 400.0, "lower_db": 32.0, "upper_db": 48.0},
    ]


def test_factory_chart_payload_fft_delta_limit_envelope_keeps_delta_bounds(monkeypatch):
    _patch_basic_chart_backends(monkeypatch, fft_levels=(64.0, 45.0))
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(66.0, 44.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="delta",
            fft_band_limits=[
                {"freq": 1000.0, "lower": -5.0, "upper": 5.0},
                {"freq": 2000.0, "lower": -4.0, "upper": 4.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["fft"]["limit_envelope"]["control_points"] == [
        {"freq_hz": 1000.0, "lower_db": -5.0, "upper_db": 5.0},
        {"freq_hz": 2000.0, "lower_db": -4.0, "upper_db": 4.0},
    ]


def test_build_chart_payload_maps_real_channel_key_to_selected_logical_column(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)
    selected_buffer = np.full((200, 1), 0.37, dtype=np.float32)
    config = _make_config(fft_limits=False, octave_limits=False)
    config["selected_channels"] = [3]
    config["channels"] = [{
        **config["channels"][0],
        "channel_index": 3,
        "fft_band_limits": [],
        "oct_band_limits": [],
    }]
    observed = {}

    def fake_spl_trace(signal, *args, **kwargs):
        observed["spl_signal"] = np.asarray(signal).copy()
        return [0.05], [float(np.mean(signal) * 100.0)]

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl_trace",
        fake_spl_trace,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([1000.0], dtype=np.float64),
            np.array([67.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([500.0], dtype=np.float64),
            np.array([57.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: ([0.0], [2.0], 2.0),
    )

    payload = build_chart_payload(
        buffer=selected_buffer,
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        channel_column_map={3: 0},
    )

    assert sorted(payload) == [3]
    assert canonical_channel_label(next(iter(payload))) == "CH4"
    assert payload[3]["spl"]["values_db"] == [37.0]
    np.testing.assert_array_equal(observed["spl_signal"], selected_buffer[:, 0])


def test_build_chart_payload_emits_null_limits_when_spl_and_loudness_bounds_are_disabled(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            spl_lower_enabled=False,
            spl_upper_enabled=False,
            loudness_lower_enabled=False,
            loudness_upper_enabled=False,
            fft_band_limits=[],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            spl_trace_available=True,
            loudness_available=True,
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert payload[0]["spl"]["lower_limit_db"] is None
    assert payload[0]["spl"]["upper_limit_db"] is None
    assert payload[0]["loudness"]["lower_limit"] is None
    assert payload[0]["loudness"]["upper_limit"] is None


def test_build_chart_payload_spl_omits_trailing_partial_tail_for_a_weighted_series():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples * 2 + 1, 0.1, dtype=np.float32).reshape(-1, 1)

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"][0]["spl_weighting"] = "A"

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [0.05, 0.1]
    assert len(payload[0]["spl"]["values_db"]) == 2


def test_build_chart_payload_spl_omits_trailing_partial_tail_for_c_weighted_series():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples * 2 + 1, 0.1, dtype=np.float32).reshape(-1, 1)

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"][0]["spl_weighting"] = "C"

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [0.05, 0.1]
    assert len(payload[0]["spl"]["values_db"]) == 2


def test_build_chart_payload_spl_exact_chunk_boundary_keeps_full_point_count():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples * 2, 0.1, dtype=np.float32).reshape(-1, 1)

    payload = build_chart_payload(
        buffer=signal,
        config=_make_config(),
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [0.05, 0.1]
    assert len(payload[0]["spl"]["values_db"]) == 2


def test_build_chart_payload_spl_shorter_than_one_chunk_returns_one_endpoint():
    sample_rate = 48000.0
    chunk_samples = int(round(sample_rate * 0.05))
    signal = np.full(chunk_samples - 1, 0.1, dtype=np.float32).reshape(-1, 1)

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"][0]["spl_weighting"] = "C"

    payload = build_chart_payload(
        buffer=signal,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.shape[0] / sample_rate),
    )

    assert payload[0]["spl"]["times_sec"] == [round(signal.shape[0] / sample_rate, 6)]
    assert len(payload[0]["spl"]["values_db"]) == 1
    assert payload[0]["spl"]["lower_limit_db"] == 40.0
    assert payload[0]["spl"]["upper_limit_db"] == 80.0


def test_build_chart_payload_spl_trace_is_stable_for_1khz_calibrator(monkeypatch):
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: ([0.0], [2.0], 2.0),
    )
    signal, sample_rate = _stable_calibrator_signal()

    payload = build_chart_payload(
        buffer=signal.reshape(-1, 1),
        config=_make_single_channel_config(
            spl_weighting="Z",
            spl_time_constant=0.125,
            fft_band_limits=[],
            oct_band_limits=[],
        ),
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, signal.size / sample_rate),
    )

    values_db = np.asarray(payload[0]["spl"]["values_db"][1:], dtype=np.float64)
    assert values_db.size >= 10
    assert float(values_db.max() - values_db.min()) <= 0.10


def test_build_chart_payload_uses_corrected_loudness_trace_for_calibrator():
    signal, sample_rate = _stable_calibrator_signal(sample_rate=48000.0)
    config = _make_config(fft_limits=False, octave_limits=False)
    config["input_device_samplerate"] = sample_rate
    config["channels"][0]["loudness_unit"] = "phon"
    buffer = signal.reshape(-1, 1)

    payload = build_chart_payload(
        buffer=buffer,
        config=config,
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.0, 3.0),
    )

    loudness = payload[0]["loudness"]
    assert set(loudness) == {"times_sec", "values", "unit", "lower_limit", "upper_limit"}
    assert loudness["unit"] == "phon"
    assert loudness["values"]
    assert max(loudness["values"]) == pytest.approx(94.0, abs=0.75)
    assert float(np.mean(loudness["values"][-25:])) == pytest.approx(94.0, abs=0.75)
    assert max(loudness["values"]) < 95.0


def test_build_chart_payload_fft_passes_weighting_to_measured_and_golden(monkeypatch):
    calls = []
    ref_db = 20.0 * math.log10(20e-6)

    def fake_fft(signal, sample_rate, nfft, weighting="Z"):
        calls.append(str(weighting))
        return (
            np.array([1000.0, 2000.0], dtype=np.float64),
            np.array([64.0, 45.0], dtype=np.float64) + ref_db,
        )

    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        fake_fft,
    )
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_weighting="C",
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    assert calls == ["C"]
    assert cache.ensure_fft_weightings == ["C"]
    assert payload[0]["fft"]["weighting"] == "C"


def test_build_chart_payload_fft_filters_non_finite_measured_and_golden_pairs(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([0.0, 1000.0, 2000.0], dtype=np.float64),
            np.array([-np.inf, 64.0, 45.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([], dtype=np.float64),
            np.array([], dtype=np.float64),
        ),
    )
    cache = _GoldenCacheStub(
        fft_axis=(0.0, 1000.0, 2000.0),
        fft_levels=(-np.inf, 60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    fft = payload[0]["fft"]
    assert fft["frequencies_hz"] == [1000.0, 2000.0]
    assert fft["levels_db"] == [64.0, 45.0]
    assert fft["golden_overlay_levels_db"] == [60.0, 41.0]


def test_build_chart_payload_fft_absolute_mode_uses_serialized_markers_and_overlay(
    monkeypatch,
):
    expected_nfft = 4096
    _patch_basic_chart_backends(
        monkeypatch,
        fft_levels=(64.0, 45.0),
    )
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_nfft=expected_nfft,
            fft_golden_mode="absolute",
            fft_show_golden_overlay=True,
            fft_band_limits=[
                {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                {"freq": 2000.0, "lower": -1.0, "upper": 1.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_error_message=None,
            fft_band_results=[
                {
                    "freq": 1000.0,
                    "level": 64.0,
                    "lower": 61.0,
                    "upper": 66.0,
                    "passed": True,
                }
            ],
            oct_band_results=[],
        ),
    )

    assert cache.bundle_calls == 1
    assert cache.ensure_fft_calls == 1
    assert cache.ensure_fft_nffts == [expected_nfft]
    fft_payload = payload[0]["fft"]
    assert fft_payload["frequencies_hz"] == [1000.0, 2000.0]
    assert fft_payload["levels_db"] == [64.0, 45.0]
    assert fft_payload["golden_mode"] == "absolute"
    assert fft_payload["golden_overlay_levels_db"] == [60.0, 41.0]
    assert "show_zero_reference" not in fft_payload
    assert fft_payload["limit_bands"] == [
        {
            "freq_hz": 1000.0,
            "lower_db": 61.0,
            "upper_db": 66.0,
            "actual_db": 64.0,
            "passed": True,
        }
    ]


def test_build_chart_payload_fft_delta_mode_serializes_main_series_in_delta_space(
    monkeypatch,
):
    expected_nfft = 4096
    _patch_basic_chart_backends(
        monkeypatch,
        fft_levels=(64.0, 45.0),
    )
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(66.0, 44.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_nfft=expected_nfft,
            fft_golden_mode="delta",
            fft_show_golden_overlay=False,
            fft_band_limits=[
                {"freq": 1000.0, "lower": -5.0, "upper": 5.0},
                {"freq": 2000.0, "lower": -5.0, "upper": 5.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            fft_error_message=None,
            fft_band_results=[
                {
                    "freq": 1000.0,
                    "level": -2.0,
                    "lower": -3.0,
                    "upper": 3.0,
                    "passed": True,
                }
            ],
            oct_band_results=[],
        ),
    )

    assert cache.ensure_fft_nffts == [expected_nfft]
    fft_payload = payload[0]["fft"]
    assert fft_payload["levels_db"] == [-2.0, 1.0]
    assert fft_payload["golden_mode"] == "delta"
    assert fft_payload["show_zero_reference"] is True
    assert "golden_overlay_levels_db" not in fft_payload
    assert fft_payload["limit_bands"] == [
        {
            "freq_hz": 1000.0,
            "lower_db": -3.0,
            "upper_db": 3.0,
            "actual_db": -2.0,
            "passed": True,
        }
    ]


def test_build_chart_payload_octave_absolute_mode_uses_serialized_markers_and_overlay(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        octave_levels=(57.0, 43.0),
    )
    cache = _GoldenCacheStub(
        octave_axis=(500.0, 1000.0),
        octave_levels=(55.0, 40.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_golden_mode="absolute",
            oct_show_golden_overlay=True,
            oct_band_limits=[
                {"freq": 500.0, "lower": -2.0, "upper": 2.0},
                {"freq": 1000.0, "lower": -2.0, "upper": 2.0},
            ],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_band_results=[],
            octave_available=True,
            octave_error_message=None,
            oct_band_results=[
                {
                    "freq": 500.0,
                    "level": 57.0,
                    "lower": 54.0,
                    "upper": 58.0,
                    "passed": True,
                }
            ],
        ),
    )

    octave_payload = payload[0]["octave"]
    assert octave_payload["center_freqs_hz"] == [500.0, 1000.0]
    assert octave_payload["levels_db"] == [57.0, 43.0]
    assert octave_payload["golden_mode"] == "absolute"
    assert octave_payload["golden_overlay_levels_db"] == [55.0, 40.0]
    assert "show_zero_reference" not in octave_payload
    assert octave_payload["limit_bands"] == [
        {
            "freq_hz": 500.0,
            "lower_db": 54.0,
            "upper_db": 58.0,
            "actual_db": 57.0,
            "passed": True,
        }
    ]


def test_build_chart_payload_octave_delta_mode_serializes_main_series_in_delta_space(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        octave_levels=(57.0, 43.0),
    )
    cache = _GoldenCacheStub(
        octave_axis=(500.0, 1000.0),
        octave_levels=(55.0, 42.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_golden_mode="delta",
            oct_show_golden_overlay=False,
            oct_band_limits=[
                {"freq": 500.0, "lower": 1.0, "upper": 3.0},
                {"freq": 1000.0, "lower": 1.0, "upper": 3.0},
            ],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_band_results=[],
            octave_available=True,
            octave_error_message=None,
            oct_band_results=[
                {
                    "freq": 500.0,
                    "level": 2.0,
                    "lower": 1.0,
                    "upper": 3.0,
                    "passed": True,
                }
            ],
        ),
    )

    octave_payload = payload[0]["octave"]
    assert octave_payload["levels_db"] == [2.0, 1.0]
    assert octave_payload["golden_mode"] == "delta"
    assert octave_payload["show_zero_reference"] is True
    assert "golden_overlay_levels_db" not in octave_payload
    assert octave_payload["limit_bands"] == [
        {
            "freq_hz": 500.0,
            "lower_db": 1.0,
            "upper_db": 3.0,
            "actual_db": 2.0,
            "passed": True,
        }
    ]


def test_chart_payload_octave_includes_limit_lines(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_band_limits=[
                {
                    "kind": "lower",
                    "points": [
                        {"freq": 500.0, "limit": 40.0},
                        {"freq": 1000.0, "limit": 42.0},
                    ],
                }
            ],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    assert payload[0]["octave"]["limit_lines"] == [
        {
            "kind": "lower",
            "points": [
                {"freq_hz": 500.0, "limit_db": 40.0},
                {"freq_hz": 1000.0, "limit_db": 42.0},
            ],
        }
    ]


def test_chart_payload_octave_limit_bands_sample_measured_centers_in_line_spans(
    monkeypatch,
):
    _patch_basic_chart_backends(
        monkeypatch,
        octave_axis=(500.0, 1000.0, 1500.0, 2000.0, 2500.0),
        octave_levels=(50.0, 52.0, 54.0, 56.0, 58.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 1000.0, "limit": 53.0},
                        {"freq": 2000.0, "limit": 55.0},
                    ],
                }
            ],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    assert payload[0]["octave"]["limit_bands"] == [
        {
            "freq_hz": 1000.0,
            "lower_db": None,
            "upper_db": 53.0,
            "actual_db": 52.0,
            "passed": True,
        },
        {
            "freq_hz": 1500.0,
            "lower_db": None,
            "upper_db": 54.0,
            "actual_db": 54.0,
            "passed": True,
        },
        {
            "freq_hz": 2000.0,
            "lower_db": None,
            "upper_db": 55.0,
            "actual_db": 56.0,
            "passed": False,
        },
    ]


def test_chart_payload_omits_octave_when_grouped_limits_are_malformed(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_band_limits=[],
                oct_band_limits=[
                    {
                        "kind": "ceiling",
                        "points": [
                            {"freq": 500.0, "limit": 55.0},
                            {"freq": 1000.0, "limit": 55.0},
                        ],
                    }
                ],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert "octave" not in payload[0]
    assert "octave chart payload omitted" in caplog.text
    assert "倍频程限制表第 1 项类型必须是 upper 或 lower" in caplog.text


def test_chart_payload_omits_octave_when_non_empty_legacy_limits_are_malformed(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_band_limits=[],
                oct_band_limits=[{"foo": "bar"}],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert "octave" not in payload[0]
    assert "octave chart payload omitted" in caplog.text
    assert "倍频程限制表第 1 项格式无效" in caplog.text


def test_chart_payload_legacy_octave_rows_use_sampled_line_semantics(monkeypatch):
    _patch_basic_chart_backends(
        monkeypatch,
        octave_axis=(500.0, 1000.0, 1500.0, 2000.0, 2500.0),
        octave_levels=(35.0, 45.0, 51.5, 53.0, 60.0),
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_band_limits=[
                {"freq": 1000.0, "lower": 40.0, "upper": 50.0},
                {"freq": 2000.0, "lower": 42.0, "upper": 52.0},
            ],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    assert payload[0]["octave"]["limit_bands"] == [
        {
            "freq_hz": 1000.0,
            "lower_db": 40.0,
            "upper_db": 50.0,
            "actual_db": 45.0,
            "passed": True,
        },
        {
            "freq_hz": 1500.0,
            "lower_db": 41.0,
            "upper_db": 51.0,
            "actual_db": 51.5,
            "passed": False,
        },
        {
            "freq_hz": 2000.0,
            "lower_db": 42.0,
            "upper_db": 52.0,
            "actual_db": 53.0,
            "passed": False,
        },
    ]


def test_chart_payload_octave_delta_limit_lines_keep_configured_offsets(monkeypatch):
    _patch_basic_chart_backends(
        monkeypatch,
        octave_levels=(57.0, 43.0),
    )
    cache = _GoldenCacheStub(
        octave_axis=(500.0, 1000.0),
        octave_levels=(55.0, 42.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_golden_mode="delta",
            oct_band_limits=[
                {
                    "kind": "upper",
                    "points": [
                        {"freq": 500.0, "limit": 2.0},
                        {"freq": 1000.0, "limit": 3.0},
                    ],
                }
            ],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_band_results=[],
            octave_available=True,
            octave_error_message=None,
            oct_band_results=[],
        ),
    )

    assert payload[0]["octave"]["limit_lines"] == [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 500.0, "limit_db": 2.0},
                {"freq_hz": 1000.0, "limit_db": 3.0},
            ],
        }
    ]


def test_build_chart_payload_omits_fft_item_when_golden_recompute_fails(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: _GoldenCacheStub(
            fft_error=RuntimeError("fft recompute failed"),
        ),
    )

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_golden_mode="absolute",
                fft_band_limits=[{"freq": 1000.0, "lower": -1.0, "upper": 1.0}],
                oct_band_limits=[],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
            judgment_payload=_make_single_channel_judgment_payload(
                fft_available=True,
                fft_error_message=None,
                fft_band_results=[
                    {
                        "freq": 1000.0,
                        "level": 64.0,
                        "lower": 61.0,
                        "upper": 66.0,
                        "passed": True,
                    }
                ],
                oct_band_results=[],
            ),
        )

    assert set(payload[0]) == {"loudness", "spl", "octave"}
    assert "fft" not in payload[0]
    assert "fft recompute failed" in caplog.text


def test_build_chart_payload_omits_only_fft_item_when_measured_recompute_fails_in_off_mode(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (_ for _ in ()).throw(
            ValueError("empty fft input")
        ),
    )

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_nfft=4096,
                fft_golden_mode="off",
                fft_band_limits=[{"freq": 1000.0, "lower": 40.0, "upper": 60.0}],
                oct_band_limits=[],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert set(payload[0]) == {"loudness", "spl", "octave"}
    assert "fft" not in payload[0]
    assert "FFT chart payload omitted" in caplog.text


def test_build_chart_payload_omits_only_octave_item_when_measured_recompute_fails_in_off_mode(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (_ for _ in ()).throw(
            ValueError("empty octave input")
        ),
    )

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_golden_mode="off",
                fft_band_limits=[
                    {"freq": 1000.0, "lower": 40.0, "upper": 60.0},
                    {"freq": 2000.0, "lower": 40.0, "upper": 60.0},
                ],
                oct_golden_mode="off",
                oct_band_limits=[
                    {"freq": 500.0, "lower": 35.0, "upper": 55.0},
                    {"freq": 1000.0, "lower": 35.0, "upper": 55.0},
                ],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert 0 in payload
    assert {"loudness", "spl", "fft"} <= set(payload[0])
    assert "octave" not in payload[0]
    assert "octave chart payload omitted" in caplog.text


def test_build_chart_payload_omits_off_mode_fft_when_judgment_marks_unavailable(
    monkeypatch,
):
    _patch_basic_chart_backends(monkeypatch)

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="off",
            fft_band_limits=[{"freq": 1000.0, "lower": 40.0, "upper": 60.0}],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=False,
            fft_error_message="invalid FFT envelope",
            fft_band_results=[],
            oct_band_results=[],
        ),
    )

    assert "fft" not in payload[0]


def test_build_chart_payload_omits_off_mode_octave_when_judgment_marks_unavailable(
    monkeypatch,
):
    _patch_basic_chart_backends(monkeypatch)

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_band_limits=[],
            oct_golden_mode="off",
            oct_band_limits=[{"freq": 500.0, "lower": 35.0, "upper": 55.0}],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_band_results=[],
            octave_available=False,
            octave_error_message="invalid octave envelope",
            oct_band_results=[],
        ),
    )

    assert "octave" not in payload[0]


def test_build_chart_payload_keeps_off_mode_fft_payload_when_overlay_load_fails(
    monkeypatch,
    caplog,
):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: _GoldenCacheStub(
            bundle_error=GoldenReferenceError("golden overlay missing"),
        ),
    )

    with caplog.at_level("WARNING", logger="app.service.analysis.review_payloads"):
        payload = build_chart_payload(
            buffer=np.full((200, 1), 0.1, dtype=np.float32),
            config=_make_single_channel_config(
                fft_golden_mode="off",
                fft_show_golden_overlay=True,
                fft_band_limits=[
                    {"freq": 1000.0, "lower": 40.0, "upper": 60.0},
                    {"freq": 2000.0, "lower": 40.0, "upper": 60.0},
                ],
                oct_band_limits=[],
            ),
            sample_rate=100.0,
            mic_cal={},
            analysis_window=(0.0, 2.0),
        )

    assert payload[0]["fft"]["golden_mode"] == "off"
    assert payload[0]["fft"]["levels_db"] == [67.0, 45.0]
    assert "golden_overlay_levels_db" not in payload[0]["fft"]
    assert "show_zero_reference" not in payload[0]["fft"]
    assert "golden overlay missing" in caplog.text


def test_build_chart_payload_reuses_one_golden_bundle_across_fft_and_octave(
    monkeypatch,
):
    _patch_basic_chart_backends(monkeypatch)
    cache = _GoldenCacheStub(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
        octave_axis=(500.0, 1000.0),
        octave_levels=(55.0, 40.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_band_limits=[{"freq": 1000.0, "lower": -1.0, "upper": 1.0}],
            oct_golden_mode="absolute",
            oct_band_limits=[{"freq": 500.0, "lower": -2.0, "upper": 2.0}],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        judgment_payload=_make_single_channel_judgment_payload(
            fft_available=True,
            octave_available=True,
            fft_band_results=[
                {
                    "freq": 1000.0,
                    "level": 64.0,
                    "lower": 61.0,
                    "upper": 66.0,
                    "passed": True,
                }
            ],
            oct_band_results=[
                {
                    "freq": 500.0,
                    "level": 57.0,
                    "lower": 54.0,
                    "upper": 58.0,
                    "passed": True,
                }
            ],
        ),
    )

    assert cache.bundle_calls == 1
    assert cache.ensure_fft_calls == 1
    assert cache.ensure_octave_calls == 1


def test_build_chart_payload_passes_runtime_resample_option_to_golden_loader(
    monkeypatch,
):
    _patch_basic_chart_backends(monkeypatch)
    captured = []

    class CapturingGoldenCache(_GoldenCacheStub):
        def bundle_for_channel(self, **kwargs):
            captured.append(kwargs["allow_runtime_resample"])
            return super().bundle_for_channel(**kwargs)

    cache = CapturingGoldenCache(
        fft_axis=(1000.0, 2000.0),
        fft_levels=(60.0, 41.0),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.GoldenReferenceCache",
        lambda: cache,
    )

    build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=_make_single_channel_config(
            fft_golden_mode="absolute",
            fft_band_limits=[
                {"freq": 1000.0, "lower": -1.0, "upper": 1.0},
                {"freq": 2000.0, "lower": -1.0, "upper": 1.0},
            ],
            oct_band_limits=[],
        ),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
        allow_golden_runtime_resample=True,
    )

    assert captured == [True]


def test_build_chart_payload_keeps_loudness_payload_shape_with_time_varying_trace(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([1000.0], dtype=np.float64),
            np.array([67.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([500.0], dtype=np.float64),
            np.array([57.0], dtype=np.float64) + ref_db,
        ),
    )
    captured_loudness = []

    def fake_loudness_for_window(signal, sample_rate, start_sec, end_sec):
        captured_loudness.append(
            {
                "signal": np.asarray(signal, dtype=np.float64).copy(),
                "sample_rate": sample_rate,
                "window": (start_sec, end_sec),
            }
        )
        return _internal_loudness_trace(_two_display_point_values(2.0, 3.0))

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace_for_window",
        fake_loudness_for_window,
    )

    payload = build_chart_payload(
        buffer=np.column_stack(
            [
                np.full(200, 0.1, dtype=np.float32),
                np.full(200, 0.2, dtype=np.float32),
            ]
        ),
        config=_make_config(),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert payload[0]["loudness"] == {
        "times_sec": [0.5, 0.54],
        "values": [2.0, 3.0],
        "unit": "sone",
        "lower_limit": 0.0,
        "upper_limit": 10.0,
        "x_axis_range_sec": [0.5, 1.5],
    }
    np.testing.assert_allclose(captured_loudness[0]["signal"], np.full(200, 0.1))
    assert captured_loudness[0]["sample_rate"] == 100.0
    assert captured_loudness[0]["window"] == pytest.approx((0.5, 1.5))


def test_build_chart_payload_reuses_valid_internal_loudness_trace_cache_as_display_trace(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: (_ for _ in ()).throw(
            AssertionError("cache expected")
        ),
    )

    payload = build_chart_payload(
        buffer=np.column_stack(
            [
                np.full(200, 0.1, dtype=np.float32),
                np.full(200, 0.2, dtype=np.float32),
            ]
        ),
        config=_make_config(),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 0.58),
        loudness_trace_cache={
            0: _valid_loudness_cache_entry(np.arange(1.0, 14.0, dtype=np.float64)),
            1: _valid_loudness_cache_entry(np.linspace(24.0, 30.0, 13, dtype=np.float64)),
        },
    )

    assert payload[0]["loudness"] == {
        "times_sec": [0.5, 0.54, 0.58],
        "values": [1.0, 7.0, 13.0],
        "unit": "sone",
        "lower_limit": 0.0,
        "upper_limit": 10.0,
        "x_axis_range_sec": [0.5, 0.58],
    }
    assert payload[1]["loudness"]["values"] == [24.0, 27.0, 30.0]
    assert payload[1]["loudness"]["x_axis_range_sec"] == pytest.approx([0.5, 0.58])


def test_build_chart_payload_reuses_loudness_trace_cache_with_phon_unit(monkeypatch):
    _patch_basic_chart_backends(monkeypatch)
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: (_ for _ in ()).throw(
            AssertionError("cache expected")
        ),
    )
    config = _make_config()
    config["channels"][0]["loudness_unit"] = "phon"

    payload = build_chart_payload(
        buffer=np.column_stack(
            [
                np.full(200, 0.1, dtype=np.float32),
                np.full(200, 0.2, dtype=np.float32),
            ]
        ),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 0.58),
        loudness_trace_cache={
            0: _valid_loudness_cache_entry(np.linspace(42.0, 48.0, 13, dtype=np.float64)),
            1: _valid_loudness_cache_entry(np.linspace(24.0, 30.0, 13, dtype=np.float64)),
        },
    )

    assert payload[0]["loudness"]["times_sec"] == [0.5, 0.54, 0.58]
    assert payload[0]["loudness"]["values"] == pytest.approx(
        [dsp.sone_to_phon(42.0), dsp.sone_to_phon(45.0), dsp.sone_to_phon(48.0)]
    )
    assert payload[0]["loudness"]["unit"] == "phon"
    assert payload[0]["loudness"]["lower_limit"] == 0.0
    assert payload[0]["loudness"]["upper_limit"] == 10.0


@pytest.mark.parametrize(
    "mutate_cache_entry",
    [
        pytest.param(lambda entry: entry.pop("algorithm"), id="missing_algorithm"),
        pytest.param(lambda entry: entry.update({"algorithm": "legacy_rolling"}), id="wrong_algorithm"),
        pytest.param(lambda entry: entry.pop("internal_rate_hz"), id="missing_rate"),
        pytest.param(lambda entry: entry.update({"internal_rate_hz": 25.0}), id="wrong_rate"),
        pytest.param(lambda entry: entry.update({"times_sec": [], "values_sone": []}), id="empty_arrays"),
        pytest.param(lambda entry: entry.update({"values_sone": entry["values_sone"][:-1]}), id="mismatched_lengths"),
        pytest.param(lambda entry: entry["times_sec"].__setitem__(1, float("nan")), id="nonfinite_time"),
        pytest.param(lambda entry: entry["values_sone"].__setitem__(1, float("inf")), id="nonfinite_value"),
        pytest.param(lambda entry: entry["values_sone"].__setitem__(1, -0.1), id="negative_value"),
        pytest.param(lambda entry: entry["times_sec"].__setitem__(1, entry["times_sec"][0]), id="nonincreasing_time"),
        pytest.param(
            lambda entry: entry.update(
                {"times_sec": [index * 0.5 for index in range(len(entry["times_sec"]))]}
            ),
            id="wrong_cadence",
        ),
        pytest.param(
            lambda entry: entry.update(
                {"times_sec": [time_sec + 0.25 for time_sec in entry["times_sec"]]}
            ),
            id="nonzero_start",
        ),
    ],
)
def test_build_chart_payload_falls_back_when_loudness_trace_cache_is_invalid(
    monkeypatch,
    mutate_cache_entry,
):
    _patch_basic_chart_backends(monkeypatch)
    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"] = [config["channels"][0]]
    calls = []

    def fake_compute_loudness_trace(signal, sample_rate):
        calls.append((signal.copy(), sample_rate))
        return _internal_loudness_trace(_two_display_point_values(2.0, 3.0))

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        fake_compute_loudness_trace,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace_for_window",
        lambda signal, sample_rate, _start_sec, _end_sec: fake_compute_loudness_trace(
            signal,
            sample_rate,
        ),
    )
    stale_cache_entry = _valid_loudness_cache_entry(
        np.linspace(42.0, 43.0, 151, dtype=np.float64)
    )
    mutate_cache_entry(stale_cache_entry)

    payload = build_chart_payload(
        buffer=np.column_stack(
            [
                np.full(200, 0.1, dtype=np.float32),
                np.full(200, 0.2, dtype=np.float32),
            ]
        ),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
        loudness_trace_cache={
            0: stale_cache_entry,
        },
    )

    assert len(calls) == 1
    assert payload[0]["loudness"]["times_sec"] == [0.5, 0.54]
    assert payload[0]["loudness"]["values"] == [2.0, 3.0]


def test_build_chart_payload_omits_loudness_for_channel_when_recompute_fails(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([1000.0], dtype=np.float64),
            np.array([67.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([500.0], dtype=np.float64),
            np.array([57.0], dtype=np.float64) + ref_db,
        ),
    )

    def fake_compute_loudness_trace(signal, sample_rate):
        if float(signal[0]) > 0.15:
            raise RuntimeError("loudness chart failed")
        return _internal_loudness_trace(_two_display_point_values(2.0, 3.0))

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        fake_compute_loudness_trace,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace_for_window",
        lambda signal, sample_rate, _start_sec, _end_sec: fake_compute_loudness_trace(
            signal,
            sample_rate,
        ),
    )

    payload = build_chart_payload(
        buffer=np.column_stack(
            [
                np.full(200, 0.1, dtype=np.float32),
                np.full(200, 0.2, dtype=np.float32),
            ]
        ),
        config=_make_config(),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert payload[0]["loudness"]["values"] == [2.0, 3.0]
    assert "loudness" not in payload[1]
    assert set(payload[1]) == {"spl", "fft", "octave"}


def test_build_chart_payload_omits_loudness_when_serialized_judgment_marks_channel_unavailable(
    monkeypatch,
):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([1000.0], dtype=np.float64),
            np.array([67.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([500.0], dtype=np.float64),
            np.array([57.0], dtype=np.float64) + ref_db,
        ),
    )
    def fake_loudness_trace(signal, *_args):
        return _internal_loudness_trace(_two_display_point_values(2.0, 3.0))

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        fake_loudness_trace,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace_for_window",
        fake_loudness_trace,
    )

    judgment_payload = _make_judgment_payload()
    judgment_payload["channel_results"][1]["loudness_max_sone"] = None
    judgment_payload["channel_results"][1]["loudness_pass"] = None
    judgment_payload["channel_results"][1]["loudness_available"] = False
    judgment_payload["channel_results"][1]["loudness_error_message"] = "MoSQITo unavailable"

    payload = build_chart_payload(
        buffer=np.column_stack(
            [
                np.full(200, 0.1, dtype=np.float32),
                np.full(200, 0.2, dtype=np.float32),
            ]
        ),
        config=_make_config(),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
        judgment_payload=judgment_payload,
    )

    assert payload[0]["loudness"]["values"] == [2.0, 3.0]
    assert "loudness" not in payload[1]
    assert set(payload[1]) == {"spl", "fft", "octave"}


def test_build_chart_payload_octave_filters_non_finite_levels_without_axis_mismatch(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([1000.0], dtype=np.float64),
            np.array([67.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([500.0, 1000.0, 2000.0], dtype=np.float64),
            np.array([57.0, np.nan, 44.0], dtype=np.float64) + ref_db,
        ),
    )

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"] = [{
        "channel_index": 0,
        "spl_lower_limit": 40.0,
        "spl_upper_limit": 80.0,
        "fft_band_limits": [],
        "oct_band_limits": [
            {"freq": 500.0, "lower": 35.0, "upper": 55.0},
            {"freq": 2000.0, "lower": 30.0, "upper": 50.0},
        ],
    }]

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    octave = payload[0]["octave"]
    assert octave["center_freqs_hz"] == [500.0, 2000.0]
    assert octave["levels_db"] == [57.0, 44.0]
    assert len(octave["center_freqs_hz"]) == len(octave["levels_db"])
    assert octave["limit_bands"] == [
        {
            "freq_hz": 500.0,
            "lower_db": 35.0,
            "upper_db": 55.0,
            "actual_db": 57.0,
            "passed": False,
        },
        {
            "freq_hz": 2000.0,
            "lower_db": 30.0,
            "upper_db": 50.0,
            "actual_db": 44.0,
            "passed": True,
        },
    ]


def test_build_chart_payload_octave_omits_limit_band_when_configured_bin_level_is_non_finite(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft, weighting="Z": (
            np.array([1000.0], dtype=np.float64),
            np.array([67.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_octave_bands",
        lambda signal, sample_rate, weighting: (
            np.array([500.0, 1000.0, 2000.0], dtype=np.float64),
            np.array([57.0, np.nan, 44.0], dtype=np.float64) + ref_db,
        ),
    )

    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"] = [{
        "channel_index": 0,
        "spl_lower_limit": 40.0,
        "spl_upper_limit": 80.0,
        "fft_band_limits": [],
        "oct_band_limits": [
            {"freq": 900.0, "lower": 35.0, "upper": 55.0},
            {"freq": 1100.0, "lower": 35.0, "upper": 55.0},
        ],
    }]

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.0, 2.0),
    )

    octave = payload[0]["octave"]
    assert octave["center_freqs_hz"] == [500.0, 2000.0]
    assert octave["levels_db"] == [57.0, 44.0]
    assert octave["limit_bands"] == []


@pytest.mark.parametrize("stft_nfft", [512, 4096])
def test_build_chart_payload_fft_point_count_matches_fft_nfft(
    stft_nfft,
):
    fft_nfft = 128
    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"] = [
        {
            **config["channels"][0],
            "stft_nfft": stft_nfft,
            "fft_nfft": fft_nfft,
        }
    ]

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert len(payload[0]["fft"]["frequencies_hz"]) == fft_nfft // 2 + 1


def test_build_chart_payload_fft_point_count_is_independent_of_stft_nfft():
    fft_nfft = 128
    small_nfft_config = _make_config()
    small_nfft_config["selected_channels"] = [0]
    small_nfft_config["channels"] = [
        {
            **small_nfft_config["channels"][0],
            "stft_nfft": 512,
            "fft_nfft": fft_nfft,
        }
    ]

    large_nfft_config = _make_config()
    large_nfft_config["selected_channels"] = [0]
    large_nfft_config["channels"] = [
        {
            **large_nfft_config["channels"][0],
            "stft_nfft": 4096,
            "fft_nfft": fft_nfft,
        }
    ]

    buffer = np.full((200, 1), 0.1, dtype=np.float32)

    small_nfft_payload = build_chart_payload(
        buffer=buffer,
        config=small_nfft_config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )
    large_nfft_payload = build_chart_payload(
        buffer=buffer,
        config=large_nfft_config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert len(small_nfft_payload[0]["fft"]["frequencies_hz"]) == fft_nfft // 2 + 1
    assert len(large_nfft_payload[0]["fft"]["frequencies_hz"]) == fft_nfft // 2 + 1
    assert (
        len(small_nfft_payload[0]["fft"]["frequencies_hz"])
        == len(large_nfft_payload[0]["fft"]["frequencies_hz"])
    )


def test_build_chart_payload_fft_uses_selected_analysis_slice_not_full_buffer():
    sample_rate = 100.0
    lead = 4.0 * np.sin(2 * np.pi * 2.0 * (np.arange(50) / sample_rate))
    middle = 1.0 * np.sin(2 * np.pi * 10.0 * (np.arange(100) / sample_rate))
    tail = np.zeros(50, dtype=np.float64)
    buffer = np.concatenate([lead, middle, tail]).astype(np.float32).reshape(-1, 1)

    payload = build_chart_payload(
        buffer=buffer,
        config=_make_config(),
        sample_rate=sample_rate,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    frequencies = np.asarray(payload[0]["fft"]["frequencies_hz"], dtype=np.float64)
    levels = np.asarray(payload[0]["fft"]["levels_db"], dtype=np.float64)
    peak_frequency = frequencies[int(np.argmax(levels))]

    assert peak_frequency == pytest.approx(10.0, abs=1.0)
