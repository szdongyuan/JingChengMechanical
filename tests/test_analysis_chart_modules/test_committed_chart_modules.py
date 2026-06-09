from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis.frames import StftFrame
from app.services_container import (
    FactoryControllerState,
    FactoryReviewPayload,
    FactoryReviewWindow,
)
from ui.modules.loudness import LoudnessModule
from ui.modules.octave import OctaveModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.modules.stft import StftModule

from ._support import (
    _assert_empty_chart_visible,
    _assert_x_range,
    _make_services,
    _review_payload_with_chart_payload,
    _state,
)


def _with_loudness_payload(
    review_payload: FactoryReviewPayload,
    *,
    units_by_channel: dict[int, str] | None = None,
) -> FactoryReviewPayload:
    units = units_by_channel or {}
    for channel_index, channel_payload in review_payload.chart_payload.items():
        unit = units.get(channel_index, "sone")
        if unit == "phon":
            channel_payload["loudness"] = {
                "times_sec": [0.0, 1.0],
                "values": [42.0 + channel_index, 43.0 + channel_index],
                "unit": "phon",
                "lower_limit": 40.0,
                "upper_limit": 60.0,
            }
        else:
            channel_payload["loudness"] = {
                "times_sec": [0.0, 1.0],
                "values": [2.0 + channel_index, 3.0 + channel_index],
                "unit": "sone",
                "lower_limit": 0.0,
                "upper_limit": 10.0,
            }
    return review_payload


def _assert_left_axis_title_hidden_ticks_visible(widget) -> None:
    left_axis = widget.plot_item.getAxis("left")
    assert left_axis.isVisible() is True
    assert left_axis.labelText == ""
    assert left_axis.style.get("showValues") is True
    assert left_axis.style.get("tickLength") not in (None, 0)
    assert left_axis.tickStrings([0.0, 1.0], 1.0, 1.0) == ["0", "1"]
    assert widget.plot_item.getAxis("bottom").isVisible() is True


def _make_stft_frame(
    *,
    time: float,
    magnitudes: tuple[float, ...],
    frequencies: tuple[float, ...] = (100.0, 200.0),
) -> StftFrame:
    return StftFrame(
        time=time,
        frequencies=np.asarray(frequencies, dtype=float),
        magnitudes=np.asarray(magnitudes, dtype=float),
    )


def _seed_stft_cache(
    module: StftModule,
    *,
    config_id: int = 101,
    run_id: str = "run-101",
    channel_index: int = 0,
    magnitudes: tuple[tuple[float, ...], ...] = ((1.0, 2.0), (3.0, 4.0)),
) -> None:
    for frame_index, magnitude_values in enumerate(magnitudes):
        module._on_stft_updated(
            config_id,
            run_id,
            channel_index,
            _make_stft_frame(time=float(frame_index) * 0.5, magnitudes=magnitude_values),
        )


def _current_trace(
    values: tuple[float, ...],
    *,
    times: tuple[float, ...] = (0.0, 0.5),
    unit: str = "mA",
) -> dict:
    return {
        "times_sec": list(times),
        "values_a": list(values),
        "average_a": sum(values) / len(values),
        "max_a": max(values),
        "latest_a": values[-1],
        "unit": unit,
    }


def _assert_heatmap_current_trace(module: StftModule, values: list[float]) -> None:
    payload = module.canvas.heatmap.current_trace_payload()
    current_x, current_y = module.canvas.heatmap._current_curve_item.getData()
    right_axis = module.canvas.heatmap.plot_item.getAxis("right")

    assert payload is not None
    assert payload["times_sec"] == [0.0, 0.5]
    assert payload["values_a"] == values
    assert list(current_x) == [0.0, 0.5]
    assert list(current_y) == values
    assert right_axis.isVisible() is True
    assert right_axis.labelUnits == "mA"


def _assert_heatmap_current_trace_cleared(module: StftModule) -> None:
    current_x, current_y = module.canvas.heatmap._current_curve_item.getData()

    assert module.canvas.heatmap.current_trace_payload() is None
    assert current_x is None or list(current_x) == []
    assert current_y is None or list(current_y) == []
    assert module.canvas.heatmap.plot_item.getAxis("right").isVisible() is False


def test_spl_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SplModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["x_values"] == [0.0, 1.0]
    assert module.current_plot_payload()["y_values"] == [71.0, 72.0]
    assert module.current_render_channel() == 1
    assert module.plot.plot_item.getAxis("bottom").labelText == "时间 (s)"
    _assert_left_axis_title_hidden_ticks_visible(module.plot)


def test_spl_module_uses_selected_window_axis_range(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SplModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["spl"]["times_sec"] = [4.05, 4.10, 4.15]
    review_payload.chart_payload[0]["spl"]["values_db"] = [71.0, 72.0, 73.0]
    review_payload.chart_payload[0]["spl"]["x_axis_range_sec"] = [4.0, 12.0]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    _assert_x_range(module.plot, (4.0, 12.0))
    assert module.current_plot_payload()["x_values"] == [4.05, 4.10, 4.15]


def test_spl_module_keeps_axes_visibility_when_committed_payload_has_no_series_points(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SplModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["spl"] = {
        "times_sec": [],
        "values_db": [],
        "lower_limit_db": 40.0,
        "upper_limit_db": 80.0,
    }

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)
    assert module.plot.plot_item.getAxis("bottom").labelText == "时间 (s)"
    _assert_left_axis_title_hidden_ticks_visible(module.plot)


def test_loudness_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = LoudnessModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _with_loudness_payload(
            _review_payload_with_chart_payload(101, channels=(0, 1)),
            units_by_channel={1: "phon"},
        )
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["times_sec"] == [0.0, 1.0]
    assert module.current_plot_payload()["values"] == [43.0, 44.0]
    assert module.current_plot_payload()["unit"] == "phon"
    assert module.current_plot_payload()["lower_limit"] == 40.0
    assert module.current_plot_payload()["upper_limit"] == 60.0
    assert module.current_render_channel() == 1
    assert module.plot.plot_item.getAxis("bottom").labelText == "时间 (s)"
    _assert_left_axis_title_hidden_ticks_visible(module.plot)


def test_loudness_module_uses_selected_window_axis_range(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = LoudnessModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _with_loudness_payload(
        _review_payload_with_chart_payload(101, channels=(0,))
    )
    review_payload.chart_payload[0]["loudness"]["times_sec"] = [4.0, 4.04, 4.08]
    review_payload.chart_payload[0]["loudness"]["values"] = [2.8, 2.9, 3.0]
    review_payload.chart_payload[0]["loudness"]["x_axis_range_sec"] = [4.0, 12.0]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    _assert_x_range(module.plot, (4.0, 12.0))
    assert module.current_plot_payload()["times_sec"] == [4.0, 4.04, 4.08]


@pytest.mark.parametrize("module_cls", (SplModule, OctaveModule, LoudnessModule))
def test_committed_chart_modules_hide_left_axis_title_but_keep_ticks(qapp, module_cls):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    if module_cls is LoudnessModule:
        review_payload = _with_loudness_payload(review_payload)

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    _assert_left_axis_title_hidden_ticks_visible(module.plot)


def test_spectrum_module_keeps_chinese_axis_labels_visible(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    bottom_axis = module.plot.plot_item.getAxis("bottom")
    left_axis = module.plot.plot_item.getAxis("left")
    assert bottom_axis.labelText == "频率"
    assert bottom_axis.labelUnits == "Hz"
    assert left_axis.labelText == "幅值"
    assert left_axis.labelUnits == "dB"


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule, LoudnessModule))
def test_display_reset_keeps_committed_chart_controller_without_selected_api(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    try:
        review_payload = _review_payload_with_chart_payload(101, channels=(0,))
        if module_cls is LoudnessModule:
            review_payload = _with_loudness_payload(review_payload)
        services.bus.factory_state_changed.emit(_state(101, "reviewing"))
        services.bus.factory_review_changed.emit(review_payload)
        qapp.processEvents()

        services.bus.factory_display_reset.emit("manual")
        qapp.processEvents()

        assert module.plot.chart_interactions is not None
        assert not hasattr(module.plot.chart_interactions, "selected_x_range")
    finally:
        module.close()


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule, LoudnessModule))
def test_channel_refresh_keeps_committed_chart_controller_without_selected_api(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    selected_channel = {"value": 0}
    module.selected_display_channel_provider = lambda: selected_channel["value"]
    try:
        review_payload = _review_payload_with_chart_payload(101, channels=(0, 1))
        if module_cls is LoudnessModule:
            review_payload = _with_loudness_payload(review_payload)
        services.bus.factory_state_changed.emit(_state(101, "reviewing"))
        services.bus.factory_review_changed.emit(review_payload)
        qapp.processEvents()

        selected_channel["value"] = 1
        module.refresh_selected_display_channel()

        assert module.plot.chart_interactions is not None
        assert not hasattr(module.plot.chart_interactions, "selected_x_range")
    finally:
        module.close()


def test_stft_module_keeps_chinese_axis_labels_visible(qapp):
    services = _make_services(initial_state=_state(101, "running"))
    module = StftModule(services)

    try:
        module.show()
        qapp.processEvents()

        bottom_axis = module.canvas.heatmap.plot_item.getAxis("bottom")
        left_axis = module.canvas.heatmap.plot_item.getAxis("left")
        assert bottom_axis.labelText == "时间"
        assert bottom_axis.labelUnits == "s"
        assert left_axis.labelText == "频率"
        assert left_axis.labelUnits == "Hz"
    finally:
        module.close()


def test_stft_module_passes_committed_current_trace_to_heatmap(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, channel_index=0)
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["current_trace"] = _current_trace((1.2, 1.6))

    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    _assert_heatmap_current_trace(module, [1.2, 1.6])


def test_stft_module_ignores_current_trace_for_mismatched_displayed_config_id(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, channel_index=0)
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["current_trace"] = _current_trace((1.2, 1.6))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    rendered_payload = module.current_image_payload()
    assert rendered_payload is not None
    rendered_matrix = rendered_payload["magnitude_db"]
    _assert_heatmap_current_trace(module, [1.2, 1.6])

    stale_review_payload = _review_payload_with_chart_payload(
        202,
        run_id="run-101",
        channels=(0,),
    )
    stale_review_payload.chart_payload[0]["current_trace"] = _current_trace((2.2, 2.6))

    services.bus.factory_review_changed.emit(stale_review_payload)
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    stale_image_payload = module.current_image_payload()
    assert stale_image_payload is not None
    assert stale_image_payload["magnitude_db"] is rendered_matrix
    assert stale_image_payload["times_sec"] == rendered_payload["times_sec"]
    assert stale_image_payload["frequencies_hz"] == rendered_payload["frequencies_hz"]
    _assert_heatmap_current_trace_cleared(module)


def test_stft_module_ignores_current_trace_for_mismatched_run_id(qapp):
    services = _make_services(initial_state=_state(101, "running"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, run_id="run-current", channel_index=0)
    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    review_payload = _review_payload_with_chart_payload(
        101,
        run_id="run-stale",
        channels=(0,),
    )
    review_payload.chart_payload[0]["current_trace"] = _current_trace((2.2, 2.6))

    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-current", 0)
    assert module.current_image_payload() is not None
    _assert_heatmap_current_trace_cleared(module)


def test_stft_module_clears_current_trace_when_leaving_review_context(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, channel_index=0)
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["current_trace"] = _current_trace((1.2, 1.6))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()
    _assert_heatmap_current_trace(module, [1.2, 1.6])

    services.bus.factory_state_changed.emit(_state(101, "running"))
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    assert module.current_image_payload() is not None
    _assert_heatmap_current_trace_cleared(module)


def test_stft_module_clears_current_trace_on_review_reset(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, channel_index=0)
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["current_trace"] = _current_trace((1.2, 1.6))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()
    _assert_heatmap_current_trace(module, [1.2, 1.6])

    services.bus.factory_review_changed.emit(FactoryReviewPayload())
    qapp.processEvents()

    _assert_heatmap_current_trace_cleared(module)


def test_stft_module_updates_current_trace_when_selected_channel_changes(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    selected_channel = {"value": 0}
    module.selected_display_channel_provider = lambda: selected_channel["value"]
    _seed_stft_cache(module, channel_index=0)
    _seed_stft_cache(
        module,
        channel_index=1,
        magnitudes=((5.0, 6.0), (7.0, 8.0)),
    )
    review_payload = _review_payload_with_chart_payload(101, channels=(0, 1))
    review_payload.chart_payload[0]["current_trace"] = _current_trace((1.2, 1.6))
    review_payload.chart_payload[1]["current_trace"] = _current_trace((3.2, 3.6))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 0)
    _assert_heatmap_current_trace(module, [1.2, 1.6])

    selected_channel["value"] = 1
    module.refresh_selected_display_channel()
    qapp.processEvents()

    assert module.current_render_cache_key() == (101, "run-101", 1)
    _assert_heatmap_current_trace(module, [3.2, 3.6])


def test_stft_module_updates_current_trace_without_redrawing_heatmap(qapp, monkeypatch):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, channel_index=0)
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["current_trace"] = _current_trace((1.2, 1.6))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    cache_key = (101, "run-101", 0)
    cache_entry = module._stft_cache[cache_key]
    initial_revision = cache_entry.render_revision
    assert module.current_render_cache_key() == cache_key
    _assert_heatmap_current_trace(module, [1.2, 1.6])

    set_image_calls: list[dict[str, object]] = []
    real_set_image = module.canvas.heatmap.set_image

    def _spy_set_image(**kwargs):
        set_image_calls.append(kwargs)
        real_set_image(**kwargs)

    monkeypatch.setattr(module.canvas.heatmap, "set_image", _spy_set_image)

    updated_review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    updated_review_payload.chart_payload[0]["current_trace"] = _current_trace(
        (4.2, 4.6)
    )
    services.bus.factory_review_changed.emit(updated_review_payload)
    qapp.processEvents()

    assert module.current_render_cache_key() == cache_key
    assert cache_entry.render_revision == initial_revision
    assert set_image_calls == []
    _assert_heatmap_current_trace(module, [4.2, 4.6])


@pytest.mark.parametrize(
    ("trigger_source", "run_id", "judgment_payload"),
    [
        (
            "manual",
            "run-101-reanalysis",
            {"overall": "合格", "source": "reanalysis", "analysis_window": (1.0, 2.0)},
        ),
        (
            "import_audio",
            "import-101",
            {"overall": "合格", "analysis_window": (1.0, 2.0)},
        ),
    ],
)
def test_stft_committed_current_trace_keeps_full_duration_with_selected_analysis_window(
    qapp,
    trigger_source,
    run_id,
    judgment_payload,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = StftModule(services)
    module.selected_display_channel_provider = lambda: 0
    _seed_stft_cache(module, run_id=run_id, channel_index=0)
    review_payload = _review_payload_with_chart_payload(
        101,
        run_id=run_id,
        channels=(0,),
    )
    review_payload = replace(
        review_payload,
        trigger_source=trigger_source,
        default_window=FactoryReviewWindow(start_sec=1.0, end_sec=2.0),
        judgment_payload=judgment_payload,
    )
    review_payload.chart_payload[0]["current_trace"] = _current_trace(
        (1.0, 2.0, 3.0),
        times=(0.0, 1.5, 4.0),
    )

    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    payload = module.canvas.heatmap.current_trace_payload()
    assert module.selected_window == pytest.approx((1.0, 2.0))
    assert payload is not None
    assert payload["times_sec"] == [0.0, 1.5, 4.0]
    assert payload["values_a"] == [1.0, 2.0, 3.0]


def test_loudness_module_uses_spl_style_y_range_so_limit_lines_are_visible(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = LoudnessModule(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _with_loudness_payload(_review_payload_with_chart_payload(101, channels=(0,)))
    )
    qapp.processEvents()

    y_min, y_max = module.plot.plot_item.vb.viewRange()[1]
    payload = module.current_plot_payload()
    assert module.has_committed_plot_data() is True
    assert payload["lower_limit"] == 0.0
    assert payload["upper_limit"] == 10.0
    assert y_min <= 0.0
    assert y_max >= 10.0


@pytest.mark.parametrize(
    ("module_cls", "payload_key", "expected_field", "expected_values"),
    [
        (SplModule, "spl", "y_values", [76.0, 77.0]),
        (SpectrumModule, "fft", "levels_db", [46.0, 48.0]),
        (OctaveModule, "octave", "levels_db", [38.0, 40.0]),
    ],
)
def test_imported_review_chart_modules_render_chart_only_channel(
    qapp,
    module_cls,
    payload_key,
    expected_field,
    expected_values,
):
    services = _make_services(
        initial_state=FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="import_audio",
        )
    )
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 6
    review_payload = _review_payload_with_chart_payload(
        101,
        run_id="import_101_test",
        channels=(0, 6),
    )
    review_payload = replace(review_payload, trigger_source="import_audio")

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="import_audio",
        )
    )
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 6
    assert module.current_plot_payload()[expected_field] == expected_values


def test_imported_review_loudness_module_renders_chart_only_channel(qapp):
    services = _make_services(
        initial_state=FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="import_audio",
        )
    )
    module = LoudnessModule(services)
    module.selected_display_channel_provider = lambda: 6
    review_payload = _with_loudness_payload(
        _review_payload_with_chart_payload(
            101,
            run_id="import_101_test",
            channels=(0, 6),
        ),
        units_by_channel={6: "phon"},
    )
    review_payload = replace(review_payload, trigger_source="import_audio")

    services.bus.factory_state_changed.emit(
        FactoryControllerState(
            displayed_config_id=101,
            run_phase="reviewing",
            trigger_source="import_audio",
        )
    )
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 6
    assert module.current_plot_payload()["values"] == [48.0, 49.0]
    assert module.current_plot_payload()["unit"] == "phon"


def test_spectrum_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["frequencies_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["levels_db"] == [41.0, 43.0]
    assert module.current_render_channel() == 1


def test_spectrum_module_ignores_committed_current_trace(qapp, monkeypatch):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    current_trace = {
        "times_sec": [0.0, 0.2],
        "values_a": [1.2, 1.6],
        "average_a": 1.4,
        "max_a": 1.6,
        "latest_a": 1.6,
        "unit": "mA",
    }
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["current_trace"] = current_trace
    review_payload.chart_payload[0]["fft"]["current_trace"] = current_trace
    captured_kwargs: dict[str, object] = {}
    real_set_spectrum = module.plot.set_spectrum

    def _spy_set_spectrum(**kwargs):
        captured_kwargs.update(kwargs)
        return real_set_spectrum(**kwargs)

    monkeypatch.setattr(module.plot, "set_spectrum", _spy_set_spectrum)

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert "current_trace" not in captured_kwargs
    assert "current_trace" not in module.current_plot_payload()
    assert module.plot.plot_item.getAxis("right").isVisible() is False


def test_spectrum_module_passes_selected_channel_fft_x_axis_scale(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    services.config_service = SimpleNamespace(
        load_config=MagicMock(
            return_value={
                "channels": [
                    {"channel_index": 0, "fft_x_axis_scale": "log"},
                    {"channel_index": 1, "fft_x_axis_scale": "linear"},
                ],
                "selected_channels": [0, 1],
            }
        )
    )
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.current_plot_payload()["x_axis_scale"] == "log"
    services.config_service.load_config.assert_called_with(101)


def test_spectrum_module_defaults_fft_axis_to_linear_when_config_load_fails(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    services.config_service = SimpleNamespace(
        load_config=MagicMock(side_effect=RuntimeError("db offline"))
    )
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0,))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["frequencies_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["levels_db"] == [40.0, 42.0]
    assert module.current_plot_payload()["x_axis_scale"] == "linear"
    services.config_service.load_config.assert_called_with(101)


def test_spectrum_module_uses_committed_fft_axis_scale_when_config_load_fails(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    services.config_service = SimpleNamespace(
        load_config=MagicMock(side_effect=RuntimeError("db offline"))
    )
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"]["x_axis_scale"] = "log"

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["x_axis_scale"] == "log"
    assert module.plot.plot_item.getViewBox().state["logMode"][0] is True
    services.config_service.load_config.assert_not_called()


def test_spectrum_module_passes_committed_fft_limit_envelope_to_chart(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    envelope = {
        "control_points": [
            {"freq_hz": 125.0, "lower_db": 35.0, "upper_db": 45.0},
            {"freq_hz": 250.0, "lower_db": 37.0, "upper_db": 43.0},
        ],
        "failed_points": [
            {
                "freq_hz": 250.0,
                "actual_db": 47.0,
                "lower_db": 37.0,
                "upper_db": 43.0,
            },
        ],
    }
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"]["limit_envelope"] = envelope

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["limit_envelope"] == envelope


def test_spectrum_module_consumes_golden_overlay_fields_from_committed_payload(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"].update(
        {
            "golden_mode": "absolute",
            "golden_overlay_levels_db": [38.0, 41.0],
            "show_zero_reference": False,
        }
    )

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["golden_overlay_levels_db"] == [38.0, 41.0]
    assert module.current_plot_payload()["show_zero_reference"] is False
    overlay_x, overlay_y = module.plot._golden_overlay_curve_item.getData()
    assert list(overlay_x) == [125.0, 250.0]
    assert list(overlay_y) == [38.0, 41.0]


def test_spectrum_module_renders_committed_chart_without_limit_band_markers(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"]["limit_bands"] = []

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["frequencies_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["limit_bands"] == []
    module.plot._band_range_item.boundingRect()


def test_spectrum_module_keeps_two_committed_limit_band_markers_renderable(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["fft"]["limit_bands"] = [
        {
            "freq_hz": 125.0,
            "lower_db": 35.0,
            "upper_db": 45.0,
            "actual_db": 40.0,
            "passed": True,
        },
        {
            "freq_hz": 250.0,
            "lower_db": 37.0,
            "upper_db": 43.0,
            "actual_db": 42.0,
            "passed": True,
        },
    ]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert len(module.current_plot_payload()["limit_bands"]) == 2
    module.plot._band_range_item.boundingRect()


def test_spectrum_module_keeps_committed_one_sided_limit_band_markers(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    limit_bands = [
        {
            "freq_hz": 125.0,
            "lower_db": None,
            "upper_db": 45.0,
            "actual_db": 48.0,
            "passed": False,
        },
        {
            "freq_hz": 250.0,
            "lower_db": 37.0,
            "upper_db": None,
            "actual_db": 35.0,
            "passed": False,
        },
    ]
    review_payload.chart_payload[0]["fft"]["limit_bands"] = limit_bands

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    marker_x, marker_y = module.plot._band_marker_item.getData()
    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["limit_bands"] == limit_bands
    assert list(marker_x) == [125.0, 250.0]
    assert list(marker_y) == [48.0, 35.0]


def test_spectrum_module_keeps_selected_channel_when_fft_payload_is_missing(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = SpectrumModule(services)
    fallback_calls: list[int] = []
    module.selected_display_channel_provider = lambda: 1
    module.selected_display_channel_sync = fallback_calls.append
    review_payload = _review_payload_with_chart_payload(101, channels=(0, 1))
    del review_payload.chart_payload[1]["fft"]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is False
    assert module.current_plot_payload() is None
    assert module.current_render_channel() == 1
    assert module.plot.isHidden() is False
    assert fallback_calls == []


def test_octave_module_renders_committed_chart_for_selected_channel(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 1

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["center_freqs_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["levels_db"] == [33.0, 35.0]
    assert module.current_render_channel() == 1


def test_octave_module_consumes_golden_overlay_fields_from_committed_payload(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["octave"].update(
        {
            "golden_mode": "absolute",
            "golden_overlay_levels_db": [30.0, 33.0],
            "show_zero_reference": False,
        }
    )

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["golden_overlay_levels_db"] == [30.0, 33.0]
    assert module.current_plot_payload()["show_zero_reference"] is False
    overlay_x, overlay_y = module.plot._golden_overlay_item.getData()
    assert list(overlay_x) == [0.0, 1.0]
    assert list(overlay_y) == [30.0, 33.0]


def test_octave_module_passes_committed_limit_lines_to_chart(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    limit_lines = [
        {
            "kind": "upper",
            "points": [
                {"freq_hz": 125.0, "limit_db": 40.0},
                {"freq_hz": 250.0, "limit_db": 40.0},
            ],
        }
    ]
    review_payload.chart_payload[0]["octave"]["limit_lines"] = limit_lines

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["limit_lines"] == limit_lines


def test_octave_module_renders_committed_chart_without_limit_band_markers(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["octave"]["limit_bands"] = []

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["center_freqs_hz"] == [125.0, 250.0]
    assert module.current_plot_payload()["limit_bands"] == []
    module.plot._band_range_item.boundingRect()


def test_octave_module_keeps_two_committed_limit_band_markers_renderable(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    review_payload.chart_payload[0]["octave"]["limit_bands"] = [
        {
            "freq_hz": 125.0,
            "lower_db": 30.0,
            "upper_db": 36.0,
            "actual_db": 32.0,
            "passed": True,
        },
        {
            "freq_hz": 250.0,
            "lower_db": 30.0,
            "upper_db": 40.0,
            "actual_db": 34.0,
            "passed": True,
        },
    ]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert len(module.current_plot_payload()["limit_bands"]) == 2
    module.plot._band_range_item.boundingRect()


def test_octave_module_keeps_committed_one_sided_limit_band_markers(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    module.selected_display_channel_provider = lambda: 0
    review_payload = _review_payload_with_chart_payload(101, channels=(0,))
    limit_bands = [
        {
            "freq_hz": 125.0,
            "lower_db": None,
            "upper_db": 36.0,
            "actual_db": 38.0,
            "passed": False,
        },
        {
            "freq_hz": 250.0,
            "lower_db": 30.0,
            "upper_db": None,
            "actual_db": 28.0,
            "passed": False,
        },
    ]
    review_payload.chart_payload[0]["octave"]["limit_bands"] = limit_bands

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    marker_x, marker_y = module.plot._band_marker_item.getData()
    assert module.has_committed_plot_data() is True
    assert module.current_plot_payload()["limit_bands"] == limit_bands
    assert list(marker_x) == [0.0, 1.0]
    assert list(marker_y) == [38.0, 28.0]


def test_octave_module_keeps_selected_channel_when_octave_payload_is_missing(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = OctaveModule(services)
    fallback_calls: list[int] = []
    module.selected_display_channel_provider = lambda: 1
    module.selected_display_channel_sync = fallback_calls.append
    review_payload = _review_payload_with_chart_payload(101, channels=(0, 1))
    del review_payload.chart_payload[1]["octave"]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is False
    assert module.current_plot_payload() is None
    assert module.current_render_channel() == 1
    assert module.plot.isHidden() is False
    assert fallback_calls == []


def test_loudness_module_keeps_selected_channel_without_fallback_when_only_loudness_payload_is_missing(
    qapp,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = LoudnessModule(services)
    fallback_calls: list[int] = []
    module.selected_display_channel_provider = lambda: 1
    module.selected_display_channel_sync = fallback_calls.append
    review_payload = _with_loudness_payload(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    del review_payload.chart_payload[1]["loudness"]

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)
    assert fallback_calls == []


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule, LoudnessModule))
def test_committed_chart_modules_keep_review_payload_visible_in_stopped_phase(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 1
    review_payload = _review_payload_with_chart_payload(101, channels=(0, 1))
    if module_cls is LoudnessModule:
        review_payload = _with_loudness_payload(review_payload)

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(review_payload)
    qapp.processEvents()

    assert module.has_committed_plot_data() is True

    services.bus.factory_state_changed.emit(_state(101, "stopped"))
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 1


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule, LoudnessModule))
def test_committed_chart_modules_stay_empty_in_reviewing_when_chart_payload_is_missing(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        FactoryReviewPayload(
            displayed_config_id=101,
            run_id="run-101",
            trigger_source="manual",
            summary_payload={
                "overall_status": "no_result",
                "overall_label": "无结果",
                "error_message": "analysis unavailable",
                "rows": [],
            },
            chart_payload=None,
        )
    )
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule, LoudnessModule))
@pytest.mark.parametrize(
    "initial_state",
    (
        FactoryControllerState(),
        _state(101, "running"),
        _state(101, "analyzing"),
        _state(101, "reanalyzing"),
    ),
)
def test_committed_chart_modules_keep_empty_chart_visible_in_no_data_phases(
    qapp,
    module_cls,
    initial_state,
):
    services = _make_services(initial_state=initial_state)
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0

    _assert_empty_chart_visible(module, qapp)


@pytest.mark.parametrize(
    ("module_cls", "live_signal_name"),
    (
        (SplModule, "spl_updated"),
        (SpectrumModule, "fft_updated"),
        (OctaveModule, "octave_updated"),
    ),
)
def test_chart_modules_ignore_live_frames_and_clear_when_reanalysis_starts(
    qapp,
    module_cls,
    live_signal_name,
):
    services = _make_services(initial_state=_state(101, "running"))
    module = module_cls(services)
    module.selected_display_channel_provider = lambda: 0

    services.bus.factory_state_changed.emit(_state(101, "running"))
    getattr(services.bus, live_signal_name).emit(101, "run-101", 0, object())
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True

    services.bus.factory_state_changed.emit(_state(101, "reanalyzing"))
    qapp.processEvents()

    _assert_empty_chart_visible(module, qapp)


@pytest.mark.parametrize("module_cls", (SplModule, SpectrumModule, OctaveModule))
def test_chart_module_falls_back_to_first_committed_channel_before_empty_state(
    qapp,
    module_cls,
):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = module_cls(services)
    fallback_calls: list[int] = []
    module.selected_display_channel_provider = lambda: 1
    module.selected_display_channel_sync = fallback_calls.append

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(2, 0))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 2
    assert fallback_calls == [2]


def test_loudness_module_falls_back_to_first_committed_channel_before_empty_state(qapp):
    services = _make_services(initial_state=_state(101, "reviewing"))
    module = LoudnessModule(services)
    fallback_calls: list[int] = []
    module.selected_display_channel_provider = lambda: 1
    module.selected_display_channel_sync = fallback_calls.append

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _with_loudness_payload(
            _review_payload_with_chart_payload(101, channels=(2, 0))
        )
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 2
    assert fallback_calls == [2]
