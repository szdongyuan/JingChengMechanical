from __future__ import annotations

import threading
from unittest.mock import MagicMock, call

import pytest

from app.service.analysis.test_manager import TestManager

from ._support import _DummySignal, _FakeSession
from .test_generation_retirement import (
    _StartRecorder,
    _assert_no_generation_state,
    _make_current_start_manager,
    _successful_judgment,
)


class Fatal(BaseException):
    pass


class _TrackingMonitor:
    instances = []

    def __init__(self, **_kwargs):
        self.error_occurred = _DummySignal()
        self.start_calls = 0
        self.stop_calls = 0
        self.wait_calls = []
        type(self).instances.append(self)

    def feed(self, _chunk):
        return None

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        return True


def _assert_runtime_maps_empty(manager: TestManager) -> None:
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
    assert manager._analyzing_sessions == set()
    assert manager._accepted_factory_stops == set()
    _assert_no_generation_state(manager)


@pytest.mark.parametrize(
    "fatal_phase",
    ["is_running", "start", "acceptance", "test_started"],
)
def test_fatal_start_transaction_rolls_back_every_registered_resource(
    monkeypatch,
    fatal_phase,
):
    fatal = Fatal(f"fatal during {fatal_phase}")
    collectors = []
    recorders = []
    accepted = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1

        def samples(self):
            self.samples_calls += 1
            return []

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.wait_calls = []
            recorders.append(self)

        def isRunning(self):
            if fatal_phase == "is_running":
                raise fatal
            return super().isRunning()

        def start(self):
            if fatal_phase == "start":
                raise fatal
            return super().start()

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    _TrackingMonitor.instances = []
    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    services.config_service.load_config.return_value["output_device_name"] = "Speaker"
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioMonitor",
        _TrackingMonitor,
    )
    if fatal_phase == "test_started":
        bus.test_started.emit.side_effect = fatal

    def on_start_accepted(run_id):
        accepted.append(run_id)
        if fatal_phase == "acceptance":
            raise fatal

    with pytest.raises(Fatal) as raised:
        manager.start(
            {"config_id": 1, "sn": "SN1", "trigger_source": "manual"},
            on_start_accepted=on_start_accepted,
        )

    assert raised.value is fatal
    assert len(accepted) == 1
    assert len(collectors) == len(recorders) == len(_TrackingMonitor.instances) == 1
    collector = collectors[0]
    recorder = recorders[0]
    monitor = _TrackingMonitor.instances[0]
    assert collector.stop_calls == collector.samples_calls == 1
    assert recorder.capture_owners == []
    assert len(recorder.release_capture_calls) == 1
    assert recorder.stop_calls == 1
    assert recorder.wait_calls == [3000]
    assert monitor.stop_calls == 1
    assert monitor.wait_calls == [1000]
    assert recorder.captured_block_ready._callbacks == []
    assert recorder.error_occurred._callbacks == []
    assert monitor.error_occurred._callbacks == []
    _assert_runtime_maps_empty(manager)
    if fatal_phase == "test_started":
        bus.test_started.emit.assert_called_once_with(1)
    else:
        bus.test_started.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
    expected_abort_count = 0 if fatal_phase == "acceptance" else 1
    assert bus.factory_runtime_aborted.emit.call_count == expected_abort_count


@pytest.mark.parametrize(
    ("cleanup_phase", "fatal"),
    [
        pytest.param("stop", SystemExit("fatal collector stop"), id="stop-system-exit"),
        pytest.param(
            "samples",
            KeyboardInterrupt("fatal collector samples"),
            id="samples-keyboard-interrupt",
        ),
    ],
)
def test_registered_auto_completion_propagates_collector_fatal_after_full_cleanup(
    monkeypatch,
    cleanup_phase,
    fatal,
):
    collectors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1
            if cleanup_phase == "stop":
                raise fatal

        def samples(self):
            self.samples_calls += 1
            if cleanup_phase == "samples":
                raise fatal
            return []

    bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
    assert manager.start({"config_id": 1, "sn": "SN1"}) is True
    session = manager._sessions[1]
    recorder = manager._recorders[1]
    session._captured_frame_count = 20
    session._run_judgment = MagicMock(return_value=_successful_judgment())

    with pytest.raises(type(fatal)) as raised:
        session._on_session_done_cb(1)

    assert raised.value is fatal
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    assert recorder.capture_owners == []
    assert len(recorder.release_capture_calls) == 1
    assert recorder.stop_calls == 1
    _assert_runtime_maps_empty(manager)
    session._run_judgment.assert_not_called()
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_later_samples_fatal_outranks_earlier_stop_diagnostic(monkeypatch):
    fatal = SystemExit("fatal samples after ordinary stop failure")

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1
            raise RuntimeError("ordinary stop diagnostic")

        def samples(self):
            self.samples_calls += 1
            raise fatal

    bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
    assert manager.start({"config_id": 1, "sn": "SN1"}) is True
    session = manager._sessions[1]
    session._captured_frame_count = 20
    session._run_judgment = MagicMock(return_value=_successful_judgment())

    with pytest.raises(SystemExit) as raised:
        session._on_session_done_cb(1)

    assert raised.value is fatal
    _assert_runtime_maps_empty(manager)
    session._run_judgment.assert_not_called()
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    assert any(
        "ordinary stop diagnostic" in str(log.args[0])
        for log in bus.log.emit.call_args_list
    )


@pytest.mark.parametrize("terminal_path", ["manual", "factory", "runtime"])
def test_registered_stop_or_error_propagates_collector_fatal_after_selected_outcome(
    monkeypatch,
    terminal_path,
):
    fatal = Fatal(f"fatal collector cleanup during {terminal_path}")
    collectors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1
            raise fatal

        def samples(self):
            self.samples_calls += 1
            return []

    bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
    assert manager.start(
        {"config_id": 1, "sn": "SN1", "trigger_source": "manual"}
    ) is True
    session = manager._sessions[1]
    recorder = manager._recorders[1]

    with pytest.raises(Fatal) as raised:
        if terminal_path == "manual":
            manager.stop(config_id=1, silent=False)
        elif terminal_path == "factory":
            manager.stop_active_run(config_id=1)
        else:
            manager._on_error(manager._session_error_routes[1], "runtime boom")

    assert raised.value is fatal
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    assert recorder.capture_owners == []
    assert len(recorder.release_capture_calls) == 1
    assert recorder.stop_calls == 1
    _assert_runtime_maps_empty(manager)
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_no_result.emit.assert_not_called()
    bus.factory_runtime_auto_analysis_error.emit.assert_not_called()
    bus.factory_runtime_stopped.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_called_once()
    if terminal_path == "manual":
        assert bus.status.emit.call_args_list.count(call("Stopped")) == 1
        assert bus.status.emit.call_args_list[-1] == call("Stopped")
    elif terminal_path == "runtime":
        assert bus.status.emit.call_args_list.count(call("Error: runtime boom")) == 1
        assert bus.status.emit.call_args_list[-1] == call("Error: runtime boom")
    else:
        terminal_statuses = [
            status_call
            for status_call in bus.status.emit.call_args_list
            if str(status_call.args[0]).startswith("Error:")
        ]
        assert len(terminal_statuses) == 1


def test_failed_or_skipped_pending_collector_cleanup_fatal_prevents_acceptance(
    monkeypatch,
):
    fatal = SystemExit("fatal pending collector cleanup")
    collectors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return False

        def stop(self):
            self.stop_calls += 1
            raise fatal

        def samples(self):
            self.samples_calls += 1
            return []

    bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
    accepted = []

    with pytest.raises(SystemExit) as raised:
        manager.start(
            {"config_id": 1, "sn": "SN1"},
            on_start_accepted=accepted.append,
        )

    assert raised.value is fatal
    assert accepted == []
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    _assert_runtime_maps_empty(manager)
    bus.test_started.emit.assert_not_called()
    bus.factory_runtime_aborted.emit.assert_not_called()


def test_stop_all_finishes_later_runs_before_propagating_first_fatal(monkeypatch):
    fatal = Fatal("first run collector fatal")
    collectors = []

    class Collector:
        def __init__(self, config, *, log_callback, sample_callback):
            self.is_first = not collectors
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1
            if self.is_first:
                raise fatal

        def samples(self):
            self.samples_calls += 1
            return []

    _, services, manager = _make_current_start_manager(monkeypatch, Collector)
    template = services.config_service.load_config.return_value
    services.config_service.load_config.side_effect = lambda config_id: {
        **template,
        "config_id": config_id,
    }
    assert manager.start({"config_id": 1, "sn": "SN1"}) is True
    assert manager.start({"config_id": 2, "sn": "SN2"}) is True

    with pytest.raises(Fatal) as raised:
        manager.stop(config_id=None, silent=True)

    assert raised.value is fatal
    assert [(c.stop_calls, c.samples_calls) for c in collectors] == [(1, 1), (1, 1)]
    _assert_runtime_maps_empty(manager)
    _assert_no_generation_state(manager, config_id=2)


def test_start_root_fatal_outranks_cleanup_fatal_but_all_rollback_steps_run(
    monkeypatch,
):
    root_fatal = Fatal("fatal collector start")
    cleanup_fatal = Fatal("fatal capture freeze")
    collectors = []
    recorders = []
    monitors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            raise root_fatal

        def stop(self):
            self.stop_calls += 1

        def samples(self):
            self.samples_calls += 1
            return []

    class Session(_FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.freeze_calls = 0

        def freeze_capture(self):
            self.freeze_calls += 1
            raise cleanup_fatal

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.wait_calls = []
            recorders.append(self)

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    class Monitor(_TrackingMonitor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            monitors.append(self)

    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    services.config_service.load_config.return_value["output_device_name"] = "Speaker"
    monkeypatch.setattr("app.service.analysis.test_manager.TestSession", Session)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", Monitor)

    with pytest.raises(Fatal) as raised:
        manager.start({"config_id": 1, "sn": "SN1"})

    assert raised.value is root_fatal
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    assert recorders[0].capture_owners == []
    assert len(recorders[0].release_capture_calls) == 1
    assert recorders[0].captured_block_ready._callbacks == []
    assert recorders[0].error_occurred._callbacks == []
    assert monitors[0].error_occurred._callbacks == []
    assert monitors[0].stop_calls == 1
    assert monitors[0].wait_calls == [1000]
    assert recorders[0].stop_calls == 1
    assert recorders[0].wait_calls == [3000]
    _assert_runtime_maps_empty(manager)
    assert any(
        str(cleanup_fatal) in str(log.args[0])
        for log in bus.log.emit.call_args_list
    )


class _FatalDisconnectSignal(_DummySignal):
    def __init__(self, fatal, calls):
        super().__init__()
        self._fatal = fatal
        self._calls = calls

    def disconnect(self, callback):
        self._calls.append(callback)
        super().disconnect(callback)
        raise self._fatal


def test_multiple_rollback_fatals_attempt_every_cleanup_step_and_keep_first(
    monkeypatch,
):
    freeze_fatal = Fatal("freeze fatal")
    captured_disconnect_fatal = Fatal("captured disconnect fatal")
    recorder_error_disconnect_fatal = Fatal("recorder error disconnect fatal")
    monitor_error_disconnect_fatal = Fatal("monitor error disconnect fatal")
    release_fatal = Fatal("release fatal")
    monitor_stop_fatal = Fatal("monitor stop fatal")
    monitor_wait_fatal = Fatal("monitor wait fatal")
    recorder_stop_fatal = Fatal("recorder stop fatal")
    recorder_wait_fatal = Fatal("recorder wait fatal")
    all_fatals = [
        freeze_fatal,
        captured_disconnect_fatal,
        recorder_error_disconnect_fatal,
        monitor_error_disconnect_fatal,
        release_fatal,
        monitor_stop_fatal,
        monitor_wait_fatal,
        recorder_stop_fatal,
        recorder_wait_fatal,
    ]
    disconnect_calls = {"captured": [], "recorder": [], "monitor": []}
    collectors = []
    recorders = []
    monitors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1

        def samples(self):
            self.samples_calls += 1
            return []

    class Session(_FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.freeze_calls = 0

        def freeze_capture(self):
            self.freeze_calls += 1
            raise freeze_fatal

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.captured_block_ready = _FatalDisconnectSignal(
                captured_disconnect_fatal,
                disconnect_calls["captured"],
            )
            self.error_occurred = _FatalDisconnectSignal(
                recorder_error_disconnect_fatal,
                disconnect_calls["recorder"],
            )
            self.wait_calls = []
            recorders.append(self)

        def release_capture(self, attachment):
            super().release_capture(attachment)
            raise release_fatal

        def stop(self):
            self.stop_calls += 1
            self._running = False
            raise recorder_stop_fatal

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            raise recorder_wait_fatal

    class Monitor:
        def __init__(self, **_kwargs):
            self.error_occurred = _FatalDisconnectSignal(
                monitor_error_disconnect_fatal,
                disconnect_calls["monitor"],
            )
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
            raise monitor_stop_fatal

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            raise monitor_wait_fatal

    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    services.config_service.load_config.return_value["output_device_name"] = "Speaker"
    bus.recording_progress.emit.side_effect = RuntimeError("trigger rollback")
    monkeypatch.setattr("app.service.analysis.test_manager.TestSession", Session)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", Monitor)

    with pytest.raises(Fatal) as raised:
        manager.start({"config_id": 1, "sn": "SN1"})

    assert raised.value is freeze_fatal
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    assert len(disconnect_calls["captured"]) == 1
    assert len(disconnect_calls["recorder"]) == 1
    assert len(disconnect_calls["monitor"]) == 1
    assert recorders[0].capture_owners == []
    assert len(recorders[0].release_capture_calls) == 1
    assert monitors[0].stop_calls == 1
    assert monitors[0].wait_calls == [1000]
    assert recorders[0].stop_calls == 1
    assert recorders[0].wait_calls == [3000]
    _assert_runtime_maps_empty(manager)
    logged = "\n".join(str(log.args[0]) for log in bus.log.emit.call_args_list)
    recorded = logged + "\n" + "\n".join(
        note
        for fatal in all_fatals
        for note in getattr(fatal, "__notes__", [])
    )
    for fatal in all_fatals:
        assert str(fatal) in recorded


@pytest.mark.parametrize(
    ("collector_error", "reporting_error", "expected_error"),
    [
        pytest.param(
            Fatal("collector fatal before reporting fatal"),
            Fatal("reporting fatal after collector fatal"),
            "collector",
            id="collector-fatal-before-reporting-fatal",
        ),
        pytest.param(
            Fatal("collector fatal before reporting diagnostic"),
            RuntimeError("ordinary reporting diagnostic"),
            "collector",
            id="collector-fatal-before-reporting-diagnostic",
        ),
        pytest.param(
            RuntimeError("ordinary collector diagnostic"),
            Fatal("reporting-only fatal"),
            "reporting",
            id="reporting-only-fatal",
        ),
        pytest.param(
            None,
            Fatal("successful-cleanup reporting fatal"),
            "reporting",
            id="success-reporting-fatal",
        ),
    ],
)
def test_collector_cleanup_owner_and_waiter_consume_one_frozen_reporting_result(
    monkeypatch,
    collector_error,
    reporting_error,
    expected_error,
):
    stop_entered = threading.Event()
    release_stop = threading.Event()
    waiter_entered = threading.Event()
    collectors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1
            stop_entered.set()
            assert release_stop.wait(2.0)
            if collector_error is not None:
                raise collector_error

        def samples(self):
            self.samples_calls += 1
            return []

    bus, _, manager = _make_current_start_manager(monkeypatch, Collector)
    assert manager.start({"config_id": 1, "sn": "SN1"}) is True
    session = manager._sessions[1]
    generation = manager._session_generations[1]
    with manager._lifecycle_lock:
        claim = manager._detach_current_collection_locked(
            1,
            session=session,
            generation=generation,
            retain_samples=False,
        )
    assert claim is not None

    bus.log.emit.side_effect = reporting_error
    observed = {"owner": [], "waiter": []}
    published_before_propagation = {"owner": False, "waiter": False}

    def run_cleanup(role):
        if role == "waiter":
            waiter_entered.set()
        try:
            manager._stop_current_collection_claim(claim)
        except BaseException as exc:
            observed[role].append(exc)
            published_before_propagation[role] = (
                claim._cleanup_complete.is_set()
                and claim._cleanup_result is not None
            )

    owner = threading.Thread(target=run_cleanup, args=("owner",))
    waiter = threading.Thread(target=run_cleanup, args=("waiter",))
    owner.start()
    assert stop_entered.wait(2.0)
    waiter.start()
    assert waiter_entered.wait(2.0)
    release_stop.set()
    owner.join(2.0)
    waiter.join(2.0)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    expected = collector_error if expected_error == "collector" else reporting_error
    assert observed == {"owner": [expected], "waiter": [expected]}
    assert published_before_propagation == {"owner": True, "waiter": True}
    assert claim._cleanup_result.fatal_error is expected
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1

    bus.log.emit.side_effect = None
    manager.stop(config_id=1, silent=True)


@pytest.mark.parametrize(
    "invalidation_error",
    [
        pytest.param(Fatal("route invalidation fatal"), id="fatal"),
        pytest.param(RuntimeError("route invalidation diagnostic"), id="diagnostic"),
    ],
)
def test_route_invalidation_failure_is_reported_after_independent_cleanup(
    monkeypatch,
    invalidation_error,
):
    collectors = []
    recorders = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            return True

        def stop(self):
            self.stop_calls += 1

        def samples(self):
            self.samples_calls += 1
            return []

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.wait_calls = []
            recorders.append(self)

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    _TrackingMonitor.instances = []
    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    services.config_service.load_config.return_value["output_device_name"] = "Speaker"
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioMonitor",
        _TrackingMonitor,
    )
    assert manager.start({"config_id": 1, "sn": "SN1"}) is True
    route = manager._session_error_routes[1]
    route.error_latch.invalidate = MagicMock(side_effect=invalidation_error)

    if isinstance(invalidation_error, Exception):
        manager.stop(config_id=1, silent=True)
    else:
        with pytest.raises(Fatal) as raised:
            manager.stop(config_id=1, silent=True)
        assert raised.value is invalidation_error

    assert route.error_latch.invalidate.call_count == 1
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    assert _TrackingMonitor.instances[0].stop_calls == 1
    assert _TrackingMonitor.instances[0].wait_calls == [1000]
    assert recorders[0].stop_calls == 1
    assert recorders[0].wait_calls == [3000]
    assert recorders[0].capture_owners == []
    assert len(recorders[0].release_capture_calls) == 1
    _assert_runtime_maps_empty(manager)
    assert any(
        str(invalidation_error) in str(log.args[0])
        for log in bus.log.emit.call_args_list
    )


def test_pending_factory_stop_uses_occurrence_order_and_cleans_each_resource_once(
    monkeypatch,
):
    collector_fatal = Fatal("collector fatal occurred first")
    monitor_fatal = Fatal("monitor fatal occurred later")
    holder = {}
    collectors = []
    recorders = []
    monitors = []
    stop_results = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            self.stop_calls = 0
            self.samples_calls = 0
            collectors.append(self)

        def start(self):
            stop_results.append(holder["manager"].stop_active_run(config_id=1))
            return True

        def stop(self):
            self.stop_calls += 1
            raise collector_fatal

        def samples(self):
            self.samples_calls += 1
            return []

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.wait_calls = []
            recorders.append(self)

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    class Monitor(_TrackingMonitor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            monitors.append(self)

        def stop(self):
            self.stop_calls += 1
            raise monitor_fatal

    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    holder["manager"] = manager
    services.config_service.load_config.return_value["output_device_name"] = "Speaker"
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", Monitor)

    with pytest.raises(Fatal) as raised:
        manager.start({"config_id": 1, "sn": "SN1"})

    assert raised.value is collector_fatal
    assert stop_results == [True]
    assert collectors[0].stop_calls == collectors[0].samples_calls == 1
    assert monitors[0].stop_calls == 1
    assert monitors[0].wait_calls == [1000]
    assert recorders[0].stop_calls == 1
    assert recorders[0].wait_calls == [3000]
    assert len(recorders[0].release_capture_calls) == 1
    assert any(
        str(monitor_fatal) in note
        for note in getattr(collector_fatal, "__notes__", [])
    )
    _assert_runtime_maps_empty(manager)
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    bus.factory_runtime_stopped.emit.assert_not_called()


def test_monitor_fatal_precedes_reporting_fatal_and_wait_still_runs_once(
    monkeypatch,
):
    monitor_fatal = Fatal("monitor stop fatal before reporting")
    reporting_fatal = Fatal("monitor reporting fatal later")
    recorders = []
    monitors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            raise AssertionError("disabled current meter must not create collector")

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.wait_calls = []
            recorders.append(self)

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    class Monitor(_TrackingMonitor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            monitors.append(self)

        def stop(self):
            self.stop_calls += 1
            raise monitor_fatal

    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    config = services.config_service.load_config.return_value
    config["current_meter"] = {"enabled": False}
    config["output_device_name"] = "Speaker"
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", Monitor)
    assert manager.start({"config_id": 1, "sn": "SN1"}) is True
    bus.log.emit.side_effect = reporting_fatal

    with pytest.raises(Fatal) as raised:
        manager.stop(config_id=1, silent=True)

    assert raised.value is monitor_fatal
    assert monitors[0].stop_calls == 1
    assert monitors[0].wait_calls == [1000]
    assert recorders[0].stop_calls == 1
    assert recorders[0].wait_calls == [3000]
    assert len(recorders[0].release_capture_calls) == 1
    _assert_runtime_maps_empty(manager)


@pytest.mark.parametrize(
    ("root_error", "expected_kind"),
    [
        pytest.param(
            Fatal("accepted-start root fatal"),
            "root",
            id="root-fatal-outranks-reporting-and-cleanup",
        ),
        pytest.param(
            RuntimeError("accepted-start ordinary root"),
            "reporting",
            id="earliest-reporting-fatal-outranks-later-cleanup-fatal",
        ),
    ],
)
def test_accepted_start_rollback_aggregates_logger_and_cleanup_fatals(
    monkeypatch,
    root_error,
    expected_kind,
):
    reporting_fatal = Fatal("accepted-start rollback reporting fatal")
    cleanup_fatal = Fatal("accepted-start rollback freeze fatal")
    recorders = []
    monitors = []

    class Collector:
        def __init__(self, _config, *, log_callback, sample_callback):
            raise AssertionError("disabled current meter must not create collector")

    class Session(_FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.freeze_calls = 0

        def freeze_capture(self):
            self.freeze_calls += 1
            raise cleanup_fatal

    class Recorder(_StartRecorder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.wait_calls = []
            recorders.append(self)

        def wait(self, timeout):
            self.wait_calls.append(timeout)
            return True

    class Monitor(_TrackingMonitor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            monitors.append(self)

    bus, services, manager = _make_current_start_manager(monkeypatch, Collector)
    config = services.config_service.load_config.return_value
    config["current_meter"] = {"enabled": False}
    config["output_device_name"] = "Speaker"
    monkeypatch.setattr("app.service.analysis.test_manager.TestSession", Session)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioRecorder", Recorder)
    monkeypatch.setattr("app.service.analysis.test_manager.AudioMonitor", Monitor)

    def fail_acceptance(_run_id):
        bus.log.emit.side_effect = reporting_fatal
        raise root_error

    with pytest.raises(BaseException) as raised:
        manager.start(
            {"config_id": 1, "sn": "SN1"},
            on_start_accepted=fail_acceptance,
        )

    expected = root_error if expected_kind == "root" else reporting_fatal
    assert raised.value is expected
    assert recorders[0].capture_owners == []
    assert len(recorders[0].release_capture_calls) == 1
    assert recorders[0].captured_block_ready._callbacks == []
    assert recorders[0].error_occurred._callbacks == []
    assert monitors[0].error_occurred._callbacks == []
    assert monitors[0].stop_calls == 1
    assert monitors[0].wait_calls == [1000]
    assert recorders[0].stop_calls == 1
    assert recorders[0].wait_calls == [3000]
    _assert_runtime_maps_empty(manager)
    bus.factory_runtime_auto_analysis_succeeded.emit.assert_not_called()
    bus.factory_runtime_stopped.emit.assert_not_called()
