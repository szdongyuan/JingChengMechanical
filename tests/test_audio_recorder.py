"""Tests for AudioRecorder.

sounddevice is mocked. These tests verify the recorder's state machine,
ring buffer accumulation, and signal emission, not actual hardware I/O.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.service.analysis.audio_recorder import AudioRecorder, _resolve_device_index
from app.service.analysis.session_capture import CaptureNotAcceptedError


HAS_PYTEST_QT = importlib.util.find_spec("pytestqt") is not None


class _ExplodingPortAudioTime:
    @property
    def inputBufferAdcTime(self):
        raise RuntimeError("PortAudio time lookup failed")

    currentTime = 1.0


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


def _recorder(
    *,
    sample_rate: float = 100.0,
    record_duration: float = 1.0,
    monotonic_func=None,
) -> AudioRecorder:
    kwargs = {}
    if monotonic_func is not None:
        kwargs["monotonic_func"] = monotonic_func
    return AudioRecorder(
        device_name="TestMic",
        hostapi="TestAPI",
        sample_rate=sample_rate,
        n_channels=1,
        record_duration=record_duration,
        **kwargs,
    )


def _portaudio_time(adc_start: float, current_time: float) -> SimpleNamespace:
    return SimpleNamespace(
        inputBufferAdcTime=adc_start,
        currentTime=current_time,
    )


class TestExactCapturedBlocks:
    def test_callback_assigns_exact_spans_and_maps_adc_time_before_queue_drain(self):
        clock_values = iter([100.020, 100.040])
        rec = _recorder(
            sample_rate=1000.0,
            record_duration=1.0,
            monotonic_func=lambda: next(clock_values),
        )
        first_data = np.arange(20, dtype=np.float32).reshape(-1, 1)
        second_data = np.arange(20, 40, dtype=np.float32).reshape(-1, 1)

        rec._sd_callback(
            first_data,
            20,
            _portaudio_time(10.000, 10.020),
            None,
        )
        rec._sd_callback(
            second_data,
            20,
            _portaudio_time(10.020, 10.040),
            None,
        )

        first = rec._queue.get_nowait()
        second = rec._queue.get_nowait()
        assert (first.sample_start, first.sample_end) == (0, 20)
        assert (second.sample_start, second.sample_end) == (20, 40)
        assert (first.monotonic_start, first.monotonic_end) == pytest.approx(
            (100.000, 100.020)
        )
        assert (second.monotonic_start, second.monotonic_end) == pytest.approx(
            (100.020, 100.040)
        )
        assert not np.shares_memory(first.data, first_data)
        assert rec._capture_sample_cursor == 40
        assert rec._write_pos == 0

    def test_exact_partial_block_is_written_and_emitted_without_tail_reconstruction(
        self, qtbot
    ):
        rec = _recorder(sample_rate=100.0, record_duration=1.0)
        rec._sd_callback(
            np.array([[7.0], [8.0], [9.0]], dtype=np.float32),
            3,
            _portaudio_time(1.0, 1.03),
            None,
        )
        block = rec._queue.get_nowait()
        captured = []
        legacy = []
        rec.captured_block_ready.connect(captured.append)
        rec.chunk_ready.connect(lambda data, index: legacy.append((data, index)))

        rec._feed_captured_block(block)
        rec._emit_captured_block(block)

        assert captured == [block]
        assert captured[0] is block
        assert legacy[0][1] == 0
        np.testing.assert_array_equal(legacy[0][0][:, 0], [7.0, 8.0, 9.0])
        np.testing.assert_array_equal(rec.get_buffer()[:, 0], [7.0, 8.0, 9.0])

    @pytest.mark.parametrize(
        "time_info",
        [
            None,
            SimpleNamespace(inputBufferAdcTime=1.0),
            SimpleNamespace(inputBufferAdcTime=np.nan, currentTime=1.0),
            SimpleNamespace(inputBufferAdcTime=1.0, currentTime=np.inf),
            _ExplodingPortAudioTime(),
        ],
    )
    def test_unusable_portaudio_time_never_back_calculates_a_block_start(
        self, time_info
    ):
        rec = _recorder(monotonic_func=lambda: 50.0)

        rec._sd_callback(np.ones((5, 1), dtype=np.float32), 5, time_info, None)

        block = rec._queue.get_nowait()
        assert block.monotonic_start is None
        assert block.monotonic_end is None


class TestCaptureAttachments:
    def test_request_before_timed_block_attaches_at_block_start(self):
        clock_values = iter([99.9, 100.02])
        rec = _recorder(
            sample_rate=1000.0,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.01)

        rec._sd_callback(
            np.zeros((20, 1), dtype=np.float32),
            20,
            _portaudio_time(10.0, 10.02),
            None,
        )

        assert handle.resolution is not None
        assert handle.resolution.attach_sample == 0
        assert handle.resolution.attach_monotonic == pytest.approx(100.0)

    def test_request_inside_timed_block_uses_ceil_and_excludes_earlier_tags(self):
        clock_values = iter([100.0051, 100.1])
        rec = _recorder(
            sample_rate=1000.0,
            record_duration=0.01,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.004)
        tagged = np.arange(100, dtype=np.float32).reshape(-1, 1)

        rec._sd_callback(
            tagged,
            100,
            _portaudio_time(10.0, 10.1),
            None,
        )
        block = rec._queue.get_nowait()
        rec._feed_captured_block(block)

        assert handle.resolution is not None
        assert handle.resolution.attach_sample == 6
        assert handle.resolution.attach_monotonic == pytest.approx(100.006)
        assert handle.resolution.attach_monotonic >= handle.request_monotonic
        np.testing.assert_array_equal(
            rec.get_capture_buffer(handle, retained_frames=4)[:, 0],
            [56.0, 57.0, 58.0, 59.0],
        )

    def test_request_exactly_at_timed_block_end_attaches_at_sample_end(self):
        clock_values = iter([100.02, 100.02])
        rec = _recorder(
            sample_rate=1000.0,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.01)

        rec._sd_callback(
            np.zeros((20, 1), dtype=np.float32),
            20,
            _portaudio_time(10.0, 10.02),
            None,
        )

        assert handle.resolution is not None
        assert handle.resolution.attach_sample == 20
        assert handle.resolution.attach_monotonic == pytest.approx(100.02)

    def test_old_adc_block_does_not_resolve_a_request_made_before_callback_entry(self):
        clock_values = iter([100.03, 100.05, 100.07])
        rec = _recorder(
            sample_rate=1000.0,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.01)

        rec._sd_callback(
            np.zeros((20, 1), dtype=np.float32),
            20,
            _portaudio_time(10.0, 10.05),
            None,
        )
        assert handle.resolution is None

        rec._sd_callback(
            np.zeros((20, 1), dtype=np.float32),
            20,
            _portaudio_time(10.05, 10.07),
            None,
        )

        assert handle.resolution is not None
        assert handle.resolution.attach_sample == 20
        assert handle.resolution.attach_monotonic == pytest.approx(100.05)

    def test_unusable_time_excludes_whole_block_and_logs_degradation_once(
        self, caplog
    ):
        clock_values = iter([100.01, 100.02, 100.03, 100.04])
        rec = _recorder(
            sample_rate=1000.0,
            monotonic_func=lambda: next(clock_values),
        )
        first = rec.request_capture(record_duration=0.01)

        with caplog.at_level(
            logging.WARNING,
            logger="app.service.analysis.audio_recorder",
        ):
            rec._sd_callback(
                np.arange(20, dtype=np.float32).reshape(-1, 1),
                20,
                None,
                None,
            )
            second = rec.request_capture(record_duration=0.01)
            rec._sd_callback(
                np.arange(20, 40, dtype=np.float32).reshape(-1, 1),
                20,
                None,
                None,
            )

        assert first.resolution is not None
        assert first.resolution.attach_sample == 20
        assert first.resolution.attach_monotonic == pytest.approx(100.02)
        assert second.resolution is not None
        assert second.resolution.attach_sample == 40
        assert second.resolution.attach_monotonic == pytest.approx(100.04)
        degradation_logs = [
            record for record in caplog.records if "ADC time" in record.message
        ]
        assert len(degradation_logs) == 1

    def test_callback_waiting_for_request_lock_resolves_handle_once_from_same_block(
        self
    ):
        clock_entered = threading.Event()
        allow_request = threading.Event()
        clock_calls = iter([100.005, 100.02])

        def clock():
            value = next(clock_calls)
            if value == 100.005:
                clock_entered.set()
                assert allow_request.wait(timeout=2)
            return value

        rec = _recorder(sample_rate=1000.0, monotonic_func=clock)
        results = []

        requester = threading.Thread(
            target=lambda: results.append(rec.request_capture(record_duration=0.01))
        )
        requester.start()
        assert clock_entered.wait(timeout=2)
        callback = threading.Thread(
            target=lambda: rec._sd_callback(
                np.zeros((20, 1), dtype=np.float32),
                20,
                _portaudio_time(10.0, 10.02),
                None,
            )
        )
        callback.start()
        allow_request.set()
        requester.join(timeout=2)
        callback.join(timeout=2)

        assert not requester.is_alive()
        assert not callback.is_alive()
        handle = results[0]
        assert handle.resolution is not None
        assert handle.resolution.attach_sample == 5
        assert handle.resolution.attach_monotonic == pytest.approx(100.005)
        assert list(rec._active_capture_owners.values()) == [handle]


class TestCaptureCapacityOwnership:
    def test_pending_owner_blocks_legacy_completion_at_old_capacity(self):
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: 10.0,
        )
        rec._feed_chunk(np.ones((10, 1), dtype=np.float32))

        handle = rec.request_capture(record_duration=0.1)

        assert handle.resolution is None
        assert rec._is_buffer_full() is False
        assert rec._commit_capture_completion() is False
        assert rec._accepting_captures is True

    def test_pending_owner_forces_exact_callback_writes_to_grow_without_truncation(
        self
    ):
        clock_values = iter([10.0, 9.1, 9.2])
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.1)

        for start in (0, 10):
            rec._sd_callback(
                np.arange(start, start + 10, dtype=np.float32).reshape(-1, 1),
                10,
                _portaudio_time(start / 100, (start + 10) / 100),
                None,
            )
            rec._feed_captured_block(rec._queue.get_nowait())

        assert handle.resolution is None
        assert rec._write_pos == 20
        assert rec._buffer.shape[0] >= 20
        np.testing.assert_array_equal(
            rec._buffer[: rec._write_pos, 0], np.arange(20, dtype=np.float32)
        )
        assert rec.get_buffer().shape == (10, 1)
        assert rec._capture_complete() is False

    def test_resolved_owner_holds_completion_through_retained_end(self):
        clock_values = iter([0.0, 0.1, 0.15])
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.1)

        rec._sd_callback(
            np.arange(10, dtype=np.float32).reshape(-1, 1),
            10,
            _portaudio_time(0.0, 0.1),
            None,
        )
        rec._feed_captured_block(rec._queue.get_nowait())

        assert handle.resolution is not None
        assert handle.resolution.retained_end == 15
        assert rec._capture_complete() is False

        rec._sd_callback(
            np.arange(10, 15, dtype=np.float32).reshape(-1, 1),
            5,
            _portaudio_time(0.1, 0.15),
            None,
        )
        rec._feed_captured_block(rec._queue.get_nowait())
        assert rec._capture_complete() is True

    def test_two_resolved_owners_hold_through_latest_end_and_release_isolated(self):
        clock_values = iter([0.0, 0.05, 0.15, 0.2])
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        early = rec.request_capture(record_duration=0.1)
        late = rec.request_capture(record_duration=0.1)
        rec._sd_callback(
            np.arange(15, dtype=np.float32).reshape(-1, 1),
            15,
            _portaudio_time(0.0, 0.15),
            None,
        )
        rec._feed_captured_block(rec._queue.get_nowait())

        assert early.resolution is not None
        assert late.resolution is not None
        assert early.resolution.retained_end == 15
        assert late.resolution.retained_end == 20
        rec.release_capture(early)
        rec.release_capture(early)
        assert early.released is True
        assert late.released is False
        assert list(rec._active_capture_owners.values()) == [late]
        assert rec._capture_complete() is False

        rec._sd_callback(
            np.arange(15, 20, dtype=np.float32).reshape(-1, 1),
            5,
            _portaudio_time(0.15, 0.2),
            None,
        )
        rec._feed_captured_block(rec._queue.get_nowait())
        assert rec._capture_complete() is True

    def test_releasing_pending_owner_removes_completion_hold_idempotently(self):
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: 10.0,
        )
        rec._feed_chunk(np.ones((10, 1), dtype=np.float32))
        handle = rec.request_capture(record_duration=0.1)

        rec.release_capture(handle)
        rec.release_capture(handle)

        assert handle.released is True
        assert not rec._pending_capture_owners
        assert rec._capture_complete() is True

    def test_capture_buffer_is_exact_and_caps_at_requested_written_and_target(self):
        clock_values = iter([0.0, 0.12])
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        handle = rec.request_capture(record_duration=0.1)
        rec._sd_callback(
            np.arange(12, dtype=np.float32).reshape(-1, 1),
            12,
            _portaudio_time(0.0, 0.12),
            None,
        )
        rec._feed_captured_block(rec._queue.get_nowait())

        np.testing.assert_array_equal(
            rec.get_capture_buffer(handle, retained_frames=4)[:, 0],
            [5.0, 6.0, 7.0, 8.0],
        )
        np.testing.assert_array_equal(
            rec.get_capture_buffer(handle, retained_frames=100)[:, 0],
            [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0],
        )

        clock_values = iter([1.0, 1.2])
        full = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        full_handle = full.request_capture(record_duration=0.1)
        full._sd_callback(
            np.arange(20, dtype=np.float32).reshape(-1, 1),
            20,
            _portaudio_time(1.0, 1.2),
            None,
        )
        full._feed_captured_block(full._queue.get_nowait())
        np.testing.assert_array_equal(
            full.get_capture_buffer(full_handle, retained_frames=100)[:, 0],
            np.arange(5, 15, dtype=np.float32),
        )

    def test_unresolved_and_released_handles_extract_empty_channel_shaped_copies(self):
        rec = _recorder(monotonic_func=lambda: 10.0)
        unresolved = rec.request_capture(record_duration=0.1)

        unresolved_buffer = rec.get_capture_buffer(unresolved, retained_frames=10)
        assert unresolved_buffer.shape == (0, 1)
        assert unresolved_buffer.flags.owndata

        rec.release_capture(unresolved)
        released_buffer = rec.get_capture_buffer(unresolved, retained_frames=10)
        assert released_buffer.shape == (0, 1)
        assert released_buffer.flags.owndata

    def test_queued_old_final_block_does_not_finish_before_pending_owner_range(
        self, monkeypatch
    ):
        clock_values = iter([0.1, 2.0, 2.1, 2.2])
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        first_written = threading.Event()
        observations = []
        done = []
        rec.recording_done.connect(lambda: done.append(True))
        original_feed = rec._feed_captured_block

        def feed_and_signal(block):
            original_feed(block)
            if block.sample_end == 10:
                first_written.set()

        monkeypatch.setattr(rec, "_feed_captured_block", feed_and_signal)
        monkeypatch.setattr(
            "app.service.analysis.audio_recorder._resolve_device_index",
            lambda *_: 0,
        )
        producer_threads = []
        handles = []

        class FakeInputStream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                self.callback(
                    np.arange(10, dtype=np.float32).reshape(-1, 1),
                    10,
                    _portaudio_time(0.0, 0.1),
                    None,
                )
                handle = rec.request_capture(record_duration=0.1)
                handles.append(handle)

                def produce_owner_range():
                    assert first_written.wait(timeout=2)
                    observations.append((list(done), handle.resolution))
                    self.callback(
                        np.arange(10, 20, dtype=np.float32).reshape(-1, 1),
                        10,
                        _portaudio_time(2.0, 2.1),
                        None,
                    )
                    self.callback(
                        np.arange(20, 30, dtype=np.float32).reshape(-1, 1),
                        10,
                        _portaudio_time(2.1, 2.2),
                        None,
                    )

                producer = threading.Thread(target=produce_owner_range)
                producer.start()
                producer_threads.append(producer)
                return self

            def __exit__(self, *_):
                for producer in producer_threads:
                    producer.join(timeout=2)

        monkeypatch.setattr(
            "app.service.analysis.audio_recorder.sd.InputStream",
            FakeInputStream,
        )

        rec.run()

        assert observations == [([], None)]
        assert handles[0].resolution is not None
        assert handles[0].resolution.retained_end == 25
        assert rec._write_pos == 30
        assert done == [True]


class TestCaptureTerminalRaces:
    def test_request_registration_wins_shared_lock_and_prevents_completion(self):
        clock_entered = threading.Event()
        allow_clock = threading.Event()

        def clock():
            clock_entered.set()
            assert allow_clock.wait(timeout=2)
            return 10.0

        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=clock,
        )
        rec._feed_chunk(np.ones((10, 1), dtype=np.float32))
        handles = []
        completion_results = []
        requester = threading.Thread(
            target=lambda: handles.append(rec.request_capture(record_duration=0.1))
        )
        requester.start()
        assert clock_entered.wait(timeout=2)
        completer = threading.Thread(
            target=lambda: completion_results.append(
                rec._commit_capture_completion()
            )
        )
        completer.start()
        allow_clock.set()
        requester.join(timeout=2)
        completer.join(timeout=2)

        assert not requester.is_alive()
        assert not completer.is_alive()
        assert len(handles) == 1
        assert completion_results == [False]
        assert rec._accepting_captures is True
        assert list(rec._pending_capture_owners.values()) == handles

    def test_terminal_completion_wins_shared_lock_and_rejects_request(
        self, monkeypatch
    ):
        completion_entered = threading.Event()
        allow_completion = threading.Event()
        rec = _recorder(
            sample_rate=100.0,
            record_duration=0.1,
            monotonic_func=lambda: 10.0,
        )
        rec._feed_chunk(np.ones((10, 1), dtype=np.float32))
        original_capture_complete_locked = rec._capture_complete_locked

        def blocked_capture_complete_locked():
            completion_entered.set()
            assert allow_completion.wait(timeout=2)
            return original_capture_complete_locked()

        monkeypatch.setattr(
            rec,
            "_capture_complete_locked",
            blocked_capture_complete_locked,
        )
        completion_results = []
        request_errors = []
        completer = threading.Thread(
            target=lambda: completion_results.append(
                rec._commit_capture_completion()
            )
        )
        completer.start()
        assert completion_entered.wait(timeout=2)

        def request_after_completion():
            try:
                rec.request_capture(record_duration=0.1)
            except CaptureNotAcceptedError as exc:
                request_errors.append(exc)

        requester = threading.Thread(target=request_after_completion)
        requester.start()
        allow_completion.set()
        completer.join(timeout=2)
        requester.join(timeout=2)

        assert not completer.is_alive()
        assert not requester.is_alive()
        assert completion_results == [True]
        assert len(request_errors) == 1
        assert rec._accepting_captures is False
        assert not rec._pending_capture_owners
        assert not rec._active_capture_owners

    @pytest.mark.parametrize("terminal_action", ["stop", "error"])
    def test_stop_and_error_close_atomic_capture_acceptance(
        self, terminal_action, monkeypatch
    ):
        rec = _recorder(monotonic_func=lambda: 10.0)
        if terminal_action == "stop":
            rec.stop()
        else:
            monkeypatch.setattr(
                "app.service.analysis.audio_recorder.sd.query_hostapis",
                lambda: [],
            )
            rec.run()

        with pytest.raises(CaptureNotAcceptedError):
            rec.request_capture(record_duration=0.1)
        assert not rec._pending_capture_owners
        assert not rec._active_capture_owners
