from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QScrollArea, QVBoxLayout, QWidget

from app.service.result_dashboard_layout import (
    CHANNEL_PRIMARY_ACOUSTIC,
    DEFAULT_RESULT_DASHBOARD_LAYOUT,
    PANEL_FAMILIES,
    PANEL_FAMILY_ACOUSTIC,
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


_STFT_SCORE_METRIC_LABEL = "频谱最低得分："


class ResultDashboardModule(QWidget):
    module_id = "result_dashboard"
    _LIVE_RUNTIME_PHASES = frozenset({"running", "analyzing"})

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
        self._display_layout: dict[str, Any] | None = None
        self._display_layout_structure_signature: tuple | None = None
        self._live_runtime_payloads_by_config_id: dict[int, tuple[str, object]] = {}
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
        self._grid.setSpacing(2)

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
        config_id = self._resolved_display_config_id()
        display_config = self._load_display_config(config_id)
        layout = self._layout_for_display_config(display_config)
        layout_structure_signature = self._layout_structure_signature(layout)
        layout_changed = (
            layout_structure_signature != self._display_layout_structure_signature
        )

        self._displayed_config_id = config_id
        self._display_config = display_config
        self._display_layout = deepcopy(layout)
        self._display_layout_structure_signature = layout_structure_signature

        if layout_changed:
            self._rebuild_dashboard(layout)
        else:
            self._refresh_mounted_panel_context(layout)

    def _rebuild_dashboard(self, layout: dict[str, Any]) -> None:
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
        if hasattr(bus, "factory_runtime_start_accepted"):
            bus.factory_runtime_start_accepted.connect(
                self._on_factory_runtime_start_accepted
            )
        if hasattr(bus, "factory_runtime_analyzing_entered"):
            bus.factory_runtime_analyzing_entered.connect(
                self._on_factory_runtime_analyzing_entered
            )

    def _on_factory_state_changed(self, state) -> None:
        previous_refresh_context = self._state_refresh_context(self._factory_state)
        self._factory_state = (
            state if isinstance(state, FactoryControllerState) else FactoryControllerState()
        )
        self._store_factory_mode_value("state", self._factory_state)
        self._clear_live_runtime_payloads_for_inactive_state()

        if (
            self._resolved_display_config_id() != self._displayed_config_id
            or self._state_refresh_context(self._factory_state) != previous_refresh_context
        ):
            self.refresh_dashboard()

    def _on_config_changed(self) -> None:
        config_id = self._resolved_display_config_id()
        if config_id is not None:
            self._discard_non_import_review_config_payload(config_id)
            self.refresh_dashboard()

    def _on_factory_review_changed(self, review) -> None:
        was_using_review_config = self._display_config_uses_review_payload
        self._review_payload = (
            review if isinstance(review, FactoryReviewPayload) else FactoryReviewPayload()
        )
        self._store_factory_mode_value("review_payload", self._review_payload)
        self._clear_live_runtime_payload(self._payload_config_id(self._review_payload))
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
        self._clear_live_runtime_payload()
        if was_using_review_config and self._resolved_display_config_id() is not None:
            self.refresh_dashboard()

    def _on_factory_runtime_start_accepted(self, payload) -> None:
        self._remember_live_runtime_payload(
            payload,
            "_on_factory_runtime_start_accepted",
        )

    def _on_factory_runtime_analyzing_entered(self, payload) -> None:
        self._remember_live_runtime_payload(
            payload,
            "_on_factory_runtime_analyzing_entered",
        )

    def _remember_live_runtime_payload(self, payload, handler_name: str) -> None:
        config_id = self._payload_config_id(payload)
        if config_id is None or self._payload_run_id(payload) is None:
            return
        self._live_runtime_payloads_by_config_id[config_id] = (handler_name, payload)
        self._replay_live_runtime_payload_to_mounted_summaries(config_id)

    def _replay_live_runtime_payload_to_mounted_summaries(self, config_id: int) -> None:
        if getattr(self._factory_state, "run_phase", None) not in self._LIVE_RUNTIME_PHASES:
            return
        if self._state_displayed_config_id() != config_id:
            return

        layout = self._display_layout
        panels = layout.get("panels") if isinstance(layout, dict) else None
        if not isinstance(panels, list):
            return

        for panel in panels:
            if not isinstance(panel, dict) or panel.get("type") != PANEL_RESULT_SUMMARY:
                continue
            panel_id = panel.get("id")
            if not isinstance(panel_id, str):
                continue
            panel_widget = self._panel_widgets_by_id.get(panel_id)
            if panel_widget is not None:
                self._replay_live_runtime_payload(panel_widget)

    def _clear_live_runtime_payload(self, config_id: int | None = None) -> None:
        if config_id is None:
            self._live_runtime_payloads_by_config_id.clear()
            return
        self._live_runtime_payloads_by_config_id.pop(config_id, None)

    def _clear_live_runtime_payloads_for_inactive_state(self) -> None:
        if getattr(self._factory_state, "run_phase", None) in self._LIVE_RUNTIME_PHASES:
            return

        config_ids = {
            self._state_displayed_config_id(),
            self._state_active_config_id(),
        }
        config_ids.discard(None)
        if not config_ids:
            self._clear_live_runtime_payload()
            return
        for config_id in config_ids:
            self._clear_live_runtime_payload(config_id)

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

    def _state_active_config_id(self) -> int | None:
        config_id = getattr(self._factory_state, "active_config_id", None)
        return config_id if isinstance(config_id, int) and not isinstance(config_id, bool) else None

    @classmethod
    def _state_refresh_context(cls, state: FactoryControllerState) -> tuple:
        return (
            cls._config_id_from_state_field(state, "displayed_config_id"),
            cls._config_id_from_state_field(state, "active_config_id"),
            tuple(
                config_id
                for config_id in getattr(state, "selected_config_ids", ()) or ()
                if isinstance(config_id, int) and not isinstance(config_id, bool)
            ),
            getattr(state, "run_phase", None),
        )

    @staticmethod
    def _config_id_from_state_field(
        state: FactoryControllerState,
        field_name: str,
    ) -> int | None:
        config_id = getattr(state, field_name, None)
        return config_id if isinstance(config_id, int) and not isinstance(config_id, bool) else None

    def _resolved_display_config_id(self) -> int | None:
        for config_id in (
            self._state_displayed_config_id(),
            self._state_active_config_id(),
            self._first_state_selected_config_id(),
            self._first_persisted_selected_config_id(),
        ):
            if config_id is not None:
                return config_id
        return None

    def _first_state_selected_config_id(self) -> int | None:
        return self._first_valid_config_id(
            getattr(self._factory_state, "selected_config_ids", ())
        )

    def _first_persisted_selected_config_id(self) -> int | None:
        selection_settings = getattr(self.services, "factory_selection_settings", None)
        load_selected_config_ids = getattr(selection_settings, "load_selected_config_ids", None)
        if not callable(load_selected_config_ids):
            return None
        try:
            selected_config_ids = load_selected_config_ids()
        except Exception:
            return None
        return self._first_valid_config_id(selected_config_ids)

    @staticmethod
    def _first_valid_config_id(config_ids) -> int | None:
        for config_id in config_ids or ():
            if isinstance(config_id, int) and not isinstance(config_id, bool):
                return config_id
        return None

    @staticmethod
    def _payload_config_id(payload) -> int | None:
        config_id = getattr(payload, "config_id", None)
        if config_id is None:
            config_id = getattr(payload, "displayed_config_id", None)
        return config_id if isinstance(config_id, int) and not isinstance(config_id, bool) else None

    @staticmethod
    def _payload_run_id(payload) -> str | None:
        run_id = getattr(payload, "run_id", None)
        if run_id is None:
            return None
        text = str(run_id)
        return text if text else None

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

    def _layout_for_display_config(self, display_config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(display_config, dict):
            return deepcopy(DEFAULT_RESULT_DASHBOARD_LAYOUT)
        return normalize_result_dashboard_layout_for_display(
            display_config.get("result_dashboard_layout"),
            selected_channels=self._normalize_channels(
                display_config.get("selected_channels", [])
            ),
        )

    @classmethod
    def _layout_structure_signature(cls, layout: dict[str, Any]) -> tuple:
        panels = layout.get("panels")
        panel_signatures = []
        if isinstance(panels, list):
            for panel in panels:
                if not isinstance(panel, dict):
                    continue
                panel_signatures.append(
                    (
                        panel.get("id"),
                        panel.get("type"),
                        cls._coerce_non_negative_int(panel.get("row")),
                        cls._coerce_non_negative_int(panel.get("column")),
                        cls._coerce_positive_int(panel.get("row_span"), default=1),
                        cls._coerce_positive_int(panel.get("column_span"), default=1),
                    )
                )
        return (
            cls._coerce_positive_int(layout.get("rows"), default=1),
            cls._coerce_positive_int(layout.get("columns"), default=1),
            tuple(sorted(panel_signatures)),
        )

    def _review_config_payload_for_display(
        self,
        config_id: int,
    ) -> dict[str, Any] | None:
        if getattr(self._review_payload, "displayed_config_id", None) != config_id:
            return None
        if getattr(self._review_payload, "trigger_source", None) != "import_audio":
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
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._cards_by_panel_id.clear()
        self._panel_widgets_by_id.clear()
        self._grid_positions_by_id.clear()

    def _refresh_mounted_panel_context(self, layout: dict[str, Any]) -> None:
        panels = layout.get("panels")
        if not isinstance(panels, list):
            return

        for panel in panels:
            if not isinstance(panel, dict):
                continue
            panel_id = panel.get("id")
            if not isinstance(panel_id, str):
                continue
            panel_widget = self._panel_widgets_by_id.get(panel_id)
            if panel_widget is None:
                continue
            self._configure_panel_widget(
                panel_widget,
                panel,
                refresh_summary_context=True,
            )

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
        self._enable_compact_chart_mode(panel_widget)
        card = Card(title, panel_widget)
        card.set_compact_dashboard_mode(True)
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

        self._configure_panel_widget(
            panel_widget,
            panel,
            refresh_summary_context=False,
        )
        return panel_widget

    def _configure_panel_widget(
        self,
        panel_widget: QWidget,
        panel: dict[str, Any],
        *,
        refresh_summary_context: bool,
    ) -> None:
        self._bind_panel_channel(panel_widget, panel)
        refresh = getattr(panel_widget, "refresh_selected_display_channel", None)
        if callable(refresh) and panel.get("type") != PANEL_RESULT_SUMMARY:
            refresh()
        if panel.get("type") == PANEL_RESULT_SUMMARY:
            self._sync_panel_factory_state(panel_widget)
            if refresh_summary_context and callable(refresh):
                refresh()
            self._replay_live_runtime_payload(panel_widget)

    def _sync_panel_factory_state(self, panel_widget: QWidget) -> None:
        on_factory_state_changed = getattr(panel_widget, "_on_factory_state_changed", None)
        if callable(on_factory_state_changed):
            on_factory_state_changed(self._factory_state)

    def _replay_live_runtime_payload(self, panel_widget: QWidget) -> None:
        if getattr(self._factory_state, "run_phase", None) not in self._LIVE_RUNTIME_PHASES:
            return

        config_id = self._state_displayed_config_id()
        if config_id is None:
            return
        replay = self._live_runtime_payloads_by_config_id.get(config_id)
        if replay is None:
            return

        handler_name, payload = replay
        if self._payload_config_id(payload) != config_id:
            return
        handler = getattr(panel_widget, handler_name, None)
        if callable(handler):
            handler(payload)

    def _enable_compact_chart_mode(self, panel_widget: QWidget) -> None:
        self._set_compact_display_mode(panel_widget)
        self._set_compact_display_mode(getattr(panel_widget, "plot", None))
        canvas = getattr(panel_widget, "canvas", None)
        self._set_compact_display_mode(getattr(canvas, "heatmap", None))

    @staticmethod
    def _set_compact_display_mode(target) -> None:
        set_compact_display_mode = getattr(target, "set_compact_display_mode", None)
        if callable(set_compact_display_mode):
            set_compact_display_mode(True)

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
        panel_type = str(panel.get("type") or "")
        if panel_type == PANEL_RESULT_SUMMARY:
            channel_key = CHANNEL_PRIMARY_ACOUSTIC
            panel_family = PANEL_FAMILY_ACOUSTIC
        else:
            channel_key = str(panel.get("channel") or "")
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
