from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

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

    def test_stop_by_config_id_emits_factory_abort_payload_and_clears_active_runtime(self, monkeypatch):
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
            """QPushButton.clicked emits bool. stop() should treat it as stop-all."""
            bus = _mock_bus()
            services = _mock_services(bus)
            mgr = TestManager(bus, services)
            mock_session = MagicMock()
            mock_session.config_id = 1
            mgr._sessions[1] = mock_session
            mgr._recorders[1] = MagicMock()
            mgr.stop(False)
            assert mgr.is_running() is False

    def test_stop_active_run_remains_cleanup_safe_when_partial_save_wav_fails(self, monkeypatch):
            save_error = RuntimeError("disk full")
            bus, _, mgr, session, recorder, monitor, partial_buf = _make_running_factory_manager(
                monkeypatch,
                save_wav_error=save_error,
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
            bus.factory_runtime_aborted.emit.assert_not_called()
            assert any(
                "ERROR saving partial recording for config=1: disk full" in call.args[0]
                for call in bus.log.emit.call_args_list
            )
            bus.status.emit.assert_any_call("Stopped")

    def test_stop_active_run_returns_false_when_factory_run_is_missing(self):
            bus = _mock_bus()
            services = _mock_services(bus)
            mgr = TestManager(bus, services)

            stopped = mgr.stop_active_run(config_id=99)

            assert stopped is False
