from __future__ import annotations

import numpy as np
import pytest

from app.service.analysis.dsp import compute_stft
from ui.widgets.analysis_charts import (
    FftLineChartWidget,
    LineChartWidget,
    OctaveBarChartWidget,
    StftImageChartWidget,
)

from ._support import _assert_x_range, _assert_xy_range, _show_widget


def _assert_left_axis_title_hidden_ticks_visible(widget) -> None:
    left_axis = widget.plot_item.getAxis("left")
    assert left_axis.isVisible() is True
    assert left_axis.labelText == ""
    assert left_axis.style.get("showValues") is True
    assert left_axis.style.get("tickLength") not in (None, 0)
    assert left_axis.tickStrings([0.0, 1.0], 1.0, 1.0) == ["0", "1"]
    assert widget.plot_item.getAxis("bottom").isVisible() is True


def _assert_left_axis_visible(widget) -> None:
    assert widget.plot_item.getAxis("left").isVisible() is True


def _assert_default_left_axis_label_refresh_visible(widget) -> None:
    widget.plot_item.setLabel("left", "Refreshed", units="dB")
    left_axis = widget.plot_item.getAxis("left")
    assert left_axis.isVisible() is True
    assert left_axis.labelText == "Refreshed"
    assert left_axis.style.get("showValues") is True


def _computed_stft_payload(*, overlap: float, time_shift_sec: float = 0.0) -> tuple:
    sample_rate = 48_000.0
    nfft = 1024
    duration_samples = nfft * 4
    sample_index = np.arange(duration_samples, dtype=float)
    signal = np.sin(2.0 * np.pi * 1_000.0 * sample_index / sample_rate)
    times_sec, frequencies_hz, magnitude_db = compute_stft(
        signal,
        sample_rate,
        nfft,
        overlap,
    )
    return times_sec + time_shift_sec, frequencies_hz, magnitude_db


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


@pytest.mark.parametrize(
    "widget_cls",
    (LineChartWidget, FftLineChartWidget, OctaveBarChartWidget, StftImageChartWidget),
)
def test_chart_widgets_keep_left_axis_visible_by_default(qapp, widget_cls):
    widget = widget_cls()

    try:
        _show_widget(widget, qapp)
        _assert_left_axis_visible(widget)
        assert widget.plot_item.getAxis("bottom").isVisible() is True
    finally:
        widget.close()


@pytest.mark.parametrize(
    "widget_cls",
    (LineChartWidget, FftLineChartWidget, OctaveBarChartWidget, StftImageChartWidget),
)
def test_chart_widgets_can_hide_only_left_axis_title_when_opted_in(qapp, widget_cls):
    widget = widget_cls(hide_left_axis_label=True)

    try:
        _show_widget(widget, qapp)
        _assert_left_axis_title_hidden_ticks_visible(widget)
    finally:
        widget.close()


@pytest.mark.parametrize(
    "widget_cls",
    (LineChartWidget, FftLineChartWidget, OctaveBarChartWidget, StftImageChartWidget),
)
def test_opted_in_chart_widgets_keep_left_axis_title_hidden_after_left_label_refresh(
    qapp,
    widget_cls,
):
    widget = widget_cls(hide_left_axis_label=True)

    try:
        _show_widget(widget, qapp)
        widget.plot_item.setLabel("left", "Refreshed", units="dB")
        qapp.processEvents()

        _assert_left_axis_title_hidden_ticks_visible(widget)
    finally:
        widget.close()


@pytest.mark.parametrize(
    "widget_cls",
    (LineChartWidget, FftLineChartWidget, OctaveBarChartWidget, StftImageChartWidget),
)
def test_default_chart_widgets_keep_normal_left_label_refresh_behavior(
    qapp,
    widget_cls,
):
    widget = widget_cls()

    try:
        _show_widget(widget, qapp)
        widget.plot_item.hideAxis("left")
        _assert_default_left_axis_label_refresh_visible(widget)
        qapp.processEvents()

        assert widget.plot_item.getAxis("bottom").isVisible() is True
    finally:
        widget.close()


def test_left_axis_title_hiding_preserves_zero_based_ranges(qapp):
    line = LineChartWidget(hide_left_axis_label=True)
    fft = FftLineChartWidget(hide_left_axis_label=True)
    octave = OctaveBarChartWidget(hide_left_axis_label=True)
    stft = StftImageChartWidget(hide_left_axis_label=True)

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

        _assert_x_range(line, (0.0, 2.0))
        _assert_x_range(fft, (0.0, 250.0))
        _assert_x_range(octave, (-0.5, 1.5))
        _assert_xy_range(stft, (0.0, 1.5), (0.0, 200.0))
        for widget in (line, fft, octave, stft):
            _assert_left_axis_title_hidden_ticks_visible(widget)
    finally:
        line.close()
        fft.close()
        octave.close()
        stft.close()


def test_line_chart_widget_uses_zero_based_x_axis_without_horizontal_padding(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([1.0, 2.0, 3.0], [70.0, 71.0, 72.0])
        qapp.processEvents()

        _assert_x_range(widget, (0.0, 3.0))
    finally:
        widget.close()


def test_line_chart_widget_uses_explicit_x_axis_range_without_padding(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series(
            [4.05, 4.10, 4.15],
            [70.0, 71.0, 72.0],
            x_axis_range=(4.0, 12.0),
        )
        qapp.processEvents()

        _assert_x_range(widget, (4.0, 12.0))
        assert widget.series_payload()["x_axis_range"] == pytest.approx((4.0, 12.0))
    finally:
        widget.close()


def test_line_chart_widget_reset_view_restores_explicit_x_axis_range(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series(
            [4.05, 4.10, 4.15],
            [70.0, 71.0, 72.0],
            x_axis_range=(4.0, 12.0),
        )
        widget.plot_item.vb.setXRange(6.0, 7.0, padding=0.0)
        widget.chart_interactions.reset_view()
        qapp.processEvents()

        _assert_x_range(widget, (4.0, 12.0))
    finally:
        widget.close()


def test_line_chart_widget_rejects_invalid_explicit_x_axis_range(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        with pytest.raises(ValueError, match="x_axis_range"):
            widget.set_series([1.0], [70.0], x_axis_range=(5.0, 5.0))
        assert widget.series_payload() is None
    finally:
        widget.close()


def test_line_chart_widget_clear_data_removes_explicit_x_axis_range(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series(
            [4.05, 4.10, 4.15],
            [70.0, 71.0, 72.0],
            x_axis_range=(4.0, 12.0),
        )
        widget.clear_data()
        qapp.processEvents()

        assert widget.series_payload() is None
        _assert_x_range(widget, (0.0, 1.0))
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


def test_line_chart_widget_rejects_unknown_y_axis_mode(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)

        with pytest.raises(ValueError):
            widget.set_series([0.0], [1.0], y_axis_mode="nope")
    finally:
        widget.close()


def test_line_chart_widget_data_focus_mode_resets_zero_span_and_clear_state(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0], [7.0], y_axis_mode="data_focus")
        qapp.processEvents()

        _x_range, y_range = widget.plot_item.vb.viewRange()
        assert y_range == pytest.approx((6.3, 7.7))

        widget.clear_data()
        qapp.processEvents()

        assert widget.plot_item.vb.viewRange()[1] == pytest.approx((0.0, 1.0))
    finally:
        widget.close()


def test_line_chart_widget_data_focus_uses_exact_non_zero_span_padding_formula(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="data_focus")
        qapp.processEvents()

        assert widget.plot_item.vb.viewRange()[1] == pytest.approx((6.9, 8.1))
    finally:
        widget.close()


def test_line_chart_widget_data_focus_rejects_non_finite_values(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="auto")
        qapp.processEvents()
        previous_y_range = widget.plot_item.vb.viewRange()[1]
        previous_payload = widget.series_payload()
        previous_mode = widget._y_axis_mode

        with pytest.raises(ValueError):
            widget.set_series([0.0, 1.0], [7.0, float("nan")], y_axis_mode="data_focus")

        assert widget._y_axis_mode == previous_mode
        assert widget.series_payload() == previous_payload
        assert widget.plot_item.vb.viewRange()[1] == pytest.approx(previous_y_range)
    finally:
        widget.close()


def test_line_chart_widget_data_focus_rejects_empty_series_without_mutating_prior_state(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="auto")
        qapp.processEvents()
        previous_y_range = widget.plot_item.vb.viewRange()[1]
        previous_payload = widget.series_payload()
        previous_mode = widget._y_axis_mode

        with pytest.raises(ValueError):
            widget.set_series([], [], y_axis_mode="data_focus")

        assert widget._y_axis_mode == previous_mode
        assert widget.series_payload() == previous_payload
        assert widget.plot_item.vb.viewRange()[1] == pytest.approx(previous_y_range)
    finally:
        widget.close()


def test_line_chart_widget_auto_mode_clears_prior_data_focus_override(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="data_focus")
        qapp.processEvents()

        widget.set_series([0.0, 1.0], [1.0, 9.0], y_axis_mode="auto")
        qapp.processEvents()

        assert widget.plot_item.vb.state["autoRange"][1] is not False
    finally:
        widget.close()


def test_line_chart_widget_clear_data_in_auto_mode_keeps_backward_compatible_y_behavior(qapp):
    widget = LineChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0], [7.0, 8.0], y_axis_mode="auto")
        qapp.processEvents()

        widget.clear_data()
        qapp.processEvents()

        assert widget.plot_item.vb.state["autoRange"][1] is not False
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

        _assert_x_range(widget, (-0.5, 2.5))
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

        _assert_x_range(widget, (-0.5, 2.5))
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

        _assert_xy_range(widget, (0.0, 3.5), (0.0, 200.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_extends_first_time_column_to_zero(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.25, 0.75, 1.25],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]),
        )
        qapp.processEvents()

        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx((0.0, 1.5))
        _assert_x_range(widget, (0.0, 1.5))
    finally:
        widget.close()


def test_stft_image_chart_widget_real_default_overlap_timestamps_start_at_zero(qapp):
    times_sec, frequencies_hz, magnitude_db = _computed_stft_payload(overlap=0.5)
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=times_sec,
            frequencies_hz=frequencies_hz,
            magnitude_db=magnitude_db,
            snap_initial_to_zero=True,
        )
        qapp.processEvents()

        step = float(np.median(np.diff(times_sec)))
        assert float(times_sec[0]) == pytest.approx(step)
        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx((0.0, float(times_sec[-1]) + 0.5 * step))
        _assert_x_range(widget, (0.0, float(times_sec[-1]) + 0.5 * step))
    finally:
        widget.close()


def test_stft_image_chart_widget_real_high_overlap_timestamps_start_at_zero(qapp):
    times_sec, frequencies_hz, magnitude_db = _computed_stft_payload(overlap=0.75)
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=times_sec,
            frequencies_hz=frequencies_hz,
            magnitude_db=magnitude_db,
            snap_initial_to_zero=True,
        )
        qapp.processEvents()

        step = float(np.median(np.diff(times_sec)))
        assert float(times_sec[0]) == pytest.approx(2.0 * step)
        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx((0.0, float(times_sec[-1]) + 0.5 * step))
        _assert_x_range(widget, (0.0, float(times_sec[-1]) + 0.5 * step))
    finally:
        widget.close()


def test_stft_image_chart_widget_shifted_imported_real_timestamps_keep_absolute_left_extent(
    qapp,
):
    times_sec, frequencies_hz, magnitude_db = _computed_stft_payload(
        overlap=0.5,
        time_shift_sec=5.0,
    )
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=times_sec,
            frequencies_hz=frequencies_hz,
            magnitude_db=magnitude_db,
        )
        qapp.processEvents()

        step = float(np.median(np.diff(times_sec)))
        expected_left = float(times_sec[0]) - 0.5 * step
        expected_right = float(times_sec[-1]) + 0.5 * step
        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx((expected_left, expected_right))
        _assert_x_range(widget, (0.0, expected_right))
    finally:
        widget.close()


def test_stft_image_chart_widget_small_shifted_imported_timestamps_keep_absolute_left_extent(
    qapp,
):
    times_sec = np.asarray([0.1853, 0.3133, 0.4413], dtype=float)
    frequencies_hz = np.asarray([100.0, 200.0], dtype=float)
    magnitude_db = np.asarray([[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]], dtype=float)
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=times_sec,
            frequencies_hz=frequencies_hz,
            magnitude_db=magnitude_db,
            snap_initial_to_zero=False,
        )
        qapp.processEvents()

        step = float(np.median(np.diff(times_sec)))
        expected_left = float(times_sec[0]) - 0.5 * step
        expected_right = float(times_sec[-1]) + 0.5 * step
        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx((expected_left, expected_right))
        assert rendered[0] != pytest.approx(0.0)
        _assert_x_range(widget, (0.0, expected_right))
    finally:
        widget.close()


def test_stft_image_chart_widget_single_time_uses_exact_one_second_extent(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.25],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0], [2.0]]),
            snap_initial_to_zero=True,
        )
        qapp.processEvents()

        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx((0.0, 1.0))
        _assert_x_range(widget, (0.0, 1.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_single_imported_time_preserves_absolute_center(qapp):
    time_sec = 0.1853
    expected_extent = (time_sec - 0.5, time_sec + 0.5)
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[time_sec],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0], [2.0]]),
            snap_initial_to_zero=False,
        )
        qapp.processEvents()

        rendered = widget.rendered_time_extent_sec()
        assert rendered == pytest.approx(expected_extent)
        assert rendered != pytest.approx((0.0, 1.0))
        _assert_x_range(widget, (0.0, expected_extent[1]))
    finally:
        widget.close()


def test_stft_image_chart_widget_applies_fixed_levels(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0], [3.0, 4.0]]),
            levels_db=(-90.0, -10.0),
        )
        qapp.processEvents()

        assert widget.image_levels() == pytest.approx((-90.0, -10.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_fixed_levels_do_not_seed_auto_cache(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0], [3.0, 4.0]]),
            levels_db=(-90.0, -10.0),
        )

        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[20.0, 25.0], [30.0, 50.0]]),
            reset_levels=False,
        )
        qapp.processEvents()

        assert widget.image_levels() == pytest.approx((20.0, 50.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_fixed_levels_clear_prior_auto_cache(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0], [3.0, 4.0]]),
        )
        assert widget.image_levels() == pytest.approx((1.0, 4.0))

        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[10.0, 12.0], [14.0, 16.0]]),
            levels_db=(-90.0, -10.0),
        )

        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[20.0, 25.0], [30.0, 50.0]]),
            reset_levels=False,
        )
        qapp.processEvents()

        assert widget.image_levels() == pytest.approx((20.0, 50.0))
    finally:
        widget.close()


def test_stft_image_chart_widget_rejects_invalid_fixed_levels_without_mutation(qapp):
    widget = StftImageChartWidget()

    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=np.array([[1.0, 2.0], [3.0, 4.0]]),
            levels_db=(-90.0, -10.0),
        )
        previous_payload = widget.image_payload()
        previous_levels = widget.image_levels()

        with pytest.raises(ValueError):
            widget.set_image(
                times_sec=[0.0, 1.0],
                frequencies_hz=[100.0, 200.0],
                magnitude_db=np.array([[5.0, 6.0], [7.0, 8.0]]),
                levels_db=(-10.0, -90.0),
            )

        assert widget.image_payload()["times_sec"] == previous_payload["times_sec"]
        assert widget.image_levels() == previous_levels
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

        _assert_x_range(widget, (-0.5, 0.5))
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
        assert x_range == pytest.approx((0.0, 5.5))
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

        _assert_xy_range(widget, (0.0, 1.0), (0.0, 1.25))
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
        _assert_x_range(octave, (-0.5, 2.5))
        _assert_xy_range(stft, (0.0, 2.5), (0.0, 300.0))
    finally:
        line.close()
        fft.close()
        octave.close()
        stft.close()
