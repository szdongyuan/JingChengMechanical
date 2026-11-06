"""Saved result widgets with a private, non-operational display context."""
from copy import deepcopy
from dataclasses import fields
from types import SimpleNamespace

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

from app.service.factory_review_payloads import FactoryReviewPayload, FactoryReviewWindow
from app.service.result_dashboard_layout import (
    PANEL_FAMILIES, PANEL_TITLES, normalize_result_dashboard_layout_for_display,
    resolve_layout_channel,
)
from app.services_container import FactoryControllerState
from ui.modules.result import ResultModule
from ui.modules.stft import StftModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.octave import OctaveModule
from ui.modules.spl import SplModule
from ui.modules.loudness import LoudnessModule
from ui.modules.vibration import VibrationAxisModule, VibrationTrendModule
from ui.widgets.cards import Card


class _DisplayBus(QObject):
    factory_state_changed = pyqtSignal(object)
    factory_review_changed = pyqtSignal(object)
    factory_display_reset = pyqtSignal(str)


class _FrozenConfig:
    def __init__(self, config):
        self._config = deepcopy(config)

    def load_config(self, _config_id):
        return deepcopy(self._config)

    load_config_for_result_dashboard = load_config


_PANELS = {
    "stft": StftModule, "spectrum": SpectrumModule, "octave": OctaveModule,
    "spl": SplModule, "loudness": LoudnessModule,
    "vibration_stft": StftModule, "vibration_spectrum": SpectrumModule,
    "vibration_octave": OctaveModule,
    "vibration_frequency_stability": VibrationAxisModule,
    "vibration_pulse": VibrationTrendModule,
}


def _display_config(snapshot):
    if snapshot.run.ingress_kind == "IMPORTED":
        return (snapshot.review or {}).get("config_payload") or snapshot.config
    return snapshot.config


def _review(snapshot):
    names = {field.name for field in fields(FactoryReviewPayload)}
    values = {key: deepcopy(value) for key, value in (snapshot.review or {}).items() if key in names}
    if snapshot.run.ingress_kind == "IMPORTED":
        metadata = (snapshot.review or {}).get("review", {}).get("import", {})
        if "display_channel_labels" in metadata:
            values["display_channel_labels"] = deepcopy(metadata["display_channel_labels"])
    for key in ("default_window", "fallback_window"):
        window = values.get(key)
        if isinstance(window, dict):
            values[key] = FactoryReviewWindow(float(window["start_sec"]), float(window["end_sec"]))
    for key in ("chart_payload", "display_channel_labels"):
        if isinstance(values.get(key), dict):
            values[key] = {int(channel): value for channel, value in values[key].items()
                           if str(channel).lstrip("-").isdigit()}
    values.update(displayed_config_id=snapshot.run.config_id, run_id=snapshot.run.run_id,
                  trigger_source=snapshot.run.trigger_source, config_payload=deepcopy(_display_config(snapshot)),
                  manual_reanalysis_allowed=False)
    return FactoryReviewPayload(**values)


class HistoricalAnalysisView(QWidget):
    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        config = _display_config(snapshot)
        channels = []
        for value in config.get("selected_channels", []):
            try:
                if not isinstance(value, bool) and int(value) not in channels:
                    channels.append(int(value))
            except (TypeError, ValueError):
                continue
        if snapshot.run.ingress_kind == "IMPORTED":
            # File-column ordering may put chart-only channels first. Preserve
            # the saved configuration's mapped channel priority for dashboards.
            mapped = [channel for channel in snapshot.config.get("selected_channels", [])
                      if type(channel) is int and channel in channels]
            channels = list(dict.fromkeys(mapped + channels))
        vibration = config.get("vibration_view_channel_index")
        if type(vibration) is not int or vibration not in channels:
            vibration = None
        self.display_services = SimpleNamespace(
            bus=_DisplayBus(self), config_service=_FrozenConfig(config),
            factory_mode=SimpleNamespace(
                state=FactoryControllerState(displayed_config_id=snapshot.run.config_id,
                    selected_config_ids=(snapshot.run.config_id,), run_phase="reviewing"),
                review_payload=_review(snapshot)),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.result_module = ResultModule(self.display_services)
        self.result_module.selected_display_channel_provider = lambda: resolve_layout_channel(
            "primary_acoustic", selected_channels=channels,
            vibration_view_channel_index=vibration, panel_family="acoustic")
        self.result_module.refresh_selected_display_channel()
        root.addWidget(Card("已保存测量值与 AI 结果", self.result_module))
        self.panel_widgets = {}
        self.cards = {}
        self.dashboard_layout = normalize_result_dashboard_layout_for_display(
            config.get("result_dashboard_layout"), selected_channels=channels)
        grid = QGridLayout()
        root.addLayout(grid, 1)
        for spec in self.dashboard_layout["panels"]:
            kind = spec["type"]
            panel = _PANELS[kind](self.display_services)
            if hasattr(panel, "set_vibration_mode"):
                panel.set_vibration_mode(kind.startswith("vibration_"))
            channel = resolve_layout_channel(spec["channel"], selected_channels=channels,
                vibration_view_channel_index=vibration, panel_family=PANEL_FAMILIES[kind])
            panel.selected_display_channel_provider = lambda selected=channel: selected
            panel.selected_display_channel_sync = None
            panel.strict_selected_display_channel = True
            panel.refresh_selected_display_channel()
            for target in (panel, getattr(panel, "plot", None),
                           getattr(getattr(panel, "canvas", None), "heatmap", None)):
                compact = getattr(target, "set_compact_display_mode", None)
                if callable(compact):
                    compact(True)
            card = Card(PANEL_TITLES[kind], panel)
            card.set_compact_dashboard_mode(True)
            card.setMinimumHeight(230)
            if isinstance(panel, StftModule):
                def score(text, target=card, source=panel):
                    if source.current_score_visible():
                        target.set_title_metric("频谱最低得分：", str(text).replace(":", "：").split("：")[-1].strip() or "--")
                    else:
                        target.set_title_metric("", "")
                score(panel.current_score_title())
                panel.score_title_changed.connect(score)
                panel.score_visibility_changed.connect(
                    lambda _visible, source=panel, update=score: update(source.current_score_title())
                )
            grid.addWidget(card, spec["row"], spec["column"], spec["row_span"], spec["column_span"])
            self.panel_widgets[spec["id"]] = panel
            self.cards[spec["id"]] = card
        for row in range(self.dashboard_layout["rows"]):
            grid.setRowStretch(row, 1)
        for column in range(self.dashboard_layout["columns"]):
            grid.setColumnStretch(column, 1)
