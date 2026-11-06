"""Tests for best-effort live display dispatch."""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.service.analysis.frames import LoudnessFrame, SplFrame, StftFrame
from app.service.analysis.live_display_dispatcher import LiveDisplayDispatcher
from app.service.analysis.pipeline import AnalysisPipeline


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Bus:
    def __init__(self):
        self.stft_updated = _Signal()
        self.spl_updated = _Signal()
        self.loudness_updated = _Signal()
        self.octave_updated = _Signal()
        self.fft_updated = _Signal()
        self.log = _Signal()


def _wait_until(predicate, *, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class _SlowPipeline:
    channel_index = 0

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.processed_values: list[float] = []

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        value = float(np.asarray(mono).reshape(-1)[0])
        self.processed_values.append(value)
        if len(self.processed_values) == 1:
            self.started.set()
            assert self.release.wait(2.0)
        return {
            "stft_frames": [],
            "stft": None,
            "spl": SplFrame(time=elapsed_time, spl_db=value, weighting="Z"),
            "loudness": None,
            "octave": None,
            "fft": None,
        }


class _ResetRecordingPipeline:
    channel_index = 0

    def __init__(self):
        self.processed_chunk_indexes: list[int] = []
        self.reset_count = 0
        self.reset_snapshots: list[list[int]] = []

    def reset(self):
        self.reset_count += 1
        self.reset_snapshots.append(list(self.processed_chunk_indexes))

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        value = int(np.asarray(mono).reshape(-1)[0])
        self.processed_chunk_indexes.append(value)
        return {
            "stft_frames": [f"stft-{value}"],
            "stft": None,
            "spl": SplFrame(time=elapsed_time, spl_db=float(value), weighting="Z"),
            "loudness": None,
            "octave": None,
            "fft": None,
        }


class _BlockingResetRecordingPipeline(_ResetRecordingPipeline):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        result = super().process_chunk(
            mono,
            elapsed_time,
            compute_loudness=compute_loudness,
        )
        if len(self.processed_chunk_indexes) == 1:
            self.started.set()
            assert self.release.wait(2.0)
        return result


class _LoudnessBlockingPipeline:
    channel_index = 0

    def __init__(self):
        self.loudness_started = threading.Event()
        self.loudness_release = threading.Event()
        self.processed_values: list[float] = []

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        value = float(np.asarray(mono).reshape(-1)[0])
        self.processed_values.append(value)
        loudness = None
        if compute_loudness:
            self.loudness_started.set()
            assert self.loudness_release.wait(2.0)
            loudness = LoudnessFrame(time=elapsed_time, loudness_sone=value)
        return {
            "stft_frames": [],
            "stft": None,
            "spl": SplFrame(time=elapsed_time, spl_db=value, weighting="Z"),
            "loudness": loudness,
            "octave": None,
            "fft": None,
        }

    def prepare_live_loudness_input(self, elapsed_time):
        return np.array([elapsed_time], dtype=np.float64)

    def compute_live_loudness_frame(self, loudness_input, elapsed_time):
        self.loudness_started.set()
        assert self.loudness_release.wait(2.0)
        return LoudnessFrame(time=elapsed_time, loudness_sone=float(loudness_input[-1]))


class _BlockingPipeline:
    channel_index = 0

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        self.started.set()
        assert self.release.wait(2.0)
        return {
            "stft_frames": ["stale-stft"],
            "stft": None,
            "spl": SplFrame(time=elapsed_time, spl_db=77.0, weighting="Z"),
            "loudness": {"stale": True},
            "octave": {"stale": True},
            "fft": {"stale": True},
        }


class _SingleSplPipeline:
    channel_index = 0

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        return {
            "stft_frames": [],
            "stft": None,
            "spl": SplFrame(time=elapsed_time, spl_db=55.0, weighting="Z"),
            "loudness": None,
            "octave": None,
            "fft": None,
        }


class _EveryFramePipeline:
    channel_index = 0

    def __init__(self):
        self.processed_values: list[float] = []

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        value = float(np.asarray(mono).reshape(-1)[0])
        self.processed_values.append(value)
        return {
            "stft_frames": [f"stft-{value:g}"],
            "stft": None,
            "spl": SplFrame(time=elapsed_time, spl_db=value, weighting="Z"),
            "loudness": LoudnessFrame(time=elapsed_time, loudness_sone=value),
            "octave": f"octave-{value:g}",
            "fft": f"fft-{value:g}",
        }


class _CoalescingPipeline:
    channel_index = 0

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.processed_chunks: list[list[float]] = []

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        values = np.asarray(mono).reshape(-1).astype(float).tolist()
        self.processed_chunks.append(values)
        if len(self.processed_chunks) == 1:
            self.started.set()
            assert self.release.wait(2.0)
        return {
            "stft_frames": [],
            "stft": None,
            "spl": None,
            "loudness": None,
            "octave": None,
            "fft": None,
        }


class _TimedStftPipeline:
    channel_index = 0

    def __init__(self):
        self.processed = 0

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        self.processed += 1
        return {
            "stft_frames": [
                StftFrame(
                    time=float(elapsed_time),
                    frequencies=np.array([100.0]),
                    magnitudes=np.array([1.0]),
                )
            ],
            "stft": None,
            "spl": None,
            "loudness": None,
            "octave": None,
            "fft": None,
        }


def test_optional_pending_coalescing_preserves_contiguous_samples():
    bus = _Bus()
    pipeline = _CoalescingPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=8,
        coalesce_pending=True,
    )

    try:
        dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.01,
            frame_run_id="run-1",
            live_loudness_channel_index=None,
            sample_start=0,
            sample_end=1,
        )
        assert pipeline.started.wait(1.0)
        dispatcher.submit(
            np.array([[2.0]], dtype=np.float32),
            elapsed_time=0.02,
            frame_run_id="run-1",
            live_loudness_channel_index=None,
            sample_start=1,
            sample_end=2,
        )
        dispatcher.submit(
            np.array([[3.0]], dtype=np.float32),
            elapsed_time=0.03,
            frame_run_id="run-1",
            live_loudness_channel_index=None,
            sample_start=2,
            sample_end=3,
        )
        pipeline.release.set()
        _wait_until(lambda: len(pipeline.processed_chunks) == 2)
        assert pipeline.processed_chunks == [[1.0], [2.0, 3.0]]
    finally:
        pipeline.release.set()
        dispatcher.stop()


def test_pending_coalescing_keeps_every_sample_without_submit_thread_merging():
    bus = _Bus()
    pipeline = _CoalescingPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=2,
        coalesce_pending=True,
    )

    try:
        dispatcher.submit(
            np.array([[0.0]], dtype=np.float32),
            elapsed_time=0.01,
            frame_run_id="run-1",
            live_loudness_channel_index=None,
            sample_start=0,
            sample_end=1,
        )
        assert pipeline.started.wait(1.0)
        for value in range(1, 7):
            dispatcher.submit(
                np.array([[float(value)]], dtype=np.float32),
                elapsed_time=(value + 1) / 100.0,
                frame_run_id="run-1",
                live_loudness_channel_index=None,
                sample_start=value,
                sample_end=value + 1,
            )
            assert dispatcher.pending_count == value

        pipeline.release.set()
        assert dispatcher.wait_until_idle(2.0) is True
        assert pipeline.processed_chunks == [
            [0.0],
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        ]
        assert bus.log.calls == []
    finally:
        pipeline.release.set()
        dispatcher.stop()


def test_optional_stft_emit_interval_limits_ui_signal_rate():
    bus = _Bus()
    pipeline = _TimedStftPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        stft_emit_interval_sec=0.05,
    )

    try:
        for index, elapsed_time in enumerate((0.01, 0.03, 0.07)):
            dispatcher.submit(
                np.array([[float(index)]], dtype=np.float32),
                elapsed_time=elapsed_time,
                frame_run_id="run-1",
                live_loudness_channel_index=None,
                sample_start=index,
                sample_end=index + 1,
            )
            _wait_until(lambda count=index + 1: pipeline.processed >= count)

        _wait_until(lambda: len(bus.stft_updated.calls) == 2)
        assert [
            call[3].time for call in bus.stft_updated.calls
        ] == pytest.approx([0.01, 0.07])
    finally:
        dispatcher.stop()


def test_ordered_display_submission_processes_pending_chunks_fifo():
    bus = _Bus()
    pipeline = _SlowPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.10,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=1,
        ) is True
        assert pipeline.started.wait(1.0)

        assert dispatcher.submit(
            np.array([[2.0]], dtype=np.float32),
            elapsed_time=0.20,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=1,
            sample_end=2,
        ) is True
        assert dispatcher.pending_count <= 4
        assert dispatcher.submit(
            np.array([[3.0]], dtype=np.float32),
            elapsed_time=0.35,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=2,
            sample_end=3,
        ) is True
        assert dispatcher.pending_count <= 4

        pipeline.release.set()
        _wait_until(lambda: pipeline.processed_values == [1.0, 2.0, 3.0])

        emitted_spl = [call[3].spl_db for call in bus.spl_updated.calls]
        assert emitted_spl == [1.0, 3.0]
    finally:
        dispatcher.stop()


def test_spl_emission_throttle_keeps_processing_and_other_display_frames():
    bus = _Bus()
    pipeline = _EveryFramePipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        for value, elapsed, start, end in (
            (1.0, 0.10, 0, 10),
            (2.0, 0.20, 10, 20),
            (3.0, 0.35, 20, 30),
        ):
            assert dispatcher.submit(
                np.array([[value]], dtype=np.float32),
                elapsed_time=elapsed,
                frame_run_id="run-1",
                live_loudness_channel_index=0,
                sample_start=start,
                sample_end=end,
            ) is True

        _wait_until(lambda: pipeline.processed_values == [1.0, 2.0, 3.0])

        assert [call[3].spl_db for call in bus.spl_updated.calls] == [1.0, 3.0]
        assert [call[3] for call in bus.stft_updated.calls] == [
            "stft-1",
            "stft-2",
            "stft-3",
        ]
        assert [call[3].loudness_sone for call in bus.loudness_updated.calls] == [
            1.0,
            2.0,
            3.0,
        ]
        assert bus.octave_updated.calls == []
        assert [call[3] for call in bus.fft_updated.calls] == [
            "fft-1",
            "fft-2",
            "fft-3",
        ]
    finally:
        dispatcher.stop()


def test_live_dispatcher_does_not_emit_octave_updates():
    bus = _Bus()
    pipeline = _EveryFramePipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.30,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=10,
        ) is True

        _wait_until(lambda: [call[3] for call in bus.fft_updated.calls] == ["fft-1"])

        assert bus.octave_updated.calls == []
    finally:
        dispatcher.stop()


def test_spl_emission_throttle_key_includes_run_id():
    bus = _Bus()
    pipeline = _EveryFramePipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.10,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=10,
        ) is True
        assert dispatcher.submit(
            np.array([[2.0]], dtype=np.float32),
            elapsed_time=0.20,
            frame_run_id="run-2",
            live_loudness_channel_index=0,
            sample_start=10,
            sample_end=20,
        ) is True

        _wait_until(lambda: pipeline.processed_values == [1.0, 2.0])

        assert [call[1] for call in bus.spl_updated.calls] == ["run-1", "run-2"]
        assert [call[3].spl_db for call in bus.spl_updated.calls] == [1.0, 2.0]
    finally:
        dispatcher.stop()


def test_sample_span_gap_resets_pipelines_before_later_chunk():
    bus = _Bus()
    pipeline = _ResetRecordingPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.10,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=10,
        ) is True
        assert dispatcher.submit(
            np.array([[3.0]], dtype=np.float32),
            elapsed_time=0.30,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=20,
            sample_end=30,
        ) is True

        _wait_until(lambda: pipeline.processed_chunk_indexes == [1, 3])

        assert pipeline.reset_count == 1
        assert pipeline.reset_snapshots == [[1]]
        assert [call[3].spl_db for call in bus.spl_updated.calls] == [1.0, 3.0]
        assert [call[3] for call in bus.stft_updated.calls] == ["stft-1", "stft-3"]
    finally:
        dispatcher.stop()


def test_sample_span_gap_resets_complete_live_spl_detector_state():
    sample_rate = 48_000.0
    channel_config = {
        "channel_index": 0,
        "stft_nfft": 4096,
        "fft_nfft": 4096,
        "stft_overlap": 0.5,
        "spl_weighting": "A",
        "spl_time_constant": 0.125,
        "oct_weighting": "Z",
    }
    pipeline = AnalysisPipeline(0, channel_config, sample_rate)
    pristine = AnalysisPipeline(0, channel_config, sample_rate)
    prefix = np.full(2400, 0.5, dtype=np.float64)
    times = np.arange(2400, dtype=np.float64) / sample_rate
    probe = 0.1 * np.sin(2.0 * np.pi * 31.5 * times)
    expected = pristine.process_chunk(
        probe,
        elapsed_time=0.15,
        compute_loudness=False,
    )["spl"].spl_db
    bus = _Bus()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            prefix.reshape(-1, 1),
            elapsed_time=0.05,
            frame_run_id="run-1",
            live_loudness_channel_index=None,
            sample_start=0,
            sample_end=2400,
        ) is True
        _wait_until(lambda: len(bus.spl_updated.calls) == 1)

        assert dispatcher.submit(
            probe.reshape(-1, 1),
            elapsed_time=0.15,
            frame_run_id="run-1",
            live_loudness_channel_index=None,
            sample_start=4800,
            sample_end=7200,
        ) is True
        _wait_until(lambda: len(bus.spl_updated.calls) == 2)

        actual = bus.spl_updated.calls[-1][3].spl_db
        assert actual == pytest.approx(expected, abs=1e-12)
    finally:
        dispatcher.stop()


def test_queue_overflow_drops_old_pending_work_and_resets_on_gap():
    bus = _Bus()
    pipeline = _BlockingResetRecordingPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=2,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[0.0]], dtype=np.float32),
            elapsed_time=0.10,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=10,
        ) is True
        assert pipeline.started.wait(1.0)

        for value, start, end in (
            (10.0, 10, 20),
            (20.0, 20, 30),
            (30.0, 30, 40),
        ):
            assert dispatcher.submit(
                np.array([[value]], dtype=np.float32),
                elapsed_time=end / 100.0,
                frame_run_id="run-1",
                live_loudness_channel_index=0,
                sample_start=start,
                sample_end=end,
            ) is True
            assert dispatcher.pending_count <= 2

        pipeline.release.set()
        _wait_until(lambda: pipeline.processed_chunk_indexes == [0, 20, 30])

        assert 10 not in pipeline.processed_chunk_indexes
        assert pipeline.reset_count == 1
        assert pipeline.reset_snapshots == [[0]]
        assert [call[3].spl_db for call in bus.spl_updated.calls] == [0.0, 20.0]
    finally:
        pipeline.release.set()
        dispatcher.stop()


def test_queue_overflow_logs_once_while_backlog_remains_overloaded():
    bus = _Bus()
    pipeline = _SlowPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=1,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[0.0]], dtype=np.float32),
            elapsed_time=0.10,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=10,
        ) is True
        assert pipeline.started.wait(1.0)

        for value, start, end in (
            (1.0, 10, 20),
            (2.0, 20, 30),
            (3.0, 30, 40),
        ):
            assert dispatcher.submit(
                np.array([[value]], dtype=np.float32),
                elapsed_time=end / 100.0,
                frame_run_id="run-1",
                live_loudness_channel_index=0,
                sample_start=start,
                sample_end=end,
            ) is True
            assert dispatcher.pending_count <= 1

        overflow_logs = [
            call[0] for call in bus.log.calls
            if "display queue overflow" in call[0].lower()
        ]
        assert len(overflow_logs) == 1
    finally:
        pipeline.release.set()
        dispatcher.stop()


def test_slow_live_loudness_does_not_block_ordered_spl_stft_processing():
    bus = _Bus()
    pipeline = _LoudnessBlockingPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
        max_queue_size=4,
    )

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.30,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=0,
            sample_end=1,
        ) is True
        assert pipeline.loudness_started.wait(1.0)

        assert dispatcher.submit(
            np.array([[2.0]], dtype=np.float32),
            elapsed_time=0.60,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
            sample_start=1,
            sample_end=2,
        ) is True

        _wait_until(lambda: [call[3].spl_db for call in bus.spl_updated.calls] == [1.0, 2.0])
    finally:
        pipeline.loudness_release.set()
        dispatcher.stop()


def test_stop_suppresses_display_emits_after_busy_worker_timeout():
    bus = _Bus()
    pipeline = _BlockingPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
    )

    dispatcher.start()
    assert dispatcher.submit(
        np.array([[1.0]], dtype=np.float32),
        elapsed_time=0.10,
        frame_run_id="run-1",
        live_loudness_channel_index=0,
    ) is True
    assert pipeline.started.wait(1.0)

    assert dispatcher.stop(timeout=0.01) is False

    assert bus.log.calls
    assert "did not stop" in bus.log.calls[-1][0].lower()

    pipeline.release.set()
    _wait_until(lambda: not dispatcher._thread.is_alive())

    assert bus.stft_updated.calls == []
    assert bus.spl_updated.calls == []
    assert bus.loudness_updated.calls == []
    assert bus.octave_updated.calls == []
    assert bus.fft_updated.calls == []


def test_stop_uses_one_deadline_for_both_workers():
    bus = _Bus()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[],
        bus=bus,
        config_id=7,
    )
    release = threading.Event()
    display_thread = threading.Thread(target=release.wait)
    loudness_thread = threading.Thread(target=release.wait)
    display_thread.start()
    loudness_thread.start()
    dispatcher._thread = display_thread
    dispatcher._loudness_thread = loudness_thread

    try:
        started = time.monotonic()
        result = dispatcher.stop(timeout=0.05)
        elapsed = time.monotonic() - started

        assert result is False
        assert elapsed < 0.08
        assert display_thread.is_alive()
        assert loudness_thread.is_alive()
    finally:
        release.set()
        display_thread.join(1.0)
        loudness_thread.join(1.0)


def test_submit_after_stop_is_rejected_without_thread_restart_or_emit():
    bus = _Bus()
    pipeline = _SingleSplPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
    )

    dispatcher.start()
    original_thread = dispatcher._thread
    assert original_thread is not None

    dispatcher.stop()
    _wait_until(lambda: not original_thread.is_alive())
    stopped_thread = dispatcher._thread

    accepted = dispatcher.submit(
        np.array([[1.0]], dtype=np.float32),
        elapsed_time=0.10,
        frame_run_id="run-1",
        live_loudness_channel_index=0,
    )

    assert accepted is False
    assert dispatcher._thread is stopped_thread
    assert bus.stft_updated.calls == []
    assert bus.spl_updated.calls == []
    assert bus.loudness_updated.calls == []
    assert bus.octave_updated.calls == []
    assert bus.fft_updated.calls == []


def test_stop_linearizes_state_transition_after_final_emit_check():
    bus = _Bus()
    pipeline = _SingleSplPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
    )
    final_emit_check_passed = threading.Event()
    release_final_emit_check = threading.Event()
    emit_started = threading.Event()
    release_emit = threading.Event()
    stop_started = threading.Event()
    stop_returned = threading.Event()
    order: list[str] = []
    stopping_states_seen_by_emit: list[bool] = []
    original_work_is_current = dispatcher._work_is_current
    current_work_passes = 0

    def gated_work_is_current(work):
        nonlocal current_work_passes
        result = original_work_is_current(work)
        if result:
            current_work_passes += 1
            if current_work_passes == 4:
                final_emit_check_passed.set()
                assert release_final_emit_check.wait(2.0)
        return result

    def emit_spl(*args):
        with dispatcher._condition:
            stopping_states_seen_by_emit.append(dispatcher._stopping)
        order.append("spl_emit")
        emit_started.set()
        assert release_emit.wait(2.0)
        bus.spl_updated.calls.append(args)

    def stop_dispatcher():
        stop_started.set()
        dispatcher.stop(timeout=0.01)
        order.append("stop_returned")
        stop_returned.set()

    dispatcher._work_is_current = gated_work_is_current
    bus.spl_updated.emit = emit_spl
    stop_thread = threading.Thread(target=stop_dispatcher)

    try:
        dispatcher.start()
        assert dispatcher.submit(
            np.array([[1.0]], dtype=np.float32),
            elapsed_time=0.10,
            frame_run_id="run-1",
            live_loudness_channel_index=0,
        ) is True
        assert final_emit_check_passed.wait(1.0)

        stop_thread.start()
        assert stop_started.wait(1.0)
        time.sleep(0.05)
        assert not stop_returned.wait(0.20)

        release_final_emit_check.set()
        assert emit_started.wait(1.0)
        assert not stop_returned.wait(0.20)
        release_emit.set()
        assert stop_returned.wait(1.0)
        stop_thread.join(1.0)

        assert stopping_states_seen_by_emit == [False]
        assert order == ["spl_emit", "stop_returned"]
        assert bus.stft_updated.calls == []
        assert len(bus.spl_updated.calls) == 1
        assert bus.loudness_updated.calls == []
        assert bus.octave_updated.calls == []
        assert bus.fft_updated.calls == []
    finally:
        release_final_emit_check.set()
        release_emit.set()
        stop_thread.join(1.0)
        dispatcher.stop()


def test_stop_clears_spl_emission_throttle_state():
    bus = _Bus()
    pipeline = _SingleSplPipeline()
    dispatcher = LiveDisplayDispatcher(
        pipelines=[pipeline],
        bus=bus,
        config_id=7,
    )

    dispatcher.start()
    assert dispatcher.submit(
        np.array([[1.0]], dtype=np.float32),
        elapsed_time=0.10,
        frame_run_id="run-1",
        live_loudness_channel_index=0,
    ) is True
    _wait_until(lambda: len(bus.spl_updated.calls) == 1)
    assert dispatcher._last_spl_emit_time_by_key

    dispatcher.stop()

    assert dispatcher._last_spl_emit_time_by_key == {}


class _CoarsePipeline:
    channel_index = 0
    sample_rate = 1000
    _nfft = 20
    _overlap = .5

    def __init__(self, block_call=None):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_call = block_call
        self.resets = 0

    def reset(self):
        self.resets += 1

    def process_chunk(self, mono, elapsed_time, *, compute_loudness=True):
        self.calls += 1
        if self.calls == self.block_call:
            self.started.set()
            assert self.release.wait(2)
        return {"stft_frames": [StftFrame(elapsed_time, np.arange(513.), np.full(513, mono[0]))]}


def _coarse_dispatcher(pipeline, bus, interval=.1):
    return LiveDisplayDispatcher(pipelines=[pipeline], bus=bus, config_id=7,
                                 coarse_stft=True, stft_emit_interval_sec=interval)


def _submit_coarse(dispatcher, index=0, value=1, run="run-1", source="source-1", start=None):
    return dispatcher.submit(np.array([[value]], dtype=float), elapsed_time=.005 + index * .01,
                             frame_run_id=run, live_loudness_channel_index=None,
                             sample_start=index * 10 if start is None else start,
                             sample_end=index * 10 + 10 if start is None else start + 10,
                             source_continuity=source)


def test_coarse_quiet_tail_emits_without_further_submission():
    bus = _Bus()
    emitted = threading.Event()
    original_emit = bus.stft_updated.emit
    bus.stft_updated.emit = lambda *args: (original_emit(*args), emitted.set())
    dispatcher = _coarse_dispatcher(_CoarsePipeline(), bus)
    try:
        assert _submit_coarse(dispatcher)
        assert emitted.wait(2)
        call, = bus.stft_updated.calls
        assert call[:3] == (7, "run-1", 0)
        assert call[3].time_bounds == pytest.approx((0, .01))
        assert call[3].source_frame_count == 1
    finally:
        dispatcher.stop()


def test_coarse_idle_includes_pending_tail_and_drains_it():
    bus = _Bus()
    dispatcher = _coarse_dispatcher(_CoarsePipeline(), bus, interval=10)
    try:
        assert _submit_coarse(dispatcher)
        _wait_until(lambda: not dispatcher._active_work and not dispatcher.pending_count)
        assert dispatcher.wait_until_idle(2)
        assert len(bus.stft_updated.calls) == 1
        assert dispatcher._coarse_pending == {}
    finally:
        dispatcher.stop()


def test_coarse_stop_clears_tail_and_suppresses_blocked_dsp_results():
    bus = _Bus()
    pipeline = _CoarsePipeline(block_call=2)
    dispatcher = _coarse_dispatcher(pipeline, bus, interval=10)
    try:
        assert _submit_coarse(dispatcher)
        assert _submit_coarse(dispatcher, 1)
        assert pipeline.started.wait(1)
        assert dispatcher.stop(timeout=.01) is False
        assert dispatcher._coarse_pending == {}
        pipeline.release.set()
        dispatcher._thread.join(2)
        assert not dispatcher._thread.is_alive()
        assert bus.stft_updated.calls == []
        assert dispatcher._coarse_pending == {}
    finally:
        pipeline.release.set()
        dispatcher.stop()


@pytest.mark.parametrize("change", ["source", "sample", "run"])
def test_coarse_pre_gap_blocked_work_keeps_original_identity(change):
    bus = _Bus()
    pipeline = _CoarsePipeline(block_call=1)
    dispatcher = _coarse_dispatcher(pipeline, bus, interval=10)
    try:
        assert _submit_coarse(dispatcher)
        assert pipeline.started.wait(1)
        kwargs = {"source": "source-2"} if change == "source" else {"start": 30} if change == "sample" else {"run": "run-2"}
        assert _submit_coarse(dispatcher, 1, value=9, **kwargs)
        pipeline.release.set()
        assert dispatcher.wait_until_idle(2)
        calls = bus.stft_updated.calls
        assert len(calls) == 2
        assert [c[3].source_frame_count for c in calls] == [1, 1]
        assert [c[3].magnitudes.max() for c in calls] == [1, 9]
        assert [c[1] for c in calls] == ["run-1", "run-2" if change == "run" else "run-1"]
        assert pipeline.resets == 1
    finally:
        pipeline.release.set()
        dispatcher.stop()


def test_coarse_two_channels_keep_independent_peaks_and_original_signal_identity():
    bus = _Bus()
    first, second = _CoarsePipeline(), _CoarsePipeline()
    second.channel_index = 1
    dispatcher = LiveDisplayDispatcher(pipelines=[first, second], bus=bus, config_id=7,
                                       coarse_stft=True, stft_emit_interval_sec=.1)
    try:
        for i in range(10):
            assert dispatcher.submit(np.array([[20 if i == 4 else 1, 70 if i == 7 else 2]]),
                                     elapsed_time=.005 + i * .01, frame_run_id="run-1",
                                     live_loudness_channel_index=None, sample_start=i, sample_end=i + 1)
            _wait_until(lambda: second.calls == i + 1)
        assert dispatcher.wait_until_idle(2)
        for channel, peak in [(0, 20), (1, 70)]:
            calls = [c for c in bus.stft_updated.calls if c[2] == channel]
            assert sum(c[3].source_frame_count for c in calls) == 10
            assert max(c[3].magnitudes.max() for c in calls) == peak
            assert all(len(c[3].magnitudes) <= 256 for c in calls)
            assert all(c[:3] == (7, "run-1", channel) for c in calls)
    finally:
        dispatcher.stop()
