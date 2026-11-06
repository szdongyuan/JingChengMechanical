"""Per-history-entry replay state and background metadata/start operations."""
from dataclasses import dataclass, field
import math
import threading

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from app.service.result_history import ResultPhase
from app.service.run_scoped_playback import (
    PlaybackCode, PlaybackResult, PlaybackStatus, RunPlaybackRequest,
)
from ui.popups.run_audio_replay_dialog import _start_worker, _stop_owned


@dataclass
class _Operation:
    playback: object
    owner: object = field(default_factory=object)
    closed: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Entry:
    generation: int
    audio: object = None
    selected: int | None = None
    pending: bool = False
    message: str = ''
    available: bool = False
    closed: threading.Event = field(default_factory=threading.Event)
    operation: _Operation | None = None


class ResultCardReplay(QObject):
    state_changed = pyqtSignal(str, object, object, bool, str)

    def __init__(self, reader, playback=None, parent=None, *, playback_provider=None):
        super().__init__(parent)
        self._reader, self._playback = reader, playback
        self._playback_provider = playback_provider
        self._playback_available = None
        self._entries = {}
        self._generation = 0
        self._closed = False
        self._lifetime = _ReplayLifetime(self._entries)
        self.destroyed.connect(self._lifetime.dispose)

    def sync(self, cards):
        if self._closed:
            return
        retained = {card.run_id for card in cards}
        for run in tuple(self._entries):
            if run not in retained:
                _retire(self._entries.pop(run))
        if retained and self._playback_available is False:
            self._resolve_playback()
        for card in cards:
            available = card.phase in (ResultPhase.TERMINAL, ResultPhase.REANALYZING)
            entry = self._entries.get(card.run_id)
            if entry is None or entry.available != available:
                selected = None if entry is None else entry.selected
                if entry is not None:
                    _retire(entry)
                self._generation += 1
                entry = _Entry(self._generation, selected=selected,
                               available=available)
                self._entries[card.run_id] = entry
                self._load(card.run_id, entry)
            elif entry.available and entry.audio is None and not entry.pending:
                self._load(card.run_id, entry)
            self._publish(card.run_id, entry)

    def _load(self, run, entry):
        if not entry.available:
            entry.message = '录音尚未结束，暂不可重放'
        elif (not callable(getattr(self._reader, 'read_replay', None))
              or (self._playback_provider is None and not _valid_playback(self._playback))):
            entry.message = '录音重放不可用：播放服务未就绪'
        else:
            entry.pending = True
            entry.message = '正在读取录音信息…'
            reader = self._reader
            _start_worker('metadata', entry.generation,
                          lambda: reader.read_replay(run), self._completed)

    def _publish(self, run, entry):
        channels = () if entry.audio is None else entry.audio.channel_order
        service_ready = self._playback_available is not False
        message = entry.message
        if not service_ready and entry.available and entry.audio is not None:
            message = '录音重放不可用：播放服务未就绪'
        self.state_changed.emit(run, channels, entry.selected,
            entry.available and entry.audio is not None and not entry.pending
            and service_ready, message)

    def _resolve_playback(self):
        # Resolve on the GUI thread: the provider may construct the service.
        try:
            playback = (self._playback_provider() if self._playback_provider
                        is not None else self._playback)
        except Exception:
            playback = None
        self._playback_available = _valid_playback(playback)
        return playback

    @pyqtSlot(str, int)
    def play(self, run, channel):
        entry = self._entries.get(run)
        if (self._closed or entry is None or entry.pending or not entry.available
                or entry.audio is None or channel not in entry.audio.channel_order):
            return
        entry.selected = channel
        playback = self._resolve_playback()
        if not self._playback_available:
            for other_run, other in self._entries.items():
                self._publish(other_run, other)
            return
        for other in self._entries.values():
            operation = other.operation
            if operation is not None and operation.playback is not playback:
                _retire_operation(operation)
        if (entry.operation is None or entry.operation.closed.is_set()
                or entry.operation.playback is not playback):
            entry.operation = _Operation(playback)
        audio = entry.audio
        request = RunPlaybackRequest(run, channel, 0.0,
            float(audio.frame_count) / float(audio.sample_rate_hz),
            display_channel_indices=(channel,))
        entry.pending = True
        entry.message = '正在启动重放…'
        self._publish(run, entry)
        closed, owner = entry.operation.closed, entry.operation.owner

        def work():
            try:
                if closed.is_set():
                    return None
                result = playback.play(request, owner=owner)
                return None if closed.is_set() else result
            finally:
                if closed.is_set():
                    _stop_owned(playback, owner, wait_for_gate=True)

        _start_worker('play', entry.generation, work, self._completed)

    @pyqtSlot(str, int, object, object)
    def _completed(self, kind, generation, value, error):
        if self._closed:
            return
        match = next(((run, entry) for run, entry in self._entries.items()
                      if entry.generation == generation), None)
        if match is None:
            return
        run, entry = match
        entry.pending = False
        if kind == 'metadata':
            if error is not None:
                entry.message = '录音信息读取失败'
            elif not _valid_metadata(value, run):
                entry.message = '录音信息无效或记录编号不匹配'
            else:
                entry.audio = value.audio
                entry.message = value.audio_message or ''
                if entry.audio is not None:
                    config = value.config
                    selected = config.get('selected_channels', ())
                    if not isinstance(selected, (list, tuple)):
                        selected = ()
                    vibration = _config_channel(config.get('vibration_view_channel_index'))
                    primary = next((c for value in selected
                                    if (c := _config_channel(value)) is not None
                                    and c != vibration), None)
                    channels = entry.audio.channel_order
                    if entry.selected not in channels:
                        entry.selected = primary if primary in channels else channels[0]
        elif (error is not None or type(value) is not PlaybackResult
              or value.status is not PlaybackStatus.STARTED
              or value.code is not PlaybackCode.STARTED or value.run_id != run):
            entry.message = '重放启动失败，请重试'
        else:
            entry.message = ''
        self._publish(run, entry)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        if self._lifetime in _LIFETIMES:
            self._lifetime.dispose()


def _config_channel(value):
    # Match the history metric convention, including legacy numeric strings.
    if isinstance(value, bool):
        return None
    try:
        channel = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return channel if channel >= 0 else None


def _valid_metadata(value, run):
    try:
        if value.run_id != run or not isinstance(value.config, dict):
            return False
        audio = value.audio
        if audio is None:
            return True
        channels = audio.channel_order
        return (audio.run_id == run and isinstance(channels, tuple) and bool(channels)
                and all(type(c) is int and c >= 0 for c in channels)
                and len(set(channels)) == len(channels)
                and type(audio.frame_count) is int and audio.frame_count > 0
                and type(audio.sample_rate_hz) is int and audio.sample_rate_hz > 0
                and math.isfinite(audio.frame_count / audio.sample_rate_hz))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _valid_playback(playback):
    return (callable(getattr(playback, 'play', None))
            and callable(getattr(playback, 'stop_if_owner', None)))


def _retire_operation(operation):
    if operation.closed.is_set():
        return
    operation.closed.set()
    _start_worker('close', 0, lambda: _stop_owned(
        operation.playback, operation.owner, wait_for_gate=True))


def _retire(entry):
    if entry.closed.is_set():
        return
    entry.closed.set()
    if entry.operation is not None:
        _retire_operation(entry.operation)


def _dispose_entries(entries):
    for entry in tuple(entries.values()):
        _retire(entry)
    entries.clear()


# A separate zero-argument Qt slot avoids wrapping a half-destroyed QObject in
# Python during cyclic GC. It owns detached data only, never the host/helper.
_LIFETIMES = set()


class _ReplayLifetime(QObject):
    def __init__(self, entries):
        super().__init__()
        self.entries = entries
        _LIFETIMES.add(self)

    @pyqtSlot()
    def dispose(self):
        _dispose_entries(self.entries)
        _LIFETIMES.discard(self)
        self.deleteLater()
