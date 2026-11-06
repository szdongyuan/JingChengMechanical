from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import app.services_container as services_module
import app.service.multi_run_integration as integration_package
from app.service.fault_types import decode_history_labels_payload
from app.service.multi_run_coordinator_mvp.contracts import (
    CapturedStartRequest,
    FrozenPayload,
    ImportedStartRequest,
    StartStatus,
    TriggerSource,
)
from app.service.plc_result_sequencer import PlcAttemptOutcome
from app.service.multi_run_integration.production_composition import (
    PreparedProductionComposition,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)


def _prepared_runtime(tmp_path: Path) -> RunPersistenceDatabase:
    return RunPersistenceDatabase(
        prepare_run_persistence_database(tmp_path / "run_v2.db")
    )


def _minimal_services(monkeypatch) -> None:
    class MinimalConfigService:
        golden_sample_service = object()

        @staticmethod
        def load_config(config_id):
            return {"config_id": config_id, "plc_id": config_id}

    monkeypatch.setattr(
        services_module.DbConnectionService,
        "connect",
        lambda self: None,
    )
    monkeypatch.setattr(
        services_module,
        "HardwareSelectService",
        lambda: object(),
    )
    monkeypatch.setattr(
        services_module,
        "InputCalibrationService",
        lambda _bus: object(),
    )
    monkeypatch.setattr(
        services_module,
        "ConfigService",
        lambda _bus: MinimalConfigService(),
    )
    monkeypatch.setattr(services_module, "AudioDataService", lambda: object())
    monkeypatch.setattr(
        services_module,
        "AiService",
        lambda: SimpleNamespace(shutdown=lambda: None),
    )
    monkeypatch.setattr(
        services_module,
        "HistoricalMetricReportService",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        services_module,
        "ConfigPackageService",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        services_module,
        "FactorySelectionSettings",
        lambda: object(),
    )
    monkeypatch.setattr(
        integration_package,
        "HistoryQtAdapter",
        lambda _dashboard, **_kwargs: SimpleNamespace(
            publish=lambda _event: None
        ),
    )


class _Stream:
    def __init__(self, hardware_id: str):
        self.hardware_id = hardware_id
        self.open_calls = []

    def open(self, *, frames_per_buffer: int):
        self.open_calls.append(frames_per_buffer)


class _Coordinator:
    def __init__(self, *, plc):
        self._plc = plc
        self.captured = []
        self.captured_event = threading.Event()
        self.imported = []

    def start_captured(self, request):
        self.captured.append(request)
        self.captured_event.set()
        return SimpleNamespace(status=StartStatus.ACCEPTED_ACTIVE)

    def start_imported(self, request):
        self.imported.append(request)
        return SimpleNamespace(status=StartStatus.ACCEPTED_ACTIVE)


def _composition_builder(
    trace,
    streams,
    coordinator_box,
    *,
    config_routes=((11, None),),
    activation_error=None,
    close_calls=None,
):
    frozen_routes = tuple(config_routes)

    class Prepared:
        coordinator = None
        ingress_open = False
        config_routes = frozen_routes
        selected_config_ids = tuple(route[0] for route in frozen_routes)

        def activate(self, history_sink):
            trace.append(("activate", 512))
            if activation_error is not None:
                raise activation_error
            coordinator = _Coordinator(plc=builder.plc)
            coordinator_box.append(coordinator)
            return SimpleNamespace(
                coordinator=coordinator,
                streams=tuple(streams),
                history_sink=history_sink,
            )

        def close(self):
            if close_calls is not None:
                close_calls.append("closed")

    class Builder:
        plc = None

        def prepare(self):
            trace.append(("prepare", 512))
            for stream in streams:
                stream.open(frames_per_buffer=512)
            return Prepared()

    builder = Builder()
    return builder


def _attach(services):
    services.attach_result_dashboard(
        SimpleNamespace(_history_store=services.result_history_store)
    )
    return services


def test_service_container_freezes_one_config_and_opens_six_low_latency_streams(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    trace = []
    streams = [_Stream(f"hardware-{index}") for index in range(6)]
    coordinators = []
    config = {
        "config_id": "config-1",
        "fault_types": [" bearing ", "seal", "bearing"],
        "manual_judgment_buttons_visible": True,
        "nested": {"channels": [0, 1]},
    }
    builder = _composition_builder(trace, streams, coordinators)
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config=config,
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)

    assert services.ingress_open is True
    assert len(services.final_composition.streams) == 6
    assert all(stream.open_calls == [512] for stream in streams)
    request = services.build_captured_start_request(
        source=TriggerSource.MANUAL,
        trigger_monotonic_ns=123,
        workorder={"sn": "SN-1"},
        config_id=11,
    )
    config["nested"]["channels"].append(9)
    ingress = json.loads(request.ingress_payload.canonical_bytes)
    analysis = ingress["analysis_input"]
    assert ingress["schema"] == "jingcheng-run-config/v1"
    assert analysis["schema"] == "analysis-input/v1"
    assert ingress["config"] == analysis["config"]
    assert ingress["config"] == {"config_id": 11}
    decoded_labels = decode_history_labels_payload(
        value_id=request.fallback_history_labels.value_id,
        canonical_bytes=request.fallback_history_labels.canonical_bytes,
    )
    assert request.fallback_history_labels.value_id.startswith("sha256:")
    assert ingress["history_labels"] == decoded_labels
    assert decoded_labels == {
        "schema": "jingcheng-history-labels/v2",
        "config": {
            "config_id": 11,
            "name": "config-1",
            "fault_types": ["bearing", "seal"],
            "manual_judgment_buttons_visible": True,
            "details": [],
        },
        "sn": "SN-1",
        "trigger_source": "manual",
    }
    assert services.preaccept_terminal_statuses == (
        StartStatus.PREFLIGHT_REJECTED,
        StartStatus.INVALID_FROZEN_INPUT,
        StartStatus.CAPTURE_CAPACITY,
        StartStatus.RESIDENT_UNAVAILABLE,
        StartStatus.ACTIVE_COMMIT_REJECTED,
    )
    assert len(set(services.preaccept_terminal_statuses)) == 5


def test_captured_and_imported_history_labels_freeze_prepared_visibility(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    builder = _composition_builder(
        [],
        [_Stream("hardware-1")],
        [],
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={
            "config_id": "mutable-fallback",
            "fault_types": [],
            "manual_judgment_buttons_visible": False,
        },
    )
    prepared_values = {
        "config_name": "Prepared config",
        "fault_types_json": '["异响"]',
        "manual_judgment_buttons_visible": True,
    }
    services.prepared_production_composition._prerequisites = SimpleNamespace(
        configs=(SimpleNamespace(config_id=11, values=prepared_values),)
    )

    captured = services.build_captured_start_request(
        source=TriggerSource.MANUAL,
        trigger_monotonic_ns=123,
        workorder={},
        config_id=11,
    )
    imported = services.build_imported_start_request(
        trigger_monotonic_ns=124,
        source_path=tmp_path / "source.wav",
    )
    prepared_values["manual_judgment_buttons_visible"] = False

    for request in (captured, imported):
        labels = decode_history_labels_payload(
            value_id=request.fallback_history_labels.value_id,
            canonical_bytes=request.fallback_history_labels.canonical_bytes,
        )
        assert labels["config"]["manual_judgment_buttons_visible"] is True


def test_unresolved_imported_history_labels_freeze_visibility_false(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    builder = _composition_builder(
        [],
        [_Stream("hardware-1")],
        [],
        config_routes=((11, None), (12, None)),
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={
            "config_id": "mutable-fallback",
            "fault_types": ["异响"],
            "manual_judgment_buttons_visible": True,
        },
    )

    imported = services.build_imported_start_request(
        trigger_monotonic_ns=124,
        source_path=tmp_path / "source.wav",
    )
    labels = decode_history_labels_payload(
        value_id=imported.fallback_history_labels.value_id,
        canonical_bytes=imported.fallback_history_labels.canonical_bytes,
    )

    assert labels["config"] == {
        "config_id": None,
        "name": "未解析配置",
        "fault_types": [],
        "manual_judgment_buttons_visible": False,
        "details": [],
    }


def test_service_container_committed_enrichment_reaches_qt_adapter_once(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    qt_bus = []
    observer_calls = []

    class Adapter:
        def __init__(self, _dashboard, **_kwargs):
            pass

        def publish(self, _event):
            pass

        def publish_enrichment(self, event):
            qt_bus.append(event)

    monkeypatch.setattr(integration_package, "HistoryQtAdapter", Adapter)
    trace = []
    coordinators = []
    builder = _composition_builder(
        trace,
        [_Stream("hardware-1")],
        coordinators,
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={"config_id": "config-1", "fault_types": []},
    )
    builder.plc = services.plc_result_sequencer
    services.attach_committed_history_observer(observer_calls.append)
    _attach(services)
    enrichment = object()

    services.final_composition.history_sink.publish_enrichment(enrichment)

    assert qt_bus == [enrichment]
    assert observer_calls == []


def test_captured_ingress_preserves_exact_timestamps_and_512_fail_closed(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    streams = [_Stream(f"hardware-{index}") for index in range(6)]
    coordinators = []
    builder = _composition_builder([], streams, coordinators)
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={"config_id": "config-1", "fault_types": []},
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)

    request = services.build_captured_start_request(
        source=TriggerSource.SCANNER,
        trigger_monotonic_ns=987_654,
        workorder={"sn": "SN-9"},
        config_id=11,
    )
    assert type(request.trigger_monotonic_ns) is int
    assert request.trigger_monotonic_ns == 987_654
    assert services.production_frames_per_buffer == 512

    for invalid in (None, True, -1, 0, "987654"):
        with pytest.raises((TypeError, ValueError)):
            services.build_captured_start_request(
                source=TriggerSource.SCANNER,
                trigger_monotonic_ns=invalid,
                workorder={},
                config_id=11,
            )

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="512"):
        services_module.ServiceContainer(
            run_persistence_database=_prepared_runtime(other),
            production_composition_builder=_composition_builder([], streams, []),
            semantic_config={"config_id": "config-1"},
            production_frames_per_buffer=256,
        )


def test_default_semantic_config_routes_manual_ingress_from_frozen_prepare_identity(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    coordinators = []
    builder = _composition_builder(
        [],
        [_Stream("hardware-1")],
        coordinators,
        config_routes=((101, 7),),
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)

    assert services.factory_mode.start_manual({"sn": "SN-101"}) is True
    assert coordinators[0].captured_event.wait(timeout=2)
    assert len(coordinators[0].captured) == 1
    assert coordinators[0].captured[0].source is TriggerSource.MANUAL
    ingress = json.loads(
        coordinators[0].captured[0].ingress_payload.canonical_bytes
    )
    assert ingress["config"] == {"config_id": 101}
    assert ingress["analysis_input"]["config"] == {"config_id": 101}


def test_two_plc_starts_do_not_wait_for_first_start_admission(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    coordinators = []
    builder = _composition_builder(
        [],
        [_Stream("hardware-1"), _Stream("hardware-2")],
        coordinators,
        config_routes=((101, 7), (202, 8)),
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)
    coordinator = coordinators[0]
    first_entered = threading.Event()
    release_first = threading.Event()

    def blocking_start(request):
        coordinator.captured.append(request)
        if len(coordinator.captured) == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        coordinator.captured_event.set()
        return SimpleNamespace(status=StartStatus.ACCEPTED_ACTIVE)

    coordinator.start_captured = blocking_start
    try:
        started = time.perf_counter()
        services._start_from_plc(7, {"sn": "A"}, 111_222)
        assert first_entered.wait(timeout=2)
        services._start_from_plc(8, {"sn": "B"}, 333_444)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.1
        deadline = time.monotonic() + 2
        while len(coordinator.captured) < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert len(coordinator.captured) == 2
        release_first.set()
    finally:
        release_first.set()
        dispatcher = services._captured_start_dispatcher
        if dispatcher is not None:
            assert dispatcher.close(timeout_seconds=2)

    assert [item.trigger_monotonic_ns for item in coordinator.captured] == [
        111_222,
        333_444,
    ]
    assert [item.source for item in coordinator.captured] == [
        TriggerSource.PLC,
        TriggerSource.PLC,
    ]
    assert [
        json.loads(item.ingress_payload.canonical_bytes)["config"]["config_id"]
        for item in coordinator.captured
    ] == [101, 202]


def test_activation_failure_enters_degraded_mode_and_closes_prepared_ownership(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    close_calls = []
    builder = _composition_builder(
        [],
        [_Stream("hardware-1")],
        [],
        activation_error=RuntimeError("activation exploded"),
        close_calls=close_calls,
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
    )
    dashboard = SimpleNamespace(
        _history_store=services.result_history_store
    )

    adapter = services.attach_result_dashboard(dashboard)

    assert adapter is services.result_history_adapter
    assert services.ingress_open is False
    assert services.startup_failure is None
    assert services.prepared_production_composition is None
    assert services.final_composition is None
    assert services.multi_run_coordinator is None
    assert services._prepared_config_routes == ()
    assert close_calls == ["closed"]
    assert services.factory_mode.state.run_phase == "not_ready"
    assert services.factory_mode.state.start_ready is False
    assert "activation exploded" in services.production_unavailable_reason
    assert "修复" in services.production_unavailable_reason
    assert "重启" in services.production_unavailable_reason


def test_prepare_failure_enters_degraded_mode_with_not_ready_factory_controller(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)

    class FailedBuilder:
        @staticmethod
        def prepare():
            raise RuntimeError("selected audio endpoint is missing")

    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=FailedBuilder(),
    )

    assert services.startup_failure is None
    assert services.production_composition_state == "failed"
    assert services.prepared_production_composition is None
    assert services.final_composition is None
    assert services.multi_run_coordinator is None
    assert services.ingress_open is False
    assert services.factory_mode.state.run_phase == "not_ready"
    assert services.factory_mode.state.start_ready is False
    assert "selected audio endpoint is missing" in services.production_unavailable_reason
    assert "修复" in services.production_unavailable_reason
    assert "重启" in services.production_unavailable_reason


def test_degraded_reason_survives_real_selection_confirm_refresh_and_edit(
    monkeypatch,
    tmp_path,
):
    from PyQt6.QtWidgets import QApplication
    from ui.modules.workorder import WorkOrderModule

    qapp = QApplication.instance() or QApplication([])
    _minimal_services(monkeypatch)

    class FailedBuilder:
        @staticmethod
        def prepare():
            raise RuntimeError("configured stream is unavailable")

    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=FailedBuilder(),
    )
    original_reason = services.production_unavailable_reason
    configs = {
        101: {"config_id": 101},
        202: {"config_id": 202},
    }
    services.config_service = SimpleNamespace(
        list_configs=lambda: list(configs.values()),
        load_config=lambda config_id: dict(configs[config_id]),
        validate_factory_runtime=lambda _config: (),
    )
    persisted = []
    services.factory_selection_settings = SimpleNamespace(
        load_selected_config_ids=lambda: (101,),
        save_selected_config_ids=lambda selected: (
            persisted.append(tuple(selected)) or None
        ),
    )

    initial_result = services.factory_mode.confirm_selection((101,))
    persisted.clear()
    workorder = WorkOrderModule(services)
    try:
        restored = services.factory_mode.state
        apply_result = services.factory_mode.confirm_selection((202,))
        edited = services.factory_mode.state
        refreshed = services.factory_mode.refresh_selection()
        changed = services.factory_mode.handle_configuration_changed()

        assert initial_result.status == "applied"
        assert apply_result.status == "applied"
        assert services.production_selection_locked is False
        assert restored.selected_config_ids == (101,)
        assert workorder.value()["selected_config_ids"] == (202,)
        assert edited.selected_config_ids == (202,)
        assert persisted == [(202,)]
        for state in (restored, edited, refreshed, changed):
            assert state.run_phase == "not_ready"
            assert state.selection_ready is False
            assert state.start_ready is False
            assert state.selection_ready_reason == original_reason
            assert state.start_ready_reason == original_reason
        assert services.factory_mode.can_arm() is False
        assert services.factory_mode.state.start_ready_reason == original_reason
    finally:
        workorder.close()
        workorder.deleteLater()
        qapp.processEvents()


def test_post_prepare_route_validation_failure_closes_returned_owner(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)

    class MalformedPrepared:
        coordinator = None
        config_routes = []
        close_calls = 0

        def close(self):
            self.close_calls += 1

    prepared = MalformedPrepared()
    builder = SimpleNamespace(prepare=lambda: prepared)

    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
    )

    assert prepared.close_calls == 1
    assert services.prepared_production_composition is None
    assert services.production_composition_state == "failed"
    assert services.startup_failure is None


def test_cleanup_failure_retains_prepared_owner_for_ordered_shutdown(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)

    class UnclosedPrepared:
        coordinator = None
        config_routes = []
        close_calls = 0

        def close(self):
            self.close_calls += 1
            raise RuntimeError("close still blocked")

    prepared = UnclosedPrepared()
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=SimpleNamespace(
            prepare=lambda: prepared
        ),
    )

    assert prepared.close_calls == 1
    assert services.prepared_production_composition is None
    assert services._production_cleanup_owner is prepared
    assert (
        services._shutdown_resources().prepared_composition
        is prepared
    )
    assert services.production_composition_state == "failed"
    assert services.ingress_open is False

    report = services.shutdown()

    assert prepared.close_calls == 2
    assert report.clean is False
    assert any(
        stage.name == "prepared_composition"
        and stage.detail == "close still blocked"
        for stage in report.stages
    )


def test_real_activation_cleanup_failure_remains_non_clean_at_shutdown(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    cleanup_trace = []

    class Prerequisites:
        selection_generation = 1
        selected_config_ids = (11,)
        configs = (
            SimpleNamespace(
                config_id=11,
                values={"plc_id": None},
            ),
        )
        monotonic_authority = SimpleNamespace(
            monotonic_domain="python/system-monotonic",
        )

        @staticmethod
        def validate_activation():
            return None

        @staticmethod
        def close():
            cleanup_trace.append("prerequisites")

    class Upstream:
        streams = (object(),)

        @staticmethod
        def close():
            cleanup_trace.append("upstream")
            raise RuntimeError("real partial cleanup failed")

    class GraphFactory:
        @staticmethod
        def activate(_prerequisites, _upstream, _history_sink):
            raise RuntimeError("activation failed")

    prepared = PreparedProductionComposition(
        prerequisites=Prerequisites(),
        upstream=Upstream(),
        graph_factory=GraphFactory(),
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=SimpleNamespace(
            prepare=lambda: prepared
        ),
    )

    _attach(services)
    report = services.shutdown()

    assert cleanup_trace == ["upstream", "prerequisites"]
    assert services.startup_failure is None
    assert services.production_composition_state == "failed"
    assert report.clean is False
    assert report.restart_required is True
    assert any(
        stage.name == "prepared_composition"
        and "real partial cleanup failed" in stage.detail
        for stage in report.stages
    )


def test_imported_trigger_is_admission_only(monkeypatch, tmp_path):
    _minimal_services(monkeypatch)
    coordinators = []
    builder = _composition_builder(
        [],
        [_Stream(f"hardware-{index}") for index in range(6)],
        coordinators,
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={"config_id": "config-1", "fault_types": []},
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)
    request = services.build_imported_start_request(
        trigger_monotonic_ns=777,
        source_path=tmp_path / "source.wav",
    )

    assert isinstance(request, ImportedStartRequest)
    assert request.trigger_monotonic_ns == 777
    assert services.submit_imported(request).status is StartStatus.ACCEPTED_ACTIVE
    assert coordinators[0].imported == [request]
    assert coordinators[0].captured == []


def test_task11_ports_share_one_sequencer_with_coordinator(monkeypatch, tmp_path):
    _minimal_services(monkeypatch)
    coordinators = []
    plc_services = {}
    plc_threads = {}
    builder = _composition_builder(
        [],
        [_Stream(f"hardware-{index}") for index in range(6)],
        coordinators,
    )
    services = services_module.ServiceContainer(
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={"config_id": "config-1", "fault_types": []},
        plc_services=plc_services,
        plc_threads=plc_threads,
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)

    live_services = services.plc_dispatch_ports._plc_services
    live_threads = services.plc_dispatch_ports._plc_threads
    assert dict(live_services) == plc_services
    assert dict(live_threads) == plc_threads
    assert (
        services.plc_result_sequencer._write_result.__self__
        is services.plc_dispatch_ports
    )
    assert services.plc_result_sequencer is coordinators[0]._plc

    replacement_service = object()
    replacement_thread = object()
    services.plc_services = {7: replacement_service}
    services.plc_threads = {7: replacement_thread}

    assert services.plc_dispatch_ports._plc_services is live_services
    assert services.plc_dispatch_ports._plc_threads is live_threads
    assert live_services[7] is replacement_service
    assert live_threads[7] is replacement_thread


def test_undefined_plc_semantics_report_one_physical_boundary_failure_and_diagnostic(
    monkeypatch,
    tmp_path,
):
    _minimal_services(monkeypatch)
    reports = []
    diagnostics = []
    bus = services_module.EventBus()
    bus.log.connect(diagnostics.append)
    builder = _composition_builder(
        [],
        [_Stream(f"hardware-{index}") for index in range(6)],
        [],
    )
    services = services_module.ServiceContainer(
        bus=bus,
        run_persistence_database=_prepared_runtime(tmp_path),
        production_composition_builder=builder,
        semantic_config={"config_id": "config-1", "fault_types": []},
        plc_report_attempt=reports.append,
    )
    builder.plc = services.plc_result_sequencer
    _attach(services)

    assert services.pending_plc_start_signal_reset_ids == {11}
    assert diagnostics == []

    result = services.publish_plc_semantic_result(
        plc_id=1,
        run_id=str(UUID("12345678-1234-4234-8234-123456789abc")),
        semantic_result={"conclusion": "OK"},
    )

    assert result is False
    assert len(reports) == 1
    assert reports[0].outcome is PlcAttemptOutcome.BOUNDARY_FAILED
    assert len(diagnostics) == 1
    assert "physical boundary failure" in diagnostics[0]
