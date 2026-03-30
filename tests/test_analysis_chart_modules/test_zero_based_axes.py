from __future__ import annotations

import numpy as np
import pytest

from ui.widgets.analysis_charts import (
    FftLineChartWidget,
    LineChartWidget,
    OctaveBarChartWidget,
    StftImageChartWidget,
)

from ._support import _assert_x_range, _assert_xy_range, _show_widget


@pytest.mark.parametrize(
    ("widget_cls", "expected_x_range", "expected_y_range"),
    (
        (LineChartWidget, (0.0, 1.0), None),
        (FftLineChartWidget, (0.0, 1.0), None),
        (OctaveBarChartWidget, (0.0, 1.0), None),
        (StftImageChartWidget, (0.0, 1.0), (0.0, 1.0)),
    ),
)
def test_chart_widgets_initial_visible_state_use_zero_based_fallback_axes(
    qapp,
    widget_cls,
    expected_x_range,
    expected_y_range,
):
    widget = widget_cls()

    try:
        _show_widget(widget, qapp)

        if expected_y_range is None:
            _assert_x_range(widget, expected_x_range)
        else:
            _assert_xy_range(widget, expected_x_range, expected_y_range)
    finally:
        widget.close()


def test_line_chart_widget_uses_zero_based_x_axis_without_horizontal_padding(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([1.0, 2.0, 3.0], [70.0, 71.0, 72.0])
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 3.0))
    finally:
        widget.close()


def test_line_chart_widget_limit_lines_do_not_expand_zero_based_x_range(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series(
            [1.0, 2.0, 3.0],
            [70.0, 71.0, 72.0],
            lower_limit=60.0,
            upper_limit=80.0,
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 3.0))
    finally:
        widget.close()


def test_fft_line_chart_widget_limit_markers_do_not_expand_zero_based_x_range(qapp):
    widget = FftLineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_spectrum(
            frequencies_hz=[125.0, 250.0, 500.0, 1000.0],
            levels_db=[42.0, 45.0, 51.0, 67.0],
            limit_bands=[
                {
                    "freq_hz": 1000.0,
                    "lower_db": 45.0,
                    "upper_db": 60.0,
                    "actual_db": 67.0,
                    "passed": False,
                }
            ],
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 1000.0))
    finally:
        widget.close()


def test_octave_bar_chart_widget_uses_zero_based_bar_center_range(qapp):
    widget = OctaveBarChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0, 500.0],
            levels_db=[32.0, 34.0, 36.0],
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 2.0))
    finally:
        widget.close()


def test_octave_bar_chart_widget_limit_markers_do_not_expand_zero_based_x_range(qapp):
    widget = OctaveBarChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0, 500.0],
            levels_db=[32.0, 34.0, 36.0],
            limit_bands=[
                {
                    "freq_hz": 500.0,
                    "lower_db": 30.0,
                    "upper_db": 40.0,
                    "actual_db": 36.0,
                    "passed": True,
                }
            ],
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 2.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_uses_zero_based_x_and_y_axes_without_padding(qapp):
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

        _assert_xy_range(widget, (0.0, 3.0), (0.0, 200.0))
    finally:
        widget.close()


def test_chart_widgets_clear_data_keep_zero_based_fallback_axes(qapp):
    line = LineChartWidget()
    fft = FftLineChartWidget()
    octave = OctaveBarChartWidget()
    stft = StftImageChartWidget()

    try:
        for widget in (line, fft, octave, stft):
            _show_widget(widget, qapp)

        line.set_series([1.0, 2.0], [70.0, 71.0])
        fft.set_spectrum(
            frequencies_hz=[125.0, 250.0],
            levels_db=[42.0, 45.0],
            limit_bands=[],
        )
        octave.set_bands(
            center_freqs_hz=[125.0, 250.0],
            levels_db=[32.0, 34.0],
        )
        stft.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
        )
        qapp.processEvents()

        line.clear_data()
        fft.clear_data()
        octave.clear_data()
        stft.clear_data()
        qapp.processEvents()

        assert line.has_series() is False
        assert line.series_payload() is None
        _assert_x_range(line, (0.0, 1.0))

        assert fft.has_series() is False
        assert fft.spectrum_payload() is None
        _assert_x_range(fft, (0.0, 1.0))

        assert octave.has_bars() is False
        assert octave.band_payload() is None
        _assert_x_range(octave, (0.0, 1.0))

        assert stft.has_image() is False
        assert stft.image_payload() is None
        _assert_xy_range(stft, (0.0, 1.0), (0.0, 1.0))
    finally:
        line.close()
        fft.close()
        octave.close()
        stft.close()


def test_line_chart_widget_zero_span_uses_payload_maximum_without_padding(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([5.0], [70.0])
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 5.0))
    finally:
        widget.close()


def test_line_chart_widget_subunit_zero_span_uses_one_point_fallback(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.25], [70.0])
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 1.0))
    finally:
        widget.close()


def test_fft_line_chart_widget_zero_span_uses_payload_maximum_without_padding(qapp):
    widget = FftLineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_spectrum(
            frequencies_hz=[500.0],
            levels_db=[42.0],
            limit_bands=[],
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 500.0))
    finally:
        widget.close()


def test_fft_line_chart_widget_subunit_zero_span_uses_one_point_fallback(qapp):
    widget = FftLineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_spectrum(
            frequencies_hz=[0.25],
            levels_db=[42.0],
            limit_bands=[],
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 1.0))
    finally:
        widget.close()


def test_octave_bar_chart_widget_single_bar_uses_one_point_fallback(qapp):
    widget = OctaveBarChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_bands(
            center_freqs_hz=[125.0],
            levels_db=[32.0],
        )
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 1.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_zero_span_time_uses_rendered_extent_without_padding(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[5.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0], [2.0]]),
        )
        qapp.processEvents()

        x_range, _y_range = widget.plot_item.vb.viewRange()
        assert x_range == pytest.approx((0.0, 6.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_zero_span_subunit_uses_rendered_single_frame_extent(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.5],
            frequencies_hz=[0.25],
            magnitude_db=np.array([[1.0]]),
        )
        qapp.processEvents()

        _assert_xy_range(widget, (0.0, 1.5), (0.0, 1.25))
    finally:
        widget.close()


def test_stft_image_chart_widget_zero_span_frequency_uses_rendered_extent_without_padding(
    qapp,
):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[300.0],
            magnitude_db=np.array([[1.0, 2.0]]),
        )
        qapp.processEvents()

        _x_range, y_range = widget.plot_item.vb.viewRange()
        assert y_range == pytest.approx((0.0, 301.0))
    finally:
        widget.close()


def test_chart_widgets_programmatic_refresh_reset_preserve_zero_based_axes(qapp):
    line = LineChartWidget()
    fft = FftLineChartWidget()
    octave = OctaveBarChartWidget()
    stft = StftImageChartWidget()

    try:
        for widget in (line, fft, octave, stft):
            _show_widget(widget, qapp)

        line.set_series([1.0, 2.0], [70.0, 71.0])
        fft.set_spectrum(
            frequencies_hz=[125.0, 250.0],
            levels_db=[42.0, 45.0],
            limit_bands=[],
        )
        octave.set_bands(
            center_freqs_hz=[125.0, 250.0],
            levels_db=[32.0, 34.0],
        )
        stft.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0], [3.0, 4.0]]),
        )
        qapp.processEvents()

        line.clear_data()
        fft.clear_data()
        octave.clear_data()
        stft.clear_data()
        qapp.processEvents()

        line.set_series([2.0, 4.0], [72.0, 73.0])
        fft.set_spectrum(
            frequencies_hz=[125.0, 250.0, 500.0],
            levels_db=[43.0, 46.0, 52.0],
            limit_bands=[],
        )
        octave.set_bands(
            center_freqs_hz=[125.0, 250.0, 500.0],
            levels_db=[33.0, 35.0, 37.0],
        )
        stft.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[150.0, 300.0],
            magnitude_db=np.array([[2.0, 3.0, 4.0], [4.0, 5.0, 6.0]]),
        )
        qapp.processEvents()

        _assert_x_range(line, (0.0, 4.0))
        _assert_x_range(fft, (0.0, 500.0))
        _assert_x_range(octave, (0.0, 2.0))
        _assert_xy_range(stft, (0.0, 2.0), (0.0, 300.0))
    finally:
        line.close()
        fft.close()
        octave.close()
        stft.close()
