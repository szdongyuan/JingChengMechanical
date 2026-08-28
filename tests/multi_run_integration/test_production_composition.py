from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import time
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from app.service.managed_input_sounddevice_backend import (
    MonotonicSoundDeviceAudioBackend,
    PythonSystemMonotonicAuthority,
)
from app.service.managed_input_contracts import (
    InputEndpointDescriptor,
    ResidentOpenFailure,
    ResidentReadyStream,
    ResidentStartupOpenResult,
    ResidentStreamGenerationStamp,
)
from app.service.multi_run_integration.production_prerequisites import (
    PreparedProductionPrerequisites,
)
from app.service.multi_run_integration.production_composition import (
    ActiveProductionComposition,
    PreparedProductionComposition,
    ProductionCompositionBuilder,
    ProductionCompositionError,
    ProductionGraphFactory,
    ProductionCurrentCollectorFactory,
    _ProductionLiveDisplayManager,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    FrozenPayload,
    TriggerSource,
)
from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackStatus,
)
from tests.multi_run_coordinator_mvp._support import (
    canonical_payload,
    captured_request,
    frozen_captured,
)


class _Prerequisites:
    selection_generation = 7
    selected_config_ids = (11,)
    configs = (
        SimpleNamespace(
            config_id=11,
            values={"plc_id": 7},
        ),
    )
    managed_inputs = ("frozen-hardware",)
    golden_artifacts = ("frozen-golden",)
    monotonic_authority = SimpleNamespace(
        monotonic_domain="python/system-monotonic",
        monotonic=lambda: 10.0,
        monotonic_ns=lambda: 10_000_000_000,
    )

    def __init__(self, trace):
        self.trace = trace
        self.validate_calls = 0
        self.close_calls = 0

    def validate_activation(self):
        self.validate_calls += 1
        self.trace.append("selection-revalidated")

    def close(self):
        self.close_calls += 1
        self.trace.append("lease-released")


class _PrerequisiteProvider:
    def __init__(self, prepared):
        self.prepared = prepared
        self.prepare_calls = 0

    def prepare(self, *, monotonic_authority):
        self.prepare_calls += 1
        assert monotonic_authority is self.prepared.monotonic_authority
        return self.prepared


class _Upstream:
    def __init__(self, trace):
        self.trace = trace
        self.close_calls = 0
        self.streams = (object(),)

    def close(self):
        self.close_calls += 1
        self.trace.append("upstream-closed")


class _GraphFactory:
    def __init__(self, trace, *, fail_activation=False):
        self.trace = trace
        self.fail_activation = fail_activation
        self.upstream = _Upstream(trace)
        self.activate_calls = []
        self.coordinator_constructed = False
        self.graph_close_calls = 0

    def prepare_upstream(self, prerequisites):
        self.trace.append("upstream-prepared")
        assert tuple(item.config_id for item in prerequisites.configs) == (11,)
        return self.upstream

    def activate(self, prerequisites, upstream, history_sink):
        self.trace.append("activate")
        self.activate_calls.append((prerequisites, upstream, history_sink))
        if self.fail_activation:
            raise RuntimeError("activation exploded")
        self.coordinator_constructed = True
        graph = SimpleNamespace(
            coordinator=object(),
            scheduler_bridge=object(),
            save_dispatcher=object(),
            resident_capture=object(),
            managed_resident=upstream,
            plc_dispatch_ports=object(),
            plc_result_sequencer=object(),
            playback=object(),
            history_sink=history_sink,
            streams=upstream.streams,
        )
        graph.close = self._close_graph
        return graph

    def _close_graph(self):
        self.graph_close_calls += 1


def _builder(trace, *, fail_activation=False):
    prerequisites = _Prerequisites(trace)
    provider = _PrerequisiteProvider(prerequisites)
    factory = _GraphFactory(trace, fail_activation=fail_activation)
    return (
        ProductionCompositionBuilder(
            prerequisites=provider,
            graph_factory=factory,
            monotonic_authority=prerequisites.monotonic_authority,
        ),
        prerequisites,
        factory,
    )


def test_prepare_has_no_coordinator_and_keeps_ingress_closed():
    trace = []
    builder, prerequisites, factory = _builder(trace)

    prepared = builder.prepare()

    assert prepared.coordinator is None
    assert prepared.ingress_open is False
    assert prepared.selected_config_ids == (11,)
    assert prepared.config_routes == ((11, 7),)
    assert factory.coordinator_constructed is False
    assert prerequisites.close_calls == 0
    assert trace == ["upstream-prepared"]


def test_live_display_uses_resident_managed_hardware_identity_and_exact_window(
    monkeypatch,
):
    dispatchers = []

    class Dispatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.submissions = []
            self.pending_count = 0
            dispatchers.append(self)

        def start(self):
            pass

        def submit(self, chunk, **kwargs):
            self.submissions.append((np.asarray(chunk).copy(), kwargs))

        def stop(self):
            pass

        def wait_until_idle(self, _timeout):
            return True

    monkeypatch.setattr(
        "app.service.multi_run_integration.production_composition.LiveDisplayDispatcher",
        Dispatcher,
    )
    frozen = frozen_captured(captured_request(), config_id="7")
    frozen = frozen.__class__(
        run_id=frozen.run_id,
        trigger_monotonic_ns=frozen.trigger_monotonic_ns,
        source=frozen.source,
        config_id=frozen.config_id,
        hardware_id="9",
        target_frame_count=frozen.target_frame_count,
        config_payload=canonical_payload(
            {
                "schema": "jingcheng-run-config/v1",
                "config": {
                    "config_id": 7,
                    "config_name": "A",
                    "fault_types": [],
                    "selected_channels": [0],
                    "channels": [{"channel_index": 0}],
                },
            }
        ),
        hardware_payload=frozen.hardware_payload,
        calibration_payload=frozen.calibration_payload,
        work_order_payload=frozen.work_order_payload,
        history_labels=frozen.history_labels,
        analysis_input=frozen.analysis_input,
        final_path_request=frozen.final_path_request,
        metadata=frozen.metadata,
    )
    manager = _ProductionLiveDisplayManager(bus=SimpleNamespace())
    manager.register_frozen(frozen)

    assert "managed-input-9" in manager._runs_by_hardware
    manager.accept_block(
        "managed-input-9",
        np.asarray([[1.0], [2.0]], dtype=np.float32),
        2,
        frozen.trigger_monotonic_ns + 125_000,
        16_000,
        BackendCallbackStatus.OK,
    )
    assert manager.wait_until_idle(1.0)

    assert len(dispatchers) == 1
    assert len(dispatchers[0].submissions) == 1
    chunk, submission = dispatchers[0].submissions[0]
    np.testing.assert_allclose(chunk[:, 0], [1.0, 2.0])
    assert submission["sample_start"] == 0
    assert submission["sample_end"] == 2
    manager.close()


def test_live_display_uses_frozen_calibration_and_feeds_configured_monitor(
    monkeypatch,
):
    dispatchers = []
    monitors = []

    class Dispatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.pending_count = 0
            dispatchers.append(self)

        def start(self):
            pass

        def submit(self, _chunk, **_kwargs):
            pass

        def stop(self):
            pass

        def wait_until_idle(self, _timeout):
            return True

    class Monitor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chunks = []
            self.feed_threads = []
            self.started = False
            self.stopped = False
            self.error_occurred = SimpleNamespace(connect=lambda _slot: None)
            monitors.append(self)

        def start(self):
            self.started = True

        def feed(self, chunk):
            self.feed_threads.append(threading.current_thread().name)
            self.chunks.append(np.asarray(chunk).copy())
            return True

        def stop(self):
            self.stopped = True

        def wait(self, _timeout_ms):
            return True

    monkeypatch.setattr(
        "app.service.multi_run_integration.production_composition.LiveDisplayDispatcher",
        Dispatcher,
    )
    monkeypatch.setattr(
        "app.service.multi_run_integration.production_composition.AudioMonitor",
        Monitor,
    )
    frozen = frozen_captured(captured_request(), config_id="7")
    frozen = frozen.__class__(
        run_id=frozen.run_id,
        trigger_monotonic_ns=frozen.trigger_monotonic_ns,
        source=frozen.source,
        config_id=frozen.config_id,
        hardware_id="9",
        target_frame_count=frozen.target_frame_count,
        config_payload=canonical_payload(
            {
                "schema": "jingcheng-run-config/v1",
                "config": {
                    "config_id": 7,
                    "config_name": "A",
                    "fault_types": [],
                    "selected_channels": [1],
                    "channels": [{"channel_index": 1}],
                    "output_device_name": "Speakers",
                    "output_device_hostapi": "Windows MME",
                    "output_device_samplerate": 16_000.0,
                    "monitor_channel_index": 1,
                    "monitor_gain_db": 12.0,
                },
            }
        ),
        hardware_payload=frozen.hardware_payload,
        calibration_payload=canonical_payload(
            {
                "hardware_id": 9,
                "calibration_revision": 2,
                "channels": [
                    [0, True, 1, 1.0],
                    [1, True, 2, 2.5],
                ],
            }
        ),
        work_order_payload=frozen.work_order_payload,
        history_labels=frozen.history_labels,
        analysis_input=frozen.analysis_input,
        final_path_request=frozen.final_path_request,
        metadata=frozen.metadata,
    )
    manager = _ProductionLiveDisplayManager(bus=SimpleNamespace())

    manager.register_frozen(frozen)
    manager.accept_block(
        "managed-input-9",
        np.asarray([[1.0, 10.0], [2.0, 20.0]], dtype=np.float32),
        2,
        frozen.trigger_monotonic_ns + 125_000,
        16_000,
        BackendCallbackStatus.OK,
    )
    assert manager.wait_until_idle(1.0)

    assert dispatchers[0].kwargs["pipelines"][0]._mic_sensitivity == 2.5
    assert monitors[0].kwargs == {
        "device_name": "Speakers",
        "hostapi": "Windows MME",
        "sample_rate": 16_000.0,
        "monitor_channel": 1,
        "gain_db": 12.0,
    }
    assert monitors[0].started is True
    np.testing.assert_allclose(
        monitors[0].chunks[0],
        [[1.0, 10.0], [2.0, 20.0]],
    )
    assert monitors[0].feed_threads == [
        "production-monitor-display-fanout"
    ]
    manager.close()


def test_live_display_fanout_never_runs_on_input_callback_thread(
    monkeypatch,
):
    manager = _ProductionLiveDisplayManager(bus=SimpleNamespace())
    manager._runs_by_hardware["managed-input-9"] = {"run": object()}
    processing_started = threading.Event()
    release_processing = threading.Event()

    def slow_process(_block):
        processing_started.set()
        release_processing.wait(1.0)

    monkeypatch.setattr(manager, "_process_block", slow_process)
    callback_finished = threading.Event()

    def callback():
        manager.accept_block(
            "managed-input-9",
            np.ones((2, 1), dtype=np.float32),
            2,
            time.monotonic_ns(),
            16_000,
            BackendCallbackStatus.OK,
        )
        callback_finished.set()

    callback_thread = threading.Thread(target=callback)
    callback_thread.start()
    assert callback_finished.wait(0.2)
    assert processing_started.wait(1.0)
    release_processing.set()
    callback_thread.join(1.0)
    assert manager.wait_until_idle(1.0)
    manager._runs_by_hardware.clear()
    manager.close()


def test_live_display_queue_drops_oldest_block_at_fixed_capacity_four(
    monkeypatch,
):
    diagnostics = []
    manager = _ProductionLiveDisplayManager(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )
    manager._runs_by_hardware["managed-input-9"] = {"run": object()}
    processing_started = threading.Event()
    release_processing = threading.Event()
    processed = []

    def slow_first(block):
        processed.append(int(block.samples[0, 0]))
        if len(processed) == 1:
            processing_started.set()
            assert release_processing.wait(1.0)

    monkeypatch.setattr(manager, "_process_block", slow_first)
    manager.accept_block(
        "managed-input-9",
        np.full((2, 1), 0, dtype=np.float32),
        2,
        time.monotonic_ns(),
        16_000,
    )
    assert processing_started.wait(1.0)

    for value in range(1, 6):
        manager.accept_block(
            "managed-input-9",
            np.full((2, 1), value, dtype=np.float32),
            2,
            time.monotonic_ns(),
            16_000,
        )

    assert len(manager._block_queue) == 4
    release_processing.set()
    assert manager.wait_until_idle(1.0)
    assert processed == [0, 2, 3, 4, 5]
    assert len(diagnostics) == 1
    assert "live_display_queue_overflow" in diagnostics[0]
    assert "capacity_blocks=4" in diagnostics[0]
    manager._runs_by_hardware.clear()
    manager.close()


def test_live_display_exception_isolated_to_diagnostic(
    monkeypatch,
):
    diagnostics = []
    manager = _ProductionLiveDisplayManager(
        bus=SimpleNamespace(),
        diagnostic_callback=diagnostics.append,
    )
    manager._runs_by_hardware["managed-input-9"] = {"run": object()}

    def fail_display(_block):
        raise RuntimeError("display exploded")

    monkeypatch.setattr(manager, "_process_block", fail_display)
    manager.accept_block(
        "managed-input-9",
        np.ones((2, 1), dtype=np.float32),
        2,
        time.monotonic_ns(),
        16_000,
    )

    assert manager.wait_until_idle(1.0)
    assert diagnostics == [
        "live monitor/display fanout failed: display exploded"
    ]
    manager._runs_by_hardware.clear()
    manager.close()


def test_activate_requires_attached_history_before_coordinator_construction():
    builder, _prerequisites, factory = _builder([])
    prepared = builder.prepare()

    with pytest.raises(ProductionCompositionError, match="history"):
        prepared.activate(None)

    assert factory.coordinator_constructed is False


def test_activate_revalidates_exact_selection_generation_and_ids():
    trace = []
    builder, prerequisites, _factory = _builder(trace)
    prepared = builder.prepare()

    active = prepared.activate(SimpleNamespace(publish=lambda _event: None))

    assert isinstance(active, ActiveProductionComposition)
    assert prerequisites.validate_calls == 1
    assert trace[:3] == [
        "upstream-prepared",
        "selection-revalidated",
        "activate",
    ]


def test_prepared_and_active_graph_never_reread_frozen_prerequisite_sources():
    builder, prerequisites, factory = _builder([])
    prepared = builder.prepare()
    active = prepared.activate(SimpleNamespace(publish=lambda _event: None))

    assert factory.activate_calls[0][0] is prerequisites
    assert active.selected_config_ids == prerequisites.selected_config_ids
    assert active.streams is factory.upstream.streams


def test_selection_lease_releases_once_after_close_or_activation_failure():
    builder, prerequisites, factory = _builder([])
    prepared = builder.prepare()
    active = prepared.activate(
        SimpleNamespace(publish=lambda _event: None)
    )

    active.close()
    active.close()
    prepared.close()
    assert prerequisites.close_calls == 1
    assert factory.graph_close_calls == 1

    failed_builder, failed_prerequisites, factory = _builder(
        [],
        fail_activation=True,
    )
    prepared = failed_builder.prepare()
    with pytest.raises(ProductionCompositionError, match="activation exploded"):
        prepared.activate(SimpleNamespace(publish=lambda _event: None))
    prepared.close()

    assert factory.upstream.close_calls == 1
    assert failed_prerequisites.close_calls == 1


def test_prepared_close_re_reports_first_failure_without_retrying_cleanup():
    builder, prerequisites, factory = _builder([])
    prepared = builder.prepare()
    upstream_close_calls = []

    def fail_upstream_close():
        upstream_close_calls.append("close")
        raise RuntimeError("prepared upstream close failed")

    factory.upstream.close = fail_upstream_close

    with pytest.raises(
        RuntimeError,
        match="prepared upstream close failed",
    ):
        prepared.close()
    with pytest.raises(
        RuntimeError,
        match="prepared upstream close failed",
    ):
        prepared.close()

    assert upstream_close_calls == ["close"]
    assert prerequisites.close_calls == 1


def test_active_close_re_reports_first_failure_without_retrying_cleanup():
    builder, prerequisites, factory = _builder([])
    prepared = builder.prepare()
    active = prepared.activate(
        SimpleNamespace(publish=lambda _event: None)
    )
    graph_close_calls = []
    upstream_close_calls = []

    def fail_graph_close():
        graph_close_calls.append("close")
        raise RuntimeError("active graph close failed")

    def fail_upstream_close():
        upstream_close_calls.append("close")
        raise RuntimeError("later upstream close failed")

    active._graph.close = fail_graph_close
    factory.upstream.close = fail_upstream_close

    with pytest.raises(RuntimeError, match="active graph close failed"):
        active.close()
    with pytest.raises(RuntimeError, match="active graph close failed"):
        active.close()

    assert graph_close_calls == ["close"]
    assert upstream_close_calls == ["close"]
    assert prerequisites.close_calls == 1


def test_ordered_shutdown_completion_releases_lease_without_double_closing_graph():
    builder, prerequisites, factory = _builder([])
    prepared = builder.prepare()
    active = prepared.activate(SimpleNamespace(publish=lambda _event: None))
    active._graph.complete_ordered_shutdown = lambda: None
    active._upstream.complete_ordered_shutdown = lambda: None

    active.complete_ordered_shutdown()
    active.complete_ordered_shutdown()
    active.close()
    prepared.close()

    assert prerequisites.close_calls == 1
    assert factory.graph_close_calls == 0
    assert factory.upstream.close_calls == 0


def _isolated_stream_prepare(
    monkeypatch,
    tmp_path,
    *,
    observed_hardware_ids,
    ready_hardware_ids,
    observed_sample_rate_hz=8_000.0,
):
    import app.service.multi_run_integration.production_composition as module

    scan_observations = []
    for hardware_id in observed_hardware_ids:
        scan_observations.append(
            SimpleNamespace(
                stable_backend_uid=None,
                device_index=hardware_id - 1,
                hostapi_index=0,
                descriptor=InputEndpointDescriptor(
                    device_name=f"Input {hardware_id}",
                    hostapi_name="API",
                    sample_rate_hz=observed_sample_rate_hz,
                    input_channels=1,
                ),
            )
        )

    class RawBackend:
        def __init__(self, _sounddevice_module):
            pass

        def query_raw_input_endpoints(self, scan_id):
            for observation in scan_observations:
                observation.scan_id = scan_id
            return SimpleNamespace(observations=tuple(scan_observations))

    class Resident:
        def __init__(self):
            self.opened_endpoints = ()
            self.close_calls = 0

        def open_startup_streams(self, endpoints):
            self.opened_endpoints = endpoints
            ready = []
            failures = []
            for endpoint in endpoints:
                hardware_id = endpoint.hardware_id.value
                if hardware_id in ready_hardware_ids:
                    ready.append(
                        ResidentReadyStream(
                            endpoint=endpoint,
                            generation=ResidentStreamGenerationStamp(
                                endpoint.hardware_id,
                                1,
                                1,
                            ),
                        )
                    )
                else:
                    failures.append(
                        ResidentOpenFailure(
                            endpoint.hardware_id,
                            "resident_open_failed",
                        )
                    )
            return ResidentStartupOpenResult(tuple(ready), tuple(failures))

        def close_all(self, _timeout):
            self.close_calls += 1

    monkeypatch.setattr(module, "ManagedInputSoundDeviceBackend", RawBackend)
    managed_inputs = tuple(
        SimpleNamespace(
            hardware_id=hardware_id,
            stable_backend_uid=None,
            descriptor_device_name=f"Input {hardware_id}",
            descriptor_hostapi_name="API",
            descriptor_sample_rate_hz=8_000.0,
            descriptor_input_channels=1,
        )
        for hardware_id in (1, 2)
    )
    configs = tuple(
        SimpleNamespace(
            values={"analysis_duration": 0.1},
            managed_input=item,
        )
        for item in managed_inputs
    )
    resident = Resident()
    passive_audio_backend = object()
    adapter_calls = []

    def build_resident_adapter(**kwargs):
        adapter_calls.append(kwargs)
        return resident

    prerequisites = SimpleNamespace(
        managed_inputs=managed_inputs,
        configs=configs,
        passive_audio_backend=passive_audio_backend,
        adapter_calls=adapter_calls,
        build_sounddevice_audio_backend=(
            lambda _module: passive_audio_backend
        ),
        build_resident_adapter=build_resident_adapter,
    )
    diagnostics = []
    factory = ProductionGraphFactory(
        runtime=object(),
        plc_dispatch_ports=object(),
        plc_result_sequencer=object(),
        output_directory=tmp_path / "run_audio",
        diagnostic_callback=diagnostics.append,
    )
    return factory, prerequisites, resident, diagnostics


def test_bad_endpoint_is_diagnosed_once_while_healthy_exact_endpoint_prepares(
    monkeypatch,
    tmp_path,
):
    import app.service.multi_run_integration.production_composition as module

    factory, prerequisites, resident, diagnostics = _isolated_stream_prepare(
        monkeypatch,
        tmp_path,
        observed_hardware_ids=(2,),
        ready_hardware_ids={2},
    )

    upstream = factory.prepare_upstream(prerequisites)

    assert len(prerequisites.adapter_calls) == 1
    adapter_call = prerequisites.adapter_calls[0]
    assert adapter_call["backend"] is prerequisites.passive_audio_backend
    assert isinstance(
        adapter_call["endpoint_resolver"],
        module.ManagedInputEndpointResolver,
    )
    assert adapter_call["endpoint_resolver"]._backend.__class__.__name__ == (
        "RawBackend"
    )
    assert adapter_call["drain_observer"].__self__ is upstream.live_display
    assert tuple(
        endpoint.hardware_id.value for endpoint in resident.opened_endpoints
    ) == (2,)
    assert tuple(
        stream.endpoint.hardware_id.value for stream in upstream.streams
    ) == (2,)
    assert len(diagnostics) == 1
    assert "managed input 1" in diagnostics[0]
    assert resident.close_calls == 0


def test_stream_open_failure_is_diagnosed_without_closing_healthy_ready_stream(
    monkeypatch,
    tmp_path,
):
    factory, prerequisites, resident, diagnostics = _isolated_stream_prepare(
        monkeypatch,
        tmp_path,
        observed_hardware_ids=(1, 2),
        ready_hardware_ids={2},
    )

    upstream = factory.prepare_upstream(prerequisites)

    assert tuple(
        stream.endpoint.hardware_id.value for stream in upstream.streams
    ) == (2,)
    assert len(diagnostics) == 1
    assert "managed input 1" in diagnostics[0]
    assert resident.close_calls == 0


def test_changed_enumerated_default_rate_does_not_reject_configured_stream(
    monkeypatch,
    tmp_path,
):
    factory, prerequisites, resident, diagnostics = _isolated_stream_prepare(
        monkeypatch,
        tmp_path,
        observed_hardware_ids=(1, 2),
        ready_hardware_ids={1, 2},
        observed_sample_rate_hz=44_100.0,
    )

    upstream = factory.prepare_upstream(prerequisites)

    assert tuple(
        stream.endpoint.hardware_id.value for stream in upstream.streams
    ) == (1, 2)
    assert resident.opened_endpoints[0].sample_rate_hz == 8_000.0
    assert resident.opened_endpoints[1].sample_rate_hz == 8_000.0
    assert diagnostics == []


def test_zero_healthy_streams_fail_global_prepare_after_one_diagnostic_each(
    monkeypatch,
    tmp_path,
):
    factory, prerequisites, resident, diagnostics = _isolated_stream_prepare(
        monkeypatch,
        tmp_path,
        observed_hardware_ids=(1, 2),
        ready_hardware_ids=set(),
    )

    with pytest.raises(ProductionCompositionError, match="zero ready"):
        factory.prepare_upstream(prerequisites)

    assert len(diagnostics) == 2
    assert resident.close_calls == 1


def test_zero_exact_endpoints_fail_global_prepare_after_one_diagnostic_each(
    monkeypatch,
    tmp_path,
):
    factory, prerequisites, resident, diagnostics = _isolated_stream_prepare(
        monkeypatch,
        tmp_path,
        observed_hardware_ids=(),
        ready_hardware_ids=set(),
    )

    with pytest.raises(ProductionCompositionError, match="zero exact"):
        factory.prepare_upstream(prerequisites)

    assert len(diagnostics) == 2
    assert resident.opened_endpoints == ()
    assert resident.close_calls == 0


def _frozen_current_config(current_meter, *, plc_id=None):
    config = {
        "config_id": 11,
        "current_meter": current_meter,
    }
    if plc_id is not None:
        config["plc_id"] = plc_id
    return SimpleNamespace(
        trigger_monotonic_ns=1_500_000_000,
        config_payload=FrozenPayload(
            "config:11",
            json.dumps(
                {
                    "schema": "analysis-input/v1",
                    "config": config,
                    "channels": [],
                    "channel_mapping": {
                        "schema": "audio-channel-map/v1",
                        "channel_order": [0],
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    )


def test_production_current_factory_returns_none_only_for_explicitly_disabled_source():
    factory = ProductionCurrentCollectorFactory(
        bus=SimpleNamespace(),
        monotonic=lambda: 1.0,
    )

    collector = factory(
        "run-1",
        _frozen_current_config(
            {
                "enabled": False,
                "source": "modbus_tcp",
            }
        ),
    )

    assert collector is None


def test_production_current_factory_builds_modbus_collector_from_frozen_values(
    monkeypatch,
):
    import app.service.multi_run_integration.production_composition as module

    created = []

    class Collector:
        def __init__(self, config, **kwargs):
            created.append((config, kwargs))

        def start(self):
            return True

        def stop(self, _timeout=None):
            return None

        def samples(self):
            return []

    monkeypatch.setattr(module, "CurrentMeterCollector", Collector)
    monotonic = lambda: 2.0
    factory = ProductionCurrentCollectorFactory(
        bus=SimpleNamespace(),
        monotonic=monotonic,
    )
    frozen = _frozen_current_config(
        {
            "enabled": True,
            "source": "modbus_tcp",
            "host": "192.168.1.20",
            "port": 1502,
            "unit_id": 3,
            "register_type": "input",
            "register_address": 120,
            "register_count": 1,
            "data_type": "uint16",
            "byte_order": "little",
            "word_order": "big",
            "scale": 0.1,
            "offset": 1.5,
            "poll_interval_ms": 250,
            "timeout_ms": 1200,
        }
    )

    collector = factory("run-2", frozen)

    assert type(collector) is Collector
    assert created[0][0]["host"] == "192.168.1.20"
    assert created[0][0]["register_address"] == 120
    assert created[0][1]["time_func"] is monotonic
    assert created[0][1]["time_origin"] == pytest.approx(1.5)
    assert collector.start() is True
    collector.stop(0.5)


def test_production_current_factory_builds_plc_collector_with_frozen_binding(
    monkeypatch,
):
    import app.service.multi_run_integration.production_composition as module

    created = []

    class Collector:
        def __init__(self, config, **kwargs):
            created.append((config, kwargs))

        def start(self):
            return True

        def stop(self, _timeout=None):
            return None

        def samples(self):
            return []

    monkeypatch.setattr(module, "PlcCurrentCollector", Collector)
    bus = SimpleNamespace(plc_current_sampled=object())
    monotonic = lambda: 3.0
    factory = ProductionCurrentCollectorFactory(
        bus=bus,
        monotonic=monotonic,
    )

    collector = factory(
        "run-3",
        _frozen_current_config(
            {"enabled": True, "source": "plc"},
            plc_id=7,
        ),
    )

    assert type(collector) is Collector
    assert created[0][1]["bus"] is bus
    assert created[0][1]["plc_id"] == 7
    assert created[0][1]["time_func"] is monotonic
    assert created[0][1]["time_origin"] == pytest.approx(1.5)
    assert collector.start() is True
    collector.stop(0.5)


def test_production_current_factory_publishes_live_trace_for_the_run(
    monkeypatch,
):
    import app.service.multi_run_integration.production_composition as module

    created = []

    class Collector:
        def __init__(self, config, **kwargs):
            created.append((config, kwargs))

    monkeypatch.setattr(module, "PlcCurrentCollector", Collector)
    updated = SimpleNamespace(emit=Mock())
    bus = SimpleNamespace(
        plc_current_sampled=object(),
        current_trace_updated=updated,
    )
    collector = ProductionCurrentCollectorFactory(
        bus=bus,
        monotonic=lambda: 3.0,
    )(
        "run-current",
        _frozen_current_config(
            {"enabled": True, "source": "plc"},
            plc_id=7,
        ),
    )

    assert type(collector) is Collector
    created[0][1]["sample_callback"](
        object(),
        (
            {"time_sec": 0.0, "current_a": 1.25},
            {"time_sec": 0.2, "current_a": 1.75},
        ),
    )
    updated.emit.assert_called_once()
    config_id, run_id, payload = updated.emit.call_args.args
    assert config_id == 11
    assert run_id == "run-current"
    assert payload["times_sec"] == [0.0, 0.2]
    assert payload["values_a"] == [1.25, 1.75]
    assert payload["unit"] == "A"


def test_frozen_run_freezer_prefers_explicit_identity_and_keeps_import_fallback(
    tmp_path,
):
    import app.service.multi_run_integration.production_composition as module

    config = SimpleNamespace(config_id=11)
    freezer = module._FrozenRunFreezer(
        SimpleNamespace(configs=(config,)),
        tmp_path,
    )
    exact = SimpleNamespace(
        ingress_payload=FrozenPayload(
            "ingress",
            b'{"config":{"config_id":11}}',
        )
    )
    missing = SimpleNamespace(
        ingress_payload=FrozenPayload(
            "missing",
            b'{"config":{}}',
        )
    )

    selected, ingress = freezer._config(exact)

    assert selected is config
    assert ingress["config"] == {"config_id": 11}
    fallback, _ingress = freezer._config(missing)
    assert fallback is config


def test_frozen_run_freezer_names_audio_by_station_time_and_sn(
    monkeypatch,
    tmp_path,
):
    import app.service.multi_run_integration.production_composition as module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 15, 30, 12)

    monkeypatch.setattr(module, "datetime", FixedDatetime)
    config = SimpleNamespace(config_id=11)
    freezer = module._FrozenRunFreezer(
        SimpleNamespace(configs=(config,)),
        tmp_path,
    )

    request = freezer._final_path(
        "12345678-1234-4234-8234-123456789abc",
        config=config,
        ingress={"work_order": {"sn": "SN/001"}},
        values={"config_name": "工位2", "plc_id": 7, "config_id": 11},
        plc_id=7,
    )

    assert Path(request.final_path).name == "工位2_20260824_153012_SN_001.wav"


def test_run_freezer_loads_latest_calibration_for_each_new_run(tmp_path):
    import app.service.multi_run_integration.production_composition as module

    current_value = 1.25

    def load_hardware(hardware_id):
        assert hardware_id == 4
        return {
            "mic_cal": {
                "In1": {
                    "deviation_value": str(current_value),
                    "is_selected": 1,
                }
            }
        }

    config = SimpleNamespace(
        config_id=11,
        input_hardware_device_id=4,
        managed_input=SimpleNamespace(
            descriptor_input_channels=1,
            calibration_revision=1,
            calibration=((0, True, 1, 0.5),),
        ),
    )
    freezer = module._FrozenRunFreezer(
        SimpleNamespace(configs=(config,)),
        tmp_path,
        calibration_loader=load_hardware,
    )

    first_payload = freezer._calibration_payload(config)
    first = json.loads(first_payload.canonical_bytes)
    current_value = 2.5
    second_payload = freezer._calibration_payload(config)
    second = json.loads(second_payload.canonical_bytes)

    assert first["channels"] == [[0, True, 1, 1.25]]
    assert second["channels"] == [[0, True, 1, 2.5]]
    assert first_payload.value_id != second_payload.value_id


def test_run_freezer_snapshots_enabled_ai_toolchain_for_each_new_run(tmp_path):
    import app.service.multi_run_integration.production_composition as module

    current_toolchain = {
        "toolchain_id": "20260828-physical-ai",
        "execution_revision": "revision-1",
        "execution_mode": "resident_process",
        "entrypoint": r"C:\AI\stft_cnn_demo_runner.exe",
        "package_dir": r"C:\AI",
        "params": {
            "decision_threshold": 0.5,
            "display_name": "三段STFT异音检测",
        },
        "timeout_seconds": 30.0,
    }
    load_calls = []

    def load_toolchain(config_id):
        load_calls.append(config_id)
        return current_toolchain

    config = SimpleNamespace(config_id=11, selected_channels=(0,))
    freezer = module._FrozenRunFreezer(
        SimpleNamespace(configs=(config,)),
        tmp_path,
        ai_toolchain_loader=load_toolchain,
    )
    values = {
        "analysis_duration": 2.25,
        "analysis_window_offset_ms": 750,
    }

    first_payload = freezer._analysis_input(
        config,
        {"work_order": {"sn": "SN-001"}},
        values,
    )
    current_toolchain["execution_revision"] = "revision-2"
    current_toolchain["params"]["decision_threshold"] = 0.65
    second_payload = freezer._analysis_input(
        config,
        {"work_order": {"sn": "SN-002"}},
        values,
    )

    first = json.loads(first_payload.canonical_bytes)
    second = json.loads(second_payload.canonical_bytes)
    assert load_calls == [11, 11]
    assert first["analysis"]["toolchain"] == {
        "toolchain_id": "20260828-physical-ai",
        "execution_revision": "revision-1",
        "execution_mode": "resident_process",
        "entrypoint": r"C:\AI\stft_cnn_demo_runner.exe",
        "package_dir": r"C:\AI",
        "params": {
            "decision_threshold": 0.5,
            "display_name": "三段STFT异音检测",
        },
        "timeout_seconds": 30.0,
    }
    assert second["analysis"]["toolchain"]["execution_revision"] == "revision-2"
    assert second["analysis"]["toolchain"]["params"]["decision_threshold"] == 0.65
    assert first_payload.value_id != second_payload.value_id


def test_run_freezer_keeps_ai_disabled_when_loader_returns_none(tmp_path):
    import app.service.multi_run_integration.production_composition as module

    load_calls = []

    def load_toolchain(config_id):
        load_calls.append(config_id)
        return None

    config = SimpleNamespace(config_id=11, selected_channels=(0,))
    freezer = module._FrozenRunFreezer(
        SimpleNamespace(configs=(config,)),
        tmp_path,
        ai_toolchain_loader=load_toolchain,
    )
    payload = freezer._analysis_input(
        config,
        {"work_order": {}},
        {
            "analysis_duration": 1.0,
            "analysis_window_offset_ms": 0,
        },
    )

    frozen = json.loads(payload.canonical_bytes)
    assert load_calls == [11]
    assert frozen["analysis"]["toolchain"] is None


@pytest.mark.parametrize(
    ("analysis_duration", "record_duration", "expected_frames"),
    [
        (0.25, None, 6_000),
        (0.25, 1.0, 8_000),
        (0.25001, None, 6_001),
        (0.25, 1.00001, 8_001),
    ],
)
def test_frozen_run_target_frames_cover_recording_and_analysis_window_end(
    analysis_duration,
    record_duration,
    expected_frames,
):
    import app.service.multi_run_integration.production_composition as module

    values = {
        "analysis_duration": analysis_duration,
        "analysis_window_offset_ms": 500,
    }
    if record_duration is not None:
        values["record_duration"] = record_duration
    config = SimpleNamespace(
        values=values,
        managed_input=SimpleNamespace(
            descriptor_sample_rate_hz=8_000,
        ),
    )

    assert module._FrozenRunFreezer._target_frames(config) == expected_frames


def test_frozen_run_target_frame_ceiling_preserves_allocation_bound():
    import app.service.multi_run_integration.production_composition as module

    config = SimpleNamespace(
        values={
            "analysis_duration": module.MAX_ALLOCATABLE_FRAMES + 0.5,
        },
        managed_input=SimpleNamespace(
            descriptor_sample_rate_hz=1,
        ),
    )

    with pytest.raises(
        ValueError,
        match="capture frame count is unsupported",
    ):
        module._FrozenRunFreezer._target_frames(config)


def test_frozen_run_freezer_converts_channel_json_columns_to_runtime_values():
    import app.service.multi_run_integration.production_composition as module

    config = SimpleNamespace(
        config_id=11,
        selected_channels=(0,),
        values={
            "config_name": "test",
            "fault_types_json": "[]",
        },
        channels=(
            SimpleNamespace(
                values={
                    "channel_index": 0,
                    "fft_band_limits": "[]",
                    "oct_band_limits": '[{"kind":"upper","points":[]}]',
                    "ai_config": '{"enabled":false}',
                }
            ),
        ),
    )

    semantic = module._FrozenRunFreezer._semantic_config(config)

    channel = semantic["channels"][0]
    assert channel["fft_band_limits"] == []
    assert channel["oct_band_limits"] == [
        {"kind": "upper", "points": []}
    ]
    assert channel["ai_config"] == {"enabled": False}


def test_run_freezer_snapshots_current_dashboard_layout_at_acceptance(tmp_path):
    import app.service.multi_run_integration.production_composition as module

    original_layout = {
        "version": 2,
        "rows": 1,
        "columns": 1,
        "panels": [
            {
                "id": "old",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "selected_1",
            }
        ],
    }
    current_layout = {
        "version": 2,
        "rows": 1,
        "columns": 2,
        "panels": [
            {
                "id": "current",
                "type": "stft",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 2,
                "channel": "selected_1",
            }
        ],
    }
    later_layout = {
        "version": 2,
        "rows": 2,
        "columns": 1,
        "panels": [
            {
                "id": "later",
                "type": "spl",
                "row": 0,
                "column": 0,
                "row_span": 2,
                "column_span": 1,
                "channel": "selected_1",
            }
        ],
    }
    config = SimpleNamespace(
        config_id=11,
        input_hardware_device_id=4,
        selected_channels=(0,),
        values={
            "config_name": "test",
            "fault_types_json": "[]",
            "analysis_duration": 0.01,
            "analysis_window_offset_ms": 0,
            "result_dashboard_layout": original_layout,
        },
        channels=(
            SimpleNamespace(
                values={
                    "channel_index": 0,
                    "fft_band_limits": [],
                    "oct_band_limits": [],
                    "ai_config": {},
                }
            ),
        ),
        managed_input=SimpleNamespace(
            descriptor_sample_rate_hz=48_000,
            descriptor_input_channels=1,
            calibration_revision=1,
            calibration=((0, True, 1.0),),
            values={"hardware_id": 4},
        ),
    )
    load_calls = []
    loaded_layout = current_layout

    def load_dashboard_config(config_id):
        load_calls.append(config_id)
        return {
            "config_id": config_id,
            "selected_channels": [0],
            "analysis_duration": 99.0,
            "result_dashboard_layout": loaded_layout,
        }

    freezer = module._FrozenRunFreezer(
        SimpleNamespace(configs=(config,)),
        tmp_path,
        dashboard_config_loader=load_dashboard_config,
    )
    request = SimpleNamespace(
        run_id="00000000-0000-4000-8000-000000000001",
        trigger_monotonic_ns=10,
        source=TriggerSource.MANUAL,
        ingress_payload=canonical_payload(
            {"config": {"config_id": 11}, "work_order": {}}
        ),
        fallback_history_labels=canonical_payload({"labels": {}}),
    )

    frozen = freezer.freeze(request)
    loaded_layout = later_layout
    request.run_id = "00000000-0000-4000-8000-000000000002"
    later_frozen = freezer.freeze(request)
    run_config = json.loads(frozen.config_payload.canonical_bytes)["config"]
    analysis_config = json.loads(
        frozen.analysis_input.canonical_bytes
    )["config"]
    later_config = json.loads(
        later_frozen.config_payload.canonical_bytes
    )["config"]

    assert load_calls == [11, 11]
    assert run_config["result_dashboard_layout"] == current_layout
    assert analysis_config["result_dashboard_layout"] == current_layout
    assert later_config["result_dashboard_layout"] == later_layout
    assert run_config["analysis_duration"] == 0.01
    assert analysis_config["analysis_duration"] == 0.01


def test_real_composition_uses_one_python_system_monotonic_domain_for_all_consumers():
    builder, prerequisites, _factory = _builder([])

    prepared = builder.prepare()
    active = prepared.activate(SimpleNamespace(publish=lambda _event: None))

    assert prepared.monotonic_authority is prerequisites.monotonic_authority
    assert active.monotonic_authority is prerequisites.monotonic_authority
    assert active.monotonic_domain == "python/system-monotonic"


def test_real_composition_rejects_different_monotonic_source_without_touching_resident():
    builder, prerequisites, factory = _builder([])
    prerequisites.monotonic_authority = SimpleNamespace(
        monotonic_domain="foreign-domain",
        monotonic=lambda: 1.0,
        monotonic_ns=lambda: 1,
    )

    with pytest.raises(ProductionCompositionError, match="monotonic"):
        builder.prepare()

    assert factory.upstream.close_calls == 0
    assert prerequisites.close_calls == 0


def test_activation_failure_closes_owned_resources_once_without_fallback():
    trace = []
    builder, prerequisites, factory = _builder(trace, fail_activation=True)
    prepared = builder.prepare()

    with pytest.raises(ProductionCompositionError, match="activation exploded"):
        prepared.activate(SimpleNamespace(publish=lambda _event: None))

    assert trace == [
        "upstream-prepared",
        "selection-revalidated",
        "activate",
        "upstream-closed",
        "lease-released",
    ]
    assert factory.upstream.close_calls == 1
    assert prerequisites.close_calls == 1


def test_real_activation_failure_closes_dispatcher_scheduler_upstream_and_lease_once(
    monkeypatch,
    tmp_path,
):
    import app.service.multi_run_integration.production_composition as module

    trace = []
    scheduler_configs = []
    executor_build_kwargs = []

    class Dispatcher:
        def __init__(self, *_args, **_kwargs):
            trace.append("dispatcher-created")

        def shutdown(self, _deadline):
            trace.append("dispatcher-closed")

    class Scheduler:
        def __init__(self, **kwargs):
            trace.append("scheduler-created")
            scheduler_configs.append(kwargs["config"])

        def shutdown(self, _timeout):
            trace.append("scheduler-closed")

    monkeypatch.setattr(module, "BoundedAudioSaveDispatcher", Dispatcher)
    monkeypatch.setattr(module, "AnalysisScheduler", Scheduler)
    monkeypatch.setattr(
        module,
        "RunPersistenceSchedulerBridge",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bridge failed")),
    )
    prerequisites = _Prerequisites(trace)
    prerequisites.selected_config_ids = ()
    prerequisites.configs = ()
    def build_analysis_executor(**kwargs):
        executor_build_kwargs.append(kwargs)
        return object()

    prerequisites.build_analysis_executor = build_analysis_executor
    upstream = _Upstream(trace)
    upstream.resident_capture = object()
    upstream.managed_resident = object()
    factory = ProductionGraphFactory(
        runtime=object(),
        plc_dispatch_ports=object(),
        plc_result_sequencer=object(),
        output_directory=tmp_path,
    )
    prepared = PreparedProductionComposition(
        prerequisites=prerequisites,
        upstream=upstream,
        graph_factory=factory,
    )

    with pytest.raises(ProductionCompositionError, match="bridge failed"):
        prepared.activate(SimpleNamespace(publish=lambda _event: None))
    prepared.close()

    assert trace == [
        "selection-revalidated",
        "dispatcher-created",
        "scheduler-created",
        "scheduler-closed",
        "dispatcher-closed",
        "upstream-closed",
        "lease-released",
    ]
    assert len(scheduler_configs) == 1
    assert scheduler_configs[0].worker_count == 2
    assert executor_build_kwargs == [{"process_count": 2}]


@pytest.mark.parametrize("stream_count", [1, 6])
def test_real_prepare_opens_actual_managed_stream_count_with_exact_512(
    tmp_path,
    stream_count,
):
    output_directory = tmp_path / "missing" / "run_audio"
    assert output_directory.exists() is False

    class NativeStream:
        def __init__(self, owner, kwargs):
            self.owner = owner
            self.kwargs = kwargs

        def start(self):
            assert output_directory.is_dir()
            self.owner.started.append(self.kwargs)
            self.kwargs["callback"](
                np.zeros((512, 1), dtype=np.float32),
                512,
                None,
                False,
            )

        def stop(self):
            pass

        def wait_until_idle(self, _timeout):
            return True

        def close(self):
            pass

    class SoundDevice:
        class CallbackStop(Exception):
            pass

        def __init__(self):
            self.started = []

        def query_devices(self):
            return tuple(
                {
                    "name": f"Input {index}",
                    "hostapi": 0,
                    "default_samplerate": 8_000.0,
                    "max_input_channels": 1,
                }
                for index in range(stream_count)
            )

        @staticmethod
        def query_hostapis():
            return ({"name": "API"},)

        def InputStream(self, **kwargs):
            return NativeStream(self, kwargs)

    authority = PythonSystemMonotonicAuthority()
    sounddevice = SoundDevice()

    class Prerequisites:
        monotonic_authority = authority
        managed_inputs = tuple(
            SimpleNamespace(
                hardware_id=index + 1,
                stable_backend_uid=None,
                descriptor_device_name=f"Input {index}",
                descriptor_hostapi_name="API",
                descriptor_sample_rate_hz=8_000.0,
                descriptor_input_channels=1,
            )
            for index in range(stream_count)
        )
        configs = tuple(
            SimpleNamespace(
                values={"analysis_duration": 0.1},
                managed_input=item,
            )
            for item in managed_inputs
        )

        def build_sounddevice_audio_backend(self, sounddevice_module):
            return MonotonicSoundDeviceAudioBackend(
                sounddevice_module,
                authority,
            )

        def build_resident_adapter(self, **kwargs):
            self.managed_inputs = Prerequisites.managed_inputs
            return PreparedProductionPrerequisites.build_resident_adapter(
                self,
                **kwargs,
            )

    factory = ProductionGraphFactory(
        runtime=object(),
        plc_dispatch_ports=object(),
        plc_result_sequencer=object(),
        output_directory=output_directory,
        sounddevice_module=sounddevice,
    )

    upstream = factory.prepare_upstream(Prerequisites())
    try:
        if stream_count == 1:
            from app.service.multi_run_coordinator_mvp import (
                AudioSaveStatus,
                SaveCancelToken,
            )
            from app.service.run_persistence.prepare_database import (
                prepare_run_persistence_database,
            )
            from tests.multi_run_integration.test_production_audio_saver import (
                _captured_request,
                _insert_active,
                _real_saver,
            )
            from tests.run_persistence.test_audio_repository import (
                ready_runtime,
            )

            runtime = ready_runtime(
                prepare_run_persistence_database(tmp_path / "save.db")
            )
            try:
                run_id = "12345678-1234-4234-8234-123456789abc"
                now = "2026-07-29T12:00:00.000000Z"
                _insert_active(runtime, run_id=run_id, now=now)
                saver, _run_repository = _real_saver(runtime, now)
                final_path = output_directory / f"{run_id}.wav"
                outcome = saver.save_captured(
                    _captured_request(run_id, final_path),
                    SaveCancelToken(),
                )
                assert outcome.status is AudioSaveStatus.SAVED
                with wave.open(str(final_path), "rb") as reader:
                    assert reader.getframerate() == 8_000
                    assert reader.getnframes() == 2
            finally:
                runtime.close_after_upstream_stopped()
        assert len(upstream.streams) == stream_count
        assert len(sounddevice.started) == stream_count
        assert all(item["blocksize"] == 512 for item in sounddevice.started)
    finally:
        upstream.close()


def test_output_directory_conflict_rejects_before_hardware_backend_construction(
    monkeypatch,
    tmp_path,
):
    import app.service.multi_run_integration.production_composition as module

    output_path = tmp_path / "run_audio"
    output_path.write_text("conflict", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "ManagedInputSoundDeviceBackend",
        lambda _module: (_ for _ in ()).throw(
            AssertionError("hardware backend constructed before output validation")
        ),
    )
    factory = ProductionGraphFactory(
        runtime=object(),
        plc_dispatch_ports=object(),
        plc_result_sequencer=object(),
        output_directory=output_path,
    )

    with pytest.raises(ProductionCompositionError, match="output"):
        factory.prepare_upstream(SimpleNamespace())


@pytest.mark.parametrize("failure_stage", ["hard_link", "cleanup"])
def test_publication_probe_failure_rejects_before_backend_or_streams(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    import app.service.multi_run_integration.production_composition as module

    backend_constructions = []
    monkeypatch.setattr(
        module,
        "ManagedInputSoundDeviceBackend",
        lambda _module: backend_constructions.append(True),
    )
    if failure_stage == "hard_link":
        monkeypatch.setattr(
            os,
            "link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("hard-link denied")
            ),
        )
    else:
        real_unlink = Path.unlink

        def fail_probe_cleanup(path, *args, **kwargs):
            if path.name.startswith(".jingcheng-publication-probe-"):
                raise OSError("cleanup denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_probe_cleanup)
    factory = ProductionGraphFactory(
        runtime=object(),
        plc_dispatch_ports=object(),
        plc_result_sequencer=object(),
        output_directory=tmp_path / "run_audio",
    )

    with pytest.raises(ProductionCompositionError, match="publication probe"):
        factory.prepare_upstream(SimpleNamespace())

    assert backend_constructions == []
