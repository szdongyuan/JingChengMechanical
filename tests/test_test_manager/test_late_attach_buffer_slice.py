from __future__ import annotations

import gc
import weakref
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication, QEventLoop
from scipy.io import wavfile

from app.service.analysis.audio_recorder import AudioRecorder
from app.service.analysis.frames import JudgmentResult
from app.service.analysis.selected_channels import project_selected_channels
from app.service.analysis.session_capture import (
    CapturedAudioBlock,
    RecorderAttachment,
    ResolvedCaptureBounds,
    frames_for_duration,
)
from app.service.analysis.test_manager import TestManager

from ._support import (
    _DummySignal,
    _complete_current_session,
    _expected_no_result_payload,
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


class _InlineDisplayDispatcher:
    def __init__(self, pipelines):
        self._pipelines = list(pipelines)

    def submit(
        self,
        chunk,
        *,
        elapsed_time,
        frame_run_id,
        live_loudness_channel_index,
    ):
        for pipeline in self._pipelines:
            channel_index = pipeline.channel_index
            if chunk.ndim == 2:
                mono = chunk[:, channel_index]
            else:
                mono = chunk
            pipeline.process_chunk(
                mono,
                elapsed_time,
                compute_loudness=(channel_index == live_loudness_channel_index),
            )
        return True

    def stop(self):
        return None


def _install_completed_capture(session, recorder, buffer: np.ndarray):
    attachment = object()
    session.capture_attachment = attachment
    session.captured_frame_count = int(buffer.shape[0])
    session.freeze_capture = MagicMock(return_value=int(buffer.shape[0]))
    recorder._n_channels = int(buffer.shape[1])
    recorder.get_capture_buffer.return_value = buffer
    recorder.captured_block_ready = _DummySignal()
    return attachment


class _ExactStreamingRecorder:
    def __init__(self, *, device_name, hostapi, sample_rate, n_channels, record_duration):
        self._sample_rate = float(sample_rate)
        self._n_channels = int(n_channels)
        self._record_duration = float(record_duration)
        self._buffer = np.empty((0, self._n_channels), dtype=np.float32)
        self._chunk_samples = int(self._sample_rate * 0.05)
        self._write_pos = 0
        self._running = False
        self._attach_emit_value = None
        self.captured_block_ready = _DummySignal()
        self.recording_done = _DummySignal()
        self.error_occurred = _DummySignal()
        self._owners = set()

    def request_capture(self, *, record_duration):
        warmup_samples = frames_for_duration(self._sample_rate, 0.05)
        target_samples = frames_for_duration(self._sample_rate, record_duration)
        owner = RecorderAttachment(
            request_monotonic=float(self._write_pos) / self._sample_rate,
            warmup_samples=warmup_samples,
            target_samples=target_samples,
        )
        owner._resolve_once(
            ResolvedCaptureBounds(
                attach_sample=self._write_pos,
                attach_monotonic=float(self._write_pos) / self._sample_rate,
                warmup_samples=warmup_samples,
                target_samples=target_samples,
            )
        )
        self._owners.add(owner)
        if self._attach_emit_value is not None:
            emit_value = self._attach_emit_value
            self._attach_emit_value = None
            self.emit_chunk(emit_value)
        return owner

    def release_capture(self, owner):
        self._owners.discard(owner)
        owner._mark_released()

    def get_capture_buffer(self, owner, *, retained_frames):
        bounds = owner.resolution
        start = bounds.retained_start
        end = min(start + retained_frames, self._write_pos)
        return self._buffer[start:end].copy()

    def isRunning(self):
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def stop_if_no_capture_owners(self):
        if self._owners:
            return False
        self.stop()
        return True

    def wait(self, timeout):
        return True

    def emit_chunk(self, fill_value, *, new_samples=None):
        frame_count = self._chunk_samples if new_samples is None else int(new_samples)
        data = np.full(
            (frame_count, self._n_channels),
            fill_value,
            dtype=np.float32,
        )
        sample_start = self._write_pos
        self._buffer = np.concatenate((self._buffer, data), axis=0)
        self._write_pos += frame_count
        self.captured_block_ready.emit(
            CapturedAudioBlock(
                data=data,
                sample_start=sample_start,
                sample_end=self._write_pos,
                monotonic_start=None,
                monotonic_end=None,
            )
        )


class TestLateAttachBufferSlice:
    def test_block_internal_attach_keeps_one_tagged_timeline_end_to_end(
        self,
        monkeypatch,
        tmp_path,
    ):
        sample_rate = 100.0
        attach_sample = 2
        warmup_frames = 5
        target_frames = 10
        expected_tags = np.arange(7, 17, dtype=np.float32)

        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "100",
                "channel_count": "1",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "100",
                "channel_count": "1",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.return_value = {
            "config_id": 1,
            "config_name": "Tagged timeline",
            "record_duration": 0.1,
            "analysis_duration": 0.1,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_nfft": 16,
                    "fft_nfft": 16,
                    "stft_overlap": 0.5,
                    "spl_weighting": "Z",
                    "spl_time_constant": 0.125,
                    "spl_upper_limit": 85.0,
                    "spl_lower_limit": 40.0,
                    "fft_band_limits": [],
                    "oct_weighting": "Z",
                    "oct_band_limits": [],
                }
            ],
            "input_device_name": "Mic",
            "input_device_hostapi": "MME",
            "input_device_samplerate": 100,
            "output_device_name": "Speaker",
            "output_device_hostapi": "MME",
            "output_device_samplerate": 100,
            "monitor_channel": 0,
        }

        class HardwarelessRecorder(AudioRecorder):
            """Use the production capture path without opening PortAudio."""

            _hardwareless_running = False

            def isRunning(self):
                return self._hardwareless_running

            def start(self):
                self._hardwareless_running = True

            def stop(self):
                self._hardwareless_running = False
                super().stop()

            def wait(self, timeout):
                return True

        clock_values = iter([0.02, 0.04, 0.10, 0.12, 0.17])
        recorder = HardwarelessRecorder(
            device_name="Mic",
            hostapi="MME",
            sample_rate=sample_rate,
            n_channels=1,
            record_duration=0.1,
            monotonic_func=lambda: next(clock_values),
        )
        recorder._buffer = np.zeros((3_840_000, 1), dtype=np.float32)
        assert recorder._buffer.nbytes == 15_360_000
        recorder_factory_calls = []

        def recorder_factory(**kwargs):
            recorder_factory_calls.append(dict(kwargs))
            return recorder

        def emit_tagged_range(start, end):
            data = np.arange(start, end, dtype=np.float32).reshape(-1, 1)
            recorder._sd_callback(
                data,
                data.shape[0],
                SimpleNamespace(
                    inputBufferAdcTime=start / sample_rate,
                    currentTime=end / sample_rate,
                ),
                None,
            )
            block = recorder._queue.get_nowait()
            assert (block.sample_start, block.sample_end) == (start, end)
            assert (block.monotonic_start, block.monotonic_end) == pytest.approx(
                (start / sample_rate, end / sample_rate)
            )
            recorder._feed_captured_block(block)
            recorder._emit_captured_block(block)

        class TaggedMonitor:
            instances = []

            def __init__(self, **kwargs):
                self.error_occurred = _DummySignal()
                self.feed_blocks = []
                TaggedMonitor.instances.append(self)

            def feed(self, block):
                self.feed_blocks.append(np.asarray(block).copy())

            def start(self):
                return None

            def stop(self):
                return None

            def wait(self, timeout):
                return True

        class TaggedDispatcher:
            def __init__(self):
                self.spans = []
                self.blocks = []

            def submit(
                self,
                chunk,
                *,
                elapsed_time,
                frame_run_id,
                live_loudness_channel_index,
                sample_start,
                sample_end,
            ):
                self.spans.append((sample_start, sample_end))
                self.blocks.append(np.asarray(chunk).copy())
                return True

            def stop(self):
                return None

        saved_paths = []

        def setup_tagged_pipeline(session):
            session._pipelines = []
            session._tagged_dispatcher = TaggedDispatcher()
            session._live_display_dispatcher = session._tagged_dispatcher

        def save_tagged_wav(session, buffer, output_sample_rate):
            path = tmp_path / "tagged.wav"
            wavfile.write(path, int(output_sample_rate), np.asarray(buffer))
            session._audio_file_path = str(path)
            session._audio_data_id = "tagged-audio"
            saved_paths.append(path)
            return str(path)

        def judge_tagged_buffer(session):
            session._audio_file_path = session._save_wav(
                session._buffer,
                session._sample_rate,
            )
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.1),
            )

        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            recorder_factory,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioMonitor",
            TaggedMonitor,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_session.TestSession._setup_pipelines",
            setup_tagged_pipeline,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_session.TestSession._save_wav",
            save_tagged_wav,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_session.TestSession._run_judgment",
            judge_tagged_buffer,
        )

        mgr = TestManager(bus, services)
        assert mgr.start(
            {
                "config_id": 1,
                "sn": "TAGGED",
                "factory_run_id": "run-1",
            }
        ) is True
        session = mgr._sessions[1]
        owner = session.capture_attachment
        assert isinstance(owner, RecorderAttachment)
        assert owner.resolution is None
        assert recorder_factory_calls == [
            {
                "device_name": "Mic",
                "hostapi": "MME",
                "sample_rate": 100.0,
                "n_channels": 1,
                "record_duration": 0.1,
            }
        ]
        freeze_capture = MagicMock(wraps=session.freeze_capture)
        get_capture_buffer = MagicMock(wraps=recorder.get_capture_buffer)
        monkeypatch.setattr(session, "freeze_capture", freeze_capture)
        monkeypatch.setattr(recorder, "get_capture_buffer", get_capture_buffer)

        emit_tagged_range(0, 4)
        assert owner.resolution is not None
        assert owner.resolution.attach_sample == attach_sample
        assert owner.resolution.retained_end == (
            attach_sample + warmup_frames + target_frames
        )
        assert recorder._buffer.shape[0] >= owner.resolution.retained_end
        emit_tagged_range(4, 10)
        emit_tagged_range(10, 12)

        assert recorder._write_pos == attach_sample + target_frames
        assert recorder._capture_sample_cursor == attach_sample + target_frames
        assert recorder._capture_complete() is False
        assert session.captured_frame_count == 5
        assert 1 in mgr._sessions
        assert 1 not in mgr._completed_sessions

        emit_tagged_range(12, 17)

        completed = mgr._completed_sessions[1]
        assert recorder._write_pos == attach_sample + warmup_frames + target_frames
        assert recorder._capture_sample_cursor == (
            attach_sample + warmup_frames + target_frames
        )
        assert completed.captured_frame_count == target_frames
        freeze_capture.assert_called_once_with()
        get_capture_buffer.assert_called_once_with(
            owner,
            retained_frames=target_frames,
        )
        np.testing.assert_array_equal(completed._buffer[:, 0], expected_tags)
        assert completed._tagged_dispatcher.spans[0] == (0, 3)
        np.testing.assert_array_equal(
            np.concatenate(completed._tagged_dispatcher.blocks)[:, 0],
            expected_tags,
        )
        np.testing.assert_array_equal(
            np.concatenate(TaggedMonitor.instances[0].feed_blocks)[:, 0],
            expected_tags,
        )

        assert saved_paths == [tmp_path / "tagged.wav"]
        saved_rate, saved_data = wavfile.read(saved_paths[0])
        assert saved_rate == 100
        assert saved_data.shape[0] == target_frames
        np.testing.assert_array_equal(np.asarray(saved_data).reshape(-1), expected_tags)

        review = mgr._make_factory_review_payload(1, completed)
        assert review.captured_duration_sec == pytest.approx(0.1)

        class ReplayPlayer:
            def __init__(self):
                self.segment = None

            def play(self, segment, *args, **kwargs):
                self.segment = np.asarray(segment).copy()
                return True

            def stop(self, wait=True):
                return None

        player = ReplayPlayer()
        mgr._replay_player = player
        assert mgr.replay_selection(1, None, 0, 0.0, 0.03) is True
        np.testing.assert_array_equal(player.segment, expected_tags[:3])

        monitor = TaggedMonitor.instances[0]
        recorder_ref = weakref.ref(recorder)
        monitor_ref = weakref.ref(monitor)
        TaggedMonitor.instances.clear()
        monkeypatch.undo()
        del emit_tagged_range, get_capture_buffer, recorder_factory
        del recorder, monitor
        gc.collect()

        assert mgr._completed_sessions[1] is completed
        assert recorder_ref() is None
        assert monitor_ref() is None

    def test_on_session_done_uses_recorder_owned_post_attach_buffer_slice(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session.stop_live_display = MagicMock()
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
        )
        attachment = _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        _complete_current_session(mgr, 1)

        rec.get_capture_buffer.assert_called_once_with(
            attachment,
            retained_frames=10,
        )
        rec.release_capture.assert_called_once_with(attachment)
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        session._run_judgment.assert_called_once_with()
        session.stop_live_display.assert_called_once()

    def test_completion_captures_before_reentrant_stop_from_analyzing_notification(
        self,
        monkeypatch,
    ):
        post_attach = np.arange(20, dtype=np.float32).reshape(20, 1)
        bus, _, mgr, session, rec, _, _ = _make_running_factory_manager(
            monkeypatch,
            partial_buf=post_attach,
            sample_rate=100.0,
        )
        attachment = session.capture_attachment
        original_get_capture_buffer = rec.get_capture_buffer
        observed = {}

        def get_unless_released(owner, *, retained_frames):
            if owner in rec.release_capture_calls:
                return np.empty((0, rec._n_channels), dtype=np.float32)
            return original_get_capture_buffer(
                owner,
                retained_frames=retained_frames,
            )

        rec.get_capture_buffer = get_unless_released
        session._run_judgment = MagicMock(
            side_effect=AssertionError("reentrant stop must suppress analysis")
        )

        def stop_from_analyzing(_payload):
            observed["buffer"] = session._buffer.copy()
            observed["get_calls"] = list(rec.get_capture_buffer_calls)
            observed["released"] = list(rec.release_capture_calls)
            observed["stop_result"] = mgr.stop_active_run(config_id=1)

        bus.factory_runtime_analyzing_entered.emit.side_effect = stop_from_analyzing

        _complete_current_session(mgr, 1)

        np.testing.assert_array_equal(observed["buffer"], post_attach)
        assert observed["get_calls"] == [(attachment, post_attach.shape[0])]
        assert observed["released"] == [attachment]
        assert observed["stop_result"] is True
        assert rec.get_capture_buffer_calls == [(attachment, post_attach.shape[0])]
        assert rec.release_capture_calls == [attachment]
        session._run_judgment.assert_not_called()

    def test_completion_final_commit_preserves_replacement_started_by_idle_callback(
        self,
        monkeypatch,
    ):
        bus, _, mgr, old_session, _old_recorder, _old_monitor, _ = (
            _make_running_factory_manager(
                monkeypatch,
                partial_buf=np.arange(20, dtype=np.float32).reshape(20, 1),
                sample_rate=100.0,
            )
        )
        old_session._run_judgment = MagicMock(
            return_value=JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 0.1),
            )
        )
        replacement = {}

        def start_replacement_on_idle(message):
            if message != "Idle" or replacement:
                return
            replacement["accepted"] = mgr.start({"config_id": 1, "sn": "SN2"})
            replacement["session"] = mgr._sessions.get(1)
            replacement["recorder"] = mgr._recorders.get(1)
            replacement["monitor"] = mgr._monitors.get(1)
            replacement["route"] = mgr._session_error_routes.get(1)

        bus.status.emit.side_effect = start_replacement_on_idle

        try:
            _complete_current_session(mgr, 1)

            assert replacement["accepted"] is True
            assert mgr._sessions.get(1) is replacement["session"]
            assert mgr._recorders.get(1) is replacement["recorder"]
            assert mgr._monitors.get(1) is replacement["monitor"]
            assert mgr._session_error_routes.get(1) is replacement["route"]
            assert (
                mgr._session_generations.get(1)
                is replacement["route"].generation
            )
            assert mgr._completed_sessions.get(1) is not replacement["session"]
            assert replacement["monitor"].stop_calls == 0
            assert mgr.is_running(config_id=1) is True
        finally:
            if mgr.is_running(config_id=1):
                mgr.stop(config_id=1, silent=True)

    def test_duplicate_completion_callers_share_one_capture_finalization(
        self,
        monkeypatch,
    ):
        post_attach = np.arange(20, dtype=np.float32).reshape(20, 1)
        _bus, _, mgr, session, rec, _, _ = _make_running_factory_manager(
            monkeypatch,
            partial_buf=post_attach,
            sample_rate=100.0,
        )
        attachment = session.capture_attachment
        session.freeze_capture = MagicMock(return_value=post_attach.shape[0])
        mgr._emit_auto_analysis_review = MagicMock(return_value=False)
        get_entered = Event()
        allow_get = Event()
        second_finalizer_entered = Event()
        invocation_lock = Lock()
        helper_invocations = 0
        original_get_capture_buffer = rec.get_capture_buffer
        original_finalize = mgr._freeze_and_capture_session_buffer

        def blocking_get(owner, *, retained_frames):
            get_entered.set()
            assert allow_get.wait(timeout=2)
            return original_get_capture_buffer(
                owner,
                retained_frames=retained_frames,
            )

        def observed_finalize(running_session, recorder):
            nonlocal helper_invocations
            with invocation_lock:
                helper_invocations += 1
                if helper_invocations == 2:
                    second_finalizer_entered.set()
            return original_finalize(running_session, recorder)

        rec.get_capture_buffer = blocking_get
        mgr._freeze_and_capture_session_buffer = observed_finalize
        workers = [
            Thread(target=_complete_current_session, args=(mgr, 1))
            for _ in range(2)
        ]

        workers[0].start()
        assert get_entered.wait(timeout=2)
        workers[1].start()
        assert second_finalizer_entered.wait(timeout=2)
        allow_get.set()

        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()

        assert helper_invocations == 2
        session.freeze_capture.assert_called_once_with()
        assert rec.get_capture_buffer_calls == [(attachment, post_attach.shape[0])]
        assert rec.release_capture_calls == [attachment]

    def test_on_session_done_projects_selected_channels_before_final_judgment(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.column_stack(
            [np.full(10, channel, dtype=np.float32) for channel in range(6)]
        )
        rec = MagicMock()
        rec._sample_rate = 100.0
        observed = {}

        class Session:
            config_id = 1

            def __init__(self):
                self._buffer = None
                self._sample_rate = None
                self._config = {
                    "analysis_duration": 0.1,
                    "analysis_window_offset_ms": 0,
                    "selected_channels": [3],
                    "channels": [{"channel_index": 3}],
                }
                self._workorder = {}
                self._audio_file_path = "captured.wav"
                self.last_ai_review = None
                self._source_channels = ()
                self._real_to_logical_channel = {}

            def capture_recorder_buffer(self, hardware_buffer, sample_rate):
                view = project_selected_channels(
                    hardware_buffer,
                    self._config["selected_channels"],
                )
                self._buffer = view.buffer
                self._source_channels = view.source_channels
                self._real_to_logical_channel = view.real_to_logical
                self._sample_rate = sample_rate
                return self._buffer

            def _run_judgment(self):
                observed["buffer"] = self._buffer.copy()
                observed["map"] = dict(self._real_to_logical_channel)
                return JudgmentResult(
                    overall="合格",
                    limit_pass=True,
                    ai_pass=None,
                    ai_score=None,
                    analysis_window=(0.0, 0.1),
                )

        session = Session()
        attachment = _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        _complete_current_session(mgr, 1)

        rec.get_capture_buffer.assert_called_once_with(
            attachment,
            retained_frames=10,
        )
        rec.release_capture.assert_called_once_with(attachment)
        np.testing.assert_array_equal(session._buffer, post_attach[:, [3]])
        np.testing.assert_array_equal(observed["buffer"], post_attach[:, [3]])
        assert observed["map"] == {3: 0}

    def test_on_session_done_projection_failure_emits_no_result_and_cleans_runtime(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.column_stack(
            [np.full(10, channel, dtype=np.float32) for channel in range(2)]
        )
        rec = MagicMock()
        rec._sample_rate = 100.0
        rec.stop_if_no_capture_owners.return_value = True
        mon = MagicMock()
        expected_error = "selected channel 3 is outside hardware channel range 0..1"

        class Session:
            config_id = 1

            def __init__(self):
                self._buffer = None
                self._sample_rate = None
                self._config = {
                    "analysis_duration": 0.1,
                    "analysis_window_offset_ms": 0,
                    "selected_channels": [3],
                    "channels": [{"channel_index": 3}],
                }
                self._workorder = {
                    "trigger_source": "scan",
                    "factory_run_id": "run-1",
                }
                self._audio_file_path = None
                self._audio_data_id = None
                self.last_ai_review = None
                self._source_channels = ()
                self._real_to_logical_channel = {}
                self._run_judgment = MagicMock(
                    side_effect=AssertionError("projection failure should stop analysis")
                )

            def capture_recorder_buffer(self, hardware_buffer, sample_rate):
                view = project_selected_channels(
                    hardware_buffer,
                    self._config["selected_channels"],
                )
                self._buffer = view.buffer
                self._source_channels = view.source_channels
                self._real_to_logical_channel = view.real_to_logical
                self._sample_rate = sample_rate
                return self._buffer

        session = Session()
        attachment = _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._monitors[1] = mon

        _complete_current_session(mgr, 1)

        rec.get_capture_buffer.assert_called_once_with(
            attachment,
            retained_frames=10,
        )
        rec.release_capture.assert_called_once_with(attachment)
        session._run_judgment.assert_not_called()
        assert session._buffer is None
        bus.test_finished.emit.assert_called_once_with(
            1,
            _expected_no_result_payload(expected_error),
        )
        bus.result_changed.emit.assert_called_once_with(
            _expected_no_result_payload(expected_error)
        )
        bus.factory_runtime_auto_analysis_no_result.emit.assert_called_once()
        review_payload = bus.factory_runtime_auto_analysis_no_result.emit.call_args.args[0]
        assert review_payload.run_id == "run-1"
        assert review_payload.trigger_source == "scan"
        assert review_payload.no_result is True
        assert review_payload.error_message == expected_error
        assert review_payload.judgment_payload is None
        assert review_payload.chart_payload is None
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._analyzing_sessions == set()
        assert mgr._accepted_factory_stops == set()
        assert mgr._completed_sessions[1] is session
        mon.stop.assert_called_once_with()
        mon.wait.assert_called_once_with(1000)
        rec.stop_if_no_capture_owners.assert_called_once_with()
        rec.stop.assert_not_called()
        rec.wait.assert_called_once_with(3000)

    def test_on_session_done_projection_failure_attempts_all_cleanup_when_cleanup_raises(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.column_stack(
            [np.full(10, channel, dtype=np.float32) for channel in range(2)]
        )
        rec = MagicMock()
        rec._sample_rate = 100.0
        rec.stop_if_no_capture_owners.side_effect = RuntimeError(
            "recorder stop exploded"
        )
        rec.wait.side_effect = RuntimeError("recorder wait exploded")
        mon = MagicMock()
        mon.stop.side_effect = RuntimeError("monitor stop exploded")
        mon.wait.side_effect = RuntimeError("monitor wait exploded")
        expected_error = "selected channel 3 is outside hardware channel range 0..1"

        class Session:
            config_id = 1

            def __init__(self):
                self._buffer = None
                self._sample_rate = None
                self._config = {
                    "analysis_duration": 0.1,
                    "analysis_window_offset_ms": 0,
                    "selected_channels": [3],
                    "channels": [{"channel_index": 3}],
                }
                self._workorder = {
                    "trigger_source": "scan",
                    "factory_run_id": "run-1",
                }
                self._audio_file_path = None
                self._audio_data_id = None
                self.last_ai_review = None
                self._source_channels = ()
                self._real_to_logical_channel = {}
                self._run_judgment = MagicMock(
                    side_effect=AssertionError("projection failure should stop analysis")
                )

            def capture_recorder_buffer(self, hardware_buffer, sample_rate):
                view = project_selected_channels(
                    hardware_buffer,
                    self._config["selected_channels"],
                )
                self._buffer = view.buffer
                self._source_channels = view.source_channels
                self._real_to_logical_channel = view.real_to_logical
                self._sample_rate = sample_rate
                return self._buffer

        session = Session()
        attachment = _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._monitors[1] = mon

        _complete_current_session(mgr, 1)

        rec.get_capture_buffer.assert_called_once_with(
            attachment,
            retained_frames=10,
        )
        rec.release_capture.assert_called_once_with(attachment)
        session._run_judgment.assert_not_called()
        assert session._buffer is None
        bus.test_finished.emit.assert_called_once_with(
            1,
            _expected_no_result_payload(expected_error),
        )
        bus.result_changed.emit.assert_called_once_with(
            _expected_no_result_payload(expected_error)
        )
        bus.factory_runtime_auto_analysis_no_result.emit.assert_called_once()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_called_once()
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._analyzing_sessions == set()
        assert mgr._completed_sessions[1] is session
        mon.stop.assert_called_once_with()
        mon.wait.assert_called_once_with(1000)
        rec.stop_if_no_capture_owners.assert_called_once_with()
        rec.stop.assert_not_called()
        rec.wait.assert_not_called()
        log_lines = [call.args[0] for call in bus.log.emit.call_args_list]
        assert any(expected_error in line for line in log_lines)
        assert any("monitor stop exploded" in line for line in log_lines)
        assert any("monitor wait exploded" in line for line in log_lines)
        assert any("recorder stop exploded" in line for line in log_lines)
        assert not any("recorder wait exploded" in line for line in log_lines)

    def test_on_session_done_emits_factory_run_id_on_runtime_payloads(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.93,
            analysis_window=(1.0, 3.0),
        )
        _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        _complete_current_session(mgr, 1)

        phase_payload = bus.factory_runtime_analyzing_entered.emit.call_args.args[0]
        review_payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert phase_payload.config_id == 1
        assert phase_payload.run_id == "run-1"
        assert review_payload.displayed_config_id == 1
        assert review_payload.run_id == "run-1"
        assert review_payload.trigger_source == "scan"
        assert review_payload.captured_duration_sec == 5.0
        assert review_payload.default_window.start_sec == 1.0
        assert review_payload.default_window.end_sec == 3.0
        assert review_payload.fallback_window is None
        assert review_payload.manual_reanalysis_allowed is True
        assert review_payload.no_result is False
        assert review_payload.error_message is None
        assert review_payload.judgment_payload == {
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": True,
            "ai_score": 0.93,
            "channel_results": [],
            "analysis_window": (1.0, 3.0),
        }

    @pytest.mark.parametrize(
        ("judgment_outcome", "review_signal_name"),
        [
            ("success", "factory_runtime_auto_analysis_succeeded"),
            ("no_result", "factory_runtime_auto_analysis_no_result"),
            ("error", "factory_runtime_auto_analysis_error"),
        ],
    )
    def test_on_session_done_emits_cleanup_settled_after_auto_analysis_review_signal(
        self,
        judgment_outcome,
        review_signal_name,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        session._workorder = {
            "trigger_source": "manual",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        if judgment_outcome == "success":
            session._run_judgment.return_value = JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.93,
                analysis_window=(1.0, 3.0),
            )
        elif judgment_outcome == "no_result":
            session._run_judgment.side_effect = ValueError("analysis window not fully captured")
        else:
            session._run_judgment.side_effect = RuntimeError("analysis crashed")
        _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        emission_order: list[tuple[str, bool, bool, object]] = []

        def _capture_emission(label, payload):
            emission_order.append((label, bool(mgr._sessions), mgr.is_running(), payload))

        getattr(bus, review_signal_name).emit.side_effect = (
            lambda payload: _capture_emission("review", payload)
        )
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.side_effect = (
            lambda payload: _capture_emission("cleanup", payload)
        )

        _complete_current_session(mgr, 1)

        assert [entry[0] for entry in emission_order] == ["review", "cleanup"]
        assert emission_order[0][1:3] == (True, True)
        assert emission_order[1][1:3] == (False, False)
        cleanup_payload = emission_order[1][3]
        assert cleanup_payload.config_id == 1
        assert cleanup_payload.run_id == "run-1"
        assert mgr._completed_sessions[1] is session

    @pytest.mark.parametrize(
        ("judgment_outcome", "review_signal_name"),
        [
            ("success", "factory_runtime_auto_analysis_succeeded"),
            ("no_result", "factory_runtime_auto_analysis_no_result"),
            ("error", "factory_runtime_auto_analysis_error"),
        ],
    )
    def test_stop_active_run_eligible_auto_analysis_emits_cleanup_settled_after_review_signal(
        self,
        monkeypatch,
        judgment_outcome,
        review_signal_name,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        if judgment_outcome == "success":
            session._run_judgment = MagicMock(
                return_value=JudgmentResult(
                    overall="合格",
                    limit_pass=True,
                    ai_pass=True,
                    ai_score=0.93,
                    analysis_window=(1.0, 3.0),
                )
            )
        elif judgment_outcome == "no_result":
            session._run_judgment = MagicMock(
                side_effect=ValueError("analysis window not fully captured")
            )
        else:
            session._run_judgment = MagicMock(side_effect=RuntimeError("analysis crashed"))

        emission_order: list[tuple[str, bool, bool, object]] = []

        def _capture_emission(label, payload):
            emission_order.append((label, bool(mgr._sessions), mgr.is_running(), payload))

        getattr(bus, review_signal_name).emit.side_effect = (
            lambda payload: _capture_emission("review", payload)
        )
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.side_effect = (
            lambda payload: _capture_emission("cleanup", payload)
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        assert [entry[0] for entry in emission_order] == ["review", "cleanup"]
        assert emission_order[0][1:3] == (True, True)
        assert emission_order[1][1:3] == (False, False)
        cleanup_payload = emission_order[1][3]
        assert cleanup_payload.config_id == 1
        assert cleanup_payload.run_id == session._workorder["factory_run_id"]
        bus.factory_runtime_stopped.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr._completed_sessions[1] is session

    def test_on_session_done_required_window_failure_emits_no_result_payload_and_preserves_completed_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._audio_file_path = None
        session._audio_data_id = None
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
        }

        def raise_required_window_failure():
            session._audio_file_path = "captured.wav"
            session._audio_data_id = "audio-123"
            raise ValueError("analysis window not fully captured")

        session._run_judgment.side_effect = raise_required_window_failure
        attachment = _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        _complete_current_session(mgr, 1)

        payload = _expected_no_result_payload("analysis window not fully captured")
        rec.get_capture_buffer.assert_called_once_with(
            attachment,
            retained_frames=10,
        )
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        assert session._audio_file_path == "captured.wav"
        assert session._audio_data_id == "audio-123"
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        phase_payload = bus.factory_runtime_analyzing_entered.emit.call_args.args[0]
        factory_payload = bus.factory_runtime_auto_analysis_no_result.emit.call_args.args[0]
        assert phase_payload.run_id == "run-1"
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis window not fully captured"
        assert factory_payload.judgment_payload is None
        assert mgr._completed_sessions[1] is session

    def test_on_session_done_unexpected_analysis_failure_emits_no_result_payload_and_preserves_completed_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._audio_file_path = None
        session._audio_data_id = None
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
        }

        def raise_unexpected_failure():
            session._audio_file_path = "captured.wav"
            session._audio_data_id = "audio-123"
            raise RuntimeError("analysis crashed")

        session._run_judgment.side_effect = raise_unexpected_failure
        attachment = _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        _complete_current_session(mgr, 1)

        payload = _expected_no_result_payload("analysis crashed")
        rec.get_capture_buffer.assert_called_once_with(
            attachment,
            retained_frames=10,
        )
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        assert session._audio_file_path == "captured.wav"
        assert session._audio_data_id == "audio-123"
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        phase_payload = bus.factory_runtime_analyzing_entered.emit.call_args.args[0]
        factory_payload = bus.factory_runtime_auto_analysis_error.emit.call_args.args[0]
        assert phase_payload.run_id == "run-1"
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis crashed"
        assert factory_payload.judgment_payload is None
        assert mgr._completed_sessions[1] is session

    def test_stop_during_inflight_analysis_suppresses_completion_signals_and_discards_completed_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }

        def stop_then_return_result():
            mgr.stop(config_id=1)
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.93,
            )

        session._run_judgment.side_effect = stop_then_return_result
        _install_completed_capture(session, rec, post_attach)
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        _complete_current_session(mgr, 1)

        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        bus.test_finished.emit.assert_not_called()
        bus.result_changed.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

    def test_stop_active_run_during_analyzing_emits_factory_runtime_stopped_and_suppresses_late_completion(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        entered_analysis = Event()
        release_analysis = Event()

        def _blocking_judgment():
            session._audio_file_path = "captured.wav"
            entered_analysis.set()
            assert release_analysis.wait(timeout=1)
            raise RuntimeError("late analysis should be suppressed")

        session._run_judgment = _blocking_judgment
        worker = Thread(target=_complete_current_session, args=(mgr, 1))
        worker.start()
        assert entered_analysis.wait(timeout=1)

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.factory_runtime_aborted.emit.assert_not_called()

        release_analysis.set()
        worker.join(timeout=1)

        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

    def test_stop_active_run_ineligible_stop_window_suppresses_late_completion_callback(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(200, dtype=np.float32).reshape(200, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=False,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        stop_window_entered = Event()
        release_stop_window = Event()
        stop_results: list[bool] = []
        original_capture = mgr._capture_partial_stop_buffer
        session._run_judgment = MagicMock(
            side_effect=AssertionError("late completion should be suppressed")
        )

        def _blocking_capture(config_id, running_session, recorder):
            stop_window_entered.set()
            assert release_stop_window.wait(timeout=1)
            return original_capture(config_id, running_session, recorder)

        monkeypatch.setattr(mgr, "_capture_partial_stop_buffer", _blocking_capture)

        worker = Thread(target=lambda: stop_results.append(mgr.stop_active_run(config_id=1)))
        worker.start()
        assert stop_window_entered.wait(timeout=1)

        _complete_current_session(mgr, 1)

        release_stop_window.set()
        worker.join(timeout=1)

        assert stop_results == [True]
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.factory_runtime_analyzing_entered.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        bus.test_finished.emit.assert_not_called()
        bus.result_changed.emit.assert_not_called()
        session._run_judgment.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

    def test_on_error_emits_factory_runtime_aborted_for_active_factory_run(
        self,
        monkeypatch,
    ):
        (
            bus,
            _services,
            mgr,
            session,
            rec,
            _monitor,
            _partial_buf,
        ) = _make_running_factory_manager(
            monkeypatch,
            trigger_source="plc",
        )
        run_id = session._workorder["factory_run_id"]

        state_at_abort = {}

        def capture_state_at_abort(payload):
            state_at_abort["payload"] = payload
            state_at_abort["running"] = mgr.is_running(config_id=1)
            state_at_abort["sessions"] = dict(mgr._sessions)
            state_at_abort["recorders"] = dict(mgr._recorders)

        bus.factory_runtime_aborted.emit.side_effect = capture_state_at_abort

        rec.error_occurred.emit("monitor disconnected")
        app = QCoreApplication.instance()
        if app is None:
            app = QCoreApplication([])
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

        abort_payload = state_at_abort["payload"]
        assert state_at_abort["running"] is False
        assert state_at_abort["sessions"] == {}
        assert state_at_abort["recorders"] == {}
        assert abort_payload.config_id == 1
        assert abort_payload.run_id == run_id
        assert abort_payload.trigger_source == "plc"
        assert abort_payload.error_message == "monitor disconnected"
        assert mgr.is_running(config_id=1) is False

    def test_late_attached_session_processes_attach_window_chunk_consistently_with_final_buffer(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "100",
                "channel_count": "1",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "100",
                "channel_count": "1",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 1.0,
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
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Late",
                "record_duration": 0.1,
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
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def __init__(self, live_values):
                self._live_values = live_values

            def process_chunk(self, mono, elapsed, compute_loudness=True):
                self._live_values.append(float(mono[0]))
                return {"stft_frames": [], "stft": None, "spl": None, "octave": None, "fft": None}

        class FakeRecorder(_ExactStreamingRecorder):
            pass

        def fake_setup_pipelines(self):
            self._live_chunk_values = []
            self._pipelines = [FakePipeline(self._live_chunk_values)]
            self._live_display_dispatcher = _InlineDisplayDispatcher(self._pipelines)

        def fake_run_judgment(self):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
            )

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._setup_pipelines", fake_setup_pipelines)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._run_judgment", fake_run_judgment)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]
        shared_rec.emit_chunk(0.0)
        shared_rec.emit_chunk(1.0)
        shared_rec.emit_chunk(2.0)
        shared_rec.emit_chunk(3.0)

        shared_rec._attach_emit_value = 4.0
        mgr.start({"config_id": 2, "sn": "SN2"})
        shared_rec.emit_chunk(5.0)
        shared_rec.emit_chunk(6.0)

        late_session = mgr._completed_sessions[2]
        assert late_session._live_chunk_values == [5.0, 6.0]
        expected = np.concatenate(
            [
                np.full((5, 1), 5.0, dtype=np.float32),
                np.full((5, 1), 6.0, dtype=np.float32),
            ],
            axis=0,
        )
        np.testing.assert_array_equal(late_session._buffer, expected)

    def test_late_attached_session_finishes_after_current_chunk_and_judges_exact_post_attach_audio(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "100",
                "channel_count": "1",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "100",
                "channel_count": "1",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 1.0,
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
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Late",
                "record_duration": 0.1,
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
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 100,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def process_chunk(self, mono, elapsed, compute_loudness=True):
                return {"stft_frames": [], "stft": None, "spl": None, "octave": None, "fft": None}

        class FakeRecorder(_ExactStreamingRecorder):
            pass

        def fake_setup_pipelines(self):
            self._pipelines = [FakePipeline()]
            self._live_display_dispatcher = _InlineDisplayDispatcher(self._pipelines)

        def fake_run_judgment(self):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
            )

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._setup_pipelines", fake_setup_pipelines)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._run_judgment", fake_run_judgment)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]
        shared_rec.emit_chunk(0.0)
        shared_rec.emit_chunk(1.0)
        shared_rec.emit_chunk(2.0)
        shared_rec.emit_chunk(3.0)

        mgr.start({"config_id": 2, "sn": "SN2"})
        shared_rec.emit_chunk(4.0)
        shared_rec.emit_chunk(5.0)
        shared_rec.emit_chunk(6.0)

        late_session = mgr._completed_sessions[2]
        expected = np.concatenate(
            [
                np.full((5, 1), 5.0, dtype=np.float32),
                np.full((5, 1), 6.0, dtype=np.float32),
            ],
            axis=0,
        )
        np.testing.assert_array_equal(late_session._buffer, expected)

    def test_late_attached_session_clamps_tail_elapsed_to_partial_final_write_boundary(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        services.hss_service.load_io_devices.return_value = {
            "input": {
                "id": 0,
                "name": "Mic",
                "type": "MME",
                "samplerate": "22050",
                "channel_count": "1",
            },
            "output": {
                "id": 1,
                "name": "Speaker",
                "type": "MME",
                "samplerate": "22050",
                "channel_count": "1",
            },
            "mic_cal": {},
        }
        services.config_service.load_config.side_effect = [
            {
                "config_id": 1,
                "config_name": "First",
                "record_duration": 1.0,
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
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 22050,
                "output_device_name": None,
            },
            {
                "config_id": 2,
                "config_name": "Late",
                "record_duration": 0.1,
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
                "input_device_name": "Mic",
                "input_device_hostapi": "MME",
                "input_device_samplerate": 22050,
                "output_device_name": None,
            },
        ]

        class FakePipeline:
            channel_index = 0

            def __init__(self, elapsed_values):
                self._elapsed_values = elapsed_values

            def process_chunk(self, mono, elapsed, compute_loudness=True):
                self._elapsed_values.append(elapsed)
                return {"stft_frames": [], "stft": None, "spl": None, "octave": None, "fft": None}

        class FakeRecorder(_ExactStreamingRecorder):
            pass

        def fake_setup_pipelines(self):
            self._elapsed_values = []
            self._pipelines = [FakePipeline(self._elapsed_values)]
            self._live_display_dispatcher = _InlineDisplayDispatcher(self._pipelines)

        def fake_run_judgment(self):
            return JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
            )

        monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", FakeRecorder)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._setup_pipelines", fake_setup_pipelines)
        monkeypatch.setattr("app.service.analysis.test_session.TestSession._run_judgment", fake_run_judgment)

        mgr = TestManager(bus, services)
        mgr.start({"config_id": 1, "sn": "SN1"})
        shared_rec = mgr._recorders[1]
        shared_rec.emit_chunk(0.0)
        shared_rec.emit_chunk(1.0)
        shared_rec.emit_chunk(2.0)
        shared_rec.emit_chunk(3.0)

        mgr.start({"config_id": 2, "sn": "SN2"})
        shared_rec.emit_chunk(4.0)
        shared_rec.emit_chunk(5.0)

        assert 2 not in mgr._completed_sessions

        shared_rec.emit_chunk(6.0)

        assert 2 not in mgr._completed_sessions

        shared_rec.emit_chunk(7.0, new_samples=2)

        late_session = mgr._completed_sessions[2]
        expected_elapsed_values = [1101 / 22050.0, 2203 / 22050.0, 2205 / 22050.0]
        progress_elapsed_values = [
            call.args[1]
            for call in bus.recording_progress.emit.call_args_list
            if call.args[0] == 2 and call.args[1] > 0
        ]
        expected_buffer = np.concatenate(
            [
                np.full((1101, 1), 5.0, dtype=np.float32),
                np.full((1102, 1), 6.0, dtype=np.float32),
                np.full((2, 1), 7.0, dtype=np.float32),
            ],
            axis=0,
        )

        assert late_session._elapsed_values == pytest.approx(expected_elapsed_values)
        assert progress_elapsed_values == pytest.approx(expected_elapsed_values)
        np.testing.assert_array_equal(late_session._buffer, expected_buffer)
