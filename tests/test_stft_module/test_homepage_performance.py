from __future__ import annotations

import numpy as np

from app.services_container import FactoryControllerState
from ui.modules import stft as stft_module

from ._support import _make_module, _make_stft_frame, _show


def test_stft_cache_entry_appends_frames_without_concatenate_rebuild(monkeypatch):
    cache_entry = stft_module._StftCacheEntry()

    assert cache_entry.append_frame(_make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)))

    def _fail_concatenate(*args, **kwargs):
        raise AssertionError("append should grow without concatenating the full matrix")

    monkeypatch.setattr(stft_module.np, "concatenate", _fail_concatenate)

    assert cache_entry.append_frame(_make_stft_frame(time=0.5, magnitudes=(3.0, 4.0)))

    payload = cache_entry.image_payload()
    assert payload is not None
    assert payload["times_sec"] == [0.0, 0.5]
    assert payload["frequencies_hz"] == [100.0, 200.0]
    assert np.array_equal(
        payload["magnitude_db"],
        np.array([[1.0, 3.0], [2.0, 4.0]]),
    )


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

    def _capture_set_image(*, times_sec, frequencies_hz, magnitude_db):
        captured["times_sec"] = times_sec
        captured["frequencies_hz"] = frequencies_hz
        captured["magnitude_db"] = magnitude_db

    def _fail_column_stack(*args, **kwargs):
        raise AssertionError("refresh should reuse the stored matrix directly")

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _capture_set_image)
    monkeypatch.setattr(stft_module.np, "column_stack", _fail_column_stack)

    module.refresh_selected_display_channel()

    assert captured["times_sec"] == [0.0, 0.5]
    assert captured["frequencies_hz"] == [100.0, 200.0]
    assert captured["magnitude_db"] is cached_matrix


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
