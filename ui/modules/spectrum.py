from __future__ import annotations

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.widgets.analysis_charts import FftLineChartWidget


class SpectrumModule(QWidget):
    module_id = "spectrum"
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

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.plot = FftLineChartWidget(hide_left_axis_label=True)
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
        if not self._has_plot_data:
            return None
        return self.plot.spectrum_payload()

    def current_render_channel(self) -> int | None:
        return self._rendered_channel_index

    def set_vibration_mode(self, enabled: bool) -> None:
        self.plot.set_vibration_semantics(enabled)

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
        fft_payload = (
            channel_payload.get("fft")
            if isinstance(channel_payload, dict)
            else None
        )
        if not isinstance(fft_payload, dict):
            self._clear_plot(rendered_channel_index=channel_index)
            return

        frequencies_hz = fft_payload.get("frequencies_hz") or []
        levels_db = fft_payload.get("levels_db") or []
        if not frequencies_hz or not levels_db:
            self._clear_plot(rendered_channel_index=channel_index)
            return

        try:
            self.plot.set_spectrum(
                frequencies_hz=frequencies_hz,
                levels_db=levels_db,
                limit_bands=fft_payload.get("limit_bands"),
                limit_envelope=fft_payload.get("limit_envelope"),
                golden_overlay_levels_db=fft_payload.get("golden_overlay_levels_db"),
                show_zero_reference=bool(fft_payload.get("show_zero_reference")),
                x_axis_scale=self._fft_x_axis_scale_for_payload(
                    fft_payload,
                    channel_index,
                ),
            )
        except (TypeError, ValueError):
            self._clear_plot(rendered_channel_index=channel_index)
            return

        self._has_plot_data = True
        self._rendered_channel_index = channel_index

    def _fft_x_axis_scale_for_payload(
        self,
        fft_payload: dict,
        channel_index: int | None,
    ) -> str:
        if fft_payload.get("x_axis_scale") == "log":
            return "log"
        if fft_payload.get("x_axis_scale") == "linear":
            return "linear"
        return self._fft_x_axis_scale_for_channel(channel_index)

    def _clear_plot(self, *, rendered_channel_index: int | None = None) -> None:
        self._has_plot_data = False
        self._rendered_channel_index = rendered_channel_index
        self.plot.clear_data()

    def _can_render_committed_chart(self) -> bool:
        return (
            self._factory_state.displayed_config_id is not None
            and self._factory_state.run_phase in self._RENDER_PHASES
        )

    def _load_displayed_config(self) -> dict:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return {}

        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if not callable(load_config):
            return {}

        return load_config(config_id)

    def _fft_x_axis_scale_for_channel(self, channel_index: int | None) -> str:
        if channel_index is None:
            return "linear"

        try:
            config = self._load_displayed_config()
        except Exception:
            return "linear"

        if not isinstance(config, dict):
            return "linear"

        for channel in config.get("channels", []):
            if not isinstance(channel, dict):
                continue
            if channel.get("channel_index") == channel_index:
                return "log" if channel.get("fft_x_axis_scale") == "log" else "linear"
        return "linear"

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
