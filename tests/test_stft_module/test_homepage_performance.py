from __future__ import annotations

import numpy as np
import pytest

from app.services_container import FactoryControllerState
from ui.modules import stft as stft_module
from ui.widgets.analysis_charts import StftImageChartWidget
from app.service.analysis.stft_preview import CoarseStftFrame
from types import SimpleNamespace

from ._support import (
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _make_stft_frame,
    _seed_stft_history,
    _show,
)


def _coarse(time=.1, bounds=(.05, .15)):
    return CoarseStftFrame(time, np.array([100., 200.]), np.array([5., 6.]), bounds, 4)


def test_coarse_cache_forwards_bounds_and_coalesces_refresh(qapp, monkeypatch):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    services.bus.factory_state_changed.emit(FactoryControllerState(displayed_config_id=101, run_phase="running"))
    callbacks = []
    monkeypatch.setattr(module, "_schedule_live_flush_callback", callbacks.append)
    for frame in [_coarse(), _coarse(.2, (.15, .25)), _coarse(.3, (.25, .35))]:
        services.bus.stft_updated.emit(101, "run-101", 0, frame)
    assert len(callbacks) == 1
    module.flush_pending_live_refresh()
    expected = [(.05, .15), (.15, .25), (.25, .35)]
    assert module._stft_cache[(101, "run-101", 0)].image_payload()["time_bounds_sec"] == expected
    assert module.canvas.heatmap.image_payload()["time_bounds_sec"] == expected
    assert module.canvas.heatmap.rendered_time_extent_sec() == pytest.approx((.05, .35))


@pytest.mark.parametrize("time,bounds", [(.2, (.1, .25)), (.2, (.25, .3)),
    (.2, (.2, .2)), (.2, (.3, .2)), (.2, (np.nan, .25)), (.2, (.15, np.inf)), (.2, (.15,))])
def test_invalid_coarse_bounds_leave_cache_unchanged(time, bounds):
    cache = stft_module._StftCacheEntry()
    assert cache.append_frame(_coarse())
    matrix = cache.magnitude_db
    assert not cache.append_frame(_coarse(time, bounds))
    assert cache.times_sec == [.1]
    assert cache.render_revision == 1
    assert cache.magnitude_db is matrix
    assert cache.image_payload()["time_bounds_sec"] == [(.05, .15)]


def test_later_coarse_frame_cannot_append_to_authoritative_terminal_cache(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    source = np.array([[1., 2.], [3., 4.]])
    _emit_reviewing(module, services, _make_review_payload(run_id="run-101", chart_payload={0: {"stft": {
        "times_sec": [.1, .5], "frequencies_hz": [100., 200.], "magnitude_db": source}}}))
    cache = module._stft_cache[(101, "run-101", 0)]
    matrix, revision = cache.magnitude_db, cache.render_revision
    services.bus.stft_updated.emit(101, "run-101", 0, _coarse(.55, (.5, .6)))
    module.flush_pending_live_refresh()
    assert cache.times_sec == [.1, .5]
    assert cache.magnitude_db is matrix and cache.render_revision == revision
    assert "time_bounds_sec" not in module.current_image_payload()
    np.testing.assert_array_equal(module.current_image_payload()["magnitude_db"], source)


@pytest.mark.parametrize("transition", ["reset", "runtime"])
def test_delayed_coarse_frames_do_not_reclaim_display_after_identity_transition(qapp, transition):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    services.bus.factory_state_changed.emit(FactoryControllerState(displayed_config_id=101, run_phase="running"))
    services.bus.stft_updated.emit(101, "old", 0, _coarse())
    if transition == "reset":
        services.bus.factory_display_reset.emit("new-run")
        services.bus.stft_updated.emit(101, "old", 0, _coarse(.2, (.15, .25)))
        assert module.current_image_payload() is None
    services.bus.factory_runtime_start_accepted.emit(SimpleNamespace(config_id=101, run_id="new"))
    services.bus.stft_updated.emit(101, "new", 0, _coarse())
    services.bus.stft_updated.emit(101, "old", 0, _coarse(.3, (.25, .35)))
    module.flush_pending_live_refresh()
    assert module.current_render_cache_key() == (101, "new", 0)
    assert module.current_image_payload()["times_sec"] == [.1]


@pytest.mark.parametrize("time", [0.25, 0.5, np.nan, np.inf, -np.inf])
def test_late_or_invalid_live_frame_keeps_seeded_review_cache_intact(qapp, time):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    source = np.array([[1.0, 2.0], [3.0, 4.0]])
    _emit_reviewing(module, services, _make_review_payload(
        run_id="run-101", chart_payload={0: {"stft": {
            "times_sec": [0.1, 0.5], "frequencies_hz": [100.0, 200.0],
            "magnitude_db": source,
        }}},
    ))
    cache = module._stft_cache[(101, "run-101", 0)]
    matrix = cache.magnitude_db
    revision = cache.render_revision
    services.bus.stft_updated.emit(
        101, "run-101", 0, _make_stft_frame(time=time, magnitudes=(50.0, 60.0))
    )
    module.flush_pending_live_refresh()
    assert cache.times_sec == [0.1, 0.5]
    assert cache.magnitude_db is matrix
    assert cache.render_revision == revision
    np.testing.assert_array_equal(module.current_image_payload()["magnitude_db"], source)


def test_invalid_first_frame_does_not_initialize_stft_cache():
    cache = stft_module._StftCacheEntry()
    assert cache.append_frame(_make_stft_frame(time=np.nan, magnitudes=(1, 2))) is False
    assert cache.frequencies_hz == []
    assert cache.times_sec == []
    assert cache.magnitude_db is None
    assert cache.render_revision == 0


@pytest.mark.parametrize("times", [[0.1, 0.1, 0.3], [0.1, 0.3, 0.2]])
def test_imported_stft_rejects_invalid_order_without_silently_dropping_columns(times):
    assert stft_module.StftModule._validated_imported_stft_payload({
        "times_sec": times, "frequencies_hz": [100.0, 200.0],
        "magnitude_db": np.ones((2, 3)),
    }) is None


def test_stft_cache_entry_appends_frames_without_full_matrix_rebuild(monkeypatch):
    cache_entry = stft_module._StftCacheEntry()

    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))

    def _fail_full_matrix_rebuild(*args, **kwargs):
        raise AssertionError("cache entry should reuse direct matrix storage")

    monkeypatch.setattr(stft_module.np, "concatenate", _fail_full_matrix_rebuild)
    monkeypatch.setattr(stft_module.np, "column_stack", _fail_full_matrix_rebuild)

    assert cache_entry.append_frame(_make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)))

    payload = cache_entry.image_payload()
    assert payload is not None
    assert payload["times_sec"] == [0.0, 0.5]
    assert payload["frequencies_hz"] == [100.0, 200.0]
    assert payload["magnitude_db"] is cache_entry.magnitude_db
    assert np.array_equal(
        payload["magnitude_db"],
        np.array([[1.0, 3.0], [2.0, 4.0]]),
    )


def test_stft_cache_entry_rejects_different_length_frequency_axis_without_mutation():
    cache_entry = stft_module._StftCacheEntry()

    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))
    original_payload = cache_entry.image_payload()
    assert original_payload is not None
    original_matrix = original_payload["magnitude_db"]

    assert (
        cache_entry.append_frame(
            _make_stft_frame(
                time=0.5,
                magnitudes=(3.0, 4.0, 5.0),
                frequencies=(100.0, 200.0, 300.0),
            )
        )
        is False
    )

    payload = cache_entry.image_payload()
    assert payload is not None
    assert payload["times_sec"] == [0.0]
    assert payload["frequencies_hz"] == [100.0, 200.0]
    assert payload["magnitude_db"] is original_matrix
    assert np.array_equal(payload["magnitude_db"], np.array([[1.0], [2.0]]))


def test_stft_cache_entry_increments_render_revision_only_for_valid_frames():
    cache_entry = stft_module._StftCacheEntry()

    assert cache_entry.render_revision == 0
    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))
    assert cache_entry.render_revision == 1
    assert cache_entry.append_frame(_make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)))
    assert cache_entry.render_revision == 2


def test_stft_cache_entry_rejects_mismatched_frequency_frame_without_revision_advance():
    cache_entry = stft_module._StftCacheEntry()
    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))

    assert cache_entry.render_revision == 1
    assert (
        cache_entry.append_frame(
            _make_stft_frame(
                time=0.5,
                magnitudes=(3.0, 4.0),
                frequencies=(125.0, 250.0),
            )
        )
        is False
    )
    assert cache_entry.render_revision == 1


def test_refresh_selected_display_channel_reuses_cached_matrix_without_column_stack(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
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
    ):
        services.bus.stft_updated.emit(101, "run-101", 0, frame)

    cache_entry = module._stft_cache[(101, "run-101", 0)]
    cached_matrix = cache_entry.image_payload()["magnitude_db"]
    captured: dict[str, object] = {}

    def _capture_set_image(
        *,
        times_sec,
        frequencies_hz,
        magnitude_db,
        reset_levels,
        levels_db,
        snap_initial_to_zero,
    ):
        captured["times_sec"] = times_sec
        captured["frequencies_hz"] = frequencies_hz
        captured["magnitude_db"] = magnitude_db
        captured["reset_levels"] = reset_levels
        captured["levels_db"] = levels_db
        captured["snap_initial_to_zero"] = snap_initial_to_zero

    def _fail_column_stack(*args, **kwargs):
        raise AssertionError("refresh should reuse the stored matrix directly")

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _capture_set_image)
    monkeypatch.setattr(stft_module.np, "column_stack", _fail_column_stack)

    module.refresh_selected_display_channel()

    assert captured["times_sec"] == [0.0, 0.5]
    assert captured["frequencies_hz"] == [100.0, 200.0]
    assert captured["magnitude_db"] is cached_matrix
    assert captured["reset_levels"] is False
    assert captured["levels_db"] is None
    assert captured["snap_initial_to_zero"] is True


def test_refresh_selected_display_channel_skips_redraw_when_same_signature_is_visible(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
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
    ):
        services.bus.stft_updated.emit(101, "run-101", 0, frame)
    qapp.processEvents()
    module.flush_pending_live_refresh()
    qapp.processEvents()

    redraw_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    module.refresh_selected_display_channel()

    assert redraw_calls == []


def test_review_metadata_churn_skips_redraw_for_same_run_without_new_frames(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    _emit_reviewing(
        module,
        services,
        _make_review_payload(
            run_id="run-101",
            judgment_payload={"overall": "合格"},
        ),
    )
    qapp.processEvents()

    redraw_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    services.bus.factory_review_changed.emit(
        _make_review_payload(
            run_id="run-101",
            judgment_payload=None,
        )
    )
    qapp.processEvents()

    assert redraw_calls == []
    assert module.selection_box.isVisible() is False


def test_review_and_reanalysis_state_churn_skip_redraw_for_unchanged_signature(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, channel_index=0)
    payload = _make_review_payload(
        run_id="run-101",
        judgment_payload={"overall": "合格"},
    )
    _emit_reviewing(module, services, payload)
    qapp.processEvents()

    redraw_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        module.canvas.heatmap,
        "set_image",
        lambda **kwargs: redraw_calls.append(kwargs),
    )

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reanalyzing",
            trigger_source="manual",
        )
    )
    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )
    qapp.processEvents()

    assert redraw_calls == []


def test_refresh_selected_display_channel_restores_cached_image_after_heatmap_clear(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
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
    ):
        services.bus.stft_updated.emit(101, "run-101", 0, frame)

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="manual",
        )
    )
    qapp.processEvents()

    cache_key = (101, "run-101", 0)
    cache_entry = module._stft_cache[cache_key]
    cached_payload = cache_entry.image_payload()
    assert cached_payload is not None
    cached_matrix = cached_payload["magnitude_db"]
    assert module.current_render_cache_key() == cache_key
    assert np.array_equal(
        module.canvas.heatmap.image_payload()["magnitude_db"],
        np.array([[1.0, 3.0], [2.0, 4.0]]),
    )

    render_calls: list[dict[str, object]] = []
    real_set_image = module.canvas.heatmap.set_image

    def _spy_set_image(
        *,
        times_sec,
        frequencies_hz,
        magnitude_db,
        reset_levels,
        levels_db,
        snap_initial_to_zero,
    ):
        render_calls.append(
            {
                "times_sec": list(times_sec),
                "frequencies_hz": list(frequencies_hz),
                "magnitude_db": magnitude_db,
                "reset_levels": reset_levels,
                "levels_db": levels_db,
                "snap_initial_to_zero": snap_initial_to_zero,
            }
        )
        real_set_image(
            times_sec=times_sec,
            frequencies_hz=frequencies_hz,
            magnitude_db=magnitude_db,
            reset_levels=reset_levels,
            levels_db=levels_db,
            snap_initial_to_zero=snap_initial_to_zero,
        )

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _spy_set_image)

    module.canvas.heatmap.clear_data()

    assert module.canvas.heatmap.image_payload() is None
    assert module.current_render_cache_key() == cache_key

    module.refresh_selected_display_channel()

    restored_payload = module.canvas.heatmap.image_payload()
    assert len(render_calls) == 1
    assert render_calls[0]["times_sec"] == [0.0, 0.5]
    assert render_calls[0]["frequencies_hz"] == [100.0, 200.0]
    assert render_calls[0]["magnitude_db"] is cached_matrix
    assert render_calls[0]["reset_levels"] is True
    assert render_calls[0]["levels_db"] is None
    assert render_calls[0]["snap_initial_to_zero"] is True
    assert module.current_render_cache_key() == cache_key
    assert restored_payload is not None
    assert np.array_equal(restored_payload["magnitude_db"], cached_matrix)


def test_stft_module_rejects_mismatched_frequency_frame_and_keeps_prior_cache(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
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
    ):
        services.bus.stft_updated.emit(101, "run-101", 0, frame)

    cache_entry = module._stft_cache[(101, "run-101", 0)]
    cached_payload = cache_entry.image_payload()
    assert cached_payload is not None
    cached_matrix = cached_payload["magnitude_db"]

    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(
            time=1.0,
            magnitudes=(5.0, 6.0),
            frequencies=(125.0, 250.0),
        ),
    )

    payload = cache_entry.image_payload()
    assert payload is not None
    assert payload["times_sec"] == [0.0, 0.5]
    assert payload["frequencies_hz"] == [100.0, 200.0]
    assert payload["magnitude_db"] is cached_matrix
    assert np.array_equal(
        payload["magnitude_db"],
        np.array([[1.0, 3.0], [2.0, 4.0]]),
    )

    current_payload = module.current_image_payload()
    assert current_payload is not None
    assert current_payload["times_sec"] == [0.0, 0.5]
    assert np.array_equal(
        current_payload["magnitude_db"],
        np.array([[1.0, 3.0], [2.0, 4.0]]),
    )


def test_refresh_selected_display_channel_clears_when_selected_channel_cache_is_missing(
    qapp,
):
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
    services.bus.stft_updated.emit(
        101,
        "run-101",
        2,
        _make_stft_frame(time=0.0, magnitudes=(5.0, 6.0)),
    )

    assert module.current_render_cache_key() == (101, "run-101", 0)
    assert module.current_image_payload() is not None

    selected_channel = 1
    module.refresh_selected_display_channel()

    assert module.current_render_cache_key() is None
    assert module.current_image_payload() is None


def test_live_updates_schedule_one_flush_for_many_frames_until_module_flush_runs(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    scheduled_flushes: list[object] = []
    render_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        module,
        "_schedule_live_flush_callback",
        lambda callback: scheduled_flushes.append(callback),
    )

    def _capture_set_image(
        *,
        times_sec,
        frequencies_hz,
        magnitude_db,
        reset_levels,
        levels_db,
        snap_initial_to_zero,
    ):
        render_calls.append(
            {
                "times_sec": list(times_sec),
                "frequencies_hz": list(frequencies_hz),
                "magnitude_db": magnitude_db,
                "reset_levels": reset_levels,
                "levels_db": levels_db,
                "snap_initial_to_zero": snap_initial_to_zero,
            }
        )

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _capture_set_image)
    monkeypatch.setattr(module.canvas.heatmap, "has_image", lambda: bool(render_calls))

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

    assert len(render_calls) == 1
    assert render_calls[0]["times_sec"] == [0.0]
    assert render_calls[0]["reset_levels"] is True
    assert render_calls[0]["levels_db"] is None
    assert render_calls[0]["snap_initial_to_zero"] is True
    assert module.pending_live_flush_key() is None

    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)),
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=1.0, magnitudes=(5.0, 6.0)),
    )

    assert len(scheduled_flushes) == 1
    assert module.pending_live_flush_key() == (101, "run-101", 0)
    assert len(render_calls) == 1

    module.flush_pending_live_refresh()

    assert module.pending_live_flush_key() is None
    assert len(render_calls) == 2
    assert render_calls[1]["times_sec"] == [0.0, 0.5, 1.0]
    assert render_calls[1]["frequencies_hz"] == [100.0, 200.0]
    assert render_calls[1]["reset_levels"] is False
    assert render_calls[1]["levels_db"] is None
    assert render_calls[1]["snap_initial_to_zero"] is True
    assert np.array_equal(
        render_calls[1]["magnitude_db"],
        np.array([[1.0, 3.0, 5.0], [2.0, 4.0, 6.0]]),
    )


def test_refresh_boundary_consumes_pending_live_flush_without_double_render(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)

    scheduled_flushes: list[object] = []
    render_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        module,
        "_schedule_live_flush_callback",
        lambda callback: scheduled_flushes.append(callback),
    )

    def _capture_set_image(
        *,
        times_sec,
        frequencies_hz,
        magnitude_db,
        reset_levels,
        levels_db,
        snap_initial_to_zero,
    ):
        render_calls.append(
            {
                "times_sec": list(times_sec),
                "frequencies_hz": list(frequencies_hz),
                "magnitude_db": magnitude_db,
                "reset_levels": reset_levels,
                "levels_db": levels_db,
                "snap_initial_to_zero": snap_initial_to_zero,
            }
        )

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _capture_set_image)
    monkeypatch.setattr(module.canvas.heatmap, "has_image", lambda: bool(render_calls))

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
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)),
    )

    assert len(render_calls) == 1
    assert render_calls[0]["levels_db"] is None
    assert render_calls[0]["snap_initial_to_zero"] is True
    assert len(scheduled_flushes) == 1
    assert module.pending_live_flush_key() == (101, "run-101", 0)

    module.refresh_selected_display_channel()

    assert len(render_calls) == 2
    assert render_calls[1]["times_sec"] == [0.0, 0.5]
    assert render_calls[1]["reset_levels"] is False
    assert render_calls[1]["levels_db"] is None
    assert render_calls[1]["snap_initial_to_zero"] is True
    assert module.pending_live_flush_key() is None

    scheduled_flushes[0]()

    assert len(render_calls) == 2
    assert module.pending_live_flush_key() is None


def test_stft_image_chart_widget_reuses_cached_levels_for_live_updates(
    qapp,
    monkeypatch,
):
    widget = StftImageChartWidget()
    widget.resize(640, 260)
    widget.show()
    qapp.processEvents()

    auto_levels_calls: list[bool] = []
    reused_levels: list[tuple[float, float]] = []

    monkeypatch.setattr(
        widget._image_item,
        "setImage",
        lambda image, autoLevels=False: auto_levels_calls.append(bool(autoLevels)),
    )
    monkeypatch.setattr(widget._image_item, "getLevels", lambda: (1.5, 9.5))
    monkeypatch.setattr(
        widget._image_item,
        "setLevels",
        lambda levels: reused_levels.append((float(levels[0]), float(levels[1]))),
    )
    monkeypatch.setattr(widget._image_item, "setRect", lambda rect: None)

    widget.set_image(
        times_sec=[0.0],
        frequencies_hz=[100.0, 200.0],
        magnitude_db=np.array([[1.0], [2.0]]),
        reset_levels=True,
    )
    widget.set_image(
        times_sec=[0.0, 0.5],
        frequencies_hz=[100.0, 200.0],
        magnitude_db=np.array([[1.0, 3.0], [2.0, 4.0]]),
        reset_levels=False,
    )

    assert auto_levels_calls == [True, False]
    assert reused_levels == [(1.5, 9.5)]
