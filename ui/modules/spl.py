from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QStackedLayout, QVBoxLayout, QWidget

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.analysis_charts import LineChartWidget


class SplModule(QWidget):
    module_id = "spl"
    _RENDER_PHASES = frozenset({"reviewing"})

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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedLayout()
        self.stack.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.stack, 1)

        self.empty_state = QLabel("No data")
        self.empty_state.setObjectName("ChartEmptyState")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty_state)

        self.plot = LineChartWidget()
        self.stack.addWidget(self.plot)

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
        if not self._has_plot_data:
            return None
        return self.plot.series_payload()

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
        spl_payload = (
            channel_payload.get("spl")
            if isinstance(channel_payload, dict)
            else None
        )
        if not isinstance(spl_payload, dict):
            self._clear_plot()
            return

        times_sec = spl_payload.get("times_sec") or []
        values_db = spl_payload.get("values_db") or []
        if not times_sec or not values_db:
            self._clear_plot()
            return

        try:
            self.plot.set_series(
                times_sec,
                values_db,
                lower_limit=spl_payload.get("lower_limit_db"),
                upper_limit=spl_payload.get("upper_limit_db"),
            )
        except (TypeError, ValueError):
            self._clear_plot()
            return

        self._has_plot_data = True
        self._rendered_channel_index = channel_index
        self.stack.setCurrentWidget(self.plot)

    def _clear_plot(self) -> None:
        self._has_plot_data = False
        self._rendered_channel_index = None
        self.stack.setCurrentWidget(self.empty_state)

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
        if not callable(sync_callback):
            return

        typed_sync_callback = sync_callback
        typed_sync_callback(fallback_channel)
