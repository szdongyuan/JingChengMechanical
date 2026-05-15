from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from app.service.analysis.frames import ChannelJudgment, JudgmentResult
from app.service.analysis.test_manager import TestManager

from ._support import (
    _expected_chart_payload,
    _expected_summary_payload,
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


class TestReviewPayloadEmission:
    def test_stop_triggered_auto_analysis_preserves_original_trigger_source(self, monkeypatch):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            trigger_source="plc",
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

        mgr.stop_active_run(config_id=1)

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.displayed_config_id == 1
        assert payload.run_id == session._workorder["factory_run_id"]
        assert payload.trigger_source == "plc"

    def test_stop_after_judgment_returns_suppresses_late_auto_analysis_review_emission(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
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
        stop_results = []
        original_build_summary_payload = mgr._build_summary_payload

        def _stop_before_first_review_emit(running_session, *, judgment_payload):
            stop_results.append(mgr.stop_active_run(config_id=1))
            return original_build_summary_payload(
                running_session,
                judgment_payload=judgment_payload,
            )

        monkeypatch.setattr(mgr, "_build_summary_payload", _stop_before_first_review_emit)

        mgr._on_session_done(1)

        assert stop_results == [True]
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.test_finished.emit.assert_not_called()
        bus.result_changed.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

    def test_stop_after_no_result_test_finished_suppresses_follow_on_review_emissions(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._run_judgment = MagicMock(
            side_effect=ValueError("analysis window not fully captured")
        )
        stop_results = []
        bus.test_finished.emit.side_effect = (
            lambda *args: stop_results.append(mgr.stop_active_run(config_id=1))
        )

        mgr._on_session_done(1)

        assert stop_results == [True]
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.test_finished.emit.assert_called_once()
        bus.result_changed.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

    def test_stop_after_error_test_finished_suppresses_follow_on_review_emissions(
        self,
        monkeypatch,
    ):
        partial_buf = np.arange(400, dtype=np.float32).reshape(400, 1)
        bus, _, mgr, session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            analysis_duration=2.0,
            analysis_window_offset_ms=1000,
            allow_incomplete_recording_analysis=True,
            partial_buf=partial_buf,
            sample_rate=100.0,
        )
        session._run_judgment = MagicMock(side_effect=RuntimeError("analysis exploded"))
        stop_results = []
        bus.test_finished.emit.side_effect = (
            lambda *args: stop_results.append(mgr.stop_active_run(config_id=1))
        )

        mgr._on_session_done(1)

        assert stop_results == [True]
        bus.factory_runtime_stopped.emit.assert_called_once()
        bus.test_finished.emit.assert_called_once()
        bus.result_changed.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_cleanup_settled.emit.assert_not_called()
        assert 1 not in mgr._completed_sessions
        assert mgr.is_running(config_id=1) is False

    def test_on_session_done_emits_summary_and_chart_payload_on_success(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        session._last_loudness_trace_cache = {
            0: {"times_sec": [0.25], "values_sone": [4.0], "max_sone": 4.0}
        }
        session.last_ai_review = {
            "status": "success",
            "threshold": 0.8,
            "score": 0.93,
        }
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.93,
            analysis_window=(1.0, 3.0),
        )
        expected_summary = _expected_summary_payload(overall_status="pass")
        expected_chart = _expected_chart_payload()
        calls = {}

        def fake_build_summary_payload(**kwargs):
            calls["summary"] = kwargs
            return expected_summary

        def fake_build_chart_payload(**kwargs):
            calls["chart"] = kwargs
            return expected_chart

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            fake_build_summary_payload,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            fake_build_chart_payload,
            raising=False,
        )
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        assert calls["summary"]["config"] == session._config
        assert calls["summary"]["ai_review"] == session.last_ai_review
        assert calls["summary"]["error_message"] is None
        assert calls["summary"]["judgment_payload"] == {
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": True,
            "ai_score": 0.93,
            "channel_results": [],
            "analysis_window": (1.0, 3.0),
        }
        assert calls["chart"]["config"] == session._config
        assert calls["chart"]["sample_rate"] == 100.0
        assert calls["chart"]["analysis_window"] == (1.0, 3.0)
        assert calls["chart"]["loudness_trace_cache"] is session._last_loudness_trace_cache
        assert calls["chart"]["judgment_payload"] == {
            "overall": "合格",
            "limit_pass": True,
            "ai_pass": True,
            "ai_score": 0.93,
            "channel_results": [],
            "analysis_window": (1.0, 3.0),
        }
        np.testing.assert_array_equal(calls["chart"]["buffer"], post_attach)
        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.summary_payload == expected_summary
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert payload.error_message is None

    def test_on_session_done_emits_partial_no_result_summary_when_ai_fails_but_rules_pass(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        session._workorder = {"factory_run_id": "run-1"}
        session._audio_file_path = "captured.wav"
        session.last_ai_review = {
            "status": "timeout",
            "threshold": 0.8,
            "score": None,
            "error_message": "AI timeout",
        }
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 3.0),
        )
        expected_summary = _expected_summary_payload(
            overall_status="no_result",
            error_message="AI timeout",
        )
        expected_chart = _expected_chart_payload()
        expected_summary["rows"][-1]["status"] = "无结果"

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            lambda **kwargs: expected_chart,
            raising=False,
        )
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.summary_payload["overall_status"] == "no_result"
        assert payload.summary_payload["rows"][-1]["status"] == "无结果"
        assert payload.summary_payload["error_message"] == "AI timeout"
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert payload.error_message is None

    def test_on_session_done_preserves_summary_when_fft_chart_item_is_omitted(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        session.last_ai_review = {"status": "not_enabled"}
        session._run_judgment.return_value = JudgmentResult(
            overall="不合格",
            limit_pass=False,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 3.0),
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=70.0,
                    spl_pass=True,
                    loudness_max_sone=3.0,
                    loudness_pass=True,
                    loudness_available=True,
                    loudness_error_message=None,
                    fft_available=True,
                    fft_band_results=[],
                    octave_available=True,
                    oct_band_results=[],
                )
            ],
        )
        expected_summary = _expected_summary_payload(overall_status="no_result")
        expected_summary["rows"][2]["status"] = "无结果"
        expected_summary["rows"][2]["deviation_value"] = None
        expected_summary["rows"][2]["deviation_display"] = "--"
        expected_chart = _expected_chart_payload()
        expected_chart[0].pop("fft")

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            lambda **kwargs: expected_chart,
            raising=False,
        )
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.summary_payload == expected_summary
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert payload.error_message is None

    def test_on_session_done_required_window_failure_emits_no_chart_payload_and_no_result_summary(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._audio_file_path = None
        session._audio_data_id = None
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }

        def raise_required_window_failure():
            session._audio_file_path = "captured.wav"
            session._audio_data_id = "audio-123"
            raise ValueError("analysis window not fully captured")

        expected_summary = _expected_summary_payload(
            overall_status="no_result",
            error_message="analysis window not fully captured",
        )
        for row in expected_summary["rows"]:
            row["deviation_value"] = None
            row["deviation_display"] = "--"
            row["channel_index"] = None
            row["channel_label"] = "--"
            row["status"] = "无结果"
            row["constrained"] = False

        build_summary_calls = []

        def fake_build_summary_payload(**kwargs):
            build_summary_calls.append(kwargs)
            return expected_summary

        def fail_build_chart_payload(**kwargs):
            raise AssertionError("chart payload should not be built for full no-result")

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            fake_build_summary_payload,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            fail_build_chart_payload,
            raising=False,
        )
        session._run_judgment.side_effect = raise_required_window_failure
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        assert build_summary_calls == [
            {
                "config": session._config,
                "judgment_payload": None,
                "ai_review": None,
                "error_message": "analysis window not fully captured",
            }
        ]
        payload = bus.factory_runtime_auto_analysis_no_result.emit.call_args.args[0]
        assert payload.summary_payload["overall_status"] == "no_result"
        assert payload.summary_payload["error_message"] == "analysis window not fully captured"
        assert len(payload.summary_payload["rows"]) == 5
        assert all(row["status"] == "无结果" for row in payload.summary_payload["rows"])
        assert payload.chart_payload is None
        assert payload.no_result is True

    def test_on_session_done_none_result_emits_no_chart_payload_and_no_result_summary(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(10, 20, dtype=np.float32).reshape(10, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._audio_file_path = "captured.wav"
        session._audio_data_id = "audio-123"
        session._workorder = {"factory_run_id": "run-1"}
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }

        expected_summary = _expected_summary_payload(
            overall_status="no_result",
            error_message="analysis result unavailable",
        )
        for row in expected_summary["rows"]:
            row["deviation_value"] = None
            row["deviation_display"] = "--"
            row["channel_index"] = None
            row["channel_label"] = "--"
            row["status"] = "无结果"
            row["constrained"] = False

        build_summary_calls = []

        def fake_build_summary_payload(**kwargs):
            build_summary_calls.append(kwargs)
            return expected_summary

        def fail_build_chart_payload(**kwargs):
            raise AssertionError("chart payload should not be built for full no-result")

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            fake_build_summary_payload,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            fail_build_chart_payload,
            raising=False,
        )
        session._run_judgment.return_value = None
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        assert build_summary_calls == [
            {
                "config": session._config,
                "judgment_payload": None,
                "ai_review": None,
                "error_message": "analysis result unavailable",
            }
        ]
        payload = bus.factory_runtime_auto_analysis_no_result.emit.call_args.args[0]
        assert payload.summary_payload["overall_status"] == "no_result"
        assert payload.summary_payload["error_message"] == "analysis result unavailable"
        assert len(payload.summary_payload["rows"]) == 5
        assert all(row["status"] == "无结果" for row in payload.summary_payload["rows"])
        assert payload.chart_payload is None
        assert payload.no_result is True

    def test_rejudge_success_emits_replacement_summary_and_chart_payload(self, monkeypatch):
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
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_ai_review = {
            "status": "success",
            "threshold": 0.8,
            "score": 0.88,
        }
        mock_session.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=True,
            ai_score=0.88,
            analysis_window=(1.0, 3.0),
        )
        expected_summary = _expected_summary_payload(overall_status="pass")
        expected_chart = _expected_chart_payload()

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            lambda **kwargs: expected_chart,
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.summary_payload["overall_status"] in {"pass", "fail", "no_result"}
        assert payload.summary_payload == expected_summary
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert payload.error_message is None

    def test_rejudge_chart_payload_rebuild_uses_selected_buffer_and_real_channel_map(
        self,
        monkeypatch,
    ):
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
            "selected_channels": [3],
            "channels": [{"channel_index": 3}],
        }
        mock_session._buffer = np.full((500, 1), 0.37, dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session._real_to_logical_channel = {3: 0}
        mock_session.last_ai_review = None
        mock_session.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 3.0),
            channel_results=[
                ChannelJudgment(
                    channel_index=3,
                    spl_db=70.0,
                    spl_pass=True,
                    loudness_available=False,
                )
            ],
        )
        chart_calls = {}

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: {"overall_status": "pass", "rows": []},
            raising=False,
        )

        def fake_build_chart_payload(**kwargs):
            chart_calls["kwargs"] = kwargs
            return {3: {"spl": {"values_db": [37.0]}}}

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            fake_build_chart_payload,
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)

        assert chart_calls["kwargs"]["buffer"] is mock_session._buffer
        assert chart_calls["kwargs"]["channel_column_map"] == {3: 0}
        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert sorted(payload.chart_payload) == [3]
        assert payload.no_result is False

    def test_rejudge_failure_emits_full_no_result_summary_without_chart_payload(self, monkeypatch):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {"factory_run_id": "run-1"}
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 1500,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.rejudge.side_effect = ValueError("analysis window not fully captured")
        expected_summary = _expected_summary_payload(
            overall_status="no_result",
            error_message="analysis window not fully captured",
        )
        for row in expected_summary["rows"]:
            row["deviation_value"] = None
            row["deviation_display"] = "--"
            row["channel_index"] = None
            row["channel_label"] = "--"
            row["status"] = "无结果"
            row["constrained"] = False

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("chart payload should not be built for reanalysis failure")
            ),
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=0.0, end_sec=2.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.summary_payload["overall_status"] == "no_result"
        assert len(payload.summary_payload["rows"]) == 5
        assert all(row["status"] == "无结果" for row in payload.summary_payload["rows"])
        assert payload.chart_payload is None
        assert payload.no_result is True
        assert payload.error_message == "analysis window not fully captured"

    def test_rejudge_success_logs_warning_when_loudness_chart_payload_is_missing(self, monkeypatch):
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
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_ai_review = {"status": "not_enabled"}
        mock_session.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 3.0),
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=70.0,
                    spl_pass=True,
                    loudness_max_sone=3.0,
                    loudness_pass=True,
                    loudness_available=True,
                    loudness_error_message=None,
                )
            ],
        )
        expected_summary = _expected_summary_payload(overall_status="pass")
        expected_chart = _expected_chart_payload()
        expected_chart[0].pop("loudness")

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            lambda **kwargs: expected_chart,
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.summary_payload == expected_summary
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert any(
            "WARNING" in call.args[0] and "loudness chart" in call.args[0]
            for call in bus.log.emit.call_args_list
        )

    def test_on_session_done_logs_warning_when_loudness_chart_scalar_disagrees_with_serialized_judgment(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        attach_snapshot = object()
        rec = MagicMock()
        rec.get_buffer_from.return_value = post_attach
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        session._attach_snapshot = attach_snapshot
        session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        session.last_ai_review = {"status": "not_enabled"}
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.0, 3.0),
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=70.0,
                    spl_pass=True,
                    loudness_max_sone=3.0,
                    loudness_pass=True,
                    loudness_available=True,
                    loudness_error_message=None,
                )
            ],
        )
        expected_summary = _expected_summary_payload(overall_status="pass")
        expected_chart = _expected_chart_payload()
        expected_chart[0]["loudness"]["values"] = [2.0, 5.0]

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            lambda **kwargs: expected_chart,
            raising=False,
        )
        mgr._sessions[1] = session
        mgr._recorders[1] = rec

        mgr._on_session_done(1)

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.summary_payload == expected_summary
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert any(
            "WARNING" in call.args[0] and "scalar" in call.args[0] and "loudness" in call.args[0]
            for call in bus.log.emit.call_args_list
        )
