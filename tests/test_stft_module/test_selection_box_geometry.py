from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from app.services_container import FactoryReviewWindow

from ._support import (
    _assert_box_matches_plot_bounds,
    _assert_selection_box_matches_selected_window,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _make_stft_frame,
    _seed_stft_history,
    _show,
)


def test_selection_box_left_edge_aligns_to_exact_plotted_zero_time_position(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 3.0)},
        ),
    )
    qapp.processEvents()

    heatmap_rect = module.canvas.heatmap.geometry()
    left_x = module.canvas.heatmap.widget_x_for_time_sec(0.0)

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert left_x is not None
    assert module.selection_box.geometry().left() == pytest.approx(left_x, abs=1.0)


def test_selection_box_right_edge_aligns_to_exact_plotted_window_end_position(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 3.0)},
        ),
    )
    qapp.processEvents()

    heatmap_rect = module.canvas.heatmap.geometry()
    right_x = module.canvas.heatmap.widget_x_for_time_sec(3.0)

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert right_x is not None
    assert module.selection_box.geometry().right() == pytest.approx(right_x, abs=1.0)


def test_selection_box_height_matches_plot_area_and_does_not_extend_below_it(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0, 4.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=4.0),
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 4.0)},
        ),
    )
    qapp.processEvents()

    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    box_rect = module.selection_box.geometry()

    assert plot_rect is not None
    assert box_rect.top() == plot_rect.top()
    assert box_rect.height() == plot_rect.height()
    assert box_rect.bottom() == plot_rect.bottom()


def test_selection_box_rendered_width_matches_exact_mapped_time_span_within_frame_tolerance(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    rendered_times = (0.0, 0.5, 1.0)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=rendered_times,
        magnitudes=((1.0, 2.0), (3.0, 4.0), (5.0, 6.0)),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=1.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 1.0)},
        ),
    )
    qapp.processEvents()

    heatmap_rect = module.canvas.heatmap.geometry()
    rendered_sample_x = [
        module.canvas.heatmap.widget_x_for_time_sec(time_sec)
        for time_sec in rendered_times
    ]
    box_rect = module.selection_box.geometry()

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert all(sample_x is not None for sample_x in rendered_sample_x)

    left_x = rendered_sample_x[0]
    right_x = rendered_sample_x[-1]
    assert left_x is not None
    assert right_x is not None

    consecutive_frame_px = [
        right_sample_x - left_sample_x
        for left_sample_x, right_sample_x in zip(
            rendered_sample_x,
            rendered_sample_x[1:],
        )
        if left_sample_x is not None
        and right_sample_x is not None
        and right_sample_x > left_sample_x
    ]
    frame_px = min(delta for delta in consecutive_frame_px if delta > 0.0)

    assert abs(box_rect.left() - left_x) <= frame_px
    assert abs(box_rect.right() - right_x) <= frame_px


def test_single_sample_stft_selection_box_uses_one_frame_tolerance_formula(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(2.0,),
        magnitudes=((1.0, 2.0),),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=2.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (2.0, 3.0)},
        ),
    )
    qapp.processEvents()

    heatmap_rect = module.canvas.heatmap.geometry()
    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    time_extent = module.canvas.heatmap.rendered_time_extent_sec()
    box_rect = module.selection_box.geometry()

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert plot_rect is not None
    assert time_extent is not None

    assert module.selected_window == pytest.approx(time_extent)
    left_x = module.canvas.heatmap.widget_x_for_time_sec(time_extent[0])
    right_x = module.canvas.heatmap.widget_x_for_time_sec(time_extent[1])

    assert left_x is not None
    assert right_x is not None
    assert float(plot_rect.left()) - 1.0 <= left_x <= float(plot_rect.right()) + 1.0
    assert float(plot_rect.left()) - 1.0 <= right_x <= float(plot_rect.right()) + 1.0

    assert box_rect.left() == pytest.approx(left_x, abs=1.0)
    assert box_rect.right() == pytest.approx(right_x, abs=1.0)
    _assert_box_matches_plot_bounds(module)


def test_move_selection_to_track_x_uses_chart_inverse_mapping_for_visual_and_logical_alignment(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    heatmap_rect = module.canvas.heatmap.geometry()
    target_x = module.canvas.heatmap.widget_x_for_time_sec(1.0)

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert target_x is not None
    assert module.move_selection_to_track_x(target_x) is True
    assert module.selected_window == pytest.approx((1.0, 3.0))
    _assert_selection_box_matches_selected_window(module)


def test_move_selection_to_track_x_clamps_to_captured_duration_when_capture_is_tighter_than_plot_extent(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0, 4.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=4.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()

    assert plot_rect is not None
    assert module.move_selection_to_track_x(plot_rect.right() + 100.0) is True
    assert module.selected_window == pytest.approx((2.0, 4.0))


def test_move_selection_to_track_x_clamps_to_plot_start_when_plot_starts_after_zero(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(2.0, 3.0, 4.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=2.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (2.0, 3.0)},
        ),
    )
    qapp.processEvents()

    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()

    assert plot_rect is not None
    assert module.move_selection_to_track_x(plot_rect.left() - 100.0) is True
    assert module.selected_window == pytest.approx((1.5, 2.5))
    _assert_selection_box_matches_selected_window(module)


def test_move_selection_to_track_x_clamps_to_zero_start_on_zero_based_plot(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()

    assert plot_rect is not None
    assert module.move_selection_to_track_x(plot_rect.left() - 100.0) is True
    assert module.selected_window == (0.0, 2.0)
    _assert_selection_box_matches_selected_window(module)


def test_move_selection_to_start_rerenders_box_to_exact_chart_mapped_positions(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    assert module.move_selection_to_start(1.0) is True
    assert module.selected_window == pytest.approx((1.0, 3.0))
    _assert_selection_box_matches_selected_window(module)


def test_move_selection_to_start_clamps_to_captured_plot_intersection(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    _assert_selection_box_matches_selected_window(module)
    assert module.move_selection_to_start(9.0) is True
    assert module.selected_window == pytest.approx((1.5, 3.5))
    _assert_selection_box_matches_selected_window(module)


def test_preseeded_selection_box_render_clamps_to_latest_valid_start_inside_captured_plot_intersection(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=2.5, end_sec=4.5),
            judgment_payload={"overall": "合格", "analysis_window": (2.5, 4.5)},
        ),
    )
    qapp.processEvents()

    assert module.selected_window == pytest.approx((1.5, 3.5))
    _assert_selection_box_matches_selected_window(module)


def test_selection_box_hides_when_no_full_duration_interval_exists_inside_captured_plot_intersection(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    assert module.selection_box.isVisible() is False
    assert module.move_selection_to_track_x(999.0) is False


def test_dragging_passive_selection_box_does_not_move_selected_window(qapp):
    class _MouseEventStub:
        def __init__(
            self,
            *,
            global_x: float,
            button=Qt.MouseButton.NoButton,
        ) -> None:
            self._global_x = float(global_x)
            self._button = button
            self.accepted = False

        def button(self):
            return self._button

        def globalPosition(self):
            return QPointF(self._global_x, 0.0)

        def accept(self) -> None:
            self.accepted = True

    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0, 3.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 2.0)},
        ),
    )
    qapp.processEvents()

    heatmap_rect = module.canvas.heatmap.geometry()
    target_left_x = module.canvas.heatmap.widget_x_for_time_sec(1.0)
    box_rect = module.selection_box.geometry()

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert target_left_x is not None

    drag_offset = float(box_rect.center().x() - box_rect.left())
    press_global_x = float(module.canvas.mapToGlobal(box_rect.center()).x())
    move_global_x = float(
        module.canvas.mapToGlobal(
            QPoint(int(round(target_left_x + drag_offset)), box_rect.center().y())
        ).x()
    )

    press_event = _MouseEventStub(
        global_x=press_global_x,
        button=Qt.MouseButton.LeftButton,
    )
    move_event = _MouseEventStub(global_x=move_global_x)
    release_event = _MouseEventStub(global_x=move_global_x)

    module.selection_box.mousePressEvent(press_event)
    module.selection_box.mouseMoveEvent(move_event)
    module.selection_box.mouseReleaseEvent(release_event)

    assert press_event.accepted is True
    assert move_event.accepted is True
    assert release_event.accepted is True
    assert module.selected_window == pytest.approx((0.0, 2.0), abs=0.01)
    _assert_selection_box_matches_selected_window(module)


def test_selection_box_hides_when_rendered_payload_is_unavailable_and_direct_move_is_noop(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()
    module.analysis_select_button.click()
    qapp.processEvents()

    selected_before = module.selected_window
    assert module.selection_box.isVisible() is True

    module.canvas.heatmap.clear_data()
    module._update_overlay_geometry()

    assert module.selected_window == pytest.approx(selected_before)
    assert module.selection_box.isVisible() is False
    assert module.move_selection_to_start(0.5) is False
    assert module.selection_box.isVisible() is False
    assert module.selected_window == pytest.approx(selected_before)


def test_selection_box_hides_when_geometry_helper_reports_no_plot_rect_and_drag_is_noop(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()
    module.analysis_select_button.click()
    qapp.processEvents()

    selected_before = module.selected_window
    assert module.selection_box.isVisible() is True

    monkeypatch.setattr(module, "_current_overlay_geometry", lambda: None)
    module._update_overlay_geometry()

    assert module.selected_window == pytest.approx(selected_before)
    assert module.selection_box.isVisible() is False
    assert module.move_selection_to_track_x(250.0) is False
    assert module.selected_window == pytest.approx(selected_before)


def test_move_selection_to_start_is_noop_when_geometry_is_unavailable(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()
    module.analysis_select_button.click()
    qapp.processEvents()

    selected_before = module.selected_window
    assert module.selection_box.isVisible() is True

    monkeypatch.setattr(module, "_current_overlay_geometry", lambda: None)

    assert module.move_selection_to_start(0.25) is False
    assert module.selected_window == pytest.approx(selected_before)


def test_selection_box_hides_when_plot_widget_is_hidden_and_recovers_after_show(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.5, end_sec=1.5),
            judgment_payload={"overall": "合格", "analysis_window": (0.5, 1.5)},
        ),
    )
    qapp.processEvents()
    module.analysis_select_button.click()
    qapp.processEvents()

    selected_before = module.selected_window
    geometry_before = module.selection_box.geometry()
    assert module.selection_box.isVisible() is True

    module.canvas.heatmap.plot_widget.hide()
    qapp.processEvents()
    module._update_overlay_geometry()

    assert module.selection_box.isVisible() is False
    assert module.move_selection_to_track_x(250.0) is False
    assert module.selected_window == pytest.approx(selected_before)

    module.canvas.heatmap.plot_widget.show()
    qapp.processEvents()
    module._update_overlay_geometry()

    assert module.selection_box.isVisible() is False
    assert module.analysis_select_button.isVisible() is True
    assert module.analysis_select_button.isEnabled() is True

    module.analysis_select_button.click()
    qapp.processEvents()

    assert module.selection_box.isVisible() is True
    assert module.selection_box.geometry() == geometry_before
    assert module.selected_window == pytest.approx(selected_before)
    _assert_box_matches_plot_bounds(module)


def test_selection_box_rerenders_same_selected_window_after_geometry_returns(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 1.0, 2.0),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.5, end_sec=1.5),
            judgment_payload={"overall": "合格", "analysis_window": (0.5, 1.5)},
        ),
    )
    qapp.processEvents()
    module.analysis_select_button.click()
    qapp.processEvents()

    selected_before = module.selected_window
    assert module.selection_box.isVisible() is True

    module.canvas.heatmap.clear_data()
    module._update_overlay_geometry()

    assert module.selection_box.isVisible() is False

    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=3.0, magnitudes=(7.0, 8.0)),
    )
    qapp.processEvents()

    assert module.selected_window == pytest.approx(selected_before)
    assert module.selection_box.isVisible() is False

    module.analysis_select_button.click()
    qapp.processEvents()

    assert module.selection_box.isVisible() is True
    _assert_box_matches_plot_bounds(module)


def _viewport_pos_for_time(module, time_sec: float) -> QPoint:
    heatmap = module.canvas.heatmap
    plot_rect = heatmap.plot_area_rect_in_widget()
    widget_x = heatmap.widget_x_for_time_sec(time_sec)
    assert plot_rect is not None
    assert widget_x is not None
    widget_pos = QPoint(int(round(widget_x)), plot_rect.center().y())
    return heatmap.plot_widget.viewport().mapFrom(heatmap, widget_pos)


def _send_analysis_drag(module, start_sec: float, end_sec: float) -> None:
    viewport = module.canvas.heatmap.plot_widget.viewport()
    start = _viewport_pos_for_time(module, start_sec)
    end = _viewport_pos_for_time(module, end_sec)
    events = [
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(float(start.x()), float(start.y())),
            QPointF(viewport.mapToGlobal(start)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(float(end.x()), float(end.y())),
            QPointF(viewport.mapToGlobal(end)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(float(end.x()), float(end.y())),
            QPointF(viewport.mapToGlobal(end)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    ]
    for event in events:
        QApplication.sendEvent(viewport, event)


def test_analysis_selection_drag_creates_arbitrary_duration_window(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()

    module.analysis_select_button.click()
    _send_analysis_drag(module, 1.0, 1.35)
    qapp.processEvents()

    assert module.selected_window == pytest.approx((1.0, 1.35), abs=0.01)
    _assert_selection_box_matches_selected_window(module)

    module.analyze_button.click()
    services.factory_mode.begin_reanalysis.assert_called_once_with(
        pytest.approx(1.0, abs=0.01),
        pytest.approx(1.35, abs=0.01),
    )


def test_analysis_selection_short_drag_expands_to_minimum_duration(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()

    module.analysis_select_button.click()
    _send_analysis_drag(module, 1.0, 1.02)
    qapp.processEvents()

    assert module.selected_window == pytest.approx((1.0, 1.1), abs=0.01)
    _assert_selection_box_matches_selected_window(module)


def test_selection_box_reflows_when_stft_view_range_changes(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=1.5),
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 1.5)},
        ),
    )
    qapp.processEvents()

    module.analysis_select_button.click()
    qapp.processEvents()
    before = module.selection_box.geometry()

    module.canvas.heatmap.plot_item.vb.setXRange(0.5, 2.0, padding=0.0)
    qapp.processEvents()

    assert module.selection_box.geometry() != before
    _assert_selection_box_matches_selected_window(module)


def test_analysis_selection_short_drag_clamps_minimum_duration_at_capture_end(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()

    module.analysis_select_button.click()
    _send_analysis_drag(module, 4.98, 5.0)
    qapp.processEvents()

    assert module.selected_window == pytest.approx((4.9, 5.0), abs=0.01)
    _assert_selection_box_matches_selected_window(module)


def test_analysis_selection_drag_starting_on_existing_selection_replaces_window(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=5.0,
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
        ),
    )
    qapp.processEvents()

    module.analysis_select_button.click()
    heatmap_start = QPoint(
        int(round(module.canvas.heatmap.widget_x_for_time_sec(2.0))),
        module.selection_box.geometry().center().y(),
    )
    heatmap_end = QPoint(
        int(round(module.canvas.heatmap.widget_x_for_time_sec(2.4))),
        module.selection_box.geometry().center().y(),
    )
    start_on_box = module.selection_box.mapFromGlobal(
        module.canvas.heatmap.mapToGlobal(heatmap_start)
    )
    end_on_box = module.selection_box.mapFromGlobal(
        module.canvas.heatmap.mapToGlobal(heatmap_end)
    )
    events = [
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(float(start_on_box.x()), float(start_on_box.y())),
            QPointF(module.selection_box.mapToGlobal(start_on_box)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(float(end_on_box.x()), float(end_on_box.y())),
            QPointF(module.selection_box.mapToGlobal(end_on_box)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(float(end_on_box.x()), float(end_on_box.y())),
            QPointF(module.selection_box.mapToGlobal(end_on_box)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    ]
    for event in events:
        QApplication.sendEvent(module.selection_box, event)

    assert module.selected_window == pytest.approx((2.0, 2.4), abs=0.01)
    _assert_selection_box_matches_selected_window(module)
