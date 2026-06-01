from __future__ import annotations

import pytest

from app.services_container import FactoryReviewWindow

from ._support import (
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _seed_stft_history,
    _show,
)


def _prepare_review_overlay(
    qapp,
    *,
    run_phase: str = "reviewing",
    manual_reanalysis_allowed: bool = True,
    can_replay: bool = True,
    selected_channel: int = 2,
    default_window: FactoryReviewWindow | None = None,
    enter_selection_mode: bool = True,
):
    module, services = _make_module()
    services.test.can_replay_selection.return_value = can_replay
    module.selected_display_channel_provider = lambda: selected_channel
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=202,
        channel_index=selected_channel,
        run_id="run-202",
    )
    payload = _make_review_payload(
        displayed_config_id=202,
        run_id="run-202",
        default_window=default_window
        or FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        manual_reanalysis_allowed=manual_reanalysis_allowed,
        judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
    )

    if run_phase == "reviewing":
        _emit_reviewing(module, services, payload)
    else:
        _emit_reviewing(module, services, payload)
        if enter_selection_mode and module.analysis_select_button.isEnabled():
            module.analysis_select_button.click()
        _emit_phase(services, payload, run_phase=run_phase)
    if (
        run_phase == "reviewing"
        and enter_selection_mode
        and module.analysis_select_button.isEnabled()
    ):
        module.analysis_select_button.click()
    qapp.processEvents()
    return module, services, payload


def test_replay_button_is_separate_from_analysis_button(qapp):
    module, _, _ = _prepare_review_overlay(qapp)

    assert module.replay_button is not module.analyze_button
    assert module.analyze_button.text() == "分析"
    assert module.replay_button.text() == "重放"


def test_replay_controls_are_hidden_before_selection_mode(qapp):
    module, _, _ = _prepare_review_overlay(qapp, enter_selection_mode=False)

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.replay_button.isVisible() is False
    assert module.analysis_select_button.isVisible() is True
    assert module.analysis_select_button.text() == "框选分析"


@pytest.mark.parametrize(
    ("run_phase", "expected_visible"),
    [
        ("reviewing", True),
        ("reanalyzing", False),
        ("stopped", False),
    ],
)
def test_replay_button_visibility_follows_review_selection_phases(
    qapp,
    run_phase,
    expected_visible,
):
    module, _, _ = _prepare_review_overlay(qapp, run_phase=run_phase)

    assert module.selection_box.isVisible() is expected_visible
    assert module.replay_button.isVisible() is expected_visible


def test_replay_button_is_hidden_when_selection_mode_is_unavailable(qapp):
    module, _, _ = _prepare_review_overlay(
        qapp,
        manual_reanalysis_allowed=False,
    )

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.replay_button.isVisible() is False


def test_replay_button_enabled_state_uses_service_availability(qapp):
    module, services, _ = _prepare_review_overlay(qapp, can_replay=False)

    assert module.replay_button.isVisible() is True
    assert module.replay_button.isEnabled() is False
    services.test.can_replay_selection.assert_called_with(
        202,
        "run-202",
        2,
        pytest.approx(1.0),
        pytest.approx(3.0),
    )


def test_clicking_replay_calls_replay_service_without_reanalysis(qapp):
    module, services, _ = _prepare_review_overlay(qapp)

    module.replay_button.click()

    services.test.replay_selection.assert_called_once_with(202, "run-202", 2, 1.0, 3.0)
    services.factory_mode.begin_reanalysis.assert_not_called()


def test_replay_click_returns_without_service_call_when_context_is_invalid(qapp):
    module, services = _make_module()
    _show(module, qapp)

    module._replay_selection()

    services.test.replay_selection.assert_not_called()
    services.factory_mode.begin_reanalysis.assert_not_called()


def test_no_replayable_buffer_keeps_replay_button_disabled(qapp):
    module, services, _ = _prepare_review_overlay(qapp, can_replay=False)

    module.replay_button.click()

    assert module.replay_button.isVisible() is True
    assert module.replay_button.isEnabled() is False
    services.test.replay_selection.assert_not_called()
    services.factory_mode.begin_reanalysis.assert_not_called()


def test_failed_replay_service_emits_concise_status_and_log(qapp):
    module, services, _ = _prepare_review_overlay(qapp)
    services.test.replay_selection.return_value = False

    module.replay_button.click()

    assert services.bus.status.emissions == [(("重放失败",), {})]
    assert services.bus.log.emissions == [(("重放失败",), {})]


def test_replay_button_geometry_does_not_overlap_analysis_button_or_selection_box(qapp):
    module, _, _ = _prepare_review_overlay(qapp)

    replay_rect = module.replay_button.geometry()
    analyze_rect = module.analyze_button.geometry()
    selection_rect = module.selection_box.geometry()

    assert module.replay_button.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert replay_rect.intersects(analyze_rect) is False
    assert replay_rect.intersects(selection_rect) is False
    assert analyze_rect.intersects(selection_rect) is False


def test_full_width_selection_keeps_controls_visible_inside_plot_when_sides_do_not_fit(
    qapp,
):
    module, services, _ = _prepare_review_overlay(
        qapp,
        default_window=FactoryReviewWindow(start_sec=0.0, end_sec=5.0),
    )

    selection_rect = module.selection_box.geometry()
    plot_rect = module.canvas.heatmap.plot_area_rect_in_widget()
    analyze_rect = module.analyze_button.geometry()
    replay_rect = module.replay_button.geometry()

    assert plot_rect is not None
    assert module.selection_box.isVisible() is True
    assert selection_rect.width() > 0
    assert selection_rect.left() >= plot_rect.left()
    assert selection_rect.right() <= plot_rect.right()
    assert selection_rect.top() == plot_rect.top()
    assert selection_rect.bottom() == plot_rect.bottom()
    assert module.analyze_button.isVisible() is True
    assert module.analyze_button.isEnabled() is True
    assert module.replay_button.isVisible() is True
    assert module.replay_button.isEnabled() is True
    assert plot_rect.contains(analyze_rect) is True
    assert plot_rect.contains(replay_rect) is True

    module.replay_button.click()

    services.test.replay_selection.assert_called_once_with(202, "run-202", 2, 0.0, 5.0)
