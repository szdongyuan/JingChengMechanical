from __future__ import annotations

from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryRuntimeConfigPayload,
)
from tests.test_stft_module._support import (
    _make_module,
    _make_stft_frame,
    _seed_stft_history,
    _show,
)


def _current_trace(values=(1.2, 1.4), times=(0.0, 1.0)):
    return {
        "times_sec": list(times),
        "values_a": list(values),
        "average_a": sum(values) / len(values),
        "max_a": max(values),
        "latest_a": values[-1],
        "unit": "mA",
    }


def _review_chart_without_current_trace():
    return {
        0: {
            "stft": {
                "times_sec": [0.0, 1.0],
                "frequencies_hz": [100.0, 200.0],
                "magnitude_db": [[1.0, 2.0], [3.0, 4.0]],
            },
            "stft_score": {"score": 87.0},
        }
    }


def test_stft_applies_live_current_trace_for_displayed_run(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")

    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    payload = module.canvas.heatmap.current_trace_payload()
    assert payload is not None
    assert payload["values_a"] == [1.2, 1.6]


def test_review_without_current_trace_falls_back_to_live_current_for_same_run(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")

    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.2, 1.6]

    services.bus.factory_review_changed.emit(
        FactoryReviewPayload(
            displayed_config_id=101,
            run_id="run-101",
            chart_payload=_review_chart_without_current_trace(),
        )
    )
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )
    qapp.processEvents()

    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.2, 1.6]


def test_review_current_trace_fallback_requires_same_run(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")

    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    services.bus.factory_review_changed.emit(
        FactoryReviewPayload(
            displayed_config_id=101,
            run_id="run-102",
            chart_payload=_review_chart_without_current_trace(),
        )
    )
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="reviewing")
    )
    qapp.processEvents()

    assert module.canvas.heatmap.current_trace_payload() is None


def test_stft_ignores_live_current_trace_for_other_config_or_run(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")

    services.bus.current_trace_updated.emit(102, "run-101", _current_trace((2.2, 2.6)))
    services.bus.current_trace_updated.emit(101, "run-old", _current_trace((3.2, 3.6)))
    qapp.processEvents()

    assert module.canvas.heatmap.current_trace_payload() is None


def test_live_current_does_not_establish_active_run_before_stft(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="running")
    )

    services.bus.current_trace_updated.emit(101, "run-current-first", _current_trace((1.2, 1.6)))
    services.bus.stft_updated.emit(
        101,
        "run-stft",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-stft", 0)
    assert module.canvas.heatmap.current_trace_payload() is None


def test_live_current_arriving_before_first_stft_image_applies_to_matching_active_run(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="running")
    )
    module._on_factory_runtime_context(
        FactoryRuntimeConfigPayload(config_id=101, run_id="run-101")
    )

    set_image_calls = []
    clear_data_calls = []
    original_set_image = module.canvas.heatmap.set_image
    original_clear_data = module.canvas.heatmap.clear_data

    def spy_set_image(*args, **kwargs):
        set_image_calls.append((args, kwargs))
        return original_set_image(*args, **kwargs)

    def spy_clear_data(*args, **kwargs):
        clear_data_calls.append((args, kwargs))
        return original_clear_data(*args, **kwargs)

    monkeypatch.setattr(module.canvas.heatmap, "set_image", spy_set_image)
    monkeypatch.setattr(module.canvas.heatmap, "clear_data", spy_clear_data)

    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.3, 1.7)))
    assert module.canvas.heatmap.current_trace_payload() is None
    assert set_image_calls == []
    assert clear_data_calls == []

    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    qapp.processEvents()

    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.3, 1.7]


def test_live_current_arriving_before_runtime_context_is_cached_for_matching_run(
    qapp,
    monkeypatch,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="running")
    )

    set_image_calls = []
    clear_data_calls = []
    original_set_image = module.canvas.heatmap.set_image
    original_clear_data = module.canvas.heatmap.clear_data

    def spy_set_image(*args, **kwargs):
        set_image_calls.append((args, kwargs))
        return original_set_image(*args, **kwargs)

    def spy_clear_data(*args, **kwargs):
        clear_data_calls.append((args, kwargs))
        return original_clear_data(*args, **kwargs)

    monkeypatch.setattr(module.canvas.heatmap, "set_image", spy_set_image)
    monkeypatch.setattr(module.canvas.heatmap, "clear_data", spy_clear_data)

    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    assert module.current_render_cache_key() is None
    assert module._display_run_id is None
    assert module.canvas.heatmap.current_trace_payload() is None
    assert set_image_calls == []
    assert clear_data_calls == []

    services.bus.factory_runtime_start_accepted.emit(
        FactoryRuntimeConfigPayload(config_id=101, run_id="run-101")
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.2, 1.6]


def test_pending_live_current_for_stale_run_is_not_applied_after_runtime_context(
    qapp,
):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    services.bus.factory_state_changed.emit(
        FactoryControllerState(displayed_config_id=101, run_phase="running")
    )

    services.bus.current_trace_updated.emit(101, "run-old", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    assert module._display_run_id is None
    assert module.canvas.heatmap.current_trace_payload() is None

    services.bus.factory_runtime_start_accepted.emit(
        FactoryRuntimeConfigPayload(config_id=101, run_id="run-101")
    )
    services.bus.stft_updated.emit(
        101,
        "run-101",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    assert module.canvas.heatmap.current_trace_payload() is None


def test_live_current_update_does_not_redraw_existing_heatmap(qapp, monkeypatch):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")

    set_image_calls = []
    original_set_image = module.canvas.heatmap.set_image

    def spy_set_image(*args, **kwargs):
        set_image_calls.append((args, kwargs))
        return original_set_image(*args, **kwargs)

    monkeypatch.setattr(module.canvas.heatmap, "set_image", spy_set_image)
    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    assert set_image_calls == []
    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.2, 1.6]


def test_live_current_trace_is_cleared_when_runtime_switches_to_run_without_payload(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")
    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()

    module._on_factory_runtime_context(FactoryRuntimeConfigPayload(config_id=101, run_id="run-102"))
    qapp.processEvents()

    assert module.canvas.heatmap.current_trace_payload() is None


def test_live_current_trace_keeps_full_duration_when_analysis_window_is_selected(qapp):
    module, services = _make_module()
    module.selected_display_channel_provider = lambda: 0
    _show(module, qapp)
    _seed_stft_history(module, services, qapp, displayed_config_id=101, run_id="run-101")
    module._selected_window = (1.0, 2.0)

    services.bus.current_trace_updated.emit(
        101,
        "run-101",
        _current_trace(values=(1.0, 2.0, 3.0), times=(0.0, 1.5, 4.0)),
    )
    qapp.processEvents()

    payload = module.canvas.heatmap.current_trace_payload()
    assert payload["times_sec"] == [0.0, 1.5, 4.0]
    assert payload["values_a"] == [1.0, 2.0, 3.0]


def test_live_current_trace_remains_visible_when_selected_channel_changes(qapp):
    module, services = _make_module()
    selected_channel = {"value": 0}
    module.selected_display_channel_provider = lambda: selected_channel["value"]
    _show(module, qapp)
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=0,
    )
    _seed_stft_history(
        module,
        services,
        qapp,
        displayed_config_id=101,
        run_id="run-101",
        channel_index=1,
    )
    services.bus.current_trace_updated.emit(101, "run-101", _current_trace((1.2, 1.6)))
    qapp.processEvents()
    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.2, 1.6]

    selected_channel["value"] = 1
    module.refresh_selected_display_channel()
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 1)
    assert module.canvas.heatmap.current_trace_payload()["values_a"] == [1.2, 1.6]
