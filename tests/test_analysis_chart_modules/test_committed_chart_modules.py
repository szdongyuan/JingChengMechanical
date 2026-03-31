from __future__ import annotations

import pytest

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.modules.loudness import LoudnessModule
from ui.modules.octave import OctaveModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule

from ._support import (
    _assert_empty_chart_visible,
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
    assert module.current_render_channel() == 1
    assert module.plot.plot_item.getAxis("bottom").labelText == "Time (s)"
    assert module.plot.plot_item.getAxis("left").labelText == "Loudness (phon)"


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
