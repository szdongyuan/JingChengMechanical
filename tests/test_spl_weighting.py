"""Tests for the stateful SPL frequency-weighting filter."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import signal as scipy_signal

import app.service.analysis.spl_weighting as spl_weighting_module
from app.service.analysis.spl_weighting import (
    SplFrequencyWeightingFilter,
    a_weighting,
    c_weighting,
    spl_weighting_sos,
)


STANDARD_CENTER_FREQUENCIES = np.asarray(
    [
        20.0,
        25.0,
        31.5,
        40.0,
        50.0,
        63.0,
        80.0,
        100.0,
        125.0,
        160.0,
        200.0,
        250.0,
        315.0,
        400.0,
        500.0,
        630.0,
        800.0,
        1_000.0,
        1_250.0,
        1_600.0,
        2_000.0,
        2_500.0,
        3_150.0,
        4_000.0,
        5_000.0,
        6_300.0,
        8_000.0,
        10_000.0,
        12_500.0,
        16_000.0,
        20_000.0,
    ],
    dtype=np.float64,
)


@pytest.mark.parametrize(
    ("sample_rate", "max_frequency_hz", "tolerance_db"),
    [
        (44_100.0, 12_500.0, 1.0),
        (48_000.0, 12_500.0, 1.0),
        (96_000.0, 20_000.0, 1.5),
    ],
)
@pytest.mark.parametrize(
    ("weighting", "reference"),
    [("A", a_weighting), ("C", c_weighting)],
)
def test_sos_response_matches_reference(
    sample_rate,
    max_frequency_hz,
    tolerance_db,
    weighting,
    reference,
):
    frequencies = STANDARD_CENTER_FREQUENCIES
    frequencies = frequencies[frequencies <= max_frequency_hz]
    sos = spl_weighting_sos(sample_rate, weighting)

    assert sos is not None
    _, response = scipy_signal.sosfreqz(
        sos,
        worN=frequencies,
        fs=sample_rate,
    )
    actual_db = 20.0 * np.log10(np.maximum(np.abs(response), 1e-30))
    np.testing.assert_allclose(
        actual_db,
        reference(frequencies),
        atol=tolerance_db,
        rtol=0.0,
    )


@pytest.mark.parametrize("sample_rate", [44_100.0, 48_000.0, 96_000.0])
@pytest.mark.parametrize("weighting", ["A", "C"])
def test_sos_gain_is_normalized_at_one_kilohertz(sample_rate, weighting):
    sos = spl_weighting_sos(sample_rate, weighting)

    assert sos is not None
    _, response = scipy_signal.sosfreqz(
        sos,
        worN=np.asarray([1_000.0]),
        fs=sample_rate,
    )
    gain_db = 20.0 * np.log10(abs(response[0]))
    assert gain_db == pytest.approx(0.0, abs=0.01)


def test_coefficient_lookup_caches_normalized_aliases_as_immutable_objects():
    upper = spl_weighting_sos(48_000, "A")
    normalized_alias = spl_weighting_sos(48_000.0, " a ")
    c_weighted = spl_weighting_sos(48_000.0, "C")

    assert upper is normalized_alias
    assert c_weighted is not upper
    assert upper is not None
    assert not upper.flags.writeable
    with pytest.raises(ValueError):
        upper[0, 0] = 0.0


@pytest.mark.parametrize("sample_rate", [0.0, -1.0])
def test_non_positive_sample_rate_is_rejected(sample_rate):
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        spl_weighting_sos(sample_rate, "A")


@pytest.mark.parametrize("weighting", ["A", "C"])
def test_positive_low_sample_rate_returns_stable_coefficients(weighting):
    first = spl_weighting_sos(8_000.0, weighting)
    second = spl_weighting_sos(8_000.0, weighting)

    assert first is second
    assert first is not None
    assert first.ndim == 2
    assert first.shape[1] == 6
    assert np.all(np.isfinite(first))
    assert not first.flags.writeable


def _deterministic_signal(sample_rate: float = 48_000.0) -> np.ndarray:
    times = np.arange(7_321, dtype=np.float64) / sample_rate
    return (
        0.7 * np.sin(2.0 * np.pi * 31.5 * times)
        + 0.2 * np.sin(2.0 * np.pi * 1_000.0 * times)
        + 0.1 * np.cos(2.0 * np.pi * 5_000.0 * times)
    )


@pytest.mark.parametrize("weighting", ["A", "C"])
def test_streaming_filter_is_chunk_invariant(weighting):
    signal = _deterministic_signal()
    whole = SplFrequencyWeightingFilter(48_000.0, weighting).process(signal)
    chunked_filter = SplFrequencyWeightingFilter(48_000.0, weighting)
    chunked = np.concatenate(
        [
            chunked_filter.process(signal[:137]),
            chunked_filter.process(signal[137:4_000]),
            chunked_filter.process(signal[4_000:]),
        ]
    )

    np.testing.assert_allclose(chunked, whole, atol=1e-12, rtol=1e-10)


def test_reset_replays_identical_output():
    signal = _deterministic_signal()
    weighting_filter = SplFrequencyWeightingFilter(48_000.0, "C")

    first = weighting_filter.process(signal)
    weighting_filter.reset()
    second = weighting_filter.process(signal)

    np.testing.assert_allclose(second, first)


def test_filter_instances_maintain_independent_state():
    signal = _deterministic_signal()
    prefix = signal[:2_000]
    continuation = signal[2_000:4_000]
    warmed = SplFrequencyWeightingFilter(48_000.0, "A")
    untouched = SplFrequencyWeightingFilter(48_000.0, "A")
    pristine = SplFrequencyWeightingFilter(48_000.0, "A")

    warmed.process(prefix)
    warmed_output = warmed.process(continuation)
    untouched_output = untouched.process(continuation)
    pristine_output = pristine.process(continuation)

    np.testing.assert_allclose(untouched_output, pristine_output)
    assert not np.allclose(warmed_output, pristine_output)


@pytest.mark.parametrize("weighting", ["Z", " z ", "invalid", "", None])
def test_z_and_invalid_compatibility_values_bypass_filtering(weighting):
    values = np.asarray([[1, 2], [3, 4]], dtype=np.int16)
    weighting_filter = SplFrequencyWeightingFilter(48_000.0, weighting)

    actual = weighting_filter.process(values)

    np.testing.assert_array_equal(actual, np.asarray([1.0, 2.0, 3.0, 4.0]))
    assert actual.dtype == np.float64


@pytest.mark.parametrize("weighting", ["A", "C", "Z"])
def test_empty_input_returns_empty_float_array(weighting):
    actual = SplFrequencyWeightingFilter(48_000.0, weighting).process(
        np.asarray([], dtype=np.int16)
    )

    assert actual.shape == (0,)
    assert actual.dtype == np.float64


def test_coefficient_generation_error_is_not_relabelled_as_z(monkeypatch):
    def fail_coefficient_generation(sample_rate, weighting):
        raise RuntimeError("coefficient failure")

    monkeypatch.setattr(
        spl_weighting_module,
        "spl_weighting_sos",
        fail_coefficient_generation,
    )

    with pytest.raises(RuntimeError, match="coefficient failure"):
        SplFrequencyWeightingFilter(48_000.0, "A")


def test_filter_error_is_not_relabelled_as_z(monkeypatch):
    weighting_filter = SplFrequencyWeightingFilter(48_000.0, "A")

    def fail_filtering(*args, **kwargs):
        raise RuntimeError("filter failure")

    monkeypatch.setattr(spl_weighting_module.scipy_signal, "sosfilt", fail_filtering)

    with pytest.raises(RuntimeError, match="filter failure"):
        weighting_filter.process(np.ones(10, dtype=np.float64))
