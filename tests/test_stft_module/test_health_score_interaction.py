from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from app.services_container import FactoryControllerState

from ._support import (
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _seed_stft_history,
    _show,
)


def _score_payload(
    *,
    times: tuple[float, ...] = (1.0, 2.0, 3.0),
    scores: tuple[float, ...] = (91.0, 82.0, 73.0),
) -> dict:
    return {
        "enabled": True,
        "summary_score": 90.0,
        "times_sec": list(times),
        "scores": list(scores),
    }


def _current_trace_payload(
    *,
    times: tuple[float, ...] = (1.0, 3.0),
    values: tuple[float, ...] = (1.0, 3.0),
    unit: str = "mA",
) -> dict:
    return {
        "times_sec": list(times),
        "values_a": list(values),
        "average_a": sum(values) / len(values),
        "max_a": max(values),
        "latest_a": values[-1],
        "unit": unit,
    }


def _score_text(score: float, current: str = "--") -> str:
    return f"得分：{score:.2f}\n电流：{current}"


def _make_scored_review(
    score_payloads: dict[int, dict],
    *,
    current_traces: dict[int, dict] | None = None,
    displayed_config_id: int = 101,
    run_id: str = "run-101",
):
    chart_payload = {}
    for channel_index, score_payload in score_payloads.items():
        channel_payload = {
            "stft_score": score_payload,
            "stft": {
                "times_sec": list(score_payload.get("times_sec", [])),
                "frequencies_hz": [100.0, 200.0],
                "magnitude_db": np.ones((2, len(score_payload.get("times_sec", [])))),
            },
        }
        if current_traces is not None and channel_index in current_traces:
            channel_payload["current_trace"] = current_traces[channel_index]
        chart_payload[channel_index] = channel_payload
    return _make_review_payload(
        displayed_config_id=displayed_config_id,
        run_id=run_id,
        chart_payload=chart_payload,
        judgment_payload={"overall": "合格"},
    )


def _prepare_scored_module(
    qapp,
    *,
    selected_channel: int = 0,
    score_payloads: dict[int, dict] | None = None,
    current_traces: dict[int, dict] | None = None,
    seed_channels: tuple[int, ...] = (0,),
):
    module, services = _make_module()
    selected = {"value": selected_channel}
    module.selected_display_channel_provider = lambda: selected["value"]
    _show(module, qapp)
    for channel_index in seed_channels:
        _seed_stft_history(
            module,
            services,
            qapp,
            channel_index=channel_index,
            times=(0.0, 1.0, 2.0, 3.0, 4.0),
        )
    _emit_reviewing(
        module,
        services,
        _make_scored_review(
            score_payloads or {0: _score_payload()},
            current_traces=current_traces,
        ),
    )
    qapp.processEvents()
    return module, services, selected


def _score_overlay(module) -> QLabel:
    label = module.canvas.findChild(QLabel, "StftScoreOverlayLabel")
    assert label is not None
    return label


def _viewport_pos_for_time(module, time_sec: float) -> QPoint:
    heatmap = module.canvas.heatmap
    plot_rect = heatmap.plot_area_rect_in_widget()
    widget_x = heatmap.widget_x_for_time_sec(time_sec)
    assert plot_rect is not None
    assert widget_x is not None
    widget_pos = QPoint(int(round(widget_x)), plot_rect.center().y())
    return heatmap.plot_widget.viewport().mapFrom(heatmap, widget_pos)


def _send_mouse_move_to_time(module, time_sec: float) -> None:
    viewport = module.canvas.heatmap.plot_widget.viewport()
    position = _viewport_pos_for_time(module, time_sec)
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(float(position.x()), float(position.y())),
        QPointF(viewport.mapToGlobal(position)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, event)


def test_hovering_inside_plot_resolves_nearest_score_time(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    _send_mouse_move_to_time(module, 2.1)
    qapp.processEvents()

    assert module.hover_score_text() == _score_text(82.0)
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is True
    assert _score_overlay(module).text() == _score_text(82.0)


def test_hover_score_overlay_includes_current_at_score_time(qapp):
    module, _services, _selected = _prepare_scored_module(
        qapp,
        score_payloads={0: _score_payload()},
        current_traces={0: _current_trace_payload()},
    )

    _send_mouse_move_to_time(module, 2.1)
    qapp.processEvents()

    assert module.hover_score_text() == _score_text(82.0, "2.000 mA")
    assert _score_overlay(module).text() == _score_text(82.0, "2.000 mA")


def test_same_context_current_trace_replacement_refreshes_visible_score_overlay(qapp):
    module, services, _selected = _prepare_scored_module(
        qapp,
        score_payloads={0: _score_payload()},
        current_traces={0: _current_trace_payload()},
    )
    _send_mouse_move_to_time(module, 2.1)
    qapp.processEvents()

    assert module.hover_score_text() == _score_text(82.0, "2.000 mA")
    assert _score_overlay(module).text() == _score_text(82.0, "2.000 mA")

    services.bus.factory_review_changed.emit(
        _make_scored_review(
            {0: _score_payload()},
            current_traces={
                0: _current_trace_payload(values=(2.0, 4.0)),
            },
            displayed_config_id=101,
            run_id="run-101",
        )
    )
    qapp.processEvents()

    assert module.hover_score_text() == _score_text(82.0, "3.000 mA")
    assert _score_overlay(module).isVisible() is True
    assert _score_overlay(module).text() == _score_text(82.0, "3.000 mA")


def test_pinned_score_overlay_includes_current_at_score_time(qapp):
    module, _services, _selected = _prepare_scored_module(
        qapp,
        score_payloads={0: _score_payload()},
        current_traces={0: _current_trace_payload()},
    )

    QTest.mouseClick(
        module.canvas.heatmap.plot_widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _viewport_pos_for_time(module, 3.0),
    )
    qapp.processEvents()
    _send_mouse_move_to_time(module, 2.0)
    qapp.processEvents()

    assert module.pinned_score_text() == _score_text(73.0, "3.000 mA")
    assert module.hover_score_text() == _score_text(82.0, "2.000 mA")
    assert _score_overlay(module).text() == _score_text(73.0, "3.000 mA")


def test_score_text_keeps_score_when_time_lookup_is_unavailable(qapp):
    module, _services, _selected = _prepare_scored_module(
        qapp,
        score_payloads={
            0: {
                "enabled": True,
                "summary_score": 77.0,
                "scores": [88.0, 77.0],
            }
        },
        current_traces={0: _current_trace_payload()},
    )

    assert module._score_text_for_index(1) == _score_text(77.0)


def test_left_click_pins_score_and_hover_does_not_replace_pinned_overlay(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    QTest.mouseClick(
        module.canvas.heatmap.plot_widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _viewport_pos_for_time(module, 3.0),
    )
    qapp.processEvents()
    _send_mouse_move_to_time(module, 1.0)
    qapp.processEvents()

    assert module.pinned_score_text() == _score_text(73.0)
    assert module.hover_score_text() == _score_text(91.0)
    assert _score_overlay(module).text() == _score_text(73.0)


def test_hidden_selection_box_does_not_block_hover_score(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    assert module.selection_box.isVisible() is False

    _send_mouse_move_to_time(module, 2.1)
    qapp.processEvents()

    assert module.hover_score_text() == _score_text(82.0)
    assert module.pinned_score_text() is None
    assert _score_overlay(module).text() == _score_text(82.0)


def test_hidden_selection_box_does_not_block_click_score(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    QTest.mouseClick(
        module.canvas.heatmap.plot_widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _viewport_pos_for_time(module, 2.0),
    )
    qapp.processEvents()

    assert module.pinned_score_text() == _score_text(82.0)
    assert _score_overlay(module).text() == _score_text(82.0)


def test_hidden_selection_box_does_not_block_right_click_clear(qapp, monkeypatch):
    module, _services, _selected = _prepare_scored_module(qapp)
    module.canvas.heatmap.left_time_clicked.emit(2.0)
    menu_calls = []
    monkeypatch.setattr(
        module.canvas.heatmap.chart_interactions,
        "show_context_menu",
        lambda global_pos: menu_calls.append(global_pos),
    )
    qapp.processEvents()

    QTest.mouseClick(
        module.canvas.heatmap.plot_widget.viewport(),
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
        _viewport_pos_for_time(module, 2.0),
    )
    qapp.processEvents()

    assert module.pinned_score_text() is None
    assert module.hover_score_text() is None
    assert _score_overlay(module).isVisible() is False
    assert menu_calls


def test_same_context_clear_score_payload_hides_pinned_overlay(qapp):
    module, services, _selected = _prepare_scored_module(qapp)
    module.canvas.heatmap.left_time_clicked.emit(2.0)
    qapp.processEvents()

    assert module.pinned_score_text() == _score_text(82.0)
    assert _score_overlay(module).isVisible() is True

    services.bus.factory_review_changed.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id="run-101",
            chart_payload={},
        )
    )
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False


def test_same_context_replaced_score_payload_clears_pinned_overlay(qapp):
    module, services, _selected = _prepare_scored_module(qapp)
    module.canvas.heatmap.left_time_clicked.emit(2.0)
    qapp.processEvents()

    assert module.pinned_score_text() == _score_text(82.0)
    assert _score_overlay(module).text() == _score_text(82.0)

    services.bus.factory_review_changed.emit(
        _make_scored_review(
            {0: _score_payload(scores=(41.0, 52.0, 63.0))},
            displayed_config_id=101,
            run_id="run-101",
        )
    )
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False


def test_score_overlay_does_not_cover_review_selection_controls(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    module.canvas.heatmap.hover_time_changed.emit(2.0)
    qapp.processEvents()

    overlay = _score_overlay(module)
    overlay_rect = overlay.geometry()

    assert overlay.isVisible() is True
    guarded_controls = (
        module.analysis_select_button,
    )
    for control in guarded_controls:
        assert control.isVisible() is True
        assert overlay_rect.intersects(control.geometry()) is False


def test_right_click_and_escape_clear_pinned_score(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)
    module.canvas.heatmap.left_time_clicked.emit(2.0)
    qapp.processEvents()

    module.canvas.heatmap.right_clicked.emit()
    qapp.processEvents()

    assert module.pinned_score_text() is None
    assert module.hover_score_text() is None
    assert _score_overlay(module).isVisible() is False

    module.canvas.heatmap.left_time_clicked.emit(1.0)
    module.setFocus()
    QTest.keyClick(module, Qt.Key.Key_Escape)
    qapp.processEvents()

    assert module.pinned_score_text() is None
    assert module.hover_score_text() is None
    assert _score_overlay(module).isVisible() is False


def test_mouse_leave_clears_hover_but_keeps_pinned_score(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    module.canvas.heatmap.hover_time_changed.emit(1.0)
    module.canvas.heatmap.mouse_left_plot.emit()
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert _score_overlay(module).isVisible() is False

    module.canvas.heatmap.left_time_clicked.emit(2.0)
    module.canvas.heatmap.hover_time_changed.emit(1.0)
    module.canvas.heatmap.mouse_left_plot.emit()
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() == _score_text(82.0)
    assert _score_overlay(module).text() == _score_text(82.0)


def test_dragging_selection_box_does_not_pin_score(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(1.0, 2.0, 3.0),
    )
    payload = _make_review_payload(
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [1.0, 2.0, 3.0],
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.ones((2, 3)),
                },
                "stft_score": _score_payload(),
            }
        },
        judgment_payload={"overall": "合格"},
    )
    _emit_reviewing(module, services, payload)
    qapp.processEvents()

    selected_before = module.selected_window
    assert module.selection_box.isVisible() is False
    start = module.selection_box.rect().center()
    QTest.mousePress(
        module.selection_box,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        pos=start,
    )
    QTest.mouseMove(module.selection_box, start + QPoint(40, 0))
    QTest.mouseRelease(
        module.selection_box,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        pos=start + QPoint(40, 0),
    )
    qapp.processEvents()

    assert selected_before is not None
    assert module.selected_window is not None
    assert module.selected_window == pytest.approx(selected_before)
    assert module.pinned_score_text() is None


@pytest.mark.parametrize(
    "clear_action",
    [
        "display_reset",
        "data_clear",
        "config_switch",
        "run_switch",
    ],
)
def test_context_changes_clear_hover_and_pinned_score(qapp, clear_action):
    module, services, _selected = _prepare_scored_module(qapp)
    module.canvas.heatmap.hover_time_changed.emit(1.0)
    module.canvas.heatmap.left_time_clicked.emit(2.0)
    qapp.processEvents()

    if clear_action == "display_reset":
        services.bus.factory_display_reset.emit("reset")
    elif clear_action == "data_clear":
        module._clear_rendering()
    elif clear_action == "config_switch":
        services.bus.factory_state_changed.emit(
            FactoryControllerState(
                displayed_config_id=202,
                run_phase="reviewing",
                trigger_source="manual",
            )
        )
    elif clear_action == "run_switch":
        module._on_factory_runtime_context(
            SimpleNamespace(config_id=101, run_id="run-new")
        )
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False


def test_channel_switch_clears_state_and_loads_selected_channel_scores(qapp):
    module, _services, selected = _prepare_scored_module(
        qapp,
        score_payloads={
            0: _score_payload(scores=(91.0, 82.0, 73.0)),
            1: _score_payload(scores=(61.0, 52.0, 43.0)),
        },
        seed_channels=(0, 1),
    )
    module.canvas.heatmap.hover_time_changed.emit(1.0)
    module.canvas.heatmap.left_time_clicked.emit(2.0)
    qapp.processEvents()

    selected["value"] = 1
    module.refresh_selected_display_channel()
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False

    module.canvas.heatmap.hover_time_changed.emit(2.0)
    qapp.processEvents()

    assert module.hover_score_text() == _score_text(52.0)


def test_no_nearby_score_hides_hover_and_does_not_pin(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    module.canvas.heatmap.hover_time_changed.emit(3.9)
    module.canvas.heatmap.left_time_clicked.emit(3.9)
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False


def test_clicking_outside_reanalysis_score_range_does_not_pin(qapp):
    module, _services, _selected = _prepare_scored_module(
        qapp,
        score_payloads={0: _score_payload(times=(1.0, 2.0, 3.0))},
    )

    module.canvas.heatmap.hover_time_changed.emit(0.1)
    module.canvas.heatmap.left_time_clicked.emit(0.1)
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False


def test_analysis_selection_mode_suppresses_hover_and_click_score_inspection(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    module.analysis_select_button.click()
    assert module.analysis_select_button.text() == "取消框选"

    _send_mouse_move_to_time(module, 2.1)
    QTest.mouseClick(
        module.canvas.heatmap.plot_widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _viewport_pos_for_time(module, 2.0),
    )
    qapp.processEvents()

    assert module.hover_score_text() is None
    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False

    module.analysis_select_button.click()
    assert module.analysis_select_button.text() == "框选分析"

    QTest.mouseClick(
        module.canvas.heatmap.plot_widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _viewport_pos_for_time(module, 2.0),
    )
    qapp.processEvents()

    assert module.pinned_score_text() == _score_text(82.0)


def test_escape_clears_score_without_canceling_analysis_selection_mode(qapp):
    module, _services, _selected = _prepare_scored_module(qapp)

    module.canvas.heatmap.left_time_clicked.emit(2.0)
    qapp.processEvents()
    assert module.pinned_score_text() == _score_text(82.0)

    module.analysis_select_button.click()
    assert module.analysis_select_button.text() == "取消框选"

    module.setFocus()
    QTest.keyClick(module, Qt.Key.Key_Escape)
    qapp.processEvents()

    assert module.pinned_score_text() is None
    assert _score_overlay(module).isVisible() is False
    assert module.analysis_select_button.text() == "取消框选"
    assert module.analysis_select_button.isChecked() is True
    assert module.selection_box.isVisible() is True
