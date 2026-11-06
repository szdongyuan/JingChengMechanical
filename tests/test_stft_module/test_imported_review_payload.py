from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from app.services_container import FactoryReviewWindow

from ._support import (
    _emit_phase,
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _make_stft_frame,
    _show,
)


def test_imported_review_payload_seeds_stft_cache_and_renders_selected_channel(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import_101_test",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.35, 0.45],
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.asarray([[1.0, 2.0], [3.0, 4.0]]),
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

    assert module.current_render_cache_key() == (101, "import_101_test", 0)
    image = module.current_image_payload()
    assert image["times_sec"] == [0.35, 0.45]
    assert image["frequencies_hz"] == [100.0, 200.0]
    np.testing.assert_allclose(image["magnitude_db"], [[1.0, 2.0], [3.0, 4.0]])


def test_imported_review_payload_shows_analysis_selection_button_when_allowed(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import_101_reanalysis",
        captured_duration_sec=1.0,
        default_window=FactoryReviewWindow(0.0, 0.5),
        fallback_window=FactoryReviewWindow(0.0, 1.0),
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.0, 0.5, 1.0],
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.asarray(
                        [[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]
                    ),
                }
            }
        },
    )
    review = replace(
        review,
        trigger_source="import_audio",
        manual_reanalysis_allowed=True,
    )

    _show(module, qapp)
    _emit_reviewing(module, services, review)

    assert module.analysis_select_button.isVisible() is True
    assert module.analysis_select_button.isEnabled() is True
    assert module.analysis_select_button.text() == "框选分析"


def test_imported_review_payload_small_absolute_offset_preserves_rendered_left_extent(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    times_sec = np.asarray([0.1853, 0.3133, 0.4413], dtype=float)
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import_101_small_offset",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": times_sec,
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.asarray(
                        [[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]
                    ),
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

    step = float(np.median(np.diff(times_sec)))
    expected_left = float(times_sec[0]) - 0.5 * step
    expected_right = float(times_sec[-1]) + 0.5 * step
    assert module.canvas.heatmap.rendered_time_extent_sec() == pytest.approx(
        (expected_left, expected_right)
    )


def test_imported_review_payload_single_column_preserves_absolute_center(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    time_sec = 0.1853
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import_101_single_column",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [time_sec],
                    "frequencies_hz": [100.0, 200.0],
                    "magnitude_db": np.asarray([[1.0], [3.0]]),
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

    assert module.current_render_cache_key() == (101, "import_101_single_column", 0)
    assert module.canvas.heatmap.rendered_time_extent_sec() == pytest.approx(
        (time_sec - 0.5, time_sec + 0.5)
    )


def test_imported_review_payload_renders_chart_only_stft_channel(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 6
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import_101_chart_only",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.1],
                    "frequencies_hz": [100.0],
                    "magnitude_db": np.asarray([[1.0]]),
                }
            },
            6: {
                "stft": {
                    "times_sec": [0.2],
                    "frequencies_hz": [100.0],
                    "magnitude_db": np.asarray([[9.0]]),
                }
            },
        },
    )
    review = replace(
        review,
        trigger_source="import_audio",
        manual_reanalysis_allowed=False,
    )

    _show(module, qapp)
    _emit_reviewing(module, services, review)

    assert module.current_render_cache_key() == (101, "import_101_chart_only", 6)
    np.testing.assert_allclose(module.current_image_payload()["magnitude_db"], [[9.0]])


def test_hardware_review_payload_seeds_stft_from_terminal_chart_payload(qapp):
    module, services = _make_module()
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="run-101",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.1],
                    "frequencies_hz": [100.0],
                    "magnitude_db": np.asarray([[1.0]]),
                }
            }
        },
    )

    _show(module, qapp)
    _emit_reviewing(module, services, review)

    assert module.current_render_cache_key() == (101, "run-101", 0)
    assert module.current_image_payload()["times_sec"] == [0.1]


def test_imported_review_payload_removes_stale_stft_cache_channels(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 6
    review = _make_review_payload(
        displayed_config_id=101,
        run_id="import_101_replace",
        chart_payload={
            0: {
                "stft": {
                    "times_sec": [0.1],
                    "frequencies_hz": [100.0],
                    "magnitude_db": np.asarray([[1.0]]),
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
    _emit_phase(services, review, run_phase="reviewing")
    services.bus.stft_updated.emit(
        101,
        "import_101_replace",
        6,
        _make_stft_frame(time=0.0, magnitudes=(9.0, 10.0)),
    )
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "import_101_replace", 6)

    _emit_reviewing(module, services, review)

    assert module.current_render_cache_key() is None
    assert module.current_image_payload() is None
