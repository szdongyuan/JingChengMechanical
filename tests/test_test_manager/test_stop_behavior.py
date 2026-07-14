from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager
from app.service.current_meter_service import CurrentSample

from ._support import (
    _expected_no_result_payload,
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


class TestTestManagerStartStop:
    def test_current_cleanup_discards_clock_edge_sample_when_capture_freezes_empty(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        mgr = TestManager(bus, _mock_services(bus))

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                pass

            def start(self):
                return True

            def stop(self):
                return None

            def samples(self):
                return [CurrentSample(0.05, 1.5, 10.05)]

        monkeypatch.setattr(
            "app.service.analysis.test_manager.CurrentMeterCollector",
            Collector,
        )
        session = SimpleNamespace(
            _config={
                "config_id": 1,
                "current_meter": {"enabled": True, "host": "127.0.0.1"},
            },
            _workorder={"factory_run_id": "empty-capture"},
            current_epoch=lambda: 10.05,
            captured_frame_count=99,
            freeze_capture=MagicMock(return_value=0),
        )
        mgr._sessions[1] = session
        generation = object()
        session._manager_generation_token = generation
        mgr._session_generations[1] = generation
        mgr._start_current_collection(1, session)

        recorder = MagicMock()
        recorder._n_channels = 1
        captured = mgr._freeze_and_capture_session_buffer(session, recorder)
        with mgr._lifecycle_lock:
            claim = mgr._detach_current_collection_locked(
                1,
                session=session,
                generation=generation,
                retain_samples=True,
            )
        cleanup = mgr._stop_current_collection_claim(claim)
        mgr._settle_current_samples_for_capture(claim, retained_frames=0)

        assert captured.shape == (0, 1)
        assert cleanup.samples == [CurrentSample(0.0, 1.5, 10.05)]
        assert 1 not in mgr._current_samples_by_config_id
        assert generation not in mgr._staged_current_samples_by_generation

    def test_stop_one_detaches_only_old_generation_current_state_before_cleanup(
        self,
        monkeypatch,
    ):
        bus, services, mgr, old_session, old_recorder, _old_monitor, _ = (
            _make_running_factory_manager(monkeypatch)
        )
        old_route = mgr._session_error_routes[1]
        old_session._config["current_meter"] = {"enabled": True}
        collector_instances = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                self._samples = [f"collector-{len(collector_instances)}"]
                collector_instances.append(self)

            def start(self):
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return list(self._samples)

        monkeypatch.setattr(
            "app.service.analysis.test_manager.CurrentMeterCollector",
            Collector,
        )
        mgr._start_current_collection(1, old_session)
        old_collector = collector_instances[0]
        mgr._current_samples_by_config_id[1] = ["old-sample"]

        old_cleanup_claimed = Event()
        allow_old_cleanup = Event()
        original_freeze_capture = old_session.freeze_capture

        def blocking_freeze_capture():
            old_cleanup_claimed.set()
            assert allow_old_cleanup.wait(1.0)
            return original_freeze_capture()

        old_session.freeze_capture = blocking_freeze_capture
        stop_result = {}

        def stop_old_generation():
            try:
                stop_result["value"] = mgr._stop_one(
                    1,
                    silent=True,
                    expected_error_route=old_route,
                )
            except BaseException as exc:  # surfaced on the asserting thread
                stop_result["error"] = exc

        cleanup_thread = Thread(target=stop_old_generation)
        cleanup_thread.start()
        assert old_cleanup_claimed.wait(1.0)

        replacement_config = dict(old_session._config)
        replacement_session = type(old_session)(
            config=replacement_config,
            bus=bus,
            ai_service=services.ai_service,
            mic_cal={},
            workorder={"factory_run_id": "replacement-run"},
            audio_dir="",
        )
        replacement_recorder = object()
        replacement_monitor = MagicMock()
        replacement_generation = object()
        replacement_session._manager_generation_token = replacement_generation
        replacement_route = type(old_route)(
            config_id=1,
            session=replacement_session,
            recorder=replacement_recorder,
            monitor=replacement_monitor,
            source_token=object(),
            generation=replacement_generation,
        )
        replacement_session._manager_error_route = replacement_route
        with mgr._lifecycle_lock:
            mgr._sessions[1] = replacement_session
            mgr._recorders[1] = replacement_recorder
            mgr._monitors[1] = replacement_monitor
            mgr._session_error_routes[1] = replacement_route
            mgr._session_generations[1] = replacement_generation

        mgr._start_current_collection(1, replacement_session)
        replacement_collector = collector_instances[1]
        replacement_samples = ["replacement-sample"]
        mgr._current_samples_by_config_id[1] = replacement_samples
        mgr._analyzing_sessions.add(1)
        mgr._accepted_factory_stops.add(1)

        allow_old_cleanup.set()
        cleanup_thread.join(2.0)

        assert cleanup_thread.is_alive() is False
        assert "error" not in stop_result
        assert stop_result["value"] is True
        assert old_collector.stop_calls == 1
        assert replacement_collector.stop_calls == 0
        assert mgr._current_collectors.get(1) is replacement_collector
        assert mgr._current_samples_by_config_id.get(1) is replacement_samples
        assert mgr._sessions.get(1) is replacement_session
        assert mgr._recorders.get(1) is replacement_recorder
        assert mgr._monitors.get(1) is replacement_monitor
        assert mgr._session_error_routes.get(1) is replacement_route
        assert mgr._session_generations.get(1) is replacement_generation
        assert 1 in mgr._analyzing_sessions
        assert 1 in mgr._accepted_factory_stops

    def test_stop_triggered_analysis_commit_preserves_idle_callback_replacement(
        self,
        monkeypatch,
    ):
        partial = np.arange(200, dtype=np.float32).reshape(200, 1)
        bus, _, mgr, old_session, _old_recorder, _old_monitor, _ = (
            _make_running_factory_manager(
                monkeypatch,
                allow_incomplete_recording_analysis=True,
                analysis_duration=1.0,
                partial_buf=partial,
                sample_rate=100.0,
            )
        )
        old_session._run_judgment = MagicMock(
            return_value=JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=None,
                ai_score=None,
                analysis_window=(0.0, 1.0),
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
            assert mgr.stop_active_run(config_id=1) is True
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
            assert mgr.is_running(config_id=1) is True
        finally:
            if mgr.is_running(config_id=1):
                mgr.stop(config_id=1, silent=True)

    def test_stop_active_run_before_warmup_saves_and_analyzes_nothing(
        self,
        monkeypatch,
    ):
        empty = np.empty((0, 1), dtype=np.float32)
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            allow_incomplete_recording_analysis=True,
            partial_buf=empty,
            sample_rate=100.0,
        )
        session._run_judgment = MagicMock()

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        assert session.saved_wavs == []
        session._run_judgment.assert_not_called()
        assert recorder.get_capture_buffer_calls == []
        assert recorder.release_capture_calls == [session.capture_attachment]
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.factory_runtime_analyzing_entered.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1

    def test_stop_all(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr._recorders[1] = MagicMock()

        mgr.stop()

        assert mgr.is_running() is False

    def test_stop_by_config_id_emits_factory_abort_payload_and_clears_active_runtime(
        self,
        monkeypatch,
    ):
        bus, _, mgr, session, recorder, monitor, partial_buf = _make_running_factory_manager(
            monkeypatch
        )
        session.stop_live_display = MagicMock()

        factory_run_id = session._workorder["factory_run_id"]

        mgr.stop(config_id=1)

        abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
        assert abort_payload.config_id == 1
        assert abort_payload.run_id == factory_run_id
        assert abort_payload.trigger_source == "manual"
        assert abort_payload.error_message is None
        assert mgr.is_running(config_id=1) is False
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._shared_recorders == {}
        assert mgr._completed_sessions == {}
        assert recorder.get_capture_buffer_calls == [
            (session.capture_attachment, partial_buf.shape[0])
        ]
        assert recorder.release_capture_calls == [session.capture_attachment]
        np.testing.assert_array_equal(session._buffer, partial_buf)
        assert session._sample_rate == recorder._sample_rate
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert monitor.stop_calls == 1
        assert monitor.wait_calls == [1000]
        session.stop_live_display.assert_called_once()

    def test_stop_with_bool_from_clicked_stops_all(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr._recorders[1] = MagicMock()

        mgr.stop(False)

        assert mgr.is_running() is False

    def test_stop_active_run_emits_factory_runtime_stopped_after_runtime_release(
        self,
        monkeypatch,
    ):
        bus, _, mgr, session, recorder, monitor, partial_buf = _make_running_factory_manager(
            monkeypatch
        )
        emission_states = []

        def _capture(payload):
            emission_states.append(
                {
                    "payload": payload,
                    "sessions": dict(mgr._sessions),
                    "recorders": dict(mgr._recorders),
                    "monitors": dict(mgr._monitors),
                    "is_running": mgr.is_running(config_id=1),
                }
            )

        bus.factory_runtime_stopped.emit.side_effect = _capture

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        assert mgr.is_running(config_id=1) is False
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._shared_recorders == {}
        assert recorder.get_capture_buffer_calls == [
            (session.capture_attachment, partial_buf.shape[0])
        ]
        assert recorder.release_capture_calls == [session.capture_attachment]
        np.testing.assert_array_equal(session._buffer, partial_buf)
        assert session._sample_rate == recorder._sample_rate
        assert len(session.saved_wavs) == 1
        saved_buf, saved_sample_rate = session.saved_wavs[0]
        np.testing.assert_array_equal(saved_buf, partial_buf)
        assert saved_sample_rate == recorder._sample_rate
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert monitor.stop_calls == 1
        assert monitor.wait_calls == [1000]
        assert len(emission_states) == 1
        stopped_payload = emission_states[0]["payload"]
        assert stopped_payload.config_id == 1
        assert stopped_payload.run_id == session._workorder["factory_run_id"]
        assert stopped_payload.trigger_source == "manual"
        assert emission_states[0]["sessions"] == {}
        assert emission_states[0]["recorders"] == {}
        assert emission_states[0]["monitors"] == {}
        assert emission_states[0]["is_running"] is False
        bus.factory_runtime_aborted.emit.assert_not_called()
        bus.status.emit.assert_any_call("Stopped")

    def test_capture_partial_stop_buffer_projects_selected_channels_before_save(
        self,
        monkeypatch,
    ):
        partial_buf = np.column_stack(
            [np.full(20, channel, dtype=np.float32) for channel in range(6)]
        )
        _, _, mgr, session, recorder, _, _ = _make_running_factory_manager(
            monkeypatch,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._config["selected_channels"] = [1, 4]
        session._config["channels"] = [{"channel_index": 1}, {"channel_index": 4}]

        capture = mgr._capture_partial_stop_buffer(1, session, recorder)

        np.testing.assert_array_equal(session._buffer, partial_buf[:, [1, 4]])
        assert session._source_channels == (1, 4)
        assert session._real_to_logical_channel == {1: 0, 4: 1}
        assert len(session.saved_wavs) == 1
        saved_buf, saved_sample_rate = session.saved_wavs[0]
        np.testing.assert_array_equal(saved_buf, partial_buf[:, [1, 4]])
        assert saved_sample_rate == 100.0
        np.testing.assert_array_equal(capture["partial_buf"], partial_buf[:, [1, 4]])

    def test_stop_active_run_projection_failure_emits_no_result_and_cleans_runtime(
        self,
        monkeypatch,
    ):
        partial_buf = np.column_stack(
            [np.full(400, channel, dtype=np.float32) for channel in range(2)]
        )
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._config["selected_channels"] = [3]
        session._config["channels"] = [{"channel_index": 3}]
        session._run_judgment = MagicMock(
            side_effect=AssertionError("projection failure should stop analysis")
        )
        expected_error = "selected channel 3 is outside hardware channel range 0..1"

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        assert recorder.get_capture_buffer_calls == [
            (session.capture_attachment, partial_buf.shape[0])
        ]
        assert recorder.release_capture_calls == [session.capture_attachment]
        session._run_judgment.assert_not_called()
        assert session._buffer is None
        assert session.saved_wavs == []
        bus.test_finished.emit.assert_called_once_with(
            1,
            _expected_no_result_payload(expected_error),
        )
        bus.result_changed.emit.assert_called_once_with(
            _expected_no_result_payload(expected_error)
        )
        bus.factory_runtime_auto_analysis_no_result.emit.assert_called_once()
        review_payload = bus.factory_runtime_auto_analysis_no_result.emit.call_args.args[0]
        assert review_payload.run_id == session._workorder["factory_run_id"]
        assert review_payload.no_result is True
        assert review_payload.error_message == expected_error
        assert review_payload.judgment_payload is None
        assert review_payload.chart_payload is None
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_called_once()
        bus.factory_runtime_stopped.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._analyzing_sessions == set()
        assert mgr._accepted_factory_stops == set()
        assert mgr._completed_sessions[1] is session
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert monitor.stop_calls == 1
        assert monitor.wait_calls == [1000]

    def test_factory_runtime_stopped_payload_preserves_original_trigger_source(
        self,
        monkeypatch,
    ):
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            trigger_source="scan",
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        stopped_payload = bus.factory_runtime_stopped.emit.call_args.args[0]
        assert stopped_payload.config_id == 1
        assert stopped_payload.run_id == session._workorder["factory_run_id"]
        assert stopped_payload.trigger_source == "scan"

    def test_stop_active_run_toggle_disabled_falls_back_to_factory_runtime_stopped_even_when_window_end_is_captured(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=False,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._run_judgment = MagicMock(
            side_effect=AssertionError("stop should not launch auto analysis when disabled")
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        stopped_payload = bus.factory_runtime_stopped.emit.call_args.args[0]
        assert stopped_payload.config_id == 1
        assert stopped_payload.run_id == session._workorder["factory_run_id"]
        assert stopped_payload.trigger_source == "manual"
        bus.factory_runtime_analyzing_entered.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr._completed_sessions == {}
        assert mgr.is_running(config_id=1) is False
        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1

    def test_stop_active_run_eligible_partial_recording_reuses_auto_analysis_success_flow(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._run_judgment = MagicMock(
            return_value=JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.93,
                analysis_window=(1.0, 3.0),
            )
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_called_once()
        bus.factory_runtime_stopped.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr.is_running(config_id=1) is False
        assert mgr._completed_sessions[1] is session
        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1

    def test_stop_active_run_below_window_end_falls_back_to_factory_runtime_stopped(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(200, dtype=np.float32).reshape(200, 1)
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._run_judgment = MagicMock(
            side_effect=AssertionError("below-threshold stop should not analyze")
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.factory_runtime_analyzing_entered.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr._completed_sessions == {}
        assert mgr.is_running(config_id=1) is False
        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1

    def test_stop_active_run_remains_cleanup_safe_when_partial_save_wav_fails(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        save_error = RuntimeError("disk full")
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            save_wav_error=save_error,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        assert mgr.is_running(config_id=1) is False
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._shared_recorders == {}
        assert recorder.get_capture_buffer_calls == [
            (session.capture_attachment, partial_buf.shape[0])
        ]
        assert recorder.release_capture_calls == [session.capture_attachment]
        np.testing.assert_array_equal(session._buffer, partial_buf)
        assert session._sample_rate == recorder._sample_rate
        assert len(session.saved_wavs) == 1
        saved_buf, saved_sample_rate = session.saved_wavs[0]
        np.testing.assert_array_equal(saved_buf, partial_buf)
        assert saved_sample_rate == recorder._sample_rate
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert monitor.stop_calls == 1
        assert monitor.wait_calls == [1000]
        stopped_payload = bus.factory_runtime_stopped.emit.call_args.args[0]
        assert stopped_payload.config_id == 1
        assert stopped_payload.run_id == session._workorder["factory_run_id"]
        assert stopped_payload.trigger_source == "manual"
        bus.factory_runtime_analyzing_entered.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert any(
            "ERROR saving partial recording for config=1: disk full" in call.args[0]
            for call in bus.log.emit.call_args_list
        )
        bus.status.emit.assert_any_call("Stopped")

    def test_stop_active_run_partial_buffer_capture_failure_falls_back_to_factory_runtime_stopped(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        recorder.get_capture_buffer = MagicMock(side_effect=RuntimeError("buffer exploded"))
        session._run_judgment = MagicMock(
            side_effect=AssertionError("capture failure should not launch analysis")
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        bus.factory_runtime_stopped.emit.assert_not_called()
        bus.factory_runtime_analyzing_entered.emit.assert_called_once()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_called_once()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr.is_running(config_id=1) is False
        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1

    def test_stop_active_run_unknown_captured_duration_falls_back_to_factory_runtime_stopped(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=0.0,
        )
        session._run_judgment = MagicMock(
            side_effect=AssertionError("unknown duration should not launch analysis")
        )

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is True
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.factory_runtime_analyzing_entered.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        assert mgr.is_running(config_id=1) is False
        assert recorder.stop_calls == 1
        assert monitor.stop_calls == 1

    def test_stop_active_run_cleanup_failure_still_emits_factory_runtime_aborted(
        self,
        monkeypatch,
    ):
        bus, _, mgr, session, recorder, _, _ = _make_running_factory_manager(monkeypatch)
        recorder.stop = MagicMock(side_effect=RuntimeError("stop exploded"))

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is False
        bus.factory_runtime_aborted.emit.assert_called_once()
        abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
        assert abort_payload.config_id == 1
        assert abort_payload.run_id == session._workorder["factory_run_id"]
        assert abort_payload.trigger_source == "manual"
        assert abort_payload.error_message == "stop exploded"
        bus.factory_runtime_stopped.emit.assert_not_called()

    def test_stop_active_run_returns_false_when_factory_run_is_missing(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)

        stopped = mgr.stop_active_run(config_id=99)

        assert stopped is False
