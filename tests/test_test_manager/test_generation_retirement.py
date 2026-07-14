from __future__ import annotations

import gc
import threading
import weakref
from unittest.mock import MagicMock, call

import numpy as np
import pytest

from app.service.analysis.frames import JudgmentResult
from app.service.analysis.test_manager import TestManager
from app.service.current_meter_service import CurrentMeterCollector, CurrentSample

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


def _seed_completed_current_owner(manager: TestManager):
    prior_generation = object()
    prior_session = MagicMock(name="prior_completed_session")
    prior_session._manager_generation_token = prior_generation
    prior_session._config = {
        "config_id": 1,
        "current_meter": {"enabled": True},
        "selected_channels": [0],
        "channels": [{"channel_index": 0}],
    }
    prior_session._sample_rate = 100.0
    prior_session._buffer = np.ones((20, 1), dtype=np.float32)
    prior_samples = [
        CurrentSample(time_sec=0.1, current_a=1.25),
        CurrentSample(time_sec=0.2, current_a=2.5),
    ]
    manager._completed_sessions[1] = prior_session
    manager._current_samples_by_config_id[1] = prior_samples
    manager._current_sample_generations[1] = prior_generation
    expected_trace = manager._current_trace_payload(
        1,
        current_meter=prior_session._config["current_meter"],
    )
    assert expected_trace is not None
    return prior_session, prior_generation, prior_samples, expected_trace


def _assert_completed_current_owner_unchanged(
    manager: TestManager,
    *,
    prior_session,
    prior_generation,
    prior_samples,
    expected_trace,
):
    assert manager._completed_sessions.get(1) is prior_session
    assert manager._current_samples_by_config_id.get(1) is prior_samples
    assert manager._current_sample_generations.get(1) is prior_generation
    assert manager._current_trace_payload(
        1,
        current_meter=prior_session._config["current_meter"],
    ) == expected_trace
    assert manager._current_trace_payload(
        1,
        current_meter=prior_session._config["current_meter"],
        session=prior_session,
    ) == expected_trace


class TestGenerationRetirement:
    @pytest.mark.parametrize(
        "failure_phase",
        ["setup", "current_start", "progress", "acceptance", "recorder_start"],
    )
    def test_failed_replacement_preserves_completed_current_owner_as_one_unit(
        self,
        monkeypatch,
        failure_phase,
    ):
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                if failure_phase == "current_start":
                    raise RuntimeError("current collector failed")
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return []

        bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
        prior_session, prior_generation, prior_samples, expected_trace = (
            _seed_completed_current_owner(manager)
        )

        if failure_phase == "setup":
            monkeypatch.setattr(
                _FakeSession,
                "_setup_pipelines",
                lambda self: (_ for _ in ()).throw(RuntimeError("setup failed")),
            )
        elif failure_phase == "progress":
            bus.recording_progress.emit.side_effect = RuntimeError("progress failed")
        elif failure_phase == "recorder_start":
            monkeypatch.setattr(
                _StartRecorder,
                "start",
                lambda self: (_ for _ in ()).throw(RuntimeError("recorder failed")),
            )

        on_start_accepted = None
        if failure_phase == "acceptance":
            on_start_accepted = lambda _run_id: (_ for _ in ()).throw(
                RuntimeError("acceptance failed")
            )

        assert manager.start(
            {"config_id": 1, "sn": "SN2"},
            on_start_accepted=on_start_accepted,
        ) is False

        _assert_completed_current_owner_unchanged(
            manager,
            prior_session=prior_session,
            prior_generation=prior_generation,
            prior_samples=prior_samples,
            expected_trace=expected_trace,
        )

    def test_successful_replacement_retires_prior_owner_and_matching_trace_atomically(
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
                return []

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        prior_session, prior_generation, prior_samples, expected_trace = (
            _seed_completed_current_owner(manager)
        )
        accepted_snapshot = []

        def accepted(_run_id):
            accepted_snapshot.append(
                (
                    manager._completed_sessions.get(1),
                    manager._current_samples_by_config_id.get(1),
                    manager._current_sample_generations.get(1),
                    manager._current_trace_payload(
                        1,
                        current_meter=prior_session._config["current_meter"],
                    ),
                )
            )

        assert manager.start(
            {"config_id": 1, "sn": "SN2"},
            on_start_accepted=accepted,
        ) is True
        assert accepted_snapshot == [
            (prior_session, prior_samples, prior_generation, expected_trace)
        ]
        assert 1 not in manager._completed_sessions
        assert 1 not in manager._current_samples_by_config_id
        assert 1 not in manager._current_sample_generations

        replacement_generation = manager._session_generations[1]
        replacement_samples = [CurrentSample(time_sec=0.3, current_a=3.75)]
        manager._current_samples_by_config_id[1] = replacement_samples
        manager._current_sample_generations[1] = replacement_generation
        with manager._lifecycle_lock:
            manager._retire_current_samples_locked(1, prior_generation)
        assert manager._current_samples_by_config_id[1] is replacement_samples
        assert manager._current_sample_generations[1] is replacement_generation

        manager.stop(config_id=1, silent=True)

    def test_collector_start_reentrant_completion_retains_trace_once(self, monkeypatch):
        holder = {}
        collectors = []
        tagged_samples = [CurrentSample(time_sec=0.1, current_a=4.5)]

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                session = holder["manager"]._sessions[1]
                session._captured_frame_count = 1
                session._on_session_done_cb(1)
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return tagged_samples

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        reviews = []
        manager._emit_auto_analysis_review = (
            lambda config_id, session, **_kwargs: reviews.append(
                manager._current_trace_payload(
                    config_id,
                    current_meter=session._config["current_meter"],
                )
            )
            or True
        )

        assert manager.start({"config_id": 1, "sn": "SN1"}) is True

        assert len(reviews) == 1
        assert reviews[0]["values_a"] == [4.5]
        assert collectors[0].stop_calls == 1
        assert manager._completed_sessions.get(1) is not None
        assert manager._current_samples_by_config_id.get(1) is tagged_samples
        assert manager._current_collection_generations == {}
        assert manager._current_collector_generations == {}
        assert getattr(manager, "_pending_current_collections", {}) == {}

    def test_reentrant_replacement_completion_stages_new_trace_until_owner_commit(
        self,
        monkeypatch,
    ):
        holder = {}
        tagged_samples = [CurrentSample(time_sec=0.15, current_a=5.5)]

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0

            def start(self):
                session = holder["manager"]._sessions[1]
                session._captured_frame_count = 1
                session._on_session_done_cb(1)
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return tagged_samples

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        prior_session, prior_generation, prior_samples, expected_trace = (
            _seed_completed_current_owner(manager)
        )
        review_snapshots = []

        def review(config_id, session, **_kwargs):
            review_snapshots.append(
                {
                    "completed": manager._completed_sessions.get(config_id),
                    "public_samples": manager._current_samples_by_config_id.get(
                        config_id
                    ),
                    "public_generation": manager._current_sample_generations.get(
                        config_id
                    ),
                    "old_trace": manager._current_trace_payload(
                        config_id,
                        current_meter=prior_session._config["current_meter"],
                    ),
                    "new_trace": manager._current_trace_payload(
                        config_id,
                        current_meter=session._config["current_meter"],
                        session=session,
                    ),
                }
            )
            return True

        manager._emit_auto_analysis_review = review

        assert manager.start({"config_id": 1, "sn": "SN2"}) is True

        assert review_snapshots == [
            {
                "completed": prior_session,
                "public_samples": prior_samples,
                "public_generation": prior_generation,
                "old_trace": expected_trace,
                "new_trace": {
                    "times_sec": [0.15],
                    "values_a": [5.5],
                    "average_a": 5.5,
                    "max_a": 5.5,
                    "latest_a": 5.5,
                    "unit": "mA",
                },
            }
        ]
        replacement_session = manager._completed_sessions[1]
        replacement_generation = replacement_session._manager_generation_token
        assert replacement_session is not prior_session
        assert manager._current_samples_by_config_id[1] is tagged_samples
        assert manager._current_sample_generations[1] is replacement_generation
        assert manager._staged_current_samples_by_generation == {}

    def test_collector_start_concurrent_completion_waits_for_settle_and_retains_trace_once(
        self,
        monkeypatch,
    ):
        start_entered = threading.Event()
        release_start = threading.Event()
        completion_ingress = threading.Event()
        tagged_samples = [CurrentSample(time_sec=0.2, current_a=6.25)]
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                start_entered.set()
                assert release_start.wait(2.0)
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return tagged_samples

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        reviews = []
        manager._emit_auto_analysis_review = (
            lambda config_id, session, **_kwargs: reviews.append(
                manager._current_trace_payload(
                    config_id,
                    current_meter=session._config["current_meter"],
                )
            )
            or True
        )
        original_ingress = manager._completion_ingress_is_current_locked

        def observed_ingress(*args, **kwargs):
            completion_ingress.set()
            return original_ingress(*args, **kwargs)

        manager._completion_ingress_is_current_locked = observed_ingress
        start_result = []
        start_thread = threading.Thread(
            target=lambda: start_result.append(
                manager.start({"config_id": 1, "sn": "SN1"})
            )
        )
        start_thread.start()
        assert start_entered.wait(2.0)
        session = manager._sessions[1]
        session._captured_frame_count = 1
        completion_thread = threading.Thread(target=lambda: session._on_session_done_cb(1))
        completion_thread.start()
        assert completion_ingress.wait(2.0)
        assert reviews == []

        release_start.set()
        start_thread.join(2.0)
        completion_thread.join(2.0)

        assert not start_thread.is_alive()
        assert not completion_thread.is_alive()
        assert start_result == [True]
        assert len(reviews) == 1
        assert reviews[0]["values_a"] == [6.25]
        assert collectors[0].stop_calls == 1
        assert manager._current_samples_by_config_id.get(1) is tagged_samples
        assert manager._current_collection_generations == {}
        assert manager._current_collector_generations == {}
        assert getattr(manager, "_pending_current_collections", {}) == {}

    def test_collector_start_reentrant_factory_stop_defers_entire_stop_until_trace_is_ready(
        self,
        monkeypatch,
    ):
        holder = {}
        collectors = []
        stop_results = []
        tagged_samples = [CurrentSample(time_sec=0.25, current_a=7.75)]

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                manager = holder["manager"]
                session = manager._sessions[1]
                session._captured_frame_count = 44100
                session._run_judgment = MagicMock(return_value=_successful_judgment())
                stop_results.append(manager.stop_active_run(config_id=1))
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return tagged_samples

        bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        config = services.config_service.load_config.return_value
        config["allow_incomplete_recording_analysis"] = True
        manager._build_chart_payload = lambda *_args, **_kwargs: {}

        assert manager.start({"config_id": 1, "sn": "SN1"}) is True

        completed_session = manager._completed_sessions[1]
        trace = manager._current_trace_payload(
            1,
            current_meter=config["current_meter"],
            session=completed_session,
        )
        assert stop_results == [True]
        assert completed_session._audio_data_id == "audio-123"
        assert trace is not None
        assert trace["values_a"] == [7.75]
        assert bus.factory_runtime_auto_analysis_succeeded.emit.call_count == 1
        assert bus.status.emit.call_args_list == [
            call("Analyzing..."),
            call("Idle"),
        ]
        assert collectors[0].stop_calls == 1
        assert manager._pending_current_collections == {}
        assert manager._current_collection_generations == {}
        assert manager._current_collector_generations == {}
        assert manager._session_generations == {}

    def test_collector_stop_reentrant_completion_never_waits_on_its_owner_thread(
        self,
        monkeypatch,
    ):
        holder = {}
        collectors = []
        start_results = []
        finished = threading.Event()

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                raise RuntimeError("collector start failed")

            def stop(self):
                self.stop_calls += 1
                session = holder["manager"]._sessions[1]
                session._captured_frame_count = 1
                session._on_session_done_cb(1)

            def samples(self):
                return [CurrentSample(time_sec=0.1, current_a=8.0)]

        bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        worker = threading.Thread(
            target=lambda: (
                start_results.append(manager.start({"config_id": 1, "sn": "SN1"})),
                finished.set(),
            ),
            daemon=True,
        )
        worker.start()
        try:
            assert finished.wait(1.0), "collector.stop completion deadlocked its owner"
        finally:
            if worker.is_alive():
                pending = manager._pending_current_collections.get(1)
                if pending is not None:
                    pending.settled.set()
            worker.join(2.0)

        assert start_results == [False]
        assert collectors[0].stop_calls == 1
        assert bus.factory_runtime_auto_analysis_succeeded.emit.call_count == 0
        assert manager._completed_sessions == {}
        assert manager._current_samples_by_config_id == {}
        assert manager._pending_current_collections == {}
        _assert_no_generation_state(manager)

    @pytest.mark.parametrize("cleanup_failure", ["stop", "samples"])
    def test_pending_terminal_waits_for_cleanup_diagnostic_before_selected_stop(
        self,
        monkeypatch,
        cleanup_failure,
    ):
        start_entered = threading.Event()
        release_start = threading.Event()
        completion_ingress = threading.Event()
        completion_returned = threading.Event()
        stop_ingress = threading.Event()
        stop_entered = threading.Event()
        release_stop = threading.Event()
        stop_thread_ref = {}
        collectors = []
        start_results = []
        stop_results = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                start_entered.set()
                assert release_start.wait(2.0)
                return True

            def stop(self):
                self.stop_calls += 1
                stop_entered.set()
                assert release_stop.wait(2.0)
                if cleanup_failure == "stop":
                    raise RuntimeError("collector cleanup failed")

            def samples(self):
                if cleanup_failure == "samples":
                    raise RuntimeError("collector cleanup failed")
                return [CurrentSample(time_sec=0.2, current_a=9.5)]

        bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
        original_ingress = manager._completion_ingress_is_current_locked

        def observed_ingress(*args, **kwargs):
            completion_ingress.set()
            return original_ingress(*args, **kwargs)

        manager._completion_ingress_is_current_locked = observed_ingress
        original_pending_lookup = manager._pending_current_for_identity_locked

        def observed_pending_lookup(*args, **kwargs):
            pending = original_pending_lookup(*args, **kwargs)
            if (
                threading.current_thread() is stop_thread_ref.get("thread")
                and pending is not None
            ):
                stop_ingress.set()
            return pending

        manager._pending_current_for_identity_locked = observed_pending_lookup
        start_thread = threading.Thread(
            target=lambda: start_results.append(
                manager.start({"config_id": 1, "sn": "SN1"})
            )
        )
        start_thread.start()
        assert start_entered.wait(2.0)
        session = manager._sessions[1]
        session._captured_frame_count = 1
        completion_thread = threading.Thread(
            target=lambda: (
                session._on_session_done_cb(1),
                completion_returned.set(),
            )
        )
        completion_thread.start()
        assert completion_ingress.wait(2.0)
        stop_thread = threading.Thread(
            target=lambda: stop_results.append(manager.stop_active_run(config_id=1))
        )
        stop_thread_ref["thread"] = stop_thread
        stop_thread.start()
        assert stop_ingress.wait(2.0)

        release_start.set()
        assert stop_entered.wait(2.0)
        try:
            assert not completion_returned.wait(0.2), (
                "completion observed pending settlement before cleanup outcome"
            )
        finally:
            release_stop.set()
            start_thread.join(2.0)
            completion_thread.join(2.0)
            stop_thread.join(2.0)

        assert not start_thread.is_alive()
        assert not completion_thread.is_alive()
        assert not stop_thread.is_alive()
        assert start_results == [False]
        assert stop_results == [True]
        assert collectors[0].stop_calls == 1
        assert bus.factory_runtime_auto_analysis_succeeded.emit.call_count == 0
        bus.factory_runtime_stopped.emit.assert_not_called()
        assert bus.factory_runtime_aborted.emit.call_count == 0
        assert bus.status.emit.call_args_list == [call("Stopped")]
        assert any(
            "collector cleanup failed" in str(log_call.args[0])
            for log_call in bus.log.emit.call_args_list
        )
        assert manager._completed_sessions == {}
        assert manager._current_samples_by_config_id == {}
        assert manager._pending_current_collections == {}
        _assert_no_generation_state(manager)

    @pytest.mark.parametrize(
        ("actions", "winner"),
        [
            (("completion", "completion"), "completion"),
            (("factory_stop", "factory_stop"), "factory_stop"),
            (("completion", "factory_stop"), "factory_stop"),
            (("factory_stop", "completion"), "factory_stop"),
            (("error", "factory_stop"), "error"),
            (("factory_stop", "error"), "error"),
        ],
    )
    def test_same_owner_pending_terminal_priority_and_deduplication(
        self,
        monkeypatch,
        actions,
        winner,
    ):
        holder = {}
        collectors = []
        action_results = []
        tagged_samples = [CurrentSample(time_sec=0.3, current_a=11.0)]

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                manager = holder["manager"]
                session = manager._sessions[1]
                session._captured_frame_count = 44100
                session._run_judgment = MagicMock(return_value=_successful_judgment())
                for action in actions:
                    if action == "completion":
                        action_results.append(session._on_session_done_cb(1))
                    elif action == "factory_stop":
                        action_results.append(
                            manager.stop_active_run(config_id=1)
                        )
                    elif action == "error":
                        action_results.append(
                            manager._on_error(
                                manager._session_error_routes[1],
                                "pending runtime failure",
                            )
                        )
                    else:
                        raise AssertionError(action)
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return tagged_samples

        bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        config = services.config_service.load_config.return_value
        config["allow_incomplete_recording_analysis"] = True
        manager._build_chart_payload = lambda *_args, **_kwargs: {}

        expected_start = winner != "error"
        assert manager.start({"config_id": 1, "sn": "SN1"}) is expected_start

        assert action_results == [
            None if action == "completion" else True
            for action in actions
        ]
        assert collectors[0].stop_calls == 1
        assert manager._pending_current_collections == {}
        assert manager._current_collectors == {}
        assert manager._current_collection_generations == {}
        assert manager._current_collector_generations == {}
        assert manager._session_generations == {}
        if winner == "error":
            assert bus.factory_runtime_auto_analysis_succeeded.emit.call_count == 0
            assert bus.factory_runtime_aborted.emit.call_count == 1
            assert manager._completed_sessions == {}
            assert manager._current_samples_by_config_id == {}
            assert bus.status.emit.call_args_list == [
                call("Error: pending runtime failure")
            ]
        else:
            assert bus.factory_runtime_auto_analysis_succeeded.emit.call_count == 1
            assert bus.factory_runtime_aborted.emit.call_count == 0
            completed_session = manager._completed_sessions[1]
            trace = manager._current_trace_payload(
                1,
                current_meter=config["current_meter"],
                session=completed_session,
            )
            assert trace is not None
            assert trace["values_a"] == [11.0]
            assert bus.status.emit.call_args_list == [
                call("Analyzing..."),
                call("Idle"),
            ]

    def test_concurrent_double_completion_and_double_stop_stress_is_at_most_once(
        self,
        monkeypatch,
    ):
        for round_index in range(5):
            start_entered = threading.Event()
            release_start = threading.Event()
            request_gate_entered = threading.Event()
            request_barrier = threading.Barrier(5)
            request_count_lock = threading.Lock()
            request_count = 0
            request_threads = []
            collectors = []
            start_results = []
            stop_results = []
            tagged_samples = [
                CurrentSample(
                    time_sec=0.4,
                    current_a=20.0 + round_index,
                )
            ]

            class Collector:
                def __init__(self, _config, *, log_callback, sample_callback):
                    self.stop_calls = 0
                    collectors.append(self)

                def start(self):
                    start_entered.set()
                    assert release_start.wait(2.0)
                    return True

                def stop(self):
                    self.stop_calls += 1

                def samples(self):
                    return tagged_samples

            bus, services, manager = _make_current_start_manager(
                monkeypatch,
                Collector,
            )
            config = services.config_service.load_config.return_value
            config["allow_incomplete_recording_analysis"] = True
            manager._build_chart_payload = lambda *_args, **_kwargs: {}
            original_pending_lookup = manager._pending_current_for_identity_locked

            def observed_pending_lookup(*args, **kwargs):
                nonlocal request_count
                pending = original_pending_lookup(*args, **kwargs)
                if threading.current_thread() in request_threads:
                    with request_count_lock:
                        request_count += 1
                        if request_count == len(request_threads):
                            request_gate_entered.set()
                return pending

            manager._pending_current_for_identity_locked = observed_pending_lookup
            start_thread = threading.Thread(
                target=lambda: start_results.append(
                    manager.start({"config_id": 1, "sn": f"SN{round_index}"})
                )
            )
            start_thread.start()
            assert start_entered.wait(2.0)
            session = manager._sessions[1]
            session._captured_frame_count = 44100
            session._run_judgment = MagicMock(return_value=_successful_judgment())

            def request_completion():
                request_barrier.wait()
                session._on_session_done_cb(1)

            def request_stop():
                request_barrier.wait()
                stop_results.append(manager.stop_active_run(config_id=1))

            request_threads.extend(
                [
                    threading.Thread(target=request_completion),
                    threading.Thread(target=request_completion),
                    threading.Thread(target=request_stop),
                    threading.Thread(target=request_stop),
                ]
            )
            for request_thread in request_threads:
                request_thread.start()
            request_barrier.wait()
            assert request_gate_entered.wait(2.0)
            with manager._lifecycle_lock:
                pending = manager._pending_current_collections[1]
                assert pending.completion_continuation is not None
                assert pending.stop_continuation is not None
            release_start.set()

            start_thread.join(2.0)
            for request_thread in request_threads:
                request_thread.join(2.0)

            assert not start_thread.is_alive()
            assert all(not thread.is_alive() for thread in request_threads)
            assert start_results == [True]
            assert sorted(stop_results) == [True, True]
            assert collectors[0].stop_calls == 1
            assert bus.factory_runtime_auto_analysis_succeeded.emit.call_count == 1
            assert bus.factory_runtime_aborted.emit.call_count == 0
            assert manager._pending_current_collections == {}
            assert manager._current_collectors == {}
            assert manager._current_collection_generations == {}
            assert manager._current_collector_generations == {}
            completed_session = manager._completed_sessions[1]
            trace = manager._current_trace_payload(
                1,
                current_meter=config["current_meter"],
                session=completed_session,
            )
            assert trace is not None
            assert trace["values_a"] == [20.0 + round_index]

    def test_concurrent_repeated_stop_claims_pending_collector_exactly_once(
        self,
        monkeypatch,
    ):
        start_entered = threading.Event()
        release_start = threading.Event()
        stop_gate_entered = threading.Event()
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                start_entered.set()
                assert release_start.wait(2.0)
                return True

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return []

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        start_result = []
        start_thread = threading.Thread(
            target=lambda: start_result.append(
                manager.start({"config_id": 1, "sn": "SN1"})
            )
        )
        start_thread.start()
        assert start_entered.wait(2.0)
        original_pending_lookup = manager._pending_current_for_identity_locked
        stop_thread = None

        def observed_pending_lookup(*args, **kwargs):
            pending = original_pending_lookup(*args, **kwargs)
            if threading.current_thread() is stop_thread and pending is not None:
                stop_gate_entered.set()
            return pending

        manager._pending_current_for_identity_locked = observed_pending_lookup
        stop_thread = threading.Thread(
            target=lambda: (
                manager.stop(config_id=1, silent=True),
                manager.stop(config_id=1, silent=True),
            )
        )
        stop_thread.start()
        assert stop_gate_entered.wait(2.0)

        release_start.set()
        start_thread.join(2.0)
        stop_thread.join(2.0)

        assert not start_thread.is_alive()
        assert not stop_thread.is_alive()
        assert start_result == [False]
        assert collectors[0].stop_calls == 1
        assert manager._pending_current_collections == {}
        assert manager._current_collectors == {}
        assert manager._current_samples_by_config_id == {}
        _assert_no_generation_state(manager)

    def test_collector_start_exception_stops_pending_collector_exactly_once(
        self,
        monkeypatch,
    ):
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                raise RuntimeError("collector startup failed")

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return []

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)

        assert manager.start({"config_id": 1, "sn": "SN1"}) is False
        assert collectors[0].stop_calls == 1
        assert getattr(manager, "_pending_current_collections", {}) == {}
        _assert_no_generation_state(manager)

    def test_reentrant_completion_followed_by_start_exception_aborts_without_review(
        self,
        monkeypatch,
    ):
        holder = {}
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                session = holder["manager"]._sessions[1]
                session._captured_frame_count = 1
                session._on_session_done_cb(1)
                raise RuntimeError("collector failed after completion ingress")

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return [CurrentSample(time_sec=0.1, current_a=9.0)]

        _, _, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        reviews = []
        manager._emit_auto_analysis_review = (
            lambda *_args, **_kwargs: reviews.append(object()) or True
        )

        assert manager.start({"config_id": 1, "sn": "SN1"}) is False
        assert reviews == []
        assert collectors[0].stop_calls == 1
        assert manager._pending_current_collections == {}
        assert manager._current_samples_by_config_id == {}
        _assert_no_generation_state(manager)

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

    def test_collector_start_false_skips_current_and_keeps_audio_run_active(
        self,
        monkeypatch,
    ):
        collectors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                collectors.append(self)

            def start(self):
                return False

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                return []

        bus, _, manager = _make_current_start_manager(monkeypatch, Collector)

        try:
            assert manager.start({"config_id": 1, "sn": "SN1"}) is True
            session = manager._sessions[1]
            recorder = manager._recorders[1]
            assert recorder.isRunning() is True
            assert manager._session_generations.get(1) is (
                session._manager_generation_token
            )
            assert collectors[0].stop_calls == 1
            assert manager._pending_current_collections == {}
            assert manager._current_collectors == {}
            assert manager._current_collection_generations == {}
            assert manager._current_collector_generations == {}
            assert manager._current_samples_by_config_id == {}
            bus.factory_runtime_aborted.emit.assert_not_called()
            assert all(
                "current collector did not start" not in str(log_call.args[0])
                for log_call in bus.log.emit.call_args_list
            )
        finally:
            manager.stop(config_id=1, silent=True)

        assert collectors[0].stop_calls == 1

    def test_real_current_collector_without_host_skips_current_and_keeps_audio_running(
        self,
        monkeypatch,
    ):
        bus, _, manager = _make_current_start_manager(
            monkeypatch,
            CurrentMeterCollector,
        )

        try:
            assert manager.start({"config_id": 1, "sn": "SN1"}) is True
            assert manager.is_running(config_id=1) is True
            assert manager._recorders[1].isRunning() is True
            assert manager._pending_current_collections == {}
            assert manager._current_collectors == {}
            assert manager._current_collection_generations == {}
            assert manager._current_collector_generations == {}
            assert any(
                "跳过采集" in str(log_call.args[0])
                for log_call in bus.log.emit.call_args_list
            )
            bus.factory_runtime_aborted.emit.assert_not_called()
        finally:
            manager.stop(config_id=1, silent=True)

    @pytest.mark.parametrize("cleanup_failure", ["stop", "samples"])
    @pytest.mark.parametrize("root_event", ["runtime", "manual", "factory"])
    def test_pending_root_event_outranks_current_cleanup_diagnostic(
        self,
        monkeypatch,
        root_event,
        cleanup_failure,
    ):
        holder = {}
        collectors = []
        cleanup_message = f"collector {cleanup_failure} boom"
        tagged_samples = [CurrentSample(time_sec=0.1, current_a=2.5)]

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                self.samples_calls = 0
                collectors.append(self)

            def start(self):
                manager = holder["manager"]
                holder["session"] = manager._sessions[1]
                holder["recorder"] = manager._recorders[1]
                if root_event == "runtime":
                    assert manager._on_error(
                        manager._session_error_routes[1],
                        "runtime boom",
                    ) is True
                elif root_event == "manual":
                    manager.stop(config_id=1, silent=False)
                else:
                    holder["session"]._captured_frame_count = 44100
                    holder["session"]._run_judgment = MagicMock(
                        return_value=_successful_judgment()
                    )
                    assert manager.stop_active_run(config_id=1) is True
                return True

            def stop(self):
                self.stop_calls += 1
                if cleanup_failure == "stop":
                    raise RuntimeError(cleanup_message)

            def samples(self):
                self.samples_calls += 1
                if cleanup_failure == "samples":
                    raise RuntimeError(cleanup_message)
                return tagged_samples

        bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
        holder["manager"] = manager
        if root_event == "factory":
            services.config_service.load_config.return_value[
                "allow_incomplete_recording_analysis"
            ] = True
            manager._build_chart_payload = lambda *_args, **_kwargs: {}

        started = manager.start({"config_id": 1, "sn": "SN1"})

        assert started is (root_event == "factory")
        assert collectors[0].stop_calls == 1
        assert collectors[0].samples_calls == 1
        assert manager._sessions == {}
        assert manager._recorders == {}
        assert manager._monitors == {}
        assert manager._session_error_routes == {}
        assert manager._pending_current_collections == {}
        assert manager._current_collectors == {}
        assert manager._session_generations == {}
        assert manager._current_collection_generations == {}
        assert manager._current_collector_generations == {}
        if root_event == "factory" and cleanup_failure == "stop":
            assert manager._current_samples_by_config_id.get(1) == tagged_samples
            assert manager._current_sample_generations.get(1) is (
                holder["session"]._manager_generation_token
            )
        else:
            assert manager._current_samples_by_config_id == {}
            assert manager._current_sample_generations == {}
        recorder = holder["recorder"]
        assert recorder.capture_owners == []
        assert len(recorder.release_capture_calls) == 1
        assert recorder.stop_calls == 1
        assert any(
            cleanup_message in str(log_call.args[0])
            for log_call in bus.log.emit.call_args_list
        )

        if root_event == "runtime":
            assert bus.status.emit.call_args_list == [call("Error: runtime boom")]
            bus.factory_runtime_aborted.emit.assert_called_once()
            abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
            assert abort_payload.error_message == "runtime boom"
            bus.factory_runtime_stopped.emit.assert_not_called()
        elif root_event == "manual":
            assert bus.status.emit.call_args_list == [call("Stopped")]
            bus.factory_runtime_aborted.emit.assert_called_once()
            abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
            assert cleanup_message not in str(abort_payload.error_message)
            bus.factory_runtime_stopped.emit.assert_not_called()
        else:
            assert bus.status.emit.call_args_list == [
                call("Analyzing..."),
                call("Idle"),
            ]
            bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()
            bus.factory_runtime_stopped.emit.assert_not_called()
            bus.factory_runtime_aborted.emit.assert_not_called()
            assert manager._completed_sessions.get(1) is holder["session"]
        if root_event != "factory":
            bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()

    @pytest.mark.parametrize("cleanup_failure", ["stop", "samples"])
    def test_registered_collector_cleanup_diagnostic_cannot_zombie_auto_completion(
        self,
        monkeypatch,
        cleanup_failure,
    ):
        collectors = []
        cleanup_message = f"collector {cleanup_failure} boom"
        tagged_samples = [CurrentSample(time_sec=0.2, current_a=7.5)]

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                self.samples_calls = 0
                collectors.append(self)

            def start(self):
                return True

            def stop(self):
                self.stop_calls += 1
                if cleanup_failure == "stop":
                    raise RuntimeError(cleanup_message)

            def samples(self):
                self.samples_calls += 1
                if cleanup_failure == "samples":
                    raise RuntimeError(cleanup_message)
                return tagged_samples

        bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
        assert manager.start({"config_id": 1, "sn": "SN1"}) is True
        session = manager._sessions[1]
        recorder = manager._recorders[1]
        generation = manager._session_generations[1]
        session._captured_frame_count = 20
        session._run_judgment = MagicMock(return_value=_successful_judgment())
        completion = session._on_session_done_cb
        completion_errors = []
        completion_returned = threading.Event()

        def complete():
            try:
                completion(1)
            except BaseException as exc:
                completion_errors.append(exc)
            finally:
                completion_returned.set()

        worker = threading.Thread(target=complete, daemon=True)
        worker.start()
        assert completion_returned.wait(2.0), "auto completion did not settle"
        worker.join(2.0)

        assert not worker.is_alive()
        assert completion_errors == []
        assert collectors[0].stop_calls == 1
        assert collectors[0].samples_calls == 1
        assert manager._sessions == {}
        assert manager._recorders == {}
        assert manager._monitors == {}
        assert manager._session_error_routes == {}
        assert manager._pending_current_collections == {}
        assert manager._current_collectors == {}
        assert manager._current_collection_generations == {}
        assert manager._current_collector_generations == {}
        assert manager._session_generations == {}
        assert manager._completed_sessions.get(1) is session
        assert recorder.capture_owners == []
        assert len(recorder.release_capture_calls) == 1
        assert recorder.stop_calls == 1
        assert any(
            cleanup_message in str(log_call.args[0])
            for log_call in bus.log.emit.call_args_list
        )
        bus.factory_runtime_auto_analysis_succeeded.emit.assert_called_once()
        bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
        bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
        bus.factory_runtime_aborted.emit.assert_not_called()
        session._run_judgment.assert_called_once()

        if cleanup_failure == "stop":
            assert manager._current_samples_by_config_id.get(1) == tagged_samples
            assert manager._current_sample_generations.get(1) is generation
        else:
            assert 1 not in manager._current_samples_by_config_id
            assert 1 not in manager._current_sample_generations

    @pytest.mark.parametrize(
        "fatal",
        [
            pytest.param(SystemExit("fatal system exit"), id="system-exit"),
            pytest.param(None, id="custom-base-exception"),
        ],
    )
    def test_collector_start_base_exception_rolls_back_then_propagates_same_object(
        self,
        monkeypatch,
        fatal,
    ):
        class Fatal(BaseException):
            pass

        if fatal is None:
            fatal = Fatal("fatal collector start")
        collectors = []
        recorders = []
        monitors = []

        class Collector:
            def __init__(self, _config, *, log_callback, sample_callback):
                self.stop_calls = 0
                self.samples_calls = 0
                collectors.append(self)

            def start(self):
                raise fatal

            def stop(self):
                self.stop_calls += 1

            def samples(self):
                self.samples_calls += 1
                return []

        class Recorder(_StartRecorder):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                recorders.append(self)

        class Monitor:
            def __init__(self, **_kwargs):
                self.error_occurred = _DummySignal()
                self.start_calls = 0
                self.stop_calls = 0
                self.wait_calls = []
                monitors.append(self)

            def feed(self, _chunk):
                return None

            def start(self):
                self.start_calls += 1

            def stop(self):
                self.stop_calls += 1

            def wait(self, timeout):
                self.wait_calls.append(timeout)
                return True

        _, services, manager = _make_current_start_manager(monkeypatch, Collector)
        services.config_service.load_config.return_value["output_device_name"] = (
            "Speaker"
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            Recorder,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioMonitor",
            Monitor,
        )

        with pytest.raises(type(fatal)) as raised:
            manager.start({"config_id": 1, "sn": "SN1"})

        assert raised.value is fatal
        assert len(collectors) == len(recorders) == len(monitors) == 1
        assert collectors[0].stop_calls == 1
        assert collectors[0].samples_calls == 1
        assert recorders[0].capture_owners == []
        assert len(recorders[0].release_capture_calls) == 1
        assert recorders[0].stop_calls == 1
        assert monitors[0].start_calls == 0
        assert monitors[0].stop_calls == 1
        assert monitors[0].wait_calls == [1000]
        assert manager._sessions == {}
        assert manager._recorders == {}
        assert manager._monitors == {}
        assert manager._shared_recorders == {}
        assert manager._session_error_routes == {}
        assert manager._pending_current_collections == {}
        assert manager._current_collectors == {}
        assert manager._current_samples_by_config_id == {}
        assert manager._staged_current_samples_by_generation == {}
        assert manager._completion_claims == set()
        _assert_no_generation_state(manager)

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
        assert manager._pending_current_collections == {}
        _assert_no_generation_state(manager)
