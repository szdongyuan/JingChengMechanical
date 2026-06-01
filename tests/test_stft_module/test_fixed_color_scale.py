from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from ._support import (
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _make_stft_frame,
    _seed_stft_history,
    _show,
)


def _make_module_with_config(config: dict):
    module, services = _make_module()
    services.config_service = SimpleNamespace(
        load_config=MagicMock(return_value=config),
    )
    return module, services


def test_stft_module_uses_fixed_color_scale_for_selected_channel(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -80.0,
                    "stft_color_scale_max_db": -5.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-30.0, -20.0),),
    )
    module.flush_pending_live_refresh()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-80.0, -5.0))


def test_stft_module_restores_fixed_levels_when_switching_channels(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0, 1],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -90.0,
                    "stft_color_scale_max_db": -10.0,
                },
                {
                    "channel_index": 1,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -60.0,
                    "stft_color_scale_max_db": 0.0,
                },
            ],
        }
    )
    selected = {"channel": 0}
    module.selected_display_channel_provider = lambda: selected["channel"]

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-40.0, -30.0),),
    )
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=1,
        times=(0.25,),
        magnitudes=((-20.0, -10.0),),
    )
    module.flush_pending_live_refresh()
    assert module.canvas.heatmap.image_levels() == pytest.approx((-90.0, -10.0))

    selected["channel"] = 1
    module.refresh_selected_display_channel()
    qapp.processEvents()
    assert module.canvas.heatmap.image_levels() == pytest.approx((-60.0, 0.0))

    selected["channel"] = 0
    module.refresh_selected_display_channel()
    qapp.processEvents()
    assert module.canvas.heatmap.image_levels() == pytest.approx((-90.0, -10.0))


def test_stft_module_keeps_auto_levels_when_fixed_scale_disabled(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": False,
                    "stft_color_scale_min_db": -80.0,
                    "stft_color_scale_max_db": -5.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-30.0, -20.0),),
    )
    module.flush_pending_live_refresh()

    assert module.canvas.heatmap.image_levels() != pytest.approx((-80.0, -5.0))


def test_refresh_selected_display_channel_updates_levels_when_fixed_scale_enabled(
    qapp,
):
    automatic_config = {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [
            {
                "channel_index": 0,
                "stft_color_scale_enabled": False,
                "stft_color_scale_min_db": -80.0,
                "stft_color_scale_max_db": -5.0,
            }
        ],
    }
    fixed_config = {
        "config_id": 101,
        "selected_channels": [0],
        "channels": [
            {
                "channel_index": 0,
                "stft_color_scale_enabled": True,
                "stft_color_scale_min_db": -80.0,
                "stft_color_scale_max_db": -5.0,
            }
        ],
    }
    module, services = _make_module_with_config(automatic_config)
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-30.0, -20.0),),
    )
    module.flush_pending_live_refresh()
    assert module.canvas.heatmap.image_levels() == pytest.approx((-30.0, -20.0))

    services.config_service.load_config.return_value = fixed_config
    module.refresh_selected_display_channel()
    qapp.processEvents()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-80.0, -5.0))


@pytest.mark.parametrize(
    "channel_config",
    [
        {
            "channel_index": 0,
            "stft_color_scale_enabled": True,
        },
        {
            "channel_index": 0,
            "stft_color_scale_enabled": True,
            "stft_color_scale_min_db": -5.0,
            "stft_color_scale_max_db": -80.0,
        },
        {
            "channel_index": 0,
            "stft_color_scale_enabled": True,
            "stft_color_scale_min_db": "invalid",
            "stft_color_scale_max_db": -5.0,
        },
    ],
)
def test_stft_module_keeps_auto_levels_when_fixed_scale_invalid_or_missing(
    qapp,
    channel_config,
):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [channel_config],
        }
    )
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-30.0, -20.0),),
    )
    module.flush_pending_live_refresh()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-30.0, -20.0))


def test_stft_module_keeps_auto_levels_without_config_service(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-30.0, -20.0),),
    )
    module.flush_pending_live_refresh()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-30.0, -20.0))


def test_stft_module_keeps_fixed_color_scale_when_later_live_frames_arrive(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -80.0,
                    "stft_color_scale_max_db": -5.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25,),
        magnitudes=((-30.0, -20.0),),
    )
    module.flush_pending_live_refresh()
    assert module.canvas.heatmap.image_levels() == pytest.approx((-80.0, -5.0))

    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.75, magnitudes=(-8.0, -2.0)),
    )
    module.flush_pending_live_refresh()
    qapp.processEvents()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-80.0, -5.0))


def test_stft_module_uses_fixed_color_scale_for_review_rendering(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -85.0,
                    "stft_color_scale_max_db": -15.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25, 0.75),
        magnitudes=((-35.0, -25.0), (-30.0, -20.0)),
    )
    module.flush_pending_live_refresh()
    _emit_reviewing(
        module,
        services,
        _make_review_payload(displayed_config_id=101, run_id="run-101"),
    )
    qapp.processEvents()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-85.0, -15.0))


def test_stft_module_keeps_fixed_color_scale_for_reanalysis_review_rendering(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -82.0,
                    "stft_color_scale_max_db": -12.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 0

    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
        times=(0.25, 0.75),
        magnitudes=((-35.0, -25.0), (-30.0, -20.0)),
    )
    module.flush_pending_live_refresh()
    review = _make_review_payload(displayed_config_id=101, run_id="run-101")
    _emit_reviewing(module, services, review)
    _emit_phase(services, review, run_phase="reanalyzing")
    qapp.processEvents()
    assert module.canvas.heatmap.image_levels() == pytest.approx((-82.0, -12.0))

    completion = _make_review_payload(
        displayed_config_id=101,
        run_id="run-101",
        judgment_payload={"overall": "合格", "source": "reanalysis"},
    )
    _emit_reviewing(module, services, completion)
    qapp.processEvents()

    assert module.canvas.heatmap.image_levels() == pytest.approx((-82.0, -12.0))


def test_imported_mapped_stft_channel_uses_configured_fixed_color_scale(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -70.0,
                    "stft_color_scale_max_db": -7.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 0
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import-101",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.25],
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.asarray([[-30.0], [-20.0]]),
                }
            }
        },
    )
    review = replace(
        review,
        trigger_source="import_audio",
        manual_reanalysis_allowed=False,
    )

    _show(module, qapp)
    _emit_reviewing(module, services, review)

    assert module.canvas.heatmap.image_levels() == pytest.approx((-70.0, -7.0))


def test_imported_chart_only_stft_channel_uses_auto_color_scale(qapp):
    module, services = _make_module_with_config(
        {
            "config_id": 101,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_color_scale_enabled": True,
                    "stft_color_scale_min_db": -70.0,
                    "stft_color_scale_max_db": -7.0,
                }
            ],
        }
    )
    module.selected_display_channel_provider = lambda: 6
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import-101",
        chart_payload={
            6: {
                "stft": {
                    "times_sec": [0.25],
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.asarray([[-30.0], [-20.0]]),
                }
            }
        },
    )
    review = replace(
        review,
        trigger_source="import_audio",
        manual_reanalysis_allowed=False,
    )

    _show(module, qapp)
    _emit_reviewing(module, services, review)

    assert module.canvas.heatmap.image_levels() != pytest.approx((-70.0, -7.0))
