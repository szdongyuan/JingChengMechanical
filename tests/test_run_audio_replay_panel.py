from types import SimpleNamespace
import threading

import numpy as np
import pytest
from PyQt6.QtWidgets import QDialog, QWidget
from scipy.io import wavfile

from app.service.run_scoped_playback import PlaybackCode, PlaybackLifecycleCode, PlaybackResult, PlaybackStatus
from ui.popups import run_audio_replay_dialog as replay


RUN_ID = "12345678-1234-4234-8234-123456789012"


class Playback:
    def __init__(self):
        self.owner = None
        self.requests = []
        self.stops = []
        self.started = threading.Event()
        self.release = None

    def play(self, request, *, owner=None):
        self.started.set()
        if self.release is not None:
            assert self.release.wait(5)
        self.owner = owner
        self.requests.append(request)
        return PlaybackResult(PlaybackStatus.STARTED, request.run_id, PlaybackCode.STARTED, "Playback started.")

    def stop_if_owner(self, owner, timeout_seconds):
        if self.owner is owner:
            self.stops.append(owner)
            self.owner = None


@pytest.fixture
def record(tmp_path):
    path = tmp_path / "audio.wav"
    wavfile.write(path, 8000, np.column_stack((np.arange(800, dtype=np.int16), -np.arange(800, dtype=np.int16))))
    return SimpleNamespace(run_id=RUN_ID, source_wav_path=path, sample_rate_hz=8000, frame_count=800, channel_count=2, channel_order=(7, 3))


def panel(qtbot, record, playback=None, **kwargs):
    widget = replay.RunAudioReplayPanel(record=record, playback=playback or Playback(), **kwargs)
    qtbot.addWidget(widget)
    return widget


def test_embedded_panel_loads_audio_and_prepares_plots_off_gui_thread(qtbot, record, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    threads = []
    original = replay.prepare_replay_audio

    def delayed(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(replay, "prepare_replay_audio", delayed)
    widget = panel(qtbot, record)
    try:
        assert isinstance(widget, QWidget) and not isinstance(widget, QDialog)
        qtbot.waitUntil(entered.is_set)
        assert not widget.play_button.isEnabled()
        assert "加载" in widget.status_label.text()
    finally:
        release.set()
    qtbot.waitUntil(widget.play_button.isEnabled)
    assert threads == [threads[0]] and threads[0] != threading.get_ident()
    assert widget.wave_curve.getData()[0].size == 800
    assert widget.stft_image.image is not None
    assert "试听" in widget.wave_plot.plotItem.titleLabel.text


def test_channel_range_gain_and_scoped_stop(qtbot, record):
    playback = Playback()
    widget = panel(qtbot, record, playback)
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget.channel_list.item(1).setSelected(True)
    widget.gain_spin.setValue(12.5)
    widget._set_selection_range(0.01, 0.08)
    widget._play()
    qtbot.waitUntil(lambda: len(playback.requests) == 1 and widget.play_button.isEnabled())
    assert "失败" not in widget.status_label.text()
    assert widget.stop_button.isEnabled()
    request = playback.requests[0]
    assert request.run_id == RUN_ID
    assert request.display_channel_indices == (7, 3)
    assert request.window_start_seconds == 0.01
    assert request.window_end_seconds == 0.08
    assert request.gain_db_override == 12.5
    owner = playback.owner
    widget._stop()
    qtbot.waitUntil(lambda: playback.stops == [owner])


@pytest.mark.parametrize("failure_mode", ["exception", "result"])
def test_failed_start_reports_error_and_restores_controls(qtbot, record, failure_mode):
    playback = Playback()

    def failed_play(request, *, owner=None):
        playback.started.set()
        if failure_mode == "exception":
            raise RuntimeError("backend start failed")
        return PlaybackResult(
            PlaybackStatus.FAILED,
            request.run_id,
            PlaybackCode.BACKEND_START_FAILED,
            "Playback could not start.",
        )

    playback.play = failed_play
    widget = panel(qtbot, record, playback)
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget._play()
    qtbot.waitUntil(lambda: playback.started.is_set() and widget.play_button.isEnabled())
    assert widget.status_label.text() == "回放启动失败"
    assert widget.stop_button.isEnabled()
    assert playback.owner is None


def test_selection_stft_discards_late_result(qtbot, record, monkeypatch):
    widget = panel(qtbot, record)
    qtbot.waitUntil(widget.play_button.isEnabled)
    entered, release = threading.Event(), threading.Event()
    original = replay.prepare_replay_plots
    threads = []

    def delayed(samples, sample_rate, columns, selection):
        threads.append(threading.get_ident())
        if selection == (0.01, 0.04):
            entered.set()
            assert release.wait(5)
        return original(samples, sample_rate, columns, selection)

    monkeypatch.setattr(replay, "prepare_replay_plots", delayed)
    widget._selection_finished(0.01, 0.04)
    try:
        qtbot.waitUntil(entered.is_set)
        widget._selection_finished(0.05, 0.09)
        qtbot.waitUntil(lambda: widget._applied_plot_range == (0.05, 0.09))
    finally:
        release.set()
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)
    assert widget._applied_plot_range == (0.05, 0.09)
    assert all(value != threading.get_ident() for value in threads)


def test_close_during_audio_loading_retains_worker_and_ignores_result(qtbot, record, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = replay.prepare_replay_audio

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(replay, "prepare_replay_audio", delayed)
    widget = panel(qtbot, record)
    try:
        qtbot.waitUntil(entered.is_set)
        widget.close()
        widget.deleteLater()
        qtbot.wait(10)
        assert replay._ACTIVE_WORKERS
    finally:
        release.set()
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)


def test_close_during_start_cleans_up_late_playback(qtbot, record):
    playback = Playback()
    playback.release = threading.Event()
    widget = panel(qtbot, record, playback)
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget._play()
    try:
        qtbot.waitUntil(playback.started.is_set)
        assert not widget.play_button.isEnabled()
        assert not widget.stop_button.isEnabled()
        widget.close()
        widget.deleteLater()
        qtbot.wait(10)
    finally:
        playback.release.set()
    qtbot.waitUntil(lambda: len(playback.stops) == 1)
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)
    assert playback.owner is None


@pytest.mark.parametrize("replacement", [None, object()])
def test_shutdown_does_not_stop_a_later_owner_or_unowned_playback(qtbot, record, replacement):
    playback = Playback()
    widget = panel(qtbot, record, playback)
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget._play()
    qtbot.waitUntil(lambda: bool(playback.requests) and widget.play_button.isEnabled())
    playback.owner = replacement
    widget.shutdown()
    qtbot.waitUntil(lambda: not replay._ACTIVE_WORKERS)
    assert playback.owner is replacement
    assert playback.stops == []


def test_missing_audio_reports_unavailable(qtbot, record):
    record.source_wav_path = record.source_wav_path.parent / "missing.wav"
    widget = panel(qtbot, record)
    qtbot.waitUntil(lambda: "不可用" in widget.status_label.text())
    assert not widget.play_button.isEnabled()


def test_prepared_audio_can_be_embedded_without_loading(qtbot, record, monkeypatch):
    prepared = replay.prepare_replay_audio(record.source_wav_path)
    monkeypatch.setattr(replay, "prepare_replay_audio", lambda *args: pytest.fail("unexpected load"))
    widget = panel(qtbot, record, prepared_audio=prepared)
    assert widget.play_button.isEnabled()
    assert widget._range() == (0.0, 0.1)


def test_legacy_dialog_reject_stops_owned_playback(qtbot, record):
    playback = Playback()
    dialog = replay.RunAudioReplayDialog(record=record, playback=playback)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._play()
    owner = playback.owner
    assert owner is not None
    dialog.reject()
    qtbot.waitUntil(lambda: playback.stops == [owner])


def test_preloaded_samples_are_prepared_asynchronously(qtbot, record, monkeypatch):
    record.source_wav_path = None
    samples = np.ones((800, 2), dtype=np.float32)
    widget = panel(qtbot, record, preloaded_audio=(8000, samples))
    qtbot.waitUntil(widget.play_button.isEnabled)
    assert np.all(widget.wave_curve.getData()[1] == 1.0)


def test_stop_is_asynchronous_and_disables_both_buttons(qtbot, record):
    playback = Playback()
    entered, release = threading.Event(), threading.Event()
    original_stop = playback.stop_if_owner

    def delayed_stop(owner, timeout_seconds):
        entered.set()
        assert release.wait(5)
        return original_stop(owner, timeout_seconds)

    playback.stop_if_owner = delayed_stop
    widget = panel(qtbot, record, playback)
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget._play()
    qtbot.waitUntil(lambda: bool(playback.requests) and widget.play_button.isEnabled())
    widget._stop()
    try:
        qtbot.waitUntil(entered.is_set)
        assert not widget.play_button.isEnabled()
        assert not widget.stop_button.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(widget.play_button.isEnabled)
    assert playback.owner is None


def test_close_retries_busy_service_gate_to_stop_owned_playback(qtbot, record):
    playback = Playback()
    widget = panel(qtbot, record, playback)
    qtbot.waitUntil(widget.play_button.isEnabled)
    widget._play()
    qtbot.waitUntil(lambda: bool(playback.requests) and widget.play_button.isEnabled())
    calls = []
    original_stop = playback.stop_if_owner

    def busy_once(owner, timeout_seconds):
        calls.append(owner)
        if len(calls) == 1:
            return SimpleNamespace(code=PlaybackLifecycleCode.BUSY)
        return original_stop(owner, timeout_seconds)

    playback.stop_if_owner = busy_once
    widget.close()
    qtbot.waitUntil(lambda: len(playback.stops) == 1)
    assert len(calls) == 2
