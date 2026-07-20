from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

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


def _make_concurrent_shared_start_manager(
    monkeypatch,
    recorder_type,
    *,
    configs=None,
):
    config_load_barrier = threading.Barrier(2)
    if configs is None:
        configs = {
            config_id: {
                **_factory_runtime_config(config_id=config_id),
                "output_device_name": None,
            }
            for config_id in (1, 2)
        }
    bus = _mock_bus()
    services = _mock_services(bus)

    def load_config(config_id):
        config_load_barrier.wait(timeout=2.0)
        return dict(configs[config_id])

    services.config_service.load_config.side_effect = load_config
    monkeypatch.setattr(
        "app.service.analysis.test_manager.AudioRecorder",
        recorder_type,
    )
    monkeypatch.setattr(
        "app.service.analysis.test_manager.TestSession",
        _FakeSession,
    )
    return TestManager(bus, services), configs


def _run_two_config_starts(manager):
    results = {}
    errors = []

    def run(config_id):
        try:
            results[config_id] = manager.start(
                {"config_id": config_id, "sn": f"SN{config_id}"}
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(config_id,), daemon=True)
        for config_id in (1, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    return results


class _ConcurrentStartRecorder(_CaptureRecorderDoubleMixin):
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
        self.captured_block_ready = _DummySignal()
        self.error_occurred = _DummySignal()
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0

    def isRunning(self):
        return self._running

    def start(self):
        self.start_calls += 1
        self._running = True

    def stop(self):
        self.stop_calls += 1
        self._running = False

    def wait(self, _timeout):
        return True


class TestSharedRecorderConcurrentStart:
    def test_same_device_concurrent_start_constructs_one_shared_recorder(
        self,
        monkeypatch,
    ):
        constructor_barrier = threading.Barrier(2)
        recorders = []

        class Recorder(_ConcurrentStartRecorder):
            def __init__(self, **kwargs):
                try:
                    constructor_barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
                super().__init__(**kwargs)
                recorders.append(self)

        manager, configs = _make_concurrent_shared_start_manager(
            monkeypatch,
            Recorder,
        )
        results = _run_two_config_starts(manager)
        device_key = manager._device_key(configs[1])

        try:
            assert results == {1: True, 2: True}
            assert len(recorders) == 1
            assert manager._recorders == {1: recorders[0], 2: recorders[0]}
            assert manager._shared_recorders == {device_key: recorders[0]}
            assert len(recorders[0].capture_owners) == 2
            assert len(recorders[0].captured_block_ready._callbacks) == 2
            assert recorders[0].start_calls == 1
        finally:
            manager.stop(silent=True)

    def test_same_recorder_concurrent_start_calls_qthread_start_once(
        self,
        monkeypatch,
    ):
        running_barrier = threading.Barrier(2)

        class Recorder(_ConcurrentStartRecorder):
            def isRunning(self):
                observed_running = self._running
                try:
                    running_barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
                return observed_running

        manager, configs = _make_concurrent_shared_start_manager(
            monkeypatch,
            Recorder,
        )
        recorder = Recorder(
            device_name="InputMic",
            hostapi="InputAPI",
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )
        device_key = manager._device_key(configs[1])
        manager._shared_recorders[device_key] = recorder

        results = _run_two_config_starts(manager)

        try:
            assert results == {1: True, 2: True}
            assert manager._recorders == {1: recorder, 2: recorder}
            assert len(recorder.capture_owners) == 2
            assert len(recorder.captured_block_ready._callbacks) == 2
            assert recorder.start_calls == 1
        finally:
            manager.stop(silent=True)

    def test_different_devices_construct_recorders_in_parallel(
        self,
        monkeypatch,
    ):
        constructor_barrier = threading.Barrier(2)
        recorders = []

        class Recorder(_ConcurrentStartRecorder):
            def __init__(self, **kwargs):
                constructor_barrier.wait(timeout=2.0)
                super().__init__(**kwargs)
                recorders.append(self)

        configs = {
            config_id: {
                **_factory_runtime_config(config_id=config_id),
                "input_device_name": f"InputMic{config_id}",
                "output_device_name": None,
            }
            for config_id in (1, 2)
        }
        manager, _ = _make_concurrent_shared_start_manager(
            monkeypatch,
            Recorder,
            configs=configs,
        )
        results = _run_two_config_starts(manager)

        try:
            assert results == {1: True, 2: True}
            assert len(recorders) == 2
            assert manager._recorders[1] is not manager._recorders[2]
            assert len(manager._shared_recorders) == 2
            assert all(recorder.start_calls == 1 for recorder in recorders)
        finally:
            manager.stop(silent=True)

    def test_start_gate_wakes_waiter_to_retry_after_owner_error(self):
        owner_start_entered = threading.Event()
        release_owner_start = threading.Event()
        second_call_entered = threading.Event()
        first_error = RuntimeError("first recorder start failed")

        class Recorder(_ConcurrentStartRecorder):
            def start(self):
                self.start_calls += 1
                if self.start_calls == 1:
                    owner_start_entered.set()
                    assert release_owner_start.wait(2.0)
                    raise first_error
                self._running = True

        bus = _mock_bus()
        services = _mock_services(bus)
        manager = TestManager(bus, services)
        recorder = Recorder(
            device_name="InputMic",
            hostapi="InputAPI",
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )
        successes = []
        errors = []

        def call_start(*, second=False):
            if second:
                second_call_entered.set()
            try:
                manager._start_recorder_once(recorder)
                successes.append(second)
            except BaseException as exc:
                errors.append(exc)

        owner_thread = threading.Thread(target=call_start, daemon=True)
        owner_thread.start()
        assert owner_start_entered.wait(2.0)
        waiter_thread = threading.Thread(
            target=lambda: call_start(second=True),
            daemon=True,
        )
        waiter_thread.start()
        assert second_call_entered.wait(2.0)
        release_owner_start.set()
        owner_thread.join(3.0)
        waiter_thread.join(3.0)

        assert not owner_thread.is_alive()
        assert not waiter_thread.is_alive()
        assert errors == [first_error]
        assert successes == [True]
        assert recorder.start_calls == 2
        assert recorder._running is True

        manager._start_recorder_once(recorder)
        assert recorder.start_calls == 2

    def test_pending_selection_prevents_cleanup_stop_and_discard(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        manager = TestManager(bus, services)
        config = {
            **_factory_runtime_config(config_id=1),
            "output_device_name": None,
        }
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            _ConcurrentStartRecorder,
        )
        device_key = manager._device_key(config)
        recorder = manager._get_or_create_shared_recorder(
            device_key,
            config=config,
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )

        manager._stop_recorder_if_unused(recorder)

        assert recorder.stop_calls == 0
        assert manager._shared_recorders == {device_key: recorder}
        assert manager._pending_shared_recorder_users[recorder] == 1

        manager._release_shared_recorder_selection(device_key, recorder)
        manager._stop_recorder_if_unused(recorder)

        assert recorder.stop_calls == 1
        assert manager._shared_recorders == {}
        assert dict(manager._pending_shared_recorder_users) == {}

    @pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
    def test_request_capture_error_releases_pending_selection(
        self,
        monkeypatch,
        error_type,
    ):
        request_error = error_type("request capture failed")
        recorders = []

        class Recorder(_ConcurrentStartRecorder):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                recorders.append(self)

            def request_capture(self, *, record_duration):
                raise request_error

        bus = _mock_bus()
        services = _mock_services(bus)
        config = {
            **_factory_runtime_config(config_id=1),
            "output_device_name": None,
        }
        services.config_service.load_config.return_value = config
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            Recorder,
        )
        monkeypatch.setattr(
            "app.service.analysis.test_manager.TestSession",
            _FakeSession,
        )
        manager = TestManager(bus, services)

        if isinstance(request_error, Exception):
            assert manager.start({"config_id": 1, "sn": "SN-request-error"}) is False
        else:
            with pytest.raises(error_type) as exc_info:
                manager.start({"config_id": 1, "sn": "SN-request-fatal"})
            assert exc_info.value is request_error

        assert len(recorders) == 1
        assert dict(manager._pending_shared_recorder_users) == {}
        assert manager._shared_recorders == {}
        assert manager._start_reservations == {}

    def test_discard_old_recorder_preserves_identity_replacement(
        self,
        monkeypatch,
    ):
        bus = _mock_bus()
        services = _mock_services(bus)
        manager = TestManager(bus, services)
        config = {
            **_factory_runtime_config(config_id=1),
            "output_device_name": None,
        }
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            _ConcurrentStartRecorder,
        )
        device_key = manager._device_key(config)
        old_recorder = manager._get_or_create_shared_recorder(
            device_key,
            config=config,
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )
        manager._release_shared_recorder_selection(device_key, old_recorder)
        replacement = _ConcurrentStartRecorder(
            device_name="InputMic",
            hostapi="InputAPI",
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )
        with manager._lifecycle_lock:
            manager._shared_recorders[device_key] = replacement
            manager._shared_recorder_device_keys[replacement] = device_key

        manager._discard_shared_recorder(old_recorder)

        assert manager._shared_recorders == {device_key: replacement}
        manager._stop_recorder_if_unused(replacement)

    def test_fatal_stop_cleanup_preserves_new_pending_selection(
        self,
        monkeypatch,
    ):
        discard_entered = threading.Event()
        release_discard = threading.Event()
        stop_error = KeyboardInterrupt("recorder stop failed")

        class Recorder(_ConcurrentStartRecorder):
            def stop_if_no_capture_owners(self):
                raise stop_error

        bus = _mock_bus()
        services = _mock_services(bus)
        manager = TestManager(bus, services)
        config = {
            **_factory_runtime_config(config_id=1),
            "output_device_name": None,
        }
        monkeypatch.setattr(
            "app.service.analysis.test_manager.AudioRecorder",
            Recorder,
        )
        device_key = manager._device_key(config)
        recorder = manager._get_or_create_shared_recorder(
            device_key,
            config=config,
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )
        manager._release_shared_recorder_selection(device_key, recorder)
        cleanup_results = []
        original_discard = manager._discard_shared_recorder

        def delayed_discard(target, *args, **kwargs):
            discard_entered.set()
            assert release_discard.wait(2.0)
            return original_discard(target, *args, **kwargs)

        monkeypatch.setattr(
            manager,
            "_discard_shared_recorder",
            delayed_discard,
        )

        cleanup_thread = threading.Thread(
            target=lambda: cleanup_results.append(
                manager._cleanup_monitor_recorder_safely(
                    1,
                    rec=recorder,
                    context="fatal stop race",
                    excluding_config_id=1,
                )
            ),
            daemon=True,
        )
        cleanup_thread.start()
        assert discard_entered.wait(2.0)

        selected = manager._get_or_create_shared_recorder(
            device_key,
            config=config,
            sample_rate=44100,
            n_channels=2,
            record_duration=2.0,
        )
        assert selected is recorder
        assert manager._pending_shared_recorder_users[recorder] == 1

        release_discard.set()
        cleanup_thread.join(3.0)

        assert not cleanup_thread.is_alive()
        assert len(cleanup_results) == 1
        assert [failure.error for failure in cleanup_results[0]] == [stop_error]
        assert manager._shared_recorders == {device_key: recorder}

        manager._release_shared_recorder_selection(device_key, recorder)
        manager._discard_shared_recorder(recorder)


class TestSharedRecorderTeardown:
    def test_shared_recorder_not_stopped_while_other_session_uses_it(self):
        """When two sessions share a recorder, stopping one should NOT stop the recorder."""
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        shared_rec = MagicMock()
        shared_rec.stop_if_no_capture_owners.return_value = True
        mock_s1 = MagicMock()
        mock_s1._buffer = None
        mock_s2 = MagicMock()
        mock_s2._buffer = None
        mgr._sessions = {1: mock_s1, 2: mock_s2}
        mgr._recorders = {1: shared_rec, 2: shared_rec}
        mgr._shared_recorders = {"dev|MME|44100": shared_rec}
        mgr._stop_one(1)
        shared_rec.stop_if_no_capture_owners.assert_not_called()
        shared_rec.stop.assert_not_called()
        mgr._stop_one(2)
        shared_rec.stop_if_no_capture_owners.assert_called_once_with()
        shared_rec.stop.assert_not_called()

    def test_failed_factory_stop_does_not_reuse_stale_shared_recorder_on_next_start(
        self,
        monkeypatch,
    ):
        bus, _, mgr, _, recorder, _, _ = _make_running_factory_manager(monkeypatch)

        def _explode_stop():
            recorder.stop_calls += 1
            recorder._running = False
            raise RuntimeError("stop exploded")

        recorder.stop = MagicMock(side_effect=_explode_stop)

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is False
        bus.factory_runtime_aborted.emit.assert_called_once()

        recorder.start = MagicMock(side_effect=AssertionError("stale recorder reused"))

        restarted = mgr.start({"config_id": 1, "sn": "SN2", "trigger_source": "manual"})

        assert restarted is True
        assert mgr._recorders[1] is not recorder
        recorder.start.assert_not_called()
        assert bus.factory_runtime_aborted.emit.call_count == 1

    def test_monitor_stop_failure_still_stops_recorder_and_discards_shared_recorder(
        self,
        monkeypatch,
    ):
        bus, _, mgr, session, recorder, monitor, _ = _make_running_factory_manager(monkeypatch)

        def _explode_monitor_stop():
            monitor.stop_calls += 1
            raise RuntimeError("monitor stop exploded")

        monitor.stop = MagicMock(side_effect=_explode_monitor_stop)

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is False
        bus.factory_runtime_aborted.emit.assert_called_once()
        abort_payload = bus.factory_runtime_aborted.emit.call_args.args[0]
        assert abort_payload.config_id == session.config_id
        assert abort_payload.run_id == session._workorder["factory_run_id"]
        assert abort_payload.error_message == "monitor stop exploded"
        assert monitor.stop_calls == 1
        assert recorder.stop_calls == 1
        assert recorder.wait_calls == [3000]
        assert mgr._sessions == {}
        assert mgr._recorders == {}
        assert mgr._monitors == {}
        assert mgr._shared_recorders == {}

        recorder.start = MagicMock(side_effect=AssertionError("stale recorder reused"))

        restarted = mgr.start({"config_id": 1, "sn": "SN2", "trigger_source": "manual"})

        assert restarted is True
        assert mgr._recorders[1] is not recorder
        recorder.start.assert_not_called()

    def test_monitor_stop_failure_keeps_shared_recorder_mapping_when_another_owner_is_live(
        self,
        monkeypatch,
    ):
        bus, _, mgr, _, recorder, monitor, _ = _make_running_factory_manager(monkeypatch)
        other_session = MagicMock()
        other_session.config_id = 2
        mgr._sessions[2] = other_session
        mgr._recorders[2] = recorder

        def _explode_monitor_stop():
            monitor.stop_calls += 1
            raise RuntimeError("monitor stop exploded")

        monitor.stop = MagicMock(side_effect=_explode_monitor_stop)

        stopped = mgr.stop_active_run(config_id=1)

        assert stopped is False
        bus.factory_runtime_aborted.emit.assert_called_once()
        assert mgr._sessions == {2: other_session}
        assert mgr._recorders == {2: recorder}
        assert mgr._monitors == {}
        assert mgr._shared_recorders == {
            "InputMic|InputAPI|44100": recorder,
        }
        assert mgr.is_running(config_id=2) is True
        assert recorder.stop_calls == 0
        assert recorder.wait_calls == []
