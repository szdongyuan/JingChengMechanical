"""Durable record -> Data Management row -> real viewer and manual save.

Set HISTORICAL_REPLAY_SCREENSHOTS=1 to capture the same synthetic record with
the application stylesheet under ignored outputs/, without devices or user DBs.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase
from scipy.io import wavfile

from app.service.run_persistence.data_management import RunDataManagementService
from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisReader
from consts.style import APP_QSS
from tests.run_persistence.test_historical_analysis_reader import saved_run, _save_attempt
from tests.test_historical_analysis_dialog import Playback
from ui.modules.data_management import DataManagementModule


@pytest.fixture
def screenshot_style(qapp):
    previous_style, previous_font = qapp.styleSheet(), qapp.font()
    if os.environ.get("HISTORICAL_REPLAY_SCREENSHOTS") == "1":
        for font_name in ("msyh.ttc", "segoeui.ttf"):
            path = Path("C:/Windows/Fonts") / font_name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
        qapp.setFont(QFont("Microsoft YaHei", 9))
        qapp.setStyleSheet(APP_QSS)
    yield
    qapp.setStyleSheet(previous_style)
    qapp.setFont(previous_font)


@pytest.fixture
def representative_saved_run(saved_run):
    runtime, run_id, _, audio_path, _, _ = saved_run
    times = np.linspace(0, 3, 121)
    spl = 72 + 2 * np.sin(times * 5)
    loudness = 4.2 + 0.5 * np.sin(times * 5)
    review = {
        "judgment_payload": {"channel_results": [{
            "channel_index": 3, "spl_average_db": 72.2, "loudness_average_sone": 4.25,
        }]},
        "summary_payload": {
            "overall_status": "pass", "overall_label": "PASS",
            "rows": [
                {"item_key": key, "actual_display": value, "status": "OK"}
                for key, value in (
                    ("spl", "74.0 dB"), ("spl_average", "72.2 dB"),
                    ("loudness", "4.70 sone"), ("loudness_average", "4.25 sone"),
                    ("stft_score", "96.8"), ("ai", "正常 · 98.6%"),
                )
            ],
        },
        "chart_payload": {"3": {
            "spl": {"times_sec": times.tolist(), "values_db": spl.tolist()},
            "loudness": {"times_sec": times.tolist(), "values": loudness.tolist(), "unit": "sone"},
        }},
        "display_channel_labels": {"3": "声学传感器"},
        "default_window": {"start_sec": 0.0, "end_sec": 3.0},
        "captured_duration_sec": 3.0,
    }
    audio_times = np.arange(24000) / 8000
    signal = 7000 * np.sin(2 * np.pi * 440 * audio_times) * (0.7 + 0.3 * np.sin(5 * audio_times))
    wavfile.write(audio_path, 8000, np.column_stack((signal, signal / 2)).astype(np.int16))
    with runtime.operation() as database:
        envelope = json.loads(database.execute_sql(
            "SELECT config_snapshot_json FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()[0])
        envelope["config"].update({
            "name": "历史回放 · 声学检测", "selected_channels": [3, 1],
            "channels": [{"channel_index": 3, "loudness_unit": "sone"}],
            "result_dashboard_layout": {
                "version": 2, "rows": 1, "columns": 2,
                "panels": [
                    {"id": kind, "type": kind, "row": 0, "column": column,
                     "row_span": 1, "column_span": 1, "channel": "selected_1"}
                    for column, kind in enumerate(("spl", "loudness"))
                ],
            },
        })
        database.execute_sql("UPDATE run SET config_snapshot_json = ? WHERE run_id = ?",
                             (json.dumps(envelope), run_id))
        database.execute_sql(
            "UPDATE audio SET frame_count = 24000, duration_ms = 3000, byte_size = ? WHERE run_id = ?",
            (audio_path.stat().st_size, run_id),
        )
    _, artifact = _save_attempt(saved_run, review=review)
    return saved_run, review, artifact


def test_persisted_row_view_save_and_audition_preserve_original_artifacts(
    qtbot, qapp, representative_saved_run, monkeypatch, screenshot_style,
):
    saved, review, artifact = representative_saved_run
    runtime, run_id, _, audio_path, store, _ = saved
    artifact_before, audio_before = artifact.read_bytes(), audio_path.read_bytes()
    reader = HistoricalAnalysisReader(runtime, result_artifact_store=store)
    management = RunDataManagementService(runtime)
    playback = Playback()
    # Expose only the dependencies this workflow is allowed to use.
    services = SimpleNamespace(historical_analysis_reader=reader,
        run_data_management_service=management,
        final_composition=SimpleNamespace(playback=playback))
    module = DataManagementModule(services)
    qtbot.addWidget(module)
    assert module.table.rowCount() == 1
    assert module.table.item(0, 1).text() == run_id
    module.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    module.table.cellWidget(0, module._ANALYSIS_COLUMN).click()
    widget = module._analysis_dialogs[run_id]
    qtbot.waitUntil(lambda: widget.snapshot is not None)
    qtbot.waitUntil(lambda: widget.replay_panel is not None and widget.replay_panel.play_button.isEnabled())
    assert widget.snapshot.review == review
    assert widget.result_view.result_module.table.item(0, 1).text() == "74.0 dB"
    assert widget.result_view.result_module.table.item(2, 1).text() == "4.70 sone"
    assert widget.result_view.result_module.table.item(5, 1).text() == "正常 · 98.6%"
    for panel in widget.result_view.panel_widgets.values():
        assert panel.has_committed_plot_data()
        assert panel.current_render_channel() == 3
    config_before = widget.snapshot.run.config_snapshot_json
    widget.label_combo.setCurrentIndex(widget.label_combo.findData("轴承异响"))
    widget.save_label_button.click()
    qtbot.waitUntil(lambda: widget.save_label_button.isEnabled())
    persisted = reader.read(run_id)
    assert persisted.run.judgment == "FAIL"
    assert persisted.run.judgment_source == "MANUAL"
    assert persisted.run.fault_type == "轴承异响"
    assert module._records[0].judgment_source == "MANUAL"
    assert module._records[0].fault_type == "轴承异响"
    assert module.table.item(0, 0).checkState() == Qt.CheckState.Checked
    assert widget.snapshot.run == persisted.run
    assert widget.snapshot.original_analysis_judgment == "PASS"
    assert widget.result_view.result_module.lb_overall.text() == "通过"
    assert persisted.review == review
    assert persisted.run.config_snapshot_json == config_before
    assert artifact.read_bytes() == artifact_before
    assert audio_path.read_bytes() == audio_before
    qtbot.waitUntil(lambda: widget.replay_panel.play_button.isEnabled())
    panel = widget.replay_panel
    assert panel.channel_combo.currentData() == 3
    panel.channel_combo.setCurrentIndex(panel.channel_combo.findData(1))
    panel.gain_spin.setValue(3.0)
    panel.play_button.click()
    qtbot.waitUntil(lambda: len(playback.requests) == 1 and panel.play_button.isEnabled())
    assert playback.requests[0].run_id == run_id
    assert playback.requests[0].display_channel_indices == (1,)
    assert playback.requests[0].window_start_seconds == 0.0
    assert playback.requests[0].window_end_seconds == 3.0
    assert playback.requests[0].gain_db_override == 3.0

    if os.environ.get("HISTORICAL_REPLAY_SCREENSHOTS") == "1":
        _capture_screenshots(qtbot, module, widget, monkeypatch)
    widget.close()
    qtbot.waitUntil(lambda: run_id not in module._analysis_dialogs)
    qtbot.waitUntil(lambda: playback.owner is None)


def _capture_screenshots(qtbot, module, widget, monkeypatch):
    output = Path("outputs")
    output.mkdir(exist_ok=True)
    widget.show()
    qtbot.wait(250)
    assert widget.grab().save(str(output / "historical-replay-single-page.png"))
    # Avoid the separate legacy fault-type catalogue when showing this
    # standalone integration fixture; its row data is already populated.
    monkeypatch.setattr(module, "_refresh_fault_type_options_cache", lambda: None)
    module.resize(1550, 450)
    module.show()
    qtbot.wait(100)
    assert module.grab().save(str(output / "historical-replay-data-management.png"))
