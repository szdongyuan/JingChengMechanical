"""Tests for AudioRecorder.

sounddevice is mocked — these tests verify the recorder's state machine,
ring buffer accumulation, and signal emission, not actual hardware I/O.
"""
from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from app.service.analysis.audio_recorder import AudioRecorder


HAS_PYTEST_QT = importlib.util.find_spec("pytestqt") is not None


class TestAudioRecorderInit:
    def test_creates_ring_buffer(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=2, record_duration=5.0,
        )
        assert rec._buffer.shape == (int(44100 * 5), 2)
        assert rec._write_pos == 0

    def test_default_chunk_duration(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=3.0,
        )
        # Default chunk ~50 ms
        assert rec._chunk_samples == int(44100 * 0.05)


class TestAudioRecorderBuffer:
    def test_feed_writes_to_buffer(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=1.0,
        )
        chunk = np.ones((100, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._write_pos == 100
        np.testing.assert_array_equal(rec._buffer[:100], chunk)

    def test_feed_multiple_chunks(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=2, record_duration=1.0,
        )
        chunk1 = np.ones((50, 2), dtype=np.float32)
        chunk2 = np.ones((50, 2), dtype=np.float32) * 2.0
        rec._feed_chunk(chunk1)
        rec._feed_chunk(chunk2)
        assert rec._write_pos == 100
        np.testing.assert_array_equal(rec._buffer[50:100], chunk2)

    def test_get_buffer_returns_written_portion(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=1.0,
        )
        chunk = np.ones((200, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        buf = rec.get_buffer()
        assert buf.shape == (200, 1)

    def test_buffer_does_not_overflow(self):
        rec = AudioRecorder(
            device_index=0, sample_rate=100.0,
            n_channels=1, record_duration=1.0,
        )
        # Buffer holds 100 samples. Feed 150.
        chunk = np.ones((150, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        # Should clamp at buffer size
        assert rec._write_pos == 100


@pytest.mark.skipif(not HAS_PYTEST_QT, reason="pytest-qt not installed")
class TestAudioRecorderSignals:
    def test_chunk_ready_signal_emitted(self, qtbot):
        rec = AudioRecorder(
            device_index=0, sample_rate=44100.0,
            n_channels=1, record_duration=1.0,
        )
        signals = []
        rec.chunk_ready.connect(lambda data, idx: signals.append((data, idx)))
        chunk = np.ones((100, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        rec._emit_chunk()
        assert len(signals) == 1
        assert signals[0][1] == 0  # chunk_index

    def test_recording_done_signal(self, qtbot):
        rec = AudioRecorder(
            device_index=0, sample_rate=100.0,
            n_channels=1, record_duration=0.5,
        )
        done = []
        rec.recording_done.connect(lambda: done.append(True))
        # Fill buffer completely
        chunk = np.ones((50, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._is_buffer_full()
