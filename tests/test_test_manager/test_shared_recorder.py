from __future__ import annotations

from unittest.mock import MagicMock

from app.service.analysis.test_manager import TestManager

from ._support import (
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
