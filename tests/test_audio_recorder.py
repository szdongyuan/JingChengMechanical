"""Tests for AudioRecorder.

sounddevice is mocked. These tests verify the recorder's state machine,
ring buffer accumulation, and signal emission, not actual hardware I/O.
"""
from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from app.service.analysis.audio_recorder import AudioRecorder, _resolve_device_index


HAS_PYTEST_QT = importlib.util.find_spec("pytestqt") is not None


class TestAudioRecorderInit:
    def test_creates_ring_buffer(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=2,
            record_duration=5.0,
        )
        assert rec._buffer.shape == (int(44100 * 5), 2)
        assert rec._write_pos == 0

    def test_default_chunk_duration(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=3.0,
        )
        assert rec._chunk_samples == int(44100 * 0.05)

    def test_stores_device_identity(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=3.0,
        )
        assert rec._device_name == "TestMic"
        assert rec._hostapi == "TestAPI"


class TestResolveDeviceIndex:
    def test_returns_matching_device_index(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.audio_recorder.sd.query_hostapis",
            lambda: [{"name": "Other"}, {"name": "TestAPI"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_recorder.sd.query_devices",
            lambda: [
                {"name": "MicA", "hostapi": 0},
                {"name": "TestMic", "hostapi": 1},
            ],
        )

        assert _resolve_device_index("TestMic", "TestAPI") == 1

    def test_raises_when_hostapi_missing(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.audio_recorder.sd.query_hostapis",
            lambda: [{"name": "Other"}],
        )

        with pytest.raises(ValueError, match="Host API not found: TestAPI"):
            _resolve_device_index("TestMic", "TestAPI")


class TestAudioRecorderBuffer:
    def test_feed_writes_to_buffer(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=1.0,
        )
        chunk = np.ones((100, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._write_pos == 100
        np.testing.assert_array_equal(rec._buffer[:100], chunk)

    def test_feed_multiple_chunks(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=2,
            record_duration=1.0,
        )
        chunk1 = np.ones((50, 2), dtype=np.float32)
        chunk2 = np.ones((50, 2), dtype=np.float32) * 2.0
        rec._feed_chunk(chunk1)
        rec._feed_chunk(chunk2)
        assert rec._write_pos == 100
        np.testing.assert_array_equal(rec._buffer[50:100], chunk2)

    def test_get_buffer_returns_written_portion(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=1.0,
        )
        chunk = np.ones((200, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        buf = rec.get_buffer()
        assert buf.shape == (200, 1)

    def test_buffer_does_not_overflow(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=100.0,
            n_channels=1,
            record_duration=1.0,
        )
        chunk = np.ones((150, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._write_pos == 100

    def test_extend_duration_grows_buffer_and_preserves_recorded_audio(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=100.0,
            n_channels=1,
            record_duration=1.0,
        )
        chunk = np.arange(10, dtype=np.float32).reshape(-1, 1)

        rec._feed_chunk(chunk)
        rec.extend_duration(2.0)

        assert rec._record_duration == 2.0
        assert rec._buffer.shape == (200, 1)
        assert rec._write_pos == 10
        np.testing.assert_array_equal(rec._buffer[:10], chunk)

    def test_attach_session_returns_atomic_boundary_and_extends_total_duration(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=100.0,
            n_channels=1,
            record_duration=3.0,
        )
        pre_attach = np.arange(20, dtype=np.float32).reshape(-1, 1)
        rec._feed_chunk(pre_attach)
        rec._emit_chunk()
        rec._chunk_index = 4
        rec._emitted_chunk_index = 4

        snapshot = rec.attach_session(record_duration=5.0)

        assert snapshot.chunk_index == 4
        assert snapshot.sample_index == 20
        assert rec._record_duration == 5.2
        assert rec._buffer.shape == (520, 1)
        np.testing.assert_array_equal(rec.get_buffer_from(snapshot), rec.get_buffer()[20:])

    def test_attach_session_uses_last_emitted_boundary_when_chunk_is_written_but_not_emitted(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=100.0,
            n_channels=1,
            record_duration=4.0,
        )
        first_chunk = np.arange(10, dtype=np.float32).reshape(-1, 1)
        second_chunk = np.arange(10, 20, dtype=np.float32).reshape(-1, 1)
        rec._feed_chunk(first_chunk)
        rec._emit_chunk()
        rec._feed_chunk(second_chunk)

        snapshot = rec.attach_session(record_duration=2.0)

        assert snapshot.chunk_index == 1
        assert snapshot.sample_index == 10
        assert rec._record_duration == 4.0

    def test_get_buffer_from_returns_only_post_attach_audio(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=100.0,
            n_channels=1,
            record_duration=4.0,
        )
        rec._feed_chunk(np.arange(12, dtype=np.float32).reshape(-1, 1))
        rec._emit_chunk()
        snapshot = rec.attach_session(record_duration=2.0)
        post_attach = np.arange(12, 18, dtype=np.float32).reshape(-1, 1)
        rec._feed_chunk(post_attach)

        np.testing.assert_array_equal(rec.get_buffer_from(snapshot), post_attach)


class TestAudioRecorderErrors:
    def test_sd_callback_emits_portaudio_status(self):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=1.0,
        )
        errors = []
        rec.error_occurred.connect(errors.append)

        rec._sd_callback(
            np.ones((10, 1), dtype=np.float32),
            10,
            None,
            "input overflow",
        )

        assert errors == ["PortAudio status: input overflow"]

    def test_run_emits_error_when_device_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "app.service.analysis.audio_recorder.sd.query_hostapis",
            lambda: [{"name": "Other"}],
        )
        monkeypatch.setattr(
            "app.service.analysis.audio_recorder.sd.query_devices",
            lambda: [],
        )
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=1.0,
        )
        errors = []
        rec.error_occurred.connect(errors.append)

        rec.run()

        assert errors == ["Host API not found: TestAPI"]


@pytest.mark.skipif(not HAS_PYTEST_QT, reason="pytest-qt not installed")
class TestAudioRecorderSignals:
    def test_chunk_ready_signal_emitted(self, qtbot):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=44100.0,
            n_channels=1,
            record_duration=1.0,
        )
        signals = []
        rec.chunk_ready.connect(lambda data, idx: signals.append((data, idx)))
        chunk = np.ones((100, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        rec._emit_chunk()
        assert len(signals) == 1
        assert signals[0][1] == 0

    def test_recording_done_signal(self, qtbot):
        rec = AudioRecorder(
            device_name="TestMic",
            hostapi="TestAPI",
            sample_rate=100.0,
            n_channels=1,
            record_duration=0.5,
        )
        done = []
        rec.recording_done.connect(lambda: done.append(True))
        chunk = np.ones((50, 1), dtype=np.float32)
        rec._feed_chunk(chunk)
        assert rec._is_buffer_full()
