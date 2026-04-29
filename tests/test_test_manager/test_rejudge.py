from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager

from ._support import (
    _expected_no_result_payload,
    _mock_bus,
    _mock_services,
)


class TestTestManagerRejudge:
    def test_rejudge_delegates_to_session(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._sessions[1] = mock_session
        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)
        mock_session.rejudge.assert_called_with(1.0, 3.0)

    def test_rejudge_works_on_completed_session(self):
        """After recording completes, session moves to _completed_sessions.
        rejudge() should still find it there."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mgr._completed_sessions[1] = mock_session
        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)
        mock_session.rejudge.assert_called_with(0.0, 2.0)

    def test_rejudge_emits_factory_runtime_reanalysis_completed_on_success(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {
            "trigger_source": "manual",
            "factory_run_id": "run-1",
        }
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
            analysis_window=(1.0, 3.0),
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.displayed_config_id == 1
        assert payload.run_id == "run-1"
        assert payload.trigger_source == "manual"
        assert payload.captured_duration_sec == 5.0
        assert payload.default_window.start_sec == 1.0
        assert payload.default_window.end_sec == 3.0
        assert payload.fallback_window is None
        assert payload.manual_reanalysis_allowed is True
        assert payload.no_result is False
        assert payload.error_message is None
        assert payload.judgment_payload == {
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": True,
            "ai_score": 0.88,
            "channel_results": [],
            "analysis_window": (1.0, 3.0),
        }

    def test_rejudge_without_session_emits_factory_runtime_reanalysis_completed(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)

        mgr.rejudge(config_id=99, start_sec=0.0, end_sec=2.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.displayed_config_id == 99
        assert payload.run_id is None
        assert payload.captured_duration_sec is None
        assert payload.default_window is None
        assert payload.fallback_window is None
        assert payload.manual_reanalysis_allowed is False
        assert payload.no_result is True
        assert payload.error_message == "no session for config 99"
        assert payload.judgment_payload is None
        assert payload.summary_payload["overall_status"] == "no_result"
        assert payload.summary_payload["error_message"] == "no session for config 99"
        assert len(payload.summary_payload["rows"]) == 7
        assert all(row["status"] == "无结果" for row in payload.summary_payload["rows"])
        assert payload.chart_payload is None
        bus.test_finished.emit.assert_not_called()
        bus.result_changed.emit.assert_not_called()

    def test_rejudge_required_window_failure_emits_no_result_payload_without_overwriting_trace(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        prior_result = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
        )
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {"factory_run_id": "run-1"}
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_result = prior_result
        mock_session.rejudge.side_effect = ValueError("analysis window not fully captured")
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)

        payload = _expected_no_result_payload("analysis window not fully captured")
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        factory_payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis window not fully captured"
        assert factory_payload.judgment_payload is None
        assert mock_session.last_result is prior_result

    def test_rejudge_none_result_emits_no_result_payload_without_overwriting_trace(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        prior_result = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
        )
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {"factory_run_id": "run-1"}
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_result = prior_result
        mock_session.rejudge.return_value = None
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)

        payload = _expected_no_result_payload("analysis result unavailable")
        bus.test_finished.emit.assert_called_once_with(1, payload)
        bus.result_changed.emit.assert_called_once_with(payload)
        factory_payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert factory_payload.displayed_config_id == 1
        assert factory_payload.run_id == "run-1"
        assert factory_payload.no_result is True
        assert factory_payload.error_message == "analysis result unavailable"
        assert factory_payload.judgment_payload is None
        assert mock_session.last_result is prior_result
