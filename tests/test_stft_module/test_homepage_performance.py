from __future__ import annotations

import numpy as np

from app.services_container import FactoryControllerState
from ui.modules import stft as stft_module
from ui.widgets.analysis_charts import StftImageChartWidget

from ._support import (
    _emit_reviewing,
    _make_module,
    _make_review_payload,
    _make_stft_frame,
    _seed_stft_history,
    _show,
)


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

    def _capture_set_image(*, times_sec, frequencies_hz, magnitude_db, reset_levels):
        captured["times_sec"] = times_sec
        captured["frequencies_hz"] = frequencies_hz
        captured["magnitude_db"] = magnitude_db
        captured["reset_levels"] = reset_levels

    def _fail_column_stack(*args, **kwargs):
        raise AssertionError("refresh should reuse the stored matrix directly")

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _capture_set_image)
    monkeypatch.setattr(stft_module.np, "column_stack", _fail_column_stack)

    module.refresh_selected_display_channel()

    assert captured["times_sec"] == [0.0, 0.5]
    assert captured["frequencies_hz"] == [100.0, 200.0]
    assert captured["magnitude_db"] is cached_matrix
    assert captured["reset_levels"] is False


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
    assert module.selection_box.isVisible() is True


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

    def _spy_set_image(*, times_sec, frequencies_hz, magnitude_db, reset_levels):
        render_calls.append(
            {
                "times_sec": list(times_sec),
                "frequencies_hz": list(frequencies_hz),
                "magnitude_db": magnitude_db,
                "reset_levels": reset_levels,
            }
        )
        real_set_image(
            times_sec=times_sec,
            frequencies_hz=frequencies_hz,
            magnitude_db=magnitude_db,
            reset_levels=reset_levels,
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

    def _capture_set_image(*, times_sec, frequencies_hz, magnitude_db, reset_levels):
        render_calls.append(
            {
                "times_sec": list(times_sec),
                "frequencies_hz": list(frequencies_hz),
                "magnitude_db": magnitude_db,
                "reset_levels": reset_levels,
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

    def _capture_set_image(*, times_sec, frequencies_hz, magnitude_db, reset_levels):
        render_calls.append(
            {
                "times_sec": list(times_sec),
                "frequencies_hz": list(frequencies_hz),
                "magnitude_db": magnitude_db,
                "reset_levels": reset_levels,
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
    assert len(scheduled_flushes) == 1
    assert module.pending_live_flush_key() == (101, "run-101", 0)

    module.refresh_selected_display_channel()

    assert len(render_calls) == 2
    assert render_calls[1]["times_sec"] == [0.0, 0.5]
    assert render_calls[1]["reset_levels"] is False
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
