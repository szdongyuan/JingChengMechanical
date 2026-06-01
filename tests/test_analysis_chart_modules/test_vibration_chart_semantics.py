from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.widgets.analysis_charts import FftLineChartWidget, OctaveBarChartWidget


def test_fft_widget_vibration_semantics_avoid_acoustic_export_headers(qapp):
    widget = FftLineChartWidget()
    try:
        widget.set_vibration_semantics(True)
        widget.set_spectrum(
            frequencies_hz=[10.0, 20.0],
            levels_db=[1.0, 2.0],
        )

        table = widget.export_csv_table()

        assert widget.value_semantics() == "vibration"
        assert "level_db" not in table.headers
        assert "golden_level_db" not in table.headers
        assert "calibrated_amplitude" in table.headers
        assert "golden_calibrated_amplitude" in table.headers
        assert "failed" in table.headers
        assert "passed" not in table.headers
        assert widget.plot_item.getAxis("left").labelText == "校准幅值"
    finally:
        widget.close()


def test_octave_widget_vibration_semantics_avoid_acoustic_export_headers(qapp):
    widget = OctaveBarChartWidget()
    try:
        widget.set_vibration_semantics(True)
        widget.set_bands(
            center_freqs_hz=[125.0, 250.0],
            levels_db=[1.0, 2.0],
        )

        table = widget.export_csv_table()

        assert widget.value_semantics() == "vibration"
        assert "level_db" not in table.headers
        assert "golden_level_db" not in table.headers
        assert "calibrated_amplitude" in table.headers
        assert "golden_calibrated_amplitude" in table.headers
        assert "passed" in table.headers
        assert widget.plot_item.getAxis("left").labelText == "校准幅值"
    finally:
        widget.close()
