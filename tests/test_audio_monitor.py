"""Tests for AudioMonitor."""
from __future__ import annotations

import numpy as np

from app.service.analysis.audio_monitor import AudioMonitor


class TestAudioMonitorInit:
    def test_init(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=0,
        )
        assert mon._monitor_channel == 0
        assert mon._sample_rate == 44100.0


class TestAudioMonitorFeed:
    def test_extract_channel(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=1,
        )
        chunk = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, np.array([[2.0], [4.0]]))

    def test_extract_single_channel(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=0,
        )
        chunk = np.array([[1.0], [3.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, chunk)

    def test_channel_out_of_range_returns_zeros(self):
        mon = AudioMonitor(
            device_index=0, sample_rate=44100.0, monitor_channel=5,
        )
        chunk = np.array([[1.0, 2.0]], dtype=np.float32)
        mono = mon._extract_channel(chunk)
        np.testing.assert_array_equal(mono, np.zeros((1, 1), dtype=np.float32))
