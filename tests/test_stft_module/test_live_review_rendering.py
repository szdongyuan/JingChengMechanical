from __future__ import annotations

import numpy as np

from app.services_container import FactoryControllerState

from ._support import (
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _make_stft_frame,
    _show,
)


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


def test_stft_module_live_cache_begins_at_earliest_pipeline_frame_time(qapp):
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
    for frame in (
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
        _make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)),
        _make_stft_frame(time=1.0, magnitudes=(5.0, 6.0)),
    ):
        services.bus.stft_updated.emit(101, "run-101", 1, frame)

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

    payload = module.current_image_payload()
    assert payload is not None
    assert payload["times_sec"][0] == 0.0
