from __future__ import annotations

from copy import deepcopy

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.analysis_charts import LineChartWidget


class LoudnessModule(QWidget):
    module_id = "loudness"
    title_changed = pyqtSignal(str)
    _RENDER_PHASES = frozenset({"reviewing", "stopped"})

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.selected_display_channel_provider = None
        self.selected_display_channel_sync = None
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
        self._current_title = "响度"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.plot = LineChartWidget(hide_left_axis_label=True)
        self.plot.plot_item.setLabel("bottom", "Time (s)")
        self.plot.plot_item.setLabel("left", "Loudness")
        lay.addWidget(self.plot, 1)

        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)

        self._refresh_committed_rendering()

    def has_committed_plot_data(self) -> bool:
        return self._has_plot_data

    def current_plot_payload(self) -> dict | None:
        return deepcopy(self._current_payload)

    def current_render_channel(self) -> int | None:
        return self._rendered_channel_index

    def current_title(self) -> str:
        return self._current_title

    def refresh_selected_display_channel(self) -> None:
        self._refresh_committed_rendering()

    @staticmethod
    def _title_for_unit(unit: str | None) -> str:
        normalized = str(unit or "").strip().lower()
        if normalized in {"sone", "phon"}:
            return f"响度（{normalized}）"
        return "响度"

    def _emit_title_for_unit(self, unit: str | None) -> None:
        self._current_title = self._title_for_unit(unit)
        self.title_changed.emit(self._current_title)

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
        loudness_payload = (
            channel_payload.get("loudness")
            if isinstance(channel_payload, dict)
            else None
        )
        if not isinstance(loudness_payload, dict):
            self._clear_plot()
            return

        times_sec = loudness_payload.get("times_sec") or []
        values = loudness_payload.get("values") or []
        unit = str(loudness_payload.get("unit") or "").strip().lower()
        if unit not in {"sone", "phon"} or not times_sec or not values:
            self._clear_plot()
            return

        try:
            self.plot.set_series(
                times_sec,
                values,
                lower_limit=loudness_payload.get("lower_limit"),
                upper_limit=loudness_payload.get("upper_limit"),
                y_axis_mode="data_focus",
            )
        except (TypeError, ValueError):
            self._clear_plot()
            return

        self.plot.plot_item.setLabel("bottom", "Time (s)")
        self.plot.plot_item.setLabel("left", f"Loudness ({unit})")
        self._has_plot_data = True
        self._rendered_channel_index = channel_index
        self._current_payload = {
            "times_sec": list(times_sec),
            "values": list(values),
            "unit": unit,
            "lower_limit": loudness_payload.get("lower_limit"),
            "upper_limit": loudness_payload.get("upper_limit"),
        }
        self._emit_title_for_unit(unit)

    def _clear_plot(self) -> None:
        self._has_plot_data = False
        self._rendered_channel_index = None
        self._current_payload = None
        self.plot.clear_data()
        self.plot.plot_item.setLabel("bottom", "Time (s)")
        self.plot.plot_item.setLabel("left", "Loudness")
        self._emit_title_for_unit(None)

    def _can_render_committed_chart(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._RENDER_PHASES
        )

    def _chart_payload_for_displayed_config(self) -> dict[int, dict]:
        if self._review_payload.displayed_config_id != self._factory_state.displayed_config_id:
            return {}

        raw_chart_payload = getattr(self._review_payload, "chart_payload", None)
        if not isinstance(raw_chart_payload, dict):
            return {}

        normalized: dict[int, dict] = {}
        for channel_key, channel_payload in raw_chart_payload.items():
            try:
                channel_index = int(channel_key)
            except (TypeError, ValueError):
                continue
            if channel_index in normalized or not isinstance(channel_payload, dict):
                continue
            normalized[channel_index] = channel_payload
        return normalized

    def _resolve_channel_payload(
        self,
        chart_payload: dict[int, dict],
    ) -> tuple[int | None, dict | None]:
        selected_channel = self._selected_channel_index()
        if selected_channel is not None and selected_channel in chart_payload:
            return selected_channel, chart_payload[selected_channel]

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
