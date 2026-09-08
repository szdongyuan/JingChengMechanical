from types import SimpleNamespace
import threading

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QPushButton

from app.service.run_scoped_playback import PlaybackCode, PlaybackResult, PlaybackStatus
from tests.test_historical_analysis_dialog import Playback
from ui.popups import run_audio_replay_dialog as replay


@pytest.fixture
def record():
    return SimpleNamespace(run_id="12345678-1234-4234-8234-123456789012",
        source_wav_path="unused.wav", sample_rate_hz=8000, frame_count=24000,
        channel_order=(3, 0, 1))


def controls(qtbot, record, playback):
    widget = replay.RunAudioReplayControls(record=record, playback=playback)
    qtbot.addWidget(widget)
    return widget


@pytest.mark.parametrize("channels,expected", [((3, 0, 1), 0), ((3, 1), 3)])
def test_metadata_only_controls_default_channel_and_gain(qtbot, record, monkeypatch, channels, expected):
    def unexpected(*args, **kwargs):
        pytest.fail("compact controls must not load audio or prepare audition plots")
    monkeypatch.setattr(replay.wavfile, "read", unexpected)
    monkeypatch.setattr(replay, "prepare_replay_audio", unexpected)
    monkeypatch.setattr(replay, "prepare_replay_plots", unexpected)
    record.channel_order = channels
    playback = Playback()
    widget = controls(qtbot, record, playback)
    assert not isinstance(widget, replay.RunAudioReplayPanel)
    assert not widget.findChildren(replay.pg.PlotWidget)
    assert [widget.channel_combo.itemData(i) for i in range(widget.channel_combo.count())] == list(channels)
    assert widget.channel_combo.currentData() == expected
    assert widget.channel_combo.currentText() == f"CH{expected + 1}"
    assert widget.gain_spin.value() == 20.0
    assert (widget.gain_spin.minimum(), widget.gain_spin.maximum()) == (-60.0, 35.0)
    assert [button.text() for button in widget.findChildren(QPushButton)] == ["重放"]
    assert widget.play_button.isEnabled()
    assert playback.requests == []


def test_selected_channel_full_record_gain_and_repeat_start_off_gui_thread(qtbot, record):
    playback = Playback()
    playback.release = threading.Event()
    threads = []
    original_play = playback.play
    def delayed(request, *, owner):
        threads.append(threading.get_ident())
        return original_play(request, owner=owner)
    playback.play = delayed
    widget = controls(qtbot, record, playback)
    widget.channel_combo.setCurrentIndex(widget.channel_combo.findData(1))
    widget.gain_spin.setValue(8.5)
    widget.play_button.click()
    try:
        qtbot.waitUntil(playback.entered.is_set)
        assert not widget.play_button.isEnabled()
        widget._play()
        assert playback.requests == []
    finally:
        playback.release.set()
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget.play_button.click()
    qtbot.waitUntil(lambda: len(playback.requests) == 2 and widget.play_button.isEnabled())
    assert len(threads) == 2 and all(thread != threading.get_ident() for thread in threads)
    for request in playback.requests:
        assert request.run_id == record.run_id
        assert request.display_channel_indices == (1,)
        assert request.display_channel_index == 1
        assert (request.window_start_seconds, request.window_end_seconds) == (0.0, 3.0)
        assert request.gain_db_override == 8.5
    assert "失败" not in widget.status_label.text()


@pytest.mark.parametrize("failure", ["exception", "result"])
def test_failed_start_is_visible_and_retry_available(qtbot, record, failure):
    playback = Playback()
    def fail(request, *, owner):
        if failure == "exception":
            raise RuntimeError("backend unavailable")
        return PlaybackResult(PlaybackStatus.FAILED, request.run_id,
            PlaybackCode.BACKEND_START_FAILED, "failed")
    playback.play = fail
    widget = controls(qtbot, record, playback)
    widget.play_button.click()
    qtbot.waitUntil(widget.play_button.isEnabled)
    assert "回放启动失败" in widget.status_label.text()


@pytest.mark.parametrize("missing", ["audio", "service"])
def test_missing_dependency_disables_replay_with_inline_reason(qtbot, record, missing):
    widget = controls(qtbot, None if missing == "audio" else record,
        None if missing == "service" else Playback())
    assert not widget.play_button.isEnabled()
    assert "未保存录音" in widget.status_label.text() if missing == "audio" else "播放服务未就绪" in widget.status_label.text()


def test_shutdown_during_start_survives_widget_deletion_and_is_idempotent(qtbot, record):
    playback = Playback()
    playback.release = threading.Event()
    widget = controls(qtbot, record, playback)
    widget.play_button.click()
    try:
        qtbot.waitUntil(playback.entered.is_set)
        widget.shutdown()
        widget.shutdown()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        playback.release.set()
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)
    assert len(playback.stops) == 1
    assert playback.owner is None


@pytest.mark.parametrize("replacement", [None, object()])
def test_shutdown_preserves_later_playback_owner(qtbot, record, replacement):
    playback = Playback()
    widget = controls(qtbot, record, playback)
    widget.play_button.click()
    qtbot.waitUntil(widget.play_button.isEnabled)
    playback.owner = replacement
    widget.shutdown()
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)
    assert playback.owner is replacement
    assert playback.stops == []
