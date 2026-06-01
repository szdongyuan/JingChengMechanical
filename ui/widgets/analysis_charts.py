from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui.widgets.chart_interactions import (
    ChartInteractionController,
    CsvExportScope,
    CsvTable,
)

pg.setConfigOptions(antialias=True, background="w", foreground="#334155")

_LINE_CHART_Y_AXIS_MODES = frozenset({"auto", "data_focus"})
_FFT_SPECTRUM_LINE_WIDTH = 0.7


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


def _normalize_fft_x_axis_scale(value) -> str:
    return "log" if str(value or "").strip().lower() == "log" else "linear"


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


def _format_csv_number(value: float | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if not np.isfinite(number):
        return ""
    return f"{number:.17g}"


@dataclass(frozen=True)
class LineChartExportSchema:
    x_header: str
    y_header: str
    lower_header: str
    upper_header: str
    metadata_headers: tuple[str, ...] = ()

    @classmethod
    def generic(cls) -> "LineChartExportSchema":
        return cls(
            x_header="x_values",
            y_header="y_values",
            lower_header="lower_limit",
            upper_header="upper_limit",
        )

    @classmethod
    def spl(cls) -> "LineChartExportSchema":
        return cls(
            x_header="time_sec",
            y_header="value_db",
            lower_header="lower_limit_db",
            upper_header="upper_limit_db",
        )

    @classmethod
    def loudness(cls) -> "LineChartExportSchema":
        return cls(
            x_header="time_sec",
            y_header="value",
            lower_header="lower_limit",
            upper_header="upper_limit",
            metadata_headers=("unit",),
        )


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
        hide_left_axis_label: bool = False,
    ):
        super().__init__(parent)
        self._hide_left_axis_label = bool(hide_left_axis_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot_widget = pg.PlotWidget(parent=self)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.hideButtons()
        layout.addWidget(self.plot_widget)

        self.plot_item = self.plot_widget.getPlotItem()
        if self._hide_left_axis_label:
            self._install_left_axis_hidden_label_guard()
        self.plot_item.showGrid(x=True, y=True, alpha=0.18)
        self._apply_x_axis_policy(payload_values=None)
        if self._hide_left_axis_label:
            self.apply_left_axis_label_hidden_style()

    def apply_left_axis_label_hidden_style(self) -> None:
        left_axis = self.plot_item.getAxis("left")
        left_axis.show()
        left_axis.setStyle(showValues=True)
        original_set_label = getattr(self, "_original_set_label", self.plot_item.setLabel)
        original_set_label("left", "", units="")

    def _install_left_axis_hidden_label_guard(self) -> None:
        self._original_set_label = self.plot_item.setLabel

        def set_label_with_left_axis_guard(*args, **kwargs):
            axis = args[0] if args else kwargs.get("axis")
            result = self._original_set_label(*args, **kwargs)
            if (
                isinstance(axis, str)
                and axis.lower() == "left"
                and self._hide_left_axis_label
            ):
                self.apply_left_axis_label_hidden_style()
            return result

        self.plot_item.setLabel = set_label_with_left_axis_guard

    def _apply_x_axis_policy(self, *, payload_values: Iterable[float] | None) -> None:
        visible_max = _visible_axis_max(payload_values)
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        view_box.setXRange(0.0, visible_max, padding=0.0)

    def domain_x_range_from_display_range(
        self,
        display_range: tuple[float, float],
    ) -> tuple[float, float]:
        lower, upper = sorted((float(display_range[0]), float(display_range[1])))
        return (lower, upper)

    def display_x_range_from_domain_range(
        self,
        domain_range: tuple[float, float],
    ) -> tuple[float, float]:
        lower, upper = sorted((float(domain_range[0]), float(domain_range[1])))
        return (lower, upper)


class StftImageChartWidget(_BasePlotChart):
    hover_time_changed = pyqtSignal(float)
    left_time_clicked = pyqtSignal(float)
    right_clicked = pyqtSignal()
    mouse_left_plot = pyqtSignal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis_label: bool = False,
    ):
        super().__init__(parent, hide_left_axis_label=hide_left_axis_label)
        self._image_payload: dict | None = None
        self._cached_levels: tuple[float, float] | None = None
        self._left_press_pos: QPoint | None = None
        self._left_press_inside_plot = False
        self._score_inspection_enabled = True
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
        self._snap_initial_to_zero = False
        self.plot_item.setLabel("bottom", "时间", units="s")
        self.plot_item.setLabel("left", "频率", units="Hz")
        if self._hide_left_axis_label:
            self.apply_left_axis_label_hidden_style()
        self._apply_stft_y_axis_policy(payload_values=None)
        self._install_mouse_event_hooks()
        self.chart_interactions = ChartInteractionController(
            self,
            parent=self,
            pass_through_left_clicks=True,
            pass_through_right_clicks=True,
            context_menu_actions=("reset_view", "save_png", "copy_image"),
        )

    def eventFilter(self, watched, event):
        if watched is self.plot_widget.viewport():
            if not self._score_inspection_enabled:
                if event.type() == QEvent.Type.Leave:
                    self._left_press_pos = None
                    self._left_press_inside_plot = False
                return super().eventFilter(watched, event)
            if event.type() in {QEvent.Type.MouseMove, QEvent.Type.HoverMove}:
                time_sec = self._event_time_sec(event)
                if time_sec is None:
                    self.mouse_left_plot.emit()
                else:
                    self.hover_time_changed.emit(time_sec)
            elif event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._left_press_pos = event.position().toPoint()
                    self._left_press_inside_plot = (
                        self._event_time_sec(event) is not None
                    )
                elif event.button() == Qt.MouseButton.RightButton:
                    self.right_clicked.emit()
            elif event.type() == QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._handle_left_release(event)
            elif event.type() == QEvent.Type.Leave:
                self._left_press_pos = None
                self._left_press_inside_plot = False
                self.mouse_left_plot.emit()
        return super().eventFilter(watched, event)

    def _install_mouse_event_hooks(self) -> None:
        self.setMouseTracking(True)
        self.plot_widget.setMouseTracking(True)
        viewport = self.plot_widget.viewport()
        viewport.setMouseTracking(True)
        viewport.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        viewport.installEventFilter(self)

    def set_score_inspection_enabled(self, enabled: bool) -> None:
        self._score_inspection_enabled = bool(enabled)
        if self._score_inspection_enabled:
            return
        self._left_press_pos = None
        self._left_press_inside_plot = False
        self.mouse_left_plot.emit()

    def _handle_left_release(self, event) -> None:
        press_pos = self._left_press_pos
        press_inside_plot = self._left_press_inside_plot
        self._left_press_pos = None
        self._left_press_inside_plot = False
        if press_pos is None or not press_inside_plot:
            return

        release_pos = event.position().toPoint()
        movement_px = (release_pos - press_pos).manhattanLength()
        if movement_px > QApplication.startDragDistance():
            return

        time_sec = self._event_time_sec(event)
        if time_sec is not None:
            self.left_time_clicked.emit(time_sec)

    def _event_time_sec(self, event) -> float | None:
        plot_rect = self.plot_area_rect_in_widget()
        if plot_rect is None:
            return None

        event_pos = event.position().toPoint()
        widget_pos = self.plot_widget.viewport().mapTo(self, event_pos)
        if not plot_rect.contains(widget_pos):
            return None
        return self.time_sec_for_widget_x(float(widget_pos.x()))

    def set_image(
        self,
        *,
        times_sec: Iterable[float],
        frequencies_hz: Iterable[float],
        magnitude_db,
        reset_levels: bool = True,
        levels_db: tuple[float, float] | None = None,
        snap_initial_to_zero: bool = False,
    ) -> None:
        fixed_levels = self._validated_levels(levels_db)
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

        self._snap_initial_to_zero = bool(snap_initial_to_zero)
        self._image_payload = {
            "times_sec": normalized_times,
            "frequencies_hz": normalized_frequencies,
            "magnitude_db": normalized_image.copy(),
        }

        if fixed_levels is not None:
            self._cached_levels = None
            self._image_item.setImage(normalized_image, autoLevels=False)
            self._image_item.setLevels(fixed_levels)
        else:
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
        self.chart_interactions.refresh_full_view()

    def clear_data(self) -> None:
        self._image_payload = None
        self._cached_levels = None
        self._snap_initial_to_zero = False
        self._image_item.clear()
        self._apply_x_axis_policy(payload_values=None)
        self._apply_stft_y_axis_policy(payload_values=None)
        self.chart_interactions.refresh_full_view()

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

    def image_levels(self) -> tuple[float, float] | None:
        levels = self._image_item.getLevels()
        if levels is None:
            return None
        return (float(levels[0]), float(levels[1]))

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
        step = self._time_step(times_sec)
        if len(times_sec) == 1:
            x_min = float(times_sec[0]) - 0.5
            if self._snap_initial_to_zero:
                x_min = max(0.0, x_min)
            x_width = 1.0
        else:
            first_time = float(times_sec[0])
            should_snap_initial = (
                self._snap_initial_to_zero
                and self._should_snap_initial_left_edge_to_zero(first_time, step)
            )
            if should_snap_initial:
                x_min = 0.0
            else:
                x_min = max(0.0, first_time - 0.5 * step)
            x_max = float(times_sec[-1]) + 0.5 * step
            x_width = max(step, x_max - x_min)
        y_min = frequencies_hz[0]
        y_max = (
            frequencies_hz[-1]
            if len(frequencies_hz) > 1
            else frequencies_hz[0] + 1.0
        )
        return QRectF(x_min, y_min, x_width, y_max - y_min)

    @staticmethod
    def _time_step(values: list[float]) -> float:
        if len(values) < 2:
            return 1.0
        deltas = np.diff(np.asarray(values, dtype=float))
        positive = deltas[np.isfinite(deltas) & (deltas > 0.0)]
        if positive.size == 0:
            return 1.0
        return float(np.median(positive))

    @staticmethod
    def _should_snap_initial_left_edge_to_zero(
        first_time: float,
        step: float,
    ) -> bool:
        if not np.isfinite(first_time) or not np.isfinite(step) or step <= 0.0:
            return False
        return 0.0 <= first_time <= 0.5 and first_time <= 2.0 * step

    @staticmethod
    def _validated_levels(
        levels_db: tuple[float, float] | None,
    ) -> tuple[float, float] | None:
        if levels_db is None:
            return None
        try:
            lower = float(levels_db[0])
            upper = float(levels_db[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError("levels_db must contain lower and upper values") from None
        if not np.isfinite(lower) or not np.isfinite(upper) or lower >= upper:
            raise ValueError("levels_db lower must be finite and less than upper")
        return (lower, upper)

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
        hide_left_axis_label: bool = False,
        export_schema: LineChartExportSchema | None = None,
    ):
        super().__init__(parent, hide_left_axis_label=hide_left_axis_label)
        self._series_payload: dict | None = None
        self._y_axis_mode = "auto"
        self._export_schema = export_schema or LineChartExportSchema.generic()
        self._export_metadata: dict[str, str] = {}
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
        self.chart_interactions = ChartInteractionController(self, parent=self)

    def set_series(
        self,
        x_values: Iterable[float],
        y_values: Iterable[float],
        *,
        lower_limit: float | None = None,
        upper_limit: float | None = None,
        y_axis_mode: str = "auto",
        export_metadata: dict | None = None,
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
        self._export_metadata = {
            str(key): "" if value is None else str(value)
            for key, value in (export_metadata or {}).items()
        }

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
        self.chart_interactions.refresh_full_view()

    def clear_data(self) -> None:
        self._series_payload = None
        self._export_metadata = {}
        self._curve.setData([], [])
        self._lower_limit_line.hide()
        self._upper_limit_line.hide()
        self._apply_x_axis_policy(payload_values=None)
        if self._y_axis_mode == "data_focus":
            self._apply_data_focus_y_axis_policy(0.0, 1.0)
        else:
            self._apply_auto_y_axis_policy()
        self.chart_interactions.refresh_full_view()

    def has_series(self) -> bool:
        return self._series_payload is not None

    def series_payload(self) -> dict | None:
        return deepcopy(self._series_payload)

    def export_csv_table(self, *, scope: CsvExportScope = "all") -> CsvTable:
        schema = self._export_schema
        headers = [
            schema.x_header,
            schema.y_header,
            *schema.metadata_headers,
            schema.lower_header,
            schema.upper_header,
        ]
        if self._series_payload is None:
            table = CsvTable(headers=headers, rows=[], x_values=[])
            return self.chart_interactions.filter_csv_table(table, scope)

        lower = self._series_payload["lower_limit"]
        upper = self._series_payload["upper_limit"]
        x_values = list(self._series_payload["x_values"])
        rows = [
            [
                _format_csv_number(x_value),
                _format_csv_number(y_value),
                *[
                    self._export_metadata.get(metadata_header, "")
                    for metadata_header in schema.metadata_headers
                ],
                _format_csv_number(lower),
                _format_csv_number(upper),
            ]
            for x_value, y_value in zip(
                x_values,
                self._series_payload["y_values"],
                strict=False,
            )
        ]
        table = CsvTable(headers=headers, rows=rows, x_values=x_values)
        return self.chart_interactions.filter_csv_table(table, scope)

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
        hide_left_axis_label: bool = False,
    ):
        super().__init__(parent, hide_left_axis_label=hide_left_axis_label)
        self._spectrum_payload: dict | None = None
        self._value_semantics = "acoustic"
        self._curve.setPen(pg.mkPen(color="#2563eb", width=_FFT_SPECTRUM_LINE_WIDTH))
        self._golden_overlay_curve_item = self.plot_item.plot(
            pen=pg.mkPen(
                color="#64748b",
                width=_FFT_SPECTRUM_LINE_WIDTH,
                style=pg.QtCore.Qt.PenStyle.DashLine,
            ),
        )
        self._failed_spectrum_curve_item = self.plot_item.plot(
            pen=pg.mkPen(color="#dc2626", width=_FFT_SPECTRUM_LINE_WIDTH),
        )
        self._lower_envelope_curve_item = self.plot_item.plot(
            pen=pg.mkPen(color="#16a34a", width=2),
        )
        self._upper_envelope_curve_item = self.plot_item.plot(
            pen=pg.mkPen(color="#dc2626", width=2),
        )
        self._failed_marker_item = pg.PlotDataItem(
            pen=None,
            symbol="x",
            symbolSize=10,
            symbolBrush="#dc2626",
            symbolPen=pg.mkPen(color="#dc2626", width=2),
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
        self.plot_item.addItem(self._failed_marker_item)
        self.plot_item.addItem(self._band_range_item)
        self.plot_item.addItem(self._band_marker_item)
        self._zero_reference_line.hide()
        self.plot_item.setLabel("bottom", "频率", units="Hz")
        self.plot_item.setLabel("left", "幅值", units="dB")
        if self._hide_left_axis_label:
            self.apply_left_axis_label_hidden_style()

    def set_vibration_semantics(self, enabled: bool) -> None:
        self._value_semantics = "vibration" if enabled else "acoustic"
        if enabled:
            self.plot_item.setLabel("left", "校准幅值")
        else:
            self.plot_item.setLabel("left", "幅值", units="dB")
        if self._hide_left_axis_label:
            self.apply_left_axis_label_hidden_style()

    def value_semantics(self) -> str:
        return getattr(self, "_value_semantics", "acoustic")

    def set_spectrum(
        self,
        *,
        frequencies_hz: Iterable[float],
        levels_db: Iterable[float],
        limit_bands: Iterable[dict] | None = None,
        limit_envelope: dict | None = None,
        golden_overlay_levels_db: Iterable[float] | None = None,
        show_zero_reference: bool = False,
        x_axis_scale: str = "linear",
    ) -> None:
        normalized_frequencies = _normalize_vector(
            frequencies_hz, name="frequencies_hz"
        )
        normalized_levels = _normalize_vector(levels_db, name="levels_db")
        if len(normalized_frequencies) != len(normalized_levels):
            raise ValueError("frequencies_hz and levels_db must be the same length")

        normalized_limit_bands = _normalize_limit_bands(limit_bands)
        normalized_golden_overlay = _normalize_optional_overlay_levels(
            golden_overlay_levels_db,
            expected_length=len(normalized_frequencies),
            name="golden_overlay_levels_db",
        )
        scale = _normalize_fft_x_axis_scale(x_axis_scale)
        self.plot_item.setLogMode(x=(scale == "log"), y=False)

        freq_array = np.asarray(normalized_frequencies, dtype=np.float64)
        level_array = np.asarray(normalized_levels, dtype=np.float64)
        display_mask = (
            np.isfinite(freq_array)
            & np.isfinite(level_array)
            & (freq_array > 0.0)
        )
        display_frequencies = freq_array[display_mask].tolist()
        display_levels = level_array[display_mask].tolist()
        display_golden_overlay = (
            np.asarray(normalized_golden_overlay, dtype=np.float64)[
                display_mask
            ].tolist()
            if normalized_golden_overlay is not None
            else None
        )
        display_envelope = self._display_limit_envelope(limit_envelope)

        self.set_series(display_frequencies, display_levels)
        self._spectrum_payload = {
            "frequencies_hz": normalized_frequencies,
            "levels_db": normalized_levels,
            "display_frequencies_hz": display_frequencies,
            "display_levels_db": display_levels,
            "limit_bands": normalized_limit_bands,
            "limit_envelope": deepcopy(limit_envelope)
            if isinstance(limit_envelope, dict)
            else None,
            "display_limit_envelope": display_envelope,
            "golden_overlay_levels_db": normalized_golden_overlay,
            "display_golden_overlay_levels_db": display_golden_overlay,
            "show_zero_reference": bool(show_zero_reference),
            "x_axis_scale": scale,
        }

        self._render_golden_overlay(
            x_values=display_frequencies,
            levels_db=display_golden_overlay,
        )
        self._update_zero_reference_line(bool(show_zero_reference))
        self._render_limit_envelope(
            display_envelope,
            x_values=display_frequencies,
            levels_db=display_levels,
        )
        if limit_envelope is None:
            self._failed_spectrum_curve_item.setData([], [])
            self._render_band_markers(
                x_values=[band["freq_hz"] for band in normalized_limit_bands],
                limit_bands=normalized_limit_bands,
                x_axis_scale=scale,
            )
        else:
            _clear_error_bar_item(self._band_range_item)
            self._band_marker_item.setData([])
        self._apply_fft_x_axis_policy(
            display_frequencies=display_frequencies,
            x_axis_scale=scale,
        )
        self.plot_item.vb.updateAutoRange()
        self.chart_interactions.refresh_full_view()

    def clear_data(self) -> None:
        self.plot_item.setLogMode(x=False, y=False)
        _clear_error_bar_item(self._band_range_item)
        super().clear_data()
        self._spectrum_payload = None
        self._golden_overlay_curve_item.setData([], [])
        self._failed_spectrum_curve_item.setData([], [])
        self._lower_envelope_curve_item.setData([], [])
        self._upper_envelope_curve_item.setData([], [])
        self._failed_marker_item.setData([], [])
        self._band_marker_item.setData([])
        self._zero_reference_line.hide()
        self.chart_interactions.refresh_full_view()

    def spectrum_payload(self) -> dict | None:
        return deepcopy(self._spectrum_payload)

    def is_log_x_display(self) -> bool:
        payload = self._spectrum_payload or {}
        return payload.get("x_axis_scale") == "log"

    def domain_x_range_from_display_range(
        self,
        display_range: tuple[float, float],
    ) -> tuple[float, float]:
        lower, upper = sorted((float(display_range[0]), float(display_range[1])))
        if self.is_log_x_display():
            return (10.0**lower, 10.0**upper)
        return (lower, upper)

    def display_x_range_from_domain_range(
        self,
        domain_range: tuple[float, float],
    ) -> tuple[float, float]:
        lower, upper = sorted((float(domain_range[0]), float(domain_range[1])))
        if self.is_log_x_display():
            lower = max(lower, 1e-300)
            upper = max(upper, 1e-300)
            return (math.log10(lower), math.log10(upper))
        return (lower, upper)

    def export_csv_table(self, *, scope: CsvExportScope = "all") -> CsvTable:
        if self.value_semantics() == "vibration":
            headers = [
                "frequency_hz",
                "calibrated_amplitude",
                "golden_calibrated_amplitude",
                "lower_limit",
                "upper_limit",
                "failed",
            ]
        else:
            headers = [
                "frequency_hz",
                "level_db",
                "golden_level_db",
                "lower_limit_db",
                "upper_limit_db",
                "failed",
            ]
        payload = self._spectrum_payload
        if payload is None:
            table = CsvTable(headers=headers, rows=[], x_values=[])
            return self.chart_interactions.filter_csv_table(table, scope)

        frequencies = payload["frequencies_hz"]
        levels = payload["levels_db"]
        golden_overlay = payload.get("golden_overlay_levels_db")
        row_entries: list[tuple[int, float, float]] = []
        for index, (freq_hz, level_db) in enumerate(
            zip(frequencies, levels, strict=False)
        ):
            try:
                frequency = float(freq_hz)
                level = float(level_db)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(frequency) or not np.isfinite(level):
                continue
            row_entries.append((index, frequency, level))

        x_values = [frequency for _index, frequency, _level in row_entries]
        envelope_points = self._export_envelope_points(payload)
        discrete_limits = (
            {}
            if payload.get("limit_envelope") is not None
            else self._export_discrete_limits_by_row(x_values, payload)
        )

        rows: list[list[str]] = []
        for row_index, (source_index, frequency, level) in enumerate(row_entries):
            lower_limit = ""
            upper_limit = ""
            failed = ""
            envelope_limits = self._interpolated_envelope_limits(
                frequency,
                envelope_points,
                log_x=self.is_log_x_display(),
            )
            if envelope_limits is not None:
                lower, upper = envelope_limits
                lower_limit = _format_csv_number(lower)
                upper_limit = _format_csv_number(upper)
                failed = "1" if level < lower or level > upper else "0"
            elif row_index in discrete_limits:
                lower, upper = discrete_limits[row_index]
                lower_limit = _format_csv_number(lower)
                upper_limit = _format_csv_number(upper)
                failed = "1" if level < lower or level > upper else "0"

            rows.append(
                [
                    _format_csv_number(frequency),
                    _format_csv_number(level),
                    self._golden_overlay_export_cell(
                        golden_overlay,
                        source_index,
                    ),
                    lower_limit,
                    upper_limit,
                    failed,
                ]
            )

        table = CsvTable(headers=headers, rows=rows, x_values=x_values)
        return self.chart_interactions.filter_csv_table(table, scope)

    @staticmethod
    def _golden_overlay_export_cell(
        golden_overlay: list[float] | None,
        source_index: int,
    ) -> str:
        if golden_overlay is None or source_index >= len(golden_overlay):
            return ""
        return _format_csv_number(golden_overlay[source_index])

    @classmethod
    def _export_envelope_points(
        cls,
        payload: dict,
    ) -> list[tuple[float, float, float]]:
        display_envelope = payload.get("display_limit_envelope") or {}
        points = (
            display_envelope.get("line_points")
            or display_envelope.get("control_points")
            or []
        )
        grouped: dict[float, tuple[float, float]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            freq_hz = cls._positive_finite(point.get("freq_hz"))
            lower_db = cls._finite_float(point.get("lower_db"))
            upper_db = cls._finite_float(point.get("upper_db"))
            if freq_hz is None or lower_db is None or upper_db is None:
                continue
            if freq_hz in grouped:
                existing_lower, existing_upper = grouped[freq_hz]
                grouped[freq_hz] = (
                    max(existing_lower, lower_db),
                    min(existing_upper, upper_db),
                )
            else:
                grouped[freq_hz] = (lower_db, upper_db)

        return [
            (freq_hz, lower_db, upper_db)
            for freq_hz, (lower_db, upper_db) in sorted(grouped.items())
            if lower_db < upper_db
        ]

    @staticmethod
    def _interpolated_envelope_limits(
        frequency: float,
        envelope_points: list[tuple[float, float, float]],
        *,
        log_x: bool = False,
    ) -> tuple[float, float] | None:
        if len(envelope_points) < 2 or frequency <= 0.0:
            return None
        freq_values = np.asarray(
            [point[0] for point in envelope_points],
            dtype=np.float64,
        )
        interpolation_x = float(frequency)
        if log_x:
            freq_values = np.log10(freq_values)
            interpolation_x = math.log10(interpolation_x)
        if (
            interpolation_x < float(freq_values[0])
            or interpolation_x > float(freq_values[-1])
        ):
            return None
        lower_values = np.asarray(
            [point[1] for point in envelope_points],
            dtype=np.float64,
        )
        upper_values = np.asarray(
            [point[2] for point in envelope_points],
            dtype=np.float64,
        )
        return (
            float(np.interp(interpolation_x, freq_values, lower_values)),
            float(np.interp(interpolation_x, freq_values, upper_values)),
        )

    @classmethod
    def _export_discrete_limits_by_row(
        cls,
        x_values: list[float],
        payload: dict,
    ) -> dict[int, tuple[float, float]]:
        if not x_values:
            return {}

        frequencies = np.asarray(x_values, dtype=np.float64)
        assigned: dict[int, tuple[float, float]] = {}
        for band in payload.get("limit_bands") or []:
            if not isinstance(band, dict):
                continue
            band_freq = cls._finite_float(band.get("freq_hz"))
            lower_db = cls._finite_float(band.get("lower_db"))
            upper_db = cls._finite_float(band.get("upper_db"))
            if band_freq is None or lower_db is None or upper_db is None:
                continue
            row_index = int(np.abs(frequencies - band_freq).argmin())
            if row_index in assigned:
                existing_lower, existing_upper = assigned[row_index]
                assigned[row_index] = (
                    max(existing_lower, lower_db),
                    min(existing_upper, upper_db),
                )
            else:
                assigned[row_index] = (lower_db, upper_db)
        return assigned

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

    def _render_limit_envelope(
        self,
        display_envelope: dict,
        *,
        x_values: list[float],
        levels_db: list[float],
    ) -> None:
        control_points = display_envelope["control_points"]
        line_points = display_envelope.get("line_points") or control_points
        self._lower_envelope_curve_item.setData(
            [point["freq_hz"] for point in line_points],
            [point["lower_db"] for point in line_points],
        )
        self._upper_envelope_curve_item.setData(
            [point["freq_hz"] for point in line_points],
            [point["upper_db"] for point in line_points],
        )
        self._failed_marker_item.setData([], [])
        failed_x, failed_y = self._failed_spectrum_curve_data(
            x_values=x_values,
            levels_db=levels_db,
            envelope_points=line_points,
        )
        self._failed_spectrum_curve_item.setData(failed_x, failed_y)

    @staticmethod
    def _failed_spectrum_curve_data(
        *,
        x_values: list[float],
        levels_db: list[float],
        envelope_points: list[dict],
    ) -> tuple[list[float], list[float]]:
        if len(x_values) != len(levels_db) or len(envelope_points) < 2:
            return [], []

        grouped: dict[float, tuple[float, float]] = {}
        for point in envelope_points:
            try:
                freq = float(point["freq_hz"])
                lower = float(point["lower_db"])
                upper = float(point["upper_db"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not np.isfinite(freq)
                or not np.isfinite(lower)
                or not np.isfinite(upper)
                or freq <= 0.0
                or lower >= upper
            ):
                continue
            if freq in grouped:
                existing_lower, existing_upper = grouped[freq]
                grouped[freq] = (max(existing_lower, lower), min(existing_upper, upper))
            else:
                grouped[freq] = (lower, upper)

        if len(grouped) < 2:
            return [], []

        ordered = sorted(grouped.items(), key=lambda item: item[0])
        envelope_freqs = np.asarray([freq for freq, _limits in ordered], dtype=np.float64)
        lower_limits = np.asarray(
            [limits[0] for _freq, limits in ordered],
            dtype=np.float64,
        )
        upper_limits = np.asarray(
            [limits[1] for _freq, limits in ordered],
            dtype=np.float64,
        )
        valid_envelope = lower_limits < upper_limits
        if not np.all(valid_envelope):
            envelope_freqs = envelope_freqs[valid_envelope]
            lower_limits = lower_limits[valid_envelope]
            upper_limits = upper_limits[valid_envelope]
        if envelope_freqs.size < 2:
            return [], []

        try:
            x_array = np.asarray(x_values, dtype=np.float64)
            y_array = np.asarray(levels_db, dtype=np.float64)
        except (TypeError, ValueError):
            return [], []
        if x_array.ndim != 1 or y_array.ndim != 1 or x_array.size != y_array.size:
            return [], []

        finite = np.isfinite(x_array) & np.isfinite(y_array) & (x_array > 0.0)
        within_envelope = (
            finite
            & (x_array >= float(envelope_freqs[0]))
            & (x_array <= float(envelope_freqs[-1]))
        )
        if not np.any(within_envelope):
            return [], []

        interpolated_lower = np.interp(x_array, envelope_freqs, lower_limits)
        interpolated_upper = np.interp(x_array, envelope_freqs, upper_limits)
        failed = within_envelope & (
            (y_array < interpolated_lower) | (y_array > interpolated_upper)
        )
        if not np.any(failed):
            return [], []

        failed_y = np.full(y_array.shape, np.nan, dtype=np.float64)
        failed_y[failed] = y_array[failed]
        return x_array.tolist(), failed_y.tolist()

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
        x_axis_scale: str = "linear",
    ) -> None:
        if not limit_bands:
            _clear_error_bar_item(self._band_range_item)
            self._band_marker_item.setData([])
            return

        display_entries: list[tuple[float, dict]] = []
        for x_value, band in zip(x_values, limit_bands, strict=False):
            try:
                raw_x = float(x_value)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(raw_x) or raw_x <= 0.0:
                continue
            display_x = float(np.log10(raw_x)) if x_axis_scale == "log" else raw_x
            display_entries.append((display_x, band))

        if not display_entries:
            _clear_error_bar_item(self._band_range_item)
            self._band_marker_item.setData([])
            return

        rendered_x_values = [x_value for x_value, _ in display_entries]
        rendered_limit_bands = [band for _, band in display_entries]

        centers = np.array(
            [
                (band["lower_db"] + band["upper_db"]) / 2.0
                for band in rendered_limit_bands
            ],
            dtype=float,
        )
        spans = np.array(
            [
                (band["upper_db"] - band["lower_db"]) / 2.0
                for band in rendered_limit_bands
            ],
            dtype=float,
        )
        x_array = np.asarray(rendered_x_values, dtype=float)
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
                    "pos": (x_value, band["actual_db"]),
                    "brush": "#16a34a" if band["passed"] else "#dc2626",
                }
                for x_value, band in zip(
                    rendered_x_values, rendered_limit_bands, strict=False
                )
            ]
        )

    def _apply_fft_x_axis_policy(
        self,
        *,
        display_frequencies: list[float],
        x_axis_scale: str,
    ) -> None:
        if x_axis_scale != "log":
            self._apply_x_axis_policy(payload_values=display_frequencies)
            return

        positive = np.asarray(display_frequencies, dtype=np.float64)
        positive = positive[np.isfinite(positive) & (positive > 0.0)]
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        if positive.size == 0:
            view_box.setXRange(0.0, 1.0, padding=0.0)
            return

        lower = float(np.log10(positive.min()))
        upper = float(np.log10(positive.max()))
        if not np.isfinite(lower) or not np.isfinite(upper):
            view_box.setXRange(0.0, 1.0, padding=0.0)
            return
        if np.isclose(lower, upper):
            padding = 0.5
            lower -= padding
            upper += padding
        view_box.setXRange(lower, upper, padding=0.0)

    @classmethod
    def _display_limit_envelope(cls, limit_envelope: dict | None) -> dict:
        if not isinstance(limit_envelope, dict):
            return {"control_points": [], "failed_points": []}
        control_points = []
        for point in limit_envelope.get("control_points", []):
            normalized = cls._normalize_envelope_control_point(point)
            if normalized is not None:
                control_points.append(normalized)
        line_points = []
        for point in limit_envelope.get("line_points", []):
            normalized = cls._normalize_envelope_control_point(point)
            if normalized is not None:
                line_points.append(normalized)
        failed_points = []
        for point in limit_envelope.get("failed_points", []):
            normalized = cls._normalize_envelope_failed_point(point)
            if normalized is not None:
                failed_points.append(normalized)
        display_envelope = {
            "control_points": control_points,
            "failed_points": failed_points,
        }
        if isinstance(limit_envelope.get("line_points"), list):
            display_envelope["line_points"] = line_points
        return display_envelope

    @staticmethod
    def _positive_finite(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(number) or number <= 0.0:
            return None
        return number

    @staticmethod
    def _finite_float(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if np.isfinite(number) else None

    @classmethod
    def _normalize_envelope_control_point(cls, point) -> dict | None:
        if not isinstance(point, dict):
            return None
        freq_hz = cls._positive_finite(point.get("freq_hz"))
        lower_db = cls._finite_float(point.get("lower_db"))
        upper_db = cls._finite_float(point.get("upper_db"))
        if freq_hz is None or lower_db is None or upper_db is None:
            return None
        return {"freq_hz": freq_hz, "lower_db": lower_db, "upper_db": upper_db}

    @classmethod
    def _normalize_envelope_failed_point(cls, point) -> dict | None:
        if not isinstance(point, dict):
            return None
        freq_hz = cls._positive_finite(point.get("freq_hz"))
        actual_db = cls._finite_float(point.get("actual_db"))
        lower_db = cls._finite_float(point.get("lower_db"))
        upper_db = cls._finite_float(point.get("upper_db"))
        if (
            freq_hz is None
            or actual_db is None
            or lower_db is None
            or upper_db is None
        ):
            return None
        return {
            "freq_hz": freq_hz,
            "actual_db": actual_db,
            "lower_db": lower_db,
            "upper_db": upper_db,
        }


_OCTAVE_BAR_WIDTH = 0.6
_OCTAVE_BAR_EDGE_MARGIN = 0.2


class OctaveBarChartWidget(_BasePlotChart):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hide_left_axis_label: bool = False,
    ):
        super().__init__(parent, hide_left_axis_label=hide_left_axis_label)
        self._band_payload: dict | None = None
        self._value_semantics = "acoustic"
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
        self.plot_item.setLabel("bottom", "频带", units="Hz")
        self.plot_item.setLabel("left", "幅值", units="dB")
        if self._hide_left_axis_label:
            self.apply_left_axis_label_hidden_style()
        self.chart_interactions = ChartInteractionController(self, parent=self)

    def set_vibration_semantics(self, enabled: bool) -> None:
        self._value_semantics = "vibration" if enabled else "acoustic"
        if enabled:
            self.plot_item.setLabel("left", "校准幅值")
        else:
            self.plot_item.setLabel("left", "幅值", units="dB")
        if self._hide_left_axis_label:
            self.apply_left_axis_label_hidden_style()

    def value_semantics(self) -> str:
        return getattr(self, "_value_semantics", "acoustic")

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
        self._apply_bar_x_axis_policy(x_values=x_values)
        self.plot_item.vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.plot_item.vb.updateAutoRange()
        self.chart_interactions.refresh_full_view()

    def clear_data(self) -> None:
        self._band_payload = None
        _clear_error_bar_item(self._band_range_item)
        self._golden_overlay_item.setData([], [])
        self._band_marker_item.setData([])
        self._replace_bar_item(x=np.array([], dtype=float), heights=np.array([], dtype=float))
        self.plot_item.getAxis("bottom").setTicks(None)
        self._apply_x_axis_policy(payload_values=None)
        self._zero_reference_line.hide()
        self.chart_interactions.refresh_full_view()

    def has_bars(self) -> bool:
        return self._band_payload is not None

    def band_payload(self) -> dict | None:
        return deepcopy(self._band_payload)

    def export_csv_table(self, *, scope: CsvExportScope = "all") -> CsvTable:
        if self.value_semantics() == "vibration":
            headers = [
                "center_frequency_hz",
                "calibrated_amplitude",
                "golden_calibrated_amplitude",
                "lower_limit",
                "upper_limit",
                "passed",
            ]
        else:
            headers = [
                "center_frequency_hz",
                "level_db",
                "golden_level_db",
                "lower_limit_db",
                "upper_limit_db",
                "passed",
            ]
        payload = self._band_payload
        if payload is None:
            table = CsvTable(headers=headers, rows=[], x_values=[])
            return self.chart_interactions.filter_csv_table(table, scope)

        center_freqs = payload["center_freqs_hz"]
        levels = payload["levels_db"]
        golden_overlay = payload.get("golden_overlay_levels_db")
        limits_by_row = self._export_octave_limits_by_row(payload)
        x_values = [float(index) for index in range(len(center_freqs))]
        rows: list[list[str]] = []
        for index, (center_freq, level) in enumerate(
            zip(center_freqs, levels, strict=False)
        ):
            limit = limits_by_row.get(index)
            if limit is None:
                lower_limit = ""
                upper_limit = ""
                passed = ""
            else:
                lower, upper, is_passed = limit
                lower_limit = _format_csv_number(lower)
                upper_limit = _format_csv_number(upper)
                passed = "1" if is_passed else "0"

            rows.append(
                [
                    _format_csv_number(center_freq),
                    _format_csv_number(level),
                    self._golden_overlay_export_cell(golden_overlay, index),
                    lower_limit,
                    upper_limit,
                    passed,
                ]
            )

        table = CsvTable(headers=headers, rows=rows, x_values=x_values)
        return self.chart_interactions.filter_csv_table(table, scope)

    @staticmethod
    def _golden_overlay_export_cell(
        golden_overlay: list[float] | None,
        source_index: int,
    ) -> str:
        if golden_overlay is None or source_index >= len(golden_overlay):
            return ""
        return _format_csv_number(golden_overlay[source_index])

    @staticmethod
    def _export_octave_limits_by_row(
        payload: dict,
    ) -> dict[int, tuple[float, float, bool]]:
        center_freqs = payload["center_freqs_hz"]
        limit_bands = payload.get("limit_bands") or []
        if not center_freqs or not limit_bands:
            return {}

        positions = _nearest_positions(
            center_freqs,
            [band["freq_hz"] for band in limit_bands],
        )
        limits_by_row: dict[int, tuple[float, float, bool]] = {}
        for position, band in zip(positions, limit_bands, strict=False):
            if not np.isfinite(position):
                continue
            limits_by_row[int(position)] = (
                float(band["lower_db"]),
                float(band["upper_db"]),
                bool(band["passed"]),
            )
        return limits_by_row

    def _apply_bar_x_axis_policy(self, *, x_values: np.ndarray) -> None:
        if x_values.size == 0:
            self._apply_x_axis_policy(payload_values=None)
            return

        edge_padding = (_OCTAVE_BAR_WIDTH / 2.0) + _OCTAVE_BAR_EDGE_MARGIN
        view_box = self.plot_item.vb
        view_box.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        view_box.setXRange(
            float(x_values[0]) - edge_padding,
            float(x_values[-1]) + edge_padding,
            padding=0.0,
        )

    def _replace_bar_item(self, *, x: np.ndarray, heights: np.ndarray) -> None:
        if self._bar_item is not None:
            self.plot_item.removeItem(self._bar_item)
        self._bar_item = pg.BarGraphItem(
            x=x,
            y0=0.0,
            height=heights,
            width=_OCTAVE_BAR_WIDTH,
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
    "LineChartExportSchema",
    "LineChartWidget",
    "OctaveBarChartWidget",
    "StftImageChartWidget",
]
