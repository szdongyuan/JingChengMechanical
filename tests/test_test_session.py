"""Tests for TestSession lifecycle."""
from __future__ import annotations

import json
import os
import math
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

from app.service.analysis import dsp
from app.service.analysis import test_session as test_session_module
from app.service.analysis.frames import JudgmentResult
from app.service.analysis.session_capture import (
    CapturedAudioBlock,
    RecorderAttachment,
    ResolvedCaptureBounds,
    frames_for_duration,
)
from app.service.analysis.test_manager import TestManager
from app.service.analysis.test_session import TestSession
from app.service.config_service import _CHANNEL_DEFAULTS
from model.audio_data_table import AudioDataTable


class _SignalStub:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback, connection_type=None):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


def _make_mock_bus():
    bus = MagicMock()
    bus.log = MagicMock()
    bus.log.emit = MagicMock()
    bus.status = MagicMock()
    bus.status.emit = MagicMock()
    bus.result_changed = MagicMock()
    bus.result_changed.emit = MagicMock()
    bus.stft_updated = MagicMock()
    bus.stft_updated.emit = MagicMock()
    bus.spl_updated = MagicMock()
    bus.spl_updated.emit = MagicMock()
    bus.loudness_updated = MagicMock()
    bus.loudness_updated.emit = MagicMock()
    bus.octave_updated = MagicMock()
    bus.octave_updated.emit = MagicMock()
    bus.fft_updated = MagicMock()
    bus.fft_updated.emit = MagicMock()
    bus.recording_progress = MagicMock()
    bus.recording_progress.emit = MagicMock()
    bus.test_started = MagicMock()
    bus.test_started.emit = MagicMock()
    bus.test_finished = MagicMock()
    bus.test_finished.emit = MagicMock()
    bus.factory_display_channel_changed = _SignalStub()
    return bus


def _make_config(**overrides) -> dict:
    config = {
        "config_id": 1,
        "config_name": "Test",
        "record_duration": 2.0,
        "analysis_duration": 1.0,
        "selected_channels": [0],
        "channels": [{
            "channel_index": 0,
            "stft_nfft": 1024,
            "fft_nfft": 4096,
            "stft_overlap": 0.5,
            "spl_weighting": "Z",
            "spl_time_constant": 0.125,
            "spl_upper_limit": 85.0,
            "spl_lower_limit": 40.0,
            "fft_band_limits": [],
            "oct_weighting": "Z",
            "oct_band_limits": [],
        }],
        "input_device_name": "Test Mic",
        "input_device_hostapi": "MME",
        "input_device_samplerate": 44100,
        "output_device_name": None,
    }
    config.update(overrides)
    return config


class _FakeRecorder:
    def __init__(self, *args, **kwargs):
        self.captured_block_ready = _SignalStub()
        self.recording_done = _SignalStub()
        self.error_occurred = _SignalStub()
        self._sample_rate = float(kwargs.get("sample_rate", 44100.0))
        self.started = False

    def request_capture(self, *, record_duration):
        return object()

    def release_capture(self, attachment):
        return None

    def get_capture_buffer(self, attachment, *, retained_frames):
        return np.empty((0, 1), dtype=np.float32)

    def isRunning(self):
        return False

    def start(self):
        self.started = True


class _CapturingLiveDisplayDispatcher:
    def __init__(self, *, fail: Exception | None = None):
        self.calls = []
        self.fail = fail
        self.stopped = False

    def submit(
        self,
        chunk,
        *,
        elapsed_time,
        frame_run_id,
        live_loudness_channel_index,
        sample_start=None,
        sample_end=None,
    ):
        self.calls.append(
            {
                "chunk": np.asarray(chunk).copy(),
                "elapsed_time": elapsed_time,
                "frame_run_id": frame_run_id,
                "live_loudness_channel_index": live_loudness_channel_index,
                "sample_start": sample_start,
                "sample_end": sample_end,
            }
        )
        if self.fail is not None:
            raise self.fail
        return True

    def stop(self):
        self.stopped = True


def _capture_owner(
    session: TestSession,
    *,
    attach_sample: int = 10,
    attach_monotonic: float = 20.0,
    resolved: bool = True,
) -> RecorderAttachment:
    warmup_samples = frames_for_duration(session._sample_rate, 0.05)
    target_samples = frames_for_duration(
        session._sample_rate,
        session._record_duration,
    )
    owner = RecorderAttachment(
        request_monotonic=attach_monotonic,
        warmup_samples=warmup_samples,
        target_samples=target_samples,
    )
    if resolved:
        owner._resolve_once(
            ResolvedCaptureBounds(
                attach_sample=attach_sample,
                attach_monotonic=attach_monotonic,
                warmup_samples=warmup_samples,
                target_samples=target_samples,
            )
        )
    return owner


def _captured_block(sample_start: int, values) -> CapturedAudioBlock:
    data = np.asarray(values, dtype=np.float32)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    return CapturedAudioBlock(
        data=data,
        sample_start=sample_start,
        sample_end=sample_start + data.shape[0],
        monotonic_start=None,
        monotonic_end=None,
    )


def _bind_resolved_capture(
    session: TestSession,
    *,
    attach_sample: int = 0,
) -> tuple[object, int]:
    token = object()
    owner = _capture_owner(session, attach_sample=attach_sample)
    session.prepare_for_attach(token)
    session.attach_to_recorder(owner, source_token=token)
    return token, owner.resolution.retained_start


def _emit_captured_data(
    session: TestSession,
    token: object,
    sample_start: int,
    data: np.ndarray,
) -> None:
    session._on_captured_block(
        _captured_block(sample_start, data),
        source_token=token,
    )


class TestTestManagerDisplayChannelRouting:
    def test_display_channel_change_is_stored_and_applied_to_running_session(self):
        bus = _make_mock_bus()
        manager = TestManager(bus, SimpleNamespace())
        session = SimpleNamespace(
            _config=_make_config(selected_channels=[0, 1]),
            set_live_loudness_channel_index=MagicMock(),
        )
        manager._sessions[1] = session

        bus.factory_display_channel_changed.emit(1, 1)

        assert manager._live_loudness_channel_by_config_id[1] == 1
        session.set_live_loudness_channel_index.assert_called_once_with(1)

    def test_start_applies_stored_display_channel_after_pipeline_setup(
        self,
        monkeypatch,
    ):
        events = []

        class FakeSession:
            def __init__(
                self,
                *,
                config,
                bus,
                ai_service,
                mic_cal,
                workorder,
                audio_dir,
            ):
                self._config = config
                self.config_id = config["config_id"]
                self._sample_rate = float(config.get("input_device_samplerate", 44100))
                self._workorder = workorder
                self._on_chunk = MagicMock()
                self._live_loudness_channel_index = None

            def _setup_pipelines(self):
                events.append("setup")
                self._live_loudness_channel_index = self._config["selected_channels"][0]

            def set_live_loudness_channel_index(self, channel_index):
                events.append(("set_live", channel_index))
                self._live_loudness_channel_index = channel_index

            def set_monitor_feed(self, callback):
                self._monitor_feed = callback

            def prepare_for_attach(self, source_token):
                events.append("prepare")
                self._source_token = source_token

            def attach_to_recorder(self, attachment, *, source_token):
                assert source_token is self._source_token
                self.capture_attachment = attachment
                events.append(("attach", attachment))

            def _on_captured_block(self, block, *, source_token):
                return None

            def freeze_capture(self):
                return 0

        config = _make_config(selected_channels=[0, 1])
        config["channels"] = [
            config["channels"][0],
            {**config["channels"][0], "channel_index": 1},
        ]
        services = SimpleNamespace(
            config_service=SimpleNamespace(load_config=MagicMock(return_value=config)),
            hss_service=SimpleNamespace(
                load_io_devices=MagicMock(
                    return_value={
                        "mic_cal": {},
                        "input": {"samplerate": 44100.0, "channel_count": 2},
                    }
                )
            ),
            ai_service=MagicMock(),
        )
        bus = _make_mock_bus()
        manager = TestManager(bus, services)
        bus.factory_display_channel_changed.emit(1, 1)
        monkeypatch.setattr(
            "app.service.analysis.test_manager.TestSession",
            FakeSession,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            _FakeRecorder,
        )

        assert manager.start({"config_id": 1, "rotation_speed": 1450}) is True

        assert events[:3] == ["setup", ("set_live", 1), "prepare"]
        assert manager._sessions[1]._live_loudness_channel_index == 1
        assert manager._sessions[1]._workorder["rotation_speed"] == 1450
        assert type(manager._sessions[1]._workorder["rotation_speed"]) is int

    def test_start_ignores_stored_display_channel_outside_selected_channels(
        self,
        monkeypatch,
    ):
        events = []

        class FakeSession:
            def __init__(
                self,
                *,
                config,
                bus,
                ai_service,
                mic_cal,
                workorder,
                audio_dir,
            ):
                self._config = config
                self.config_id = config["config_id"]
                self._sample_rate = float(config.get("input_device_samplerate", 44100))
                self._on_chunk = MagicMock()
                self._live_loudness_channel_index = None

            def _setup_pipelines(self):
                self._live_loudness_channel_index = self._config["selected_channels"][0]

            def set_live_loudness_channel_index(self, channel_index):
                events.append(("set_live", channel_index))
                self._live_loudness_channel_index = channel_index

            def set_monitor_feed(self, callback):
                self._monitor_feed = callback

            def prepare_for_attach(self, source_token):
                self._source_token = source_token

            def attach_to_recorder(self, attachment, *, source_token):
                assert source_token is self._source_token
                self.capture_attachment = attachment

            def _on_captured_block(self, block, *, source_token):
                return None

            def freeze_capture(self):
                return 0

        config = _make_config(selected_channels=[0, 1])
        services = SimpleNamespace(
            config_service=SimpleNamespace(load_config=MagicMock(return_value=config)),
            hss_service=SimpleNamespace(
                load_io_devices=MagicMock(
                    return_value={
                        "mic_cal": {},
                        "input": {"samplerate": 44100.0, "channel_count": 2},
                    }
                )
            ),
            ai_service=MagicMock(),
        )
        bus = _make_mock_bus()
        manager = TestManager(bus, services)
        bus.factory_display_channel_changed.emit(1, 3)
        monkeypatch.setattr(
            "app.service.analysis.test_manager.TestSession",
            FakeSession,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            _FakeRecorder,
        )

        assert manager.start({"config_id": 1}) is True

        assert events == []
        assert manager._sessions[1]._live_loudness_channel_index == 0


class TestTestSessionInit:
    def test_init(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "123", "model": "X"},
            audio_dir="/tmp/audio",
        )
        assert session.config_id == 1
        assert session._record_duration == 2.0


class TestTestSessionCaptureRouting:
    @staticmethod
    def _session(*, sample_rate: float = 100.0, record_duration: float = 0.1):
        bus = _make_mock_bus()
        session = TestSession(
            config=_make_config(
                input_device_samplerate=sample_rate,
                record_duration=record_duration,
            ),
            bus=bus,
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        return session, bus

    def test_unbound_capture_buffers_exact_block_and_publishes_only_zero_progress(self):
        session, bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        monitor_calls = []
        session._live_display_dispatcher = dispatcher
        session.set_monitor_feed(lambda data: monitor_calls.append(data.copy()))
        token = object()
        session.prepare_for_attach(token)
        block = _captured_block(10, np.arange(8, dtype=np.float32))

        session._on_captured_block(block, source_token=token)

        assert monitor_calls == []
        assert dispatcher.calls == []
        bus.recording_progress.emit.assert_called_once_with(1, 0.0)

        owner = _capture_owner(session, attach_sample=10)
        session.attach_to_recorder(owner, source_token=token)

        assert len(monitor_calls) == 1
        np.testing.assert_array_equal(monitor_calls[0][:, 0], [5.0, 6.0, 7.0])
        np.testing.assert_array_equal(dispatcher.calls[0]["chunk"], monitor_calls[0])

    def test_prewarmup_block_reaches_neither_monitor_nor_live_display(self):
        session, bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        monitor = MagicMock()
        session._live_display_dispatcher = dispatcher
        session.set_monitor_feed(monitor)
        token = object()
        session.prepare_for_attach(token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=token,
        )

        session._on_captured_block(
            _captured_block(10, np.arange(5, dtype=np.float32)),
            source_token=token,
        )

        monitor.assert_not_called()
        assert dispatcher.calls == []
        assert bus.recording_progress.emit.call_args.args == (1, 0.0)
        assert session.captured_frame_count == 0

    def test_crossing_block_fans_retained_suffix_to_monitor_before_display(self):
        session, bus = self._session()
        events = []

        class OrderedDispatcher(_CapturingLiveDisplayDispatcher):
            def submit(self, chunk, **kwargs):
                events.append(("display", np.asarray(chunk).copy()))
                return super().submit(chunk, **kwargs)

        dispatcher = OrderedDispatcher()
        session._live_display_dispatcher = dispatcher
        session.set_monitor_feed(
            lambda data: events.append(("monitor", np.asarray(data).copy()))
        )
        token = object()
        session.prepare_for_attach(token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=token,
        )

        session._on_captured_block(
            _captured_block(12, np.arange(12, 19, dtype=np.float32)),
            source_token=token,
        )

        assert [kind for kind, _ in events] == ["monitor", "display"]
        np.testing.assert_array_equal(events[0][1][:, 0], [15.0, 16.0, 17.0, 18.0])
        assert dispatcher.calls[0]["sample_start"] == 0
        assert dispatcher.calls[0]["sample_end"] == 4
        assert dispatcher.calls[0]["elapsed_time"] == pytest.approx(0.04)
        assert bus.recording_progress.emit.call_args.args == (1, pytest.approx(0.04))

    def test_adjacent_blocks_use_exact_sample_elapsed_at_non_round_rate(self):
        session, bus = self._session(sample_rate=101.0, record_duration=0.1)
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        session._on_session_done_cb = MagicMock()
        token = object()
        session.prepare_for_attach(token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=token,
        )

        session._on_captured_block(
            _captured_block(16, np.arange(4, dtype=np.float32)),
            source_token=token,
        )
        session._on_captured_block(
            _captured_block(20, np.arange(4, 11, dtype=np.float32)),
            source_token=token,
        )

        assert [call["sample_start"] for call in dispatcher.calls] == [0, 4]
        assert [call["sample_end"] for call in dispatcher.calls] == [4, 11]
        assert [call["elapsed_time"] for call in dispatcher.calls] == pytest.approx(
            [4 / 101.0, 11 / 101.0]
        )
        assert [call.args[1] for call in bus.recording_progress.emit.call_args_list] == pytest.approx(
            [4 / 101.0, 11 / 101.0]
        )
        session._on_session_done_cb.assert_called_once_with(1)

    def test_target_crossing_is_clipped_and_completion_callback_runs_after_unlock(self):
        session, bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        callback_lock_states = []
        session._on_session_done_cb = lambda config_id: callback_lock_states.append(
            (config_id, session._capture_lock._is_owned())
        )
        token = object()
        session.prepare_for_attach(token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=token,
        )

        session._on_captured_block(
            _captured_block(14, np.arange(14, 30, dtype=np.float32)),
            source_token=token,
        )
        session._on_captured_block(
            _captured_block(25, np.arange(25, 28, dtype=np.float32)),
            source_token=token,
        )

        assert dispatcher.calls[0]["sample_start"] == 0
        assert dispatcher.calls[0]["sample_end"] == 10
        np.testing.assert_array_equal(
            dispatcher.calls[0]["chunk"][:, 0],
            np.arange(15, 25, dtype=np.float32),
        )
        assert bus.recording_progress.emit.call_args.args == (1, pytest.approx(0.1))
        assert callback_lock_states == [(1, False)]

    def test_freeze_linearizes_after_inflight_fanout_and_rejects_later_blocks(self):
        session, _bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        monitor_entered = threading.Event()
        release_monitor = threading.Event()
        session._live_display_dispatcher = dispatcher

        def blocking_monitor(_data):
            monitor_entered.set()
            assert release_monitor.wait(2)

        session.set_monitor_feed(blocking_monitor)
        token = object()
        session.prepare_for_attach(token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=token,
        )
        callback_thread = threading.Thread(
            target=session._on_captured_block,
            args=(_captured_block(15, np.arange(4, dtype=np.float32)),),
            kwargs={"source_token": token},
        )
        callback_thread.start()
        assert monitor_entered.wait(2)

        frozen_counts = []
        freeze_thread = threading.Thread(
            target=lambda: frozen_counts.append(session.freeze_capture())
        )
        freeze_thread.start()
        release_monitor.set()
        callback_thread.join(2)
        freeze_thread.join(2)

        assert not callback_thread.is_alive()
        assert not freeze_thread.is_alive()
        assert frozen_counts == [4]
        assert len(dispatcher.calls) == 1

        session._on_captured_block(
            _captured_block(19, np.arange(4, 8, dtype=np.float32)),
            source_token=token,
        )
        assert len(dispatcher.calls) == 1
        assert session.freeze_capture() == 4

    def test_freeze_winning_prevents_callback_fanout(self):
        session, _bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        monitor = MagicMock()
        session._live_display_dispatcher = dispatcher
        session.set_monitor_feed(monitor)
        token = object()
        session.prepare_for_attach(token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=token,
        )

        assert session.freeze_capture() == 0
        session._on_captured_block(
            _captured_block(15, np.arange(4, dtype=np.float32)),
            source_token=token,
        )

        monitor.assert_not_called()
        assert dispatcher.calls == []
        assert session.captured_frame_count == 0

    def test_rotated_source_token_drops_rejected_generation_pending_and_delayed_blocks(self):
        session, bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        rejected_token = object()
        accepted_token = object()
        rejected_block = _captured_block(15, np.full(3, -1.0, dtype=np.float32))
        accepted_block = _captured_block(15, np.arange(3, dtype=np.float32))

        session.prepare_for_attach(rejected_token)
        session._on_captured_block(rejected_block, source_token=rejected_token)
        session.prepare_for_attach(accepted_token)
        session._on_captured_block(rejected_block, source_token=rejected_token)
        session.attach_to_recorder(
            _capture_owner(session, attach_sample=10),
            source_token=accepted_token,
        )
        session._on_captured_block(accepted_block, source_token=accepted_token)

        assert len(dispatcher.calls) == 1
        np.testing.assert_array_equal(
            dispatcher.calls[0]["chunk"][:, 0],
            np.arange(3, dtype=np.float32),
        )
        assert bus.recording_progress.emit.call_count == 2

    def test_callback_first_pending_append_is_drained_once_by_bind(self):
        session, bus = self._session()
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        token = object()
        session.prepare_for_attach(token)
        pending_emitted = threading.Event()
        release_callback = threading.Event()

        def block_pending_progress(*_args):
            pending_emitted.set()
            assert release_callback.wait(2)

        bus.recording_progress.emit.side_effect = block_pending_progress
        callback_thread = threading.Thread(
            target=session._on_captured_block,
            args=(_captured_block(15, np.arange(3, dtype=np.float32)),),
            kwargs={"source_token": token},
        )
        callback_thread.start()
        assert pending_emitted.wait(2)

        bind_started = threading.Event()

        def bind():
            bind_started.set()
            session.attach_to_recorder(
                _capture_owner(session, attach_sample=10),
                source_token=token,
            )

        bind_thread = threading.Thread(target=bind)
        bind_thread.start()
        assert bind_started.wait(2)
        release_callback.set()
        callback_thread.join(2)
        bind_thread.join(2)

        assert not callback_thread.is_alive()
        assert not bind_thread.is_alive()
        assert len(dispatcher.calls) == 1
        np.testing.assert_array_equal(
            dispatcher.calls[0]["chunk"][:, 0],
            np.arange(3, dtype=np.float32),
        )

    def test_bind_first_drains_older_pending_before_callback_processes_directly(
        self,
        monkeypatch,
    ):
        session, _bus = self._session()
        monitor_chunks = []
        session.set_monitor_feed(lambda data: monitor_chunks.append(data.copy()))
        token = object()
        session.prepare_for_attach(token)
        session._on_captured_block(
            _captured_block(15, [0.0, 1.0]),
            source_token=token,
        )

        real_window = test_session_module.SessionCaptureWindow
        bind_holds_lock = threading.Event()
        release_bind = threading.Event()

        def blocking_window(**kwargs):
            bind_holds_lock.set()
            assert release_bind.wait(2)
            return real_window(**kwargs)

        monkeypatch.setattr(
            test_session_module,
            "SessionCaptureWindow",
            blocking_window,
        )
        bind_thread = threading.Thread(
            target=session.attach_to_recorder,
            args=(_capture_owner(session, attach_sample=10),),
            kwargs={"source_token": token},
        )
        bind_thread.start()
        assert bind_holds_lock.wait(2)

        callback_started = threading.Event()

        def callback():
            callback_started.set()
            session._on_captured_block(
                _captured_block(17, [2.0, 3.0, 4.0]),
                source_token=token,
            )

        callback_thread = threading.Thread(target=callback)
        callback_thread.start()
        assert callback_started.wait(2)
        release_bind.set()
        bind_thread.join(2)
        callback_thread.join(2)

        assert not bind_thread.is_alive()
        assert not callback_thread.is_alive()
        assert len(monitor_chunks) == 2
        np.testing.assert_array_equal(monitor_chunks[0][:, 0], [0.0, 1.0])
        np.testing.assert_array_equal(monitor_chunks[1][:, 0], [2.0, 3.0, 4.0])


class TestTestSessionProcessing:
    def test_setup_pipelines_uses_channel_defaults_when_selected_channel_config_missing(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["selected_channels"] = [1]
        config["channels"] = []
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )

        session._setup_pipelines()

        assert len(session._pipelines) == 1
        pipeline = session._pipelines[0]
        assert pipeline.channel_index == 1
        assert pipeline._cfg["stft_nfft"] == _CHANNEL_DEFAULTS["stft_nfft"]
        assert pipeline._cfg["spl_weighting"] == _CHANNEL_DEFAULTS["spl_weighting"]

    def test_setup_pipelines_creates_fresh_independent_spl_weighting_state(self):
        config = _make_config()
        config["input_device_samplerate"] = 48000
        config["channels"][0]["spl_weighting"] = "A"
        first_session = TestSession(
            config=config,
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        second_session = TestSession(
            config=config,
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )

        first_session._setup_pipelines()
        second_session._setup_pipelines()
        try:
            first_detector = first_session._pipelines[0]._spl_detector
            second_detector = second_session._pipelines[0]._spl_detector
            prefix = np.full(4000, 0.5, dtype=np.float64)
            times = np.arange(9600, dtype=np.float64) / 48000.0
            signal = (
                np.sqrt(2.0)
                * 20e-6
                * (10.0 ** (94.0 / 20.0))
                * np.sin(2.0 * np.pi * 31.5 * times)
            )

            first_detector.process(prefix)
            actual = second_detector.process(signal)
            pristine = dsp.SplDetector(48000.0, "A", 0.125).process(signal)

            assert first_detector is not second_detector
            assert (
                first_detector._frequency_weighting
                is not second_detector._frequency_weighting
            )
            assert actual == pytest.approx(pristine, abs=1e-12)
        finally:
            first_session.stop_live_display()
            second_session.stop_live_display()

    def test_setup_pipelines_defaults_live_loudness_channel_to_first_selected_channel(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(selected_channels=[2, 0])
        config["channels"] = [
            {
                **config["channels"][0],
                "channel_index": 2,
            },
            config["channels"][0],
        ]
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )

        session._setup_pipelines()

        assert session._live_loudness_channel_index == 2

    def test_setup_pipelines_submits_primary_acoustic_live_loudness_channel(
        self,
        monkeypatch,
    ):
        created_dispatchers = []

        class FakeLiveDisplayDispatcher(_CapturingLiveDisplayDispatcher):
            def __init__(self, *, pipelines, bus, config_id):
                super().__init__()
                self.pipelines = pipelines
                self.bus = bus
                self.config_id = config_id
                created_dispatchers.append(self)

        monkeypatch.setattr(
            "app.service.analysis.test_session.LiveDisplayDispatcher",
            FakeLiveDisplayDispatcher,
        )
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(
            selected_channels=[1, 0],
            vibration_view_channel_index=1,
        )
        config["channels"] = [
            {**config["channels"][0], "channel_index": 1},
            config["channels"][0],
        ]
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )

        session._setup_pipelines()
        token, retained_start = _bind_resolved_capture(session)
        _emit_captured_data(
            session,
            token,
            retained_start,
            np.ones((2048, 2), dtype=np.float32),
        )

        assert created_dispatchers[-1].calls[-1]["live_loudness_channel_index"] == 0

    def test_setup_pipelines_submits_no_live_loudness_channel_without_acoustic(
        self,
        monkeypatch,
    ):
        created_dispatchers = []

        class FakeLiveDisplayDispatcher(_CapturingLiveDisplayDispatcher):
            def __init__(self, *, pipelines, bus, config_id):
                super().__init__()
                self.pipelines = pipelines
                self.bus = bus
                self.config_id = config_id
                created_dispatchers.append(self)

        monkeypatch.setattr(
            "app.service.analysis.test_session.LiveDisplayDispatcher",
            FakeLiveDisplayDispatcher,
        )
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(
            selected_channels=[1],
            vibration_view_channel_index=1,
        )
        config["channels"] = [{**config["channels"][0], "channel_index": 1}]
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )

        session._setup_pipelines()
        token, retained_start = _bind_resolved_capture(session)
        _emit_captured_data(
            session,
            token,
            retained_start,
            np.ones((2048, 1), dtype=np.float32),
        )

        assert created_dispatchers[-1].calls[-1]["live_loudness_channel_index"] is None

    def test_on_chunk_submits_live_loudness_channel_to_display_dispatcher(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(selected_channels=[0, 1])
        config["channels"] = [
            config["channels"][0],
            {**config["channels"][0], "channel_index": 1},
        ]
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"factory_run_id": "factory-run-1"},
            audio_dir="/tmp/audio",
        )
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        session.set_live_loudness_channel_index(1)

        token, retained_start = _bind_resolved_capture(session)
        _emit_captured_data(
            session,
            token,
            retained_start,
            np.ones((2048, 2), dtype=np.float32),
        )

        assert dispatcher.calls[-1]["live_loudness_channel_index"] == 1

    def test_on_chunk_submits_live_display_work(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        chunk = np.random.randn(2048, 1).astype(np.float32)

        token, retained_start = _bind_resolved_capture(session)
        _emit_captured_data(session, token, retained_start, chunk)

        assert len(dispatcher.calls) == 1
        np.testing.assert_array_equal(dispatcher.calls[0]["chunk"], chunk)

    def test_on_chunk_submits_live_display_sample_span_metadata(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(input_device_samplerate=1000)
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        token, retained_start = _bind_resolved_capture(session, attach_sample=400)
        chunk = np.ones((100, 1), dtype=np.float32)

        _emit_captured_data(session, token, retained_start, chunk)
        _emit_captured_data(session, token, retained_start + 100, chunk)

        assert dispatcher.calls[0]["sample_start"] == 0
        assert dispatcher.calls[0]["sample_end"] == 100
        assert dispatcher.calls[1]["sample_start"] == 100
        assert dispatcher.calls[1]["sample_end"] == 200

    def test_on_chunk_submits_display_work_with_factory_run_id(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"factory_run_id": "factory-run-1"},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher

        chunk = np.random.randn(2048, 1).astype(np.float32)

        token, retained_start = _bind_resolved_capture(session)
        _emit_captured_data(session, token, retained_start, chunk)

        assert dispatcher.calls[0]["frame_run_id"] == "factory-run-1"

    def test_on_chunk_does_not_call_display_dsp_backends_synchronously(self, monkeypatch):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(
            record_duration=2.0,
            input_device_samplerate=1000,
        )
        config["channels"] = [{
            **config["channels"][0],
            "stft_nfft": 8,
            "fft_nfft": 8,
        }]
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        main_thread_id = threading.get_ident()

        def _forbid_sync(name, value):
            def _impl(*args, **kwargs):
                if threading.get_ident() == main_thread_id:
                    raise AssertionError(f"{name} ran on recorder chunk path")
                return value
            return _impl

        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_spl",
            _forbid_sync("compute_spl", 60.0),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_stft",
            _forbid_sync(
                "compute_stft",
                (
                    np.array([0.0], dtype=np.float64),
                    np.array([100.0], dtype=np.float64),
                    np.array([[1.0]], dtype=np.float64),
                ),
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_fft_spectrum",
            _forbid_sync(
                "compute_fft_spectrum",
                (
                    np.array([0.0, 100.0], dtype=np.float64),
                    np.array([0.0, -10.0], dtype=np.float64),
                ),
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_octave_bands",
            _forbid_sync(
                "compute_octave_bands",
                (
                    np.array([125.0], dtype=np.float64),
                    np.array([55.0], dtype=np.float64),
                ),
            ),
        )
        monkeypatch.setattr(
            "app.service.analysis.pipeline.dsp.compute_loudness_trace",
            _forbid_sync(
                "compute_loudness_trace",
                ([0.0], [1.5], 1.5),
            ),
        )

        try:
            token, retained_start = _bind_resolved_capture(session)
            _emit_captured_data(
                session,
                token,
                retained_start,
                np.ones((512, 1), dtype=np.float32),
            )
        finally:
            stop_live_display = getattr(session, "stop_live_display", None)
            if callable(stop_live_display):
                stop_live_display()

        bus.recording_progress.emit.assert_called_once()

    def test_on_chunk_display_submission_failure_does_not_block_progress_or_completion(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config(record_duration=0.05)
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        session._live_display_dispatcher = _CapturingLiveDisplayDispatcher(
            fail=RuntimeError("display busy")
        )
        session._on_session_done_cb = MagicMock()
        chunk = np.ones((int(session._sample_rate * 0.05), 1), dtype=np.float32)

        token, retained_start = _bind_resolved_capture(session)
        _emit_captured_data(session, token, retained_start, chunk)

        bus.recording_progress.emit.assert_called_once()
        session._on_session_done_cb.assert_called_once_with(1)
        assert any(
            "display busy" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_on_chunk_notifies_when_session_duration_expires(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["record_duration"] = 0.1
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        session._on_session_done_cb = MagicMock()

        chunk = np.random.randn(int(session._sample_rate * 0.05), 1).astype(np.float32)
        token, retained_start = _bind_resolved_capture(session)

        _emit_captured_data(session, token, retained_start, chunk)
        _emit_captured_data(
            session,
            token,
            retained_start + chunk.shape[0],
            chunk,
        )

        session._on_session_done_cb.assert_called_once_with(1)

    def test_on_chunk_ignores_pre_attach_chunks_and_uses_attach_boundary_elapsed_time(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["record_duration"] = 0.1
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={},
            audio_dir="/tmp/audio",
        )
        session._setup_pipelines()
        session._on_session_done_cb = MagicMock()
        token, retained_start = _bind_resolved_capture(session, attach_sample=400)

        chunk = np.random.randn(int(session._sample_rate * 0.05), 1).astype(np.float32)

        _emit_captured_data(session, object(), retained_start, chunk)
        bus.recording_progress.emit.assert_not_called()
        _emit_captured_data(session, token, retained_start, chunk)
        _emit_captured_data(
            session,
            token,
            retained_start + chunk.shape[0],
            chunk,
        )
        bus.recording_progress.emit.assert_any_call(1, 0.05)
        bus.recording_progress.emit.assert_any_call(1, 0.1)
        session._on_session_done_cb.assert_called_once_with(1)

    def test_on_chunk_uses_sample_derived_elapsed_for_display_submission_and_progress_at_non_round_sample_rate(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["input_device_samplerate"] = 22050
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        chunk = np.ones((int(22050 * 0.05), 1), dtype=np.float32)
        expected_elapsed = chunk.shape[0] / 22050.0
        token, retained_start = _bind_resolved_capture(session)

        _emit_captured_data(session, token, retained_start, chunk)

        assert dispatcher.calls[0]["elapsed_time"] == pytest.approx(expected_elapsed)
        assert bus.recording_progress.emit.call_args.args == (1, pytest.approx(expected_elapsed))

    def test_on_chunk_completion_clamps_elapsed_to_session_target_after_attach_at_non_round_sample_rate(self):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["input_device_samplerate"] = 22050
        config["record_duration"] = 0.1
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={},
            audio_dir="/tmp/audio",
        )
        dispatcher = _CapturingLiveDisplayDispatcher()
        session._live_display_dispatcher = dispatcher
        session._on_session_done_cb = MagicMock()
        token, retained_start = _bind_resolved_capture(session, attach_sample=400)
        chunk_window_samples = int(22050 * 0.05)
        total_target_samples = frames_for_duration(22050, config["record_duration"])
        chunk = np.ones((chunk_window_samples, 1), dtype=np.float32)
        expected_elapsed_values = [
            samples / 22050.0
            for samples in (chunk_window_samples, chunk_window_samples * 2, total_target_samples)
        ]

        _emit_captured_data(session, token, retained_start, chunk)
        session._on_session_done_cb.assert_not_called()
        _emit_captured_data(
            session,
            token,
            retained_start + chunk_window_samples,
            chunk,
        )
        session._on_session_done_cb.assert_not_called()
        _emit_captured_data(
            session,
            token,
            retained_start + 2 * chunk_window_samples,
            chunk,
        )

        observed_elapsed_values = [
            call["elapsed_time"]
            for call in dispatcher.calls
        ]
        assert observed_elapsed_values == pytest.approx(expected_elapsed_values)
        progress_elapsed_values = [
            call.args[1]
            for call in bus.recording_progress.emit.call_args_list
        ]
        assert progress_elapsed_values == pytest.approx(expected_elapsed_values)
        assert observed_elapsed_values[-1] == pytest.approx(total_target_samples / 22050.0)
        session._on_session_done_cb.assert_called_once_with(1)


class TestTestSessionJudgment:
    @staticmethod
    def _make_persistence_result(overall: str) -> JudgmentResult:
        return JudgmentResult(
            overall=overall,
            limit_pass=(overall == "合格"),
            ai_pass=None,
            ai_score=None,
        )

    def test_run_judgment_produces_result(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._setup_pipelines()
        sr = 44100.0
        duration = 2.0
        t = np.arange(int(sr * duration)) / sr
        amplitude = 20e-6 * 10 ** (60 / 20)
        sig = amplitude * np.sin(2 * np.pi * 1000 * t)
        session._buffer = sig.reshape(-1, 1)
        session._sample_rate = sr
        result = session._run_judgment()
        assert isinstance(result, JudgmentResult)
        assert result.overall in ("合格", "不合格")

    def test_run_judgment_stores_loudness_trace_cache(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        created_caches = []

        def fake_judge(*args, loudness_trace_cache=None, **kwargs):
            assert loudness_trace_cache == {}
            created_caches.append(loudness_trace_cache)
            loudness_trace_cache[0] = {
                "times_sec": [0.25],
                "values_sone": [4.0],
                "max_sone": 4.0,
                "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
                "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
            }
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 1.0),
            )

        session._judgment = MagicMock()
        session._judgment.judge.side_effect = fake_judge

        result = session._run_judgment()

        assert result is session.last_result
        assert session._last_loudness_trace_cache is created_caches[0]
        assert session._last_loudness_trace_cache[0]["values_sone"] == [4.0]
        assert (
            session._last_loudness_trace_cache[0]["algorithm"]
            == dsp.LOUDNESS_TRACE_ALGORITHM
        )

    def test_run_judgment_required_window_failure_preserves_no_result_session_state(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.side_effect = ValueError("analysis window not fully captured")

        with pytest.raises(ValueError, match="analysis window not fully captured"):
            session._run_judgment()

        assert session.buffer is not None
        assert session._audio_file_path is not None
        assert os.path.isfile(session._audio_file_path)
        assert session._audio_data_id is not None
        assert session.last_result is None

    def test_run_judgment_missing_buffer_returns_none_for_manager_no_result_path(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )

        result = session._run_judgment()

        assert result is None
        assert session.last_result is None
        assert session.last_ai_review is None
        assert session._audio_file_path is None
        assert session._audio_data_id is None

    def test_run_judgment_default_window_offset_ms_uses_config_analysis_window_offset(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["analysis_window_offset_ms"] = 1500
        ai.resolve_binding.return_value = {
            "enabled": True,
            "analysis_window_offset_ms": 250,
        }
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.91,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M", "rotation_speed": 1450},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.5, 2.5),
        )

        result = session._run_judgment()

        ai.run_ai_analysis.assert_called_once()
        kwargs = ai.run_ai_analysis.call_args.kwargs
        assert kwargs["config_id"] == session.config_id
        assert re.fullmatch(r"run_1_\d+", kwargs["test_run_id"])
        assert session._workorder["rotation_speed"] == 1450
        assert type(session._workorder["rotation_speed"]) is int
        assert kwargs["context"] == {
            "config_id": session.config_id,
            "workorder": {"sn": "test", "model": "M"},
        }
        assert kwargs["context"]["workorder"] is not session._workorder
        assert os.path.normpath(kwargs["ai_runs_dir"]).endswith(os.path.normpath("ai_runs"))
        assert len(kwargs["segments"]) == 1
        segment = kwargs["segments"][0]
        assert segment["segment_id"] == session._audio_data_id
        assert segment["role"] == "device_1"
        assert segment["audio_path"] == session._audio_file_path
        assert segment["channels"] == [0]
        assert segment["duration_ms"] == 1000
        assert segment["sample_rate"] == 44100
        assert kwargs["window_info"] == {
            "window_overridden": False,
            "default_window_offset_ms": 1500,
            "default_window_duration_ms": 1000,
            "final_window_offset_ms": 1500,
            "final_window_duration_ms": 1000,
        }
        assert result.ai_pass is True
        assert result.ai_score == 0.91
        assert result.overall == "合格"

    def test_run_judgment_ai_segment_uses_projected_single_channel_metadata(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.91,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        config = _make_config(
            selected_channels=[3],
            channels=[{"channel_index": 3}],
        )
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        hardware = np.column_stack(
            [np.full(20, channel, dtype=np.float32) for channel in range(6)]
        )
        session.capture_recorder_buffer(hardware, 100.0)
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.0, 0.2),
        )

        session._run_judgment()

        segment = ai.run_ai_analysis.call_args.kwargs["segments"][0]
        assert segment["segment_id"] == session._audio_data_id
        assert segment["role"] == "device_1"
        assert segment["audio_path"] == session._audio_file_path
        assert segment["channels"] == [0]
        assert segment["source_channels"] == [3]
        assert segment["channel_map"] == [
            {"logical_channel": 0, "source_channel": 3},
        ]
        assert segment["duration_ms"] == 200
        assert segment["sample_rate"] == 100
        assert segment["metadata"] == {}

    def test_run_judgment_ai_segment_preserves_multi_channel_selected_order(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.91,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        config = _make_config(
            selected_channels=[1, 4],
            channels=[{"channel_index": 1}, {"channel_index": 4}],
        )
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        hardware = np.column_stack(
            [np.full(20, channel, dtype=np.float32) for channel in range(6)]
        )
        session.capture_recorder_buffer(hardware, 100.0)
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.0, 0.2),
        )

        session._run_judgment()

        segment = ai.run_ai_analysis.call_args.kwargs["segments"][0]
        assert segment["audio_path"] == session._audio_file_path
        assert segment["channels"] == [0, 1]
        assert segment["source_channels"] == [1, 4]
        assert segment["channel_map"] == [
            {"logical_channel": 0, "source_channel": 1},
            {"logical_channel": 1, "source_channel": 4},
        ]

    def test_run_judgment_marks_result_failed_when_ai_label_is_fail(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "FAIL",
            "score": 0.12,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(config_name="Config A"), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.ai_pass is False
        assert result.ai_score == 0.12
        assert result.overall == "不合格"
        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert rec.labels == "NG"
        assert rec.label_source == "auto"
        assert rec.analyse_config == "Config A"
        snapshot = json.loads(rec.config_content)
        assert snapshot["config_name"] == "Config A"
        assert snapshot["selected_channels"] == [0]

    def test_run_judgment_updates_saved_audio_row_to_ok(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._judgment = MagicMock()
        session._judgment.judge.return_value = self._make_persistence_result("合格")
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0

        session._run_judgment()

        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert rec.labels == "OK"
        assert rec.label_source == "auto"
        assert rec.analyse_config == "Config A"

    def test_run_judgment_updates_saved_audio_row_to_ng(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._judgment = MagicMock()
        session._judgment.judge.return_value = self._make_persistence_result("不合格")
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0

        session._run_judgment()

        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert rec.labels == "NG"
        assert rec.label_source == "auto"
        assert rec.analyse_config == "Config A"

    def test_run_judgment_keeps_flow_when_audio_row_update_affects_zero_rows(
        self, tmp_path, monkeypatch
    ):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._judgment = MagicMock()
        session._judgment.judge.return_value = self._make_persistence_result("合格")
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0

        class _ZeroRowQuery:
            def where(self, *_args, **_kwargs):
                return self

            def execute(self):
                return 0

        monkeypatch.setattr(AudioDataTable, "update", lambda **_kwargs: _ZeroRowQuery())

        result = session._run_judgment()

        assert result.overall == "合格"
        assert any(
            "audio metadata update affected 0 rows" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_persist_audio_result_metadata_warns_and_continues_for_missing_audio_data_id(self, tmp_path):
        bus = _make_mock_bus()
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._audio_data_id = None

        session._persist_audio_result_metadata(self._make_persistence_result("合格"))

        assert any(
            "audio metadata update skipped: missing audio_data_id" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_run_judgment_keeps_flow_when_audio_row_update_raises(self, tmp_path, monkeypatch):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._judgment = MagicMock()
        session._judgment.judge.return_value = self._make_persistence_result("合格")
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0

        def _raise_update(**_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(AudioDataTable, "update", _raise_update)

        result = session._run_judgment()

        assert result.overall == "合格"
        assert any(
            "failed to persist audio result metadata" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_run_judgment_leaves_label_empty_for_unexpected_overall(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._judgment = MagicMock()
        session._judgment.judge.return_value = self._make_persistence_result("未知")
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0

        session._run_judgment()

        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert rec.labels is None
        assert rec.label_source is None
        assert any(
            "unexpected judgment overall" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_apply_manual_label_overwrites_saved_audio_row_to_manual_ok(self, tmp_path):
        bus = _make_mock_bus()
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN-1", "model": "M"},
            audio_dir=str(tmp_path),
        )
        path = session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)
        AudioDataTable.update(labels="NG", label_source="auto").where(
            AudioDataTable.audio_data_id == session._audio_data_id
        ).execute()

        result = session.apply_manual_label("OK")

        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert result.success is True
        assert result.message == ""
        assert result.audio_data_id == session._audio_data_id
        assert rec.labels == "OK"
        assert rec.label_source == "manual"
        assert rec.file_path == path

    def test_apply_manual_label_overwrites_saved_audio_row_to_manual_ng(self, tmp_path):
        bus = _make_mock_bus()
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN-1", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)
        AudioDataTable.update(labels="OK", label_source="auto").where(
            AudioDataTable.audio_data_id == session._audio_data_id
        ).execute()

        result = session.apply_manual_label("NG")

        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert result.success is True
        assert result.message == ""
        assert result.audio_data_id == session._audio_data_id
        assert rec.labels == "NG"
        assert rec.label_source == "manual"

    def test_apply_manual_label_rejects_missing_audio_data_id(self, tmp_path):
        session = TestSession(
            config=_make_config(),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir=str(tmp_path),
        )

        result = session.apply_manual_label("OK")

        assert result.success is False
        assert "没有已保存的音频记录" in result.message
        assert result.audio_data_id is None

    def test_apply_manual_label_rejects_missing_database_row(self, tmp_path):
        session = TestSession(
            config=_make_config(),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir=str(tmp_path),
        )
        session._audio_data_id = "missing-audio-id"

        result = session.apply_manual_label("NG")

        assert result.success is False
        assert "音频记录不存在" in result.message
        assert result.audio_data_id == "missing-audio-id"

    def test_apply_manual_label_rejects_invalid_label(self, tmp_path):
        session = TestSession(
            config=_make_config(),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir=str(tmp_path),
        )
        session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)

        result = session.apply_manual_label("BAD")

        rec = AudioDataTable.get_by_id(session._audio_data_id)
        assert result.success is False
        assert "无效判定" in result.message
        assert result.audio_data_id is None
        assert rec.labels is None
        assert rec.label_source is None

    def test_apply_manual_label_reports_update_exception(self, tmp_path, monkeypatch):
        bus = _make_mock_bus()
        session = TestSession(
            config=_make_config(),
            bus=bus,
            ai_service=MagicMock(),
            mic_cal={},
            workorder={},
            audio_dir=str(tmp_path),
        )
        session._save_wav(np.zeros((100, 1), dtype=np.float32), 44100)

        def _raise_update(**_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(AudioDataTable, "update", _raise_update)

        result = session.apply_manual_label("OK")

        assert result.success is False
        assert "保存人工判定失败" in result.message
        assert "boom" in result.message
        assert result.audio_data_id == session._audio_data_id
        assert any(
            "failed to persist manual judgment" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_run_judgment_logs_warning_and_preserves_limit_result_when_ai_envelope_not_success(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "timeout",
            "ai_label": None,
            "score": None,
            "error_summary": "Process timed out",
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        bus.log.emit.assert_called_with(
            "[TestSession] WARNING: AI analysis did not succeed for config=1: "
            "module_status=timeout ai_label=None error=Process timed out"
        )

    def test_run_judgment_preserves_ai_review_details_separately_from_judgment_result_on_timeout(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {
            "enabled": True,
        }
        ai.run_ai_analysis.return_value = {
            "module_status": "timeout",
            "ai_label": None,
            "score": None,
            "error_summary": "Process timed out",
            "channel_index": 0,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "timeout"
        assert session.last_ai_review["threshold"] is None
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["channel_index"] == 0
        assert session.last_ai_review["error_message"] == "Process timed out"

    def test_run_judgment_keeps_limit_result_when_ai_analysis_fails(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.side_effect = RuntimeError("AI unavailable")
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        ai.run_ai_analysis.assert_called_once()
        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        bus.log.emit.assert_called_with("[TestSession] WARNING: AI analysis failed for config=1: AI unavailable")

    def test_run_judgment_logs_warning_when_ai_binding_resolution_fails(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.side_effect = RuntimeError("binding lookup failed")
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session._run_judgment()

        assert result.limit_pass is True
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        bus.log.emit.assert_called_with(
            "[TestSession] WARNING: failed to resolve AI binding for config=1: binding lookup failed"
        )

    def test_rejudge_reruns_ai_and_patches_result(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "FAIL",
            "score": 0.12,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session.rejudge(0.1, 0.5)

        session._judgment.rejudge.assert_called_once_with(
            session._config,
            session._buffer,
            session._sample_rate,
            session._mic_cal,
            session._audio_file_path,
            0.1,
            0.5,
            loudness_trace_cache=ANY,
        )
        ai.run_ai_analysis.assert_called_once()
        assert result is not None
        assert result is session.last_result
        assert result.ai_pass is False
        assert result.ai_score == 0.12
        assert result.overall == "不合格"

    def test_rejudge_replaces_loudness_trace_cache(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        old_cache = {
            0: {
                "times_sec": [9.0],
                "values_sone": [9.0],
                "max_sone": 9.0,
            }
        }
        session._last_loudness_trace_cache = old_cache
        created_caches = []

        def fake_rejudge(*args, loudness_trace_cache=None, **kwargs):
            assert loudness_trace_cache == {}
            created_caches.append(loudness_trace_cache)
            loudness_trace_cache[0] = {
                "times_sec": [0.25],
                "values_sone": [2.0],
                "max_sone": 2.0,
                "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
                "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
            }
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.1, 0.5),
            )

        session._judgment = MagicMock()
        session._judgment.rejudge.side_effect = fake_rejudge

        result = session.rejudge(0.1, 0.5)

        assert result is session.last_result
        assert session._last_loudness_trace_cache is created_caches[0]
        assert session._last_loudness_trace_cache is not old_cache
        assert session._last_loudness_trace_cache[0]["values_sone"] == [2.0]
        assert (
            session._last_loudness_trace_cache[0]["internal_rate_hz"]
            == dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ
        )

    def test_rejudge_passes_selected_channel_mapping_to_judgment(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        config = _make_config(
            selected_channels=[3],
            channels=[{"channel_index": 3}],
        )
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        hardware = np.column_stack(
            [np.full(8, channel, dtype=np.float32) for channel in range(6)]
        )
        session.capture_recorder_buffer(hardware, 100.0)
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        session.rejudge(0.1, 0.5)

        session._judgment.rejudge.assert_called_once_with(
            session._config,
            session._buffer,
            session._sample_rate,
            session._mic_cal,
            session._audio_file_path,
            0.1,
            0.5,
            channel_column_map={3: 0},
            loudness_trace_cache=ANY,
        )

    def test_rejudge_uses_same_ai_envelope_normalization_as_run_judgment(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.88,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )

        result = session.rejudge(0.1, 0.5)

        assert result is not None
        assert result.ai_pass is True
        assert result.ai_score == 0.88
        assert result.overall == "合格"

    def test_rejudge_required_window_failure_preserves_prior_no_result_trace(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = None
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        prior_result = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
        )
        session._last_result = prior_result
        session._judgment = MagicMock()
        session._judgment.rejudge.side_effect = ValueError("analysis window not fully captured")

        with pytest.raises(ValueError, match="analysis window not fully captured"):
            session.rejudge(0.1, 0.5)

        assert session.last_result is prior_result
        assert session._audio_file_path == str(tmp_path / "existing.wav")
        assert session._audio_data_id == "audio-123"

    def test_rejudge_passes_selected_window_to_ai_analysis_with_config_default_window_offset(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        config = _make_config()
        config["analysis_window_offset_ms"] = 1500
        ai.resolve_binding.return_value = {
            "enabled": True,
            "analysis_window_offset_ms": 250,
        }
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.88,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=config, bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "existing.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.1, 0.5),
        )

        session.rejudge(0.1, 0.5)

        kwargs = ai.run_ai_analysis.call_args.kwargs
        assert kwargs["segments"][0]["duration_ms"] == 400
        assert kwargs["window_info"] == {
            "window_overridden": True,
            "default_window_offset_ms": 1500,
            "default_window_duration_ms": 1000,
            "final_window_offset_ms": 100,
            "final_window_duration_ms": 400,
        }

    def test_rejudge_ai_segment_falls_back_to_selected_buffer_width_without_projection_metadata(
        self, tmp_path
    ):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.88,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(
                selected_channels=[3],
                channels=[{"channel_index": 3}],
            ),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(100, 1).astype(np.float32)
        session._sample_rate = 100.0
        session._audio_file_path = str(tmp_path / "selected.wav")
        session._audio_data_id = "audio-123"
        session._judgment = MagicMock()
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.1, 0.5),
        )

        session.rejudge(0.1, 0.5)

        segment = ai.run_ai_analysis.call_args.kwargs["segments"][0]
        assert segment["audio_path"] == session._audio_file_path
        assert segment["channels"] == [0]
        assert segment["source_channels"] == [3]
        assert segment["channel_map"] == [
            {"logical_channel": 0, "source_channel": 3},
        ]

    def test_repeated_ai_attempts_in_same_session_reuse_test_run_id(self, tmp_path, monkeypatch):
        bus = _make_mock_bus()
        ai = MagicMock()
        ai.resolve_binding.return_value = {"enabled": True}
        ai.run_ai_analysis.return_value = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.91,
            "decision_threshold": 0.5,
            "error_summary": None,
        }
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._judgment = MagicMock()
        session._judgment.judge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 2.0),
        )
        session._judgment.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.1, 0.5),
        )

        base = datetime(2026, 3, 18, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        timestamps = iter([
            base,
            base + timedelta(milliseconds=1),
            base + timedelta(milliseconds=2),
            base + timedelta(milliseconds=25),
        ])

        class _FixedDateTime:
            @staticmethod
            def now(tz=None):
                current = next(timestamps)
                if tz is None:
                    return current.replace(tzinfo=None)
                return current.astimezone(tz)

        monkeypatch.setattr("app.service.analysis.test_session.datetime", _FixedDateTime)

        session._run_judgment()
        first_run_id = ai.run_ai_analysis.call_args.kwargs["test_run_id"]
        session.rejudge(0.1, 0.5)
        second_run_id = ai.run_ai_analysis.call_args.kwargs["test_run_id"]

        assert re.fullmatch(r"run_1_\d+", first_run_id)
        assert second_run_id == first_run_id


class TestTestSessionWavSave:
    def test_save_wav(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        sr = 44100.0
        buf = np.random.randn(int(sr * 1), 1).astype(np.float32)
        path = session._save_wav(buf, sr)
        assert os.path.isfile(path)
        assert path.endswith(".wav")
        records = list(AudioDataTable.select())
        assert len(records) == 1
        rec = records[0]
        assert rec.file_path == path
        assert rec.barcode == "SN001"
        assert rec.product_model == "M1"
        assert rec.sample_rate == 44100
        assert rec.audio_data_id
        assert rec.record_date
        assert rec.labels is None

    def test_save_wav_persists_config_name_and_snapshot(self, tmp_path):
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(config_name="Config A"),
            bus=bus,
            ai_service=ai,
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        sr = 44100.0
        buf = np.random.randn(int(sr * 1), 1).astype(np.float32)

        path = session._save_wav(buf, sr)

        rec = AudioDataTable.get(AudioDataTable.file_path == path)
        assert rec.analyse_config == "Config A"
        snapshot = json.loads(rec.config_content)
        assert snapshot["config_name"] == "Config A"
        assert snapshot["selected_channels"] == [0]

    def test_audio_config_metadata_falls_back_to_config_id_when_name_blank(self, tmp_path):
        session = TestSession(
            config=_make_config(config_id=42, config_name="   "),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )

        config_name, _snapshot = session._audio_config_metadata()

        assert config_name == "42"

    def test_audio_config_metadata_final_fallback_is_empty_string(self, tmp_path):
        session = TestSession(
            config=_make_config(config_id=None, config_name=""),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )

        config_name, _snapshot = session._audio_config_metadata()

        assert config_name == ""

    def test_audio_config_metadata_returns_none_snapshot_and_warns_when_json_fails(self, tmp_path):
        config = _make_config(config_name="Config A")
        config["self"] = config
        bus = _make_mock_bus()
        session = TestSession(
            config=config,
            bus=bus,
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )

        config_name, snapshot = session._audio_config_metadata()

        assert config_name == "Config A"
        assert snapshot is None
        assert any(
            "failed to serialize config snapshot" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_save_wav_returns_absolute_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bus = _make_mock_bus()
        ai = MagicMock()
        session = TestSession(
            config=_make_config(), bus=bus, ai_service=ai,
            mic_cal={}, workorder={"sn": "SN001", "model": "M1"},
            audio_dir="audio",
        )
        sr = 44100.0
        buf = np.random.randn(int(sr * 1), 1).astype(np.float32)

        path = session._save_wav(buf, sr)

        assert os.path.isabs(path)
        assert os.path.isfile(path)

    def test_capture_recorder_buffer_saves_only_selected_real_channel(self, tmp_path):
        session = TestSession(
            config=_make_config(
                selected_channels=[3],
                channels=[{"channel_index": 3}],
            ),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        sr = 100.0
        hardware = np.column_stack(
            [np.full(12, channel, dtype=np.float32) for channel in range(6)]
        )

        captured = session.capture_recorder_buffer(hardware, sr)
        path = session._save_wav(session._buffer, session._sample_rate)

        assert captured.shape == (12, 1)
        np.testing.assert_array_equal(session._buffer[:, 0], hardware[:, 3])
        assert session._source_channels == (3,)
        assert session._real_to_logical_channel == {3: 0}
        saved_sr, saved_data = wavfile.read(path)
        assert saved_sr == 100
        assert saved_data.ndim == 1 or saved_data.shape[1] == 1
        np.testing.assert_allclose(saved_data.reshape(-1), hardware[:, 3])

    def test_capture_recorder_buffer_preserves_selected_channel_order(self, tmp_path):
        session = TestSession(
            config=_make_config(
                selected_channels=[1, 4],
                channels=[{"channel_index": 1}, {"channel_index": 4}],
            ),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        sr = 100.0
        hardware = np.column_stack(
            [np.full(12, channel, dtype=np.float32) for channel in range(6)]
        )

        captured = session.capture_recorder_buffer(hardware, sr)
        path = session._save_wav(session._buffer, session._sample_rate)

        assert captured.shape == (12, 2)
        np.testing.assert_array_equal(session._buffer, hardware[:, [1, 4]])
        assert session._source_channels == (1, 4)
        assert session._real_to_logical_channel == {1: 0, 4: 1}
        saved_sr, saved_data = wavfile.read(path)
        assert saved_sr == 100
        assert saved_data.shape == (12, 2)
        np.testing.assert_allclose(saved_data, hardware[:, [1, 4]])

    def test_capture_recorder_buffer_rejects_invalid_selected_channel_before_save_or_analysis(self, tmp_path):
        session = TestSession(
            config=_make_config(
                selected_channels=[3],
                channels=[{"channel_index": 3}],
            ),
            bus=_make_mock_bus(),
            ai_service=MagicMock(),
            mic_cal={},
            workorder={"sn": "SN001", "model": "M1"},
            audio_dir=str(tmp_path),
        )
        session._save_wav = MagicMock(side_effect=AssertionError("should not save"))
        session._judgment = MagicMock()
        hardware = np.zeros((12, 2), dtype=np.float32)

        with pytest.raises(ValueError, match="outside hardware channel range"):
            session.capture_recorder_buffer(hardware, 100.0)

        assert session._buffer is None
        assert session._sample_rate == 44100.0
        assert session._audio_file_path is None
        session._save_wav.assert_not_called()
        session._judgment.judge.assert_not_called()


class TestScoreThresholdDecision:
    """_run_ai() runtime threshold decision logic."""

    def _make_session(self, tmp_path, ai_mock, binding):
        ai_mock.resolve_binding.return_value = binding
        session = TestSession(
            config=_make_config(), bus=_make_mock_bus(), ai_service=ai_mock,
            mic_cal={}, workorder={"sn": "test", "model": "M"},
            audio_dir=str(tmp_path),
        )
        session._buffer = np.random.randn(44100, 1).astype(np.float32)
        session._sample_rate = 44100.0
        session._audio_file_path = str(tmp_path / "test.wav")
        session._audio_data_id = "audio-test"
        return session

    def _make_base_result(self):
        return JudgmentResult(
            overall="合格", limit_pass=True, ai_pass=None, ai_score=None,
        )

    def _make_success_envelope(self, **overrides):
        envelope = {
            "module_status": "success",
            "ai_label": "PASS",
            "score": 0.9,
            "decision_threshold": 0.8,
            "error_summary": None,
        }
        envelope.update(overrides)
        return envelope

    def test_score_above_envelope_threshold_overrides_fail_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="FAIL",
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score == 0.9
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "success"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["score"] == 0.9

    def test_chinese_defect_label_marks_ai_failed_and_preserves_label(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="嗡嗡音",
            score=0.91,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score == 0.91
        assert result.overall == "不合格"
        assert session.last_ai_review["status"] == "success"
        assert session.last_ai_review["score"] == 0.91
        assert session.last_ai_review["passed"] is False
        assert session.last_ai_review["ai_label"] == "嗡嗡音"

    def test_score_below_envelope_threshold_fails_even_if_ai_label_is_pass(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(score=0.3)
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score == 0.3
        assert result.overall == "不合格"

    def test_score_uses_resolved_request_threshold_from_envelope(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="FAIL",
            score=0.6,
            decision_threshold=0.6,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score == 0.6
        assert result.overall == "合格"
        assert session.last_ai_review["threshold"] == 0.6

    def test_score_uses_default_threshold_from_envelope_when_upstream_falls_back_to_point_five(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            score=0.4,
            decision_threshold=0.5,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score == 0.4
        assert result.overall == "不合格"
        assert session.last_ai_review["threshold"] == 0.5

    def test_missing_score_ai_label_fail_falls_back_to_false(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="FAIL",
            score=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is False
        assert result.ai_score is None
        assert result.overall == "不合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["error_message"] == "AI score unavailable"

    def test_missing_score_ai_label_pass_falls_back_to_true(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(score=None)
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is True
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["error_message"] == "AI score unavailable"

    def test_missing_score_unknown_ai_label_degrades_to_no_result(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="MAYBE",
            score=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())
        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["error_message"] == "AI label is not a recognized binary label"

    @pytest.mark.parametrize("ai_label", [None, 123])
    def test_missing_score_without_usable_ai_label_degrades_to_no_result_with_explicit_error(
        self, tmp_path, ai_label
    ):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label=ai_label,
            score=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})

        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["error_message"] == "AI score and usable AI label are unavailable"

    def test_run_ai_malformed_output_none_marks_invalid_output_and_preserves_limit_result(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = None
        session = self._make_session(tmp_path, ai, {"enabled": True})

        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is None
        assert result.ai_score is None
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "invalid_output"
        assert session.last_ai_review["threshold"] is None
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["channel_index"] is None
        assert session.last_ai_review["error_message"] == "AI analysis returned invalid output"

    @pytest.mark.parametrize(
        ("score", "ai_label", "expected_ai_pass"),
        [
            (float("nan"), "PASS", True),
            (float("inf"), "FAIL", False),
            ("0.7", "PASS", True),
        ],
    )
    def test_unusable_score_enters_ai_label_fallback_and_preserves_missing_score_review(
        self, tmp_path, score, ai_label, expected_ai_pass
    ):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label=ai_label,
            score=score,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})
        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is expected_ai_pass
        assert result.ai_score is None
        assert session.last_ai_review["status"] == "missing_score"
        assert session.last_ai_review["threshold"] == 0.8
        assert session.last_ai_review["score"] is None
        assert session.last_ai_review["error_message"] == "AI score unavailable"

    def test_score_without_envelope_threshold_degrades_to_no_result_instead_of_label_fallback(self, tmp_path):
        ai = MagicMock()
        ai.run_ai_analysis.return_value = self._make_success_envelope(
            ai_label="PASS",
            decision_threshold=None,
        )
        session = self._make_session(tmp_path, ai, {"enabled": True})

        result = session._run_ai(self._make_base_result())

        assert result.ai_pass is None
        assert result.ai_score == 0.9
        assert result.overall == "合格"
        assert session.last_ai_review["status"] == "success"
        assert session.last_ai_review["threshold"] is None
        assert session.last_ai_review["score"] == 0.9
        assert session.last_ai_review["error_message"] == "AI decision threshold unavailable"
