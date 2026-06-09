from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt

from app.services_container import FactoryReviewWindow
from ui.widgets.cards import Card

from ._support import (
    _assert_selection_box_matches_selected_window,
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _seed_stft_history,
    _show,
)


def test_review_success_hides_analysis_box_until_selection_mode(qapp):
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

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.replay_button.isVisible() is False
    assert module.analysis_select_button.isVisible() is True
    assert module.analysis_select_button.isEnabled() is True
    assert module.analysis_select_button.text() == "框选分析"
    assert module.selected_window == pytest.approx((1.0, 3.0))
    assert module.canvas.heatmap.chart_interactions.reset_button is None
    assert module.analysis_select_button.parentWidget() is module
    assert module.analysis_select_button.width() >= 144
    assert module.analysis_select_button.height() >= (
        module.analysis_select_button.sizeHint().height()
    )
    assert module.analysis_select_button.x() == 4
    assert (
        module.height()
        - module.analysis_select_button.geometry().bottom()
        - 1
    ) == 4

    module.move_selection_to_start(2.25)

    assert module.selected_window == pytest.approx((2.25, 4.25))
    assert module.selected_window[1] - module.selected_window[0] == pytest.approx(2.0)
    assert module.selection_box.isVisible() is False

    module.analysis_select_button.click()

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.analysis_select_button.text() == "取消框选"
    assert module.selection_box.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    ) is False
    _assert_selection_box_matches_selected_window(module)

    module.analysis_select_button.click()

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.replay_button.isVisible() is False


def test_review_selection_button_uses_card_bottom_padding_when_embedded(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    card = Card("STFT时频图", module)
    card.resize(700, 320)
    card.show()
    qapp.processEvents()
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

    button = module.analysis_select_button

    assert button.isVisible() is True
    assert button.parentWidget() is card
    assert button.height() >= button.sizeHint().height()
    assert button.geometry().bottom() == card.height() - 5
    assert button.geometry().bottom() > module.geometry().bottom()
    assert module.analysis_select_button.text() == "框选分析"


def test_stale_displayed_config_review_cannot_activate_selection_or_reanalysis(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        times=(0.0, 0.5, 1.0, 1.5),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            displayed_config_id=101,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=0.5),
            manual_reanalysis_allowed=False,
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 0.5)},
        ),
    )

    assert module.current_render_cache_key() == (101, "run-101", 0)
    rendered_payload = module.current_image_payload()
    assert rendered_payload is not None
    rendered_matrix = rendered_payload["magnitude_db"]
    assert module.selected_window == pytest.approx((0.0, 0.5))
    assert module.is_selection_movable() is False

    services.bus.factory_review_changed.emit(
        _make_review_payload(
            displayed_config_id=202,
            run_id="run-101",
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=1.5),
            manual_reanalysis_allowed=True,
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 1.5)},
        )
    )
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    stale_image_payload = module.current_image_payload()
    assert stale_image_payload is not None
    assert stale_image_payload["magnitude_db"] is rendered_matrix
    assert stale_image_payload["times_sec"] == rendered_payload["times_sec"]
    assert stale_image_payload["frequencies_hz"] == rendered_payload["frequencies_hz"]
    if module.selected_window is not None:
        assert not (module.selected_window == pytest.approx((1.0, 1.5)))
    assert module.is_selection_movable() is False
    assert module.analysis_select_button.isVisible() is False

    module._begin_reanalysis()

    services.factory_mode.begin_reanalysis.assert_not_called()


def test_stale_run_review_cannot_activate_selection_or_reanalysis(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        channel_index=0,
        run_id="run-current",
        times=(0.0, 0.5, 1.0, 1.5),
    )

    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            displayed_config_id=101,
            run_id="run-current",
            default_window=FactoryReviewWindow(start_sec=0.0, end_sec=0.5),
            manual_reanalysis_allowed=False,
            judgment_payload={"overall": "合格", "analysis_window": (0.0, 0.5)},
        ),
    )

    assert module.current_render_cache_key() == (101, "run-current", 0)
    rendered_payload = module.current_image_payload()
    assert rendered_payload is not None
    rendered_matrix = rendered_payload["magnitude_db"]
    assert module.selected_window == pytest.approx((0.0, 0.5))
    assert module.is_selection_movable() is False
    module._on_factory_runtime_context(
        SimpleNamespace(config_id=101, run_id="run-current")
    )

    services.bus.factory_review_changed.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id="run-stale",
            default_window=FactoryReviewWindow(start_sec=1.0, end_sec=1.5),
            manual_reanalysis_allowed=True,
            judgment_payload={"overall": "合格", "analysis_window": (1.0, 1.5)},
        )
    )
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-current", 0)
    stale_image_payload = module.current_image_payload()
    assert stale_image_payload is not None
    assert stale_image_payload["magnitude_db"] is rendered_matrix
    assert stale_image_payload["times_sec"] == rendered_payload["times_sec"]
    assert stale_image_payload["frequencies_hz"] == rendered_payload["frequencies_hz"]
    if module.selected_window is not None:
        assert not (module.selected_window == pytest.approx((1.0, 1.5)))
    assert module.is_selection_movable() is False
    assert module.analysis_select_button.isVisible() is False

    module._begin_reanalysis()

    services.factory_mode.begin_reanalysis.assert_not_called()


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

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analysis_select_button.isVisible() is True
    assert module.selected_window == pytest.approx((0.0, 2.0))

    module.analysis_select_button.click()

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True


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
    assert module.analysis_select_button.isVisible() is False

    module.analyze_button.click()

    services.factory_mode.begin_reanalysis.assert_not_called()


def test_stft_keeps_rendering_in_stopped_phase_but_hides_manual_controls(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    payload = _make_review_payload()

    _emit_phase(services, payload, run_phase="stopped")
    qapp.processEvents()

    assert module.current_image_payload() is not None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analysis_select_button.isVisible() is False


def test_stft_preserved_review_content_in_plain_stopped_still_hides_manual_controls(qapp):
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
    _emit_phase(services, payload, run_phase="stopped")
    qapp.processEvents()

    assert module.current_image_payload() is not None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False


def test_stft_shows_review_controls_for_stop_triggered_no_result_reviewing(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    payload = _make_review_payload(
        no_result=True,
        error_message="analysis unavailable",
        fallback_window=FactoryReviewWindow(start_sec=0.0, end_sec=2.0),
        judgment_payload=None,
    )

    _emit_phase(services, payload, run_phase="reviewing")
    services.bus.factory_review_changed.emit(payload)
    qapp.processEvents()

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analysis_select_button.isVisible() is True
    assert module.analysis_select_button.isEnabled() is True


@pytest.mark.parametrize(
    ("run_phase", "box_visible", "button_visible", "button_enabled"),
    [
        ("not_ready", False, False, False),
        ("armed", False, False, False),
        ("running", False, False, False),
        ("analyzing", False, False, False),
        ("reviewing", False, True, True),
        ("reanalyzing", False, True, False),
        ("stopped", False, False, False),
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
    assert module.analyze_button.isVisible() is False
    assert module.analyze_button.isEnabled() is False
    assert module.analysis_select_button.isVisible() is button_visible
    assert module.analysis_select_button.isEnabled() is button_enabled
