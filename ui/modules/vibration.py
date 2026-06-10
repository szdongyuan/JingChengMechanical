from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from copy import deepcopy

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.analysis_charts import LineChartExportSchema, LineChartWidget


def _finite_float(value) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _numeric_list(values: Iterable | None) -> list[float]:
    if values is None:
        return []
    normalized: list[float] = []
    try:
        iterator = iter(values)
    except TypeError:
        return []
    for value in iterator:
        numeric = _finite_float(value)
        if numeric is None:
            return []
        normalized.append(numeric)
    return normalized


def _format_metric(value, *, digits: int = 2) -> str:
    numeric = _finite_float(value)
    if numeric is None:
        return "--"
    return f"{numeric:.{digits}f}"


def _format_percent(value) -> str:
    numeric = _finite_float(value)
    if numeric is None:
        return "--"
    return f"{round(_clamp_ratio(numeric) * 100):d}%"


def _data_focus_range(values: Iterable[float]) -> tuple[float, float] | None:
    normalized = _numeric_list(values)
    if not normalized:
        return None

    y_min = min(normalized)
    y_max = max(normalized)
    if math.isclose(y_min, y_max):
        padding = max(0.1 * abs(y_min), 0.5)
    else:
        padding = max(0.1 * (y_max - y_min), 0.1)
    return (y_min - padding, y_max + padding)


def _clamp_ratio(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(1.0, numeric))


def _relative_level(value: float | None, reference: float | None) -> float:
    if value is None or reference is None or reference <= 0:
        return 0.0
    return _clamp_ratio(value / reference)


def _vibration_indicator_snapshot(payload: dict) -> dict[str, float]:
    rms = _finite_float(payload.get("overall_rms")) or 0.0
    peak = _finite_float(payload.get("overall_peak")) or 0.0
    crest = _finite_float(payload.get("overall_crest_factor")) or 0.0
    dominant_hz = _finite_float(payload.get("dominant_frequency_hz")) or 0.0
    low_share = _finite_float(payload.get("low_frequency_energy_share"))
    rms_values = _numeric_list(payload.get("rms_values"))
    peak_values = _numeric_list(payload.get("peak_values"))

    rolling_rms = (
        sum(abs(value) for value in rms_values) / len(rms_values)
        if rms_values
        else abs(rms)
    )
    energy_reference = max(
        abs(peak),
        rolling_rms,
        abs(rms),
        max((abs(value) for value in peak_values), default=0.0),
        1.0,
    )
    energy = _relative_level(max(abs(rms), rolling_rms), energy_reference)

    peak_envelope = max((abs(value) for value in peak_values), default=abs(peak))
    envelope_ratio = peak_envelope / max(rolling_rms, abs(rms), 1e-9)
    impulse = (
        _clamp_ratio(max(crest - 1.0, envelope_ratio - 1.0) / 5.0)
        if (crest > 0 or peak_envelope > 0)
        else 0.0
    )

    if len(rms_values) >= 2:
        mean_rms = sum(abs(value) for value in rms_values) / len(rms_values)
        rms_variation = (
            statistics.pstdev(rms_values) / mean_rms
            if mean_rms > 0
            else 0.0
        )
    else:
        rms_variation = 0.5 if rms > 0 else 1.0

    if peak_values and rms_values and len(peak_values) == len(rms_values):
        ratios = [
            abs(peak_value) / max(abs(rms_value), 1e-9)
            for peak_value, rms_value in zip(peak_values, rms_values)
        ]
        ratio_variation = statistics.pstdev(ratios) / max(
            sum(ratios) / len(ratios),
            1e-9,
        )
    else:
        ratio_variation = 0.0
    stability = _clamp_ratio(1.0 - ((rms_variation * 0.7) + (ratio_variation * 0.3)))

    if peak_values and rms_values and len(peak_values) == len(rms_values):
        standout_count = sum(
            1
            for peak_value, rms_value in zip(peak_values, rms_values)
            if abs(peak_value) >= max(abs(rms_value) * 1.8, abs(rms) * 1.4)
        )
        impact_density = _clamp_ratio(standout_count / len(peak_values))
    else:
        impact_density = 0.0

    dominant_presence = 1.0 if dominant_hz > 0 else 0.0
    low_share_focus = _clamp_ratio(1.0 - abs((low_share or 0.0) - 0.5) * 1.2)
    frequency_focus = _clamp_ratio(
        (dominant_presence * 0.55) + (low_share_focus * 0.45)
    )

    return {
        "energy": energy,
        "stability": stability,
        "impulse": impulse,
        "impact_density": impact_density,
        "frequency_focus": frequency_focus,
        "low_frequency_share": _clamp_ratio(low_share or 0.0),
    }


def _frequency_stability_snapshot(
    fundamental_values: list[float],
    centroid_values: list[float],
    bandwidth_values: list[float],
) -> dict[str, float]:
    if not fundamental_values:
        return {}
    fundamental_mean = statistics.mean(fundamental_values)
    fundamental_jitter = (
        statistics.pstdev(fundamental_values)
        if len(fundamental_values) >= 2
        else 0.0
    )
    snapshot = {
        "fundamental_mean_hz": float(fundamental_mean),
        "fundamental_jitter_hz": float(fundamental_jitter),
        "fundamental_drift_hz": float(max(fundamental_values) - min(fundamental_values)),
    }
    if centroid_values:
        snapshot["centroid_mean_hz"] = float(statistics.mean(centroid_values))
    if bandwidth_values:
        snapshot["bandwidth_mean_hz"] = float(statistics.mean(bandwidth_values))
    return snapshot


class _BaseVibrationModule(QWidget):
    _RENDER_PHASES = frozenset({"reviewing", "stopped"})

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.selected_display_channel_provider = None
        self.selected_display_channel_sync = None
        self.strict_selected_display_channel = False
        self._factory_state = getattr(
            services.factory_mode,
            "state",
            FactoryControllerState(),
        )
        self._review_payload = getattr(
            services.factory_mode,
            "review_payload",
            FactoryReviewPayload(),
        )
        self._has_plot_data = False
        self._rendered_channel_index: int | None = None
        self._current_payload: dict | None = None

        bus = getattr(self.services, "bus", None)
        if hasattr(bus, "factory_state_changed"):
            bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(bus, "factory_display_reset"):
            bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(bus, "factory_review_changed"):
            bus.factory_review_changed.connect(self._on_factory_review_changed)

    def has_committed_plot_data(self) -> bool:
        return self._has_plot_data

    def current_payload(self) -> dict | None:
        return deepcopy(self._current_payload)

    def current_render_channel(self) -> int | None:
        return self._rendered_channel_index

    def refresh_selected_display_channel(self) -> None:
        self._refresh_committed_rendering()

    def _on_factory_state_changed(self, state) -> None:
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()
        self._refresh_committed_rendering()

    def _on_factory_display_reset(self, _reason: str) -> None:
        self._review_payload = FactoryReviewPayload()
        self._clear_plot()

    def _on_factory_review_changed(self, review) -> None:
        if isinstance(review, FactoryReviewPayload):
            self._review_payload = review
        else:
            self._review_payload = FactoryReviewPayload()
        self._refresh_committed_rendering()

    def _refresh_committed_rendering(self) -> None:
        if not self._can_render_committed_chart():
            self._clear_plot()
            return

        chart_payload = self._chart_payload_for_displayed_config()
        if not chart_payload:
            self._clear_plot()
            return

        channel_index, channel_payload = self._resolve_channel_payload(chart_payload)
        vibration_payload = (
            channel_payload.get("vibration")
            if isinstance(channel_payload, dict)
            else None
        )
        if not isinstance(vibration_payload, dict):
            self._clear_plot(
                rendered_channel_index=(
                    channel_index
                    if self.strict_selected_display_channel and channel_payload is None
                    else None
                )
            )
            return

        if not self._render_vibration_payload(channel_index, vibration_payload):
            self._clear_plot()

    def _render_vibration_payload(
        self,
        channel_index: int | None,
        vibration_payload: dict,
    ) -> bool:
        raise NotImplementedError

    def _clear_plot(self, *, rendered_channel_index: int | None = None) -> None:
        raise NotImplementedError

    def _set_rendered_payload(
        self,
        channel_index: int | None,
        payload: dict,
    ) -> None:
        self._has_plot_data = True
        self._rendered_channel_index = channel_index
        self._current_payload = deepcopy(payload)

    def _clear_rendered_payload(
        self,
        *,
        rendered_channel_index: int | None = None,
    ) -> None:
        self._has_plot_data = False
        self._rendered_channel_index = rendered_channel_index
        self._current_payload = None

    def _can_render_committed_chart(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._RENDER_PHASES
        )

    def _chart_payload_for_displayed_config(self) -> dict[int, dict]:
        if self._review_payload.displayed_config_id != self._factory_state.displayed_config_id:
            return {}

        raw = getattr(self._review_payload, "chart_payload", None)
        if not isinstance(raw, dict):
            return {}

        normalized: dict[int, dict] = {}
        for key, payload in raw.items():
            try:
                channel_index = int(key)
            except (TypeError, ValueError):
                continue
            if channel_index in normalized or not isinstance(payload, dict):
                continue
            normalized[channel_index] = payload
        return normalized

    def _resolve_channel_payload(
        self,
        chart_payload: dict[int, dict],
    ) -> tuple[int | None, dict | None]:
        selected_channel = self._selected_channel_index()
        if self.strict_selected_display_channel and selected_channel is None:
            return None, None
        if selected_channel is not None:
            if selected_channel in chart_payload:
                return selected_channel, chart_payload[selected_channel]
            if self.strict_selected_display_channel:
                return selected_channel, None

        for channel_index, channel_payload in chart_payload.items():
            self._sync_selected_channel(selected_channel, channel_index)
            return channel_index, channel_payload
        return None, None

    def _selected_channel_index(self) -> int | None:
        provider = self.selected_display_channel_provider
        if not callable(provider):
            return None
        provided_channel = provider()
        return provided_channel if isinstance(provided_channel, int) else None

    def _sync_selected_channel(
        self,
        selected_channel: int | None,
        fallback_channel: int,
    ) -> None:
        if selected_channel == fallback_channel:
            return

        sync_callback = self.selected_display_channel_sync
        if callable(sync_callback):
            sync_callback(fallback_channel)


class VibrationAxisModule(_BaseVibrationModule):
    module_id = "vibration_axis"

    def __init__(self, services):
        super().__init__(services)
        self._frequency_snapshot: dict[str, float] = {}
        self._fundamental_frequency_values: list[float] = []
        self._centroid_values: list[float] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.plot = LineChartWidget(
            hide_left_axis_label=True,
            export_schema=LineChartExportSchema.generic(),
        )
        self._set_axis_labels()
        self._centroid_curve = self.plot.plot_item.plot(
            pen=pg.mkPen(color=QColor(20, 184, 166, 120), width=2),
        )
        self._centroid_curve.setZValue(1)
        lay.addWidget(self.plot, 1)

        metrics = QFrame(self)
        metrics.setStyleSheet(
            "QFrame { border-top: 1px solid #dbe3ea; }"
            "QLabel {"
            " color: #334155;"
            " padding: 4px 6px;"
            " background: #f8fafc;"
            " border: 1px solid #e2e8f0;"
            " border-radius: 4px;"
            "}"
        )
        grid = QGridLayout(metrics)
        grid.setContentsMargins(0, 6, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        self.label_dominant_mean = QLabel()
        self.label_dominant_jitter = QLabel()
        labels = (self.label_dominant_mean, self.label_dominant_jitter)
        for index, label in enumerate(labels):
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(label, 0, index)
        lay.addWidget(metrics)

        self._set_empty_labels()
        self._refresh_committed_rendering()

    def indicator_snapshot(self) -> dict[str, float]:
        return dict(self._frequency_snapshot)

    def current_fundamental_frequency_values(self) -> list[float]:
        return list(self._fundamental_frequency_values)

    def current_dominant_frequency_values(self) -> list[float]:
        return self.current_fundamental_frequency_values()

    def current_centroid_values(self) -> list[float]:
        return list(self._centroid_values)

    def _render_vibration_payload(
        self,
        channel_index: int | None,
        vibration_payload: dict,
    ) -> bool:
        times_sec = _numeric_list(vibration_payload.get("frequency_times_sec"))
        fundamental_values = _numeric_list(
            vibration_payload.get("fundamental_frequency_values_hz")
        )
        if not fundamental_values:
            fundamental_values = _numeric_list(
                vibration_payload.get("dominant_frequency_values_hz")
            )
        centroid_values = _numeric_list(
            vibration_payload.get("spectral_centroid_values_hz")
        )
        bandwidth_values = _numeric_list(
            vibration_payload.get("frequency_bandwidth_values_hz")
        )
        if not times_sec or not fundamental_values or len(times_sec) != len(fundamental_values):
            return False
        if centroid_values and len(centroid_values) != len(times_sec):
            centroid_values = []
        if bandwidth_values and len(bandwidth_values) != len(times_sec):
            bandwidth_values = []

        try:
            self.plot.set_series(
                times_sec,
                fundamental_values,
                y_axis_mode="data_focus",
                x_axis_range=vibration_payload.get("x_axis_range_sec"),
            )
        except (TypeError, ValueError):
            return False

        self._set_axis_labels()
        if centroid_values:
            self._centroid_curve.setData(times_sec, centroid_values)
        else:
            self._centroid_curve.setData([], [])

        combined_values = [*fundamental_values, *centroid_values]
        combined_range = _data_focus_range(combined_values)
        if combined_range is not None:
            self.plot.plot_item.vb.enableAutoRange(
                axis=pg.ViewBox.YAxis,
                enable=False,
            )
            self.plot.plot_item.vb.setYRange(*combined_range, padding=0.0)
            self.plot.chart_interactions.refresh_full_view()

        snapshot = _frequency_stability_snapshot(
            fundamental_values,
            centroid_values,
            bandwidth_values,
        )
        self._frequency_snapshot = snapshot
        self._fundamental_frequency_values = list(fundamental_values)
        self._centroid_values = list(centroid_values)
        self._set_frequency_labels(snapshot)
        self._set_rendered_payload(channel_index, vibration_payload)
        return True

    def _clear_plot(self, *, rendered_channel_index: int | None = None) -> None:
        self._clear_rendered_payload(rendered_channel_index=rendered_channel_index)
        self._frequency_snapshot = {}
        self._fundamental_frequency_values = []
        self._centroid_values = []
        self.plot.clear_data()
        self._centroid_curve.setData([], [])
        self._set_axis_labels()
        self._set_empty_labels()

    def _set_axis_labels(self) -> None:
        self.plot.plot_item.setLabel("bottom", "时间 (s)")
        self.plot.plot_item.setLabel("left", "频率 (Hz)")

    def _set_frequency_labels(self, snapshot: dict[str, float]) -> None:
        self.label_dominant_mean.setText(
            f"主频均值 {_format_metric(snapshot.get('fundamental_mean_hz'), digits=1)} Hz"
        )
        self.label_dominant_jitter.setText(
            f"主频抖动 {_format_metric(snapshot.get('fundamental_jitter_hz'), digits=2)} Hz"
        )

    def _set_empty_labels(self) -> None:
        self.label_dominant_mean.setText("主频均值 --")
        self.label_dominant_jitter.setText("主频抖动 --")


class VibrationTrendModule(_BaseVibrationModule):
    module_id = "vibration_trend"

    def __init__(self, services):
        super().__init__(services)
        self._peak_values: list[float] = []
        self._pulse_snapshot: dict[str, float] = {}
        self._pulse_marker_count = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.plot = LineChartWidget(
            hide_left_axis_label=True,
            export_schema=LineChartExportSchema.generic(),
        )
        self._set_axis_labels()
        self._rms_emphasis_curve = self.plot.plot_item.plot(
            pen=pg.mkPen(color=QColor(20, 184, 166, 95), width=5),
        )
        self._rms_emphasis_curve.setZValue(0.5)
        self._peak_curve = self.plot.plot_item.plot(
            pen=pg.mkPen(color="#f97316", width=2),
        )
        self._peak_curve.setZValue(2)
        self._pulse_marker_curve = self.plot.plot_item.plot(
            pen=pg.mkPen(color=QColor(249, 115, 22, 150), width=2),
        )
        self._pulse_marker_curve.setZValue(1)
        lay.addWidget(self.plot, 1)

        readouts = QFrame(self)
        readouts.setStyleSheet(
            "QFrame { border-top: 1px solid #dbe3ea; }"
            "QLabel {"
            " color: #334155;"
            " padding: 4px 6px;"
            " background: #f8fafc;"
            " border: 1px solid #e2e8f0;"
            " border-radius: 4px;"
            "}"
        )
        readout_grid = QGridLayout(readouts)
        readout_grid.setContentsMargins(0, 6, 0, 0)
        readout_grid.setHorizontalSpacing(6)
        readout_grid.setVerticalSpacing(4)
        self.label_impulse = QLabel()
        self.label_impact_density = QLabel()
        self.label_peak_envelope = QLabel()
        for index, label in enumerate(
            (self.label_impulse, self.label_impact_density, self.label_peak_envelope)
        ):
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            readout_grid.addWidget(label, 0, index)
        lay.addWidget(readouts)

        self._set_pulse_labels()
        self._refresh_committed_rendering()

    def current_peak_values(self) -> list[float]:
        return list(self._peak_values)

    def pulse_snapshot(self) -> dict[str, float]:
        return dict(self._pulse_snapshot)

    def current_pulse_marker_count(self) -> int:
        return self._pulse_marker_count

    def _render_vibration_payload(
        self,
        channel_index: int | None,
        vibration_payload: dict,
    ) -> bool:
        times_sec = _numeric_list(vibration_payload.get("times_sec"))
        rms_values = _numeric_list(vibration_payload.get("rms_values"))
        peak_values = _numeric_list(vibration_payload.get("peak_values"))
        if (
            not times_sec
            or not rms_values
            or len(times_sec) != len(rms_values)
            or (peak_values and len(times_sec) != len(peak_values))
        ):
            return False

        try:
            self.plot.set_series(
                times_sec,
                rms_values,
                y_axis_mode="data_focus",
                x_axis_range=vibration_payload.get("x_axis_range_sec"),
            )
        except (TypeError, ValueError):
            return False

        self._set_axis_labels()
        self._rms_emphasis_curve.setData(times_sec, rms_values)
        if peak_values:
            self._peak_curve.setData(times_sec, peak_values)
        else:
            self._peak_curve.setData([], [])
        pulse_x, pulse_y, marker_count = self._pulse_marker_series(
            times_sec,
            rms_values,
            peak_values,
            _finite_float(vibration_payload.get("overall_rms")) or 0.0,
        )
        self._pulse_marker_curve.setData(pulse_x, pulse_y)
        self._pulse_marker_count = marker_count
        combined_values = [*rms_values, *peak_values]
        combined_range = _data_focus_range(combined_values)
        if combined_range is not None:
            self.plot.plot_item.vb.enableAutoRange(
                axis=pg.ViewBox.YAxis,
                enable=False,
            )
            self.plot.plot_item.vb.setYRange(*combined_range, padding=0.0)
            self.plot.chart_interactions.refresh_full_view()
        self._pulse_snapshot = self._trend_snapshot(vibration_payload, peak_values)
        self._set_pulse_labels(self._pulse_snapshot)
        self._peak_values = list(peak_values)
        self._set_rendered_payload(channel_index, vibration_payload)
        return True

    def _clear_plot(self, *, rendered_channel_index: int | None = None) -> None:
        self._clear_rendered_payload(rendered_channel_index=rendered_channel_index)
        self.plot.clear_data()
        self._rms_emphasis_curve.setData([], [])
        self._peak_curve.setData([], [])
        self._pulse_marker_curve.setData([], [])
        self._peak_values = []
        self._pulse_snapshot = {}
        self._pulse_marker_count = 0
        self._set_axis_labels()
        self._set_pulse_labels()

    def _set_axis_labels(self) -> None:
        self.plot.plot_item.setLabel("bottom", "时间 (s)")
        self.plot.plot_item.setLabel("left", "校准幅值")

    def _set_pulse_labels(self, snapshot: dict[str, float] | None = None) -> None:
        if not snapshot:
            self.label_impulse.setText("脉冲强度 --")
            self.label_impact_density.setText("冲击密度 --")
            self.label_peak_envelope.setText("峰值包络 --")
            return

        self.label_impulse.setText(f"脉冲强度 {_format_percent(snapshot['impulse'])}")
        self.label_impact_density.setText(
            f"冲击密度 {_format_percent(snapshot['impact_density'])}"
        )
        self.label_peak_envelope.setText(
            f"峰值包络 {_format_metric(snapshot['peak_envelope'])}"
        )

    def _trend_snapshot(
        self,
        vibration_payload: dict,
        peak_values: list[float],
    ) -> dict[str, float]:
        indicators = _vibration_indicator_snapshot(vibration_payload)
        recent_peak = peak_values[-1] if peak_values else 0.0
        peak_envelope = max((abs(value) for value in peak_values), default=0.0)
        return {
            "impulse": indicators["impulse"],
            "impact_density": indicators["impact_density"],
            "recent_peak": recent_peak,
            "peak_envelope": peak_envelope,
        }

    def _pulse_marker_series(
        self,
        times_sec: list[float],
        rms_values: list[float],
        peak_values: list[float],
        overall_rms: float,
    ) -> tuple[list[float], list[float], int]:
        if not peak_values or len(peak_values) != len(rms_values):
            return [], [], 0

        x_values: list[float] = []
        y_values: list[float] = []
        marker_count = 0
        for time_sec, rms_value, peak_value in zip(times_sec, rms_values, peak_values):
            threshold = max(abs(rms_value) * 1.8, abs(overall_rms) * 1.4)
            if abs(peak_value) < threshold:
                continue
            x_values.extend([time_sec, time_sec, math.nan])
            y_values.extend([rms_value, peak_value, math.nan])
            marker_count += 1
        return x_values, y_values, marker_count


__all__ = [
    "VibrationAxisModule",
    "VibrationTrendModule",
]
