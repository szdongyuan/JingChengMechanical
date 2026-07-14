from __future__ import annotations

import time
from threading import Event, RLock, Thread
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication, QEventLoop

from app.service.analysis.session_capture import (
    CaptureNotAcceptedError,
    CapturedAudioBlock,
    RecorderAttachment,
    frames_for_duration,
)
from app.service.analysis.test_manager import TestManager

from ._support import (
    _CaptureRecorderDoubleMixin,
    _DummySignal,
    _FakeSession,
    _factory_runtime_config,
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


def _process_qt_events_until(predicate, *, timeout: float = 1.0):
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    assert predicate()


class TestSignalDisconnection:
    def test_rejected_shared_candidate_rotates_token_disconnects_and_retries_once(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        config = _factory_runtime_config()
        config["output_device_name"] = None
        services.config_service.load_config.return_value = config
        events = []

        class RejectingRecorder:
            def __init__(self):
                self._sample_rate = 44100.0
                self._n_channels = 2
                self.captured_block_ready = _DummySignal()
                self.error_occurred = _DummySignal()
                self.saved_callback = None
                self.saved_error_callback = None
                self.request_calls = 0

            def request_capture(self, *, record_duration):
                self.request_calls += 1
                events.append("rejected.request")
                self.saved_callback = self.captured_block_ready._callbacks[0]
                self.saved_error_callback = self.error_occurred._callbacks[0]
                self.captured_block_ready.emit(
                    CapturedAudioBlock(
                        np.ones((1, 2), dtype=np.float32),
                        0,
                        1,
                        None,
                        None,
                    )
                )
                raise CaptureNotAcceptedError("terminal race")

        class ReplacementRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = float(sample_rate)
                self._n_channels = int(n_channels)
                self._buffer = np.empty((0, self._n_channels), dtype=np.float32)
                self.captured_block_ready = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self.request_calls = 0
                self.release_calls = []
                ReplacementRecorder.instances.append(self)

            def request_capture(self, *, record_duration):
                self.request_calls += 1
                events.append("replacement.request")
                return RecorderAttachment(
                    request_monotonic=1.0,
                    warmup_samples=frames_for_duration(self._sample_rate, 0.05),
                    target_samples=frames_for_duration(self._sample_rate, record_duration),
                )

            def release_capture(self, owner):
                self.release_calls.append(owner)
                owner._mark_released()

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

        rejecting = RejectingRecorder()
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            ReplacementRecorder,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_session.TestSession._setup_pipelines",
            lambda self: None,
        )
        mgr = TestManager(bus, services)
        device_key = mgr._device_key(config)
        mgr._shared_recorders[device_key] = rejecting

        assert mgr.start({"config_id": 1, "sn": "SN1"}) is True

        replacement = ReplacementRecorder.instances[0]
        session = mgr._sessions[1]
        assert events == ["rejected.request", "replacement.request"]
        assert rejecting.request_calls == 1
        assert replacement.request_calls == 1
        assert rejecting.captured_block_ready._callbacks == []
        assert replacement.captured_block_ready._callbacks == [
            session._captured_block_conn
        ]
        assert mgr._shared_recorders[device_key] is replacement
        assert session._pending_capture_blocks == []

        rejecting.saved_callback(object())
        rejecting.saved_error_callback("stale rejected-recorder error")

        assert session._pending_capture_blocks == []
        assert mgr.is_running(config_id=1) is True
        bus.factory_runtime_aborted.emit.assert_not_called()
        mgr.stop(config_id=1, silent=True)

    @pytest.mark.parametrize("error_phase", ["request", "bind", "registration"])
    def test_startup_recorder_error_is_latched_and_releases_accepted_owner(
        self,
        monkeypatch,
        error_phase,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        config = _factory_runtime_config()
        config["output_device_name"] = None
        services.config_service.load_config.return_value = config
        error_message = f"device lost during {error_phase}"

        class StartupErrorRecorder:
            instances = []

            def __init__(
                self,
                *,
                device_name,
                hostapi,
                sample_rate,
                n_channels,
                record_duration,
            ):
                self._sample_rate = float(sample_rate)
                self._n_channels = int(n_channels)
                self.captured_block_ready = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self.capture_owners = []
                self.release_calls = []
                self.stop_calls = 0
                StartupErrorRecorder.instances.append(self)

            def request_capture(self, *, record_duration):
                owner = RecorderAttachment(
                    request_monotonic=1.0,
                    warmup_samples=frames_for_duration(self._sample_rate, 0.05),
                    target_samples=frames_for_duration(
                        self._sample_rate,
                        record_duration,
                    ),
                )
                self.capture_owners.append(owner)
                if error_phase == "request":
                    self.error_occurred.emit(error_message)
                return owner

            def release_capture(self, owner):
                self.release_calls.append(owner)
                if owner in self.capture_owners:
                    self.capture_owners.remove(owner)
                owner._mark_released()

            def has_capture_owners(self):
                return bool(self.capture_owners)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self.stop_calls += 1
                self._running = False

            def wait(self, timeout):
                return True

        class StartupErrorSession(_FakeSession):
            def attach_to_recorder(self, attachment, *, source_token):
                super().attach_to_recorder(
                    attachment,
                    source_token=source_token,
                )
                if error_phase == "bind":
                    StartupErrorRecorder.instances[-1].error_occurred.emit(
                        error_message
                    )

        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            StartupErrorRecorder,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.TestSession",
            StartupErrorSession,
        )

        mgr = TestManager(bus, services)

        if error_phase == "registration":
            class ErrorEmittingSessions(dict):
                emitted = False

                def __setitem__(self, key, value):
                    super().__setitem__(key, value)
                    if not self.emitted:
                        self.emitted = True
                        StartupErrorRecorder.instances[-1].error_occurred.emit(
                            error_message
                        )

            mgr._sessions = ErrorEmittingSessions()

        assert mgr.start({"config_id": 1, "sn": "SN1"}) is False

        recorder = StartupErrorRecorder.instances[0]
        assert len(recorder.release_calls) == 1
        assert recorder.release_calls[0].released is True
        assert recorder.capture_owners == []
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr.is_running(config_id=1) is False
        assert error_message in mgr.last_start_error()
        bus.test_started.emit.assert_not_called()

    def test_pending_owner_protects_shared_recorder_during_other_session_cleanup(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        config = _factory_runtime_config(config_id=2)
        config["output_device_name"] = None
        services.config_service.load_config.return_value = config
        attach_entered = Event()
        allow_attach = Event()

        class SharedRecorder:
            def __init__(self):
                self._sample_rate = 44100.0
                self._n_channels = 2
                self.captured_block_ready = _DummySignal()
                self.error_occurred = _DummySignal()
                self._owner_lock = RLock()
                self.capture_owners = []
                self._running = True
                self.stop_calls = 0
                self.stop_if_no_owner_calls = 0
                self.start_calls = 0
                self._accepting_captures = True

            def request_capture(self, *, record_duration):
                owner = object()
                with self._owner_lock:
                    if not self._accepting_captures:
                        raise CaptureNotAcceptedError("recorder stopped")
                    self.capture_owners.append(owner)
                return owner

            def release_capture(self, owner):
                with self._owner_lock:
                    if owner in self.capture_owners:
                        self.capture_owners.remove(owner)

            def has_capture_owners(self):
                with self._owner_lock:
                    return bool(self.capture_owners)

            def stop_if_no_capture_owners(self):
                with self._owner_lock:
                    self.stop_if_no_owner_calls += 1
                    if self.capture_owners:
                        return False
                    self._accepting_captures = False
                    self._running = False
                    self.stop_calls += 1
                    return True

            def get_capture_buffer(self, owner, *, retained_frames):
                return np.empty((0, self._n_channels), dtype=np.float32)

            def isRunning(self):
                return self._running

            def start(self):
                self.start_calls += 1
                self._running = True

            def stop(self):
                self.stop_calls += 1
                self._running = False

            def wait(self, timeout):
                return True

        recorder = SharedRecorder()
        first_owner = recorder.request_capture(record_duration=2.0)

        class BlockingAttachSession(_FakeSession):
            def attach_to_recorder(self, attachment, *, source_token):
                attach_entered.set()
                assert allow_attach.wait(timeout=2)
                super().attach_to_recorder(
                    attachment,
                    source_token=source_token,
                )

        monkeypatch.setattr(
            "app.service.analysis.test_manager.TestSession",
            BlockingAttachSession,
        )

        mgr = TestManager(bus, services)
        device_key = mgr._device_key(config)
        mgr._shared_recorders[device_key] = recorder
        first_session = MagicMock()
        first_session.capture_attachment = first_owner
        first_session.freeze_capture.return_value = 0
        mgr._sessions[1] = first_session
        mgr._recorders[1] = recorder

        start_results = []
        starter = Thread(
            target=lambda: start_results.append(
                mgr.start({"config_id": 2, "sn": "SN2"})
            )
        )
        starter.start()
        assert attach_entered.wait(timeout=2)

        mgr._stop_one(1, silent=True)

        assert recorder.stop_calls == 0
        assert recorder.stop_if_no_owner_calls == 1
        assert recorder.has_capture_owners() is True

        allow_attach.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert start_results == [True]
        assert mgr._recorders[2] is recorder

        mgr._stop_one(2, silent=True)

    @pytest.mark.parametrize(
        "message",
        [
            "Monitoring buffer overflow",
            "Monitoring output underrun",
            "Monitoring output status: output underflow",
        ],
    )
    def test_monitor_only_diagnostics_do_not_abort_running_session(
        self,
        monkeypatch,
        message,
    ):
        (
            bus,
            _services,
            mgr,
            session,
            recorder,
            monitor,
            _partial_buf,
        ) = _make_running_factory_manager(monkeypatch)
        bus.log.emit.reset_mock()
        bus.status.emit.reset_mock()
        bus.factory_runtime_aborted.emit.reset_mock()

        try:
            monitor.error_occurred.emit(message)
            _process_qt_events_until(lambda: bus.log.emit.called)

            assert mgr.is_running(config_id=1) is True
            assert mgr._sessions.get(1) is session
            assert mgr._recorders.get(1) is recorder
            assert mgr._monitors.get(1) is monitor
            assert recorder.stop_calls == 0
            assert monitor.stop_calls == 0
            bus.factory_runtime_aborted.emit.assert_not_called()
            assert not any(
                str(call.args[0]).startswith("Error:")
                for call in bus.status.emit.call_args_list
            )
            bus.log.emit.assert_any_call(
                f"[TestManager] monitor diagnostic config=1: {message}"
            )
        finally:
            mgr.stop(config_id=1, silent=True)

    @pytest.mark.parametrize(
        "message",
        [
            "Host API not found: TestAPI",
            "Audio device error: output failed",
            "Monitoring error: callback crashed",
        ],
    )
    def test_fatal_monitor_errors_still_abort_running_session(
        self,
        monkeypatch,
        message,
    ):
        (
            bus,
            _services,
            mgr,
            _session,
            recorder,
            monitor,
            _partial_buf,
        ) = _make_running_factory_manager(monkeypatch)
        bus.log.emit.reset_mock()
        bus.status.emit.reset_mock()
        bus.factory_runtime_aborted.emit.reset_mock()

        monitor.error_occurred.emit(message)
        _process_qt_events_until(lambda: not mgr.is_running(config_id=1))

        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1
        bus.factory_runtime_aborted.emit.assert_called_once()
        bus.log.emit.assert_any_call(f"[TestManager] ERROR config=1: {message}")
        bus.status.emit.assert_any_call(f"Error: {message}")

    def test_stop_disconnects_session_and_monitor_callbacks(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
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
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": "OutputSpeaker",
            "output_device_hostapi": "OutputAPI",
            "output_device_samplerate": 48000,
        }

        class FakeRecorder(_CaptureRecorderDoubleMixin):
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((int(sample_rate * record_duration), n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self._running = False

            def wait(self, timeout):
                return True

            def attach_session(self, *, record_duration):
                if record_duration > self._record_duration:
                    self._record_duration = record_duration
                    self._buffer = np.zeros((int(self._sample_rate * record_duration), self._n_channels), dtype=np.float32)
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.error_occurred = _DummySignal()
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                return None

            def start(self):
                return None

            def stop(self):
                return None

            def wait(self, timeout):
                return True

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", _FakeSession)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})

        recorder = FakeRecorder.instances[0]
        monitor = FakeMonitor.instances[0]
        session = mgr._sessions[1]
        captured_block_conn = getattr(session, "_captured_block_conn", None)
        error_lambda = getattr(session, "_error_lambda", None)
        monitor_error_lambda = getattr(session, "_monitor_error_lambda", None)

        assert recorder.captured_block_ready._callbacks == [captured_block_conn]
        assert recorder.chunk_ready._callbacks == []
        assert recorder.recording_done._callbacks == []
        assert error_lambda in recorder.error_occurred._callbacks
        assert session._monitor_feed == monitor.feed
        assert monitor_error_lambda in monitor.error_occurred._callbacks

        mgr.stop(config_id=1)

        assert captured_block_conn not in recorder.captured_block_ready._callbacks
        assert recorder.chunk_ready._callbacks == []
        assert recorder.recording_done._callbacks == []
        assert error_lambda not in recorder.error_occurred._callbacks
        assert monitor_error_lambda not in monitor.error_occurred._callbacks

    def test_monitor_feed_is_connected_before_live_display_chunk_handler(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value["input"]["channel_count"] = "4"
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Test",
            "record_duration": 2.0,
            "analysis_duration": 1.0,
            "selected_channels": [3],
            "channels": [{
                "channel_index": 3,
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
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": "OutputSpeaker",
            "output_device_hostapi": "OutputAPI",
            "output_device_samplerate": 48000,
        }
        events = []

        class FakeRecorder(_CaptureRecorderDoubleMixin):
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def attach_session(self, *, record_duration):
                return MagicMock(chunk_index=0, sample_index=0)

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.monitor_channel = monitor_channel
                self.error_occurred = _DummySignal()
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                events.append(("monitor", chunk[:, self.monitor_channel].copy()))
                return True

            def start(self):
                return None

            def stop(self):
                return None

            def wait(self, timeout):
                return True

        class FakeSession(_FakeSession):
            def _on_captured_block(self, block, *, source_token):
                if source_token is not self._source_token:
                    return
                if self._monitor_feed is not None:
                    self._monitor_feed(block.data)
                events.append(("display", block.sample_start))

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

        mgr = TestManager(bus, services)

        assert mgr.start({"config_id": 1, "sn": "SN1"}) is True
        chunk = np.column_stack(
            [np.full(4, channel, dtype=np.float32) for channel in range(4)]
        )
        FakeRecorder.instances[0].captured_block_ready.emit(
            CapturedAudioBlock(chunk, 0, 4, None, None)
        )

        assert events[0][0] == "monitor"
        np.testing.assert_array_equal(events[0][1], chunk[:, 3])
        assert events[1] == ("display", 0)

    def test_monitor_overflow_does_not_stop_reentrantly_from_chunk_callback(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.config_service.load_config.return_value = {
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
            "input_device_name": "InputMic",
            "input_device_hostapi": "InputAPI",
            "input_device_samplerate": 44100,
            "output_device_name": "OutputSpeaker",
            "output_device_hostapi": "OutputAPI",
            "output_device_samplerate": 48000,
        }
        events = []
        mgr = None

        class FakeRecorder(_CaptureRecorderDoubleMixin):
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
                self._n_channels = n_channels
                self._record_duration = record_duration
                self._buffer = np.zeros((4, n_channels), dtype=np.float32)
                self.chunk_ready = _DummySignal()
                self.recording_done = _DummySignal()
                self.error_occurred = _DummySignal()
                self._running = False
                self.stop_calls = 0
                self.wait_calls = []
                FakeRecorder.instances.append(self)

            def isRunning(self):
                return self._running

            def start(self):
                self._running = True

            def stop(self):
                self.stop_calls += 1
                self._running = False

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                return True

            def attach_session(self, *, record_duration):
                return MagicMock(chunk_index=0, sample_index=0)

            def get_buffer_from(self, snapshot):
                return self._buffer

        class FakeMonitor:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, monitor_channel):
                self.error_occurred = _DummySignal()
                self.stop_calls = 0
                self.wait_calls = []
                FakeMonitor.instances.append(self)

            def feed(self, chunk):
                self.error_occurred.emit("Monitoring buffer overflow")
                recorder = FakeRecorder.instances[0]
                events.append(
                    (
                        "feed_after_error",
                        mgr.is_running(config_id=1),
                        recorder.stop_calls,
                        list(recorder.wait_calls),
                    )
                )
                return False

            def start(self):
                return None

            def stop(self):
                self.stop_calls += 1

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                return True

        class FakeSession(_FakeSession):
            def _on_captured_block(self, block, *, source_token):
                if source_token is not self._source_token:
                    return
                if self._monitor_feed is not None:
                    self._monitor_feed(block.data)
                events.append(("session_chunk", mgr.is_running(config_id=1)))

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

        mgr = TestManager(bus, services)
        assert mgr.start({"config_id": 1, "sn": "SN1"}) is True

        try:
            FakeRecorder.instances[0].captured_block_ready.emit(
                CapturedAudioBlock(
                    np.ones((4, 1), dtype=np.float32),
                    0,
                    4,
                    None,
                    None,
                )
            )

            assert events[0] == ("feed_after_error", True, 0, [])
            assert events[1] == ("session_chunk", True)
            assert mgr.is_running(config_id=1) is True

            _process_qt_events_until(lambda: bus.log.emit.called)

            recorder = FakeRecorder.instances[0]
            monitor = FakeMonitor.instances[0]
            assert mgr.is_running(config_id=1) is True
            assert recorder.stop_calls == 0
            assert recorder.wait_calls == []
            assert monitor.stop_calls == 0
            assert monitor.wait_calls == []
            bus.factory_runtime_aborted.emit.assert_not_called()
            bus.log.emit.assert_any_call(
                "[TestManager] monitor diagnostic config=1: Monitoring buffer overflow"
            )
            assert not any(
                str(call.args[0]).startswith("Error:")
                for call in bus.status.emit.call_args_list
            )
        finally:
            mgr.stop(config_id=1, silent=True)

    def test_recorder_cleanup_skips_wait_when_called_from_recorder_thread(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)

        class CurrentThreadRecorder:
            def __init__(self):
                self.stop_if_no_owner_calls = 0
                self.wait_calls = []

            def stop_if_no_capture_owners(self):
                self.stop_if_no_owner_calls += 1
                return True

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                return True

            def isCurrentThread(self):
                return True

        recorder = CurrentThreadRecorder()

        mgr._cleanup_monitor_recorder_safely(
            1,
            rec=recorder,
            context="recorder error",
        )

        assert recorder.stop_if_no_owner_calls == 1
        assert recorder.wait_calls == []

    def test_recorder_cleanup_keeps_shared_mapping_when_atomic_shutdown_loses(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)

        class OwnedRecorder:
            def __init__(self):
                self.stop_if_no_owner_calls = 0
                self.wait_calls = []

            def stop_if_no_capture_owners(self):
                self.stop_if_no_owner_calls += 1
                return False

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                return True

        recorder = OwnedRecorder()
        mgr._shared_recorders["device"] = recorder

        errors = mgr._cleanup_monitor_recorder_safely(
            1,
            rec=recorder,
            context="pending owner",
        )

        assert errors == []
        assert recorder.stop_if_no_owner_calls == 1
        assert recorder.wait_calls == []
        assert mgr._shared_recorders == {"device": recorder}
