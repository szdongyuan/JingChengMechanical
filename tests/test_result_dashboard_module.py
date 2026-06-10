from __future__ import annotations

import gc
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtWidgets import QApplication, QWidget

try:
    from pyqtgraph.widgets.GraphicsView import GraphicsView
except Exception:  # pragma: no cover - pyqtgraph import is environment-dependent
    GraphicsView = None
else:
    _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT = GraphicsView.paintEvent
    _NOOP_GRAPHICS_VIEW_PAINT_EVENT = lambda self, event: None

from app.service.result_dashboard_layout import DEFAULT_RESULT_DASHBOARD_LAYOUT
from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.modules.result_dashboard import ResultDashboardModule


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.stft_updated = _SignalStub()
        self.fft_updated = _SignalStub()
        self.spl_updated = _SignalStub()
        self.loudness_updated = _SignalStub()
        self.octave_updated = _SignalStub()
        self.current_trace_updated = _SignalStub()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _close_top_level_widgets(qapp):
    if GraphicsView is not None:
        GraphicsView.paintEvent = _NOOP_GRAPHICS_VIEW_PAINT_EVENT
    yield
    chart_widgets = []
    for widget in list(qapp.topLevelWidgets()):
        if sip.isdeleted(widget):
            continue
        for child in widget.findChildren(QWidget):
            if _is_analysis_chart_widget(child):
                chart_widgets.append(child)
                _disable_updates_and_remove_events(child)
                _close_plot_widget(child)
        widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        try:
            widget.close()
        except AttributeError:
            pass
        if not sip.isdeleted(widget):
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for widget in chart_widgets:
        if not sip.isdeleted(widget):
            QCoreApplication.removePostedEvents(widget)
    for _ in range(3):
        qapp.processEvents()
    gc.collect()
    for _ in range(3):
        qapp.processEvents()
    if GraphicsView is not None:
        GraphicsView.paintEvent = _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT


def _is_analysis_chart_widget(widget: QWidget) -> bool:
    return type(widget).__module__ == "ui.widgets.analysis_charts"


def _disable_updates_and_remove_events(widget: QWidget) -> None:
    widget.setUpdatesEnabled(False)
    widgets = [widget]
    try:
        widgets.extend(widget.findChildren(QWidget))
    except RuntimeError:
        pass
    for target in widgets:
        if not sip.isdeleted(target):
            target.setUpdatesEnabled(False)
            QCoreApplication.removePostedEvents(target)


def _close_plot_widget(chart_widget: QWidget) -> None:
    plot_widget = getattr(chart_widget, "plot_widget", None)
    if plot_widget is None or sip.isdeleted(plot_widget):
        return
    _disable_updates_and_remove_events(plot_widget)
    if getattr(plot_widget, "plotItem", None) is None:
        return
    try:
        plot_widget.close()
    except AttributeError:
        pass


def _make_services_with_config(config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        bus=_BusStub(),
        config_service=SimpleNamespace(
            load_config=MagicMock(return_value=config),
            load_config_for_result_dashboard=MagicMock(return_value=config),
        ),
        factory_mode=SimpleNamespace(
            state=FactoryControllerState(),
            review_payload=FactoryReviewPayload(),
        ),
    )


def test_dashboard_renders_cards_from_layout(qapp):
    services = _make_services_with_config(
        {
            "config_id": 101,
            "selected_channels": [0, 1],
            "vibration_view_channel_index": 1,
            "result_dashboard_layout": {
                "version": 1,
                "columns": 2,
                "rows": 1,
                "panels": [
                    {
                        "id": "summary",
                        "type": "result_summary",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "none",
                    },
                    {
                        "id": "vib",
                        "type": "vibration_pulse",
                        "row": 0,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "vibration",
                    },
                ],
            },
        }
    )
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )

    assert [card.title() for card in module.cards_by_panel_id().values()] == [
        "结果摘要",
        "脉冲强度（振动）",
    ]


def test_duplicate_spectrum_panels_get_different_channel_providers(qapp):
    services = _make_services_with_config(
        {
            "config_id": 101,
            "selected_channels": [0, 1],
            "vibration_view_channel_index": 1,
            "result_dashboard_layout": {
                "version": 1,
                "columns": 3,
                "rows": 1,
                "panels": [
                    {
                        "id": "summary",
                        "type": "result_summary",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "none",
                    },
                    {
                        "id": "spectrum_a",
                        "type": "spectrum",
                        "row": 0,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "selected_1",
                    },
                    {
                        "id": "spectrum_b",
                        "type": "spectrum",
                        "row": 0,
                        "column": 2,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "selected_2",
                    },
                ],
            },
        }
    )
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )

    first = module.panel_widget("spectrum_a")
    second = module.panel_widget("spectrum_b")
    assert first.selected_display_channel_provider() == 0
    assert second.selected_display_channel_provider() == 1
    assert first.strict_selected_display_channel is True
    assert second.strict_selected_display_channel is True


def test_vibration_panel_selected_binding_returns_none_for_non_vibration_channel(qapp):
    services = _make_services_with_config(
        {
            "config_id": 101,
            "selected_channels": [0, 1],
            "vibration_view_channel_index": 1,
            "result_dashboard_layout": {
                "version": 1,
                "columns": 2,
                "rows": 1,
                "panels": [
                    {
                        "id": "summary",
                        "type": "result_summary",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "none",
                    },
                    {
                        "id": "vib",
                        "type": "vibration_pulse",
                        "row": 0,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "selected_1",
                    },
                ],
            },
        }
    )
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )

    vibration_panel = module.panel_widget("vib")
    assert vibration_panel.selected_display_channel_provider() is None
    assert vibration_panel.strict_selected_display_channel is True


def test_vibration_panel_provider_returns_none_when_vibration_channel_not_configured(qapp):
    services = _make_services_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "vibration_view_channel_index": None,
            "result_dashboard_layout": {
                "version": 1,
                "columns": 2,
                "rows": 1,
                "panels": [
                    {
                        "id": "summary",
                        "type": "result_summary",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "none",
                    },
                    {
                        "id": "vib",
                        "type": "vibration_pulse",
                        "row": 0,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "selected_1",
                    },
                ],
            },
        }
    )
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )

    vibration_panel = module.panel_widget("vib")
    assert vibration_panel.selected_display_channel_provider() is None


def test_dashboard_places_cards_with_configured_grid_spans(qapp):
    services = _make_services_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "vibration_view_channel_index": None,
            "result_dashboard_layout": {
                "version": 1,
                "columns": 3,
                "rows": 2,
                "panels": [
                    {
                        "id": "summary",
                        "type": "result_summary",
                        "row": 0,
                        "column": 0,
                        "row_span": 2,
                        "column_span": 1,
                        "channel": "none",
                    },
                    {
                        "id": "spectrum",
                        "type": "spectrum",
                        "row": 0,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 2,
                        "channel": "selected_1",
                    },
                ],
            },
        }
    )
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )

    assert module.grid_position("summary") == (0, 0, 2, 1)
    assert module.grid_position("spectrum") == (0, 1, 1, 2)


def test_dashboard_keeps_configured_cards_present_when_payload_data_missing(qapp):
    services = _make_services_with_config(
        {
            "config_id": 101,
            "selected_channels": [0, 1],
            "vibration_view_channel_index": 1,
            "result_dashboard_layout": {
                "version": 1,
                "columns": 3,
                "rows": 1,
                "panels": [
                    {
                        "id": "summary",
                        "type": "result_summary",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "none",
                    },
                    {
                        "id": "spectrum",
                        "type": "spectrum",
                        "row": 0,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "selected_1",
                    },
                    {
                        "id": "vib",
                        "type": "vibration_pulse",
                        "row": 0,
                        "column": 2,
                        "row_span": 1,
                        "column_span": 1,
                        "channel": "vibration",
                    },
                ],
            },
        }
    )
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )
    before = {
        panel_id: module.grid_position(panel_id)
        for panel_id in ("summary", "spectrum", "vib")
    }

    services.bus.factory_review_changed.emit(FactoryReviewPayload(displayed_config_id=101))
    qapp.processEvents()

    assert list(module.cards_by_panel_id()) == ["summary", "spectrum", "vib"]
    assert {
        panel_id: module.grid_position(panel_id)
        for panel_id in ("summary", "spectrum", "vib")
    } == before


def test_dashboard_uses_display_config_loader_for_runtime_layout(qapp):
    display_config = {
        "config_id": 101,
        "selected_channels": [0],
        "vibration_view_channel_index": None,
        "result_dashboard_layout": DEFAULT_RESULT_DASHBOARD_LAYOUT,
    }
    services = _make_services_with_config(display_config)
    module = ResultDashboardModule(services)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )

    assert "summary" in module.cards_by_panel_id()
    services.config_service.load_config_for_result_dashboard.assert_called_with(101)
    services.config_service.load_config.assert_not_called()
