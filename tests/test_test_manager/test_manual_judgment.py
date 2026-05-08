from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.test_manager import TestManager
from tests.test_test_manager._support import _mock_bus, _mock_services


class _SessionStub:
    def __init__(self, *, run_id="run-1", audio_data_id="audio-1", result=None):
        self._workorder = {"factory_run_id": run_id, "trigger_source": "manual"}
        self._audio_data_id = audio_data_id
        self.apply_manual_label = MagicMock(return_value=result)


class _ManualResult:
    def __init__(self, success, message="", audio_data_id="audio-1"):
        self.success = success
        self.message = message
        self.audio_data_id = audio_data_id


def _make_manager():
    bus = _mock_bus()
    services = _mock_services(bus)
    return TestManager(bus, services), bus


def test_apply_manual_judgment_updates_completed_session():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(True))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("OK")


def test_apply_manual_judgment_allows_saved_no_result_session():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(True), audio_data_id="audio-1")
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="NG")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("NG")


def test_apply_manual_judgment_rejects_active_recording_session():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(True), audio_data_id="audio-1")
    mgr._sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is False
    assert "测试仍在进行中" in result.message
    session.apply_manual_label.assert_not_called()


def test_apply_manual_judgment_rejects_active_analyzing_session():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(True), audio_data_id="audio-1")
    mgr._completed_sessions[101] = session
    mgr._analyzing_sessions.add(101)

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="NG")

    assert result.success is False
    assert "测试仍在进行中" in result.message
    session.apply_manual_label.assert_not_called()


def test_apply_manual_judgment_rejects_missing_session():
    mgr, _bus = _make_manager()

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is False
    assert "没有可判定的测试结果" in result.message


def test_apply_manual_judgment_rejects_stale_run_id():
    mgr, _bus = _make_manager()
    session = _SessionStub(run_id="run-current", result=_ManualResult(True))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-old", label="OK")

    assert result.success is False
    assert "测试结果已过期" in result.message
    session.apply_manual_label.assert_not_called()


def test_apply_manual_judgment_returns_session_failure_message():
    mgr, _bus = _make_manager()
    session = _SessionStub(result=_ManualResult(False, "当前测试音频记录不存在"))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-1", label="OK")

    assert result.success is False
    assert result.message == "当前测试音频记录不存在"


def test_apply_manual_judgment_rejects_provided_run_id_when_session_has_no_run_id():
    mgr, _bus = _make_manager()
    session = _SessionStub(run_id="", result=_ManualResult(True))
    mgr._completed_sessions[101] = session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-from-ui", label="OK")

    assert result.success is False
    assert "测试结果已过期" in result.message
    session.apply_manual_label.assert_not_called()


def test_stopped_partial_saved_session_can_be_manually_judged(monkeypatch):
    from tests.test_test_manager._support import _make_running_factory_manager

    (
        _bus,
        _services,
        mgr,
        session,
        _recorder,
        _monitor,
        _partial_buf,
    ) = _make_running_factory_manager(monkeypatch)
    session.apply_manual_label = MagicMock(return_value=_ManualResult(True))

    stopped = mgr.stop_active_run(config_id=1)

    assert stopped is True
    assert mgr._completed_sessions[1] is session

    result = mgr.apply_manual_judgment(
        config_id=1,
        run_id=session._workorder["factory_run_id"],
        label="OK",
    )

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("OK")


def test_no_result_after_saved_audio_row_can_be_manually_judged():
    mgr, _bus = _make_manager()
    session = _SessionStub(
        run_id="run-no-result",
        audio_data_id="audio-1",
        result=_ManualResult(True),
    )
    session._config = {"config_id": 101, "selected_channels": [0], "analysis_duration": 1.0}
    session._buffer = np.zeros((100, 1), dtype=np.float32)
    session._sample_rate = 44100.0
    session._audio_file_path = "saved.wav"
    session._run_judgment = MagicMock(side_effect=ValueError("analysis result unavailable"))
    mgr._sessions[101] = session

    mgr._on_session_done(101)

    assert mgr._completed_sessions[101] is session

    result = mgr.apply_manual_judgment(config_id=101, run_id="run-no-result", label="NG")

    assert result.success is True
    session.apply_manual_label.assert_called_once_with("NG")
