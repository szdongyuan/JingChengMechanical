from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QWidget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _disable_updates(widget: QWidget) -> None:
    widget.setUpdatesEnabled(False)
    try:
        children = widget.findChildren(QWidget)
    except RuntimeError:
        return
    for child in children:
        if not sip.isdeleted(child):
            child.setUpdatesEnabled(False)


def _is_chart_test_widget(widget: QWidget) -> bool:
    module_name = type(widget).__module__
    return module_name == "ui.widgets.analysis_charts" or module_name.startswith(
        "ui.modules."
    )


def _remove_posted_widget_events(widget: QWidget) -> None:
    widgets = [widget]
    try:
        widgets.extend(widget.findChildren(QWidget))
    except RuntimeError:
        pass
    for target in widgets:
        if not sip.isdeleted(target):
            QCoreApplication.removePostedEvents(target)


def _close_plot_widget(chart_widget: QWidget) -> None:
    plot_widget = getattr(chart_widget, "plot_widget", None)
    if plot_widget is None or sip.isdeleted(plot_widget):
        return
    _disable_updates(plot_widget)
    _remove_posted_widget_events(plot_widget)
    if getattr(plot_widget, "plotItem", None) is None:
        return
    try:
        plot_widget.close()
    except AttributeError:
        pass


def _patch_chart_widget_close(monkeypatch) -> None:
    from ui.widgets import analysis_charts

    original_close = analysis_charts._BasePlotChart.close

    def _close_without_pending_paints(self, *args, **kwargs):
        if not sip.isdeleted(self):
            _disable_updates(self)
            _close_plot_widget(self)
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        return original_close(self, *args, **kwargs)

    monkeypatch.setattr(
        analysis_charts._BasePlotChart,
        "close",
        _close_without_pending_paints,
    )


@pytest.fixture(autouse=True)
def _close_top_level_widgets(qapp, monkeypatch):
    _patch_chart_widget_close(monkeypatch)
    yield
    widgets = list(qapp.topLevelWidgets())
    chart_widgets = [widget for widget in widgets if _is_chart_test_widget(widget)]
    for widget in chart_widgets:
        if not sip.isdeleted(widget):
            _disable_updates(widget)
            _close_plot_widget(widget)
            _remove_posted_widget_events(widget)
    for widget in widgets:
        if sip.isdeleted(widget):
            continue
        try:
            widget.close()
        except AttributeError:
            pass
        if sip.isdeleted(widget):
            continue
        if widget in chart_widgets:
            _remove_posted_widget_events(widget)
        widget.deleteLater()
    for widget in chart_widgets:
        if not sip.isdeleted(widget):
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    for _ in range(5):
        qapp.processEvents()
