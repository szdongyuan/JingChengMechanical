from __future__ import annotations

import math

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication, QWidget

import ui.widgets.chart_interactions as chart_interactions_module
from ui.widgets.analysis_charts import (
    FftLineChartWidget,
    LineChartExportSchema,
    LineChartWidget,
    OctaveBarChartWidget,
    StftImageChartWidget,
)

from ._support import _show_widget


def _assert_view_range_approx(actual, expected) -> None:
    assert actual[0] == pytest.approx(expected[0])
    assert actual[1] == pytest.approx(expected[1])


def _plot_center(widget) -> QPoint:
    rect = widget.plot_item.vb.sceneBoundingRect()
    center = widget.plot_widget.mapFromScene(rect.center())
    return QPoint(int(round(center.x())), int(round(center.y())))


def _plot_pos_for_fraction(widget, fraction: tuple[float, float]) -> QPoint:
    rect = widget.plot_item.vb.sceneBoundingRect()
    scene_pos = QPointF(
        rect.left() + rect.width() * float(fraction[0]),
        rect.top() + rect.height() * float(fraction[1]),
    )
    mapped = widget.plot_widget.mapFromScene(scene_pos)
    return QPoint(int(round(mapped.x())), int(round(mapped.y())))


def _mouse_event(
    widget,
    event_type: QEvent.Type,
    position: QPoint,
    *,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton | Qt.MouseButtons,
) -> QMouseEvent:
    viewport = widget.plot_widget.viewport()
    return QMouseEvent(
        event_type,
        QPointF(float(position.x()), float(position.y())),
        QPointF(viewport.mapToGlobal(position)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


def _mouse_double_click_event_at_plot_center(
    widget,
    *,
    button: Qt.MouseButton,
) -> QMouseEvent:
    return _mouse_event(
        widget,
        QEvent.Type.MouseButtonDblClick,
        _plot_center(widget),
        button=button,
        buttons=button,
    )


def _mouse_press_event_at_plot_center(
    widget,
    *,
    button: Qt.MouseButton,
) -> QMouseEvent:
    return _mouse_event(
        widget,
        QEvent.Type.MouseButtonPress,
        _plot_center(widget),
        button=button,
        buttons=button,
    )


def _wheel_event_at_plot_center(widget, *, delta_y: int) -> QWheelEvent:
    position = _plot_center(widget)
    viewport = widget.plot_widget.viewport()
    return QWheelEvent(
        QPointF(float(position.x()), float(position.y())),
        QPointF(viewport.mapToGlobal(position)),
        QPoint(0, 0),
        QPoint(0, int(delta_y)),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def _send_drag_through_event_filter(
    widget,
    *,
    start_fraction: tuple[float, float],
    end_fraction: tuple[float, float],
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> None:
    viewport = widget.plot_widget.viewport()
    start = _plot_pos_for_fraction(widget, start_fraction)
    end = _plot_pos_for_fraction(widget, end_fraction)

    press = _mouse_event(
        widget,
        QEvent.Type.MouseButtonPress,
        start,
        button=button,
        buttons=button,
    )
    move = _mouse_event(
        widget,
        QEvent.Type.MouseMove,
        end,
        button=Qt.MouseButton.NoButton,
        buttons=button,
    )
    release = _mouse_event(
        widget,
        QEvent.Type.MouseButtonRelease,
        end,
        button=button,
        buttons=Qt.MouseButton.NoButton,
    )

    assert widget.chart_interactions.eventFilter(viewport, press) is True
    assert widget.chart_interactions.eventFilter(viewport, move) is True
    assert widget.chart_interactions.eventFilter(viewport, release) is True


def test_non_stft_charts_install_controller_without_mode_toolbar(qapp):
    widgets = [LineChartWidget(), FftLineChartWidget(), OctaveBarChartWidget()]
    try:
        for widget in widgets:
            controller = widget.chart_interactions
            assert controller is not None
            assert not hasattr(controller, "_mode_buttons")
            assert not hasattr(controller, "toolbar")
            assert controller.reset_button is None
    finally:
        for widget in widgets:
            widget.close()


def test_stft_chart_installs_shared_controller_without_reset_button(qapp):
    widget = StftImageChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=[[1.0, 2.0, 3.0], [2.0, 3.0, 4.0]],
        )

        assert widget.chart_interactions is not None
        assert widget.chart_interactions.reset_button is None

        widget.plot_item.vb.setXRange(0.75, 1.25, padding=0.0)
        widget.chart_interactions.trigger_context_menu_action("reset_view")

        assert widget.plot_item.vb.viewRange()[0] == pytest.approx([0.0, 2.5])
    finally:
        widget.close()


def test_reset_view_restores_full_range(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        widget.plot_item.vb.setXRange(0.75, 1.25, padding=0.0)

        widget.chart_interactions.trigger_context_menu_action("reset_view")

        assert widget.plot_item.vb.viewRange()[0] == pytest.approx([0.0, 2.0])
    finally:
        widget.close()


def test_resize_does_not_create_visible_reset_button(qapp):
    widget = LineChartWidget()
    try:
        widget.resize(360, 180)
        widget.show()
        qapp.processEvents()

        assert widget.chart_interactions.reset_button is None

        widget.resize(520, 260)
        qapp.processEvents()

        assert widget.chart_interactions.reset_button is None
    finally:
        widget.close()


def test_chart_teardown_keeps_reset_button_absent(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        controller = widget.chart_interactions
        assert controller.reset_button is None

        controller.teardown()
        qapp.processEvents()

        assert controller.reset_button is None
    finally:
        widget.close()


def test_box_zoom_helper_updates_view_range(qapp):
    widget = LineChartWidget()
    try:
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])

        widget.chart_interactions.zoom_to_view_rect((0.5, 1.5), (12.0, 28.0))

        x_range, y_range = widget.plot_item.vb.viewRange()
        assert x_range == pytest.approx([0.5, 1.5])
        assert y_range == pytest.approx([12.0, 28.0])
    finally:
        widget.close()


def test_tiny_box_zoom_helper_is_ignored(qapp):
    widget = LineChartWidget()
    try:
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        before = widget.plot_item.vb.viewRange()

        changed = widget.chart_interactions.zoom_to_view_rect(
            (1.0, 1.0 + 1e-12),
            (20.0, 20.0 + 1e-12),
        )

        assert changed is False
        _assert_view_range_approx(widget.plot_item.vb.viewRange(), before)
    finally:
        widget.close()


def test_double_click_event_routes_to_reset_view(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        widget.plot_item.vb.setXRange(0.75, 1.25, padding=0.0)

        event = _mouse_double_click_event_at_plot_center(
            widget,
            button=Qt.MouseButton.LeftButton,
        )
        handled = widget.chart_interactions.eventFilter(widget.plot_widget.viewport(), event)

        assert handled is True
        assert widget.plot_item.vb.viewRange()[0] == pytest.approx([0.0, 2.0])
    finally:
        widget.close()


def test_wheel_event_routes_to_zoom_and_changes_view(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        before = widget.plot_item.vb.viewRange()[0]

        event = _wheel_event_at_plot_center(widget, delta_y=120)
        handled = widget.chart_interactions.eventFilter(widget.plot_widget.viewport(), event)

        after = widget.plot_item.vb.viewRange()[0]
        assert handled is True
        assert (after[1] - after[0]) < (before[1] - before[0])
    finally:
        widget.close()


def test_left_button_box_zoom_can_be_disabled_without_disabling_wheel_zoom(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        controller = widget.chart_interactions
        controller.set_left_box_zoom_enabled(False)

        press = _mouse_press_event_at_plot_center(
            widget,
            button=Qt.MouseButton.LeftButton,
        )
        wheel = _wheel_event_at_plot_center(widget, delta_y=120)

        assert controller.eventFilter(widget.plot_widget.viewport(), press) is False
        assert controller.eventFilter(widget.plot_widget.viewport(), wheel) is True
    finally:
        widget.close()


def test_middle_button_drag_pans_view_without_mode_selection(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        before = widget.plot_item.vb.viewRange()[0]

        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.65, 0.5),
            end_fraction=(0.35, 0.5),
            button=Qt.MouseButton.MiddleButton,
        )

        assert widget.plot_item.vb.viewRange()[0] != pytest.approx(before)
    finally:
        widget.close()


def test_left_button_drag_box_zooms_without_mode_selection(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        before = widget.plot_item.vb.viewRange()

        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.25, 0.75),
            end_fraction=(0.75, 0.25),
            button=Qt.MouseButton.LeftButton,
        )

        after = widget.plot_item.vb.viewRange()
        assert (after[0][1] - after[0][0]) < (before[0][1] - before[0][0])
        assert (after[1][1] - after[1][0]) < (before[1][1] - before[1][0])
    finally:
        widget.close()


def test_selected_range_api_is_not_exposed(qapp):
    widget = LineChartWidget()
    try:
        controller = widget.chart_interactions
        assert not hasattr(controller, "selected_x_range")
        assert not hasattr(controller, "set_selected_x_range")
        assert not hasattr(controller, "clear_selection")
        assert not hasattr(controller, "select_domain_x_range")
        assert not hasattr(controller, "_selection_item")
    finally:
        widget.close()


def test_left_button_drag_shows_and_hides_rubber_band(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        viewport = widget.plot_widget.viewport()
        controller = widget.chart_interactions
        start = _plot_pos_for_fraction(widget, (0.25, 0.75))
        end = _plot_pos_for_fraction(widget, (0.75, 0.25))

        press = _mouse_event(
            widget,
            QEvent.Type.MouseButtonPress,
            start,
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        move = _mouse_event(
            widget,
            QEvent.Type.MouseMove,
            end,
            button=Qt.MouseButton.NoButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        release = _mouse_event(
            widget,
            QEvent.Type.MouseButtonRelease,
            end,
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.NoButton,
        )

        assert controller.eventFilter(viewport, press) is True
        assert controller.eventFilter(viewport, move) is True
        assert controller._rubber_band.isVisible()
        assert controller.eventFilter(viewport, release) is True
        assert not controller._rubber_band.isVisible()
    finally:
        widget.close()


def test_left_button_box_zoom_cancel_hides_rubber_band(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        viewport = widget.plot_widget.viewport()
        controller = widget.chart_interactions
        start = _plot_pos_for_fraction(widget, (0.25, 0.75))
        end = _plot_pos_for_fraction(widget, (0.75, 0.25))

        press = _mouse_event(
            widget,
            QEvent.Type.MouseButtonPress,
            start,
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        move = _mouse_event(
            widget,
            QEvent.Type.MouseMove,
            end,
            button=Qt.MouseButton.NoButton,
            buttons=Qt.MouseButton.LeftButton,
        )

        assert controller.eventFilter(viewport, press) is True
        assert controller.eventFilter(viewport, move) is True
        assert controller._rubber_band.isVisible()
        assert controller.eventFilter(viewport, QEvent(QEvent.Type.Leave)) is False
        assert not controller._rubber_band.isVisible()
    finally:
        widget.close()


def test_event_filter_ignores_events_after_viewport_reference_is_unavailable(qapp):
    widget = LineChartWidget()
    try:
        viewport = widget.plot_widget.viewport()
        event = QEvent(QEvent.Type.Leave)
        del widget.chart_interactions._viewport

        handled = widget.chart_interactions.eventFilter(viewport, event)

        assert handled is False
    finally:
        widget.close()


def test_plot_widget_deferred_delete_event_tears_down_rubber_band(qapp):
    widget = LineChartWidget()
    try:
        controller = widget.chart_interactions
        rubber_band = controller._rubber_band
        event = QEvent(QEvent.Type.DeferredDelete)

        handled = controller.eventFilter(widget.plot_widget, event)

        assert handled is False
        assert controller._rubber_band is None
        assert rubber_band.parent() is widget.plot_widget.viewport()
    finally:
        widget.close()


def test_tiny_left_button_drag_does_not_zoom_and_hides_rubber_band(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        viewport = widget.plot_widget.viewport()
        controller = widget.chart_interactions
        before = widget.plot_item.vb.viewRange()
        start = _plot_pos_for_fraction(widget, (0.25, 0.75))
        below_threshold_delta = max(1, QApplication.startDragDistance() - 1)
        end = QPoint(start.x() + below_threshold_delta, start.y())

        press = _mouse_event(
            widget,
            QEvent.Type.MouseButtonPress,
            start,
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        move = _mouse_event(
            widget,
            QEvent.Type.MouseMove,
            end,
            button=Qt.MouseButton.NoButton,
            buttons=Qt.MouseButton.LeftButton,
        )
        release = _mouse_event(
            widget,
            QEvent.Type.MouseButtonRelease,
            end,
            button=Qt.MouseButton.LeftButton,
            buttons=Qt.MouseButton.NoButton,
        )

        assert controller.eventFilter(viewport, press) is True
        assert controller.eventFilter(viewport, move) is True
        assert controller._rubber_band.isVisible()
        assert controller.eventFilter(viewport, release) is True
        _assert_view_range_approx(widget.plot_item.vb.viewRange(), before)
        assert not controller._rubber_band.isVisible()
    finally:
        widget.close()


@pytest.mark.parametrize("widget_cls", [FftLineChartWidget, OctaveBarChartWidget])
def test_wheel_event_routes_to_zoom_for_fft_and_octave(qapp, widget_cls):
    widget = widget_cls()
    try:
        _show_widget(widget, qapp)
        if widget_cls is FftLineChartWidget:
            widget.set_spectrum(
                frequencies_hz=[10.0, 100.0, 1000.0],
                levels_db=[1.0, 2.0, 3.0],
            )
        else:
            widget.set_bands(
                center_freqs_hz=[125.0, 250.0, 500.0],
                levels_db=[1.0, 2.0, 3.0],
            )
        before = widget.plot_item.vb.viewRange()[0]

        event = _wheel_event_at_plot_center(widget, delta_y=120)
        handled = widget.chart_interactions.eventFilter(widget.plot_widget.viewport(), event)

        after = widget.plot_item.vb.viewRange()[0]
        assert handled is True
        assert (after[1] - after[0]) < (before[1] - before[0])
    finally:
        widget.close()


@pytest.mark.parametrize("widget_cls", [FftLineChartWidget, OctaveBarChartWidget])
def test_direct_gestures_route_for_fft_and_octave(qapp, widget_cls):
    widget = widget_cls()
    try:
        _show_widget(widget, qapp)
        if widget_cls is FftLineChartWidget:
            widget.set_spectrum(
                frequencies_hz=[10.0, 100.0, 1000.0],
                levels_db=[1.0, 2.0, 3.0],
            )
        else:
            widget.set_bands(
                center_freqs_hz=[125.0, 250.0, 500.0],
                levels_db=[1.0, 2.0, 3.0],
            )

        before_wheel = widget.plot_item.vb.viewRange()[0]
        event = _wheel_event_at_plot_center(widget, delta_y=120)
        handled = widget.chart_interactions.eventFilter(widget.plot_widget.viewport(), event)
        after_wheel = widget.plot_item.vb.viewRange()[0]
        assert handled is True
        assert (after_wheel[1] - after_wheel[0]) < (
            before_wheel[1] - before_wheel[0]
        )

        widget.chart_interactions.reset_view()
        before_pan = widget.plot_item.vb.viewRange()[0]
        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.65, 0.5),
            end_fraction=(0.35, 0.5),
            button=Qt.MouseButton.MiddleButton,
        )
        assert widget.plot_item.vb.viewRange()[0] != pytest.approx(before_pan)

        widget.chart_interactions.reset_view()
        before_zoom = widget.plot_item.vb.viewRange()
        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.25, 0.75),
            end_fraction=(0.75, 0.25),
            button=Qt.MouseButton.LeftButton,
        )
        after_zoom = widget.plot_item.vb.viewRange()
        assert (after_zoom[0][1] - after_zoom[0][0]) < (
            before_zoom[0][1] - before_zoom[0][0]
        )
        assert (after_zoom[1][1] - after_zoom[1][0]) < (
            before_zoom[1][1] - before_zoom[1][0]
        )
    finally:
        widget.close()


def test_fft_log_wheel_zoom_uses_display_log_coordinates(qapp):
    widget = FftLineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_spectrum(
            frequencies_hz=[10.0, 100.0, 1000.0],
            levels_db=[1.0, 2.0, 3.0],
            x_axis_scale="log",
        )
        before = widget.plot_item.vb.viewRange()[0]

        event = _wheel_event_at_plot_center(widget, delta_y=120)
        handled = widget.chart_interactions.eventFilter(widget.plot_widget.viewport(), event)

        after = widget.plot_item.vb.viewRange()[0]
        assert handled is True
        assert (after[1] - after[0]) < (before[1] - before[0])
        assert after[0] >= math.log10(10.0) - 1e-9
        assert after[1] <= math.log10(1000.0) + 1e-9
    finally:
        widget.close()


def test_fft_log_left_button_box_zoom_uses_display_log_coordinates(qapp):
    widget = FftLineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_spectrum(
            frequencies_hz=[10.0, 100.0, 1000.0],
            levels_db=[1.0, 2.0, 3.0],
            x_axis_scale="log",
        )
        before = widget.plot_item.vb.viewRange()[0]

        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.2, 0.75),
            end_fraction=(0.8, 0.25),
            button=Qt.MouseButton.LeftButton,
        )

        after = widget.plot_item.vb.viewRange()[0]
        assert (after[1] - after[0]) < (before[1] - before[0])
        assert after[0] >= math.log10(10.0) - 1e-9
        assert after[1] <= math.log10(1000.0) + 1e-9
    finally:
        widget.close()


def test_old_mode_api_is_not_exposed(qapp):
    widget = LineChartWidget()
    try:
        assert not hasattr(widget.chart_interactions, "mode")
        assert not hasattr(widget.chart_interactions, "set_mode")
    finally:
        widget.close()


def test_stft_chart_interaction_controller_keeps_legacy_mode_api_absent(qapp):
    widget = StftImageChartWidget()
    try:
        assert widget.chart_interactions is not None
        assert not hasattr(widget.chart_interactions, "mode")
        assert not hasattr(widget.chart_interactions, "set_mode")
    finally:
        widget.close()


def test_context_menu_does_not_include_selected_range_actions(qapp):
    widget = LineChartWidget()
    try:
        labels = widget.chart_interactions.context_menu_action_labels()
        assert "导出选中范围 CSV" not in labels
        assert "清除选择" not in labels
        assert labels == [
            "重置视图",
            "保存图片为 PNG",
            "复制图片",
            "导出全部 CSV",
            "导出可见范围 CSV",
        ]
    finally:
        widget.close()


def test_selected_csv_scope_is_rejected(qapp):
    widget = LineChartWidget()
    try:
        with pytest.raises(ValueError, match="Unsupported CSV export scope"):
            widget.export_csv_table(scope="selected")
    finally:
        widget.close()


def test_stft_context_menu_omits_unsupported_csv_actions(qapp):
    widget = StftImageChartWidget()
    try:
        labels = widget.chart_interactions.context_menu_action_labels()

        assert labels == [
            "重置视图",
            "保存图片为 PNG",
            "复制图片",
        ]
    finally:
        widget.close()


@pytest.mark.parametrize("action_key", ["export_selected_csv", "clear_selection"])
def test_selected_context_menu_actions_are_rejected(qapp, monkeypatch, action_key):
    widget = LineChartWidget()
    calls = []
    try:
        monkeypatch.setattr(
            widget.chart_interactions,
            "export_csv",
            lambda scope: calls.append(scope),
        )

        with pytest.raises(ValueError, match="Unsupported context menu action"):
            widget.chart_interactions.trigger_context_menu_action(action_key)

        assert calls == []
    finally:
        widget.close()


def test_right_click_event_routes_to_custom_context_menu_and_is_handled(
    qapp,
    monkeypatch,
):
    widget = LineChartWidget()
    calls = []
    try:
        _show_widget(widget, qapp)
        monkeypatch.setattr(
            widget.chart_interactions,
            "show_context_menu",
            lambda global_pos: calls.append(global_pos),
        )

        event = _mouse_press_event_at_plot_center(
            widget,
            button=Qt.MouseButton.RightButton,
        )
        handled = widget.chart_interactions.eventFilter(
            widget.plot_widget.viewport(),
            event,
        )

        assert handled is True
        assert calls
    finally:
        widget.close()


def test_context_menu_popup_replaces_previous_chart_menu(qapp, monkeypatch):
    class _Signal:
        def __init__(self):
            self._callbacks = []

        def connect(self, callback):
            self._callbacks.append(callback)

        def emit(self):
            for callback in list(self._callbacks):
                callback()

    class _Action:
        def __init__(self, label):
            self.label = label
            self.enabled = True
            self.triggered = _Signal()

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

    class _Menu:
        def __init__(self, _chart):
            self.aboutToHide = _Signal()
            self.actions = []
            self.closed = False
            self.deleted = False
            self.popup_pos = None
            menus.append(self)

        def addAction(self, label):
            action = _Action(label)
            self.actions.append(action)
            return action

        def popup(self, global_pos):
            self.popup_pos = global_pos

        def exec(self, _global_pos):
            raise AssertionError("context menus must not use blocking exec()")

        def close(self):
            self.closed = True
            self.aboutToHide.emit()

        def deleteLater(self):
            self.deleted = True

    menus = []
    monkeypatch.setattr(chart_interactions_module, "QMenu", _Menu)
    monkeypatch.setattr(chart_interactions_module, "_is_deleted", lambda _wrapped: False)

    first = LineChartWidget()
    second = LineChartWidget()
    try:
        _show_widget(first, qapp)
        _show_widget(second, qapp)

        first.chart_interactions.show_context_menu(QPoint(10, 20))
        second.chart_interactions.show_context_menu(QPoint(30, 40))

        assert len(menus) == 2
        assert menus[0].closed is True
        assert menus[0].deleted is True
        assert menus[1].closed is False
        assert menus[1].popup_pos == QPoint(30, 40)
        assert [action.label for action in menus[1].actions] == [
            "重置视图",
            "保存图片为 PNG",
            "复制图片",
            "导出全部 CSV",
            "导出可见范围 CSV",
        ]
    finally:
        first.close()
        second.close()
        chart_interactions_module.ChartInteractionController._active_context_menu = None


def test_stft_right_click_routes_context_menu_without_consuming_score_clear(
    qapp,
    monkeypatch,
):
    widget = StftImageChartWidget()
    calls = []
    clears = []
    try:
        _show_widget(widget, qapp)
        widget.set_image(
            times_sec=[0.0, 1.0, 2.0],
            frequencies_hz=[100.0, 200.0],
            magnitude_db=[[1.0, 2.0, 3.0], [2.0, 3.0, 4.0]],
        )
        widget.right_clicked.connect(lambda: clears.append(True))
        monkeypatch.setattr(
            widget.chart_interactions,
            "show_context_menu",
            lambda global_pos: calls.append(global_pos),
        )

        event = _mouse_press_event_at_plot_center(
            widget,
            button=Qt.MouseButton.RightButton,
        )
        handled_by_controller = widget.chart_interactions.eventFilter(
            widget.plot_widget.viewport(),
            event,
        )
        handled_by_widget = widget.eventFilter(widget.plot_widget.viewport(), event)

        assert handled_by_controller is False
        assert handled_by_widget is False
        assert calls
        assert clears == [True]
    finally:
        widget.close()


def test_context_menu_reset_action_restores_full_view(qapp):
    widget = LineChartWidget()
    try:
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        widget.plot_item.vb.setXRange(0.75, 1.25, padding=0.0)

        widget.chart_interactions.trigger_context_menu_action("reset_view")

        assert widget.plot_item.vb.viewRange()[0] == pytest.approx([0.0, 2.0])
    finally:
        widget.close()


def test_export_csv_action_writes_visible_scope(tmp_path, qapp, monkeypatch):
    widget = LineChartWidget()
    try:
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        widget.plot_item.vb.setXRange(0.5, 1.5, padding=0.0)
        target = tmp_path / "visible.csv"
        monkeypatch.setattr(
            widget.chart_interactions,
            "_choose_csv_path",
            lambda _scope: str(target),
        )

        widget.chart_interactions.export_csv("visible")

        assert target.read_text(encoding="utf-8").splitlines() == [
            "x_values,y_values,lower_limit,upper_limit",
            "1,20,,",
        ]
    finally:
        widget.close()


def test_save_png_uses_injected_path_and_export_helper(tmp_path, qapp, monkeypatch):
    widget = LineChartWidget()
    calls = []
    try:
        target = tmp_path / "chart.png"
        monkeypatch.setattr(
            widget.chart_interactions,
            "_choose_png_path",
            lambda: str(target),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_save_png_to_path",
            lambda path: calls.append(path),
        )

        widget.chart_interactions.save_png()

        assert calls == [str(target)]
    finally:
        widget.close()


def test_copy_image_uses_injected_clipboard_helper(qapp, monkeypatch):
    widget = LineChartWidget()
    calls = []
    try:
        sentinel_pixmap = object()
        monkeypatch.setattr(
            widget.chart_interactions,
            "_grab_plot_pixmap",
            lambda: sentinel_pixmap,
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_copy_pixmap_to_clipboard",
            lambda pixmap: calls.append(pixmap),
        )

        widget.chart_interactions.copy_image()

        assert calls == [sentinel_pixmap]
    finally:
        widget.close()


def test_copy_image_failure_shows_warning(qapp, monkeypatch):
    widget = LineChartWidget()
    warnings = []
    try:
        monkeypatch.setattr(
            widget.chart_interactions,
            "_grab_plot_pixmap",
            lambda: object(),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_copy_pixmap_to_clipboard",
            lambda _pixmap: (_ for _ in ()).throw(RuntimeError("clipboard failed")),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_show_warning",
            lambda title, text: warnings.append((title, text)),
        )

        widget.chart_interactions.copy_image()

        assert warnings
        assert warnings[0][0] == "复制图像失败"
    finally:
        widget.close()


def test_save_png_failure_shows_warning(tmp_path, qapp, monkeypatch):
    widget = LineChartWidget()
    warnings = []
    try:
        target = tmp_path / "chart.png"
        monkeypatch.setattr(
            widget.chart_interactions,
            "_choose_png_path",
            lambda: str(target),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_save_png_to_path",
            lambda _path: (_ for _ in ()).throw(RuntimeError("image failed")),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_show_warning",
            lambda title, text: warnings.append((title, text)),
        )

        widget.chart_interactions.save_png()

        assert warnings
        assert warnings[0][0] == "保存图像失败"
    finally:
        widget.close()


def test_save_png_export_false_shows_warning(tmp_path, qapp, monkeypatch):
    class FailingImageExporter:
        def __init__(self, plot_item):
            self.plot_item = plot_item

        def export(self, path):
            return False

    widget = LineChartWidget()
    warnings = []
    try:
        target = tmp_path / "chart.png"
        monkeypatch.setattr(
            widget.chart_interactions,
            "_choose_png_path",
            lambda: str(target),
        )
        monkeypatch.setattr(
            chart_interactions_module,
            "ImageExporter",
            FailingImageExporter,
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_show_warning",
            lambda title, text: warnings.append((title, text)),
        )

        widget.chart_interactions.save_png()

        assert warnings
        assert warnings[0][0] == "保存图像失败"
    finally:
        widget.close()


def test_export_csv_failure_shows_warning(tmp_path, qapp, monkeypatch):
    widget = LineChartWidget()
    warnings = []
    try:
        widget.set_series([0.0], [1.0])
        target = tmp_path / "chart.csv"
        monkeypatch.setattr(
            widget.chart_interactions,
            "_choose_csv_path",
            lambda _scope: str(target),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_write_csv_to_path",
            lambda _path, _table: (_ for _ in ()).throw(RuntimeError("csv failed")),
        )
        monkeypatch.setattr(
            widget.chart_interactions,
            "_show_warning",
            lambda title, text: warnings.append((title, text)),
        )

        widget.chart_interactions.export_csv("all")

        assert warnings
        assert warnings[0][0] == "导出 CSV 失败"
    finally:
        widget.close()


def test_line_chart_close_preserves_interaction_controller_for_reuse(qapp):
    widget = LineChartWidget()
    try:
        controller = widget.chart_interactions
        widget.set_series([0.0, 1.0], [1.0, 2.0])

        QWidget.close(widget)
        widget.set_series([0.0, 2.0], [3.0, 4.0])

        table = widget.export_csv_table(scope="all")
        assert widget.chart_interactions is controller
        assert table.rows == [["0", "3", "", ""], ["2", "4", "", ""]]
    finally:
        widget.close()


def test_spl_line_export_schema_uses_time_and_db_columns(qapp):
    widget = LineChartWidget(export_schema=LineChartExportSchema.spl())
    try:
        widget.set_series(
            [0.0, 1.0],
            [70.0, 71.0],
            lower_limit=60.0,
            upper_limit=80.0,
        )

        table = widget.export_csv_table(scope="all")

        assert table.headers == [
            "time_sec",
            "value_db",
            "lower_limit_db",
            "upper_limit_db",
        ]
        assert table.rows == [["0", "70", "60", "80"], ["1", "71", "60", "80"]]
    finally:
        widget.close()


def test_csv_exports_preserve_high_precision_numeric_strings(qapp):
    precise = 0.123456789012345
    large = 1234567.89012345
    expected_precise = "0.123456789012345"
    expected_large = "1234567.89012345"
    widgets = [LineChartWidget(), FftLineChartWidget(), OctaveBarChartWidget()]
    try:
        line_widget, fft_widget, octave_widget = widgets
        line_widget.set_series([precise, large], [precise, large])
        fft_widget.set_spectrum(
            frequencies_hz=[precise, large],
            levels_db=[precise, large],
        )
        octave_widget.set_bands(
            center_freqs_hz=[precise, large],
            levels_db=[precise, large],
        )

        assert line_widget.export_csv_table(scope="all").rows == [
            [expected_precise, expected_precise, "", ""],
            [expected_large, expected_large, "", ""],
        ]
        assert [row[:2] for row in fft_widget.export_csv_table(scope="all").rows] == [
            [expected_precise, expected_precise],
            [expected_large, expected_large],
        ]
        assert [
            row[:2] for row in octave_widget.export_csv_table(scope="all").rows
        ] == [
            [expected_precise, expected_precise],
            [expected_large, expected_large],
        ]
    finally:
        for widget in widgets:
            widget.close()


def test_loudness_export_includes_unit_metadata(qapp):
    widget = LineChartWidget(export_schema=LineChartExportSchema.loudness())
    try:
        widget.set_series(
            [0.0],
            [2.5],
            lower_limit=0.0,
            upper_limit=10.0,
            export_metadata={"unit": "sone"},
        )

        table = widget.export_csv_table(scope="all")

        assert table.headers == [
            "time_sec",
            "value",
            "unit",
            "lower_limit",
            "upper_limit",
        ]
        assert table.rows == [["0", "2.5", "sone", "0", "10"]]
    finally:
        widget.close()


def test_line_export_visible_scope_filters_current_x_view(qapp):
    widget = LineChartWidget()
    try:
        widget.set_series([0.0, 1.0, 2.0], [10.0, 20.0, 30.0])
        widget.plot_item.vb.setXRange(0.5, 1.5, padding=0.0)

        table = widget.export_csv_table(scope="visible")

        assert table.rows == [["1", "20", "", ""]]
    finally:
        widget.close()


def test_line_visible_export_uses_left_box_zoom_range(qapp):
    widget = LineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_series(
            [0.0, 1.0, 2.0, 3.0],
            [10.0, 20.0, 30.0, 40.0],
        )
        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.25, 0.75),
            end_fraction=(0.75, 0.25),
            button=Qt.MouseButton.LeftButton,
        )
        visible_range = widget.chart_interactions.domain_range_for_scope("visible")
        assert visible_range is not None
        table = widget.export_csv_table(scope="visible")
        assert visible_range[0] < 1.0 < 2.0 < visible_range[1]
        assert table.rows == [["1", "20", "", ""], ["2", "30", "", ""]]
    finally:
        widget.close()


def test_fft_export_interpolates_envelope_and_failed_column(qapp):
    widget = FftLineChartWidget()
    try:
        widget.set_spectrum(
            frequencies_hz=[100.0, 200.0, 300.0],
            levels_db=[20.0, 35.0, 80.0],
            limit_envelope={
                "line_points": [
                    {"freq_hz": 100.0, "lower_db": 10.0, "upper_db": 40.0},
                    {"freq_hz": 300.0, "lower_db": 20.0, "upper_db": 60.0},
                ]
            },
        )

        table = widget.export_csv_table(scope="all")

        assert table.headers == [
            "frequency_hz",
            "level_db",
            "golden_level_db",
            "lower_limit_db",
            "upper_limit_db",
            "failed",
        ]
        assert table.rows[0][-1] == "0"
        assert table.rows[2][-1] == "1"
    finally:
        widget.close()


def test_fft_log_export_interpolates_envelope_in_display_space(qapp):
    widget = FftLineChartWidget()
    try:
        widget.set_spectrum(
            frequencies_hz=[10.0, 100.0, 1000.0],
            levels_db=[10.0, 55.0, 70.0],
            limit_envelope={
                "line_points": [
                    {"freq_hz": 10.0, "lower_db": 0.0, "upper_db": 20.0},
                    {"freq_hz": 1000.0, "lower_db": 40.0, "upper_db": 60.0},
                ]
            },
            x_axis_scale="log",
        )

        table = widget.export_csv_table(scope="all")
        row = table.rows[1]

        assert row[0] == "100"
        assert row[3] == "20"
        assert row[4] == "40"
        assert row[5] == "1"
    finally:
        widget.close()


def test_fft_discrete_limit_bands_assign_nearest_row_tighten_bounds_and_compute_failed(qapp):
    widget = FftLineChartWidget()
    try:
        widget.set_spectrum(
            frequencies_hz=[100.0, 200.0, 300.0],
            levels_db=[20.0, 50.0, 80.0],
            limit_bands=[
                {
                    "freq_hz": 190.0,
                    "lower_db": 10.0,
                    "upper_db": 60.0,
                    "actual_db": 50.0,
                    "passed": True,
                },
                {
                    "freq_hz": 210.0,
                    "lower_db": 30.0,
                    "upper_db": 40.0,
                    "actual_db": 50.0,
                    "passed": False,
                },
            ],
        )

        table = widget.export_csv_table(scope="all")

        assert table.rows[0][-3:] == ["", "", ""]
        assert table.rows[1][-3:] == ["30", "40", "1"]
        assert table.rows[2][-3:] == ["", "", ""]
    finally:
        widget.close()


def test_fft_visible_export_filters_by_visible_domain_range(qapp):
    widget = FftLineChartWidget()
    try:
        widget.set_spectrum(
            frequencies_hz=[10.0, 100.0, 1000.0],
            levels_db=[1.0, 2.0, 3.0],
            x_axis_scale="log",
        )
        widget.plot_item.vb.setXRange(
            math.log10(50.0),
            math.log10(500.0),
            padding=0.0,
        )

        table = widget.export_csv_table(scope="visible")

        assert [row[0] for row in table.rows] == ["100"]
    finally:
        widget.close()


def test_fft_log_visible_export_uses_left_box_zoom_domain_range(qapp):
    widget = FftLineChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_spectrum(
            frequencies_hz=[10.0, 30.0, 100.0, 300.0, 1000.0],
            levels_db=[1.0, 2.0, 3.0, 4.0, 5.0],
            x_axis_scale="log",
        )
        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.25, 0.75),
            end_fraction=(0.75, 0.25),
            button=Qt.MouseButton.LeftButton,
        )
        visible_range = widget.chart_interactions.domain_range_for_scope("visible")
        assert visible_range is not None
        table = widget.export_csv_table(scope="visible")
        assert visible_range[0] < 100.0 < 300.0 < visible_range[1]
        assert table.rows == [
            ["100", "3", "", "", "", ""],
            ["300", "4", "", "", "", ""],
        ]
    finally:
        widget.close()


def test_octave_export_writes_passed_column_for_matching_limit_bands(qapp):
    widget = OctaveBarChartWidget()
    try:
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0],
            levels_db=[32.0, 44.0],
            limit_bands=[
                {
                    "freq_hz": 250.0,
                    "lower_db": 30.0,
                    "upper_db": 40.0,
                    "actual_db": 44.0,
                    "passed": False,
                }
            ],
        )

        table = widget.export_csv_table(scope="all")

        assert table.rows[0][-1] == ""
        assert table.rows[1][-3:] == ["30", "40", "0"]
    finally:
        widget.close()


def test_octave_visible_export_uses_left_box_zoom_rendered_range(qapp):
    widget = OctaveBarChartWidget()
    try:
        _show_widget(widget, qapp)
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0, 500.0, 1000.0],
            levels_db=[1.0, 2.0, 3.0, 4.0],
        )
        _send_drag_through_event_filter(
            widget,
            start_fraction=(0.25, 0.75),
            end_fraction=(0.75, 0.25),
            button=Qt.MouseButton.LeftButton,
        )
        visible = widget.export_csv_table(scope="visible")
        assert visible.rows == [
            ["250", "2", "", "", "", ""],
            ["500", "3", "", "", "", ""],
        ]
    finally:
        widget.close()


def test_octave_visible_export_filters_by_rendered_band_positions(qapp):
    widget = OctaveBarChartWidget()
    try:
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0, 500.0],
            levels_db=[1.0, 2.0, 3.0],
        )
        widget.plot_item.vb.setXRange(0.5, 1.5, padding=0.0)

        visible = widget.export_csv_table(scope="visible")
        assert [row[0] for row in visible.rows] == ["250"]
    finally:
        widget.close()
