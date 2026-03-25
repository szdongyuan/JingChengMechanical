import pytest

from app.service.analysis.review_payloads import build_summary_payload


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
