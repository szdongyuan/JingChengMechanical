from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis.test_manager import TestManager

from ._support import _mock_bus, _mock_services


class _FakeReplayPlayer:
    def __init__(self, *, play_result: bool = True):
        self.play_result = play_result
        self.play_calls = []
        self.stop_calls = []

    def play(
        self,
        segment,
        sample_rate,
        device_name,
        hostapi,
        gain_db=0.0,
        error_callback=None,
    ):
        self.play_calls.append(
            {
                "segment": np.asarray(segment).copy(),
                "sample_rate": sample_rate,
                "device_name": device_name,
                "hostapi": hostapi,
                "gain_db": gain_db,
                "error_callback": error_callback,
            }
        )
        return self.play_result

    def stop(self, wait=True):
        self.stop_calls.append(wait)


class _ReplaySession:
    def __init__(
        self,
        *,
        config_id: int = 1,
        run_id: str = "run-1",
        buffer: np.ndarray | None = None,
        sample_rate: float = 10.0,
        real_to_logical_channel: dict[int, int] | None = None,
        monitor_gain_db: float = 0.0,
        output_device_name: str | None = "Speaker",
        output_device_hostapi: str | None = "MME",
    ):
        self.config_id = config_id
        self._workorder = {"factory_run_id": run_id}
        self._buffer = (
            np.arange(20, dtype=np.float32).reshape(10, 2)
            if buffer is None
            else buffer
        )
        self._sample_rate = sample_rate
        self._real_to_logical_channel = (
            {0: 0, 1: 1}
            if real_to_logical_channel is None
            else real_to_logical_channel
        )
        self._config = {
            "config_id": config_id,
            "selected_channels": list(self._real_to_logical_channel),
            "output_device_name": output_device_name,
            "output_device_hostapi": output_device_hostapi,
            "monitor_gain_db": monitor_gain_db,
        }
        self.rejudge = MagicMock(side_effect=AssertionError("rejudge should not run"))
        self._run_judgment = MagicMock(
            side_effect=AssertionError("judgment should not run")
        )


def _manager_with_player(player: _FakeReplayPlayer | None = None):
    bus = _mock_bus()
    services = _mock_services(bus)
    mgr = TestManager(bus, services)
    fake_player = player or _FakeReplayPlayer()
    mgr._replay_player = fake_player
    return bus, services, mgr, fake_player


@pytest.mark.parametrize("session_store", ["_sessions", "_completed_sessions"])
def test_can_replay_selection_accepts_active_or_completed_session(session_store):
    bus, _, mgr, player = _manager_with_player()
    session = _ReplaySession()
    getattr(mgr, session_store)[1] = session

    assert mgr.can_replay_selection(1, "run-1", 0, 0.0, 0.3) is True
    assert mgr.can_replay_selection(1, None, 0, 0.0, 0.3) is True
    assert player.play_calls == []
    bus.test_finished.emit.assert_not_called()
    bus.result_changed.emit.assert_not_called()


def test_replay_selection_maps_display_channel_slices_segment_and_passes_gain():
    bus, services, mgr, player = _manager_with_player()
    buffer = np.column_stack(
        [
            np.arange(10, dtype=np.float32),
            np.arange(100, 110, dtype=np.float32),
        ]
    )
    session = _ReplaySession(
        buffer=buffer,
        sample_rate=10.0,
        real_to_logical_channel={3: 0, 1: 1},
        monitor_gain_db=-9.5,
    )
    mgr._completed_sessions[1] = session

    assert mgr.can_replay_selection(1, "run-1", 1, 0.2, 0.5) is True
    assert player.play_calls == []

    assert mgr.replay_selection(1, "run-1", 1, 0.2, 0.5) is True

    assert len(player.play_calls) == 1
    call = player.play_calls[0]
    np.testing.assert_array_equal(call["segment"], buffer[2:5, 1])
    assert not np.shares_memory(call["segment"], buffer)
    assert call["sample_rate"] == pytest.approx(10.0)
    assert call["device_name"] == "Speaker"
    assert call["hostapi"] == "MME"
    assert call["gain_db"] == pytest.approx(-9.5)
    assert callable(call["error_callback"])
    session.rejudge.assert_not_called()
    session._run_judgment.assert_not_called()
    bus.test_finished.emit.assert_not_called()
    bus.result_changed.emit.assert_not_called()
    services.config_service.save_config.assert_not_called()


def test_replay_from_zero_starts_at_canonical_frame_zero_without_capture_window(
    monkeypatch,
):
    _, _, mgr, player = _manager_with_player()
    canonical_tags = np.arange(7, 17, dtype=np.float32).reshape(-1, 1)
    session = _ReplaySession(
        buffer=canonical_tags,
        sample_rate=100.0,
        real_to_logical_channel={0: 0},
    )
    session._audio_file_path = "already-canonical.wav"
    mgr._completed_sessions[1] = session
    capture_window = MagicMock(
        side_effect=AssertionError("replay must not trim canonical audio again")
    )
    monkeypatch.setattr(
        "app.service.analysis.test_session.SessionCaptureWindow",
        capture_window,
    )

    assert mgr.replay_selection(1, "run-1", 0, 0.0, 0.03) is True

    np.testing.assert_array_equal(
        player.play_calls[0]["segment"],
        canonical_tags[:3, 0],
    )
    capture_window.assert_not_called()


def test_can_replay_selection_does_not_log_expected_unavailability():
    bus, _, mgr, player = _manager_with_player()

    for _ in range(3):
        assert mgr.can_replay_selection(1, "run-1", 0, 0.0, 0.2) is False

    assert player.play_calls == []
    bus.log.emit.assert_not_called()
    bus.status.emit.assert_not_called()


def test_can_replay_selection_does_not_prepare_segment(monkeypatch):
    _, _, mgr, player = _manager_with_player()
    buffer = np.column_stack(
        [
            np.arange(10, dtype=np.float32),
            np.arange(100, 110, dtype=np.float32),
        ]
    )
    mgr._completed_sessions[1] = _ReplaySession(
        buffer=buffer,
        sample_rate=10.0,
        real_to_logical_channel={0: 0, 1: 1},
    )
    original_asarray = np.asarray

    def fail_on_segment_copy(value, *args, **kwargs):
        if np.shares_memory(value, buffer):
            raise AssertionError("can_replay_selection prepared the replay segment")
        return original_asarray(value, *args, **kwargs)

    monkeypatch.setattr(
        "app.service.analysis.test_manager.np.asarray",
        fail_on_segment_copy,
    )

    assert mgr.can_replay_selection(1, "run-1", 1, 0.2, 0.5) is True
    assert player.play_calls == []


def test_replay_selection_clamps_window_to_captured_duration():
    _, _, mgr, player = _manager_with_player()
    buffer = np.arange(10, dtype=np.float32).reshape(10, 1)
    mgr._completed_sessions[1] = _ReplaySession(
        buffer=buffer,
        sample_rate=10.0,
        real_to_logical_channel={4: 0},
    )

    assert mgr.replay_selection(1, "run-1", 4, -1.0, 2.0) is True

    np.testing.assert_array_equal(player.play_calls[0]["segment"], buffer[:, 0])


def test_replay_selection_replaces_previous_replay():
    _, _, mgr, player = _manager_with_player()
    mgr._completed_sessions[1] = _ReplaySession()

    assert mgr.replay_selection(1, "run-1", 0, 0.0, 0.2) is True
    assert player.stop_calls == []

    assert mgr.replay_selection(1, "run-1", 1, 0.2, 0.4) is True

    assert len(player.play_calls) == 2
    assert player.stop_calls == [True]


def test_stop_stops_replay_for_stopped_config():
    _, _, mgr, player = _manager_with_player()
    mgr._completed_sessions[1] = _ReplaySession()

    assert mgr.replay_selection(1, "run-1", 0, 0.0, 0.2) is True

    mgr.stop(config_id=1, silent=True)

    assert player.stop_calls == [True]


@pytest.mark.parametrize(
    ("label", "session", "run_id", "channel_index", "start_sec", "end_sec"),
    [
        ("missing session", None, "run-1", 0, 0.0, 0.2),
        ("stale run id", _ReplaySession(), "stale-run", 0, 0.0, 0.2),
        (
            "missing buffer",
            _ReplaySession(buffer=None),
            "run-1",
            0,
            0.0,
            0.2,
        ),
        (
            "missing channel map",
            _ReplaySession(real_to_logical_channel={}),
            "run-1",
            0,
            0.0,
            0.2,
        ),
        ("unknown channel", _ReplaySession(), "run-1", 9, 0.0, 0.2),
        ("empty window", _ReplaySession(), "run-1", 0, 0.2, 0.2),
        ("invalid window", _ReplaySession(), "run-1", 0, 0.4, 0.2),
        ("outside window", _ReplaySession(), "run-1", 0, 2.0, 3.0),
        (
            "missing output device",
            _ReplaySession(output_device_name=None),
            "run-1",
            0,
            0.0,
            0.2,
        ),
    ],
)
def test_replay_failures_return_false_log_and_do_not_play(
    label,
    session,
    run_id,
    channel_index,
    start_sec,
    end_sec,
):
    bus, _, mgr, player = _manager_with_player()
    if session is not None:
        if label == "missing buffer":
            session._buffer = None
        mgr._completed_sessions[1] = session

    assert mgr.can_replay_selection(1, run_id, channel_index, start_sec, end_sec) is False
    bus.log.emit.assert_not_called()
    assert mgr.replay_selection(1, run_id, channel_index, start_sec, end_sec) is False

    assert player.play_calls == []
    bus.log.emit.assert_called()
    bus.test_finished.emit.assert_not_called()
    bus.result_changed.emit.assert_not_called()
