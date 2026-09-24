from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.service.analysis import dsp
from app.service.analysis.production_executor import (
    _attach_stft_payload,
    _compact_chart_numeric_arrays,
)
from ui.widgets.analysis_charts import StftImageChartWidget


def _attach_precomputed(times, frequencies, magnitudes):
    originals = [array.copy() for array in (times, frequencies, magnitudes)]
    for array in (times, frequencies, magnitudes):
        array.setflags(write=False)
    chart = {}
    _attach_stft_payload(
        chart_payload=chart,
        samples=np.empty((0, 1)),
        config={"selected_channels": [0]},
        calibration={},
        sample_rate=44_100.0,
        channel_column_map={0: 0},
        channel_features={
            0: SimpleNamespace(
                stft_error=None,
                stft_times=times,
                stft_frequencies=frequencies,
                stft_magnitudes=magnitudes,
            )
        },
    )
    for array, original in zip((times, frequencies, magnitudes), originals):
        np.testing.assert_array_equal(array, original)
    return chart


def _set_image(widget, payload):
    widget.set_image(
        times_sec=payload["times_sec"],
        frequencies_hz=payload["frequencies_hz"],
        magnitude_db=payload["magnitude_db"],
        levels_db=(-120.0, 0.0),
    )
    widget._image_item.render()


@pytest.mark.parametrize("overlap,source_columns", [(0.5, 761), (0.75, 1521)])
def test_continuous_terminal_stft_renders_without_false_gaps(
    qapp, overlap, source_columns
):
    sample_rate = 44_100.0
    sample_times = np.arange(390_285, dtype=np.float64) / sample_rate
    samples = 0.25 * np.sin(2 * np.pi * 1_000 * sample_times)
    samples[120_000:120_064] += 0.5
    original_samples = samples.copy()
    source_times, _, source_magnitudes = dsp.compute_stft(
        samples, sample_rate, 1024, overlap
    )
    assert source_times.size == source_columns
    assert np.isfinite(source_magnitudes).all()
    chart = {}
    _attach_stft_payload(
        chart_payload=chart,
        samples=samples,
        config={
            "selected_channels": [0],
            "channels": [{"channel_index": 0, "stft_nfft": 1024,
                          "stft_overlap": overlap}],
        },
        calibration={},
        sample_rate=sample_rate,
        channel_column_map={0: 0},
    )
    _compact_chart_numeric_arrays(chart)
    payload = chart[0]["stft"]
    assert np.asarray(payload["magnitude_db"]).shape == (513, 512)
    assert np.isfinite(payload["magnitude_db"]).all()
    np.testing.assert_array_equal(samples, original_samples)

    widget = StftImageChartWidget()
    try:
        _set_image(widget, payload)
        raster = widget._image_item.image
        blank_columns = np.isnan(raster).all(axis=0).sum()
        assert blank_columns == 0, f"{blank_columns} blank columns in {raster.shape}"
        assert raster.shape == (513, 512)
        assert np.isfinite(raster).all()
        source_hop = (1024 - int(1024 * overlap)) / sample_rate
        assert widget.rendered_time_extent_sec() == pytest.approx(
            (source_times[0] - source_hop / 2,
             source_times[-1] + source_hop / 2),
            rel=0.0, abs=1e-9,
        )
    finally:
        widget.close()
        widget.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("columns", [1, 2, 511, 512])
def test_terminal_stft_within_time_budget_keeps_every_value(columns):
    times = 2.0 + np.arange(columns) * 0.01
    frequencies = np.array([100.0, 200.0, 300.0])
    magnitudes = np.arange(3 * columns, dtype=float).reshape(3, columns) / 7
    payload = _attach_precomputed(times, frequencies, magnitudes)[0]["stft"]
    np.testing.assert_array_equal(payload["times_sec"], times)
    np.testing.assert_array_equal(payload["frequencies_hz"], frequencies)
    np.testing.assert_array_equal(payload["magnitude_db"], magnitudes)


@pytest.mark.parametrize("columns", [513, 761, 16_385])
def test_terminal_bins_cover_every_column_and_retain_unsampled_peaks(columns):
    hop = 512 / 44_100
    times = 3.0 + np.arange(columns) * hop
    frequencies = np.array([100.0, 200.0, 300.0, 400.0])
    magnitudes = np.random.default_rng(42).uniform(-100, -50, (4, columns))
    old_indices = np.linspace(0, columns - 1, 512, dtype=int)
    skipped = int(np.setdiff1d(np.arange(columns), old_indices)[0])
    peak_columns = [0, skipped, columns - 1]
    for row, column in enumerate(peak_columns):
        magnitudes[row, column] = -10.0 + row

    payload = _attach_precomputed(times, frequencies, magnitudes)[0]["stft"]
    display_times = np.asarray(payload["times_sec"])
    display = np.asarray(payload["magnitude_db"])
    assert display.shape == (4, 512)
    np.testing.assert_array_equal(payload["frequencies_hz"], frequencies)
    assert magnitudes[1, skipped] in display[1]
    width = (times[-1] - times[0] + hop) / 512
    left_edge = times[0] - hop / 2
    np.testing.assert_allclose(
        display_times, left_edge + (np.arange(512) + 0.5) * width,
        rtol=0.0, atol=1e-12,
    )
    assert display_times[0] - width / 2 == pytest.approx(left_edge)
    assert display_times[-1] + width / 2 == pytest.approx(times[-1] + hop / 2)
    np.testing.assert_allclose(np.diff(display_times), width, rtol=1e-7, atol=0.0)

    # Integer boundaries partition [0, columns]; no source column is omitted.
    for bin_index in range(512):
        start = bin_index * columns // 512
        end = (bin_index + 1) * columns // 512
        assert end > start
        np.testing.assert_array_equal(
            display[:, bin_index], magnitudes[:, start:end].max(axis=1)
        )
        assert np.max(abs(times[start:end] - display_times[bin_index])) <= (
            width / 2 + hop
        )
        for row, peak_column in enumerate(peak_columns):
            if start <= peak_column < end:
                assert display[row, bin_index] == magnitudes[row, peak_column]


@pytest.mark.parametrize("columns", [512, 761])
def test_terminal_frequency_budget_and_color_settings_are_preserved(columns):
    times = 1.0 + np.arange(columns) * 0.01
    frequencies = np.arange(2049, dtype=float)
    magnitudes = np.broadcast_to(frequencies[:, None] / 10, (2049, columns)).copy()
    payload = _attach_precomputed(times, frequencies, magnitudes)[0]["stft"]
    expected_frequencies = frequencies[np.linspace(0, 2048, 1025, dtype=int)]
    np.testing.assert_array_equal(payload["frequencies_hz"], expected_frequencies)
    np.testing.assert_array_equal(
        payload["magnitude_db"],
        np.broadcast_to(expected_frequencies[:, None] / 10, (1025, 512)),
    )
    assert set(payload) == {
        "times_sec", "frequencies_hz", "magnitude_db",
        "stft_color_scale_enabled", "stft_color_scale_min_db", "stft_color_scale_max_db",
    }
    assert payload["stft_color_scale_enabled"] is False
    assert payload["stft_color_scale_min_db"] == -120.0
    assert payload["stft_color_scale_max_db"] == 0.0


@pytest.mark.parametrize("hop", [0.01, 1e-6])
@pytest.mark.parametrize("relative_jitter", [0.5e-7, 2e-7])
def test_terminal_continuity_uses_relative_tolerance_without_absolute_slack(
    hop, relative_jitter
):
    times = hop * (1 + np.arange(761, dtype=float))
    times[300:] += hop * relative_jitter
    frequencies = np.array([100.0, 200.0])
    magnitudes = np.tile(np.arange(761, dtype=float), (2, 1))
    deltas = np.diff(times)
    eligible = np.allclose(deltas, np.median(deltas), rtol=1e-7, atol=0.0)
    assert eligible == (relative_jitter < 1e-7)
    payload = _attach_precomputed(times, frequencies, magnitudes)[0]["stft"]
    if eligible:
        display_deltas = np.diff(payload["times_sec"])
        np.testing.assert_allclose(
            display_deltas, np.median(display_deltas), rtol=1e-7, atol=0.0
        )
        assert payload["times_sec"][0] != times[0]
    else:
        old_indices = np.linspace(0, 760, 512, dtype=int)
        np.testing.assert_array_equal(payload["times_sec"], times[old_indices])
        np.testing.assert_array_equal(payload["magnitude_db"], magnitudes[:, old_indices])


@pytest.mark.parametrize("axis_kind", ["sparse", "duplicate", "decreasing", "nan", "inf", "-inf"])
def test_noncontinuous_terminal_axes_keep_original_thinning(axis_kind):
    times = 1.0 + np.arange(761) * 0.01
    if axis_kind == "sparse":
        times[380:] += 10.0
    elif axis_kind == "duplicate":
        times[380] = times[379]
    elif axis_kind == "decreasing":
        times = times[::-1].copy()
    else:
        times[380] = float(axis_kind)
    frequencies = np.array([100.0, 200.0])
    magnitudes = np.tile(np.arange(761, dtype=float), (2, 1))
    payload = _attach_precomputed(times, frequencies, magnitudes)[0]["stft"]
    old_indices = np.linspace(0, 760, 512, dtype=int)
    np.testing.assert_array_equal(payload["times_sec"], times[old_indices])
    np.testing.assert_array_equal(payload["magnitude_db"], magnitudes[:, old_indices])


def test_sparse_terminal_stft_retains_visible_gap_after_compaction(qapp):
    times = 1.0 + np.arange(761) * 0.01
    times[380:] += 10.0
    frequencies = np.array([100.0, 200.0])
    magnitudes = np.full((2, 761), -40.0)
    chart = _attach_precomputed(times, frequencies, magnitudes)
    _compact_chart_numeric_arrays(chart)
    widget = StftImageChartWidget()
    try:
        _set_image(widget, chart[0]["stft"])
        raster = widget._image_item.image
        extent = widget.rendered_time_extent_sec()
        centers = extent[0] + (np.arange(raster.shape[1]) + 0.5) * (
            (extent[1] - extent[0]) / raster.shape[1]
        )
        gap_columns = np.flatnonzero((centers > 5.0) & (centers < 14.0))
        assert gap_columns.size > 0
        assert np.isnan(raster[:, gap_columns]).all()
        assert widget._image_item.qimage.pixelColor(int(gap_columns[0]), 0).alpha() == 0
        assert np.isfinite(raster[:, [0, -1]]).all()
    finally:
        widget.close()
        widget.deleteLater()
        qapp.processEvents()
