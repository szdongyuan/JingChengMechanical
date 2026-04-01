from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.test_manager import TestManager

from ._support import (
    _mock_bus,
    _mock_services,
)


class TestUserStopPartialSave:
    def test_user_stop_copies_buffer_and_saves(self):
        """User-initiated stop should copy recorder buffer and save partial WAV."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        partial_buf = np.random.randn(44100, 1).astype(np.float32)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = partial_buf
        rec._sample_rate = 44100.0
        session = MagicMock()
        session._buffer = None
        session.config_id = 1
        session._attach_snapshot = attach_snapshot
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._stop_one(1, silent=False)
        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, partial_buf)
        assert session._sample_rate == 44100.0
        saved_buf, saved_sr = session._save_wav.call_args.args
        np.testing.assert_array_equal(saved_buf, partial_buf)
        assert saved_sr == 44100.0

    def test_user_stop_saves_only_post_attach_buffer_slice(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(20, 30, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session._buffer = None
        session.config_id = 1
        session._attach_snapshot = attach_snapshot
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._stop_one(1, silent=False)

        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, post_attach)
        assert session._sample_rate == 100.0
        saved_buf, saved_sr = session._save_wav.call_args.args
        np.testing.assert_array_equal(saved_buf, post_attach)
        assert saved_sr == 100.0
