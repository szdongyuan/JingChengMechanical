"""Tests for AudioMonitor."""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.service.analysis.audio_monitor import AudioMonitor
from app.service.analysis.monitor_playback import OneShotReplayPlayer


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestAudioMonitorInit:
    def test_init(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=0,
        )
        assert mon._monitor_channel == 0
        assert mon._sample_rate == 44100.0

    def test_has_error_occurred_signal(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=0,
        )
        assert hasattr(mon, "error_occurred")


class TestAudioMonitorFeed:
    def test_extract_channel(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=1,
        )
        chunk = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, np.array([[2.0], [4.0]]))

    def test_extract_single_channel(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=0,
        )
        chunk = np.array([[1.0], [3.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, chunk)

    def test_feed_rejects_out_of_range_monitor_channel_with_error(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=5,
        )
        errors = []
        mon.error_occurred.connect(errors.append)
        chunk = np.array([[1.0, 2.0]], dtype=np.float32)

        accepted = mon.feed(chunk)

        assert accepted is False
        assert errors
        assert "monitor channel" in errors[0].lower()
        assert mon._playback_buffer.buffered_frames() == 0

    def test_feed_applies_gain_and_clips_before_buffering(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=10.0,
            monitor_channel=1,
            gain_db=6.0,
            prebuffer_seconds=0.0,
            buffer_seconds=1.0,
        )
        assert mon.feed(np.array([[0.25, 0.75], [0.5, 0.9]], dtype=np.float32)) is True
        out = np.zeros((2, 1), dtype=np.float32)

        mon._output_callback(out, 2, None, None)

        np.testing.assert_allclose(
            out[:, 0],
            [min(0.75 * (10 ** (6 / 20)), 1.0), 1.0],
        )

    def test_callback_emits_silence_until_prebuffer_target_reached(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=10.0,
            monitor_channel=0,
            prebuffer_seconds=0.3,
            buffer_seconds=1.0,
        )
        assert mon.feed(np.array([[1.0], [2.0]], dtype=np.float32)) is True
        out = np.full((2, 1), 9.0, dtype=np.float32)

        mon._output_callback(out, 2, None, None)

        np.testing.assert_array_equal(out, np.zeros((2, 1), dtype=np.float32))
        assert mon._playback_buffer.buffered_frames() == 2

    def test_callback_drains_exactly_requested_frames_after_prebuffering_starts(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=10.0,
            monitor_channel=0,
            prebuffer_seconds=0.2,
            buffer_seconds=1.0,
        )
        assert mon.feed(np.array([[0.1], [0.2], [0.3]], dtype=np.float32)) is True
        out = np.zeros((2, 1), dtype=np.float32)

        mon._output_callback(out, 2, None, None)

        np.testing.assert_array_equal(out[:, 0], np.array([0.1, 0.2], dtype=np.float32))
        assert mon._playback_buffer.buffered_frames() == 1

    def test_underrun_fills_remaining_frames_and_emits_throttled_error(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=10.0,
            monitor_channel=0,
            prebuffer_seconds=0.0,
            buffer_seconds=1.0,
        )
        errors = []
        mon.error_occurred.connect(errors.append)
        assert mon.feed(np.array([[0.5]], dtype=np.float32)) is True
        out = np.full((3, 1), 9.0, dtype=np.float32)

        mon._output_callback(out, 3, None, None)
        mon._output_callback(np.zeros((3, 1), dtype=np.float32), 3, None, None)

        np.testing.assert_array_equal(out[:, 0], np.array([0.5, 0.0, 0.0], dtype=np.float32))
        assert len(errors) == 1
        assert "underrun" in errors[0].lower()

    def test_callback_status_flags_emit_throttled_monitor_error(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=10.0,
            monitor_channel=0,
            prebuffer_seconds=0.0,
            buffer_seconds=1.0,
        )
        errors = []
        mon.error_occurred.connect(errors.append)

        mon._output_callback(np.zeros((1, 1), dtype=np.float32), 1, None, "underflow")
        mon._output_callback(np.zeros((1, 1), dtype=np.float32), 1, None, "underflow")

        assert len(errors) == 1
        assert "status" in errors[0].lower()
        assert "underflow" in errors[0].lower()

    def test_feed_overflow_drops_oldest_returns_false_and_does_not_block(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=10.0,
            monitor_channel=0,
            prebuffer_seconds=0.0,
            buffer_seconds=0.3,
        )
        errors = []
        mon.error_occurred.connect(errors.append)
        assert mon.feed(np.array([[0.1], [0.2], [0.3]], dtype=np.float32)) is True

        accepted = mon.feed(np.array([[0.4], [0.5]], dtype=np.float32))

        assert accepted is False
        assert errors
        assert "overflow" in errors[0].lower()
        out = np.zeros((3, 1), dtype=np.float32)
        mon._output_callback(out, 3, None, None)
        np.testing.assert_array_equal(out[:, 0], np.array([0.3, 0.4, 0.5], dtype=np.float32))


class TestOneShotReplayPlayer:
    def test_replay_player_drains_finite_segment_and_stops(self, monkeypatch):
        captured = {}
        entered = threading.Event()
        exited = threading.Event()

        class FakeOutputStream:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                entered.set()
                return self

            def __exit__(self, exc_type, exc, tb):
                exited.set()
                return False

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        player = OneShotReplayPlayer()
        assert player.play(
            segment=np.array([0.1, 0.2], dtype=np.float32),
            sample_rate=10.0,
            device_name="Speaker",
            hostapi="WASAPI",
        )
        assert entered.wait(1.0)
        out = np.full((4, 1), 9.0, dtype=np.float32)

        with pytest.raises(Exception) as exc_info:
            captured["callback"](out, 4, None, None)

        assert exc_info.type.__name__ == "CallbackStop"
        np.testing.assert_array_equal(
            out[:, 0],
            np.array([0.1, 0.2, 0.0, 0.0], dtype=np.float32),
        )
        assert exited.wait(1.0)

    def test_replay_player_applies_gain_and_clips_segment(self, monkeypatch):
        captured = {}
        entered = threading.Event()

        class FakeOutputStream:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                entered.set()
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        player = OneShotReplayPlayer()
        assert player.play(
            segment=np.array([0.25, 0.75], dtype=np.float32),
            sample_rate=10.0,
            device_name="Speaker",
            hostapi="WASAPI",
            gain_db=6.0,
        )
        assert entered.wait(1.0)
        out = np.zeros((2, 1), dtype=np.float32)

        with pytest.raises(Exception) as exc_info:
            captured["callback"](out, 2, None, None)

        assert exc_info.type.__name__ == "CallbackStop"
        np.testing.assert_allclose(
            out[:, 0],
            np.array([0.25 * (10 ** (6 / 20)), 1.0], dtype=np.float32),
        )
        player.stop()

    def test_replay_player_replaces_previous_replay(self, monkeypatch):
        instances = []
        order = []

        class FakeOutputStream:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.entered = threading.Event()
                self.exited = threading.Event()
                self.index = len(instances)
                instances.append(self)

            def __enter__(self):
                order.append(("enter", self.index))
                self.entered.set()
                return self

            def __exit__(self, exc_type, exc, tb):
                order.append(("exit", self.index))
                self.exited.set()
                return False

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        player = OneShotReplayPlayer()
        assert player.play(
            segment=np.array([0.1, 0.2], dtype=np.float32),
            sample_rate=10.0,
            device_name="Speaker",
            hostapi="WASAPI",
        )
        assert _wait_until(lambda: len(instances) == 1 and instances[0].entered.is_set())

        assert player.play(
            segment=np.array([0.8], dtype=np.float32),
            sample_rate=10.0,
            device_name="Speaker",
            hostapi="WASAPI",
        )

        assert _wait_until(lambda: len(instances) == 2 and instances[1].entered.is_set())
        assert instances[0].exited.is_set()
        assert order.index(("exit", 0)) < order.index(("enter", 1))
        out = np.zeros((1, 1), dtype=np.float32)
        with pytest.raises(Exception) as exc_info:
            instances[1].kwargs["callback"](out, 1, None, None)

        assert exc_info.type.__name__ == "CallbackStop"
        np.testing.assert_array_equal(out[:, 0], np.array([0.8], dtype=np.float32))
        player.stop()

    def test_replay_player_failed_request_stops_previous_replay(self, monkeypatch):
        instances = []

        class FakeOutputStream:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.entered = threading.Event()
                self.exited = threading.Event()
                instances.append(self)

            def __enter__(self):
                self.entered.set()
                return self

            def __exit__(self, exc_type, exc, tb):
                self.exited.set()
                return False

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        player = OneShotReplayPlayer()
        try:
            assert player.play(
                segment=np.array([0.1, 0.2], dtype=np.float32),
                sample_rate=10.0,
                device_name="Speaker",
                hostapi="WASAPI",
            )
            assert _wait_until(lambda: len(instances) == 1 and instances[0].entered.is_set())

            assert (
                player.play(
                    segment=np.array([], dtype=np.float32),
                    sample_rate=10.0,
                    device_name="Speaker",
                    hostapi="WASAPI",
                )
                is False
            )

            assert instances[0].exited.is_set()
            assert len(instances) == 1
        finally:
            player.stop()

    def test_replay_player_rejects_empty_segment_without_stream(self, monkeypatch):
        stream_calls = []

        class FakeOutputStream:
            def __init__(self, **kwargs):
                stream_calls.append(kwargs)

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        player = OneShotReplayPlayer()
        assert (
            player.play(
                segment=np.array([], dtype=np.float32),
                sample_rate=10.0,
                device_name="Speaker",
                hostapi="WASAPI",
            )
            is False
        )
        assert stream_calls == []

    def test_replay_player_reports_output_stream_startup_failure(self, monkeypatch):
        class FakePortAudioError(Exception):
            pass

        class FakeOutputStream:
            def __init__(self, **kwargs):
                raise FakePortAudioError("boom")

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.PortAudioError",
            FakePortAudioError,
        )

        errors = []
        player = OneShotReplayPlayer()

        assert (
            player.play(
                segment=np.array([0.1], dtype=np.float32),
                sample_rate=10.0,
                device_name="Speaker",
                hostapi="WASAPI",
                error_callback=errors.append,
            )
            is False
        )
        assert errors == ["Audio replay device error: boom"]

    def test_replay_player_reports_callback_status(self, monkeypatch):
        captured = {}
        entered = threading.Event()
        errors = []

        class FakeOutputStream:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                entered.set()
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        player = OneShotReplayPlayer()
        assert player.play(
            segment=np.array([0.1, 0.2], dtype=np.float32),
            sample_rate=10.0,
            device_name="Speaker",
            hostapi="WASAPI",
            error_callback=errors.append,
        )
        assert entered.wait(1.0)
        out = np.zeros((1, 1), dtype=np.float32)

        captured["callback"](out, 1, None, "underflow")

        assert _wait_until(lambda: len(errors) == 1)
        assert "status" in errors[0].lower()
        assert "underflow" in errors[0].lower()
        player.stop()

    def test_replay_player_defers_slow_status_error_callback(self, monkeypatch):
        captured = {}
        entered = threading.Event()
        callback_threads = []
        error_threads = []

        class FakeOutputStream:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                entered.set()
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_hostapis",
            lambda: [{"name": "WASAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.query_devices",
            lambda: [{"name": "Speaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.monitor_playback.sd.OutputStream",
            FakeOutputStream,
        )

        def slow_error_callback(message):
            error_threads.append(threading.get_ident())
            time.sleep(0.2)

        player = OneShotReplayPlayer()
        assert player.play(
            segment=np.array([0.1, 0.2], dtype=np.float32),
            sample_rate=10.0,
            device_name="Speaker",
            hostapi="WASAPI",
            error_callback=slow_error_callback,
        )
        assert entered.wait(1.0)
        out = np.zeros((1, 1), dtype=np.float32)

        callback_threads.append(threading.get_ident())
        started = time.monotonic()
        captured["callback"](out, 1, None, "underflow")
        elapsed = time.monotonic() - started

        assert elapsed < 0.05
        assert error_threads == []
        assert _wait_until(lambda: len(error_threads) == 1)
        assert error_threads[0] != callback_threads[0]
        player.stop()


class TestAudioMonitorErrors:
    def test_run_opens_callback_output_stream(self, monkeypatch):
        captured = {}
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=0,
            output_latency_seconds=0.05,
        )

        class FakeOutputStream:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                mon.stop()
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.query_hostapis",
            lambda: [{"name": "TestAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.query_devices",
            lambda: [{"name": "TestSpeaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.OutputStream",
            FakeOutputStream,
        )

        mon.run()

        assert captured["device"] == 0
        assert captured["samplerate"] == 44100.0
        assert captured["channels"] == 1
        assert captured["dtype"] == "float32"
        assert captured["latency"] == 0.05
        assert captured["callback"] == mon._output_callback

    def test_run_emits_error_when_device_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.query_hostapis",
            lambda: [{"name": "Other"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.query_devices",
            lambda: [],
        )
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=0,
        )
        errors = []
        mon.error_occurred.connect(errors.append)

        mon.run()

        assert errors == ["Host API not found: TestAPI"]

    def test_run_emits_portaudio_errors(self, monkeypatch):
        class FakePortAudioError(Exception):
            pass

        class FakeOutputStream:
            def __init__(self, **kwargs):
                assert kwargs["channels"] == 1
                assert kwargs["dtype"] == "float32"
                assert kwargs["callback"]
                assert "latency" in kwargs
                raise FakePortAudioError("boom")

        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.query_hostapis",
            lambda: [{"name": "TestAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.query_devices",
            lambda: [{"name": "TestSpeaker", "hostapi": 0}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.OutputStream",
            FakeOutputStream,
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_monitor.sd.PortAudioError",
            FakePortAudioError,
        )

        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=0,
        )
        errors = []
        mon.error_occurred.connect(errors.append)

        mon.run()

        assert errors == ["Audio device error: boom"]
