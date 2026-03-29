from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.modules.octave import OctaveModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.widgets.analysis_charts import (
    FftLineChartWidget,
    LineChartWidget,
    OctaveBarChartWidget,
    StftImageChartWidget,
)

REQUIRED_CHART_RUNTIME_DEPENDENCY = "pyqtgraph==0.13.7"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _SignalStub:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.factory_state_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.spl_updated = _SignalStub()
        self.fft_updated = _SignalStub()
        self.octave_updated = _SignalStub()


def _state(config_id: int, phase: str) -> FactoryControllerState:
    return FactoryControllerState(
        displayed_config_id=config_id,
        run_phase=phase,
        trigger_source="manual",
    )


def _chart_payload_for_channels(channels: tuple[int, ...]) -> dict:
    payload: dict[int, dict] = {}
    for channel_index in channels:
        payload[channel_index] = {
            "spl": {
                "times_sec": [0.0, 1.0],
                "values_db": [70.0 + channel_index, 71.0 + channel_index],
                "lower_limit_db": 60.0,
                "upper_limit_db": 80.0,
            },
            "fft": {
                "frequencies_hz": [125.0, 250.0],
                "levels_db": [40.0 + channel_index, 42.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 35.0,
                        "upper_db": 45.0,
                        "actual_db": 42.0 + channel_index,
                        "passed": True,
                    }
                ],
            },
            "octave": {
                "center_freqs_hz": [125.0, 250.0],
                "levels_db": [32.0 + channel_index, 34.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 30.0,
                        "upper_db": 40.0,
                        "actual_db": 34.0 + channel_index,
                        "passed": True,
                    }
                ],
            },
        }
    return payload


def _review_payload_with_chart_payload(
    config_id: int,
    *,
    run_id: str = "run-101",
    channels: tuple[int, ...] = (0, 1),
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id,
        trigger_source="manual",
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload=_chart_payload_for_channels(channels),
    )


def _make_services(
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
):
    bus = _BusStub()
    factory_mode = SimpleNamespace(
        state=initial_state or FactoryControllerState(),
        review_payload=initial_review_payload or FactoryReviewPayload(),
    )
    return SimpleNamespace(
        bus=bus,
        factory_mode=factory_mode,
    )


def _assert_empty_chart_visible(module, qapp) -> None:
    module.show()
    qapp.processEvents()

    assert module.has_committed_plot_data() is False
    assert module.current_plot_payload() is None
    assert module.current_render_channel() is None
    assert module.plot.isHidden() is False

    has_series = getattr(module.plot, "has_series", None)
    if callable(has_series):
        assert has_series() is False

    has_bars = getattr(module.plot, "has_bars", None)
    if callable(has_bars):
        assert has_bars() is False


def _show_widget(widget, qapp) -> None:
    widget.resize(640, 260)
    widget.show()
    qapp.processEvents()


def _assert_x_range(widget, expected_range: tuple[float, float]) -> None:
    x_range, _ = widget.plot_item.vb.viewRange()
    assert x_range == pytest.approx(expected_range)


def _assert_xy_range(
    widget,
    expected_x_range: tuple[float, float],
    expected_y_range: tuple[float, float],
) -> None:
    x_range, y_range = widget.plot_item.vb.viewRange()
    assert x_range == pytest.approx(expected_x_range)
    assert y_range == pytest.approx(expected_y_range)


def test_requirements_txt_is_utf8_and_lists_chart_runtime_dependency():
    requirements_path = Path(__file__).resolve().parents[1] / "requirements.txt"
    requirements_text = requirements_path.read_text(encoding="utf-8")
    entries = {
        line.split("#", 1)[0].strip().casefold()
        for line in requirements_text.splitlines()
        if line.split("#", 1)[0].strip()
    }

    assert isinstance(requirements_text, str)
    assert REQUIRED_CHART_RUNTIME_DEPENDENCY in entries


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

        assert widget.rendered_time_extent_sec() == pytest.approx((0.0, 1.0))
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
        assert widget.widget_x_for_time_sec(3.0) == pytest.approx(
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


def test_spl_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SplModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["x_values"] == [0.0, 1.0]
    assert module.current_plot_payload()["y_values"] == [71.0, 72.0]
    assert module.current_render_channel() == 1


def test_spectrum_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["frequencies_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["levels_db"] == [41.0, 43.0]
    assert module.current_render_channel() == 1


def test_spectrum_module_renders_committed_chart_without_limit_band_markers(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"]["limit_bands"] = []

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["frequencies_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["limit_bands"] == []
    module.plot._band_range_item.boundingRect()


def test_spectrum_module_keeps_two_committed_limit_band_markers_renderable(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"]["limit_bands"] = [
        {
            "freq_hz": 125.0,
            "lower_db": 35.0,
            "upper_db": 45.0,
            "actual_db": 40.0,
            "passed": True,
        },
        {
            "freq_hz": 250.0,
            "lower_db": 37.0,
            "upper_db": 43.0,
            "actual_db": 42.0,
            "passed": True,
        },
    ]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert len(module.current_plot_payload()["limit_bands"]) == 2
    module.plot._band_range_item.boundingRect()


def test_octave_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["center_freqs_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["levels_db"] == [33.0, 35.0]
    assert module.current_render_channel() == 1


def test_octave_module_renders_committed_chart_without_limit_band_markers(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["octave"]["limit_bands"] = []

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["center_freqs_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["limit_bands"] == []
    module.plot._band_range_item.boundingRect()


def test_octave_module_keeps_two_committed_limit_band_markers_renderable(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["octave"]["limit_bands"] = [
        {
            "freq_hz": 125.0,
            "lower_db": 30.0,
            "upper_db": 36.0,
            "actual_db": 32.0,
            "passed": True,
        },
        {
            "freq_hz": 250.0,
            "lower_db": 30.0,
            "upper_db": 40.0,
            "actual_db": 34.0,
            "passed": True,
        },
    ]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert len(module.current_plot_payload()["limit_bands"]) == 2
    module.plot._band_range_item.boundingRect()


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule))
def test_committed_chart_modules_stay_empty_in_reviewing_when_chart_payload_is_missing(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        FactoryReviewPayload(
            displayed_config_id=101,
            run_id="run-101",
            trigger_source="manual",
            summary_payload={
                "overall_status": "no_result",
                "overall_label": "无结果",
                "error_message": "analysis unavailable",
                "rows": [],
            },
            chart_payload=None,
        )
    )
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule))
@pytest.mark.parametrize(
    "initial_state",
    (
        FactoryControllerState(),
        _state(101, "running"),
        _state(101, "analyzing"),
        _state(101, "reanalyzing"),
    ),
)
def test_committed_chart_modules_keep_empty_chart_visible_in_no_data_phases(
    qapp,
    module_cls,
    initial_state,
):
    services = _make_services(initial_state=initial_state)
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0

    _assert_empty_chart_visible(module, qapp)


@pytest.mark.parametrize(
    ("module_cls", "live_signal_name"),
    (
        (SplModule, "spl_updated"),
        (SpectrumModule, "fft_updated"),
        (OctaveModule, "octave_updated"),
    ),
)
def test_chart_modules_ignore_live_frames_and_clear_when_reanalysis_starts(
    qapp,
    module_cls,
    live_signal_name,
):
    services = _make_services(initial_state=_state(101, "running"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "running"))
    getattr(services.bus, live_signal_name).emit(101, "run-101", 0, object())
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True

    services.bus.factory_state_changed.emit(_state(101, "reanalyzing"))
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule))
def test_chart_module_falls_back_to_first_committed_channel_before_empty_state(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    fallback_calls: list[int] = []
    module.selected_display_channel_provider = lambda: 1
    module.selected_display_channel_sync = fallback_calls.append

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(2, 0))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 2
    assert fallback_calls == [2]
