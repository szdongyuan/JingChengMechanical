from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QScrollArea, QVBoxLayout, QWidget

from app.service.result_dashboard_layout import (
    DEFAULT_RESULT_DASHBOARD_LAYOUT,
    PANEL_FAMILIES,
    PANEL_LOUDNESS,
    PANEL_OCTAVE,
    PANEL_RESULT_SUMMARY,
    PANEL_SPECTRUM,
    PANEL_SPL,
    PANEL_STFT,
    PANEL_TITLES,
    PANEL_VIBRATION_FREQUENCY_STABILITY,
    PANEL_VIBRATION_OCTAVE,
    PANEL_VIBRATION_PULSE,
    PANEL_VIBRATION_SPECTRUM,
    PANEL_VIBRATION_STFT,
    normalize_result_dashboard_layout_for_display,
    resolve_layout_channel,
)
from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.modules.loudness import LoudnessModule
from ui.modules.octave import OctaveModule
from ui.modules.result import ResultModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.modules.stft import StftModule
from ui.modules.vibration import VibrationAxisModule, VibrationTrendModule
from ui.widgets.cards import Card


@dataclass(frozen=True)
class _PanelFactory:
    widget_type: type[QWidget]
    vibration_mode: bool | None = None


_PANEL_FACTORIES: dict[str, _PanelFactory] = {
    PANEL_RESULT_SUMMARY: _PanelFactory(ResultModule),
    PANEL_STFT: _PanelFactory(StftModule, False),
    PANEL_SPECTRUM: _PanelFactory(SpectrumModule, False),
    PANEL_OCTAVE: _PanelFactory(OctaveModule, False),
    PANEL_SPL: _PanelFactory(SplModule),
    PANEL_LOUDNESS: _PanelFactory(LoudnessModule),
    PANEL_VIBRATION_STFT: _PanelFactory(StftModule, True),
    PANEL_VIBRATION_SPECTRUM: _PanelFactory(SpectrumModule, True),
    PANEL_VIBRATION_OCTAVE: _PanelFactory(OctaveModule, True),
    PANEL_VIBRATION_FREQUENCY_STABILITY: _PanelFactory(VibrationAxisModule),
    PANEL_VIBRATION_PULSE: _PanelFactory(VibrationTrendModule),
}


_STFT_SCORE_METRIC_LABEL = "频谱平均得分："


class ResultDashboardModule(QWidget):
    module_id = "result_dashboard"

    def __init__(self, services):
        super().__init__()
        self.services = services
        factory_mode = getattr(self.services, "factory_mode", None)
        self._factory_state = getattr(factory_mode, "state", FactoryControllerState())
        self._review_payload = getattr(
            factory_mode,
            "review_payload",
            FactoryReviewPayload(),
        )
        self._display_config: dict[str, Any] = {}
        self._display_config_uses_review_payload = False
        self._displayed_config_id: int | None = None
        self._cards_by_panel_id: dict[str, Card] = {}
        self._panel_widgets_by_id: dict[str, QWidget] = {}
        self._grid_positions_by_id: dict[str, tuple[int, int, int, int]] = {}
        self._row_count = 0
        self._column_count = 0

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(5)

        self._scroll.setWidget(self._grid_widget)
        root_layout.addWidget(self._scroll, 1)

        self._connect_factory_signals()
        self.refresh_dashboard()

    def cards_by_panel_id(self) -> dict[str, Card]:
        return dict(self._cards_by_panel_id)

    def panel_widget(self, panel_id: str) -> QWidget | None:
        return self._panel_widgets_by_id.get(panel_id)

    def grid_position(self, panel_id: str) -> tuple[int, int, int, int] | None:
        return self._grid_positions_by_id.get(panel_id)

    def refresh_dashboard(self) -> None:
        config_id = self._state_displayed_config_id()
        self._displayed_config_id = config_id
        self._display_config = self._load_display_config(config_id)
        layout = self._display_config.get("result_dashboard_layout")
        if not isinstance(layout, dict):
            layout = DEFAULT_RESULT_DASHBOARD_LAYOUT

        self._clear_grid()
        self._apply_grid_stretch(
            rows=self._coerce_positive_int(layout.get("rows"), default=1),
            columns=self._coerce_positive_int(layout.get("columns"), default=1),
        )

        panels = layout.get("panels")
        if not isinstance(panels, list):
            panels = []

        for panel in panels:
            if not isinstance(panel, dict):
                continue
            self._add_panel_card(panel)

    def _connect_factory_signals(self) -> None:
        bus = getattr(self.services, "bus", None)
        if hasattr(bus, "config_changed"):
            bus.config_changed.connect(self._on_config_changed)
        if hasattr(bus, "factory_state_changed"):
            bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(bus, "factory_review_changed"):
            bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(bus, "factory_display_reset"):
            bus.factory_display_reset.connect(self._on_factory_display_reset)

    def _on_factory_state_changed(self, state) -> None:
        self._factory_state = (
            state if isinstance(state, FactoryControllerState) else FactoryControllerState()
        )
        self._store_factory_mode_value("state", self._factory_state)

        if self._state_displayed_config_id() != self._displayed_config_id:
            self.refresh_dashboard()

    def _on_config_changed(self) -> None:
        config_id = self._state_displayed_config_id()
        if config_id is not None:
            self._discard_non_import_review_config_payload(config_id)
            self.refresh_dashboard()

    def _on_factory_review_changed(self, review) -> None:
        was_using_review_config = self._display_config_uses_review_payload
        self._review_payload = (
            review if isinstance(review, FactoryReviewPayload) else FactoryReviewPayload()
        )
        self._store_factory_mode_value("review_payload", self._review_payload)
        config_id = self._state_displayed_config_id()
        if config_id is None:
            return
        if (
            self._review_config_payload_for_display(config_id) is not None
            or was_using_review_config
        ):
            self.refresh_dashboard()

    def _on_factory_display_reset(self, _reason: str) -> None:
        was_using_review_config = self._display_config_uses_review_payload
        self._review_payload = FactoryReviewPayload()
        self._store_factory_mode_value("review_payload", self._review_payload)
        if was_using_review_config and self._state_displayed_config_id() is not None:
            self.refresh_dashboard()

    def _store_factory_mode_value(self, name: str, value) -> None:
        factory_mode = getattr(self.services, "factory_mode", None)
        if factory_mode is None:
            return
        try:
            setattr(factory_mode, name, value)
        except Exception:
            pass

    def _discard_non_import_review_config_payload(self, config_id: int) -> None:
        if getattr(self._review_payload, "trigger_source", None) == "import_audio":
            return
        if getattr(self._review_payload, "displayed_config_id", None) != config_id:
            return
        if not isinstance(getattr(self._review_payload, "config_payload", None), dict):
            return

        self._review_payload = replace(self._review_payload, config_payload=None)
        self._store_factory_mode_value("review_payload", self._review_payload)

    def _state_displayed_config_id(self) -> int | None:
        config_id = getattr(self._factory_state, "displayed_config_id", None)
        return config_id if isinstance(config_id, int) and not isinstance(config_id, bool) else None

    def _load_display_config(self, config_id: int | None) -> dict[str, Any]:
        if config_id is None:
            self._display_config_uses_review_payload = False
            return {}

        review_config = self._review_config_payload_for_display(config_id)
        if review_config is not None:
            self._display_config_uses_review_payload = True
            return review_config

        self._display_config_uses_review_payload = False
        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config_for_result_dashboard", None)
        if not callable(load_config):
            return {}

        try:
            config = load_config(config_id)
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    def _review_config_payload_for_display(
        self,
        config_id: int,
    ) -> dict[str, Any] | None:
        if getattr(self._review_payload, "displayed_config_id", None) != config_id:
            return None
        config_payload = getattr(self._review_payload, "config_payload", None)
        if not isinstance(config_payload, dict):
            return None

        display_config = deepcopy(config_payload)
        selected_channels = self._normalize_channels(
            display_config.get("selected_channels", [])
        )
        display_config["selected_channels"] = selected_channels
        display_config["result_dashboard_layout"] = (
            normalize_result_dashboard_layout_for_display(
                display_config.get("result_dashboard_layout"),
                selected_channels=selected_channels,
            )
        )
        return display_config

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            widget.setParent(None)
            widget.deleteLater()
        self._cards_by_panel_id.clear()
        self._panel_widgets_by_id.clear()
        self._grid_positions_by_id.clear()

    def _apply_grid_stretch(self, *, rows: int, columns: int) -> None:
        for row in range(max(self._row_count, rows)):
            self._grid.setRowStretch(row, 1 if row < rows else 0)
        for column in range(max(self._column_count, columns)):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        self._row_count = rows
        self._column_count = columns

    def _add_panel_card(self, panel: dict[str, Any]) -> None:
        panel_id = panel.get("id")
        panel_type = panel.get("type")
        if not isinstance(panel_id, str) or not isinstance(panel_type, str):
            return

        factory = _PANEL_FACTORIES.get(panel_type)
        title = PANEL_TITLES.get(panel_type)
        if factory is None or title is None:
            return

        position = self._panel_grid_position(panel)
        if position is None:
            return

        panel_widget = self._create_panel_widget(panel, factory)
        card = Card(title, panel_widget)
        self._bind_stft_score_metric(panel_type, panel_widget, card)

        row, column, row_span, column_span = position
        self._grid.addWidget(card, row, column, row_span, column_span)
        self._cards_by_panel_id[panel_id] = card
        self._panel_widgets_by_id[panel_id] = panel_widget
        self._grid_positions_by_id[panel_id] = position

    def _create_panel_widget(
        self,
        panel: dict[str, Any],
        factory: _PanelFactory,
    ) -> QWidget:
        panel_widget = factory.widget_type(self.services)
        if factory.vibration_mode is not None:
            self._set_vibration_mode(panel_widget, factory.vibration_mode)

        self._bind_panel_channel(panel_widget, panel)
        refresh = getattr(panel_widget, "refresh_selected_display_channel", None)
        if callable(refresh):
            refresh()
        return panel_widget

    def _bind_stft_score_metric(
        self,
        panel_type: str,
        panel_widget: QWidget,
        card: Card,
    ) -> None:
        if panel_type not in {PANEL_STFT, PANEL_VIBRATION_STFT}:
            return

        current_score_title = getattr(panel_widget, "current_score_title", None)
        if callable(current_score_title):
            self._set_stft_card_score_metric(card, current_score_title())

        score_title_changed = getattr(panel_widget, "score_title_changed", None)
        if hasattr(score_title_changed, "connect"):
            score_title_changed.connect(
                lambda text, dashboard_card=card: self._set_stft_card_score_metric(
                    dashboard_card,
                    text,
                )
            )

    @staticmethod
    def _set_stft_card_score_metric(card: Card, text: str) -> None:
        card.set_title_metric(
            _STFT_SCORE_METRIC_LABEL,
            ResultDashboardModule._stft_score_value(text),
        )

    @staticmethod
    def _stft_score_value(text: str) -> str:
        normalized = str(text or "").strip()
        for separator in ("：", ":"):
            if separator in normalized:
                return normalized.split(separator, 1)[1].strip() or "--"
        return normalized or "--"

    def _bind_panel_channel(self, panel_widget: QWidget, panel: dict[str, Any]) -> None:
        provider = self._provider_for_panel(panel)
        if hasattr(panel_widget, "selected_display_channel_provider"):
            panel_widget.selected_display_channel_provider = provider
        if hasattr(panel_widget, "selected_display_channel_sync"):
            panel_widget.selected_display_channel_sync = None
        panel_widget.strict_selected_display_channel = True

    def _provider_for_panel(self, panel: dict[str, Any]) -> Callable[[], int | None]:
        channel_key = str(panel.get("channel") or "")
        panel_type = str(panel.get("type") or "")
        panel_family = PANEL_FAMILIES.get(panel_type, "")

        def provider() -> int | None:
            return resolve_layout_channel(
                channel_key,
                selected_channels=self._selected_channels(),
                vibration_view_channel_index=self._vibration_channel(),
                panel_family=panel_family,
            )

        return provider

    def _selected_channels(self) -> list[int]:
        return self._normalize_channels(self._display_config.get("selected_channels", []))

    def _vibration_channel(self) -> int | None:
        value = self._display_config.get("vibration_view_channel_index")
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return value if value in self._selected_channels() else None

    @staticmethod
    def _normalize_channels(channels) -> list[int]:
        normalized: list[int] = []
        for channel in channels or ():
            if isinstance(channel, bool):
                continue
            try:
                channel_index = int(channel)
            except (TypeError, ValueError):
                continue
            if channel_index not in normalized:
                normalized.append(channel_index)
        return normalized

    @staticmethod
    def _set_vibration_mode(panel_widget: QWidget, enabled: bool) -> None:
        set_vibration_mode = getattr(panel_widget, "set_vibration_mode", None)
        if callable(set_vibration_mode):
            set_vibration_mode(enabled)

    @classmethod
    def _panel_grid_position(
        cls,
        panel: dict[str, Any],
    ) -> tuple[int, int, int, int] | None:
        row = cls._coerce_non_negative_int(panel.get("row"))
        column = cls._coerce_non_negative_int(panel.get("column"))
        row_span = cls._coerce_positive_int(panel.get("row_span"), default=1)
        column_span = cls._coerce_positive_int(panel.get("column_span"), default=1)
        if row is None or column is None:
            return None
        return (row, column, row_span, column_span)

    @staticmethod
    def _coerce_non_negative_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized >= 0 else None

    @staticmethod
    def _coerce_positive_int(value, *, default: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return default
        return normalized if normalized > 0 else default
