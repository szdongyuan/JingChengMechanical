from __future__ import annotations

import gc
import os

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

from consts.style import APP_QSS
from ui.widgets.cards import Card


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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
        if sip.isdeleted(widget):
            continue
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for widget in chart_widgets:
        if not sip.isdeleted(widget):
            QCoreApplication.removePostedEvents(widget)
    for _ in range(5):
        qapp.processEvents()
    gc.collect()
    for _ in range(5):
        qapp.processEvents()
    if GraphicsView is not None:
        GraphicsView.paintEvent = _ORIGINAL_GRAPHICS_VIEW_PAINT_EVENT


def test_card_compact_dashboard_mode_reduces_layout_chrome(qapp):
    body = QWidget()
    card = Card("频谱", body)

    normal_margins = card.layout().contentsMargins()
    assert normal_margins.left() == 12
    assert normal_margins.top() == 10
    assert normal_margins.right() == 12
    assert normal_margins.bottom() == 12
    assert card.layout().spacing() == 8
    assert card.title_metric_value_label.minimumWidth() == 68
    assert card.title_metric_value_label.minimumHeight() == 26
    assert card.property("dashboardCompact") in (None, False)

    card.set_compact_dashboard_mode(True)

    compact_margins = card.layout().contentsMargins()
    assert compact_margins.left() == 4
    assert compact_margins.top() == 4
    assert compact_margins.right() == 4
    assert compact_margins.bottom() == 4
    assert card.layout().spacing() == 3
    assert card.title_metric_value_label.minimumWidth() <= 56
    assert card.title_metric_value_label.minimumHeight() <= 22
    assert card.property("dashboardCompact") is True


def test_card_title_suffix_does_not_overlap_title_at_narrow_width(qapp):
    body = QWidget()
    card = Card("时频图", body)
    card.set_title_metric("频谱最低得分：", "87.42")
    card.resize(220, 120)
    card.show()
    qapp.processEvents()

    assert card.title_label.geometry().right() < card.title_metric_widget.geometry().left()
    assert card.title_metric_widget.geometry().top() >= card.contentsRect().top()
    assert card.title_metric_widget.geometry().right() <= card.contentsRect().right()
    assert card.title_metric_widget.geometry().bottom() < body.geometry().top()
    assert card.title_metric_value_label.minimumWidth() == 68
    assert card.title_metric_value_label.minimumHeight() == 26


def test_card_title_metric_uses_score_value_box_style():
    label_style = APP_QSS.split("QLabel#CardTitleMetricLabel", 1)[1].split("}", 1)[0]
    value_style = APP_QSS.split("QLabel#CardTitleMetricValue", 1)[1].split("}", 1)[0]

    assert "font-size: 15px;" in label_style
    assert "font-size: 15px;" in value_style
    assert "border: 1px solid #cbd5e1;" in value_style


def test_dashboard_compact_card_qss_removes_card_chrome():
    style = APP_QSS.split('QFrame#Card[dashboardCompact="true"]', 1)[1].split(
        "}", 1
    )[0]
    metric_value_style = APP_QSS.split(
        'QFrame#Card[dashboardCompact="true"] QLabel#CardTitleMetricValue',
        1,
    )[1].split("}", 1)[0]

    assert "border: 0px solid transparent;" in style
    assert "border-radius: 0px;" in style
    assert "font-size: 12px;" in metric_value_style
    assert "padding: 1px 6px;" in metric_value_style


def test_card_empty_title_suffix_is_hidden_and_title_uses_row_width(qapp):
    body = QWidget()
    card = Card("时频图", body)
    card.resize(360, 120)
    card.show()
    qapp.processEvents()

    assert card.title_suffix() == ""
    assert card.title_suffix_label.isHidden()
    assert card.title_label.geometry().width() > body.geometry().width() * 0.70
