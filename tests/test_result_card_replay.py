from types import SimpleNamespace
from uuid import uuid4
import threading

import pytest

from app.service.result_history import ResultPhase
from app.service.run_scoped_playback import PlaybackCode, PlaybackResult, PlaybackStatus
from ui.widgets.result_card_replay import ResultCardReplay


class Reader:
    def __init__(self, config=None, channels=(3, 7)):
        self.config = config or {}
        self.channels = channels
        self.calls = []
        self.release = None
        self.entered = threading.Event()

    def read_replay(self, run):
        self.calls.append((run, threading.get_ident()))
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        return SimpleNamespace(run_id=run, config=self.config, audio_message=None,
            audio=SimpleNamespace(run_id=run, channel_order=self.channels,
                frame_count=24000, sample_rate_hz=8000))


class Playback:
    def __init__(self):
        self.requests = []
        self.owner = None
        self.stops = []
        self.release = None
        self.entered = threading.Event()
        self.fail = False

    def play(self, request, *, owner):
        self.requests.append((request, threading.get_ident()))
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        if self.fail:
            raise RuntimeError('device error')
        self.owner = owner
        return PlaybackResult(PlaybackStatus.STARTED, request.run_id, PlaybackCode.STARTED, 'Playback started.')

    def stop_if_owner(self, owner, timeout):
        self.stops.append(owner)
        if self.owner is owner:
            self.owner = None


@pytest.fixture
def make_helper(qtbot):
    helpers = []
    def make(reader=None, playback=None):
        states = {}
        helper = ResultCardReplay(reader, playback)
        helper.state_changed.connect(lambda run, channels, selected, enabled, message:
            states.update({run: (channels, selected, enabled, message)}))
        helpers.append(helper)
        return helper, states
    yield make
    for helper in helpers:
        helper.shutdown()


def sync(helper, run, phase=ResultPhase.TERMINAL, revision=1):
    helper.sync((SimpleNamespace(run_id=run, phase=phase, revision=revision),))


@pytest.mark.parametrize('config,expected', [
    ({'selected_channels': [3, 7], 'vibration_view_channel_index': 3}, 7),
    ({'selected_channels': [99, 7]}, 3),
    ({}, 3),
    ({'selected_channels': ['7'], 'vibration_view_channel_index': '3'}, 7),
    ({'selected_channels': [1, 7], 'vibration_view_channel_index': True}, 3),
    ({'selected_channels': [True, -1, 'bad']}, 3),
])
def test_default_remembered_full_record_replay_off_gui_thread(make_helper, qtbot, config, expected):
    reader, playback, run = Reader(config), Playback(), str(uuid4())
    helper, states = make_helper(reader, playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][2])
    assert states[run][:2] == ((3, 7), expected)
    helper.play(run, 7)
    qtbot.waitUntil(lambda: states[run][2])
    sync(helper, run, revision=2)
    assert states[run][1] == 7
    helper.play(run, 7)
    qtbot.waitUntil(lambda: len(playback.requests) == 2 and states[run][2])
    assert len(reader.calls) == 1
    assert reader.calls[0][1] != threading.get_ident()
    for request, thread in playback.requests:
        assert thread != threading.get_ident()
        assert request.run_id == run
        assert request.display_channel_index == 7
        assert request.display_channel_indices == (7,)
        assert (request.window_start_seconds, request.window_end_seconds) == (0.0, 3.0)
        assert request.gain_db_override is None


def test_pending_start_blocks_duplicates_and_failure_can_retry(make_helper, qtbot):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    helper, states = make_helper(reader, playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][2])
    playback.release = threading.Event()
    playback.fail = True
    helper.play(run, 7)
    try:
        qtbot.waitUntil(playback.entered.is_set)
        helper.play(run, 3)
        sync(helper, run, revision=2)
        assert not states[run][2]
        assert len(playback.requests) == 1
    finally:
        playback.release.set()
    qtbot.waitUntil(lambda: states[run][2])
    assert '失败' in states[run][3]
    assert states[run][1] == 7
    playback.fail = False
    helper.play(run, 7)
    qtbot.waitUntil(lambda: states[run][2])
    assert states[run][3] == ''
    assert len(playback.requests) == 2


@pytest.mark.parametrize('phase', [ResultPhase.TERMINAL, ResultPhase.REANALYZING])
def test_active_entry_becomes_replayable_when_saved(make_helper, qtbot, phase):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    helper, states = make_helper(reader, playback)
    sync(helper, run, ResultPhase.ANALYZING)
    assert not states[run][2] and '尚未结束' in states[run][3]
    assert reader.calls == []
    sync(helper, run, phase, revision=2)
    qtbot.waitUntil(lambda: states[run][2])


@pytest.mark.parametrize('operation', ['metadata', 'play'])
@pytest.mark.parametrize('dispose', ['remove', 'shutdown', 'delete'])
def test_stale_work_after_removal_or_shutdown_cannot_publish_or_keep_playing(make_helper, qtbot, operation, dispose):
    from PyQt6.QtCore import QCoreApplication, QEvent
    reader, playback, run = Reader(), Playback(), str(uuid4())
    helper, states = make_helper(reader, playback)
    worker_source = reader if operation == 'metadata' else playback
    worker_source.release = threading.Event()
    sync(helper, run)
    if operation == 'play':
        qtbot.waitUntil(lambda: states[run][2])
        helper.play(run, 7)
    try:
        qtbot.waitUntil(worker_source.entered.is_set)
        previous = states.copy()
        if dispose == 'remove':
            helper.sync(())
        elif dispose == 'shutdown':
            helper.shutdown()
        else:
            helper.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        worker_source.release.set()
    from ui.popups.run_audio_replay_dialog import _ACTIVE_WORKERS
    qtbot.waitUntil(lambda: not _ACTIVE_WORKERS)
    assert states == previous
    assert playback.owner is None
    if operation == 'metadata':
        assert playback.requests == []


def test_removed_generation_cannot_overwrite_same_run_recreated(make_helper, qtbot):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    helper, states = make_helper(reader, playback)
    reader.release = threading.Event()
    sync(helper, run)
    qtbot.waitUntil(reader.entered.is_set)
    helper.sync(())
    helper._reader = Reader({'selected_channels': [7]})
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][2])
    reader.release.set()
    from ui.popups.run_audio_replay_dialog import _ACTIVE_WORKERS
    qtbot.waitUntil(lambda: not _ACTIVE_WORKERS)
    assert states[run][1] == 7


def test_shutdown_only_stops_owned_playback(make_helper, qtbot):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    helper, states = make_helper(reader, playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][2])
    helper.play(run, 3)
    qtbot.waitUntil(lambda: states[run][2])
    other_surface = object()
    playback.owner = other_surface
    helper.shutdown()
    qtbot.waitUntil(lambda: bool(playback.stops))
    assert playback.owner is other_surface


@pytest.mark.parametrize('missing', ['reader', 'playback', 'audio'])
def test_unavailable_has_chinese_reason(make_helper, qtbot, missing):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    if missing == 'audio':
        reader.read_replay = lambda run: SimpleNamespace(run_id=run, config={}, audio=None, audio_message='未保存录音')
    helper, states = make_helper(None if missing == 'reader' else reader,
                                  None if missing == 'playback' else playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: '正在' not in states[run][3])
    assert not states[run][2]
    assert '未' in states[run][3]


@pytest.mark.parametrize('invalid', ['identity', 'empty', 'duration', 'duplicates'])
def test_invalid_metadata_disables_replay(make_helper, qtbot, invalid):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    value = reader.read_replay(run)
    if invalid == 'identity':
        value.audio.run_id = str(uuid4())
    elif invalid == 'empty':
        value.audio.channel_order = ()
    elif invalid == 'duration':
        value.audio.sample_rate_hz = 0
    else:
        value.audio.channel_order = (3, 3)
    reader.read_replay = lambda run: value
    helper, states = make_helper(reader, playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: '正在' not in states[run][3])
    assert not states[run][2]
    assert states[run][3]


def test_refresh_retries_metadata_that_was_not_saved_yet(make_helper, qtbot):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    read = reader.read_replay
    reader.read_replay = lambda run: SimpleNamespace(run_id=run, config={}, audio=None, audio_message='未保存录音')
    helper, states = make_helper(reader, playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][3] == '未保存录音')
    reader.read_replay = read
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][2])


@pytest.mark.parametrize('outcome', ['failed_result', 'wrong_run', 'invalid_result'])
def test_nonstarted_or_wrong_run_result_is_retryable(make_helper, qtbot, outcome):
    reader, playback, run = Reader(), Playback(), str(uuid4())
    original_play = playback.play
    if outcome == 'failed_result':
        value = PlaybackResult(PlaybackStatus.FAILED, run, PlaybackCode.BACKEND_START_FAILED, 'Playback could not start.')
    elif outcome == 'wrong_run':
        value = PlaybackResult(PlaybackStatus.STARTED, str(uuid4()), PlaybackCode.STARTED, 'Playback started.')
    else:
        value = object()
    playback.play = lambda request, owner: value
    helper, states = make_helper(reader, playback)
    sync(helper, run)
    qtbot.waitUntil(lambda: states[run][2])
    helper.play(run, 3)
    qtbot.waitUntil(lambda: states[run][2])
    assert '失败' in states[run][3]
    playback.play = original_play
    helper.play(run, 3)
    qtbot.waitUntil(lambda: states[run][2])
    assert states[run][3] == ''


def test_two_runs_refresh_during_metadata_keep_distinct_identities(make_helper, qtbot):
    reader, playback = Reader(), Playback()
    reader.release = threading.Event()
    helper, states = make_helper(reader, playback)
    runs = [str(uuid4()), str(uuid4())]
    cards = tuple(SimpleNamespace(run_id=r, phase=ResultPhase.TERMINAL, revision=1) for r in runs)
    helper.sync(cards)
    try:
        qtbot.waitUntil(lambda: len(reader.calls) == 2)
        helper.sync(cards[::-1])
        assert len(reader.calls) == 2
        assert not any(state[2] for state in states.values())
    finally:
        reader.release.set()
    qtbot.waitUntil(lambda: all(state[2] for state in states.values()))
    helper.play(runs[0], 7)
    qtbot.waitUntil(lambda: states[runs[0]][2])
    helper.play(runs[1], 3)
    qtbot.waitUntil(lambda: states[runs[1]][2])
    assert [(r.run_id, r.display_channel_index) for r, _ in playback.requests] == [(runs[0], 7), (runs[1], 3)]
    assert states[runs[0]][1] == 7 and states[runs[1]][1] == 3


def test_garbage_collected_qt_owner_does_not_crash():
    import os
    import subprocess
    import sys
    code = '''
import gc
from PyQt6.QtWidgets import QApplication, QWidget
from ui.widgets.result_card_replay import ResultCardReplay
app = QApplication([])
for _ in range(30):
    parent = QWidget()
    helper = ResultCardReplay(None, None, parent)
    parent.helper = helper
    helper.host = parent
    del helper, parent
    gc.collect()
    app.processEvents()
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('in_flight', [False, True])
def test_provider_replacement_retires_previous_service_operations(qtbot, in_flight):
    first, second = Playback(), Playback()
    current = [first]
    helper = ResultCardReplay(Reader(), playback_provider=lambda: current[0])
    states = {}
    helper.state_changed.connect(lambda run, channels, selected, enabled, message:
                                states.update({run: enabled}))
    runs = [str(uuid4()), str(uuid4())]
    helper.sync(tuple(SimpleNamespace(run_id=run, phase=ResultPhase.TERMINAL)
                      for run in runs))
    qtbot.waitUntil(lambda: all(states.values()))
    if in_flight:
        first.release = threading.Event()
    try:
        helper.play(runs[0], 3)
        qtbot.waitUntil(first.entered.is_set)
        if not in_flight:
            qtbot.waitUntil(lambda: states[runs[0]])
        current[0] = second
        helper.play(runs[1], 7)
        qtbot.waitUntil(lambda: second.owner is not None and states[runs[1]])
        newer_owner = second.owner
        if first.release:
            first.release.set()
        from ui.popups.run_audio_replay_dialog import _ACTIVE_WORKERS
        qtbot.waitUntil(lambda: not _ACTIVE_WORKERS)
        assert first.owner is None
        assert first.stops
        assert second.owner is newer_owner
        assert second.stops == []
    finally:
        if first.release:
            first.release.set()
        helper.shutdown()
    qtbot.waitUntil(lambda: not _ACTIVE_WORKERS)
    assert second.owner is None


def test_same_service_replay_does_not_race_delayed_stop_gate(qtbot, monkeypatch):
    import ui.widgets.result_card_replay as replay_module
    class GatedPlayback(Playback):
        def __init__(self):
            super().__init__()
            self.gate = threading.Lock()
            self.stop_entered = threading.Event()
            self.release_stop = threading.Event()
            self.results = []

        def play(self, request, *, owner):
            if not self.gate.acquire(blocking=False):
                result = PlaybackResult(PlaybackStatus.UNAVAILABLE, request.run_id,
                                        PlaybackCode.BUSY,
                                        'Playback is busy with another operation.')
            else:
                try:
                    result = super().play(request, owner=owner)
                finally:
                    self.gate.release()
            self.results.append(result.code)
            return result

        def stop_if_owner(self, owner, timeout):
            with self.gate:
                self.stop_entered.set()
                assert self.release_stop.wait(5)
                super().stop_if_owner(owner, timeout)

    playback = GatedPlayback()
    start_worker = replay_module._start_worker
    def ordered_worker(kind, generation, work, receiver=None):
        result = start_worker(kind, generation, work, receiver)
        # Force the adverse scheduling order, retaining real workers and gate.
        if kind == 'close':
            assert playback.stop_entered.wait(2)
        return result
    monkeypatch.setattr(replay_module, '_start_worker', ordered_worker)
    helper = ResultCardReplay(Reader(), playback_provider=lambda: playback)
    states = {}
    helper.state_changed.connect(lambda run, channels, selected, enabled, message:
                                states.update({run: (enabled, message)}))
    run = str(uuid4())
    sync(helper, run)
    try:
        qtbot.waitUntil(lambda: states[run][0])
        for _ in range(6):
            helper.play(run, 7)
            qtbot.waitUntil(lambda: states[run][0])
            assert playback.results[-1] is PlaybackCode.STARTED
            assert states[run][1] == ''
        assert playback.stops == []
        assert len(playback.requests) == 6
    finally:
        playback.release_stop.set()
        helper.shutdown()
    from ui.popups.run_audio_replay_dialog import _ACTIVE_WORKERS
    qtbot.waitUntil(lambda: not _ACTIVE_WORKERS)


@pytest.mark.parametrize('failure', ['none', 'exception'])
def test_unavailable_provider_disables_and_refresh_recovers(qtbot, failure):
    playback = Playback()
    current = [None]
    lookups = []
    def provider():
        lookups.append(threading.get_ident())
        if current[0] is None and failure == 'exception':
            raise RuntimeError('not active')
        return current[0]
    helper = ResultCardReplay(Reader(), playback_provider=provider)
    states = {}
    helper.state_changed.connect(lambda run, channels, selected, enabled, message:
                                states.update({run: (enabled, message)}))
    run = str(uuid4())
    try:
        helper.sync(())
        assert lookups == []
        sync(helper, run)
        qtbot.waitUntil(lambda: states[run][0])
        helper.play(run, 3)
        assert states[run][0] is False
        assert '播放服务未就绪' in states[run][1]
        sync(helper, run)
        assert states[run][0] is False
        current[0] = playback
        sync(helper, run)
        assert states[run] == (True, '')
        assert playback.requests == []
        lookups_before_click = len(lookups)
        helper.play(run, 3)
        qtbot.waitUntil(lambda: bool(playback.requests) and states[run][0])
        assert len(lookups) == lookups_before_click + 1
        assert all(thread == threading.get_ident() for thread in lookups)
    finally:
        helper.shutdown()
