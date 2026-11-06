from dataclasses import replace
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest
import numpy as np
from scipy.io import wavfile
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent

from app.service.run_persistence.historical_analysis_reader import HistoricalAnalysisSnapshot, HistoricalReplayAudio
from app.service.run_scoped_playback import PlaybackResult, PlaybackStatus, PlaybackCode
from app.service.run_persistence.run_repository import RunRecord
from model.malfunction_type_table import GOOD_MALFUNCTION_TYPE_NAME, BAD_MALFUNCTION_TYPE_NAME
from ui.popups.historical_analysis_dialog import HistoricalAnalysisDialog


def snapshot(*, run_id=None, value=72.5, review=True):
    run = RunRecord(
        run_id=run_id or str(uuid4()), ingress_kind="recording", trigger_source="manual",
        config_id=101, hardware_id=None, imported_source_identity=None,
        imported_source_path=None, imported_expected_frame_count=None, causal_revision=0,
        config_snapshot_json="{}", hardware_snapshot_json="{}", calibration_snapshot_json="{}",
        state="SUCCEEDED", judgment="FAIL", judgment_source="analysis", judged_at=None,
        terminal_code="ok", terminal_summary=None, accepted_at="2026-09-07T01:00:00Z",
        terminal_at=None, updated_at="2026-09-07T01:00:00Z",
    )
    config = {
        "config_id": 101, "name": "Frozen config", "selected_channels": [7],
        "channels": [{"channel_index": 7, "loudness_unit": "sone"}],
        "result_dashboard_layout": {"version": 2, "rows": 1, "columns": 1, "panels": [
            {"id": "saved_spl", "type": "spl", "row": 0, "column": 0,
             "row_span": 1, "column_span": 1, "channel": "selected_1"}]},
    }
    payload = {
        "summary_payload": {"overall_status": "fail", "overall_label": "FAIL", "rows": [
            {"item_key": "spl", "actual_display": f"{value} dB", "status": "OK"},
            {"item_key": "ai", "actual_display": "bearing (98%)", "status": "FAIL"}]},
        "chart_payload": {"7": {"spl": {"times_sec": [0.0, 0.1], "values_db": [value, value + 1]}}},
        "display_channel_labels": {"7": "Sensor"},
        "default_window": {"start_sec": 0.0, "end_sec": 0.1},
        "manual_reanalysis_allowed": True,
    }
    return HistoricalAnalysisSnapshot(run, config, {"sn": "SN-1", "model": "M1"},
        payload if review else None, str(uuid4()), "FAIL", None, ("bearing",),
        None if review else "未保存成功分析结果", None, "未保存录音")


class Reader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def read(self, run_id):
        self.calls.append((run_id, threading.get_ident()))
        return self.value


def dialog(qtbot, value, *, reader=None, management=None, playback=None):
    reader = reader or Reader(value)
    services = SimpleNamespace(historical_analysis_reader=reader,
        run_data_management_service=management, final_composition=SimpleNamespace(playback=playback))
    widget = HistoricalAnalysisDialog(run_id=value.run.run_id, services=services)
    qtbot.addWidget(widget)
    qtbot.waitUntil(lambda: widget.snapshot is not None)
    return widget, reader


def test_saved_values_real_chart_and_private_display(qtbot):
    a, reader = dialog(qtbot, snapshot(value=72.5))
    b, _ = dialog(qtbot, snapshot(value=84.0))
    assert reader.calls == [(a.run_id, reader.calls[0][1])]
    assert reader.calls[0][1] != threading.get_ident()
    assert not a.isModal()
    assert a.result_view.result_module.table.item(0, 1).text() == "72.5 dB"
    assert a.result_view.result_module.table.item(5, 1).text() == "bearing (98%)"
    assert b.result_view.result_module.table.item(0, 1).text() == "84.0 dB"
    panel = a.result_view.panel_widgets["saved_spl"]
    assert panel.has_committed_plot_data()
    assert panel.current_render_channel() == 7
    assert list(panel.current_plot_payload()["y_values"]) == [72.5, 73.5]
    context = a.result_view.display_services
    assert context is not b.result_view.display_services
    assert not hasattr(context.factory_mode, "begin_reanalysis")
    assert context.factory_mode.review_payload.manual_reanalysis_allowed is False
    assert context.factory_mode.review_payload.default_window.end_sec == 0.1
    assert "SN-1" in a.metadata_label.text()
    assert "原始" in a.original_label.text()
    assert "未保存录音" in a.replay_panel.status_label.text()


def test_label_combo_displays_full_fault_name_with_application_style(qtbot, qapp):
    from consts.style import APP_QSS
    from PyQt6.QtGui import QFont, QFontDatabase
    from PyQt6.QtWidgets import QStyle, QStyleOptionComboBox
    from pathlib import Path

    previous_style, previous_font = qapp.styleSheet(), qapp.font()
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        qapp.setFont(QFont("Microsoft YaHei", 9))
    qapp.setStyleSheet(APP_QSS)
    try:
        value = snapshot()
        value = replace(value, accepted_fault_types=("轴承异响",),
            run=replace(value.run, fault_type="轴承异响"))
        widget, _ = dialog(qtbot, value)
        widget.show()
        qapp.processEvents()
        combo = widget.label_combo
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        contents = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox,
            option, QStyle.SubControl.SC_ComboBoxEditField, combo)
        widest_label = max(combo.fontMetrics().horizontalAdvance(combo.itemText(index))
            for index in range(combo.count()))
        assert contents.width() >= widest_label
    finally:
        qapp.setStyleSheet(previous_style)
        qapp.setFont(previous_font)


class Management:
    def __init__(self, reader, error=None):
        self.reader, self.error, self.calls = reader, error, []

    def update_fault_type(self, run_id, label):
        self.calls.append((run_id, label))
        if self.error:
            raise RuntimeError(self.error)
        previous = self.reader.value
        self.reader.value = replace(previous, run=replace(previous.run,
            judgment="PASS" if label == GOOD_MALFUNCTION_TYPE_NAME else "FAIL",
            judgment_source="manual", fault_type=None if label == GOOD_MALFUNCTION_TYPE_NAME else label))
        return True


def test_explicit_save_reloads_only_committed_manual_status(qtbot):
    value = snapshot()
    reader = Reader(value)
    management = Management(reader)
    widget, _ = dialog(qtbot, value, reader=reader, management=management)
    saved = []
    widget.label_saved.connect(saved.append)
    widget.label_combo.setCurrentIndex(widget.label_combo.findData(GOOD_MALFUNCTION_TYPE_NAME))
    assert management.calls == []
    widget.save_label_button.click()
    qtbot.waitUntil(lambda: saved == [value.run.run_id])
    assert management.calls == [(value.run.run_id, GOOD_MALFUNCTION_TYPE_NAME)]
    assert widget.snapshot.run.judgment == "PASS"
    assert "manual" in widget.current_label.text()
    assert widget.snapshot.review == value.review
    assert "FAIL" in widget.original_label.text()
    assert widget.result_view.result_module.lb_overall.text() == "不通过"
    widget.label_combo.setCurrentIndex(widget.label_combo.findData("bearing"))
    widget.save_label_button.click()
    qtbot.waitUntil(lambda: len(saved) == 2)
    assert widget.snapshot.run.fault_type == "bearing"


def test_failed_save_preserves_committed_label_and_does_not_emit(qtbot):
    value = snapshot()
    reader = Reader(value)
    widget, _ = dialog(qtbot, value, reader=reader, management=Management(reader, "database busy"))
    saved = []
    widget.label_saved.connect(saved.append)
    widget.label_combo.setCurrentIndex(widget.label_combo.findData(GOOD_MALFUNCTION_TYPE_NAME))
    widget.save_label_button.click()
    assert saved == []
    assert widget.snapshot.run.judgment == "FAIL"
    assert "database busy" in widget.status_label.text()
    assert widget.label_combo.currentData() != GOOD_MALFUNCTION_TYPE_NAME


@pytest.mark.parametrize("has_result", [True, False])
def test_partial_result_availability_still_allows_marking(qtbot, has_result):
    value = replace(snapshot(review=has_result), accepted_fault_types=())
    widget, _ = dialog(qtbot, value)
    assert widget.save_label_button.isEnabled()
    assert widget.label_combo.findData(BAD_MALFUNCTION_TYPE_NAME) >= 0
    assert widget.label_combo.findData("bearing") < 0
    assert (widget.result_view is not None) == has_result
    if not has_result:
        assert "未保存成功分析结果" in widget.result_message_label.text()


def test_deleted_run_refresh_disables_editing(qtbot):
    widget, reader = dialog(qtbot, snapshot())
    def deleted(_run_id):
        raise RuntimeError("记录不存在或已删除")
    reader.read = deleted
    widget.refresh()
    qtbot.waitUntil(lambda: "已删除" in widget.status_label.text())
    assert not widget.save_label_button.isEnabled()
    assert not widget.label_combo.isEnabled()
    assert widget.snapshot is None
    assert widget.result_view is None


@pytest.fixture
def audio_snapshot(tmp_path):
    value = snapshot()
    path = tmp_path / "record.wav"
    wavfile.write(path, 8000, np.ones((800, 2), dtype=np.int16))
    return replace(value, audio=HistoricalReplayAudio(value.run.run_id, path, 8000, 2, 800, (7, 3)), audio_message=None)


class Playback:
    def __init__(self):
        self.owner = None
        self.requests = []
        self.stops = []
        self.entered = threading.Event()
        self.release = None

    def play(self, request, *, owner=None):
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        self.owner = owner
        self.requests.append(request)
        return PlaybackResult(PlaybackStatus.STARTED, request.run_id, PlaybackCode.STARTED, "Playback started.")

    def stop_if_owner(self, owner, timeout_seconds):
        if self.owner is owner:
            self.stops.append(owner)
            self.owner = None


def test_import_uses_persisted_display_config_and_provenance(qtbot):
    value = snapshot()
    display = dict(value.config, selected_channels=[2], channels=[{'channel_index': 2}])
    payload = dict(value.review, config_payload=display, chart_payload={'2': value.review['chart_payload']['7']},
        review={'import': {'display_channel_labels': {'2': 'WAV 通道 1（仅图表）'}, 'current_available': False}})
    value = replace(value, run=replace(value.run, ingress_kind='IMPORTED', trigger_source='IMPORT'), review=payload)
    widget, _ = dialog(qtbot, value)
    review = widget.result_view.display_services.factory_mode.review_payload
    assert review.config_payload['selected_channels'] == [2]
    assert review.display_channel_labels == {2: 'WAV 通道 1（仅图表）'}
    assert widget.result_view.panel_widgets['saved_spl'].current_render_channel() == 2
    assert '导入' in widget.metadata_label.text()
    assert '无电流数据' in widget.metadata_label.text()


def test_import_keeps_explicit_layout_and_original_selected_channel_meaning(qtbot):
    value = snapshot()
    display = dict(value.config, selected_channels=[8, 7],
        channels=[{'channel_index': 8}, {'channel_index': 7}])
    payload = dict(value.review, config_payload=display,
        chart_payload={'7': value.review['chart_payload']['7'], '8': value.review['chart_payload']['7']})
    value = replace(value, run=replace(value.run, ingress_kind='IMPORTED'), review=payload)
    widget, _ = dialog(qtbot, value)
    assert widget.result_view.dashboard_layout == display['result_dashboard_layout']
    assert widget.result_view.panel_widgets['saved_spl'].current_render_channel() == 7
    assert widget.result_view.result_module._metric_selected_channel_index == 7


def test_automatic_enrichment_preserves_active_replay_and_choices(qtbot, audio_snapshot):
    playback = Playback()
    widget, reader = dialog(qtbot, audio_snapshot, playback=playback)
    panel = widget.replay_panel
    panel.channel_combo.setCurrentIndex(1)
    panel.gain_spin.setValue(-7.0)
    panel.play_button.click()
    qtbot.waitUntil(lambda: bool(playback.requests))
    reader.value = replace(audio_snapshot, charts_message='enriched')
    widget.refresh_enrichment()
    qtbot.waitUntil(lambda: widget.snapshot.charts_message == 'enriched')
    assert widget.replay_panel is panel
    assert panel.channel_combo.currentIndex() == 1
    assert panel.gain_spin.value() == -7.0
    assert playback.stops == []


def test_historical_playback_available_without_production_composition(qtbot, audio_snapshot):
    playback = Playback()
    services = SimpleNamespace(historical_analysis_reader=Reader(audio_snapshot), historical_playback=playback)
    widget = HistoricalAnalysisDialog(run_id=audio_snapshot.run.run_id, services=services)
    qtbot.addWidget(widget)
    qtbot.waitUntil(lambda: widget.replay_panel is not None and widget.replay_panel.play_button.isEnabled())
    widget.replay_panel.play_button.click()
    qtbot.waitUntil(lambda: bool(playback.requests))
    assert playback.requests[0].run_id == audio_snapshot.run.run_id


def ready(qtbot, widget):
    qtbot.waitUntil(lambda: widget.replay_panel is not None and widget.replay_panel.play_button.isEnabled())
    return widget.replay_panel


def test_single_page_replay_maps_channel_full_record_gain_and_same_run_takeover(qtbot, audio_snapshot):
    from PyQt6.QtWidgets import QTabWidget
    from ui.popups.run_audio_replay_dialog import RunAudioReplayPanel
    playback = Playback()
    a, _ = dialog(qtbot, audio_snapshot, playback=playback)
    b, _ = dialog(qtbot, audio_snapshot, playback=playback)
    panel_a, panel_b = ready(qtbot, a), ready(qtbot, b)
    a.show()
    assert not a.findChildren(QTabWidget)
    assert not a.findChildren(RunAudioReplayPanel)
    assert a.result_view.isVisible()
    assert a.label_combo.isVisible() and panel_a.isVisible()
    panel_a.channel_combo.setCurrentIndex(1)
    panel_a.gain_spin.setValue(8.5)
    panel_a._play()
    qtbot.waitUntil(lambda: len(playback.requests) == 1 and panel_a.play_button.isEnabled())
    assert "失败" not in panel_a.status_label.text()
    request = playback.requests[0]
    assert request.run_id == a.run_id
    assert request.display_channel_indices == (3,)
    assert request.window_start_seconds == 0.0
    assert request.window_end_seconds == 0.1
    assert request.gain_db_override == 8.5
    panel_b._play()
    qtbot.waitUntil(lambda: len(playback.requests) == 2)
    owner_b = playback.owner
    a.close()
    qtbot.wait(30)
    assert playback.owner is owner_b
    b.close()
    qtbot.waitUntil(lambda: playback.owner is None)


@pytest.mark.parametrize("action", ["close", "reject", "deleteLater", "refresh"])
def test_host_cleanup_stops_late_play_start(qtbot, audio_snapshot, action):
    playback = Playback()
    playback.release = threading.Event()
    widget, _ = dialog(qtbot, audio_snapshot, playback=playback)
    panel = ready(qtbot, widget)
    panel._play()
    qtbot.waitUntil(playback.entered.is_set)
    try:
        getattr(widget, action)()
        if action == "deleteLater":
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        playback.release.set()
    qtbot.waitUntil(lambda: len(playback.stops) == 1)
    assert playback.owner is None


def test_no_result_can_still_replay(qtbot, audio_snapshot):
    value = replace(audio_snapshot, review=None, result_message="未保存成功分析结果")
    widget, _ = dialog(qtbot, value, playback=Playback())
    assert ready(qtbot, widget).channel_combo.currentData() == 7
    assert widget.result_view is None


@pytest.mark.parametrize("action", ["refresh", "close", "reject", "deleteLater"])
def test_stale_read_does_not_replace_newer_snapshot_or_closed_window(qtbot, action):
    old = snapshot(value=11.0)
    new = replace(snapshot(run_id=old.run.run_id, value=99.0), audio=None)
    entered, release = threading.Event(), threading.Event()
    reader = Reader(old)
    def delayed(run_id):
        reader.calls.append((run_id, threading.get_ident()))
        if len(reader.calls) == 1:
            entered.set()
            assert release.wait(5)
            return old
        return new
    reader.read = delayed
    services = SimpleNamespace(historical_analysis_reader=reader)
    widget = HistoricalAnalysisDialog(run_id=old.run.run_id, services=services)
    qtbot.addWidget(widget)
    qtbot.waitUntil(entered.is_set)
    assert "加载" in widget.status_label.text()
    assert widget.snapshot is None
    try:
        getattr(widget, action)()
        if action == "refresh":
            qtbot.waitUntil(lambda: widget.snapshot is new)
        if action == "deleteLater":
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        release.set()
    from ui.popups.historical_analysis_dialog import _ACTIVE_READERS
    qtbot.waitUntil(lambda: not _ACTIVE_READERS)
    if action == "refresh":
        assert widget.snapshot is new
        assert widget.result_view.result_module.table.item(0, 1).text() == "99.0 dB"
    elif action == "deleteLater":
        assert sip.isdeleted(widget)
    else:
        assert widget.snapshot is None


@pytest.mark.parametrize("kind", ["stft", "spectrum", "octave", "loudness", "vibration_stft",
    "vibration_spectrum", "vibration_octave", "vibration_frequency_stability", "vibration_pulse"])
def test_real_configured_chart_families_render_saved_payload(qtbot, kind):
    value = snapshot()
    value.config["result_dashboard_layout"]["panels"][0].update(type=kind)
    if kind.startswith("vibration_"):
        value.config["vibration_view_channel_index"] = 7
        value.config["result_dashboard_layout"]["panels"][0]["channel"] = "vibration"
    value.review["captured_duration_sec"] = 0.1
    value.review["chart_payload"]["7"].update({
        "stft": {"times_sec": [0.0, 0.1], "frequencies_hz": [100.0, 200.0], "magnitude_db": [[10.0, 20.0], [30.0, 40.0]]},
        "fft": {"frequencies_hz": [100.0, 200.0], "levels_db": [20.0, 30.0]},
        "octave": {"center_freqs_hz": [100.0, 200.0], "levels_db": [20.0, 30.0]},
        "loudness": {"times_sec": [0.0, 0.1], "values": [2.0, 3.0], "unit": "sone"},
        "vibration": {"frequency_times_sec": [0.0, 0.1], "fundamental_frequency_values_hz": [100.0, 105.0],
            "spectral_centroid_values_hz": [101.0, 106.0], "times_sec": [0.0, 0.1], "rms_values": [1.0, 2.0], "peak_values": [2.0, 3.0]},
    })
    widget, _ = dialog(qtbot, value)
    panel = widget.result_view.panel_widgets["saved_spl"]
    if "stft" in kind:
        assert panel.current_image_payload() is not None
        assert not panel.analyze_button.isEnabled()
    else:
        assert panel.has_committed_plot_data()
        assert panel.current_render_channel() == 7


def test_old_replay_and_historical_same_run_owner_isolation(qtbot, audio_snapshot):
    from ui.popups.run_audio_replay_dialog import RunAudioReplayDialog
    playback = Playback()
    widget, _ = dialog(qtbot, audio_snapshot, playback=playback)
    panel = ready(qtbot, widget)
    old = RunAudioReplayDialog(record=audio_snapshot.audio, playback=playback)
    qtbot.addWidget(old)
    old._play()
    panel._play()
    qtbot.waitUntil(lambda: len(playback.requests) == 2)
    historical_owner = playback.owner
    old.close()
    qtbot.wait(30)
    assert playback.owner is historical_owner
    # An unowned legacy replacement also supersedes the history window.
    playback.play(playback.requests[-1])
    widget.close()
    qtbot.wait(30)
    assert playback.stops == []


def test_live_bus_emissions_cannot_replace_history(qtbot):
    from PyQt6.QtCore import QObject, pyqtSignal
    class Bus(QObject):
        factory_review_changed = pyqtSignal(object)
        factory_state_changed = pyqtSignal(object)
    value = snapshot()
    reader = Reader(value)
    services = SimpleNamespace(historical_analysis_reader=reader, bus=Bus())
    widget = HistoricalAnalysisDialog(run_id=value.run.run_id, services=services)
    qtbot.addWidget(widget)
    qtbot.waitUntil(lambda: widget.snapshot is not None)
    assert services.bus.receivers(services.bus.factory_review_changed) == 0
    services.bus.factory_review_changed.emit(snapshot(value=1.0).review)
    services.bus.factory_state_changed.emit(object())
    assert widget.result_view.result_module.table.item(0, 1).text() == "72.5 dB"


def test_missing_charts_message_and_active_record_label_disabled(qtbot):
    value = snapshot()
    value.review["chart_payload"] = {}
    value = replace(value, charts_message="尚无已保存图表，可刷新读取后来保存的结果", run=replace(value.run, state="ACTIVE"))
    widget, _ = dialog(qtbot, value)
    assert "尚无已保存图表" in widget.result_message_label.text()
    assert not widget.save_label_button.isEnabled()
    assert not widget.result_view.panel_widgets["saved_spl"].has_committed_plot_data()


def test_refresh_button_reloads_persisted_snapshot(qtbot):
    widget, reader = dialog(qtbot, snapshot())
    replacement = snapshot(run_id=widget.run_id, value=42.0)
    reader.value = replacement
    widget.refresh_button.click()
    qtbot.waitUntil(lambda: widget.snapshot is replacement)
    assert widget.result_view.result_module.table.item(0, 1).text() == "42.0 dB"


def test_deleted_run_on_save_is_reported_and_cannot_be_played(qtbot, audio_snapshot):
    reader = Reader(audio_snapshot)
    management = Management(reader, "run not found")
    widget, _ = dialog(qtbot, audio_snapshot, reader=reader, management=management, playback=Playback())
    ready(qtbot, widget)
    def deleted(_run_id):
        raise RuntimeError("记录不存在或已删除")
    reader.read = deleted
    saved = []
    widget.label_saved.connect(saved.append)
    widget.label_combo.setCurrentIndex(widget.label_combo.findData(GOOD_MALFUNCTION_TYPE_NAME))
    widget.save_label_button.click()
    qtbot.waitUntil(lambda: widget.snapshot is None)
    assert "已删除" in widget.status_label.text()
    assert widget.replay_panel is None
    assert not widget.save_label_button.isEnabled()
    assert saved == []


def test_committed_save_notifies_even_when_closed_during_metadata_reload(qtbot):
    value = snapshot()
    reader = Reader(value)
    widget, _ = dialog(qtbot, value, reader=reader, management=Management(reader))
    release, entered = threading.Event(), threading.Event()
    def delayed(_run_id):
        entered.set()
        assert release.wait(5)
        return reader.value
    reader.read = delayed
    saved = []
    widget.label_saved.connect(saved.append)
    widget.label_combo.setCurrentIndex(widget.label_combo.findData(GOOD_MALFUNCTION_TYPE_NAME))
    widget.save_label_button.click()
    qtbot.waitUntil(entered.is_set)
    try:
        widget.close()
        assert saved == [widget.run_id]
    finally:
        release.set()
    from ui.popups.historical_analysis_dialog import _ACTIVE_READERS
    qtbot.waitUntil(lambda: not _ACTIVE_READERS)
    assert saved == [widget.run_id]


@pytest.mark.parametrize("action", ["close", "refresh"])
def test_metadata_only_controls_refresh_or_close_without_audio_preparation(qtbot, audio_snapshot, monkeypatch, action):
    from ui.popups import run_audio_replay_dialog as replay
    def unexpected(*args, **kwargs):
        pytest.fail("historical viewer must not prepare audition audio")
    monkeypatch.setattr(replay, "prepare_replay_audio", unexpected)
    monkeypatch.setattr(replay, "prepare_replay_plots", unexpected)
    monkeypatch.setattr(replay.wavfile, "read", unexpected)
    widget, _ = dialog(qtbot, audio_snapshot, playback=Playback())
    first_panel = ready(qtbot, widget)
    first_panel.channel_combo.setCurrentIndex(1)
    getattr(widget, action)()
    assert first_panel._closed.is_set()
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)
    if action == "refresh":
        assert ready(qtbot, widget) is not first_panel
        assert widget.replay_panel.channel_combo.currentData() == 7
    else:
        assert widget.replay_panel is None
