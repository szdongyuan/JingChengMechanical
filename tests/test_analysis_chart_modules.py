from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

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
