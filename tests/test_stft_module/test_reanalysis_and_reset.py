from __future__ import annotations

import pytest

from app.services_container import FactoryControllerState, FactoryReviewWindow

from ._support import (
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _seed_stft_history,
    _show,
)


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

    module.analysis_select_button.click()
    module.move_selection_to_start(9.0)
    module.analyze_button.click()

    assert module.selected_window == pytest.approx((3.0, 5.0))
    services.factory_mode.begin_reanalysis.assert_called_once_with(3.0, 5.0)
    assert len(services.bus.factory_review_changed.emissions) == review_event_count
    assert len(services.bus.factory_display_reset.emissions) == reset_event_count


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
    module.analysis_select_button.click()
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
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analyze_button.isEnabled() is False
    assert module.analysis_select_button.text() == "取消框选"
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
    module.analysis_select_button.click()
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

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analysis_select_button.text() == "取消框选"
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
    assert module.analyze_button.isVisible() is True
    assert module.analysis_select_button.isEnabled() is True
    assert module.analysis_select_button.text() == "取消框选"
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
    module.analysis_select_button.click()
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

    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
    assert module.analysis_select_button.text() == "取消框选"
    assert module.analyze_button.isEnabled() is False

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )

    assert module.selection_box.isVisible() is True
    assert module.analyze_button.isVisible() is True
    assert module.analysis_select_button.isEnabled() is True
    assert module.analysis_select_button.text() == "取消框选"
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
    assert module.selection_box.isVisible() is False

    services.bus.factory_display_reset.emit("next_run_accepted")

    assert module.selected_window is None
    assert module.selection_box.isVisible() is False
    assert module.analyze_button.isVisible() is False
