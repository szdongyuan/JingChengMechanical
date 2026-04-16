from __future__ import annotations

import pytest

from app.services_container import FactoryReviewWindow

from ._support import (
    _assert_selection_box_matches_selected_window,
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _seed_stft_history,
    _show,
)


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

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.analyze_button.isEnabled() is True


@pytest.mark.parametrize(
    ("run_phase", "box_visible", "button_visible", "button_enabled"),
    [
        ("not_ready", False, False, False),
        ("armed", False, False, False),
        ("running", False, False, False),
        ("analyzing", False, False, False),
        ("reviewing", True, True, True),
        ("reanalyzing", True, True, False),
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
    assert module.analyze_button.isVisible() is button_visible
    assert module.analyze_button.isEnabled() is button_enabled
