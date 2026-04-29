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


def _iter_widget_tree(widget: QWidget) -> list[QWidget]:
    widgets = [widget]
    try:
        widgets.extend(widget.findChildren(QWidget))
    except RuntimeError:
        pass
    return widgets


def _disable_updates_and_remove_events(widget: QWidget) -> None:
    for target in _iter_widget_tree(widget):
        if not sip.isdeleted(target):
            target.setUpdatesEnabled(False)
            QCoreApplication.removePostedEvents(target)


def _is_plot_widget(widget: QWidget) -> bool:
    widget_type = type(widget)
    return (
        widget_type.__module__ == "pyqtgraph.widgets.PlotWidget"
        and widget_type.__name__ == "PlotWidget"
    )


def _can_close_widget(widget: QWidget) -> bool:
    return not (_is_plot_widget(widget) and getattr(widget, "plotItem", None) is None)


def _clear_pending_live_flushes(widget: QWidget) -> None:
    for target in _iter_widget_tree(widget):
        if sip.isdeleted(target):
            continue
        clear_pending = getattr(target, "_clear_pending_live_flush", None)
        if callable(clear_pending):
            clear_pending()


def _close_child_plot_widgets(widget: QWidget) -> None:
    for child in _iter_widget_tree(widget)[1:]:
        if sip.isdeleted(child) or not _is_plot_widget(child):
            continue
        _disable_updates_and_remove_events(child)
        if _can_close_widget(child):
            try:
                child.close()
            except AttributeError:
                pass


@pytest.fixture(autouse=True)
def _close_top_level_widgets(qapp):
    yield
    widgets = list(qapp.topLevelWidgets())
    for widget in widgets:
        if sip.isdeleted(widget):
            continue
        _clear_pending_live_flushes(widget)
        _disable_updates_and_remove_events(widget)
        _close_child_plot_widgets(widget)
        if _can_close_widget(widget):
            try:
                widget.close()
            except AttributeError:
                pass
        if sip.isdeleted(widget):
            continue
        _disable_updates_and_remove_events(widget)
        widget.deleteLater()
    for widget in widgets:
        if not sip.isdeleted(widget):
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    for _ in range(5):
        qapp.processEvents()
