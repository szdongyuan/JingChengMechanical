from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.selected_channels import project_selected_channels
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

    def test_user_stop_projects_selected_channels_before_saving_partial_wav(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        hardware_buf = np.column_stack(
            [np.full(10, channel, dtype=np.float32) for channel in range(6)]
        )
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = hardware_buf
        rec._sample_rate = 100.0

        class Session:
            config_id = 1

            def __init__(self):
                self._buffer = None
                self._sample_rate = None
                self._attach_snapshot = attach_snapshot
                self._config = {
                    "selected_channels": [3],
                    "channels": [{"channel_index": 3}],
                }
                self._source_channels = ()
                self._real_to_logical_channel = {}
                self._save_wav = MagicMock(return_value="partial.wav")

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
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._stop_one(1, silent=False)

        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        np.testing.assert_array_equal(session._buffer, hardware_buf[:, [3]])
        assert session._real_to_logical_channel == {3: 0}
        saved_buf, saved_sr = session._save_wav.call_args.args
        np.testing.assert_array_equal(saved_buf, hardware_buf[:, [3]])
        assert saved_sr == 100.0

    def test_user_stop_projection_failure_logs_and_cleans_without_saving(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        hardware_buf = np.column_stack(
            [np.full(10, channel, dtype=np.float32) for channel in range(2)]
        )
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = hardware_buf
        rec._sample_rate = 100.0
        mon = MagicMock()
        expected_error = "selected channel 3 is outside hardware channel range 0..1"

        class Session:
            config_id = 1

            def __init__(self):
                self._buffer = None
                self._sample_rate = None
                self._attach_snapshot = attach_snapshot
                self._config = {
                    "selected_channels": [3],
                    "channels": [{"channel_index": 3}],
                }
                self._source_channels = ()
                self._real_to_logical_channel = {}
                self._save_wav = MagicMock(return_value="partial.wav")

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
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._monitors[1] = mon

        mgr._stop_one(1, silent=False)

        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        assert session._buffer is None
        assert session._sample_rate is None
        session._save_wav.assert_not_called()
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        mon.stop.assert_called_once_with()
        mon.wait.assert_called_once_with(1000)
        rec.stop.assert_called_once_with()
        rec.wait.assert_called_once_with(3000)
        assert any(
            f"ERROR capturing partial recording for config=1: {expected_error}" in call.args[0]
            for call in bus.log.emit.call_args_list
        )
        bus.status.emit.assert_any_call("Stopped")

    def test_user_stop_projection_failure_attempts_all_cleanup_when_cleanup_raises(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        hardware_buf = np.column_stack(
            [np.full(10, channel, dtype=np.float32) for channel in range(2)]
        )
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = hardware_buf
        rec._sample_rate = 100.0
        rec.stop.side_effect = RuntimeError("recorder stop exploded")
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
                self._attach_snapshot = attach_snapshot
                self._config = {
                    "selected_channels": [3],
                    "channels": [{"channel_index": 3}],
                }
                self._source_channels = ()
                self._real_to_logical_channel = {}
                self._save_wav = MagicMock(return_value="partial.wav")

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
        mgr._sessions[1] = session
        mgr._recorders[1] = rec
        mgr._monitors[1] = mon
        mgr._analyzing_sessions.add(1)
        mgr._accepted_factory_stops.add(1)

        mgr._stop_one(1, silent=False)

        rec.get_buffer_from.assert_called_once_with(attach_snapshot)
        assert session._buffer is None
        assert session._sample_rate is None
        session._save_wav.assert_not_called()
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._analyzing_sessions == set()
        assert mgr._accepted_factory_stops == set()
        mon.stop.assert_called_once_with()
        mon.wait.assert_called_once_with(1000)
        rec.stop.assert_called_once_with()
        rec.wait.assert_called_once_with(3000)
        log_lines = [call.args[0] for call in bus.log.emit.call_args_list]
        assert any(expected_error in line for line in log_lines)
        assert any("monitor stop exploded" in line for line in log_lines)
        assert any("monitor wait exploded" in line for line in log_lines)
        assert any("recorder stop exploded" in line for line in log_lines)
        assert any("recorder wait exploded" in line for line in log_lines)
        bus.status.emit.assert_any_call("Stopped")
