from __future__ import annotations

from copy import deepcopy
from typing import Iterable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QVBoxLayout, QWidget

pg.setConfigOptions(antialias=True, background="w", foreground="#334155")


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


def _nearest_positions(base_values: list[float], band_freqs: Iterable[float]) -> list[float]:
    if not base_values:
        return []

    base = np.asarray(base_values, dtype=float)
    positions: list[float] = []
    for freq_hz in band_freqs:
        index = int(np.abs(base - float(freq_hz)).argmin())
        positions.append(float(index))
    return positions


class _BasePlotChart(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot_widget = pg.PlotWidget(parent=self)
        self.plot_widget.setMenuEnabled(False)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.hideButtons()
        layout.addWidget(self.plot_widget)

        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.showGrid(x=True, y=True, alpha=0.18)


class StftImageChartWidget(_BasePlotChart):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._image_payload: dict | None = None
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

    def set_image(
        self,
        *,
        times_sec: Iterable[float],
        frequencies_hz: Iterable[float],
        magnitude_db,
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

        self._image_item.setImage(normalized_image, autoLevels=True)
        self._image_item.setRect(self._image_rect())

    def clear_data(self) -> None:
        self._image_payload = None
        self._image_item.clear()

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


class LineChartWidget(_BasePlotChart):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._series_payload: dict | None = None
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
    ) -> None:
        normalized_x = _normalize_vector(x_values, name="x_values")
        normalized_y = _normalize_vector(y_values, name="y_values")
        if len(normalized_x) != len(normalized_y):
            raise ValueError("x_values and y_values must be the same length")

        self._series_payload = {
            "x_values": normalized_x,
            "y_values": normalized_y,
            "lower_limit": None if lower_limit is None else float(lower_limit),
            "upper_limit": None if upper_limit is None else float(upper_limit),
        }

        self._curve.setData(normalized_x, normalized_y)
        self._update_limit_line(self._lower_limit_line, self._series_payload["lower_limit"])
        self._update_limit_line(self._upper_limit_line, self._series_payload["upper_limit"])

    def clear_data(self) -> None:
        self._series_payload = None
        self._curve.setData([], [])
        self._lower_limit_line.hide()
        self._upper_limit_line.hide()

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


class FftLineChartWidget(LineChartWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._spectrum_payload: dict | None = None
        self._band_range_item = pg.ErrorBarItem()
        self._band_marker_item = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None))
        self.plot_item.addItem(self._band_range_item)
        self.plot_item.addItem(self._band_marker_item)
        self.plot_item.setLabel("bottom", "Frequency", units="Hz")
        self.plot_item.setLabel("left", "Level", units="dB")

    def set_spectrum(
        self,
        *,
        frequencies_hz: Iterable[float],
        levels_db: Iterable[float],
        limit_bands: Iterable[dict] | None = None,
    ) -> None:
        normalized_frequencies = _normalize_vector(
            frequencies_hz, name="frequencies_hz"
        )
        normalized_levels = _normalize_vector(levels_db, name="levels_db")
        normalized_limit_bands = _normalize_limit_bands(limit_bands)

        self.set_series(normalized_frequencies, normalized_levels)
        self._spectrum_payload = {
            "frequencies_hz": normalized_frequencies,
            "levels_db": normalized_levels,
            "limit_bands": normalized_limit_bands,
        }

        self._render_band_markers(
            x_values=[band["freq_hz"] for band in normalized_limit_bands],
            limit_bands=normalized_limit_bands,
        )

    def clear_data(self) -> None:
        super().clear_data()
        self._spectrum_payload = None
        self._band_range_item.setData(x=[], y=[], top=[], bottom=[])
        self._band_marker_item.setData([])

    def spectrum_payload(self) -> dict | None:
        return deepcopy(self._spectrum_payload)

    def _render_band_markers(
        self,
        *,
        x_values: list[float],
        limit_bands: list[dict],
    ) -> None:
        if not limit_bands:
            self._band_range_item.setData(x=[], y=[], top=[], bottom=[])
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
            beam=np.zeros(len(limit_bands), dtype=float),
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
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._band_payload: dict | None = None
        self._bar_item: pg.BarGraphItem | None = None
        self._band_range_item = pg.ErrorBarItem()
        self._band_marker_item = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None))
        self.plot_item.addItem(self._band_range_item)
        self.plot_item.addItem(self._band_marker_item)
        self.plot_item.setLabel("bottom", "Band", units="Hz")
        self.plot_item.setLabel("left", "Level", units="dB")

    def set_bands(
        self,
        *,
        center_freqs_hz: Iterable[float],
        levels_db: Iterable[float],
        limit_bands: Iterable[dict] | None = None,
    ) -> None:
        normalized_frequencies = _normalize_vector(
            center_freqs_hz, name="center_freqs_hz"
        )
        normalized_levels = _normalize_vector(levels_db, name="levels_db")
        if len(normalized_frequencies) != len(normalized_levels):
            raise ValueError("center_freqs_hz and levels_db must be the same length")

        normalized_limit_bands = _normalize_limit_bands(limit_bands)
        self._band_payload = {
            "center_freqs_hz": normalized_frequencies,
            "levels_db": normalized_levels,
            "limit_bands": normalized_limit_bands,
        }

        x_values = np.arange(len(normalized_frequencies), dtype=float)
        self._replace_bar_item(x=x_values, heights=np.asarray(normalized_levels, dtype=float))
        self.plot_item.getAxis("bottom").setTicks(
            [[(float(index), self._format_frequency_label(freq_hz)) for index, freq_hz in enumerate(normalized_frequencies)]]
        )
        self._render_band_markers(
            x_values=_nearest_positions(
                normalized_frequencies,
                [band["freq_hz"] for band in normalized_limit_bands],
            ),
            limit_bands=normalized_limit_bands,
        )

    def clear_data(self) -> None:
        self._band_payload = None
        self._replace_bar_item(x=np.array([], dtype=float), heights=np.array([], dtype=float))
        self._band_range_item.setData(x=[], y=[], top=[], bottom=[])
        self._band_marker_item.setData([])
        self.plot_item.getAxis("bottom").setTicks([])

    def has_bars(self) -> bool:
        return self._band_payload is not None

    def band_payload(self) -> dict | None:
        return deepcopy(self._band_payload)

    def _replace_bar_item(self, *, x: np.ndarray, heights: np.ndarray) -> None:
        if self._bar_item is not None:
            self.plot_item.removeItem(self._bar_item)
        self._bar_item = pg.BarGraphItem(
            x=x,
            height=heights,
            width=0.6,
            brush="#f59e0b",
            pen=pg.mkPen(color="#d97706", width=1),
        )
        self.plot_item.addItem(self._bar_item)

    def _render_band_markers(
        self,
        *,
        x_values: list[float],
        limit_bands: list[dict],
    ) -> None:
        if not limit_bands:
            self._band_range_item.setData(x=[], y=[], top=[], bottom=[])
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
            beam=np.full(len(limit_bands), 0.3, dtype=float),
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
