from __future__ import annotations

import gc
import weakref
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager

from ._support import (
    _CaptureRecorderDoubleMixin,
    _DummySignal,
    _FakeSession,
    _factory_runtime_config,
    _make_running_factory_manager,
    _mock_bus,
    _mock_services,
)


def _successful_judgment() -> JudgmentResult:
    return JudgmentResult(
        overall="合格",
        limit_pass=True,
        ai_pass=None,
        ai_score=None,
        analysis_window=(0.0, 0.1),
    )


def _assert_no_generation_state(manager: TestManager, config_id: int = 1) -> None:
    assert config_id not in manager._session_generations
    assert config_id not in manager._current_collection_generations
    assert config_id not in manager._current_collector_generations
    assert config_id not in manager._current_sample_generations


class _StartRecorder(_CaptureRecorderDoubleMixin):
    def __init__(
        self,
        *,
        device_name,
        hostapi,
        sample_rate,
        n_channels,
        record_duration,
    ):
        self._sample_rate = float(sample_rate)
        self._n_channels = int(n_channels)
        self._buffer = np.zeros(
            (int(self._sample_rate * record_duration), self._n_channels),
            dtype=np.float32,
        )
        self.error_occurred = _DummySignal()
        self._running = False
        self.stop_calls = 0

    def isRunning(self):
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self.stop_calls += 1
        self._running = False

    def wait(self, _timeout):
        return True


def _make_current_start_manager(monkeypatch, collector_type):
    bus = _mock_bus()
    services = _mock_services(bus)
    config = _factory_runtime_config()
    config["output_device_name"] = None
    config["current_meter"] = {"enabled": True}
    services.config_service.load_config.return_value = config
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioRecorder",
        _StartRecorder,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.TestSession",
        _FakeSession,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.CurrentMeterCollector",
        collector_type,
    )
    return bus, services, TestManager(bus, services)


class TestGenerationRetirement:
    def test_completion_ingress_has_no_config_only_fallback(self, monkeypatch):
        _, _, manager, _, _, _, _ = _make_running_factory_manager(monkeypatch)

        try:
            with pytest.raises(TypeError):
                manager._on_session_done(1)
        finally:
            manager.stop(config_id=1, silent=True)

    def test_stale_completion_after_final_progress_replacement_cannot_finalize_new_run(
        self,
        monkeypatch,
    ):
        bus, _, manager, old_session, _, _, _ = _make_running_factory_manager(
            monkeypatch,
            partial_buf=np.arange(20, dtype=np.float32).reshape(20, 1),
            sample_rate=100.0,
        )
        old_completion = old_session._on_session_done_cb
        replacement = {}

        def replace_from_final_progress(config_id, elapsed):
            if replacement or config_id != 1 or elapsed != 2.0:
                return
            manager.stop(config_id=1, silent=True)
            replacement["accepted"] = manager.start({"config_id": 1, "sn": "SN2"})
            replacement["session"] = manager._sessions[1]
            replacement["recorder"] = manager._recorders[1]
            replacement["route"] = manager._session_error_routes[1]
            replacement["generation"] = manager._session_generations[1]
            replacement["completion"] = replacement["session"]._on_session_done_cb
            replacement["session"]._run_judgment = MagicMock(
                return_value=_successful_judgment()
            )

        bus.recording_progress.emit.side_effect = replace_from_final_progress

        bus.recording_progress.emit(1, 2.0)
        old_completion(1)

        assert replacement["accepted"] is True
        assert manager._sessions.get(1) is replacement["session"]
        assert manager._recorders.get(1) is replacement["recorder"]
        assert manager._session_error_routes.get(1) is replacement["route"]
        assert manager._session_generations.get(1) is replacement["generation"]
        assert replacement["generation"] is replacement["route"].generation
        assert replacement["generation"] is not replacement["route"]
        assert replacement["generation"] is not replacement["session"]
        assert replacement["generation"] is not replacement["recorder"]
        assert manager._completed_sessions.get(1) is not replacement["session"]
        assert manager.is_running(config_id=1) is True

        replacement["completion"](1)

        assert manager.is_running(config_id=1) is False
        assert manager._completed_sessions.get(1) is replacement["session"]

    def test_disabled_current_normal_stop_retires_tokens_and_releases_resources(
        self,
        monkeypatch,
    ):
        _, _, manager, session, recorder, monitor, _ = _make_running_factory_manager(
            monkeypatch
        )
        session_ref = weakref.ref(session)
        recorder_ref = weakref.ref(recorder)
        monitor_ref = weakref.ref(monitor)
        assert 1 in manager._session_generations
        assert 1 not in manager._current_collection_generations

        manager.stop(config_id=1, silent=True)

        type(session).instances.clear()
        type(recorder).instances.clear()
        type(monitor).instances.clear()
        del session, recorder, monitor
        gc.collect()

        assert session_ref() is None
        assert recorder_ref() is None
        assert monitor_ref() is None
        _assert_no_generation_state(manager)

    def test_completion_route_pop_then_reentrant_stop_retires_exact_run_tokens(
        self,
        monkeypatch,
    ):
        _, _, manager, session, _, _, _ = _make_running_factory_manager(monkeypatch)
        completion = session._on_session_done_cb
        original_freeze = session.freeze_capture

        def stop_after_completion_claim():
            manager.stop(config_id=1, silent=True)
            return original_freeze()

        session.freeze_capture = stop_after_completion_claim

        completion(1)

        _assert_no_generation_state(manager)
        assert manager.is_running(config_id=1) is False

    def test_enabled_current_normal_stop_retires_all_generation_maps(
        self,
        monkeypatch,
    ):
        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0

            def start(self):
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return ["old-sample"]

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)

        assert manager.start({"config_id": 1, "sn": "SN1"}) is True
        collector = manager._current_collectors[1]
        manager.stop(config_id=1, silent=True)

        assert collector.stop_calls == 1
        _assert_no_generation_state(manager)
        assert 1 not in manager._current_samples_by_config_id

    def test_enabled_completion_retires_live_maps_and_keeps_only_sample_token(
        self,
        monkeypatch,
    ):
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return ["completed-sample"]

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        assert manager.start({"config_id": 1, "sn": "SN1"}) is True
        session = manager._sessions[1]
        recorder = manager._recorders[1]
        route = manager._session_error_routes[1]
        generation = manager._session_generations[1]
        session._captured_frame_count = 20
        session._run_judgment = MagicMock(return_value=_successful_judgment())

        session._on_session_done_cb(1)

        assert collectors[0].stop_calls == 1
        assert manager._completed_sessions.get(1) is session
        assert 1 not in manager._session_generations
        assert 1 not in manager._current_collection_generations
        assert 1 not in manager._current_collector_generations
        assert manager._current_sample_generations.get(1) is generation
        assert manager._current_samples_by_config_id.get(1) == ["completed-sample"]
        assert generation is route.generation
        assert generation is not route
        assert generation is not session
        assert generation is not recorder

    def test_collector_start_false_retires_unregistered_collection_generation(
        self,
        monkeypatch,
    ):
        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                pass

            def start(self):
                return False

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)

        try:
            assert manager.start({"config_id": 1, "sn": "SN1"}) is True
            assert 1 in manager._session_generations
            assert 1 not in manager._current_collection_generations
            assert 1 not in manager._current_collector_generations
        finally:
            manager.stop(config_id=1, silent=True)

    def test_collector_start_failure_retires_failed_run_generation_state(
        self,
        monkeypatch,
    ):
        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                pass

            def start(self):
                raise RuntimeError("collector startup failed")

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)

        assert manager.start({"config_id": 1, "sn": "SN1"}) is False
        _assert_no_generation_state(manager)

    def test_failed_start_after_collector_registration_retires_exact_generation(
        self,
        monkeypatch,
    ):
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return ["startup-sample"]

        bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
        bus.recording_progress.emit.side_effect = RuntimeError("progress failed")

        assert manager.start({"config_id": 1, "sn": "SN1"}) is False

        assert collectors[0].stop_calls == 1
        assert manager.is_running(config_id=1) is False
        assert manager._current_collectors == {}
        assert manager._current_samples_by_config_id == {}
        _assert_no_generation_state(manager)

    def test_collector_start_reentrant_stop_cannot_register_orphan(
        self,
        monkeypatch,
    ):
        holder = {}
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                self.start_lock_owned = None
                self.stop_lock_owned = None
                collectors.append(self)

            def start(self):
                self.start_lock_owned = holder["manager"]._lifecycle_lock._is_owned()
                holder["manager"].stop(config_id=1, silent=True)
                return True

            def stop(self):
                self.stop_lock_owned = holder["manager"]._lifecycle_lock._is_owned()
                self.stop_calls += 1

            def samples(self):
                return []

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager

        assert manager.start({"config_id": 1, "sn": "SN1"}) is False

        assert len(collectors) == 1
        assert collectors[0].stop_calls == 1
        assert collectors[0].start_lock_owned is False
        assert collectors[0].stop_lock_owned is False
        assert manager.is_running(config_id=1) is False
        assert manager._current_collectors == {}
        assert manager._current_samples_by_config_id == {}
        _assert_no_generation_state(manager)
