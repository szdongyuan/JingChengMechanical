import math

import numpy as np
import pytest

from app.service.analysis import dsp
from app.service.analysis.review_payloads import build_chart_payload, build_summary_payload


def _make_config(*, fft_limits=True, octave_limits=True):
    def _channel(channel_index: int):
        return {
            "channel_index": channel_index,
            "spl_lower_limit": 40.0,
            "spl_upper_limit": 80.0,
            "loudness_unit": "sone",
            "loudness_lower_limit": 0.0,
            "loudness_upper_limit": 10.0,
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
                "loudness_max_sone": 7.0,
                "loudness_pass": True,
                "loudness_available": True,
                "loudness_error_message": None,
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
                "loudness_max_sone": 12.0,
                "loudness_pass": False,
                "loudness_available": True,
                "loudness_error_message": None,
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


def test_build_summary_payload_uses_worst_case_deviation_across_channels():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=_make_judgment_payload(),
        ai_review={"status": "success", "threshold": 0.8, "score": 0.93},
    )

    assert summary["overall_status"] == "fail"
    assert summary["overall_label"] == "FAIL"
    assert [row["label"] for row in summary["rows"]] == ["SPL", "Loudness", "FFT", "倍频程", "AI分析"]
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

    assert summary["rows"][1]["label"] == "Loudness"
    assert summary["rows"][1]["deviation_value"] == 2.0
    assert summary["rows"][1]["deviation_display"] == "2 sone"
    assert summary["rows"][1]["channel_label"] == "CH2"
    assert summary["rows"][1]["status"] == "NG"
    assert summary["rows"][2]["label"] == "FFT"
    assert summary["rows"][2]["deviation_value"] == -3.0
    assert summary["rows"][2]["deviation_display"] == "-3 dB"
    assert summary["rows"][2]["channel_label"] == "CH1"
    assert summary["rows"][2]["status"] == "OK"
    assert summary["rows"][3]["label"] == "倍频程"
    assert summary["rows"][3]["deviation_value"] == 0.0
    assert summary["rows"][3]["deviation_display"] == "0 dB"
    assert summary["rows"][3]["channel_label"] == "CH1"
    assert summary["rows"][3]["status"] == "OK"


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
                    "loudness_max_sone": 5.0,
                    "loudness_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 71.0,
                    "spl_pass": True,
                    "loudness_max_sone": 6.0,
                    "loudness_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "success", "channel_index": None},
    )

    assert summary["overall_status"] == "pass"
    assert summary["rows"][1]["label"] == "Loudness"
    assert summary["rows"][1]["status"] == "OK"
    assert summary["rows"][1]["constrained"] is True
    assert summary["rows"][2]["label"] == "FFT"
    assert summary["rows"][2]["deviation_value"] is None
    assert summary["rows"][2]["deviation_display"] == "--"
    assert summary["rows"][2]["channel_index"] is None
    assert summary["rows"][2]["channel_label"] == "--"
    assert summary["rows"][2]["status"] == "OK"
    assert summary["rows"][2]["constrained"] is False
    assert summary["rows"][3]["label"] == "倍频程"
    assert summary["rows"][3]["status"] == "OK"
    assert summary["rows"][3]["deviation_display"] == "--"
    assert summary["rows"][4]["label"] == "AI分析"
    assert summary["rows"][4]["channel_label"] == "--"
    assert summary["rows"][4]["status"] == "OK"
    assert summary["rows"][4]["constrained"] is False


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
    assert summary["rows"][3]["label"] == "倍频程"
    assert summary["rows"][3]["deviation_value"] is None
    assert summary["rows"][3]["deviation_display"] == "--"
    assert summary["rows"][3]["channel_index"] is None
    assert summary["rows"][3]["channel_label"] == "--"
    assert summary["rows"][3]["status"] == "无结果"
    assert summary["rows"][3]["constrained"] is True


@pytest.mark.parametrize(
    ("config_key", "result_key", "row_index", "label"),
    [
        ("fft_band_limits", "fft_band_results", 2, "FFT"),
        ("oct_band_limits", "oct_band_results", 3, "倍频程"),
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
        ("fft_band_limits", "fft_band_results", 2, "FFT"),
        ("oct_band_limits", "oct_band_results", 3, "倍频程"),
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
    assert summary["rows"][2]["label"] == "FFT"
    assert summary["rows"][2]["deviation_value"] == -1.0
    assert summary["rows"][2]["deviation_display"] == "-1 dB"
    assert summary["rows"][2]["channel_index"] == 0
    assert summary["rows"][2]["channel_label"] == "CH1"
    assert summary["rows"][2]["status"] == "OK"
    assert summary["rows"][2]["constrained"] is True


def test_build_summary_payload_uses_canonical_sone_ranking_for_loudness_but_displays_in_selected_channel_unit():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0, 1],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_limit": 6.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "phon",
                    "loudness_lower_limit": 20.0,
                    "loudness_upper_limit": 40.0,
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
                    "loudness_max_sone": 6.5,
                    "loudness_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "loudness_max_sone": 2.0,
                    "loudness_pass": False,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
            ],
        },
        ai_review={"status": "not_enabled"},
    )

    assert summary["rows"][1]["label"] == "Loudness"
    assert summary["rows"][1]["channel_index"] == 1
    assert summary["rows"][1]["channel_label"] == "CH2"
    assert summary["rows"][1]["status"] == "NG"
    assert summary["rows"][1]["deviation_value"] == pytest.approx(10.0)
    assert summary["rows"][1]["deviation_display"] == "10 phon"


def test_build_summary_payload_marks_loudness_row_no_result_from_serialized_judgment():
    summary = build_summary_payload(
        config=_make_config(),
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
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 72.0,
                    "spl_pass": True,
                    "loudness_max_sone": None,
                    "loudness_pass": None,
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
    assert summary["rows"][1]["label"] == "Loudness"
    assert summary["rows"][1]["deviation_display"] == "--"
    assert summary["rows"][1]["status"] == "无结果"
    assert summary["rows"][1]["constrained"] is True


def test_build_summary_payload_preserves_loudness_partial_no_result_after_backend_failure_serialization():
    summary = build_summary_payload(
        config=_make_config(),
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
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 72.0,
                    "spl_pass": True,
                    "loudness_max_sone": None,
                    "loudness_pass": None,
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
    assert summary["rows"][1]["status"] == "无结果"
    assert summary["error_message"] == "backend failed"


def test_build_summary_payload_keeps_loudness_row_ng_when_serialized_loudness_pass_is_false_even_if_scalar_is_in_range():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_limit": 10.0,
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
                    "loudness_max_sone": 5.0,
                    "loudness_pass": False,
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
    assert summary["rows"][1]["label"] == "Loudness"
    assert summary["rows"][1]["deviation_value"] == -5.0
    assert summary["rows"][1]["deviation_display"] == "-5 sone"
    assert summary["rows"][1]["status"] == "NG"
    assert summary["rows"][1]["channel_label"] == "CH1"


def test_build_summary_payload_ranks_loudness_using_unrounded_raw_canonical_deviation():
    summary = build_summary_payload(
        config={
            "config_id": 101,
            "selected_channels": [0, 1],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_limit": 10.0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                },
                {
                    "channel_index": 1,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 80.0,
                    "loudness_unit": "sone",
                    "loudness_lower_limit": 0.0,
                    "loudness_upper_limit": 10.0,
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
                    "loudness_max_sone": 9.9999994,
                    "loudness_pass": True,
                    "loudness_available": True,
                    "loudness_error_message": None,
                    "fft_band_results": [],
                    "oct_band_results": [],
                },
                {
                    "channel_index": 1,
                    "spl_db": 70.0,
                    "spl_pass": True,
                    "loudness_max_sone": 9.99999949,
                    "loudness_pass": True,
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
    assert summary["rows"][1]["label"] == "Loudness"
    assert summary["rows"][1]["channel_index"] == 1
    assert summary["rows"][1]["channel_label"] == "CH2"
    assert summary["rows"][1]["deviation_value"] == -1e-06
    assert summary["rows"][1]["deviation_display"] == "-1e-06 sone"
    assert summary["rows"][1]["status"] == "OK"


def test_build_summary_payload_renders_five_no_result_rows_for_full_no_result():
    summary = build_summary_payload(
        config=_make_config(),
        judgment_payload=None,
        error_message="analysis window not fully captured",
    )

    assert summary["overall_status"] == "no_result"
    assert summary["overall_label"] == "无结果"
    assert summary["error_message"] == "analysis window not fully captured"
    assert [row["label"] for row in summary["rows"]] == ["SPL", "Loudness", "FFT", "倍频程", "AI分析"]
    assert len(summary["rows"]) == 5
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
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: (
            [0.0, 1.0],
            [2.0 + (10.0 if float(signal[0]) > 0.15 else 0.0), 3.0 + (10.0 if float(signal[0]) > 0.15 else 0.0)],
            3.0 + (10.0 if float(signal[0]) > 0.15 else 0.0),
        ),
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
        "times_sec": [0.0, 1.0],
        "values": [2.0, 3.0],
        "unit": "sone",
        "lower_limit": 0.0,
        "upper_limit": 10.0,
    }
    assert payload[1]["loudness"]["times_sec"] == [0.0, 1.0]
    assert payload[1]["loudness"]["unit"] == "phon"
    assert payload[1]["loudness"]["lower_limit"] == 40.0
    assert payload[1]["loudness"]["upper_limit"] == 70.0
    assert payload[1]["loudness"]["values"] == pytest.approx(
        [dsp.sone_to_phon(12.0), dsp.sone_to_phon(13.0)]
    )
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


def test_build_chart_payload_keeps_loudness_payload_shape_with_segmented_backend(monkeypatch):
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
            np.array([500.0], dtype=np.float64),
            np.array([57.0], dtype=np.float64) + ref_db,
        ),
    )
    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        lambda signal, sample_rate: ([0.0, 1.0], [2.0, 3.0], 3.0),
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
        "times_sec": [0.0, 1.0],
        "values": [2.0, 3.0],
        "unit": "sone",
        "lower_limit": 0.0,
        "upper_limit": 10.0,
    }


def test_build_chart_payload_omits_loudness_for_channel_when_recompute_fails(monkeypatch):
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
            np.array([500.0], dtype=np.float64),
            np.array([57.0], dtype=np.float64) + ref_db,
        ),
    )

    def fake_compute_loudness_trace(signal, sample_rate):
        if float(signal[0]) > 0.15:
            raise RuntimeError("loudness chart failed")
        return [0.0, 1.0], [2.0, 3.0], 3.0

    monkeypatch.setattr(
        "app.service.analysis.review_payloads.dsp.compute_loudness_trace",
        fake_compute_loudness_trace,
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
        lambda signal, sample_rate, nfft: (
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
        lambda signal, sample_rate: ([0.0, 1.0], [2.0, 3.0], 3.0),
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


@pytest.mark.parametrize("stft_nfft", [512, 4096])
def test_build_chart_payload_fft_point_count_matches_selected_window_sample_count(
    stft_nfft,
):
    config = _make_config()
    config["selected_channels"] = [0]
    config["channels"] = [
        {
            **config["channels"][0],
            "stft_nfft": stft_nfft,
        }
    ]

    payload = build_chart_payload(
        buffer=np.full((200, 1), 0.1, dtype=np.float32),
        config=config,
        sample_rate=100.0,
        mic_cal={},
        analysis_window=(0.5, 1.5),
    )

    assert len(payload[0]["fft"]["frequencies_hz"]) == 51


def test_build_chart_payload_fft_point_count_is_independent_of_stft_nfft():
    small_nfft_config = _make_config()
    small_nfft_config["selected_channels"] = [0]
    small_nfft_config["channels"] = [
        {
            **small_nfft_config["channels"][0],
            "stft_nfft": 512,
        }
    ]

    large_nfft_config = _make_config()
    large_nfft_config["selected_channels"] = [0]
    large_nfft_config["channels"] = [
        {
            **large_nfft_config["channels"][0],
            "stft_nfft": 4096,
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

    assert len(small_nfft_payload[0]["fft"]["frequencies_hz"]) == 51
    assert len(large_nfft_payload[0]["fft"]["frequencies_hz"]) == 51
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
