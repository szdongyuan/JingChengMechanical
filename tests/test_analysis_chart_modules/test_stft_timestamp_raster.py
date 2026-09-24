from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6.QtCore import QPointF

from ui.widgets.analysis_charts import StftImageChartWidget


@pytest.mark.parametrize("bounds", [
    [(0.02, 0.07)],
    [(0.02, 0.12), (0.32, 0.42)],
    [(0.02, 0.12), (0.12, 0.22), (0.22, 0.25), (0.45, 0.55)],
])
def test_explicit_bounds_color_only_observed_support(qapp, bounds):
    widget = StftImageChartWidget()
    widget.resize(640, 260)
    widget.show()
    times = np.mean(bounds, axis=1)
    source = np.tile(np.arange(len(bounds)) + 1, (2, 1))
    widget.set_image(times_sec=times, frequencies_hz=[100, 200], magnitude_db=source,
                     time_bounds_sec=bounds, snap_initial_to_zero=True, levels_db=(-80, 10))
    qapp.processEvents()
    assert widget.rendered_time_extent_sec() == pytest.approx((bounds[0][0], bounds[-1][1]))
    raster = widget._image_item.image
    centers = bounds[0][0] + (np.arange(raster.shape[1]) + .5) * (
        bounds[-1][1] - bounds[0][0]) / raster.shape[1]
    covered = np.zeros(len(centers), dtype=bool)
    for index, (start, end) in enumerate(bounds):
        inside = (centers >= start) & (centers < end)
        assert inside.any()
        np.testing.assert_array_equal(raster[:, inside], source[:, index:index + 1].repeat(inside.sum(), axis=1))
        covered |= inside
    assert np.isnan(raster[:, ~covered]).all()
    if (~covered).any():
        widget._image_item.render()
        assert widget._image_item.qimage.pixelColor(int(np.flatnonzero(~covered)[0]), 0).alpha() == 0
    assert widget.image_payload()["time_bounds_sec"] == bounds
    assert widget.image_levels() == (-80, 10)
    for center in times:
        assert widget.time_sec_for_widget_x(widget.widget_x_for_time_sec(center)) == pytest.approx(center)


@pytest.mark.parametrize("bounds", [
    [(0, .2)], [(0, .2), (.3,)], [(0, .2), (.1, .4)],
    [(0, .2), (.4, .3)], [(0, .2), (.3, .3)],
    [(0, .2), (.3, np.inf)], [(0, .2), (np.nan, .4)],
    [(0, .2), (.36, .4)], [(-1e308, 0), (0, 1e308)],
])
def test_invalid_explicit_bounds_rejected_before_chart_mutation(qapp, bounds):
    widget = StftImageChartWidget()
    source = np.ones((2, 2))
    widget.set_image(times_sec=[.1, .35], frequencies_hz=[100, 200], magnitude_db=source)
    image, extent, levels = widget._image_item.image, widget.rendered_time_extent_sec(), widget.image_levels()
    with pytest.raises(ValueError, match="time_bounds_sec"):
        widget.set_image(times_sec=[.1, .35], frequencies_hz=[100, 200], magnitude_db=source + 50,
                         time_bounds_sec=bounds, snap_initial_to_zero=True, levels_db=(-100, 0))
    assert widget._image_item.image is image
    assert widget.rendered_time_extent_sec() == extent
    assert widget.image_levels() == levels
    assert widget._snap_initial_to_zero is False
    assert "time_bounds_sec" not in widget.image_payload()


def test_explicit_bounds_huge_range_allocation_is_bounded(qapp, monkeypatch):
    widget = StftImageChartWidget()
    original = np.full
    def bounded(shape, *args, **kwargs):
        if isinstance(shape, tuple) and len(shape) == 2:
            assert shape[1] <= 8192
            assert np.prod(shape) <= 2_097_152
        return original(shape, *args, **kwargs)
    monkeypatch.setattr(np, "full", bounded)
    widget.set_image(times_sec=[.05, 1e100], frequencies_hz=[100, 200], magnitude_db=np.ones((2, 2)),
                     time_bounds_sec=[(0, .1), (1e100 - 1e90, 1e100 + 1e90)])
    assert np.isnan(widget._image_item.image[:, 1:-1]).all()


def test_roundoff_allowance_cannot_hide_substantial_overlap_of_tiny_intervals(qapp):
    widget = StftImageChartWidget()
    with pytest.raises(ValueError, match="time_bounds_sec"):
        widget.set_image(times_sec=[1e16 + 2, 1e16 + 4], frequencies_hz=[100, 200],
                         magnitude_db=np.ones((2, 2)),
                         time_bounds_sec=[(1e16, 1e16 + 8), (1e16 + 2, 1e16 + 6)])
    assert widget.image_payload() is None


@pytest.mark.parametrize("sparse", [False, True])
def test_four_segments_render_at_true_interior_times_with_current_overlay(qapp, sparse):
    times = np.arange(0.1, 8.0, 0.1)
    if sparse:
        times = np.array([0.1, 0.2, 0.3, 0.4, 2.1, 4.1, 4.2, 4.3, 4.4,
                          6.1, 6.2, 6.3, 6.4, 7.9])
    source = np.vstack((np.arange(len(times)), 100 + np.arange(len(times))))
    widget = StftImageChartWidget()
    widget.resize(900, 360)
    widget.show()
    widget.set_image(times_sec=times, frequencies_hz=[100, 200], magnitude_db=source)
    trace = {"times_sec": [0.1, 2.1, 4.1, 6.1, 7.9], "values_a": [1, 2, 3, 4, 5],
             "average_a": 3, "max_a": 5, "latest_a": 5}
    widget.set_current_trace(trace)
    qapp.processEvents()
    image = widget._image_item.image
    rect = widget._image_item.mapRectToParent(widget._image_item.boundingRect())
    pixel_width = rect.width() / image.shape[1]
    for index, time in enumerate(times):
        columns = np.flatnonzero(image[0] == index)
        assert len(columns), f"missing source column at {time}"
        rendered_time = rect.left() + (columns.mean() + 0.5) * pixel_width
        assert rendered_time == pytest.approx(time, abs=pixel_width)
        assert np.all(image[1, columns] == 100 + index)
        # Selection and current use the same true-time view coordinates.
        widget_x = widget.widget_x_for_time_sec(time)
        assert widget.time_sec_for_widget_x(widget_x) == pytest.approx(time)
    current_x, current_y = widget._current_curve_item.getData()
    assert list(current_x) == trace["times_sec"]
    assert list(current_y) == trace["values_a"]
    image_scene_x = widget._image_item.mapToScene(
        QPointF((4.1 - rect.left()) / pixel_width, 0)
    ).x()
    current_scene_x = widget._current_view_box.mapViewToScene(QPointF(4.1, 3)).x()
    assert image_scene_x == pytest.approx(current_scene_x, abs=1.0)
    if sparse:
        centers = rect.left() + (np.arange(image.shape[1]) + 0.5) * pixel_width
        distance = np.min(np.abs(centers[:, None] - times), axis=1)
        assert np.isnan(image[:, distance > 0.05 + pixel_width]).all()
    payload = widget.image_payload()
    assert payload["times_sec"] == list(times)
    np.testing.assert_array_equal(payload["magnitude_db"], source)


@pytest.mark.parametrize("times", [[0, 0, 1], [0, 2, 1], [0, np.nan, 1],
                                   [0, np.inf, 1], [-np.inf, 0, 1]])
def test_invalid_timestamps_leave_chart_unchanged(qapp, times):
    widget = StftImageChartWidget()
    source = np.array([[1, 2, 3], [4, 5, 6]])
    widget.set_image(times_sec=[0, 1, 2], frequencies_hz=[100, 200], magnitude_db=source)
    image = widget._image_item.image
    levels = widget.image_levels()
    extent = widget.rendered_time_extent_sec()
    with pytest.raises(ValueError, match="times_sec"):
        widget.set_image(times_sec=times, frequencies_hz=[100, 200],
                         magnitude_db=source + 100, snap_initial_to_zero=True)
    assert widget._image_item.image is image
    assert widget.image_levels() == levels
    assert widget.rendered_time_extent_sec() == extent
    assert widget._snap_initial_to_zero is False
    np.testing.assert_array_equal(widget.image_payload()["magnitude_db"], source)


def test_large_gap_raster_is_bounded_before_allocation(qapp, monkeypatch):
    widget = StftImageChartWidget()
    original_full = np.full

    def bounded_full(shape, *args, **kwargs):
        if isinstance(shape, tuple) and len(shape) == 2:
            assert shape[1] <= 8192
        return original_full(shape, *args, **kwargs)

    monkeypatch.setattr(np, "full", bounded_full)
    widget.set_image(times_sec=[0.1, 0.2, 0.3, 1e100], frequencies_hz=[100, 200],
                     magnitude_db=np.ones((2, 4)), levels_db=(-80, 0))
    raster = widget._image_item.image
    assert raster.shape[1] <= 8192
    assert np.isnan(raster[:, raster.shape[1] // 2]).all()
    assert widget.image_levels() == (-80, 0)


def test_capped_raster_preserves_endpoint_clusters_and_blank_middle(qapp):
    times = [0.1, 0.2, 0.3, 1_000_000, 1_000_000.1, 1_000_000.2]
    source = np.array([[1, 2, 3, 4, 5, 6], [11, 12, 13, 14, 15, 16]])
    widget = StftImageChartWidget()
    widget.set_image(times_sec=times, frequencies_hz=[100, 200],
                     magnitude_db=source, levels_db=(0, 20))
    raster = widget._image_item.image
    assert raster.shape == (2, 8192)
    assert np.isfinite(raster[:, 0]).all()
    assert np.isfinite(raster[:, -1]).all()
    assert np.isnan(raster[:, 1:-1]).all()
    assert raster[0, 0] in (1, 2, 3)
    assert raster[0, -1] in (4, 5, 6)
    payload = widget.image_payload()
    assert payload["times_sec"] == times
    np.testing.assert_array_equal(payload["magnitude_db"], source)


def test_nonuniform_live_update_reuses_levels_without_rescanning(qapp, monkeypatch):
    widget = StftImageChartWidget()
    times = [0.1, 0.2, 0.3, 2.1]
    source = np.array([[1, 2, 3, 4], [5, 6, 7, 8]])
    widget.set_image(times_sec=times, frequencies_hz=[100, 200], magnitude_db=source)
    levels = widget.image_levels()

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("live update must reuse cached color levels")

    monkeypatch.setattr(widget._image_item, "quickMinMax", unexpected_scan)
    widget.set_image(times_sec=times, frequencies_hz=[100, 200],
                     magnitude_db=source + 100, reset_levels=False)
    assert widget.image_levels() == levels


def test_sparse_raster_auto_levels_preserve_source_peak_and_cached_reuse(qapp, monkeypatch):
    times = [.005, .01, .015, .02, 2.005, 4.005, 4.01, 4.015, 4.02,
             6.005, 6.01, 6.015, 6.02, 7.9]
    source = np.full((1025, len(times)), -80.0)
    source[:, -1] = 0.0
    widget = StftImageChartWidget()
    frequencies = np.arange(source.shape[0])
    widget.set_image(times_sec=times, frequencies_hz=frequencies, magnitude_db=source)

    assert widget.image_levels() == (-80.0, 0.0)
    raster = widget._image_item.image
    peak_column = int(np.flatnonzero(raster[0] == 0)[0])
    floor_column = int(np.flatnonzero(raster[0] == -80)[0])
    widget._image_item.render()
    rendered = widget._image_item.qimage
    assert rendered.pixelColor(peak_column, 0).alpha() == 255
    assert rendered.pixelColor(peak_column, 0) != rendered.pixelColor(floor_column, 0)

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("live updates must not rescan source or raster levels")

    monkeypatch.setattr(widget, "_source_auto_levels", unexpected_scan)
    monkeypatch.setattr(widget._image_item, "quickMinMax", unexpected_scan)
    widget.set_image(times_sec=times, frequencies_hz=frequencies,
                     magnitude_db=source + 20, reset_levels=False)
    assert widget.image_levels() == (-80.0, 0.0)


@pytest.mark.parametrize("values", [
    [-80.0, -60.0, np.nan, -20.0],
    [-80.0] * 4,
    [np.nan] * 4,
    [-np.inf, -80.0, -20.0, np.inf],
    [np.inf] * 4,
])
@pytest.mark.filterwarnings("ignore:All-NaN slice encountered:RuntimeWarning")
def test_nonuniform_auto_levels_match_source_imageitem_edge_cases(qapp, values):
    source = np.tile(values, (2, 1))
    original = pg.ImageItem(axisOrder="row-major")
    original.setImage(source, autoLevels=True)
    widget = StftImageChartWidget()
    widget.set_image(times_sec=[0.1, 0.2, 0.3, 2.1], frequencies_hz=[100, 200],
                     magnitude_db=source)
    assert widget.image_levels() == tuple(original.getLevels())


def test_nonuniform_fixed_levels_skip_source_scan_and_clear_auto_cache(qapp, monkeypatch):
    widget = StftImageChartWidget()
    times = [0.1, 0.2, 0.3, 2.1]
    source = np.array([[-80, -70, -60, 0], [-75, -65, -55, -5]])
    widget.set_image(times_sec=times, frequencies_hz=[100, 200], magnitude_db=source)

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("fixed levels must not scan source or raster levels")

    with monkeypatch.context() as fixed_patch:
        fixed_patch.setattr(widget, "_source_auto_levels", unexpected_scan)
        fixed_patch.setattr(widget._image_item, "quickMinMax", unexpected_scan)
        widget.set_image(times_sec=times, frequencies_hz=[100, 200],
                         magnitude_db=source, levels_db=(-90, 10))
    assert widget.image_levels() == (-90, 10)
    widget.set_image(times_sec=times, frequencies_hz=[100, 200],
                     magnitude_db=source + 20, reset_levels=False)
    assert widget.image_levels() == (-60, 20)


def test_nonuniform_support_uses_nearest_center_and_leaves_gaps_transparent(qapp):
    times = np.array([0.1, 0.2, 0.24, 0.34, 0.8])
    widget = StftImageChartWidget()
    widget.set_image(times_sec=times, frequencies_hz=[100, 200],
                     magnitude_db=np.tile(np.arange(5), (2, 1)), levels_db=(-1, 5))
    raster = widget._image_item.image
    rect = widget._image_item.mapRectToParent(widget._image_item.boundingRect())
    centers = rect.left() + (np.arange(raster.shape[1]) + 0.5) * rect.width() / raster.shape[1]
    nearest = np.argmin(abs(centers[:, None] - times), axis=1)
    covered = abs(centers - times[nearest]) <= 0.05
    np.testing.assert_array_equal(raster[0, covered], nearest[covered])
    assert np.isnan(raster[:, ~covered]).all()
    widget._image_item.render()
    gap_column = np.flatnonzero(~covered)[0]
    assert widget._image_item.qimage.pixelColor(int(gap_column), 0).alpha() == 0


@pytest.mark.parametrize("times,snap,extent", [([0, 0.5, 1], False, (0, 1.25)),
    ([0.1, 0.2, 0.3], True, (0, 0.35)), ([0.1], False, (-0.4, 0.6)),
    ([0.1], True, (0, 1))])
def test_uniform_and_single_column_keep_original_image_and_edge_policies(qapp, times, snap, extent):
    source = np.tile(np.arange(len(times)), (2, 1))
    widget = StftImageChartWidget()
    widget.set_image(times_sec=times, frequencies_hz=[100, 200], magnitude_db=source,
                     snap_initial_to_zero=snap, levels_db=(-80, 0))
    np.testing.assert_array_equal(widget._image_item.image, source)
    assert widget.rendered_time_extent_sec() == pytest.approx(extent)
    assert widget.image_levels() == (-80, 0)
