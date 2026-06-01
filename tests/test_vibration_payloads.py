import json
import math

import numpy as np
import pytest

from app.service.analysis.vibration_payloads import build_vibration_payload


def test_build_vibration_payload_reports_overall_metrics_and_low_frequency_share():
    sample_rate = 1000.0
    times = np.arange(0, 1.0, 1.0 / sample_rate)
    signal = 2.0 * np.sin(2.0 * math.pi * 60.0 * times)

    payload = build_vibration_payload(
        channel_index=2,
        signal=signal,
        sample_rate=sample_rate,
        calibration_value=3.5,
    )

    assert payload["channel_index"] == 2
    assert payload["sample_rate"] == pytest.approx(sample_rate)
    assert payload["calibration_value"] == pytest.approx(3.5)
    assert payload["overall_rms"] == pytest.approx(math.sqrt(2.0), rel=0.02)
    assert payload["overall_peak"] == pytest.approx(2.0, rel=0.02)
    assert payload["overall_crest_factor"] == pytest.approx(math.sqrt(2.0), rel=0.02)
    assert payload["low_frequency_energy_share"] == pytest.approx(1.0, abs=0.02)
    assert payload["dominant_frequency_hz"] == pytest.approx(60.0, rel=0.02)
    assert payload["times_sec"]
    assert len(payload["times_sec"]) == len(payload["rms_values"])
    assert len(payload["times_sec"]) == len(payload["peak_values"])
    assert payload["frequency_times_sec"]
    assert len(payload["frequency_times_sec"]) == len(
        payload["fundamental_frequency_values_hz"]
    )
    assert len(payload["frequency_times_sec"]) == len(
        payload["dominant_frequency_values_hz"]
    )
    assert len(payload["frequency_times_sec"]) == len(
        payload["spectral_centroid_values_hz"]
    )
    assert len(payload["frequency_times_sec"]) == len(
        payload["frequency_bandwidth_values_hz"]
    )
    assert payload["fundamental_frequency_values_hz"][0] == pytest.approx(60.0, abs=2.0)
    json.dumps(payload)


def test_build_vibration_payload_estimates_fundamental_when_second_harmonic_is_stronger():
    sample_rate = 2000.0
    times = np.arange(0, 1.0, 1.0 / sample_rate)
    signal = (
        0.8 * np.sin(2.0 * math.pi * 60.0 * times)
        + 2.5 * np.sin(2.0 * math.pi * 120.0 * times)
    )

    payload = build_vibration_payload(
        channel_index=0,
        signal=signal,
        sample_rate=sample_rate,
        calibration_value=None,
    )

    assert payload["dominant_frequency_hz"] == pytest.approx(120.0, abs=2.0)
    assert payload["fundamental_frequency_values_hz"]
    assert np.mean(payload["fundamental_frequency_values_hz"]) == pytest.approx(
        60.0,
        abs=2.0,
    )


def test_build_vibration_payload_uses_zero_summary_for_silence():
    payload = build_vibration_payload(
        channel_index=0,
        signal=np.zeros(1000, dtype=float),
        sample_rate=1000.0,
        calibration_value=None,
    )

    assert payload["overall_rms"] == pytest.approx(0.0)
    assert payload["overall_peak"] == pytest.approx(0.0)
    assert payload["overall_crest_factor"] == pytest.approx(0.0)
    assert payload["low_frequency_energy_share"] == pytest.approx(0.0)
    assert payload["dominant_frequency_hz"] is None
    assert payload["frequency_times_sec"] == []
    assert payload["fundamental_frequency_values_hz"] == []
    assert payload["dominant_frequency_values_hz"] == []
