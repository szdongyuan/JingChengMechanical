from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services_container import FactoryControllerState, FactoryReviewPayload

REQUIRED_CHART_RUNTIME_DEPENDENCY = "pyqtgraph==0.13.7"


class _SignalStub:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _BusStub:
    def __init__(self):
        self.factory_state_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.factory_runtime_start_accepted = _SignalStub()
        self.factory_runtime_analyzing_entered = _SignalStub()
        self.factory_review_changed = _SignalStub()
        self.spl_updated = _SignalStub()
        self.fft_updated = _SignalStub()
        self.octave_updated = _SignalStub()


def _state(config_id: int, phase: str) -> FactoryControllerState:
    return FactoryControllerState(
        displayed_config_id=config_id,
        run_phase=phase,
        trigger_source="manual",
    )


def _chart_payload_for_channels(channels: tuple[int, ...]) -> dict:
    payload: dict[int, dict] = {}
    for channel_index in channels:
        payload[channel_index] = {
            "spl": {
                "times_sec": [0.0, 1.0],
                "values_db": [70.0 + channel_index, 71.0 + channel_index],
                "lower_limit_db": 60.0,
                "upper_limit_db": 80.0,
            },
            "fft": {
                "frequencies_hz": [125.0, 250.0],
                "levels_db": [40.0 + channel_index, 42.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 35.0,
                        "upper_db": 45.0,
                        "actual_db": 42.0 + channel_index,
                        "passed": True,
                    }
                ],
            },
            "octave": {
                "center_freqs_hz": [125.0, 250.0],
                "levels_db": [32.0 + channel_index, 34.0 + channel_index],
                "limit_bands": [
                    {
                        "freq_hz": 250.0,
                        "lower_db": 30.0,
                        "upper_db": 40.0,
                        "actual_db": 34.0 + channel_index,
                        "passed": True,
                    }
                ],
            },
        }
    return payload


def _review_payload_with_chart_payload(
    config_id: int,
    *,
    run_id: str = "run-101",
    channels: tuple[int, ...] = (0, 1),
) -> FactoryReviewPayload:
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        run_id=run_id,
        trigger_source="manual",
        summary_payload={"overall_status": "pass", "rows": []},
        chart_payload=_chart_payload_for_channels(channels),
    )


def _make_services(
    *,
    initial_state: FactoryControllerState | None = None,
    initial_review_payload: FactoryReviewPayload | None = None,
):
    bus = _BusStub()
    factory_mode = SimpleNamespace(
        state=initial_state or FactoryControllerState(),
        review_payload=initial_review_payload or FactoryReviewPayload(),
    )
    return SimpleNamespace(
        bus=bus,
        factory_mode=factory_mode,
    )


def _assert_empty_chart_visible(module, qapp) -> None:
    module.show()
    qapp.processEvents()

    assert module.has_committed_plot_data() is False
    assert module.current_plot_payload() is None
    assert module.current_render_channel() is None
    assert module.plot.isHidden() is False

    has_series = getattr(module.plot, "has_series", None)
    if callable(has_series):
        assert has_series() is False

    has_bars = getattr(module.plot, "has_bars", None)
    if callable(has_bars):
        assert has_bars() is False


def _show_widget(widget, qapp) -> None:
    widget.resize(640, 260)
    widget.show()
    qapp.processEvents()


def _assert_x_range(widget, expected_range: tuple[float, float]) -> None:
    x_range, _ = widget.plot_item.vb.viewRange()
    assert x_range == pytest.approx(expected_range)


def _assert_xy_range(
    widget,
    expected_x_range: tuple[float, float],
    expected_y_range: tuple[float, float],
) -> None:
    x_range, y_range = widget.plot_item.vb.viewRange()
    assert x_range == pytest.approx(expected_x_range)
    assert y_range == pytest.approx(expected_y_range)
