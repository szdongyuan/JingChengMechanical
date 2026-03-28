"""Tests for AudioMonitor."""
from __future__ import annotations

import numpy as np

from app.service.analysis.audio_monitor import AudioMonitor


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

    def test_channel_out_of_range_returns_zeros(self):
        mon = AudioMonitor(
            device_name="TestSpeaker",
            hostapi="TestAPI",
            sample_rate=44100.0,
            monitor_channel=5,
        )
        chunk = np.array([[1.0, 2.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, np.zeros((1, 1), dtype=np.float32))


class TestAudioMonitorErrors:
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
