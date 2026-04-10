from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager

from ._support import (
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


class TestTestManagerStartStop:
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
        assert recorder.get_buffer_from_calls == [session._attach_snapshot]
        np.testing.assert_array_equal(session._buffer, partial_buf)
        assert session._sample_rate == recorder._sample_rate
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert monitor.stop_calls == 1
        assert monitor.wait_calls == [1000]

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
        assert recorder.get_buffer_from_calls == [session._attach_snapshot]
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
        assert recorder.get_buffer_from_calls == [session._attach_snapshot]
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
        recorder.get_buffer_from = MagicMock(side_effect=RuntimeError("buffer exploded"))
        session._run_judgment = MagicMock(
            side_effect=AssertionError("capture failure should not launch analysis")
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
