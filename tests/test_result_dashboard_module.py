from __future__ import annotations

import gc
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtWidgets import QApplication, QWidget

try:
    from pyqtgraph.widgets.GraphicsView import GraphicsView
except Exception:
    GraphicsView = None
else:
    _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT = GraphicsView.paintEvent
    _NOOP_GRAPHICS_VIEW_PAINT_EVENT = lambda self, event: None

from app.service.factory_review_payloads import FactoryReviewPayload
from app.service.result_history import (
    ConfigSummaryField,
    FrozenConfigSummary,
    ResultConclusion,
    ResultHistoryCardSnapshot,
    ResultPhase,
    ReviewDisplayPayload,
    RunTerminated,
)
from app.services_container import FactoryControllerState
from ui.modules.result_dashboard import ResultDashboardModule
from ui.widgets.cards import Card


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        self.emissions.append(args)
        for callback in tuple(self._callbacks):
            callback(*args)


class _BusStub:
    def __init__(self):
        for name in (
            "config_changed", "factory_state_changed", "factory_review_changed",
            "factory_display_reset", "factory_runtime_start_accepted",
            "factory_runtime_analyzing_entered", "stft_updated", "fft_updated",
            "spl_updated", "loudness_updated", "octave_updated",
            "current_trace_updated",
        ):
            setattr(self, name, _SignalStub())


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _close_widgets(qapp):
    if GraphicsView is not None:
        GraphicsView.paintEvent = _NOOP_GRAPHICS_VIEW_PAINT_EVENT
    yield
    chart_widgets = []
    for widget in list(qapp.topLevelWidgets()):
        if sip.isdeleted(widget):
            continue
        for child in widget.findChildren(QWidget):
            if type(child).__module__ == "ui.widgets.analysis_charts":
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


def _services():
    return SimpleNamespace(
        bus=_BusStub(),
        config_service=SimpleNamespace(
            load_config=MagicMock(),
            load_config_for_result_dashboard=MagicMock(),
        ),
        factory_selection_settings=SimpleNamespace(
            load_selected_config_ids=MagicMock(return_value=())
        ),
        factory_mode=SimpleNamespace(
            state=FactoryControllerState(),
            review_payload=FactoryReviewPayload(),
            begin_reanalysis=MagicMock(),
        ),
    )


def _panel(
    panel_id: str,
    panel_type: str,
    *,
    row=0,
    column=0,
    row_span=1,
    column_span=1,
    channel="selected_1",
):
    return {
        "id": panel_id,
        "type": panel_type,
        "row": row,
        "column": column,
        "row_span": row_span,
        "column_span": column_span,
        "channel": channel,
    }


def _config(
    panels,
    *,
    selected_channels=(0, 1),
    vibration_channel=1,
    rows=1,
    columns=2,
):
    return {
        "config_id": 101,
        "selected_channels": list(selected_channels),
        "vibration_view_channel_index": vibration_channel,
        "result_dashboard_layout": {
            "version": 2,
            "rows": rows,
            "columns": columns,
            "panels": list(panels),
        },
    }


def _card(run_id: str):
    return ResultHistoryCardSnapshot(
        run_id=run_id,
        config=FrozenConfigSummary(
            101,
            "Frozen",
            (ConfigSummaryField("mode", "history"),),
        ),
        sn="SN",
        trigger_source="manual",
        started_at=datetime(2026, 7, 26, 1),
        terminated_at=datetime(2026, 7, 26, 2),
        phase=ResultPhase.TERMINAL,
        conclusion=ResultConclusion.OK,
        reason=None,
        is_active_run=False,
        manual_judgment_allowed=True,
        manual_reanalysis_allowed=True,
        revision=0,
    )


def _event(
    run_id: str,
    config: dict,
    *,
    fft_levels=(1.0, 2.0),
    chart_payload=None,
):
    if chart_payload is None:
        chart_payload = {
            0: {
                "fft": {
                    "frequencies_hz": [100.0, 200.0],
                    "levels_db": list(fft_levels),
                }
            }
        }
    return RunTerminated(
        _card(run_id),
        review_payload=ReviewDisplayPayload(
            FactoryReviewPayload(
                displayed_config_id=101,
                run_id=run_id,
                trigger_source="manual",
                chart_payload=chart_payload,
            ),
            config,
        ),
    )


def test_dashboard_mounts_only_chart_cards_with_configured_grid_spans(qapp):
    config = _config(
        [
            _panel(
                "spectrum",
                "spectrum",
                row=0,
                column=1,
                column_span=2,
            ),
            _panel(
                "vibration",
                "vibration_pulse",
                row=1,
                column=1,
            ),
        ],
        rows=2,
        columns=3,
    )
    module = ResultDashboardModule(_services())
    module.apply_history_event(_event("run-1", config))

    assert list(module.cards_by_panel_id()) == ["spectrum", "vibration"]
    assert module.grid_position("spectrum") == (0, 1, 1, 2)
    assert module.grid_position("vibration") == (1, 1, 1, 1)


def test_dashboard_uses_compact_spacing_cards_and_plot_modes(qapp):
    config = _config([
        _panel("spectrum", "spectrum"),
        _panel("spl", "spl", row=1),
    ], rows=2)
    module = ResultDashboardModule(_services())
    module.apply_history_event(_event("run-1", config))

    assert module._grid.spacing() == 2
    assert all(
        isinstance(card, Card)
        for card in module.cards_by_panel_id().values()
    )
    assert module.panel_widget("spectrum").plot.compact_display_mode_enabled()
    assert module.panel_widget("spl").plot.compact_display_mode_enabled()


@pytest.mark.parametrize(
    ("panel_type", "channel", "expected"),
    [
        ("spectrum", "selected_1", 0),
        ("spectrum", "selected_2", 3),
        ("vibration_pulse", "vibration", 3),
        ("vibration_pulse", "selected_1", None),
    ],
)
def test_dashboard_resolves_panel_channels_from_frozen_config(
    qapp,
    panel_type,
    channel,
    expected,
):
    config = _config(
        [_panel("chart", panel_type, channel=channel)],
        selected_channels=(0, 3),
        vibration_channel=3,
    )
    module = ResultDashboardModule(_services())
    module.apply_history_event(_event("run-1", config))

    assert (
        module.panel_widget("chart").selected_display_channel_provider()
        == expected
    )


@pytest.mark.parametrize(
    ("panel_type", "channel", "vibration_channel"),
    [
        ("stft", "selected_1", None),
        ("vibration_stft", "vibration", 1),
    ],
)
def test_stft_card_retains_score_metric_and_compact_heatmap(
    qapp,
    panel_type,
    channel,
    vibration_channel,
):
    config = _config(
        [_panel("stft", panel_type, channel=channel)],
        vibration_channel=vibration_channel,
    )
    module = ResultDashboardModule(_services())
    module.apply_history_event(_event("run-1", config, chart_payload={}))
    panel = module.panel_widget("stft")
    card = module.cards_by_panel_id()["stft"]

    assert panel.canvas.heatmap.compact_display_mode_enabled()
    assert card.title_suffix() == "频谱最低得分：--"
    panel.score_title_changed.emit("得分：88.50")
    assert card.title_metric_value_label.text() == "88.50"


def test_same_layout_reuses_chart_widget_and_refreshes_channel_binding(qapp):
    first_config = _config(
        [_panel("spectrum", "spectrum", channel="selected_1")],
        selected_channels=(0, 3),
    )
    second_config = _config(
        [_panel("spectrum", "spectrum", channel="selected_2")],
        selected_channels=(0, 3),
    )
    module = ResultDashboardModule(_services())
    module.apply_history_event(_event("run-1", first_config))
    panel = module.panel_widget("spectrum")
    assert panel.selected_display_channel_provider() == 0

    module.apply_history_event(_event("run-2", second_config))

    assert module.panel_widget("spectrum") is panel
    assert panel.selected_display_channel_provider() == 3


def test_frozen_config_payload_is_authority_and_global_config_is_not_loaded(qapp):
    services = _services()
    config = _config([_panel("spectrum", "spectrum")])
    module = ResultDashboardModule(services)
    module.apply_history_event(_event("run-1", config))

    assert module.displayed_config() == config
    services.config_service.load_config.assert_not_called()
    services.config_service.load_config_for_result_dashboard.assert_not_called()


def test_two_runs_with_same_config_id_keep_distinct_structured_results(qapp):
    config = _config([_panel("spectrum", "spectrum")])
    module = ResultDashboardModule(_services())
    module.apply_history_event(_event("run-1", config, fft_levels=(1, 2)))
    first_payload = module.panel_widget("spectrum").current_plot_payload()
    module.apply_history_event(_event("run-2", config, fft_levels=(9, 10)))
    second_payload = module.panel_widget("spectrum").current_plot_payload()

    module.set_follow_latest(False)
    module.select_run("run-1")

    assert first_payload != second_payload
    assert module.projected_run_id() == "run-1"
    assert module.panel_widget("spectrum").current_plot_payload() == first_payload


def test_global_factory_review_and_state_do_not_change_historical_projection(qapp):
    services = _services()
    config = _config([_panel("spectrum", "spectrum")])
    module = ResultDashboardModule(services)
    module.apply_history_event(_event("run-1", config))
    panel = module.panel_widget("spectrum")
    payload = panel.current_plot_payload()

    services.bus.factory_review_changed.emit(
        FactoryReviewPayload(displayed_config_id=999, run_id="legacy")
    )
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=999, run_phase="reviewing")
    )

    assert module.panel_widget("spectrum") is panel
    assert panel.current_plot_payload() == payload
    assert module.projected_run_id() == "run-1"


def test_dashboard_cleanup_keeps_projection_callbacks_inside_owned_qt_tree(qapp):
    module = ResultDashboardModule(_services())
    module.apply_history_event(
        _event("run-1", _config([_panel("spectrum", "spectrum")]))
    )
    bus = module.projection_bus()
    chart = module.panel_widget("spectrum")

    assert bus.parent() is module
    assert chart.parent() is not None
    module.close()
