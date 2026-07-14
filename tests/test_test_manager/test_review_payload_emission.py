from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis import dsp, stft_score
from app.service.analysis.frames import ChannelJudgment, JudgmentResult
from app.service.analysis.test_manager import TestManager
from app.service.current_meter_service import CurrentSample, build_current_trace_payload

from ._support import (
    _bind_manager_run_generation,
    _complete_current_session,
    _expected_chart_payload,
    _expected_summary_payload,
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


def _install_completed_capture(session, recorder, buffer: np.ndarray):
    attachment = object()
    session.capture_attachment = attachment
    session.captured_frame_count = int(buffer.shape[0])
    session.freeze_capture = MagicMock(return_value=int(buffer.shape[0]))
    recorder._n_channels = int(buffer.shape[1])
    recorder.get_capture_buffer.return_value = buffer
    return attachment


def test_start_current_collection_emits_live_current_trace_update_with_run_id(monkeypatch):
    bus = _mock_bus()
    bus.current_trace_updated = MagicMock()
    bus.current_trace_updated.emit = MagicMock()
    services = _mock_services(bus)

    class FakeCollector:
        instances = []

        def __init__(self, config, *, sample_callback=None, log_callback=None):
            self.config = config
            self.sample_callback = sample_callback
            self.log_callback = log_callback
            FakeCollector.instances.append(self)

        def start(self):
            return True

    monkeypatch.setattr(
        "app.service.analysis.test_manager.CurrentMeterCollector",
        FakeCollector,
    )

    manager = TestManager(bus, services)
    session = SimpleNamespace(
        _config={
            "config_id": 7,
            "current_meter": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 1503,
            },
        },
        _workorder={"factory_run_id": "run-live"},
    )
    manager._sessions[7] = session
    _bind_manager_run_generation(manager, 7, session)

    manager._start_current_collection(7, session)
    FakeCollector.instances[0].sample_callback(
        CurrentSample(time_sec=0.0, current_a=1.642),
        (
            CurrentSample(time_sec=0.0, current_a=1.642),
            CurrentSample(time_sec=0.2, current_a=1.7),
        ),
    )

    bus.current_trace_updated.emit.assert_called_once_with(
        7,
        "run-live",
        {
            "times_sec": [0.0, 0.2],
            "values_a": [1.642, 1.7],
            "average_a": 1.671,
            "max_a": 1.7,
            "latest_a": 1.7,
            "unit": "mA",
        },
    )


def test_start_current_collection_emits_live_current_trace_with_raw_meter_unit(monkeypatch):
    bus = _mock_bus()
    bus.current_trace_updated = MagicMock()
    bus.current_trace_updated.emit = MagicMock()
    services = _mock_services(bus)

    class FakeCollector:
        instances = []

        def __init__(self, config, *, sample_callback=None, log_callback=None):
            self.config = config
            self.sample_callback = sample_callback
            self.log_callback = log_callback
            FakeCollector.instances.append(self)

        def start(self):
            return True

    monkeypatch.setattr(
        "app.service.analysis.test_manager.CurrentMeterCollector",
        FakeCollector,
    )

    raw_current_meter = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 1503,
        "unit": "A",
        "register_count": 2,
        "data_type": "float32",
        "scale": 1.0,
    }
    samples = (
        CurrentSample(time_sec=0.0, current_a=1.642),
        CurrentSample(time_sec=0.2, current_a=1.7),
    )

    manager = TestManager(bus, services)
    session = SimpleNamespace(
        _config={
            "config_id": 7,
            "analysis_window_offset_ms": 1000,
            "current_meter": raw_current_meter,
        },
        _workorder={"factory_run_id": "run-live"},
    )
    manager._sessions[7] = session
    _bind_manager_run_generation(manager, 7, session)

    manager._start_current_collection(7, session)
    FakeCollector.instances[0].sample_callback(samples[-1], samples)

    expected_payload = build_current_trace_payload(samples, config=raw_current_meter)
    assert expected_payload["unit"] == "A"
    bus.current_trace_updated.emit.assert_called_once_with(
        7,
        "run-live",
        expected_payload,
    )


def test_current_trace_update_is_not_emitted_without_run_id_or_payload():
    bus = _mock_bus()
    bus.current_trace_updated = MagicMock()
    bus.current_trace_updated.emit = MagicMock()
    manager = TestManager(bus, _mock_services(bus))

    manager._emit_current_trace_update(
        7,
        None,
        (CurrentSample(time_sec=0.0, current_a=1.0),),
        {"enabled": True, "host": "127.0.0.1"},
    )
    manager._emit_current_trace_update(
        7,
        "run-live",
        (),
        {"enabled": True, "host": "127.0.0.1"},
    )

    bus.current_trace_updated.emit.assert_not_called()


def test_start_current_collection_does_not_emit_live_current_when_meter_disabled(monkeypatch):
    bus = _mock_bus()
    bus.current_trace_updated = MagicMock()
    bus.current_trace_updated.emit = MagicMock()
    services = _mock_services(bus)

    class FakeCollector:
        instances = []

        def __init__(self, *args, **kwargs):
            FakeCollector.instances.append((args, kwargs))

    monkeypatch.setattr(
        "app.service.analysis.test_manager.CurrentMeterCollector",
        FakeCollector,
    )

    manager = TestManager(bus, services)
    session = SimpleNamespace(
        _config={
            "config_id": 7,
            "current_meter": {"enabled": False, "host": "127.0.0.1"},
        },
        _workorder={"factory_run_id": "run-live"},
    )

    manager._start_current_collection(7, session)

    assert FakeCollector.instances == []
    bus.current_trace_updated.emit.assert_not_called()


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

    def test_auto_analysis_review_payload_includes_deep_copied_session_config(
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
        session._config["selected_channels"] = [0]
        session._config["vibration_view_channel_index"] = 0
        session._run_judgment = MagicMock(
            return_value=JudgmentResult(
                overall="合格",
                limit_pass=True,
                ai_pass=True,
                ai_score=0.93,
                analysis_window=(1.0, 3.0),
            )
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

        mgr.stop_active_run(config_id=1)

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.config_payload["selected_channels"] == [0]
        assert payload.config_payload["vibration_view_channel_index"] == 0

        payload.config_payload["selected_channels"].append(99)
        assert session._config["selected_channels"] == [0]

    def test_short_capture_above_manual_minimum_allows_manual_reanalysis(self):
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
        mock_session._buffer = np.zeros((50, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"

        payload = mgr._make_factory_review_payload(
            1,
            mock_session,
            no_result=True,
            error_message="analysis window not fully captured",
        )

        assert payload.captured_duration_sec == pytest.approx(0.5)
        assert payload.manual_reanalysis_allowed is True
        assert payload.fallback_window.start_sec == pytest.approx(0.0)
        assert payload.fallback_window.end_sec == pytest.approx(0.5)

    def test_capture_below_manual_minimum_disallows_manual_reanalysis(self):
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
        mock_session._buffer = np.zeros((5, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"

        payload = mgr._make_factory_review_payload(
            1,
            mock_session,
            no_result=True,
            error_message="analysis window not fully captured",
        )

        assert payload.captured_duration_sec == pytest.approx(0.05)
        assert payload.manual_reanalysis_allowed is False
        assert payload.fallback_window is None

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

        _complete_current_session(mgr, 1)

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

        _complete_current_session(mgr, 1)

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

        _complete_current_session(mgr, 1)

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
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        _install_completed_capture(session, rec, post_attach)
        session._config = {
            "config_id": 1,
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
            "current_meter": {
                "port": 502,
                "unit_id": 1,
                "register_type": "input",
                "register_address": 0,
                "register_count": 1,
                "data_type": "uint16",
                "byte_order": "big",
                "word_order": "big",
                "scale": 0.001,
                "offset": 0.0,
            },
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._audio_file_path = "captured.wav"
        session._last_loudness_trace_cache = {
            0: {
                "times_sec": [0.25],
                "values_sone": [4.0],
                "max_sone": 4.0,
                "algorithm": dsp.LOUDNESS_TRACE_ALGORITHM,
                "internal_rate_hz": dsp.LOUDNESS_TRACE_INTERNAL_RATE_HZ,
            }
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
        generation = _bind_manager_run_generation(mgr, 1, session)
        mgr._current_samples_by_config_id[1] = [
            CurrentSample(time_sec=0.5, current_a=1.0),
            CurrentSample(time_sec=1.5, current_a=2.0),
            CurrentSample(time_sec=2.5, current_a=3.0),
        ]
        mgr._current_sample_generations[1] = generation

        _complete_current_session(mgr, 1)

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
        assert calls["chart"]["current_trace_payload"] == {
            "times_sec": [0.5, 1.5, 2.5],
            "values_a": [1.0, 2.0, 3.0],
            "average_a": 2.0,
            "max_a": 3.0,
            "latest_a": 3.0,
            "unit": "mA",
        }
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

    def test_auto_analysis_review_emits_event_current_labels_after_chart_payload_exists(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        chart_payload = {
            0: {
                "spl": {
                    "times_sec": [0.0, 1.0, 2.0],
                    "values_db": [60.0, 77.0, 70.0],
                },
                "loudness": {
                    "times_sec": [0.0, 1.0, 2.0],
                    "values": [2.0, 7.0, 5.0],
                    "unit": "sone",
                },
                "stft_score": {
                    "enabled": True,
                    "summary_score": 72.0,
                    "times_sec": [0.0, 1.0, 2.0],
                    "scores": [90.0, 72.0, 80.0],
                },
                "current_trace": {
                    "times_sec": [0.0, 2.0],
                    "values_a": [1.0, 3.0],
                    "average_a": 2.0,
                    "max_a": 3.0,
                    "latest_a": 3.0,
                    "unit": "mA",
                },
            }
        }
        session = MagicMock()
        session.config_id = 1
        session._config = {
            "config_id": 1,
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "spl_lower_enabled": True,
                    "spl_lower_limit": 40.0,
                    "spl_upper_enabled": True,
                    "spl_upper_limit": 80.0,
                    "loudness_lower_enabled": True,
                    "loudness_lower_limit": 1.0,
                    "loudness_upper_enabled": True,
                    "loudness_upper_limit": 10.0,
                    "loudness_unit": "sone",
                }
            ],
        }
        session._workorder = {
            "trigger_source": "scan",
            "factory_run_id": "run-1",
        }
        session._buffer = np.zeros((300, 1), dtype=np.float32)
        session._sample_rate = 100.0
        session._audio_file_path = "captured.wav"
        session.last_ai_review = {"status": "not_enabled"}
        session._run_judgment.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(0.0, 2.0),
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=70.0,
                    spl_min_db=60.0,
                    spl_max_db=77.0,
                    spl_pass=True,
                    loudness_min_sone=2.0,
                    loudness_max_sone=7.0,
                    loudness_pass=True,
                    loudness_available=True,
                )
            ],
        )
        monkeypatch.setattr(
            mgr,
            "_build_chart_payload",
            lambda *_args, **_kwargs: chart_payload,
        )
        mgr._sessions[1] = session

        assert mgr._emit_auto_analysis_review(1, session) is True

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        rows_by_key = {
            row["item_key"]: row
            for row in payload.summary_payload["rows"]
            if isinstance(row, dict)
        }
        assert rows_by_key["spl"]["channel_label"] == "2.000 mA"
        assert rows_by_key["loudness"]["channel_label"] == "2.000 mA"
        assert rows_by_key["stft_score"]["actual_display"] == "72.00"
        assert rows_by_key["stft_score"]["channel_label"] == "2.000 mA"

    def test_on_session_done_emits_partial_no_result_summary_when_ai_fails_but_rules_pass(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        _install_completed_capture(session, rec, post_attach)
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

        _complete_current_session(mgr, 1)

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
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        _install_completed_capture(session, rec, post_attach)
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

        _complete_current_session(mgr, 1)

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
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        _install_completed_capture(session, rec, post_attach)
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

        _complete_current_session(mgr, 1)

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
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        _install_completed_capture(session, rec, post_attach)
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

        _complete_current_session(mgr, 1)

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

    def test_rejudge_success_passes_full_current_trace_payload(self, monkeypatch):
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
            "config_id": 1,
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [{"channel_index": 0}],
            "current_meter": {
                "port": 502,
                "unit_id": 1,
                "register_type": "input",
                "register_address": 0,
                "register_count": 1,
                "data_type": "uint16",
                "byte_order": "big",
                "word_order": "big",
                "scale": 0.001,
                "offset": 0.0,
            },
        }
        mock_session._buffer = np.full((500, 1), 0.37, dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_ai_review = None
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
                    loudness_available=False,
                )
            ],
        )
        captured = {}

        def fake_build_chart_payload(**kwargs):
            captured.update(kwargs)
            return {0: {"spl": {"values_db": [37.0]}}}

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: {"overall_status": "pass", "rows": []},
            raising=False,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_chart_payload",
            fake_build_chart_payload,
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session
        generation = object()
        mock_session._manager_generation_token = generation
        mgr._current_samples_by_config_id[1] = [
            CurrentSample(time_sec=0.5, current_a=1.0),
            CurrentSample(time_sec=1.5, current_a=2.0),
            CurrentSample(time_sec=2.5, current_a=3.0),
        ]
        mgr._current_sample_generations[1] = generation

        mgr.rejudge(config_id=1, start_sec=1.0, end_sec=3.0)

        assert captured["analysis_window"] == (1.0, 3.0)
        assert captured["current_trace_payload"] == {
            "times_sec": [0.5, 1.5, 2.5],
            "values_a": [1.0, 2.0, 3.0],
            "average_a": 2.0,
            "max_a": 3.0,
            "latest_a": 3.0,
            "unit": "mA",
        }

    def test_rejudge_success_emits_stft_score_payload_with_reanalysis_window_times(
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
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_nfft": 128,
                    "stft_overlap": 0.5,
                    "stft_score_enabled": True,
                }
            ],
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_ai_review = None
        mock_session.rejudge.return_value = JudgmentResult(
            overall="合格",
            limit_pass=True,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.25, 3.25),
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=70.0,
                    spl_pass=True,
                    loudness_available=False,
                )
            ],
        )
        captured = {}

        def fake_score(**kwargs):
            captured.update(kwargs)
            return {
                "enabled": True,
                "summary_score": 86.5,
                "times_sec": [1.25, 2.25],
                "scores": [88.0, 85.0],
                "summary_mode": "percentile",
                "percentile": 10.0,
            }

        monkeypatch.setattr(stft_score, "compute_stft_score_payload", fake_score)
        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: {"overall_status": "pass", "rows": []},
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.25, end_sec=3.25)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.chart_payload[0]["stft_score"]["summary_score"] == 86.5
        assert payload.chart_payload[0]["stft_score"]["times_sec"][0] == pytest.approx(1.25)
        assert payload.summary_payload["rows"][0]["item_key"] == "stft_score"
        assert payload.summary_payload["rows"][0]["actual_display"] == "86.50"
        assert captured["absolute_start_sec"] == pytest.approx(1.25)

    @pytest.mark.parametrize("score_mode", ["enabled", "disabled", "raises"])
    def test_stft_score_never_changes_reanalysis_judgment_or_summary(
        self,
        monkeypatch,
        score_mode,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        mock_session = MagicMock()
        mock_session.config_id = 1
        mock_session._workorder = {"trigger_source": "manual", "factory_run_id": "run-1"}
        mock_session._config = {
            "analysis_duration": 2.0,
            "analysis_window_offset_ms": 0,
            "selected_channels": [0],
            "channels": [
                {
                    "channel_index": 0,
                    "stft_nfft": 128,
                    "stft_overlap": 0.5,
                    "stft_score_enabled": score_mode != "disabled",
                }
            ],
        }
        mock_session._buffer = np.zeros((500, 1), dtype=np.float32)
        mock_session._sample_rate = 100.0
        mock_session._audio_file_path = "captured.wav"
        mock_session.last_ai_review = None
        mock_session.rejudge.return_value = JudgmentResult(
            overall="不合格",
            limit_pass=False,
            ai_pass=None,
            ai_score=None,
            analysis_window=(1.25, 3.25),
            channel_results=[
                ChannelJudgment(
                    channel_index=0,
                    spl_db=92.0,
                    spl_pass=False,
                    loudness_available=False,
                )
            ],
        )
        expected_summary = {"overall_status": "fail", "rows": []}

        if score_mode == "raises":
            def fail_score(**_kwargs):
                raise RuntimeError("score unavailable")

            monkeypatch.setattr(stft_score, "compute_stft_score_payload", fail_score)
        elif score_mode == "enabled":
            monkeypatch.setattr(
                stft_score,
                "compute_stft_score_payload",
                lambda **kwargs: {
                    "enabled": True,
                    "summary_score": 64.0,
                    "times_sec": [float(kwargs["absolute_start_sec"])],
                    "scores": [64.0],
                    "summary_mode": "percentile",
                    "percentile": 10.0,
                },
            )

        monkeypatch.setattr(
            "app.service.analysis.test_manager.build_summary_payload",
            lambda **kwargs: expected_summary,
            raising=False,
        )
        mgr._completed_sessions[1] = mock_session

        mgr.rejudge(config_id=1, start_sec=1.25, end_sec=3.25)

        payload = bus.factory_runtime_reanalysis_completed.emit.call_args.args[0]
        assert payload.judgment_payload["overall"] == "不合格"
        assert payload.judgment_payload["limit_pass"] is False
        assert payload.summary_payload["overall_status"] == "fail"

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

    def test_on_session_done_does_not_warn_when_display_loudness_peak_differs_from_serialized_internal_peak(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        post_attach = np.arange(500, dtype=np.float32).reshape(500, 1)
        rec = MagicMock()
        rec._sample_rate = 100.0
        session = MagicMock()
        session.config_id = 1
        session._buffer = None
        _install_completed_capture(session, rec, post_attach)
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
        expected_chart[0]["loudness"]["values"] = [2.0]

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

        _complete_current_session(mgr, 1)

        payload = bus.factory_runtime_auto_analysis_succeeded.emit.call_args.args[0]
        assert payload.summary_payload == expected_summary
        assert payload.chart_payload == expected_chart
        assert payload.no_result is False
        assert not any(
            "WARNING" in call.args[0] and "scalar" in call.args[0] and "loudness" in call.args[0]
            for call in bus.log.emit.call_args_list
        )
