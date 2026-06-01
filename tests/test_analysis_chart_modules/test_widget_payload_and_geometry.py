from __future__ import annotations

import math

import numpy as np
import pytest
from PyQt6.QtCore import QPointF

from ui.widgets.analysis_charts import (
    FftLineChartWidget,
    LineChartWidget,
    OctaveBarChartWidget,
    StftImageChartWidget,
)

from ._support import _show_widget


def test_stft_image_chart_widget_accepts_render_payload(qapp):
    widget = StftImageChartWidget()

    widget.set_image(
        times_sec=[0.0, 0.5, 1.0],
        frequencies_hz=[100.0, 200.0],
        magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
    )

    assert widget.has_image() is True
    assert widget.image_payload()["times_sec"] == [0.0, 0.5, 1.0]
    assert widget.image_payload()["frequencies_hz"] == [100.0, 200.0]
    assert widget.image_payload()["magnitude_db"].shape == (2, 3)


def test_stft_image_chart_widget_image_payload_returns_defensive_copies(qapp):
    widget = StftImageChartWidget()
    widget.set_image(
        times_sec=[0.0, 0.5, 1.0],
        frequencies_hz=[100.0, 200.0],
        magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
    )

    payload = widget.image_payload()
    assert payload is not None

    payload["times_sec"].append(9.5)
    payload["frequencies_hz"][0] = -1.0
    payload["magnitude_db"][0, 0] = 99.0

    fresh_payload = widget.image_payload()
    assert fresh_payload["times_sec"] == [0.0, 0.5, 1.0]
    assert fresh_payload["frequencies_hz"] == [100.0, 200.0]
    assert np.array_equal(
        fresh_payload["magnitude_db"],
        np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
    )


def test_stft_image_chart_widget_exposes_view_box_geometry_in_widget_coordinates(qapp):
    widget = StftImageChartWidget()

    try:
        widget.resize(640, 260)
        widget.show()
        qapp.processEvents()

        widget.set_image(
            times_sec=[0.0, 0.5, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        rect = widget.plot_area_rect_in_widget()
        scene_rect = widget.plot_item.vb.sceneBoundingRect()
        expected_top_left = widget.plot_widget.mapTo(
            widget,
            widget.plot_widget.mapFromScene(scene_rect.topLeft()),
        )
        expected_bottom_right = widget.plot_widget.mapTo(
            widget,
            widget.plot_widget.mapFromScene(scene_rect.bottomRight()),
        )

        assert rect is not None
        assert abs(rect.left() - expected_top_left.x()) <= 1
        assert abs(rect.top() - expected_top_left.y()) <= 1
        assert abs(rect.right() - expected_bottom_right.x()) <= 1
        assert abs(rect.bottom() - expected_bottom_right.y()) <= 1
    finally:
        widget.close()


def test_stft_image_chart_widget_reports_rendered_time_extent_from_payload(qapp):
    widget = StftImageChartWidget()

    try:
        widget.set_image(
            times_sec=[0.0, 0.5, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )

        assert widget.rendered_time_extent_sec() == pytest.approx((0.0, 1.25))
    finally:
        widget.close()


def test_stft_image_chart_widget_reports_single_frame_rendered_time_extent_from_image_rect(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        widget.set_image(
            times_sec=[2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0], [3.5]]),
        )

        image_rect = widget._image_item.mapRectToParent(
            widget._image_item.boundingRect()
        )

        assert widget.rendered_time_extent_sec() == pytest.approx(
            (image_rect.left(), image_rect.right())
        )
    finally:
        widget.close()


def test_stft_image_chart_widget_returns_none_geometry_when_plot_is_not_ready(qapp):
    widget = StftImageChartWidget()

    try:
        assert widget.plot_area_rect_in_widget() is None
    finally:
        widget.close()


def test_stft_image_chart_widget_returns_none_geometry_when_plot_widget_is_hidden(qapp):
    widget = StftImageChartWidget()

    try:
        widget.resize(640, 260)
        widget.show()
        qapp.processEvents()
        widget.set_image(
            times_sec=[0.0, 0.5, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        widget.plot_widget.hide()
        qapp.processEvents()

        assert widget.plot_area_rect_in_widget() is None
    finally:
        widget.close()


def test_stft_image_chart_widget_returns_none_geometry_when_view_box_is_hidden(qapp):
    widget = StftImageChartWidget()

    try:
        widget.resize(640, 260)
        widget.show()
        qapp.processEvents()
        widget.set_image(
            times_sec=[0.0, 0.5, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        widget.plot_item.vb.hide()
        qapp.processEvents()

        assert widget.plot_area_rect_in_widget() is None
    finally:
        widget.close()


def test_stft_image_chart_widget_returns_none_geometry_when_view_box_is_zero_sized(qapp):
    widget = StftImageChartWidget()

    try:
        widget.resize(640, 260)
        widget.show()
        qapp.processEvents()
        widget.set_image(
            times_sec=[0.0, 0.5, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        widget.plot_widget.setGeometry(0, 0, 0, 0)
        qapp.processEvents()

        assert widget.plot_area_rect_in_widget() is None
    finally:
        widget.close()


def test_stft_image_chart_widget_maps_time_values_to_widget_x_positions(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0, 3.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array(
                [[1.0, 2.0, 3.0, 4.0], [2.0, 3.0, 4.0, 5.0]]
            ),
        )
        qapp.processEvents()

        plot_rect = widget.plot_area_rect_in_widget()

        assert plot_rect is not None
        assert widget.widget_x_for_time_sec(0.0) == pytest.approx(
            plot_rect.left(),
            abs=1.0,
        )
        rendered_left, rendered_right = widget.rendered_time_extent_sec()
        assert (rendered_left, rendered_right) == pytest.approx((0.0, 3.5))

        timestamp_x = widget.widget_x_for_time_sec(3.0)
        assert plot_rect.left() < timestamp_x < plot_rect.right()
        assert widget.widget_x_for_time_sec(rendered_right) == pytest.approx(
            plot_rect.right(),
            abs=1.0,
        )
    finally:
        widget.close()


def test_stft_image_chart_widget_matches_actual_pyqtgraph_transform_for_interior_time_values(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0, 3.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array(
                [[1.0, 2.0, 3.0, 4.0], [2.0, 3.0, 4.0, 5.0]]
            ),
        )
        qapp.processEvents()

        target_time = 1.5
        scene_point = widget.plot_item.vb.mapViewToScene(QPointF(target_time, 100.0))
        expected_x = widget.plot_widget.mapTo(
            widget,
            widget.plot_widget.mapFromScene(scene_point),
        ).x()

        assert widget.widget_x_for_time_sec(target_time) == pytest.approx(
            expected_x,
            abs=1.0,
        )
    finally:
        widget.close()


def test_stft_image_chart_widget_maps_widget_x_positions_back_to_time_values(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0, 3.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array(
                [[1.0, 2.0, 3.0, 4.0], [2.0, 3.0, 4.0, 5.0]]
            ),
        )
        qapp.processEvents()

        left_x = widget.widget_x_for_time_sec(0.0)
        right_x = widget.widget_x_for_time_sec(3.0)

        assert left_x is not None
        assert right_x is not None
        assert widget.time_sec_for_widget_x(left_x) == pytest.approx(0.0, abs=0.01)
        assert widget.time_sec_for_widget_x(right_x) == pytest.approx(3.0, abs=0.01)
    finally:
        widget.close()


def test_stft_image_chart_widget_inverse_mapping_matches_actual_pyqtgraph_transform_for_interior_widget_x(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0, 3.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array(
                [[1.0, 2.0, 3.0, 4.0], [2.0, 3.0, 4.0, 5.0]]
            ),
        )
        qapp.processEvents()

        target_time = 1.5
        scene_point = widget.plot_item.vb.mapViewToScene(QPointF(target_time, 100.0))
        widget_x = widget.plot_widget.mapTo(
            widget,
            widget.plot_widget.mapFromScene(scene_point),
        ).x()

        assert widget.time_sec_for_widget_x(widget_x) == pytest.approx(
            target_time,
            abs=0.01,
        )
    finally:
        widget.close()


def test_stft_image_chart_widget_mapping_helpers_return_none_when_geometry_is_unavailable(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        assert widget.widget_x_for_time_sec(0.0) is None
        assert widget.time_sec_for_widget_x(123.0) is None
    finally:
        widget.close()


def test_stft_image_chart_widget_mapping_helpers_return_none_when_geometry_is_unavailable_due_to_hidden_plot_widget(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        widget.plot_widget.hide()
        qapp.processEvents()

        assert widget.widget_x_for_time_sec(0.0) is None
        assert widget.time_sec_for_widget_x(123.0) is None
    finally:
        widget.close()


def test_stft_image_chart_widget_mapping_helpers_return_none_when_geometry_is_unavailable_due_to_hidden_view_box(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        widget.plot_item.vb.hide()
        qapp.processEvents()

        assert widget.widget_x_for_time_sec(0.0) is None
        assert widget.time_sec_for_widget_x(123.0) is None
    finally:
        widget.close()


def test_stft_image_chart_widget_mapping_helpers_return_none_when_geometry_is_unavailable_due_to_zero_sized_plot_widget(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        widget.plot_widget.setGeometry(0, 0, 0, 0)
        qapp.processEvents()

        assert widget.widget_x_for_time_sec(0.0) is None
        assert widget.time_sec_for_widget_x(123.0) is None
    finally:
        widget.close()


def test_stft_image_chart_widget_single_sample_frame_formula_remains_measurable(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0], [3.5]]),
        )
        qapp.processEvents()

        plot_rect = widget.plot_area_rect_in_widget()
        time_extent = widget.rendered_time_extent_sec()

        assert plot_rect is not None
        assert time_extent is not None

        left_x = widget.widget_x_for_time_sec(2.0)
        right_x = widget.widget_x_for_time_sec(time_extent[1])

        assert left_x is not None
        assert right_x is not None
        assert float(plot_rect.left()) - 1.0 <= left_x <= float(plot_rect.right()) + 1.0
        assert float(plot_rect.left()) - 1.0 <= right_x <= float(plot_rect.right()) + 1.0
        assert right_x > left_x
    finally:
        widget.close()


def test_line_chart_widget_accepts_limit_lines(qapp):
    widget = LineChartWidget()

    widget.set_series(
        x_values=[0.0, 1.0],
        y_values=[70.0, 72.0],
        lower_limit=60.0,
        upper_limit=80.0,
    )

    assert widget.has_series() is True
    assert widget.series_payload()["x_values"] == [0.0, 1.0]
    assert widget.series_payload()["y_values"] == [70.0, 72.0]
    assert widget.series_payload()["lower_limit"] == 60.0
    assert widget.series_payload()["upper_limit"] == 80.0


def test_line_chart_widget_default_auto_mode_preserves_existing_series_payload_shape(qapp):
    widget = LineChartWidget()

    widget.set_series(
        x_values=[0.0, 1.0],
        y_values=[7.0, 8.0],
        y_axis_mode="auto",
    )

    assert widget.series_payload() == {
        "x_values": [0.0, 1.0],
        "y_values": [7.0, 8.0],
        "lower_limit": None,
        "upper_limit": None,
    }


def test_line_chart_widget_auto_mode_keeps_existing_non_finite_acceptance(qapp):
    widget = LineChartWidget()

    widget.set_series([0.0, 1.0], [7.0, float("nan")], y_axis_mode="auto")

    assert math.isnan(widget.series_payload()["y_values"][1])


def test_fft_line_chart_widget_accepts_limit_band_markers(qapp):
    widget = FftLineChartWidget()
    limit_bands = [
        {
            "freq_hz": 1000.0,
            "lower_db": 40.0,
            "upper_db": 60.0,
            "actual_db": 67.0,
            "passed": False,
        }
    ]

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[42.0, 45.0, 51.0, 67.0],
        limit_bands=limit_bands,
    )

    assert widget.has_series() is True
    assert widget.spectrum_payload()["frequencies_hz"] == [125.0, 250.0, 500.0, 1000.0]
    assert widget.spectrum_payload()["levels_db"] == [42.0, 45.0, 51.0, 67.0]
    assert widget.spectrum_payload()["limit_bands"] == limit_bands


def test_fft_line_chart_widget_accepts_golden_overlay_payload(qapp):
    widget = FftLineChartWidget()

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0],
        levels_db=[42.0, 45.0, 51.0],
        limit_bands=[],
        golden_overlay_levels_db=[40.0, 44.0, 49.0],
        show_zero_reference=False,
    )

    assert widget.spectrum_payload() == {
        "frequencies_hz": [125.0, 250.0, 500.0],
        "levels_db": [42.0, 45.0, 51.0],
        "display_frequencies_hz": [125.0, 250.0, 500.0],
        "display_levels_db": [42.0, 45.0, 51.0],
        "limit_bands": [],
        "limit_envelope": None,
        "display_limit_envelope": {"control_points": [], "failed_points": []},
        "golden_overlay_levels_db": [40.0, 44.0, 49.0],
        "display_golden_overlay_levels_db": [40.0, 44.0, 49.0],
        "show_zero_reference": False,
        "x_axis_scale": "linear",
    }


def test_fft_chart_filters_zero_hz_in_linear_mode(qapp):
    widget = FftLineChartWidget()
    widget.set_spectrum(
        frequencies_hz=[0.0, 100.0, 200.0],
        levels_db=[10.0, 20.0, 30.0],
        limit_bands=[],
        x_axis_scale="linear",
    )

    payload = widget.spectrum_payload()
    assert payload["frequencies_hz"] == [0.0, 100.0, 200.0]
    assert payload["levels_db"] == [10.0, 20.0, 30.0]
    assert payload["display_frequencies_hz"] == [100.0, 200.0]
    assert payload["display_levels_db"] == [20.0, 30.0]


def test_fft_chart_filters_non_positive_golden_overlay_values(qapp):
    widget = FftLineChartWidget()
    widget.set_spectrum(
        frequencies_hz=[0.0, 100.0, 200.0],
        levels_db=[10.0, 20.0, 30.0],
        golden_overlay_levels_db=[11.0, 21.0, 31.0],
        limit_bands=[],
    )

    payload = widget.spectrum_payload()
    assert payload["display_frequencies_hz"] == [100.0, 200.0]
    assert payload["display_golden_overlay_levels_db"] == [21.0, 31.0]


def test_fft_chart_filters_non_positive_limit_envelope_points(qapp):
    widget = FftLineChartWidget()
    envelope = {
        "control_points": [
            {"freq_hz": 0.0, "lower_db": 10.0, "upper_db": 30.0},
            {"freq_hz": 100.0, "lower_db": 20.0, "upper_db": 40.0},
            {"freq_hz": 200.0, "lower_db": 30.0, "upper_db": 50.0},
        ],
        "line_points": [
            {"freq_hz": 0.0, "lower_db": 10.0, "upper_db": 30.0},
            {"freq_hz": 150.0, "lower_db": 25.0, "upper_db": 45.0},
            {"freq_hz": -200.0, "lower_db": 30.0, "upper_db": 50.0},
        ],
        "failed_points": [
            {
                "freq_hz": -1.0,
                "actual_db": 90.0,
                "lower_db": 10.0,
                "upper_db": 30.0,
            },
            {
                "freq_hz": 150.0,
                "actual_db": 90.0,
                "lower_db": 25.0,
                "upper_db": 45.0,
            },
        ],
    }
    widget.set_spectrum(
        frequencies_hz=[0.0, 100.0, 200.0],
        levels_db=[10.0, 20.0, 30.0],
        limit_envelope=envelope,
    )

    rendered = widget.spectrum_payload()["display_limit_envelope"]
    assert rendered["control_points"] == envelope["control_points"][1:]
    assert rendered["line_points"] == envelope["line_points"][1:2]
    assert rendered["failed_points"] == envelope["failed_points"][1:]


def test_fft_chart_uses_log_x_axis_mode(qapp):
    widget = FftLineChartWidget()
    widget.set_spectrum(
        frequencies_hz=[0.0, 100.0, 1000.0],
        levels_db=[10.0, 20.0, 30.0],
        limit_bands=[],
        x_axis_scale="log",
    )

    assert widget.spectrum_payload()["x_axis_scale"] == "log"
    assert widget.spectrum_payload()["display_frequencies_hz"] == [100.0, 1000.0]
    assert widget.plot_item.getViewBox().state["logMode"][0] is True


def test_fft_chart_renders_limit_envelope_lines_and_failed_spectrum_segments(qapp):
    widget = FftLineChartWidget()
    envelope = {
        "control_points": [
            {"freq_hz": 100.0, "lower_db": 30.0, "upper_db": 80.0},
            {"freq_hz": 200.0, "lower_db": 40.0, "upper_db": 90.0},
        ],
        "line_points": [
            {"freq_hz": 100.0, "lower_db": 30.0, "upper_db": 80.0},
            {"freq_hz": 150.0, "lower_db": 35.0, "upper_db": 85.0},
            {"freq_hz": 175.0, "lower_db": 37.5, "upper_db": 87.5},
            {"freq_hz": 200.0, "lower_db": 40.0, "upper_db": 90.0},
        ],
        "failed_points": [
            {
                "freq_hz": 150.0,
                "actual_db": 95.0,
                "lower_db": 35.0,
                "upper_db": 85.0,
            },
        ],
    }
    widget.set_spectrum(
        frequencies_hz=[100.0, 150.0, 175.0, 200.0],
        levels_db=[50.0, 95.0, 96.0, 60.0],
        limit_envelope=envelope,
    )

    payload = widget.spectrum_payload()
    assert payload["limit_envelope"] == envelope
    lower_x, lower_y = widget._lower_envelope_curve_item.getData()
    upper_x, upper_y = widget._upper_envelope_curve_item.getData()
    failed_x, failed_y = widget._failed_marker_item.getData()
    failed_line_x, failed_line_y = widget._failed_spectrum_curve_item.getData()
    assert list(lower_x) == [100.0, 150.0, 175.0, 200.0]
    assert list(lower_y) == [30.0, 35.0, 37.5, 40.0]
    assert list(upper_x) == [100.0, 150.0, 175.0, 200.0]
    assert list(upper_y) == [80.0, 85.0, 87.5, 90.0]
    assert failed_x is None or list(failed_x) == []
    assert failed_y is None or list(failed_y) == []
    assert list(failed_line_x) == [100.0, 150.0, 175.0, 200.0]
    assert np.isnan(failed_line_y[0])
    assert list(failed_line_y[1:3]) == [95.0, 96.0]
    assert np.isnan(failed_line_y[3])


def test_fft_chart_renders_limit_envelope_line_points_when_present(qapp):
    widget = FftLineChartWidget()
    envelope = {
        "control_points": [
            {"freq_hz": 150.0, "lower_db": 14.0, "upper_db": 16.0},
            {"freq_hz": 200.0, "lower_db": 18.0, "upper_db": 22.0},
            {"freq_hz": 200.0, "lower_db": 1.0, "upper_db": 60.0},
            {"freq_hz": 350.0, "lower_db": 32.0, "upper_db": 38.0},
        ],
        "line_points": [
            {"freq_hz": 150.0, "lower_db": 14.0, "upper_db": 16.0},
            {"freq_hz": 200.0, "lower_db": 18.0, "upper_db": 22.0},
            {"freq_hz": 300.0, "lower_db": 24.0, "upper_db": 36.0},
            {"freq_hz": 350.0, "lower_db": 32.0, "upper_db": 38.0},
        ],
        "failed_points": [],
    }

    widget.set_spectrum(
        frequencies_hz=[100.0, 200.0, 300.0, 400.0],
        levels_db=[11.0, 21.0, 31.0, 41.0],
        limit_envelope=envelope,
    )

    lower_x, lower_y = widget._lower_envelope_curve_item.getData()
    upper_x, upper_y = widget._upper_envelope_curve_item.getData()
    assert widget.spectrum_payload()["display_limit_envelope"]["line_points"] == (
        envelope["line_points"]
    )
    assert list(lower_x) == [150.0, 200.0, 300.0, 350.0]
    assert list(lower_y) == [14.0, 18.0, 24.0, 32.0]
    assert list(upper_x) == [150.0, 200.0, 300.0, 350.0]
    assert list(upper_y) == [16.0, 22.0, 36.0, 38.0]


def test_fft_chart_keeps_legacy_limit_band_markers_without_limit_envelope(qapp):
    widget = FftLineChartWidget()
    limit_bands = [
        {
            "freq_hz": 1000.0,
            "lower_db": 40.0,
            "upper_db": 60.0,
            "actual_db": 67.0,
            "passed": False,
        }
    ]

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[42.0, 45.0, 51.0, 67.0],
        limit_bands=limit_bands,
    )

    marker_x, marker_y = widget._band_marker_item.getData()
    lower_x, lower_y = widget._lower_envelope_curve_item.getData()
    upper_x, upper_y = widget._upper_envelope_curve_item.getData()
    failed_x, failed_y = widget._failed_marker_item.getData()
    assert widget.spectrum_payload()["limit_bands"] == limit_bands
    assert list(marker_x) == [1000.0]
    assert list(marker_y) == [67.0]
    assert lower_x is None or list(lower_x) == []
    assert lower_y is None or list(lower_y) == []
    assert upper_x is None or list(upper_x) == []
    assert upper_y is None or list(upper_y) == []
    assert failed_x is None or list(failed_x) == []
    assert failed_y is None or list(failed_y) == []


def test_fft_chart_linear_mode_filters_zero_hz_legacy_limit_band_markers(qapp):
    widget = FftLineChartWidget()
    limit_bands = [
        {
            "freq_hz": 0.0,
            "lower_db": 10.0,
            "upper_db": 20.0,
            "actual_db": 15.0,
            "passed": True,
        },
        {
            "freq_hz": 100.0,
            "lower_db": 20.0,
            "upper_db": 40.0,
            "actual_db": 35.0,
            "passed": True,
        },
    ]

    widget.set_spectrum(
        frequencies_hz=[100.0],
        levels_db=[35.0],
        limit_bands=limit_bands,
        limit_envelope=None,
        x_axis_scale="linear",
    )

    marker_x, marker_y = widget._band_marker_item.getData()
    assert widget.spectrum_payload()["limit_bands"] == limit_bands
    assert list(marker_x) == [100.0]
    assert list(marker_y) == [35.0]
    assert list(np.asarray(widget._band_range_item.opts["x"], dtype=float)) == [
        100.0
    ]


def test_fft_chart_log_mode_transforms_legacy_limit_band_markers(qapp):
    widget = FftLineChartWidget()
    limit_bands = [
        {
            "freq_hz": 0.0,
            "lower_db": 10.0,
            "upper_db": 20.0,
            "actual_db": 15.0,
            "passed": True,
        },
        {
            "freq_hz": 100.0,
            "lower_db": 20.0,
            "upper_db": 40.0,
            "actual_db": 35.0,
            "passed": True,
        },
        {
            "freq_hz": 1000.0,
            "lower_db": 25.0,
            "upper_db": 45.0,
            "actual_db": 55.0,
            "passed": False,
        },
        {
            "freq_hz": -50.0,
            "lower_db": 30.0,
            "upper_db": 60.0,
            "actual_db": 75.0,
            "passed": False,
        },
    ]

    widget.set_spectrum(
        frequencies_hz=[100.0, 1000.0],
        levels_db=[35.0, 55.0],
        limit_bands=limit_bands,
        limit_envelope=None,
        x_axis_scale="log",
    )

    marker_x, marker_y = widget._band_marker_item.getData()
    expected_x = [math.log10(100.0), math.log10(1000.0)]
    assert widget.spectrum_payload()["limit_bands"] == limit_bands
    assert list(marker_x) == pytest.approx(expected_x)
    assert list(marker_y) == [35.0, 55.0]
    assert list(np.asarray(widget._band_range_item.opts["x"], dtype=float)) == (
        pytest.approx(expected_x)
    )
    assert list(np.asarray(widget._band_range_item.opts["y"], dtype=float)) == [
        30.0,
        35.0,
    ]


def test_fft_line_chart_widget_renders_golden_overlay_as_second_line_item(qapp):
    widget = FftLineChartWidget()

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0],
        levels_db=[42.0, 45.0, 51.0],
        limit_bands=[],
        golden_overlay_levels_db=[40.0, 44.0, 49.0],
    )

    overlay_x, overlay_y = widget._golden_overlay_curve_item.getData()

    assert list(overlay_x) == [125.0, 250.0, 500.0]
    assert list(overlay_y) == [40.0, 44.0, 49.0]
    assert widget._zero_reference_line.isVisible() is False


def test_fft_line_chart_widget_uses_thin_spectrum_pens(qapp):
    widget = FftLineChartWidget()

    assert widget._curve.opts["pen"].widthF() == pytest.approx(0.7)
    assert widget._golden_overlay_curve_item.opts["pen"].widthF() == pytest.approx(0.7)
    assert widget._failed_spectrum_curve_item.opts["pen"].widthF() == pytest.approx(0.7)
    assert widget._lower_envelope_curve_item.opts["pen"].widthF() == pytest.approx(2.0)
    assert widget._upper_envelope_curve_item.opts["pen"].widthF() == pytest.approx(2.0)


def test_fft_line_chart_widget_shows_zero_reference_line_in_delta_mode(qapp):
    widget = FftLineChartWidget()

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0],
        levels_db=[2.0, -1.0, 3.0],
        limit_bands=[],
        show_zero_reference=True,
    )

    overlay_x, overlay_y = widget._golden_overlay_curve_item.getData()

    assert overlay_x is None
    assert overlay_y is None
    assert widget._zero_reference_line.isVisible() is True
    assert widget._zero_reference_line.value() == pytest.approx(0.0)


def test_fft_line_chart_widget_keeps_two_limit_band_markers_renderable(qapp):
    widget = FftLineChartWidget()
    limit_bands = [
        {
            "freq_hz": 500.0,
            "lower_db": 40.0,
            "upper_db": 52.0,
            "actual_db": 51.0,
            "passed": True,
        },
        {
            "freq_hz": 1000.0,
            "lower_db": 45.0,
            "upper_db": 60.0,
            "actual_db": 67.0,
            "passed": False,
        },
    ]

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[42.0, 45.0, 51.0, 67.0],
        limit_bands=limit_bands,
    )

    assert widget.spectrum_payload()["limit_bands"] == limit_bands
    widget._band_range_item.boundingRect()


def test_fft_line_chart_widget_keeps_series_renderable_without_limit_band_markers(
    qapp,
):
    widget = FftLineChartWidget()

    try:
        widget.set_spectrum(
            frequencies_hz=[125.0, 250.0, 500.0, 1000.0],
            levels_db=[42.0, 45.0, 51.0, 67.0],
            limit_bands=[],
        )

        assert widget.has_series() is True
        assert widget.spectrum_payload()["frequencies_hz"] == [
            125.0,
            250.0,
            500.0,
            1000.0,
        ]
        assert widget.spectrum_payload()["levels_db"] == [42.0, 45.0, 51.0, 67.0]
        assert widget.spectrum_payload()["limit_bands"] == []

        widget.show()
        qapp.processEvents()

        rect = widget._band_range_item.boundingRect()
        x_range, _y_range = widget.plot_item.vb.viewRange()

        assert rect.width() >= 0.0
        assert rect.height() >= 0.0
        assert x_range[1] > 100.0
        assert (x_range[1] - x_range[0]) > 100.0
    finally:
        widget.close()


def test_fft_line_chart_widget_clear_data_keeps_empty_marker_path_safe(qapp):
    widget = FftLineChartWidget()
    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0],
        levels_db=[42.0, 45.0, 51.0],
        limit_bands=[
            {
                "freq_hz": 250.0,
                "lower_db": 40.0,
                "upper_db": 50.0,
                "actual_db": 45.0,
                "passed": True,
            }
        ],
    )

    widget.clear_data()

    assert widget.has_series() is False
    assert widget.spectrum_payload() is None
    widget._band_range_item.boundingRect()


def test_octave_bar_chart_widget_accepts_limit_band_markers(qapp):
    widget = OctaveBarChartWidget()
    limit_bands = [
        {
            "freq_hz": 500.0,
            "lower_db": 35.0,
            "upper_db": 55.0,
            "actual_db": 57.0,
            "passed": False,
        }
    ]

    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[33.0, 41.0, 57.0, 49.0],
        limit_bands=limit_bands,
    )

    assert widget.has_bars() is True
    assert widget.band_payload()["center_freqs_hz"] == [125.0, 250.0, 500.0, 1000.0]
    assert widget.band_payload()["levels_db"] == [33.0, 41.0, 57.0, 49.0]
    assert widget.band_payload()["limit_bands"] == limit_bands


def test_octave_bar_chart_widget_accepts_golden_overlay_payload(qapp):
    widget = OctaveBarChartWidget()

    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0],
        levels_db=[33.0, 41.0, 57.0],
        limit_bands=[],
        golden_overlay_levels_db=[31.0, 39.0, 54.0],
        show_zero_reference=False,
    )

    assert widget.band_payload() == {
        "center_freqs_hz": [125.0, 250.0, 500.0],
        "levels_db": [33.0, 41.0, 57.0],
        "limit_bands": [],
        "golden_overlay_levels_db": [31.0, 39.0, 54.0],
        "show_zero_reference": False,
    }


def test_octave_bar_chart_widget_renders_golden_overlay_comparison_series(qapp):
    widget = OctaveBarChartWidget()

    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0],
        levels_db=[33.0, 41.0, 57.0],
        limit_bands=[],
        golden_overlay_levels_db=[31.0, 39.0, 54.0],
    )

    overlay_x, overlay_y = widget._golden_overlay_item.getData()

    assert list(overlay_x) == [0.0, 1.0, 2.0]
    assert list(overlay_y) == [31.0, 39.0, 54.0]
    assert widget._zero_reference_line.isVisible() is False


def test_octave_bar_chart_widget_downsamples_visible_axis_labels_to_approved_subset(
    qapp,
):
    widget = OctaveBarChartWidget()

    widget.set_bands(
        center_freqs_hz=[
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
            1000.0,
            1250.0,
            1600.0,
            2000.0,
            2500.0,
            3150.0,
            4000.0,
            5000.0,
            6300.0,
            8000.0,
            10000.0,
            12500.0,
            16000.0,
            20000.0,
        ],
        levels_db=[0.0] * 29,
        limit_bands=[],
    )

    ticks = widget.plot_item.getAxis("bottom")._tickLevels
    labels = [text for _value, text in ticks[0]]

    assert labels == [
        "",
        "40",
        "",
        "",
        "80",
        "",
        "",
        "160",
        "",
        "",
        "315",
        "",
        "",
        "630",
        "",
        "",
        "1250",
        "",
        "",
        "2500",
        "",
        "",
        "5000",
        "",
        "",
        "10000",
        "",
        "16000",
        "",
    ]
    assert widget.band_payload()["center_freqs_hz"] == [
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
        1000.0,
        1250.0,
        1600.0,
        2000.0,
        2500.0,
        3150.0,
        4000.0,
        5000.0,
        6300.0,
        8000.0,
        10000.0,
        12500.0,
        16000.0,
        20000.0,
    ]
    assert widget._bar_item is not None
    assert list(np.asarray(widget._bar_item.opts["x"], dtype=float)) == list(
        np.arange(29, dtype=float)
    )
    assert len(np.asarray(widget._bar_item.opts["height"], dtype=float)) == 29


def test_octave_bar_chart_widget_keeps_negative_delta_bars_anchored_to_zero(qapp):
    widget = OctaveBarChartWidget()

    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0],
        levels_db=[-3.0, -5.0, -1.0],
        limit_bands=[],
        show_zero_reference=True,
    )

    assert widget._zero_reference_line.isVisible() is True
    assert widget._zero_reference_line.value() == pytest.approx(0.0)
    assert widget._bar_item is not None
    assert widget._bar_item.opts["y0"] == pytest.approx(0.0)
    assert list(np.asarray(widget._bar_item.opts["height"], dtype=float)) == [
        -3.0,
        -5.0,
        -1.0,
    ]
    assert widget._bar_item.dataBounds(1) == pytest.approx((-5.0, 0.0))


def test_octave_bar_chart_widget_keeps_two_limit_band_markers_renderable(qapp):
    widget = OctaveBarChartWidget()
    limit_bands = [
        {
            "freq_hz": 250.0,
            "lower_db": 32.0,
            "upper_db": 42.0,
            "actual_db": 41.0,
            "passed": True,
        },
        {
            "freq_hz": 500.0,
            "lower_db": 35.0,
            "upper_db": 55.0,
            "actual_db": 57.0,
            "passed": False,
        },
    ]

    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[33.0, 41.0, 57.0, 49.0],
        limit_bands=limit_bands,
    )

    assert widget.band_payload()["limit_bands"] == limit_bands
    widget._band_range_item.boundingRect()


def test_octave_bar_chart_widget_keeps_bars_renderable_without_limit_band_markers(
    qapp,
):
    widget = OctaveBarChartWidget()

    try:
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0, 500.0, 1000.0],
            levels_db=[33.0, 41.0, 57.0, 49.0],
            limit_bands=[],
        )

        assert widget.has_bars() is True
        assert widget.band_payload()["center_freqs_hz"] == [
            125.0,
            250.0,
            500.0,
            1000.0,
        ]
        assert widget.band_payload()["levels_db"] == [33.0, 41.0, 57.0, 49.0]
        assert widget.band_payload()["limit_bands"] == []

        widget.show()
        qapp.processEvents()

        rect = widget._band_range_item.boundingRect()

        assert rect.width() >= 0.0
        assert rect.height() >= 0.0
    finally:
        widget.close()


def test_octave_bar_chart_widget_clear_data_keeps_empty_marker_path_safe(qapp):
    widget = OctaveBarChartWidget()
    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0],
        levels_db=[33.0, 41.0, 57.0],
        limit_bands=[
            {
                "freq_hz": 250.0,
                "lower_db": 30.0,
                "upper_db": 45.0,
                "actual_db": 41.0,
                "passed": True,
            }
        ],
    )

    widget.clear_data()

    assert widget.has_bars() is False
    assert widget.band_payload() is None
    widget._band_range_item.boundingRect()
