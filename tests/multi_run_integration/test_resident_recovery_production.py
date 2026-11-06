from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field
import inspect
import math
import sys
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import (
    BackendCallbackStatus,
    CaptureOutcomeStatus,
    LifecycleStatus,
)
from app.service.analysis.resident_audio_mvp.contracts import MAX_CAPTURE_BINDINGS
from app.service.captured_start_dispatcher import CapturedStartDispatcher
from app.service.managed_input_contracts import (
    BackendScanId,
    HardwareEndpointIdentity,
    ManagedHardwareId,
)
from app.service.managed_input_endpoint_resolver import (
    ManagedInputEndpointResolver,
)
from app.service.managed_input_resident_adapter import (
    ResidentManagedInputAdapter,
)
from app.service.managed_input_sounddevice_backend import (
    ManagedInputSoundDeviceBackend,
    MonotonicSoundDeviceAudioBackend,
    PythonSystemMonotonicAuthority,
)


ROOT = Path(__file__).resolve().parents[2]
COMPOSITION = (
    ROOT / "app/service/multi_run_integration/production_composition.py"
)


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.001)


def _load_live_display_types():
    tree = ast.parse(COMPOSITION.read_text(encoding="utf-8"))
    wanted = {"_LiveRun", "_QueuedLiveBlock", "_ProductionLiveDisplayManager"}
    definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in wanted
    ]

    class _LogManager:
        @staticmethod
        def set_log_handler(_name):
            return SimpleNamespace(
                info=lambda *_args: None,
                error=lambda *_args: None,
            )

    namespace = {
        "__name__": __name__,
        "AudioMonitor": object,
        "AnalysisPipeline": object,
        "BackendCallbackStatus": BackendCallbackStatus,
        "FrozenCapturedRun": object,
        "LiveDisplayDispatcher": object,
        "LogManager": _LogManager,
        "THREAD_PRIORITY_ABOVE_NORMAL": 1,
        "dataclass": dataclass,
        "deque": deque,
        "dsp": SimpleNamespace(),
        "field": field,
        "math": math,
        "np": np,
        "set_current_thread_priority": lambda _priority: None,
        "threading": threading,
        "time": time,
    }
    executable = compile(
        ast.Module(definitions, type_ignores=[]),
        str(COMPOSITION),
        "exec",
    )
    exec(executable, namespace)
    return namespace["_ProductionLiveDisplayManager"]


def _load_prepare_upstream():
    tree = ast.parse(COMPOSITION.read_text(encoding="utf-8"))
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ProductionGraphFactory"
    )
    method = next(
        node
        for node in factory.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "prepare_upstream"
    )
    executable = compile(
        ast.Module([method], type_ignores=[]),
        str(COMPOSITION),
        "exec",
    )
    return executable


def _load_class_method(class_name: str, method_name: str, namespace: dict):
    tree = ast.parse(COMPOSITION.read_text(encoding="utf-8"))
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    exec(
        compile(
            ast.Module([method], type_ignores=[]),
            str(COMPOSITION),
            "exec",
        ),
        namespace,
    )
    return namespace[method_name]


def test_production_source_has_one_resident_lifecycle_owner() -> None:
    source = COMPOSITION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }

    assert class_names.isdisjoint(
        {
            "_AcceptedHeartbeat",
            "_HardwareInputRegistration",
            "_RebuildableInputHandle",
            "_LiveDisplayAudioBackend",
        }
    )
    assert "ManagedInputEndpointResolver(raw_backend)" in source
    assert "backend=audio_backend" in source
    assert "drain_observer=live_display.accept_block" in source
    assert "readiness_probe=" not in source


def test_production_capacity_compatibility_remains_two_two_six() -> None:
    signature = inspect.signature(CapturedStartDispatcher)

    assert signature.parameters["capacity"].default == 2
    assert signature.parameters["worker_count"].default == 2
    assert MAX_CAPTURE_BINDINGS == 6
    assert "_PRODUCTION_ANALYSIS_WORKER_COUNT = 2" in (
        COMPOSITION.read_text(encoding="utf-8")
    )


def test_live_display_slow_consumer_drops_oldest_at_capacity_four() -> None:
    manager_type = _load_live_display_types()
    diagnostics = []
    manager = manager_type(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )
    manager._runs_by_hardware["managed-input-1"] = {"run": object()}
    entered = threading.Event()
    release = threading.Event()
    processed = []

    def slow_first(block):
        processed.append(int(block.samples[0, 0]))
        if len(processed) == 1:
            entered.set()
            assert release.wait(1.0)

    manager._process_block = slow_first
    manager.accept_block(
        "managed-input-1",
        np.full((2, 1), 0, dtype=np.float32),
        2,
        time.monotonic_ns(),
        1_000,
    )
    assert entered.wait(1.0)
    started = time.perf_counter()
    for value in range(1, 6):
        manager.accept_block(
            "managed-input-1",
            np.full((2, 1), value, dtype=np.float32),
            2,
            time.monotonic_ns(),
            1_000,
        )
    enqueue_elapsed = time.perf_counter() - started

    assert enqueue_elapsed < 0.1
    assert len(manager._block_queue) == 4
    release.set()
    assert manager.wait_until_idle(1.0)
    assert processed == [0, 2, 3, 4, 5]
    assert len(diagnostics) == 1
    assert "live_display_queue_overflow" in diagnostics[0]
    manager._runs_by_hardware.clear()
    manager.close()


def test_live_display_exception_stays_on_display_worker() -> None:
    manager_type = _load_live_display_types()
    diagnostics = []
    manager = manager_type(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )
    manager._runs_by_hardware["managed-input-1"] = {"run": object()}
    failure_threads = []

    def fail(_block):
        failure_threads.append(threading.current_thread().name)
        raise RuntimeError("display exploded")

    manager._process_block = fail
    manager.accept_block(
        "managed-input-1",
        np.ones((2, 1), dtype=np.float32),
        2,
        time.monotonic_ns(),
        1_000,
    )

    assert manager.wait_until_idle(1.0)
    assert failure_threads == ["production-monitor-display-fanout"]
    assert diagnostics == [
        "live monitor/display fanout failed: display exploded"
    ]
    manager._runs_by_hardware.clear()
    manager.close()


def _active_display_run(*, monitor, dispatcher):
    return SimpleNamespace(
        run_id="run-close",
        hardware_id="managed-input-1",
        trigger_monotonic_ns=1,
        target_frame_count=10,
        sample_rate_hz=1_000,
        dispatcher=dispatcher,
        live_loudness_channel_index=None,
        monitor=monitor,
        processed_frames=0,
        monitor_processed_frames=0,
    )


def test_live_display_close_stops_consumer_before_joining_fanout() -> None:
    manager_type = _load_live_display_types()
    monitor_entered = threading.Event()
    monitor_stopped = threading.Event()

    class Monitor:
        def feed(self, _chunk):
            monitor_entered.set()
            assert monitor_stopped.wait(1.0)

        def stop(self):
            monitor_stopped.set()

        def wait(self, _timeout_ms):
            return True

    class Dispatcher:
        def __init__(self):
            self.stop_timeouts = []

        def submit(self, *_args, **_kwargs):
            return False

        def stop(self, timeout=1.0):
            self.stop_timeouts.append(timeout)

    monitor = Monitor()
    dispatcher = Dispatcher()
    manager = manager_type(bus=SimpleNamespace())
    manager._runs_by_hardware["managed-input-1"] = {
        "run-close": _active_display_run(
            monitor=monitor,
            dispatcher=dispatcher,
        )
    }
    manager.accept_block(
        "managed-input-1",
        np.ones((2, 1), dtype=np.float32),
        2,
        2_000_001,
        1_000,
    )
    assert monitor_entered.wait(1.0)

    started = time.perf_counter()
    result = manager.close(timeout_seconds=0.2)
    elapsed = time.perf_counter() - started

    assert result is True
    assert elapsed < 0.2
    assert monitor_stopped.is_set()
    assert len(dispatcher.stop_timeouts) == 1
    assert not manager._block_thread.is_alive()
    assert manager.close(timeout_seconds=0.2) is True
    assert len(dispatcher.stop_timeouts) == 1


def test_live_display_close_reports_bounded_non_clean_fanout() -> None:
    manager_type = _load_live_display_types()
    diagnostics = []
    entered = threading.Event()
    release = threading.Event()
    manager = manager_type(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )

    class Monitor:
        def stop(self):
            pass

        def wait(self, _timeout_ms):
            return True

    class Dispatcher:
        def stop(self, _timeout):
            pass

    manager._runs_by_hardware["managed-input-1"] = {
        "run": _active_display_run(
            monitor=Monitor(),
            dispatcher=Dispatcher(),
        )
    }

    def never_cooperates(_block):
        entered.set()
        release.wait(1.0)

    manager._process_block = never_cooperates
    manager.accept_block(
        "managed-input-1",
        np.ones((2, 1), dtype=np.float32),
        2,
        time.monotonic_ns(),
        1_000,
    )
    assert entered.wait(1.0)

    started = time.perf_counter()
    result = manager.close(timeout_seconds=0.05)
    elapsed = time.perf_counter() - started

    assert result is False
    assert elapsed < 0.2
    assert manager._block_thread.is_alive()
    assert len(diagnostics) == 1
    assert "live_display_shutdown_non_clean" in diagnostics[0]
    assert manager.close(timeout_seconds=0.05) is False
    assert len(diagnostics) == 1
    release.set()
    manager._block_thread.join(1.0)


def test_live_display_close_reports_blocked_active_dispatcher() -> None:
    manager_type = _load_live_display_types()
    diagnostics = []

    class Dispatcher:
        def __init__(self):
            self.stop_calls = 0

        def stop(self, _timeout):
            self.stop_calls += 1
            return False

    dispatcher = Dispatcher()
    manager = manager_type(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )
    manager._runs_by_hardware["managed-input-1"] = {
        "run": _active_display_run(
            monitor=None,
            dispatcher=dispatcher,
        )
    }

    result = manager.close(timeout_seconds=0.05)

    assert result is False
    assert dispatcher.stop_calls == 1
    assert len(diagnostics) == 1
    assert "live_display_shutdown_non_clean" in diagnostics[0]


def test_live_display_close_waits_for_tracked_drain_and_finishes_clean() -> None:
    manager_type = _load_live_display_types()
    drain_entered = threading.Event()
    drain_release = threading.Event()
    close_returned = threading.Event()
    close_results = []

    class Dispatcher:
        def __init__(self):
            self.stop_calls = 0

        def wait_until_idle(self, _timeout):
            drain_entered.set()
            assert drain_release.wait(1.0)
            return True

        def stop(self, _timeout=1.0):
            self.stop_calls += 1
            return True

    class Monitor:
        def __init__(self):
            self.stop_calls = 0
            self.wait_calls = 0

        def stop(self):
            self.stop_calls += 1

        def wait(self, _timeout_ms):
            self.wait_calls += 1
            return True

    dispatcher = Dispatcher()
    monitor = Monitor()
    manager = manager_type(bus=SimpleNamespace())
    run = _active_display_run(monitor=monitor, dispatcher=dispatcher)
    manager._drain_and_stop(run)
    assert drain_entered.wait(1.0)

    def close_manager():
        close_results.append(manager.close(timeout_seconds=0.5))
        close_returned.set()

    close_thread = threading.Thread(target=close_manager)
    try:
        close_thread.start()
        assert not close_returned.wait(0.05)
        drain_release.set()
        assert close_returned.wait(1.0)
        close_thread.join(1.0)

        assert close_results == [True]
        assert dispatcher.stop_calls == 1
        assert monitor.stop_calls == 1
        assert monitor.wait_calls == 1
    finally:
        drain_release.set()
        close_thread.join(1.0)


def test_live_display_close_reports_bounded_blocked_drain() -> None:
    manager_type = _load_live_display_types()
    diagnostics = []
    drain_entered = threading.Event()
    drain_release = threading.Event()
    drain_stopped = threading.Event()

    class Dispatcher:
        def __init__(self):
            self.stop_calls = 0

        def wait_until_idle(self, _timeout):
            drain_entered.set()
            drain_release.wait(1.0)
            return True

        def stop(self, _timeout=1.0):
            self.stop_calls += 1
            drain_stopped.set()
            return True

    class Monitor:
        def __init__(self):
            self.stop_calls = 0
            self.wait_calls = 0

        def stop(self):
            self.stop_calls += 1

        def wait(self, _timeout_ms):
            self.wait_calls += 1
            return True

    dispatcher = Dispatcher()
    monitor = Monitor()
    manager = manager_type(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )
    manager._drain_and_stop(
        _active_display_run(monitor=monitor, dispatcher=dispatcher)
    )
    assert drain_entered.wait(1.0)

    try:
        started = time.monotonic()
        result = manager.close(timeout_seconds=0.05)
        elapsed = time.monotonic() - started

        assert result is False
        assert elapsed < 0.2
        assert not drain_stopped.is_set()
        assert len(diagnostics) == 1
        assert "live_display_shutdown_non_clean" in diagnostics[0]
        assert manager.close(timeout_seconds=0.05) is False
        assert len(diagnostics) == 1
    finally:
        drain_release.set()
        assert drain_stopped.wait(1.0)

    assert dispatcher.stop_calls == 1
    assert monitor.stop_calls == 1
    assert monitor.wait_calls == 1


def _normal_completion_subject(live_display, events):
    class CompositionError(RuntimeError):
        pass

    real_complete = _load_class_method(
        "_RealGraph",
        "complete_ordered_shutdown",
        {"ProductionCompositionError": CompositionError},
    )
    active_complete = _load_class_method(
        "ActiveProductionComposition",
        "complete_ordered_shutdown",
        {},
    )
    real_graph = SimpleNamespace(
        _closed=False,
        _ordered_shutdown_attempted=False,
        _ordered_shutdown_failure=None,
        analysis_executor=SimpleNamespace(
            close=lambda: events.append("analysis")
        ),
        live_display=live_display,
        current_history=None,
    )
    graph = SimpleNamespace(
        complete_ordered_shutdown=lambda: real_complete(real_graph)
    )
    active = SimpleNamespace(
        _closed=False,
        _graph=graph,
        _upstream=SimpleNamespace(
            complete_ordered_shutdown=lambda: events.append("upstream")
        ),
        _prerequisites=SimpleNamespace(
            close=lambda: events.append("prerequisites")
        ),
    )
    return active_complete, active, real_graph, CompositionError


def test_normal_ordered_completion_closes_live_display_exactly_once() -> None:
    events = []
    live_display = SimpleNamespace(
        close=lambda: events.append("live_display") or True
    )
    complete, active, real_graph, _error_type = _normal_completion_subject(
        live_display,
        events,
    )

    complete(active)
    complete(active)

    assert events == [
        "analysis",
        "live_display",
        "upstream",
        "prerequisites",
    ]
    assert active._closed is True
    assert real_graph._closed is True


@pytest.mark.parametrize("outcome", ["false", "raises"])
def test_normal_ordered_completion_reports_non_clean_live_display(
    outcome: str,
) -> None:
    events = []

    def close_live_display():
        events.append("live_display")
        if outcome == "raises":
            raise OSError("live display close exploded")
        return False

    complete, active, real_graph, composition_error = (
        _normal_completion_subject(
            SimpleNamespace(close=close_live_display),
            events,
        )
    )
    expected_error = OSError if outcome == "raises" else composition_error

    with pytest.raises(expected_error):
        complete(active)
    with pytest.raises(expected_error):
        complete(active)

    assert events == ["analysis", "live_display"]
    assert active._closed is False
    assert real_graph._closed is False


def test_zero_ready_prepare_preserves_original_error_when_diagnostics_fail() -> None:
    close_counts = {"resident": 0, "display": 0}
    diagnostics = []
    diagnostic_failures = []

    class RawBackend:
        def __init__(self, _module):
            pass

        def query_raw_input_endpoints(self, scan_id):
            return SimpleNamespace(
                observations=(
                    SimpleNamespace(
                        stable_backend_uid=None,
                        device_index=0,
                        hostapi_index=0,
                        descriptor=SimpleNamespace(
                            device_name="Exact Input",
                            hostapi_name="API",
                        ),
                    ),
                )
            )

    class LiveDisplay:
        def __init__(self, **_kwargs):
            pass

        def accept_block(self, *_args):
            pass

        def close(self):
            close_counts["display"] += 1
            raise OSError("display close failed")

    class Resident:
        def open_startup_streams(self, _endpoints):
            return SimpleNamespace(ready_streams=(), failures=())

        def close_all(self, timeout):
            assert timeout == 5.0
            close_counts["resident"] += 1
            raise RuntimeError("resident close failed")

    resident = Resident()
    prerequisites = SimpleNamespace(
        managed_inputs=(
            SimpleNamespace(
                hardware_id=1,
                stable_backend_uid=None,
                descriptor_device_name="Exact Input",
                descriptor_hostapi_name="API",
                descriptor_sample_rate_hz=1_000.0,
                descriptor_input_channels=1,
            ),
        ),
        configs=(object(),),
        build_sounddevice_audio_backend=lambda _module: object(),
        build_resident_adapter=lambda **_kwargs: resident,
    )
    namespace = {
        "BackendScanId": BackendScanId,
        "HardwareEndpointIdentity": HardwareEndpointIdentity,
        "ManagedHardwareId": ManagedHardwareId,
        "ManagedInputEndpointResolver": lambda backend: backend,
        "ManagedInputSoundDeviceBackend": RawBackend,
        "ManagedResidentCapturePort": object,
        "ManagedResidentCaptureRoute": object,
        "ProductionCompositionError": RuntimeError,
        "_FrozenRunFreezer": SimpleNamespace(
            _target_frames=lambda _config: 10,
        ),
        "_PreparedRealUpstream": object,
        "_ProductionLiveDisplayManager": LiveDisplay,
        "_LOGGER": SimpleNamespace(
            exception=lambda *args: diagnostic_failures.append(args),
        ),
        "sys": sys,
        "np": np,
    }
    exec(_load_prepare_upstream(), namespace)

    def failing_diagnostic(detail: str) -> None:
        diagnostics.append(detail)
        raise LookupError("diagnostic sink failed")

    factory = SimpleNamespace(
        _prepare_output_directory=lambda: None,
        _result_artifact_store=None,
        _sounddevice_module=None,
        _event_bus=None,
        _diagnostic_callback=None,
        _emit_startup_diagnostic=failing_diagnostic,
    )

    with pytest.raises(RuntimeError, match="zero ready"):
        namespace["prepare_upstream"](factory, prerequisites)

    assert close_counts == {"resident": 1, "display": 1}
    assert diagnostics == [
        "shutdown_cleanup/resident_close_failed: RuntimeError: "
        "resident close failed",
        "shutdown_cleanup/live_display_close_failed: OSError: "
        "display close failed",
    ]
    assert len(diagnostic_failures) == 2
    assert all(
        item[0] == "shutdown_cleanup/diagnostic_sink_failed %s: %s"
        for item in diagnostic_failures
    )


class _CallbackStop(Exception):
    pass


class _NativeInput:
    def __init__(self, owner, kwargs) -> None:
        self.owner = owner
        self.kwargs = kwargs
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.closed = False
        self.active = False

    def start(self) -> None:
        self.start_calls += 1
        self.active = True
        self.kwargs["callback"](
            np.zeros((10, 1), dtype=np.float32),
            10,
            SimpleNamespace(currentTime=1.0, inputBufferAdcTime=0.99),
            False,
        )

    def stop(self) -> None:
        self.stop_calls += 1
        self.active = False

    def close(self) -> None:
        self.close_calls += 1
        self.active = False
        self.closed = True

    def fault(self) -> None:
        with pytest.raises(_CallbackStop):
            self.kwargs["callback"](
                np.zeros((10, 1), dtype=np.float32),
                10,
                SimpleNamespace(currentTime=1.01, inputBufferAdcTime=1.0),
                _OverflowStatus(),
            )


class _OverflowStatus:
    input_overflow = True

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return "input overflow"


class _SoundDevice:
    CallbackStop = _CallbackStop

    def __init__(self) -> None:
        self.shifted = False
        self.natives: list[_NativeInput] = []

    def query_devices(self):
        exact = {
            "name": "Exact Input",
            "hostapi": 0,
            "default_samplerate": 1_000.0,
            "max_input_channels": 1,
        }
        if not self.shifted:
            return (exact,)
        return (
            {
                "name": "Output only",
                "hostapi": 0,
                "default_samplerate": 1_000.0,
                "max_input_channels": 0,
            },
            exact,
        )

    @staticmethod
    def query_hostapis():
        return ({"name": "API"},)

    def InputStream(self, **kwargs):
        native = _NativeInput(self, kwargs)
        self.natives.append(native)
        return native


def test_real_production_backend_fault_reaches_pool_and_replaces_poisoned_handle() -> None:
    sounddevice = _SoundDevice()
    authority = PythonSystemMonotonicAuthority()
    raw_backend = ManagedInputSoundDeviceBackend(sounddevice)
    resolver = ManagedInputEndpointResolver(raw_backend)
    audio_backend = MonotonicSoundDeviceAudioBackend(sounddevice, authority)
    adapter = ResidentManagedInputAdapter(
        backend=audio_backend,
        level_meter=lambda _samples, _rate: 0.0,
        frames_per_buffer=10,
        max_capture_frames=100,
        startup_stream_timeout_s=1.0,
        fault_close_timeout_s=1.0,
        shutdown_timeout_s=1.0,
        calibration_wait_margin_s=0.1,
        endpoint_resolver=resolver,
    )
    endpoint = HardwareEndpointIdentity(
        hardware_id=ManagedHardwareId(1),
        scan_id=BackendScanId("production-test", 1),
        device_index=0,
        hostapi_index=0,
        stable_backend_uid=None,
        device_name="Exact Input",
        hostapi_name="API",
        sample_rate_hz=1_000.0,
        input_channels=1,
    )
    startup = adapter.open_startup_streams((endpoint,))
    assert len(startup.ready_streams) == 1
    pool = adapter._pool
    assert pool is not None
    binding = pool.reserve_capture("run-production-fault", "managed-input-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=time.monotonic_ns(),
        target_frame_count=100,
    )
    original = sounddevice.natives[0]

    sounddevice.shifted = True
    original.fault()

    failed = completion.wait(1.0)
    assert failed is not None
    assert failed.status is CaptureOutcomeStatus.FAILED
    _wait_for(lambda: len(sounddevice.natives) == 2)
    replacement = sounddevice.natives[1]
    assert replacement is not original
    assert replacement.kwargs["device"] == 1
    assert original.start_calls == 1
    readiness = pool.begin_readiness(
        "managed-input-1", time.monotonic_ns() + 1_000_000_000
    ).future.wait(1.0)
    assert readiness is not None
    assert readiness.status in (
        LifecycleStatus.GENERATION_REPLACED,
        LifecycleStatus.GENERATION_CONTINUOUS,
    )
    adapter.close_all(1.0)
