"""Bounded peak reduction for optional live STFT display."""
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from app.service.analysis.frames import StftFrame
from app.service.analysis.stft_preview import StftPreviewReducer


def _reducer(interval=0.1):
    return StftPreviewReducer(interval_sec=interval)


def _frame(index, size=1025, peak=-80.0):
    magnitudes = np.full(size, -100.0)
    magnitudes[size // 2] = peak
    return StftFrame(0.005 + index * 0.01, np.arange(size, dtype=float), magnitudes)


def test_all_columns_contribute_once_with_time_and_frequency_peaks():
    reducer = _reducer()
    output = []
    for index in range(23):
        output.extend(reducer.push(_frame(index, peak=17.0 if index == 4 else -80.0), hop_sec=0.01))
    output.extend(reducer.flush())
    assert [frame.source_frame_count for frame in output] == [10, 10, 3]
    assert len(output[0].frequencies) <= 256
    assert output[0].magnitudes.max() == 17.0
    assert output[1].magnitudes.max() == -80.0
    np.testing.assert_allclose([f.time_bounds for f in output], [[0, .1], [.1, .2], [.2, .23]], atol=1e-14)
    assert all(lo <= f.time <= hi for f in output for lo, hi in [f.time_bounds])
    np.testing.assert_array_equal(output[0].frequencies, output[2].frequencies)


def test_frequency_groups_use_maxima_and_band_midpoints():
    reducer = _reducer()
    frame = _frame(0)
    frame.magnitudes[:] = np.arange(1025)
    reducer.push(frame, hop_sec=.01)
    result, = reducer.flush()
    edges = np.linspace(0, 1025, 257, dtype=int)
    np.testing.assert_array_equal(result.magnitudes, edges[1:] - 1)
    np.testing.assert_array_equal(result.frequencies, (edges[:-1] + edges[1:] - 1) / 2)


def test_copied_inputs_and_immutable_outputs():
    reducer = _reducer()
    frame = _frame(0)
    original = frame.magnitudes.copy()
    reducer.push(frame, hop_sec=.01)
    np.testing.assert_array_equal(frame.magnitudes, original)
    frame.magnitudes[:] = 900
    frame.frequencies[:] = -1
    result, = reducer.flush()
    assert result.magnitudes.max() == -80
    assert result.frequencies.min() >= 0
    with pytest.raises((FrozenInstanceError, AttributeError)):
        result.time = 12
    with pytest.raises(ValueError):
        result.magnitudes[0] = 12
    with pytest.raises(ValueError):
        result.frequencies[0] = 12


def test_retained_arrays_remain_bounded_over_thousands_of_columns():
    reducer = _reducer(interval=1000)
    for index in range(3000):
        assert reducer.push(_frame(index), hop_sec=.01) == []
    arrays = [value for value in vars(reducer).values() if isinstance(value, np.ndarray)]
    assert len(arrays) == 2
    assert sum(value.nbytes for value in arrays) <= 2 * 256 * 8
    assert not any(isinstance(value, list) for value in vars(reducer).values())
    result, = reducer.flush()
    assert result.source_frame_count == 3000
    assert reducer.pending is False
    assert reducer.flush() == []


@pytest.mark.parametrize("change", ["time_gap", "axis", "interior_axis", "hop"])
def test_discontinuities_flush_separate_support(change):
    reducer = _reducer()
    first = _frame(0)
    second = _frame(1)
    hop = .01
    if change == "time_gap":
        second.time += .1
    elif change == "axis":
        second.frequencies += 1
    elif change == "interior_axis":
        second.frequencies[1] += .25
    else:
        hop = .02
    reducer.push(first, hop_sec=.01)
    old = reducer.push(second, hop_sec=hop)
    new = reducer.flush()
    assert len(old) == len(new) == 1
    assert old[0].source_frame_count == new[0].source_frame_count == 1
    assert old[0].time_bounds == pytest.approx((0, .01))
    assert new[0].time_bounds == pytest.approx((second.time - hop / 2, second.time + hop / 2))
