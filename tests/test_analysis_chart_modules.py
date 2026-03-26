from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.services_container import FactoryControllerState, FactoryReviewPayload
from ui.modules.octave import OctaveModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.widgets.analysis_charts import (
    FftLineChartWidget,
    LineChartWidget,
    OctaveBarChartWidget,
    StftImageChartWidget,
)

EXPECTED_BASELINE_REQUIREMENTS = {
    "concurrent-log-handler",
    "numpy",
    "peewee",
    "pyqt6",
    "pyqtgraph==0.13.7",
    "pytest",
    "pytest-qt",
    "scipy",
    "sounddevice",
}


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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


def test_requirements_txt_lists_runtime_and_test_baseline_dependencies():
    requirements_path = Path(__file__).resolve().parents[1] / "requirements.txt"
    entries = {
        line.split("#", 1)[0].strip().casefold()
        for line in requirements_path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }

    assert entries == EXPECTED_BASELINE_REQUIREMENTS


def test_stft_image_chart_widget_accepts_render_payload(qapp):
    widget = StftImageChartWidget()

    widget.set_image(
        times_sec=[0.0, 0.5, 1.0],
        frequencies_hz=[100.0, 200.0],
        magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
    )

    assert widget.has_image() is True
    assert widget.image_payload()["times_sec"] == [0.0, 0.5, 1.0]
    assert widget.image_payload()["frequencies_hz"] == [100.0, 200.0]
    assert widget.image_payload()["magnitude_db"].shape == (2, 3)


def test_stft_image_chart_widget_image_payload_returns_defensive_copies(qapp):
    widget = StftImageChartWidget()
    widget.set_image(
        times_sec=[0.0, 0.5, 1.0],
        frequencies_hz=[100.0, 200.0],
        magnitude_db=np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
    )

    payload = widget.image_payload()
    assert payload is not None

    payload["times_sec"].append(9.5)
    payload["frequencies_hz"][0] = -1.0
    payload["magnitude_db"][0, 0] = 99.0

    fresh_payload = widget.image_payload()
    assert fresh_payload["times_sec"] == [0.0, 0.5, 1.0]
    assert fresh_payload["frequencies_hz"] == [100.0, 200.0]
    assert np.array_equal(
        fresh_payload["magnitude_db"],
        np.array([[1.0, 2.0, 3.0], [3.5, 4.5, 5.5]]),
    )


def test_line_chart_widget_accepts_limit_lines(qapp):
    widget = LineChartWidget()

    widget.set_series(
        x_values=[0.0, 1.0],
        y_values=[70.0, 72.0],
        lower_limit=60.0,
        upper_limit=80.0,
    )

    assert widget.has_series() is True
    assert widget.series_payload()["x_values"] == [0.0, 1.0]
    assert widget.series_payload()["y_values"] == [70.0, 72.0]
    assert widget.series_payload()["lower_limit"] == 60.0
    assert widget.series_payload()["upper_limit"] == 80.0


def test_fft_line_chart_widget_accepts_limit_band_markers(qapp):
    widget = FftLineChartWidget()
    limit_bands = [
        {
            "freq_hz": 1000.0,
            "lower_db": 40.0,
            "upper_db": 60.0,
            "actual_db": 67.0,
            "passed": False,
        }
    ]

    widget.set_spectrum(
        frequencies_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[42.0, 45.0, 51.0, 67.0],
        limit_bands=limit_bands,
    )

    assert widget.has_series() is True
    assert widget.spectrum_payload()["frequencies_hz"] == [125.0, 250.0, 500.0, 1000.0]
    assert widget.spectrum_payload()["levels_db"] == [42.0, 45.0, 51.0, 67.0]
    assert widget.spectrum_payload()["limit_bands"] == limit_bands


def test_octave_bar_chart_widget_accepts_limit_band_markers(qapp):
    widget = OctaveBarChartWidget()
    limit_bands = [
        {
            "freq_hz": 500.0,
            "lower_db": 35.0,
            "upper_db": 55.0,
            "actual_db": 57.0,
            "passed": False,
        }
    ]

    widget.set_bands(
        center_freqs_hz=[125.0, 250.0, 500.0, 1000.0],
        levels_db=[33.0, 41.0, 57.0, 49.0],
        limit_bands=limit_bands,
    )

    assert widget.has_bars() is True
    assert widget.band_payload()["center_freqs_hz"] == [125.0, 250.0, 500.0, 1000.0]
    assert widget.band_payload()["levels_db"] == [33.0, 41.0, 57.0, 49.0]
    assert widget.band_payload()["limit_bands"] == limit_bands


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

    assert module.has_committed_plot_data() is False
    assert module.empty_state.isHidden() is False

    services.bus.factory_state_changed.emit(_state(101, "reviewing"))
    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(101, channels=(0, 1))
    )
    qapp.processEvents()

    assert module.has_committed_plot_data() is True

    services.bus.factory_state_changed.emit(_state(101, "reanalyzing"))
    qapp.processEvents()

    assert module.has_committed_plot_data() is False
    assert module.empty_state.isHidden() is False


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
