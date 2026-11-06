from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.service.run_scoped_playback import (
    PlaybackCode,
    PlaybackLifecycleCode,
    PlaybackLifecycleReport,
    PlaybackLifecycleStatus,
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
)
from app.services_container import FactoryReviewWindow
from ui.modules.stft import StftModule

from ._support import (
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _seed_stft_history,
    _show,
)


RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
OTHER_RUN_ID = "123e4567-e89b-42d3-a456-426614174001"

PLAY_CASES = (
    (
        PlaybackStatus.STARTED,
        PlaybackCode.STARTED,
        "Playback started.",
        "Playback started.",
    ),
    (
        PlaybackStatus.UNAVAILABLE,
        PlaybackCode.BUSY,
        "Playback is busy with another operation.",
        "Playback is busy.",
    ),
    (
        PlaybackStatus.UNAVAILABLE,
        PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE,
        "Persisted audio is unavailable for the selected run.",
        "播放失败：所选运行记录的音频不存在，可能已被删除",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.PERSISTED_FACT_INVALID,
        "Persisted playback facts are invalid.",
        "Selected run playback facts are invalid.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.CONFIG_SNAPSHOT_INVALID,
        "Frozen playback configuration is invalid.",
        "Selected run playback configuration is invalid.",
    ),
    (
        PlaybackStatus.UNAVAILABLE,
        PlaybackCode.CHANNEL_UNAVAILABLE,
        "The selected channel is unavailable for persisted playback.",
        "Selected run channel is unavailable.",
    ),
    (
        PlaybackStatus.UNAVAILABLE,
        PlaybackCode.WINDOW_UNAVAILABLE,
        "The selected window is unavailable for persisted playback.",
        "Selected run window is unavailable.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.AUDIO_LOAD_FAILED,
        "Persisted audio could not be loaded safely.",
        "Selected run audio could not be loaded.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.DEADLINE_CLOCK_FAILED,
        "Playback could not establish a safe deadline.",
        "Playback deadline could not be established.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.REPLACEMENT_TIMED_OUT,
        "The previous playback did not stop before the replacement deadline.",
        "Previous playback replacement timed out.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.REPLACEMENT_NON_CLEAN,
        "The previous playback did not stop cleanly.",
        "Previous playback ended non-cleanly.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.BACKEND_START_FAILED,
        "Playback could not start.",
        "Playback backend could not start.",
    ),
    (
        PlaybackStatus.FAILED,
        PlaybackCode.BACKEND_START_TIMED_OUT,
        "Playback did not start before the startup deadline.",
        "Playback backend start timed out.",
    ),
    (
        PlaybackStatus.CLOSED,
        PlaybackCode.SERVICE_CLOSED,
        "Playback is shut down.",
        "Playback service is closed.",
    ),
)

LIFECYCLE_CASES = (
    (
        PlaybackLifecycleStatus.CLEAN,
        PlaybackLifecycleCode.CLEAN,
        "Playback lifecycle is clean.",
        "Playback lifecycle is clean.",
    ),
    (
        PlaybackLifecycleStatus.NON_CLEAN,
        PlaybackLifecycleCode.BUSY,
        "Playback is busy with another operation.",
        "Playback lifecycle is busy.",
    ),
    (
        PlaybackLifecycleStatus.NON_CLEAN,
        PlaybackLifecycleCode.DEADLINE_CLOCK_FAILED,
        "Playback could not establish a safe deadline.",
        "Playback lifecycle deadline could not be established.",
    ),
    (
        PlaybackLifecycleStatus.TIMED_OUT,
        PlaybackLifecycleCode.BACKEND_JOIN_TIMED_OUT,
        "Playback did not join before the deadline.",
        "Playback lifecycle join timed out.",
    ),
    (
        PlaybackLifecycleStatus.NON_CLEAN,
        PlaybackLifecycleCode.BACKEND_STOP_FAILED,
        "Playback stop signaling failed.",
        "Playback lifecycle stop failed.",
    ),
    (
        PlaybackLifecycleStatus.NON_CLEAN,
        PlaybackLifecycleCode.BACKEND_NON_CLEAN,
        "Playback ended non-cleanly.",
        "Playback lifecycle ended non-cleanly.",
    ),
)


def _prepare_review_overlay(
    qapp,
    *,
    run_phase: str = "reviewing",
    manual_reanalysis_allowed: bool = True,
    selected_channel: int = 2,
    default_window: FactoryReviewWindow | None = None,
    enter_selection_mode: bool = True,
):
    module, services = _make_module()
    playback = SimpleNamespace(play=MagicMock())
    module.run_scoped_playback = playback
    module.selected_run_id_provider = lambda: RUN_ID
    module.selected_display_channel_provider = lambda: selected_channel
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=202,
        channel_index=selected_channel,
        run_id=RUN_ID,
    )
    payload = _make_review_payload(
        displayed_config_id=202,
        run_id=RUN_ID,
        default_window=default_window
        or FactoryReviewWindow(start_sec=1.0, end_sec=3.0),
        manual_reanalysis_allowed=manual_reanalysis_allowed,
        judgment_payload={"overall": "合格", "analysis_window": (1.0, 3.0)},
    )

    _emit_reviewing(module, services, payload)
    if run_phase != "reviewing":
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
    return module, services, playback


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


def test_replay_button_requires_direct_run_scoped_integration_seams(qapp):
    module, _, _ = _prepare_review_overlay(qapp)

    assert module.replay_button.isEnabled() is True

    module.run_scoped_playback = None
    module.refresh_selected_display_channel()
    assert module.replay_button.isEnabled() is False

    module.run_scoped_playback = SimpleNamespace(play=MagicMock())
    module.selected_run_id_provider = None
    module.refresh_selected_display_channel()
    assert module.replay_button.isEnabled() is False


@pytest.mark.parametrize(
    ("status", "code", "summary", "expected_diagnostic"),
    PLAY_CASES,
)
def test_playback_maps_every_status_and_code_without_truthiness(
    qapp,
    status,
    code,
    summary,
    expected_diagnostic,
):
    assert {case[0] for case in PLAY_CASES} == set(PlaybackStatus)
    assert {case[1] for case in PLAY_CASES} == set(PlaybackCode)
    module, services, playback = _prepare_review_overlay(qapp)
    playback.play.return_value = PlaybackResult(status, RUN_ID, code, summary)

    module.replay_button.click()

    playback.play.assert_called_once_with(
        RunPlaybackRequest(
            run_id=RUN_ID,
            display_channel_index=2,
            window_start_seconds=1.0,
            window_end_seconds=3.0,
        )
    )
    assert services.bus.status.emissions == [((expected_diagnostic,), {})]
    assert services.bus.log.emissions == [((expected_diagnostic,), {})]
    assert module.replay_button.isEnabled() is (
        status is not PlaybackStatus.CLOSED
    )


def test_started_replacement_is_reported_explicitly(qapp):
    module, services, playback = _prepare_review_overlay(qapp)
    playback.play.return_value = PlaybackResult(
        PlaybackStatus.STARTED,
        RUN_ID,
        PlaybackCode.STARTED,
        "Playback started.",
        replaced_run_id=OTHER_RUN_ID,
    )

    module.replay_button.click()

    assert services.bus.status.emissions == [
        (("Playback replaced the previous run.",), {})
    ]
    assert services.bus.log.emissions == [
        (("Playback replaced the previous run.",), {})
    ]


@pytest.mark.parametrize(
    ("status", "code", "summary", "expected_diagnostic"),
    LIFECYCLE_CASES,
)
def test_playback_lifecycle_maps_every_status_and_code_without_truthiness(
    qapp,
    status,
    code,
    summary,
    expected_diagnostic,
):
    assert {case[0] for case in LIFECYCLE_CASES} == set(PlaybackLifecycleStatus)
    assert {case[1] for case in LIFECYCLE_CASES} == set(PlaybackLifecycleCode)
    module, services = _make_module()
    report = PlaybackLifecycleReport(status, code, summary)

    module.handle_playback_lifecycle_report(report)

    assert services.bus.status.emissions == [((expected_diagnostic,), {})]
    assert services.bus.log.emissions == [((expected_diagnostic,), {})]


def test_playback_uses_exact_authoritative_run_channel_and_window(qapp):
    module, _, playback = _prepare_review_overlay(qapp)
    playback.play.return_value = PlaybackResult(
        PlaybackStatus.STARTED,
        RUN_ID,
        PlaybackCode.STARTED,
        "Playback started.",
    )
    module._selected_window = (1.1250000000003, 3.8750000000007)

    module._play_selected_run()

    request = playback.play.call_args.args[0]
    assert request == RunPlaybackRequest(
        run_id=RUN_ID,
        display_channel_index=2,
        window_start_seconds=1.1250000000003,
        window_end_seconds=3.8750000000007,
    )
    assert not hasattr(request, "config_id")


def test_playback_never_borrows_a_different_selected_run(qapp):
    module, services, playback = _prepare_review_overlay(qapp)
    module.selected_run_id_provider = lambda: OTHER_RUN_ID

    module._play_selected_run()

    playback.play.assert_not_called()
    assert services.bus.status.emissions == []
    assert services.bus.log.emissions == []


@pytest.mark.parametrize(
    "window",
    [
        (-1.0, 2.0),
        (2.0, 2.0),
        (3.0, 2.0),
        (float("nan"), 2.0),
        (1.0, float("inf")),
        (1, 2.0),
    ],
)
def test_playback_rejects_invalid_windows_without_default_clamp_or_reorder(
    qapp,
    window,
):
    module, _, playback = _prepare_review_overlay(qapp)
    module._selected_window = window

    module._play_selected_run()

    playback.play.assert_not_called()


def test_reanalysis_forwards_the_exact_selected_window_without_duration_clamp(qapp):
    module, services, _ = _prepare_review_overlay(qapp)
    module._selected_window = (4.1250000000003, 6.8750000000007)

    module._begin_reanalysis()

    services.factory_mode.begin_reanalysis.assert_called_once_with(
        4.1250000000003,
        6.8750000000007,
    )


def test_stft_production_source_has_no_legacy_test_playback_path():
    source = inspect.getsource(StftModule)

    assert "services.test" not in source
    assert "replay_selection" not in source
    assert "can_replay_selection" not in source


def test_invalid_or_exceptional_playback_result_has_one_bounded_diagnostic(qapp):
    module, services, playback = _prepare_review_overlay(qapp)
    playback.play.side_effect = RuntimeError("private backend detail")

    module._play_selected_run()

    assert services.bus.status.emissions == [(("Playback request failed.",), {})]
    assert services.bus.log.emissions == [(("Playback request failed.",), {})]
    assert "private backend detail" not in repr(services.bus.log.emissions)


def test_truthy_non_contract_playback_result_is_not_success(qapp):
    module, services, playback = _prepare_review_overlay(qapp)
    playback.play.return_value = SimpleNamespace(
        status=PlaybackStatus.STARTED,
        code=PlaybackCode.STARTED,
        summary="Playback started.",
    )

    module._play_selected_run()

    assert services.bus.status.emissions == [(("Playback result is invalid.",), {})]
    assert services.bus.log.emissions == [(("Playback result is invalid.",), {})]


def test_truthy_non_contract_lifecycle_report_is_not_clean(qapp):
    module, services = _make_module()
    report = SimpleNamespace(
        status=PlaybackLifecycleStatus.CLEAN,
        code=PlaybackLifecycleCode.CLEAN,
        summary="Playback lifecycle is clean.",
    )

    module.handle_playback_lifecycle_report(report)

    assert services.bus.status.emissions == [
        (("Playback lifecycle report is invalid.",), {})
    ]
    assert services.bus.log.emissions == [
        (("Playback lifecycle report is invalid.",), {})
    ]


def test_replay_button_geometry_does_not_overlap_analysis_button_or_selection_box(
    qapp,
):
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
    module, _, playback = _prepare_review_overlay(
        qapp,
        default_window=FactoryReviewWindow(start_sec=0.0, end_sec=5.0),
    )
    playback.play.return_value = PlaybackResult(
        PlaybackStatus.STARTED,
        RUN_ID,
        PlaybackCode.STARTED,
        "Playback started.",
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

    playback.play.assert_called_once_with(
        RunPlaybackRequest(RUN_ID, 2, 0.0, 5.0)
    )
