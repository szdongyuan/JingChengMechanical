from __future__ import annotations

from unittest.mock import MagicMock

from app.service.analysis.test_manager import TestManager

from ._support import (
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


class TestSharedRecorderTeardown:
    def test_shared_recorder_not_stopped_while_other_session_uses_it(self):
        """When two sessions share a recorder, stopping one should NOT stop the recorder."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        shared_rec = MagicMock()
        mock_s1 = MagicMock()
        mock_s1._buffer = None
        mock_s2 = MagicMock()
        mock_s2._buffer = None
        mgr._sessions = {1: mock_s1, 2: mock_s2}
        mgr._recorders = {1: shared_rec, 2: shared_rec}
        mgr._shared_recorders = {"dev|MME|44100": shared_rec}
        mgr._stop_one(1)
        shared_rec.stop.assert_not_called()
        mgr._stop_one(2)
        shared_rec.stop.assert_called_once()

    def test_failed_factory_stop_does_not_reuse_stale_shared_recorder_on_next_start(
        self,
        monkeypatch,
    ):
        bus, _, mgr, _, recorder, _, _ = _make_running_factory_manager(monkeypatch)

        def _explode_stop():
            recorder.stop_calls += 1
            recorder._running = False
            raise RuntimeError("stop exploded")

        recorder.stop = MagicMock(side_effect=_explode_stop)

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is False
        bus.factory_runtime_aborted.emit.assert_called_once()

        recorder.start = MagicMock(side_effect=AssertionError("stale recorder reused"))

        restarted = mgr.start({"config_id": 1, "sn": "SN2", "trigger_source": "manual"})

        assert restarted is True
        assert mgr._recorders[1] is not recorder
        recorder.start.assert_not_called()
        assert bus.factory_runtime_aborted.emit.call_count == 1

    def test_monitor_stop_failure_still_stops_recorder_and_discards_shared_recorder(
        self,
        monkeypatch,
    ):
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(monkeypatch)

        def _explode_monitor_stop():
            monitor.stop_calls += 1
            raise RuntimeError("monitor stop exploded")

        monitor.stop = MagicMock(side_effect=_explode_monitor_stop)

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is False
        bus.factory_runtime_aborted.emit.assert_called_once()
        abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
        assert abort_payload.config_id == session.config_id
        assert abort_payload.run_id == session._workorder["factory_run_id"]
        assert abort_payload.error_message == "monitor stop exploded"
        assert monitor.stop_calls == 1
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._shared_recorders == {}

        recorder.start = MagicMock(side_effect=AssertionError("stale recorder reused"))

        restarted = mgr.start({"config_id": 1, "sn": "SN2", "trigger_source": "manual"})

        assert restarted is True
        assert mgr._recorders[1] is not recorder
        recorder.start.assert_not_called()
