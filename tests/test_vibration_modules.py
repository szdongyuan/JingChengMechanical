from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.modules.vibration import VibrationAxisModule, VibrationTrendModule


_FORBIDDEN_VIBRATION_TEXT = (
    "Pa",
    "dB SPL",
    "SPL",
    "sone",
    "m/s^2",
    "m/s²",
    "mm/s",
    "声压",
    "响度",
    "速度",
    "位移",
)


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
        self.factory_review_changed = _SignalStub()
        self.factory_display_reset = _SignalStub()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _services(
    *,
    state: FactoryControllerState | None = None,
    review: FactoryReviewPayload | None = None,
):
    return SimpleNamespace(
        bus=_BusStub(),
        factory_mode=SimpleNamespace(
            state=state or FactoryControllerState(),
            review_payload=review or FactoryReviewPayload(),
        ),
    )


def _assert_no_forbidden_vibration_text(widget):
    visible_text = " ".join(
        label.text()
        for label in widget.findChildren(QLabel)
        if label.text()
    )
    for forbidden in _FORBIDDEN_VIBRATION_TEXT:
        assert forbidden not in visible_text


def _visible_label_text(widget):
    return " ".join(
        label.text()
        for label in widget.findChildren(QLabel)
        if label.text()
    )


def _review_payload(config_id=101):
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        chart_payload={
            1: {
                "vibration": {
                    "channel_index": 1,
                    "sample_rate": 1000.0,
                    "overall_rms": 1.25,
                    "overall_peak": 3.5,
                    "overall_crest_factor": 2.8,
                    "dominant_frequency_hz": 42.0,
                    "times_sec": [0.05, 0.10],
                    "rms_values": [1.0, 1.5],
                    "peak_values": [2.0, 3.0],
                    "crest_factors": [2.0, 2.0],
                    "frequency_times_sec": [0.05, 0.10],
                    "fundamental_frequency_values_hz": [41.5, 42.5],
                    "dominant_frequency_values_hz": [83.0, 85.0],
                    "spectral_centroid_values_hz": [43.0, 43.4],
                    "frequency_bandwidth_values_hz": [2.0, 2.5],
                    "calibration_value": 9.0,
                }
            }
        },
    )


def _enhanced_review_payload(config_id=101):
    return FactoryReviewPayload(
        displayed_config_id=config_id,
        chart_payload={
            1: {
                "vibration": {
                    "overall_rms": 1.25,
                    "overall_peak": 3.5,
                    "overall_crest_factor": 2.8,
                    "dominant_frequency_hz": 42.0,
                    "low_frequency_energy_share": 0.36,
                    "times_sec": [0.05, 0.10, 0.15, 0.20],
                    "rms_values": [1.0, 1.2, 1.1, 1.5],
                    "peak_values": [1.8, 2.1, 2.0, 3.0],
                    "frequency_times_sec": [0.05, 0.10, 0.15, 0.20],
                    "fundamental_frequency_values_hz": [41.8, 42.0, 42.2, 42.0],
                    "dominant_frequency_values_hz": [83.6, 84.0, 84.4, 84.0],
                    "spectral_centroid_values_hz": [43.0, 43.1, 43.0, 43.2],
                    "frequency_bandwidth_values_hz": [2.0, 2.1, 2.2, 2.1],
                }
            }
        },
    )


def test_vibration_axis_module_renders_selected_channel_payload(qapp):
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=_review_payload(101),
    )
    module = VibrationAxisModule(services)
    module.selected_display_channel_provider = lambda: 1

    module.refresh_selected_display_channel()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 1
    assert module.current_payload()["overall_rms"] == pytest.approx(1.25)
    assert module.plot.series_payload()["y_values"] == pytest.approx([41.5, 42.5])
    assert module.current_fundamental_frequency_values() == pytest.approx([41.5, 42.5])
    assert "42" in module.label_dominant_mean.text()
    _assert_no_forbidden_vibration_text(module)


def test_vibration_axis_module_exposes_frequency_stability_indicators(qapp):
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=_enhanced_review_payload(101),
    )
    module = VibrationAxisModule(services)
    module.selected_display_channel_provider = lambda: 1

    module.refresh_selected_display_channel()

    visible = _visible_label_text(module)
    for text in ("主频均值", "主频抖动"):
        assert text in visible
    for removed_text in ("振动能量", "频率集中度", "低频占比", "峰值因子"):
        assert removed_text not in visible
    _assert_no_forbidden_vibration_text(module)
    snapshot = module.indicator_snapshot()
    assert set(snapshot) >= {"fundamental_mean_hz", "fundamental_jitter_hz"}
    assert snapshot["fundamental_mean_hz"] == pytest.approx(42.0)
    assert snapshot["fundamental_jitter_hz"] < 0.2


def test_vibration_trend_module_renders_rms_and_peak_series(qapp):
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=_review_payload(101),
    )
    module = VibrationTrendModule(services)
    module.selected_display_channel_provider = lambda: 1

    module.refresh_selected_display_channel()

    assert module.has_committed_plot_data() is True
    assert module.current_render_channel() == 1
    assert module.plot.series_payload()["x_values"] == pytest.approx([0.05, 0.10])
    assert module.plot.series_payload()["y_values"] == pytest.approx([1.0, 1.5])
    assert module.current_peak_values() == pytest.approx([2.0, 3.0])
    _assert_no_forbidden_vibration_text(module)


def test_vibration_trend_module_exposes_pulse_indicators(qapp):
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=_enhanced_review_payload(101),
    )
    module = VibrationTrendModule(services)
    module.selected_display_channel_provider = lambda: 1

    module.refresh_selected_display_channel()

    visible = _visible_label_text(module)
    for text in ("脉冲强度", "冲击密度", "峰值包络"):
        assert text in visible
    snapshot = module.pulse_snapshot()
    assert set(snapshot) >= {"impulse", "impact_density", "recent_peak"}
    assert 0.0 <= snapshot["impulse"] <= 1.0
    assert 0.0 <= snapshot["impact_density"] <= 1.0
    assert module.current_peak_values() == pytest.approx([1.8, 2.1, 2.0, 3.0])
    _assert_no_forbidden_vibration_text(module)


def test_vibration_trend_module_y_range_includes_peak_overlay(qapp):
    review = _review_payload(101)
    vibration_payload = review.chart_payload[1]["vibration"]
    vibration_payload["rms_values"] = [1.0, 1.5]
    vibration_payload["peak_values"] = [2.0, 12.0]
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=review,
    )
    module = VibrationTrendModule(services)
    module.selected_display_channel_provider = lambda: 1

    module.refresh_selected_display_channel()
    qapp.processEvents()

    _, y_range = module.plot.plot_item.vb.viewRange()
    assert y_range[1] >= max(vibration_payload["peak_values"])


def test_vibration_trend_reset_view_keeps_peak_overlay_in_y_range(qapp):
    review = _review_payload(101)
    vibration_payload = review.chart_payload[1]["vibration"]
    vibration_payload["rms_values"] = [1.0, 1.5]
    vibration_payload["peak_values"] = [2.0, 12.0]
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=review,
    )
    module = VibrationTrendModule(services)
    module.selected_display_channel_provider = lambda: 1

    module.refresh_selected_display_channel()
    qapp.processEvents()
    module.plot.chart_interactions.reset_view()

    _, y_range = module.plot.plot_item.vb.viewRange()
    assert y_range[1] >= max(vibration_payload["peak_values"])


def test_vibration_modules_clear_when_payload_is_missing(qapp):
    valid_review = _enhanced_review_payload(101)
    services = _services(
        state=FactoryControllerState(displayed_config_id=101, run_phase="reviewing"),
        review=valid_review,
    )
    axis = VibrationAxisModule(services)
    trend = VibrationTrendModule(services)
    axis.selected_display_channel_provider = lambda: 1
    trend.selected_display_channel_provider = lambda: 1

    axis.refresh_selected_display_channel()
    trend.refresh_selected_display_channel()
    assert axis.has_committed_plot_data() is True
    assert trend.has_committed_plot_data() is True

    services.bus.factory_review_changed.emit(
        FactoryReviewPayload(displayed_config_id=101, chart_payload={1: {}})
    )

    assert axis.has_committed_plot_data() is False
    assert trend.has_committed_plot_data() is False
    assert axis.indicator_snapshot() == {}
    assert trend.pulse_snapshot() == {}
    assert trend.current_peak_values() == []
    assert trend.current_pulse_marker_count() == 0
    assert "主频均值 --" in _visible_label_text(axis)
    assert "脉冲强度 --" in _visible_label_text(trend)
