import math

import numpy as np
import pytest

from app.service.analysis.review_payloads import build_chart_payload, build_summary_payload


def _make_config(*, fft_limits=True, octave_limits=True):
    def _channel(channel_index: int):
        return {
            "channel_index": channel_index,
            "spl_lower_limit": 40.0,
            "spl_upper_limit": 80.0,
            "fft_band_limits": (
                [{"freq": 1000.0, "lower": 40.0, "upper": 60.0}]
                if fft_limits
                else []
            ),
            "oct_band_limits": (
                [{"freq": 500.0, "lower": 35.0, "upper": 55.0}]
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
                    "fft_band_results": band_results if result_key == "fft_band_results" else [],
                    "oct_band_results": band_results if result_key == "oct_band_results" else [],
                }
            ],
        },
        ai_review={"status": "not_enabled"},
    )


def test_build_summary_payload_uses_worst_case_deviation_across_channels():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=_make_judgment_payload(),
        ai_review={"status": "success", "threshold": 0.8, "score": 0.93},
    )

    assert summary["overall_status"] == "fail"
    assert summary["overall_label"] == "FAIL"
    assert [row["label"] for row in summary["rows"]] == ["SPL", "FFT", "倍频程", "AI分析"]
    assert summary["rows"][0]["label"] == "SPL"
    assert summary["rows"][0]["item_key"] == "spl"
    assert summary["rows"][0]["deviation_value"] == 6.0
    assert summary["rows"][0]["deviation_display"] == "6 dB"
    assert summary["rows"][0]["channel_index"] == 1
    assert summary["rows"][0]["channel_label"] == "CH2"
    assert summary["rows"][0]["status"] == "NG"
    assert summary["rows"][0]["constrained"] is True


def test_build_summary_payload_handles_bounded_pass_margin_and_exact_boundary():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=_make_judgment_payload(),
        ai_review={"status": "success", "threshold": 0.8, "score": 0.93},
    )

    assert summary["rows"][1]["label"] == "FFT"
    assert summary["rows"][1]["deviation_value"] == -3.0
    assert summary["rows"][1]["deviation_display"] == "-3 dB"
    assert summary["rows"][1]["channel_label"] == "CH1"
    assert summary["rows"][1]["status"] == "OK"
    assert summary["rows"][2]["label"] == "倍频程"
    assert summary["rows"][2]["deviation_value"] == 0.0
    assert summary["rows"][2]["deviation_display"] == "0 dB"
    assert summary["rows"][2]["channel_label"] == "CH1"
    assert summary["rows"][2]["status"] == "OK"


def test_build_summary_payload_renders_unconstrained_rows_and_ai_channel_fallback():
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
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 71.0,
                    "spl_pass": True,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "success", "channel_index": None},
    )

    assert summary["overall_status"] == "pass"
    assert summary["rows"][1]["label"] == "FFT"
    assert summary["rows"][1]["deviation_value"] is None
    assert summary["rows"][1]["deviation_display"] == "--"
    assert summary["rows"][1]["channel_index"] is None
    assert summary["rows"][1]["channel_label"] == "--"
    assert summary["rows"][1]["status"] == "OK"
    assert summary["rows"][1]["constrained"] is False
    assert summary["rows"][2]["label"] == "倍频程"
    assert summary["rows"][2]["status"] == "OK"
    assert summary["rows"][2]["deviation_display"] == "--"
    assert summary["rows"][3]["label"] == "AI分析"
    assert summary["rows"][3]["channel_label"] == "--"
    assert summary["rows"][3]["status"] == "OK"
    assert summary["rows"][3]["constrained"] is False


@pytest.mark.parametrize("status", ["not_enabled", "not_configured", "disabled", "skipped"])
def test_build_summary_payload_treats_not_enabled_ai_statuses_as_unconstrained_even_with_threshold(status):
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=_make_judgment_payload(),
        ai_review={
            "status": status,
            "threshold": 0.8,
            "score": None,
            "channel_index": 1,
            "error_message": "should be ignored",
        },
    )

    assert summary["overall_status"] == "fail"
    assert summary["overall_label"] == "FAIL"
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
        config=_make_config(),
        judgment_payload=_make_judgment_payload(),
        ai_review={"status": "timeout", "error_message": "AI timeout"},
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "AI timeout"
    assert summary["rows"][0]["label"] == "SPL"
    assert summary["rows"][0]["status"] == "NG"
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
                    "oct_band_limits": [{"freq": 1000.0, "lower": 35.0, "upper": 55.0}],
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
    assert summary["rows"][2]["label"] == "倍频程"
    assert summary["rows"][2]["deviation_value"] is None
    assert summary["rows"][2]["deviation_display"] == "--"
    assert summary["rows"][2]["channel_index"] is None
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["status"] == "无结果"
    assert summary["rows"][2]["constrained"] is True


@pytest.mark.parametrize(
    ("config_key", "result_key", "row_index", "label"),
    [
        ("fft_band_limits", "fft_band_results", 1, "FFT"),
        ("oct_band_limits", "oct_band_results", 2, "倍频程"),
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
        ("fft_band_limits", "fft_band_results", 1, "FFT"),
        ("oct_band_limits", "oct_band_results", 2, "倍频程"),
    ],
)
def test_build_summary_payload_marks_mixed_usable_and_missing_band_rows_as_no_result(
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
    assert summary["rows"][1]["label"] == "FFT"
    assert summary["rows"][1]["deviation_value"] == -1.0
    assert summary["rows"][1]["deviation_display"] == "-1 dB"
    assert summary["rows"][1]["channel_index"] == 0
    assert summary["rows"][1]["channel_label"] == "CH1"
    assert summary["rows"][1]["status"] == "OK"
    assert summary["rows"][1]["constrained"] is True


def test_build_summary_payload_renders_four_no_result_rows_for_full_no_result():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=None,
        error_message="analysis window not fully captured",
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "analysis window not fully captured"
    assert [row["label"] for row in summary["rows"]] == ["SPL", "FFT", "倍频程", "AI分析"]
    assert len(summary["rows"]) == 4
    assert all(row["status"] == "无结果" for row in summary["rows"])
    assert all(row["deviation_display"] == "--" for row in summary["rows"])
    assert all(row["channel_label"] == "--" for row in summary["rows"])


def test_build_chart_payload_returns_render_ready_series_per_channel(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    def fake_compute_spl(signal, sample_rate, weighting, time_constant):
        return float(np.mean(signal) * 10.0)

    def fake_compute_fft_spectrum(signal, sample_rate, nfft):
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

    buffer = np.column_stack(
        [
            np.full(200, 0.1, dtype=np.float32),
            np.full(200, 0.2, dtype=np.float32),
        ]
    )

    payload = build_chart_payload(
        buffer=buffer,
        config=_make_config(),
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert sorted(payload) == [0, 1]
    assert set(payload[0]) == {"spl", "fft", "octave"}
    assert "stft" not in payload[0]
    assert payload[0]["spl"]["times_sec"]
    assert payload[0]["spl"]["values_db"]
    assert payload[0]["spl"]["lower_limit_db"] == 40.0
    assert payload[0]["spl"]["upper_limit_db"] == 80.0
    assert payload[0]["fft"]["frequencies_hz"] == [1000.0, 2000.0]
    assert payload[0]["fft"]["levels_db"] == [67.0, 45.0]
    assert payload[0]["fft"]["limit_bands"][0] == {
        "freq_hz": 1000.0,
        "lower_db": 40.0,
        "upper_db": 60.0,
        "actual_db": 67.0,
        "passed": False,
    }
    assert payload[0]["octave"]["center_freqs_hz"] == [500.0, 1000.0]
    assert payload[0]["octave"]["levels_db"] == [57.0, 43.0]
    assert payload[0]["octave"]["limit_bands"][0] == {
        "freq_hz": 500.0,
        "lower_db": 35.0,
        "upper_db": 55.0,
        "actual_db": 57.0,
        "passed": False,
    }


def test_build_chart_payload_octave_filters_non_finite_levels_without_axis_mismatch(monkeypatch):
    ref_db = 20.0 * math.log10(20e-6)

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_spl",
        lambda signal, sample_rate, weighting, time_constant: 70.0,
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_fft_spectrum",
        lambda signal, sample_rate, nfft: (
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
        lambda signal, sample_rate, nfft: (
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
            {"freq": 1000.0, "lower": 35.0, "upper": 55.0},
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
