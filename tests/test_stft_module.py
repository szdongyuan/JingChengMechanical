from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtWidgets import QApplication

from app.service.analysis.frames import StftFrame
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
from ui.modules.stft import StftModule


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
        self.stft_updated = _SignalStub()
        self.factory_state_changed = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_module():
    bus = _BusStub()
    factory_mode = SimpleNamespace(
        state=FactoryControllerState(),
        review_payload=FactoryReviewPayload(),
        begin_reanalysis=MagicMock(return_value=True),
    )
    services = SimpleNamespace(
        bus=bus,
        factory_mode=factory_mode,
    )
    return StftModule(services), services


def _show(module, qapp):
    module.resize(640, 260)
    module.show()
    qapp.processEvents()


def _emit_reviewing(module, services, payload: FactoryReviewPayload) -> None:
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=payload.displayed_config_id,
            run_phase="reviewing",
            trigger_source=payload.trigger_source,
        )
    )
    services.bus.factory_review_changed.emit(payload)


def _emit_phase(
    services,
    payload: FactoryReviewPayload,
    *,
    run_phase: str,
    ) -> None:
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=payload.displayed_config_id,
            run_phase=run_phase,
            trigger_source=payload.trigger_source,
        )
    )


def _make_stft_frame(
    *,
    time: float,
    magnitudes: tuple[float, ...],
    frequencies: tuple[float, ...] = (100.0, 200.0),
) -> StftFrame:
    return StftFrame(
        time=time,
        frequencies=np.asarray(frequencies, dtype=float),
        magnitudes=np.asarray(magnitudes, dtype=float),
    )


def _make_review_payload(
    *,
    displayed_config_id: int = 101,
    run_id: str | None = None,
    captured_duration_sec: float = 5.0,
    default_window: FactoryReviewWindow | None = None,
    fallback_window: FactoryReviewWindow | None = None,
    manual_reanalysis_allowed: bool = True,
    no_result: bool = False,
    error_message: str | None = None,
    judgment_payload: dict | None = None,
    summary_payload: dict | None = None,
    chart_payload: dict | None = None,
) -> FactoryReviewPayload:
    if default_window is None:
        default_window = FactoryReviewWindow(start_sec=1.0, end_sec=3.0)
    return FactoryReviewPayload(
        displayed_config_id=displayed_config_id,
        run_id=run_id or f"run-{displayed_config_id}",
        trigger_source="manual",
        captured_duration_sec=captured_duration_sec,
        default_window=default_window,
        fallback_window=fallback_window,
        manual_reanalysis_allowed=manual_reanalysis_allowed,
        no_result=no_result,
        error_message=error_message,
        judgment_payload=judgment_payload,
        summary_payload=summary_payload,
        chart_payload=chart_payload,
    )


def _seed_stft_history(
    module,
    services,
    qapp,
    *,
    displayed_config_id: int = 101,
    run_id: str | None = None,
    channel_index: int = 0,
    times: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0),
    magnitudes: tuple[tuple[float, ...], ...] | None = None,
) -> None:
    if run_id is None:
        run_id = f"run-{displayed_config_id}"
    if magnitudes is None:
        magnitudes = tuple(
            (float((index * 2) + 1), float((index * 2) + 2))
            for index in range(len(times))
        )

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=displayed_config_id,
            run_phase="running",
            trigger_source="manual",
        )
    )
    for time, magnitude_values in zip(times, magnitudes):
        services.bus.stft_updated.emit(
            displayed_config_id,
            run_id,
            channel_index,
            _make_stft_frame(time=time, magnitudes=magnitude_values),
        )
    qapp.processEvents()


def _assert_box_matches_plot_bounds(module) -> None:
    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    box_rect = module.selection_box.geometry()

    assert plot_rect is not None
    assert box_rect.left() >= plot_rect.left()
    assert box_rect.right() <= plot_rect.right()
    assert box_rect.top() == plot_rect.top()
    assert box_rect.bottom() == plot_rect.bottom()


def _assert_selection_box_matches_selected_window(
    module,
    *,
    tolerance_px: int = 1,
) -> None:
    selected_window = module.selected_window
    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    box_rect = module.selection_box.geometry()
    heatmap_rect = module.canvas.heatmap.geometry()

    assert selected_window is not None
    assert plot_rect is not None
    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert heatmap_rect.size() == module.canvas.rect().size()

    start_sec, end_sec = selected_window
    expected_left = module.canvas.heatmap.widget_x_for_time_sec(start_sec)
    expected_right = module.canvas.heatmap.widget_x_for_time_sec(end_sec)

    assert expected_left is not None
    assert expected_right is not None
    assert box_rect.left() == pytest.approx(expected_left, abs=tolerance_px)
    assert box_rect.right() == pytest.approx(expected_right, abs=tolerance_px)
    assert box_rect.top() == plot_rect.top()
    assert box_rect.bottom() == plot_rect.bottom()


def test_stft_module_keeps_live_frames_visible_in_review_without_chart_payload(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 1
    _show(module, qapp)

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="running",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            judgment_payload={"overall": "合格"},
            summary_payload={"overall_status": "pass", "rows": []},
            chart_payload=None,
        ),
    )

    assert module.has_cached_run_data("run-101", channel_index=1) is True
    assert module.current_render_cache_key() == (101, "run-101", 1)
    payload = module.current_image_payload()
    assert payload is not None
    assert payload["times_sec"] == [0.0, 0.5]
    assert payload["frequencies_hz"] == [100.0, 200.0]
    assert np.array_equal(
        payload["magnitude_db"],
        np.array([[1.0, 3.0], [2.0, 4.0]]),
    )


def test_stft_module_keeps_selected_cached_channel_visible_during_analyzing_without_committed_chart_payload(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 1
    _show(module, qapp)

    payload = _make_review_payload(
        run_id="run-101",
        judgment_payload=None,
        summary_payload=None,
        chart_payload=None,
    )

    _emit_phase(services, payload, run_phase="running")
    services.bus.stft_updated.emit(
        101,
        "run-101",
        1,
        _make_stft_frame(time=0.25, magnitudes=(6.0, 7.0)),
    )

    _emit_phase(services, payload, run_phase="analyzing")

    assert module.current_render_cache_key() == (101, "run-101", 1)
    payload = module.current_image_payload()
    assert payload is not None
    assert payload["times_sec"] == [0.25]
    assert np.array_equal(payload["magnitude_db"], np.array([[6.0], [7.0]]))


def test_stft_module_clears_when_selected_channel_has_no_cached_frames_yet(qapp):
    module, services = _make_module()
    selected_channel = 0
    module.selected_display_channel_provider = lambda: selected_channel
    _show(module, qapp)

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="running",
            trigger_source="manual",
        )
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )

    assert module.current_render_cache_key() == (101, "run-101", 0)
    assert module.current_image_payload() is not None

    selected_channel = 1
    module.refresh_selected_display_channel()

    assert module.current_render_cache_key() is None
    assert module.current_image_payload() is None


def test_review_success_shows_analysis_box_from_automatic_window_with_fixed_duration(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            judgment_payload={
                "overall": "合格",
                "analysis_window": (1.0, 3.0),
            }
        ),
    )

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.selected_window == pytest.approx((1.0, 3.0))

    module.move_selection_to_start(2.25)

    assert module.selected_window == pytest.approx((2.25, 4.25))
    assert module.selected_window[1] - module.selected_window[0] == pytest.approx(2.0)
    _assert_selection_box_matches_selected_window(module)


def test_auto_analysis_failure_uses_first_valid_manual_window_when_reanalysis_is_allowed(qapp):
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
            no_result=True,
            error_message="analysis window not fully captured",
            default_window=FactoryReviewWindow(start_sec=4.0, end_sec=6.0),
            fallback_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            manual_reanalysis_allowed=True,
            judgment_payload=None,
        ),
    )

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.selected_window == pytest.approx((0.0, 2.0))


def test_auto_analysis_failure_without_full_valid_window_hides_reanalysis_controls_and_blocks_requests(
    qapp,
):
    module, services = _make_module()
    _show(module, qapp)

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            captured_duration_sec=1.5,
            no_result=True,
            error_message="analysis window not fully captured",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
            fallback_window=None,
            manual_reanalysis_allowed=False,
            judgment_payload=None,
        ),
    )

    assert module.selected_window is None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analyze_button.isEnabled() is False

    module.analyze_button.click()

    services.factory_mode.begin_reanalysis.assert_not_called()


@pytest.mark.parametrize(
    ("run_phase", "box_visible", "button_visible", "button_enabled"),
    [
        ("not_ready", False, False, False),
        ("armed", False, False, False),
        ("running", False, False, False),
        ("analyzing", False, False, False),
        ("reviewing", True, True, True),
        ("reanalyzing", True, True, False),
    ],
)
def test_analysis_controls_follow_review_only_visibility_rule(
    qapp,
    run_phase,
    box_visible,
    button_visible,
    button_enabled,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    payload = _make_review_payload(
        judgment_payload={
            "overall": "合格",
            "analysis_window": (1.0, 3.0),
        }
    )

    _emit_phase(services, payload, run_phase=run_phase)
    services.bus.factory_review_changed.emit(payload)

    assert module.selected_window == pytest.approx((1.0, 3.0))
    assert module.selection_box.isVisible() is box_visible
    assert module.analyze_button.isVisible() is button_visible
    assert module.analyze_button.isEnabled() is button_enabled


def test_analysis_action_clamps_selection_to_audio_bounds_without_local_reset_hacks(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            judgment_payload={
                "overall": "合格",
                "analysis_window": (0.0, 2.0),
            }
        ),
    )
    review_event_count = len(services.bus.factory_review_changed.emissions)
    reset_event_count = len(services.bus.factory_display_reset.emissions)

    module.move_selection_to_start(9.0)
    module.analyze_button.click()

    assert module.selected_window == pytest.approx((3.0, 5.0))
    services.factory_mode.begin_reanalysis.assert_called_once_with(3.0, 5.0)
    assert len(services.bus.factory_review_changed.emissions) == review_event_count
    assert len(services.bus.factory_display_reset.emissions) == reset_event_count


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
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 0.5, 1.0),
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
    left_x = module.canvas.heatmap.widget_x_for_time_sec(0.0)
    frame_step_x = module.canvas.heatmap.widget_x_for_time_sec(0.25)
    right_x = module.canvas.heatmap.widget_x_for_time_sec(1.0)
    box_rect = module.selection_box.geometry()

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert left_x is not None
    assert frame_step_x is not None
    assert right_x is not None

    consecutive_frame_px = [
        frame_step_x - left_x,
        right_x - frame_step_x,
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
    time_extent = module.canvas.heatmap.rendered_time_extent_sec()
    left_x = module.canvas.heatmap.widget_x_for_time_sec(2.0)
    box_rect = module.selection_box.geometry()

    assert heatmap_rect.x() == 0
    assert heatmap_rect.y() == 0
    assert time_extent is not None

    right_x = module.canvas.heatmap.widget_x_for_time_sec(time_extent[1])

    assert left_x is not None
    assert right_x is not None

    frame_px = right_x - left_x
    assert abs(box_rect.left() - left_x) <= frame_px
    assert abs(box_rect.right() - right_x) <= frame_px


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
    assert module.selected_window == pytest.approx((2.0, 3.0))
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
    assert module.selected_window == pytest.approx((1.0, 3.0))
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

    assert module.selected_window == pytest.approx((1.0, 3.0))
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


def test_dragging_selection_box_uses_chart_mapping_for_visual_and_logical_alignment(qapp):
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
    assert module.selected_window == pytest.approx((1.0, 3.0))
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
    qapp, monkeypatch
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

    selected_before = module.selected_window
    assert module.selection_box.isVisible() is True

    monkeypatch.setattr(module, "_current_overlay_geometry", lambda: None)
    module._update_overlay_geometry()

    assert module.selected_window == pytest.approx(selected_before)
    assert module.selection_box.isVisible() is False
    assert module.move_selection_to_track_x(250.0) is False
    assert module.selected_window == pytest.approx(selected_before)


def test_move_selection_to_start_is_noop_when_geometry_is_unavailable(
    qapp, monkeypatch
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
    assert module.selection_box.isVisible() is True
    _assert_box_matches_plot_bounds(module)


def test_clicking_analysis_preserves_selection_when_controller_clears_review_then_enters_reanalysis(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    payload = _make_review_payload(
        judgment_payload={
            "overall": "合格",
            "analysis_window": (1.0, 3.0),
        }
    )

    _emit_reviewing(module, services, payload)
    module.move_selection_to_start(1.25)
    reset_event_count = len(services.bus.factory_display_reset.emissions)

    def _controller_begin_reanalysis(start_sec: float, end_sec: float) -> bool:
        assert (start_sec, end_sec) == pytest.approx((1.25, 3.25))
        services.bus.factory_review_changed.emit(
            _make_review_payload(
                judgment_payload=None,
            )
        )

        assert module.selection_box.isVisible() is True
        assert module.analyze_button.isVisible() is True
        assert module.analyze_button.isEnabled() is True
        assert module.selected_window == pytest.approx((1.25, 3.25))

        _emit_phase(services, payload, run_phase="reanalyzing")
        return True

    services.factory_mode.begin_reanalysis.side_effect = _controller_begin_reanalysis

    module.analyze_button.click()

    services.factory_mode.begin_reanalysis.assert_called_once_with(1.25, 3.25)
    assert len(services.bus.factory_display_reset.emissions) == reset_event_count
    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.analyze_button.isEnabled() is False
    assert module.selected_window == pytest.approx((1.25, 3.25))


def test_reanalysis_failure_keeps_box_visible_and_locked_until_reviewing_unlocks_again(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            judgment_payload={
                "overall": "合格",
                "analysis_window": (1.0, 3.0),
            }
        ),
    )
    module.move_selection_to_start(1.25)
    assert module.selected_window == pytest.approx((1.25, 3.25))

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reanalyzing",
            trigger_source="manual",
        )
    )

    module.move_selection_to_start(0.25)

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.analyze_button.isEnabled() is False
    assert module.selected_window == pytest.approx((1.25, 3.25))

    completion_payload = _make_review_payload(
        no_result=True,
        error_message="manual reanalysis failed",
        judgment_payload=None,
    )

    services.bus.factory_review_changed.emit(completion_payload)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isEnabled() is True
    assert module.selected_window == pytest.approx((1.25, 3.25))

    module.move_selection_to_start(0.25)

    assert module.selected_window == pytest.approx((0.25, 2.25))


def test_reanalysis_success_payload_reseeds_selection_from_controller_window_after_unlock(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            judgment_payload={
                "overall": "合格",
                "analysis_window": (1.0, 3.0),
            }
        ),
    )
    module.move_selection_to_start(1.25)
    assert module.selected_window == pytest.approx((1.25, 3.25))

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reanalyzing",
            trigger_source="manual",
        )
    )

    completion_payload = _make_review_payload(
        default_window=FactoryReviewWindow(start_sec=0.5, end_sec=2.5),
        judgment_payload={
            "overall": "合格",
            "analysis_window": (0.5, 2.5),
        },
    )

    services.bus.factory_review_changed.emit(completion_payload)

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.analyze_button.isEnabled() is False

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isEnabled() is True
    assert module.selected_window == pytest.approx((0.5, 2.5))


def test_display_reset_clears_selection_and_hides_manual_controls(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            judgment_payload={
                "overall": "合格",
                "analysis_window": (1.0, 3.0),
            }
        ),
    )
    assert module.selection_box.isVisible() is True

    services.bus.factory_display_reset.emit("next_run_accepted")

    assert module.selected_window is None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
