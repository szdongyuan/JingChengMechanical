from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication, QEventLoop

from app.service.analysis.test_manager import TestManager

from ._support import (
    _DummySignal,
    _FakeSession,
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

        class FakeRecorder:
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
        done_lambda = getattr(session, "_done_lambda", None)
        error_lambda = getattr(session, "_error_lambda", None)
        monitor_lambda = getattr(session, "_monitor_lambda", None)
        monitor_error_lambda = getattr(session, "_monitor_error_lambda", None)

        assert session._on_chunk in recorder.chunk_ready._callbacks
        assert done_lambda in recorder.recording_done._callbacks
        assert error_lambda in recorder.error_occurred._callbacks
        assert monitor_lambda in recorder.chunk_ready._callbacks
        assert monitor_error_lambda in monitor.error_occurred._callbacks

        mgr.stop(config_id=1)

        assert session._on_chunk not in recorder.chunk_ready._callbacks
        assert done_lambda not in recorder.recording_done._callbacks
        assert error_lambda not in recorder.error_occurred._callbacks
        assert monitor_lambda not in recorder.chunk_ready._callbacks
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

        class FakeRecorder:
            instances = []

            def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
                self._sample_rate = sample_rate
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
            def _on_chunk(self, chunk, chunk_index):
                events.append(("display", chunk_index))

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

        mgr = TestManager(bus, services)

        assert mgr.start({"config_id": 1, "sn": "SN1"}) is True
        chunk = np.column_stack(
            [np.full(4, channel, dtype=np.float32) for channel in range(4)]
        )
        FakeRecorder.instances[0].chunk_ready.emit(chunk, 0)

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

        class FakeRecorder:
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
            def _on_chunk(self, chunk, chunk_index):
                events.append(("session_chunk", mgr.is_running(config_id=1)))

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", FakeMonitor)
        monkeypatch.setattr("app.service.analysis.test_manager.TestSession", FakeSession)

        mgr = TestManager(bus, services)
        assert mgr.start({"config_id": 1, "sn": "SN1"}) is True

        try:
            FakeRecorder.instances[0].chunk_ready.emit(
                np.ones((4, 1), dtype=np.float32),
                0,
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
                self.stop_calls = 0
                self.wait_calls = []

            def stop(self):
                self.stop_calls += 1

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

        assert recorder.stop_calls == 1
        assert recorder.wait_calls == []
