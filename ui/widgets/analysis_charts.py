from __future__ import annotations

from copy import deepcopy
from typing import Iterable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QRect, QRectF
from PyQt6.QtWidgets import QVBoxLayout, QWidget

pg.setConfigOptions(antialias=True, background="w", foreground="#334155")

_LINE_CHART_Y_AXIS_MODES = frozenset({"auto", "data_focus"})


def _normalize_vector(values: Iterable[float], *, name: str) -> list[float]:
    normalized: list[float] = []
    for value in values:
        try:
            normalized.append(float(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must contain only numeric values") from exc
    return normalized


def _normalize_image_matrix(matrix) -> np.ndarray:
    array = np.asarray(matrix, dtype=float)
    if array.ndim != 2:
        raise ValueError("magnitude_db must be a 2D matrix")
    return array


def _normalize_limit_bands(limit_bands: Iterable[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    if limit_bands is None:
        return normalized

    required_keys = ("freq_hz", "lower_db", "upper_db", "actual_db", "passed")
    for band in limit_bands:
        if not isinstance(band, dict):
            raise ValueError("limit_bands must contain dict entries")
        missing_keys = [key for key in required_keys if key not in band]
        if missing_keys:
            missing = ", ".join(missing_keys)
            raise ValueError(f"limit band is missing required keys: {missing}")
        normalized.append(
            {
                "freq_hz": float(band["freq_hz"]),
                "lower_db": float(band["lower_db"]),
                "upper_db": float(band["upper_db"]),
                "actual_db": float(band["actual_db"]),
                "passed": bool(band["passed"]),
            }
        )
    return normalized


def _normalize_optional_overlay_levels(
    levels_db: Iterable[float] | None,
    *,
    expected_length: int,
    name: str,
) -> list[float] | None:
    if levels_db is None:
        return None

    normalized = _normalize_vector(levels_db, name=name)
    if len(normalized) != expected_length:
        raise ValueError(f"{name} must be the same length as the plotted series")
    return normalized


def _nearest_positions(base_values: list[float], band_freqs: Iterable[float]) -> list[float]:
    if not base_values:
        return []

    base = np.asarray(base_values, dtype=float)
    positions: list[float] = []
    for freq_hz in band_freqs:
        index = int(np.abs(base - float(freq_hz)).argmin())
        positions.append(float(index))
    return positions


def _clear_error_bar_item(item: pg.ErrorBarItem) -> None:
    item.setData(x=None, y=None, top=None, bottom=None, beam=None)


def _visible_axis_max(values: Iterable[float] | None) -> float:
    if values is None:
        return 1.0

    normalized = [float(value) for value in values]
    if not normalized:
        return 1.0

    payload_min = min(normalized)
    payload_max = max(normalized)
    if payload_max <= 0.0 or np.isclose(payload_min, payload_max):
        return max(payload_max, 1.0)
    return payload_max


def _normalize_line_chart_y_axis_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    if normalized not in _LINE_CHART_Y_AXIS_MODES:
        raise ValueError(f"y_axis_mode must be one of {sorted(_LINE_CHART_Y_AXIS_MODES)}")
    return normalized


def _validate_data_focus_series(*, x_values: list[float], y_values: list[float]) -> tuple[float, float]:
    if not x_values or not y_values:
        raise ValueError("data_focus requires a non-empty series")

    x_array = np.asarray(x_values, dtype=float)
    y_array = np.asarray(y_values, dtype=float)
    if not np.all(np.isfinite(x_array)) or not np.all(np.isfinite(y_array)):
        raise ValueError("data_focus requires only finite plotted values")

    y_min = float(y_array.min())
    y_max = float(y_array.max())
    if np.isclose(y_min, y_max):
        padding = max(0.1 * abs(y_min), 0.5)
        return (y_min - padding, y_max + padding)

    padding = max(0.1 * (y_max - y_min), 0.1)
    return (y_min - padding, y_max + padding)


class _BasePlotChart(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis: bool = False,
    ):
        super().__init__(parent)
        self._hide_left_axis = bool(hide_left_axis)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot_widget = pg.PlotWidget(parent=self)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.hideButtons()
        layout.addWidget(self.plot_widget)

        self.plot_item = self.plot_widget.getPlotItem()
        if self._hide_left_axis:
            self._install_left_axis_hidden_label_guard()
        self.plot_item.showGrid(x=True, y=True, alpha=0.18)
        self._apply_x_axis_policy(payload_values=None)
        if self._hide_left_axis:
            self.apply_left_axis_hidden_style()

    def apply_left_axis_hidden_style(self) -> None:
        self.plot_item.hideAxis("left")

    def _install_left_axis_hidden_label_guard(self) -> None:
        original_set_label = self.plot_item.setLabel

        def set_label_with_left_axis_guard(*args, **kwargs):
            axis = args[0] if args else kwargs.get("axis")
            result = original_set_label(*args, **kwargs)
            if isinstance(axis, str) and axis.lower() == "left":
                self.apply_left_axis_hidden_style()
            return result

        self.plot_item.setLabel = set_label_with_left_axis_guard

    def _apply_x_axis_policy(self, *, payload_values: Iterable[float] | None) -> None:
        visible_max = _visible_axis_max(payload_values)
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        view_box.setXRange(0.0, visible_max, padding=0.0)


class StftImageChartWidget(_BasePlotChart):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis: bool = False,
    ):
        super().__init__(parent, hide_left_axis=hide_left_axis)
        self._image_payload: dict | None = None
        self._cached_levels: tuple[float, float] | None = None
        self._image_item = pg.ImageItem(axisOrder="row-major")
        self._image_item.setLookupTable(
            pg.ColorMap(
                pos=np.array([0.0, 0.35, 0.7, 1.0]),
                color=np.array(
                    [
                        [12, 36, 97, 255],
                        [25, 106, 166, 255],
                        [255, 196, 61, 255],
                        [234, 88, 12, 255],
                    ],
                    dtype=np.ubyte,
                ),
            ).getLookupTable(0.0, 1.0, 256)
        )
        self.plot_item.addItem(self._image_item)
        self.plot_item.setLabel("bottom", "Time", units="s")
        self.plot_item.setLabel("left", "Frequency", units="Hz")
        if self._hide_left_axis:
            self.apply_left_axis_hidden_style()
        self._apply_stft_y_axis_policy(payload_values=None)

    def set_image(
        self,
        *,
        times_sec: Iterable[float],
        frequencies_hz: Iterable[float],
        magnitude_db,
        reset_levels: bool = True,
    ) -> None:
        normalized_times = _normalize_vector(times_sec, name="times_sec")
        normalized_frequencies = _normalize_vector(
            frequencies_hz, name="frequencies_hz"
        )
        normalized_image = _normalize_image_matrix(magnitude_db)

        expected_shape = (len(normalized_frequencies), len(normalized_times))
        if normalized_image.shape != expected_shape:
            raise ValueError(
                "magnitude_db shape must match "
                f"(len(frequencies_hz), len(times_sec)); got {normalized_image.shape}"
            )

        self._image_payload = {
            "times_sec": normalized_times,
            "frequencies_hz": normalized_frequencies,
            "magnitude_db": normalized_image.copy(),
        }

        should_reset_levels = bool(reset_levels) or self._cached_levels is None
        self._image_item.setImage(normalized_image, autoLevels=should_reset_levels)
        if should_reset_levels:
            levels = self._image_item.getLevels()
            if levels is None:
                self._cached_levels = None
            else:
                self._cached_levels = (float(levels[0]), float(levels[1]))
        elif self._cached_levels is not None:
            self._image_item.setLevels(self._cached_levels)
        self._image_item.setRect(self._image_rect())
        rendered_rect = self._rendered_image_rect()
        if rendered_rect is None:
            x_axis_values = normalized_times
            y_axis_values = normalized_frequencies
        else:
            x_axis_values = (float(rendered_rect.left()), float(rendered_rect.right()))
            y_axis_values = (float(rendered_rect.top()), float(rendered_rect.bottom()))

        self._apply_x_axis_policy(payload_values=x_axis_values)
        self._apply_stft_y_axis_policy(payload_values=y_axis_values)

    def clear_data(self) -> None:
        self._image_payload = None
        self._cached_levels = None
        self._image_item.clear()
        self._apply_x_axis_policy(payload_values=None)
        self._apply_stft_y_axis_policy(payload_values=None)

    def has_image(self) -> bool:
        return self._image_payload is not None

    def image_payload(self) -> dict | None:
        if self._image_payload is None:
            return None
        payload = dict(self._image_payload)
        payload["times_sec"] = list(self._image_payload["times_sec"])
        payload["frequencies_hz"] = list(self._image_payload["frequencies_hz"])
        payload["magnitude_db"] = self._image_payload["magnitude_db"].copy()
        return payload

    def plot_area_rect_in_widget(self) -> QRect | None:
        if not self.isVisible() or not self.plot_widget.isVisible():
            return None
        if self.plot_widget.width() <= 0 or self.plot_widget.height() <= 0:
            return None

        view_box = self.plot_item.vb
        if not view_box.isVisible():
            return None

        scene_rect = view_box.sceneBoundingRect()
        if scene_rect.isEmpty() or scene_rect.width() <= 0 or scene_rect.height() <= 0:
            return None

        top_left = self.plot_widget.mapTo(
            self,
            self.plot_widget.mapFromScene(scene_rect.topLeft()),
        )
        bottom_right = self.plot_widget.mapTo(
            self,
            self.plot_widget.mapFromScene(scene_rect.bottomRight()),
        )
        rect = QRect(top_left, bottom_right).normalized()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        return rect

    def rendered_time_extent_sec(self) -> tuple[float, float] | None:
        rendered_rect = self._rendered_image_rect()
        if rendered_rect is None:
            return None
        return (float(rendered_rect.left()), float(rendered_rect.right()))

    def widget_x_for_time_sec(self, time_sec: float) -> float | None:
        mapping_context = self._stft_mapping_context()
        if mapping_context is None:
            return None

        view_y, _device_y = mapping_context
        device_point = self.plot_item.vb.mapViewToDevice(
            QPointF(float(time_sec), view_y)
        )
        device_x = float(device_point.x())
        if not np.isfinite(device_x):
            return None
        return device_x + float(self.plot_widget.x())

    def time_sec_for_widget_x(self, widget_x: float) -> float | None:
        mapping_context = self._stft_mapping_context()
        if mapping_context is None:
            return None

        _view_y, device_y = mapping_context
        widget_x_value = float(widget_x)
        if not np.isfinite(widget_x_value):
            return None

        view_point = self.plot_item.vb.mapDeviceToView(
            QPointF(widget_x_value - float(self.plot_widget.x()), device_y)
        )
        time_sec = float(view_point.x())
        if not np.isfinite(time_sec):
            return None
        return time_sec

    def _apply_stft_y_axis_policy(
        self,
        *,
        payload_values: Iterable[float] | None,
    ) -> None:
        visible_max = _visible_axis_max(payload_values)
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        view_box.setYRange(0.0, visible_max, padding=0.0)

    def _image_rect(self) -> QRectF:
        assert self._image_payload is not None
        times_sec = self._image_payload["times_sec"]
        frequencies_hz = self._image_payload["frequencies_hz"]
        x_min = times_sec[0]
        x_max = times_sec[-1] if len(times_sec) > 1 else times_sec[0] + 1.0
        y_min = frequencies_hz[0]
        y_max = (
            frequencies_hz[-1]
            if len(frequencies_hz) > 1
            else frequencies_hz[0] + 1.0
        )
        return QRectF(x_min, y_min, x_max - x_min, y_max - y_min)

    def _rendered_image_rect(self) -> QRectF | None:
        if self._image_payload is None:
            return None
        rendered_rect = self._image_item.mapRectToParent(
            self._image_item.boundingRect()
        )
        if rendered_rect.isEmpty() or rendered_rect.width() <= 0:
            return None
        return rendered_rect

    def _stft_mapping_context(self) -> tuple[float, float] | None:
        if self.plot_area_rect_in_widget() is None:
            return None

        rendered_rect = self._rendered_image_rect()
        if rendered_rect is None:
            return None

        view_y = float(rendered_rect.center().y())
        reference_view = rendered_rect.center()
        reference_device = self.plot_item.vb.mapViewToDevice(reference_view)
        device_y = float(reference_device.y())
        if not np.isfinite(view_y) or not np.isfinite(device_y):
            return None
        return (view_y, device_y)


class LineChartWidget(_BasePlotChart):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis: bool = False,
    ):
        super().__init__(parent, hide_left_axis=hide_left_axis)
        self._series_payload: dict | None = None
        self._y_axis_mode = "auto"
        self._curve = self.plot_item.plot(
            pen=pg.mkPen(color="#2563eb", width=2),
        )
        self._lower_limit_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(color="#dc2626", style=pg.QtCore.Qt.PenStyle.DashLine),
            movable=False,
        )
        self._upper_limit_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(color="#ea580c", style=pg.QtCore.Qt.PenStyle.DashLine),
            movable=False,
        )
        self.plot_item.addItem(self._lower_limit_line)
        self.plot_item.addItem(self._upper_limit_line)
        self._lower_limit_line.hide()
        self._upper_limit_line.hide()

    def set_series(
        self,
        x_values: Iterable[float],
        y_values: Iterable[float],
        *,
        lower_limit: float | None = None,
        upper_limit: float | None = None,
        y_axis_mode: str = "auto",
    ) -> None:
        normalized_x = _normalize_vector(x_values, name="x_values")
        normalized_y = _normalize_vector(y_values, name="y_values")
        if len(normalized_x) != len(normalized_y):
            raise ValueError("x_values and y_values must be the same length")
        normalized_mode = _normalize_line_chart_y_axis_mode(y_axis_mode)
        data_focus_y_range = None
        if normalized_mode == "data_focus":
            data_focus_y_range = _validate_data_focus_series(
                x_values=normalized_x,
                y_values=normalized_y,
            )

        self._series_payload = {
            "x_values": normalized_x,
            "y_values": normalized_y,
            "lower_limit": None if lower_limit is None else float(lower_limit),
            "upper_limit": None if upper_limit is None else float(upper_limit),
        }
        self._y_axis_mode = normalized_mode

        self._curve.setData(normalized_x, normalized_y)
        self._update_limit_line(
            self._lower_limit_line,
            self._series_payload["lower_limit"],
        )
        self._update_limit_line(
            self._upper_limit_line,
            self._series_payload["upper_limit"],
        )
        self._apply_x_axis_policy(payload_values=normalized_x)
        if data_focus_y_range is None:
            self._apply_auto_y_axis_policy()
        else:
            self._apply_data_focus_y_axis_policy(*data_focus_y_range)

    def clear_data(self) -> None:
        self._series_payload = None
        self._curve.setData([], [])
        self._lower_limit_line.hide()
        self._upper_limit_line.hide()
        self._apply_x_axis_policy(payload_values=None)
        if self._y_axis_mode == "data_focus":
            self._apply_data_focus_y_axis_policy(0.0, 1.0)
        else:
            self._apply_auto_y_axis_policy()

    def has_series(self) -> bool:
        return self._series_payload is not None

    def series_payload(self) -> dict | None:
        return deepcopy(self._series_payload)

    @staticmethod
    def _update_limit_line(limit_line: pg.InfiniteLine, value: float | None) -> None:
        if value is None:
            limit_line.hide()
            return
        limit_line.setPos(value)
        limit_line.show()

    def _apply_auto_y_axis_policy(self) -> None:
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        view_box.updateAutoRange()

    def _apply_data_focus_y_axis_policy(self, y_min: float, y_max: float) -> None:
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        view_box.setYRange(y_min, y_max, padding=0.0)


class FftLineChartWidget(LineChartWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis: bool = False,
    ):
        super().__init__(parent, hide_left_axis=hide_left_axis)
        self._spectrum_payload: dict | None = None
        self._golden_overlay_curve_item = self.plot_item.plot(
            pen=pg.mkPen(
                color="#64748b",
                width=2,
                style=pg.QtCore.Qt.PenStyle.DashLine,
            ),
        )
        self._band_range_item = pg.ErrorBarItem()
        self._band_marker_item = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None))
        self._zero_reference_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(
                color="#94a3b8",
                width=1,
                style=pg.QtCore.Qt.PenStyle.DashLine,
            ),
            movable=False,
        )
        self.plot_item.addItem(self._zero_reference_line)
        self.plot_item.addItem(self._band_range_item)
        self.plot_item.addItem(self._band_marker_item)
        self._zero_reference_line.hide()
        self.plot_item.setLabel("bottom", "Frequency", units="Hz")
        self.plot_item.setLabel("left", "Level", units="dB")
        if self._hide_left_axis:
            self.apply_left_axis_hidden_style()

    def set_spectrum(
        self,
        *,
        frequencies_hz: Iterable[float],
        levels_db: Iterable[float],
        limit_bands: Iterable[dict] | None = None,
        golden_overlay_levels_db: Iterable[float] | None = None,
        show_zero_reference: bool = False,
    ) -> None:
        normalized_frequencies = _normalize_vector(
            frequencies_hz, name="frequencies_hz"
        )
        normalized_levels = _normalize_vector(levels_db, name="levels_db")
        normalized_limit_bands = _normalize_limit_bands(limit_bands)
        normalized_golden_overlay = _normalize_optional_overlay_levels(
            golden_overlay_levels_db,
            expected_length=len(normalized_frequencies),
            name="golden_overlay_levels_db",
        )

        self.set_series(normalized_frequencies, normalized_levels)
        self._spectrum_payload = {
            "frequencies_hz": normalized_frequencies,
            "levels_db": normalized_levels,
            "limit_bands": normalized_limit_bands,
            "golden_overlay_levels_db": normalized_golden_overlay,
            "show_zero_reference": bool(show_zero_reference),
        }

        self._render_golden_overlay(
            x_values=normalized_frequencies,
            levels_db=normalized_golden_overlay,
        )
        self._update_zero_reference_line(bool(show_zero_reference))
        self._render_band_markers(
            x_values=[band["freq_hz"] for band in normalized_limit_bands],
            limit_bands=normalized_limit_bands,
        )
        self._apply_x_axis_policy(payload_values=normalized_frequencies)
        self.plot_item.vb.updateAutoRange()

    def clear_data(self) -> None:
        _clear_error_bar_item(self._band_range_item)
        super().clear_data()
        self._spectrum_payload = None
        self._golden_overlay_curve_item.setData([], [])
        self._band_marker_item.setData([])
        self._zero_reference_line.hide()

    def spectrum_payload(self) -> dict | None:
        return deepcopy(self._spectrum_payload)

    def _render_golden_overlay(
        self,
        *,
        x_values: list[float],
        levels_db: list[float] | None,
    ) -> None:
        if levels_db is None:
            self._golden_overlay_curve_item.setData([], [])
            return
        self._golden_overlay_curve_item.setData(x_values, levels_db)

    def _update_zero_reference_line(self, show_zero_reference: bool) -> None:
        if not show_zero_reference:
            self._zero_reference_line.hide()
            return
        self._zero_reference_line.setPos(0.0)
        self._zero_reference_line.show()

    def _render_band_markers(
        self,
        *,
        x_values: list[float],
        limit_bands: list[dict],
    ) -> None:
        if not limit_bands:
            _clear_error_bar_item(self._band_range_item)
            self._band_marker_item.setData([])
            return

        centers = np.array(
            [
                (band["lower_db"] + band["upper_db"]) / 2.0
                for band in limit_bands
            ],
            dtype=float,
        )
        spans = np.array(
            [
                (band["upper_db"] - band["lower_db"]) / 2.0
                for band in limit_bands
            ],
            dtype=float,
        )
        x_array = np.asarray(x_values, dtype=float)
        self._band_range_item.setData(
            x=x_array,
            y=centers,
            top=spans,
            bottom=spans,
            beam=0.0,
        )
        self._band_marker_item.setData(
            [
                {
                    "pos": (band["freq_hz"], band["actual_db"]),
                    "brush": "#16a34a" if band["passed"] else "#dc2626",
                }
                for band in limit_bands
            ]
        )


class OctaveBarChartWidget(_BasePlotChart):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis: bool = False,
    ):
        super().__init__(parent, hide_left_axis=hide_left_axis)
        self._band_payload: dict | None = None
        self._bar_item: pg.BarGraphItem | None = None
        self._golden_overlay_item = pg.PlotDataItem(
            pen=pg.mkPen(
                color="#64748b",
                width=2,
                style=pg.QtCore.Qt.PenStyle.DashLine,
            ),
            symbol="o",
            symbolSize=7,
            symbolBrush="#64748b",
            symbolPen=pg.mkPen(color="#475569", width=1),
        )
        self._band_range_item = pg.ErrorBarItem()
        self._band_marker_item = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None))
        self._zero_reference_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(
                color="#94a3b8",
                width=1,
                style=pg.QtCore.Qt.PenStyle.DashLine,
            ),
            movable=False,
        )
        self.plot_item.addItem(self._zero_reference_line)
        self.plot_item.addItem(self._golden_overlay_item)
        self.plot_item.addItem(self._band_range_item)
        self.plot_item.addItem(self._band_marker_item)
        self._zero_reference_line.hide()
        self.plot_item.setLabel("bottom", "Band", units="Hz")
        self.plot_item.setLabel("left", "Level", units="dB")
        if self._hide_left_axis:
            self.apply_left_axis_hidden_style()

    def set_bands(
        self,
        *,
        center_freqs_hz: Iterable[float],
        levels_db: Iterable[float],
        limit_bands: Iterable[dict] | None = None,
        golden_overlay_levels_db: Iterable[float] | None = None,
        show_zero_reference: bool = False,
    ) -> None:
        normalized_frequencies = _normalize_vector(
            center_freqs_hz, name="center_freqs_hz"
        )
        normalized_levels = _normalize_vector(levels_db, name="levels_db")
        if len(normalized_frequencies) != len(normalized_levels):
            raise ValueError("center_freqs_hz and levels_db must be the same length")

        normalized_limit_bands = _normalize_limit_bands(limit_bands)
        normalized_golden_overlay = _normalize_optional_overlay_levels(
            golden_overlay_levels_db,
            expected_length=len(normalized_frequencies),
            name="golden_overlay_levels_db",
        )
        self._band_payload = {
            "center_freqs_hz": normalized_frequencies,
            "levels_db": normalized_levels,
            "limit_bands": normalized_limit_bands,
            "golden_overlay_levels_db": normalized_golden_overlay,
            "show_zero_reference": bool(show_zero_reference),
        }

        x_values = np.arange(len(normalized_frequencies), dtype=float)
        self._replace_bar_item(
            x=x_values,
            heights=np.asarray(normalized_levels, dtype=float),
        )
        self._render_golden_overlay(
            x_values=x_values,
            levels_db=normalized_golden_overlay,
        )
        self._update_zero_reference_line(bool(show_zero_reference))
        visible_labels_hz = {
            40.0,
            80.0,
            160.0,
            315.0,
            630.0,
            1250.0,
            2500.0,
            5000.0,
            10000.0,
            16000.0,
        }
        ticks = [
            (
                float(index),
                self._format_frequency_label(freq_hz)
                if freq_hz in visible_labels_hz
                else "",
            )
            for index, freq_hz in enumerate(normalized_frequencies)
        ]
        self.plot_item.getAxis("bottom").setTicks([ticks])
        self._render_band_markers(
            x_values=_nearest_positions(
                normalized_frequencies,
                [band["freq_hz"] for band in normalized_limit_bands],
            ),
            limit_bands=normalized_limit_bands,
        )
        self._apply_x_axis_policy(payload_values=x_values.tolist())
        self.plot_item.vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.plot_item.vb.updateAutoRange()

    def clear_data(self) -> None:
        self._band_payload = None
        _clear_error_bar_item(self._band_range_item)
        self._golden_overlay_item.setData([], [])
        self._band_marker_item.setData([])
        self._replace_bar_item(x=np.array([], dtype=float), heights=np.array([], dtype=float))
        self.plot_item.getAxis("bottom").setTicks(None)
        self._apply_x_axis_policy(payload_values=None)
        self._zero_reference_line.hide()

    def has_bars(self) -> bool:
        return self._band_payload is not None

    def band_payload(self) -> dict | None:
        return deepcopy(self._band_payload)

    def _replace_bar_item(self, *, x: np.ndarray, heights: np.ndarray) -> None:
        if self._bar_item is not None:
            self.plot_item.removeItem(self._bar_item)
        self._bar_item = pg.BarGraphItem(
            x=x,
            y0=0.0,
            height=heights,
            width=0.6,
            brush="#f59e0b",
            pen=pg.mkPen(color="#d97706", width=1),
        )
        self.plot_item.addItem(self._bar_item)

    def _render_golden_overlay(
        self,
        *,
        x_values: np.ndarray,
        levels_db: list[float] | None,
    ) -> None:
        if levels_db is None:
            self._golden_overlay_item.setData([], [])
            return
        self._golden_overlay_item.setData(x_values, levels_db)

    def _update_zero_reference_line(self, show_zero_reference: bool) -> None:
        if not show_zero_reference:
            self._zero_reference_line.hide()
            return
        self._zero_reference_line.setPos(0.0)
        self._zero_reference_line.show()

    def _render_band_markers(
        self,
        *,
        x_values: list[float],
        limit_bands: list[dict],
    ) -> None:
        if not limit_bands:
            _clear_error_bar_item(self._band_range_item)
            self._band_marker_item.setData([])
            return

        centers = np.array(
            [
                (band["lower_db"] + band["upper_db"]) / 2.0
                for band in limit_bands
            ],
            dtype=float,
        )
        spans = np.array(
            [
                (band["upper_db"] - band["lower_db"]) / 2.0
                for band in limit_bands
            ],
            dtype=float,
        )
        x_array = np.asarray(x_values, dtype=float)
        self._band_range_item.setData(
            x=x_array,
            y=centers,
            top=spans,
            bottom=spans,
            beam=0.3,
        )
        self._band_marker_item.setData(
            [
                {
                    "pos": (x_value, band["actual_db"]),
                    "brush": "#16a34a" if band["passed"] else "#dc2626",
                }
                for x_value, band in zip(x_values, limit_bands, strict=False)
            ]
        )

    @staticmethod
    def _format_frequency_label(freq_hz: float) -> str:
        if freq_hz >= 1000.0 and float(freq_hz).is_integer():
            return f"{int(freq_hz)}"
        return f"{freq_hz:g}"


__all__ = [
    "FftLineChartWidget",
    "LineChartWidget",
    "OctaveBarChartWidget",
    "StftImageChartWidget",
]
