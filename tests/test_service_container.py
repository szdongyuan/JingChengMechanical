"""Test that ServiceContainer wires AiService."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import importlib
import importlib.util
import inspect
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import app.services_container as mod
from app.service.factory_selection_settings import FactorySelectionSettings
from app.service.historical_metric_report_service import HistoricalMetricReportService
from app.services_container import EventBus
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.data_management import RunDataManagementService
from app.service.run_persistence.export_reader import RunExportReader
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)
from app.service.run_record_export.archive import RunArchiveService


class _PreparedOnlyComposition:
    coordinator = None
    ingress_open = False
    streams = (object(),)

    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class _PreparedOnlyBuilder:
    def __init__(self):
        self.prepared = _PreparedOnlyComposition()

    def prepare(self):
        return self.prepared


def _fresh_runtime(tmp_path: Path) -> RunPersistenceDatabase:
    return RunPersistenceDatabase(
        prepare_run_persistence_database(tmp_path / "run_v2.db")
    )


def _prepared_only_kwargs(tmp_path: Path) -> dict[str, object]:
    builder = _PreparedOnlyBuilder()
    return {
        "run_persistence_database": _fresh_runtime(tmp_path),
        "production_composition_builder": builder,
    }


class _ConfigServiceStub:
    def __init__(self, configs, *, failures=()):
        self._configs = dict(configs)
        self._failures = set(failures)

    def load_config(self, config_id: int):
        if config_id in self._failures:
            raise RuntimeError(f"failed to load config {config_id}")
        return self._configs[config_id]


def _make_lightweight_service_container(config_service):
    services = object.__new__(mod.ServiceContainer)
    services.bus = EventBus()
    services.config_service = config_service
    services.plc_services = {}
    services.pending_plc_start_signal_reset_ids = set()
    services.bus.plc_start_signal_reset_result.connect(
        services._on_plc_start_signal_reset_result
    )
    return services


def _load_factory_mode_controller_module():
    spec = importlib.util.find_spec("app.service.factory_mode_controller")
    assert spec is not None, "app.service.factory_mode_controller module is missing"
    return importlib.import_module("app.service.factory_mode_controller")


def _normalized_signal_argument_types(signal) -> tuple[str, ...]:
    signatures = tuple(signal.signatures)
    assert len(signatures) == 1

    signature = signatures[0]
    if "(" in signature:
        signature = signature.split("(", 1)[1]
    signature = signature.removesuffix(")")
    return tuple(part.strip() for part in signature.split(","))


def test_services_container_cold_imports_in_subprocess():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import app.services_container; print('ok')"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_service_container_constructs_historical_report_service_without_circular_import(
    monkeypatch,
    tmp_path,
):
    import app.service.factory_mode_controller as factory_mode_controller

    class FakeDb:
        def connect(self):
            pass

        def close(self):
            pass

    class FakeConfigService:
        golden_sample_service = object()

    class FakeAiService:
        def shutdown(self):
            pass

    monkeypatch.setattr(mod, "DbConnectionService", FakeDb)
    monkeypatch.setattr(mod, "HardwareSelectService", lambda: object())
    monkeypatch.setattr(mod, "InputCalibrationService", lambda bus: object())
    monkeypatch.setattr(mod, "ConfigService", lambda bus: FakeConfigService())
    monkeypatch.setattr(mod, "AudioDataService", lambda: object())
    monkeypatch.setattr(mod, "AiService", FakeAiService)
    monkeypatch.setattr(
        mod,
        "ConfigPackageService",
        lambda *, config_service, ai_service: object(),
    )
    monkeypatch.setattr(mod, "FactorySelectionSettings", lambda: object())
    monkeypatch.setattr(
        factory_mode_controller,
        "FactoryModeController",
        lambda bus, services, **_kwargs: object(),
    )

    services = mod.ServiceContainer(**_prepared_only_kwargs(tmp_path))

    assert isinstance(
        services.historical_metric_report_service,
        HistoricalMetricReportService,
    )
    assert (
        services.historical_metric_report_service._audio_data_service
        is services.audio_data_service
    )


def test_service_container_has_ai_service():
    source = inspect.getsource(mod.ServiceContainer.__post_init__)
    assert "ai_service" in source
    assert "AiService" in source


def test_service_container_has_config_package_service():
    source = inspect.getsource(mod.ServiceContainer.__post_init__)
    assert "config_package_service" in source
    assert "ConfigPackageService" in source


def test_service_container_owns_one_history_store_and_attaches_one_adapter():
    source = inspect.getsource(mod.ServiceContainer)
    assert "result_history_store" in source
    assert "attach_result_dashboard" in source
    assert "HistoryQtAdapter" in source


def test_service_container_wires_one_durable_run_export_reader_and_archive_service(
    monkeypatch,
    tmp_path,
):
    runtime = RunPersistenceDatabase(
        prepare_run_persistence_database(tmp_path / "prepared.sqlite3")
    )
    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(
        mod,
        "HardwareSelectService",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        mod,
        "InputCalibrationService",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        mod,
        "ConfigService",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    services = mod.ServiceContainer(run_persistence_database=runtime)

    assert services.run_persistence_database is runtime
    assert type(services.run_export_reader) is RunExportReader
    assert services.run_export_reader._runtime is runtime
    assert (
        services.run_export_reader._result_artifact_store
        is services.analysis_result_artifact_store
    )
    assert type(services.run_archive_service) is RunArchiveService
    assert services.run_archive_service._reader is services.run_export_reader
    assert type(services.run_data_management_service) is RunDataManagementService
    assert services.run_data_management_service._runtime is runtime
    assert hasattr(EventBus, "run_records_deleted")


def test_analysis_signals_exist():
    """All analysis kernel signals should be defined on EventBus."""
    for name in (
        "stft_updated",
        "spl_updated",
        "loudness_updated",
        "octave_updated",
        "fft_updated",
        "recording_progress",
        "test_started",
        "test_finished",
    ):
        assert hasattr(EventBus, name), f"Missing signal: {name}"


def test_event_bus_exposes_ai_toolchains_changed_signal():
    bus = EventBus()
    received = []

    bus.ai_toolchains_changed.connect(lambda: received.append("fired"))
    bus.ai_toolchains_changed.emit()

    assert received == ["fired"]


def test_event_bus_exposes_plc_changed_signal():
    bus = EventBus()
    received = []

    bus.plc_changed.connect(lambda: received.append("fired"))
    bus.plc_changed.emit()

    assert received == ["fired"]


def test_event_bus_exposes_plc_start_signal_reset_contract():
    assert _normalized_signal_argument_types(
        EventBus.request_plc_start_signal_reset
    ) == ("int",)
    assert _normalized_signal_argument_types(
        EventBus.plc_start_signal_reset_result
    ) == ("int", "bool", "QString")

    bus = EventBus()
    requests = []
    results = []
    bus.request_plc_start_signal_reset.connect(requests.append)
    bus.plc_start_signal_reset_result.connect(
        lambda plc_id, success, message: results.append(
            (plc_id, success, message)
        )
    )

    bus.request_plc_start_signal_reset.emit(7)
    bus.plc_start_signal_reset_result.emit(7, False, "reset failed")

    assert requests == [7]
    assert results == [(7, False, "reset failed")]


def test_service_container_coordinates_pending_plc_start_signal_resets():
    services = _make_lightweight_service_container(
        _ConfigServiceStub(
            {
                101: {"plc_id": 7},
                202: {"plc_id": 7},
                303: {"plc_id": 8},
                404: {"plc_id": None},
                606: {"plc_id": True},
            },
            failures={505},
        )
    )
    requested_plc_ids = []
    log_messages = []
    services.bus.request_plc_start_signal_reset.connect(requested_plc_ids.append)
    services.bus.log.connect(log_messages.append)

    plc_ids = services.request_plc_start_signal_resets_for_configs(
        (101, 202, 303, 404, 505)
    )

    assert plc_ids == (7, 8)
    assert services.pending_plc_start_signal_reset_ids == {7, 8}
    assert requested_plc_ids == []

    services.plc_services[7] = object()
    assert services.dispatch_pending_plc_start_signal_reset(7) is True
    assert requested_plc_ids == [7]

    assert services.request_plc_start_signal_resets_for_configs([202]) == (7,)
    assert requested_plc_ids == [7]

    services.bus.plc_start_signal_reset_result.emit(7, False, "reset failed")
    assert services.pending_plc_start_signal_reset_ids == {7, 8}
    assert any("reset failed" in message for message in log_messages)

    services.bus.plc_start_signal_reset_result.emit(7, True, "")
    assert services.pending_plc_start_signal_reset_ids == {8}

    assert services.request_plc_start_signal_resets_for_configs([101]) == (7,)
    assert services.pending_plc_start_signal_reset_ids == {7, 8}
    assert requested_plc_ids == [7, 7]

    assert services.dispatch_pending_plc_start_signal_reset(8) is False
    assert requested_plc_ids == [7, 7]
    services.plc_services[8] = object()
    assert services.dispatch_pending_plc_start_signal_reset(8) is True
    assert requested_plc_ids == [7, 7, 8]

    assert services.request_plc_start_signal_resets_for_configs([606]) == ()
    assert len(log_messages) >= 3
    assert any("404" in message for message in log_messages)
    assert any("505" in message for message in log_messages)
    assert any("606" in message for message in log_messages)


def test_analysis_frame_signals_include_run_id_contract():
    expected_argument_types = ("int", "QString", "int", "PyQt_PyObject")

    assert _normalized_signal_argument_types(EventBus.stft_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.spl_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.loudness_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.octave_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.fft_updated) == expected_argument_types

    EventBus()

    assert _normalized_signal_argument_types(EventBus.stft_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.spl_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.loudness_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.octave_updated) == expected_argument_types
    assert _normalized_signal_argument_types(EventBus.fft_updated) == expected_argument_types


def test_factory_mode_signals_exist():
    for name in (
        "factory_plc_event",
        "factory_scan_event",
        "factory_start_blocked",
        "factory_selection_notice",
        "factory_runtime_start_accepted",
        "factory_runtime_analyzing_entered",
        "factory_runtime_auto_analysis_succeeded",
        "factory_runtime_auto_analysis_no_result",
        "factory_runtime_auto_analysis_error",
        "factory_runtime_auto_analysis_cleanup_settled",
        "factory_runtime_stopped",
        "factory_runtime_aborted",
        "factory_runtime_reanalysis_completed",
        "factory_state_changed",
        "factory_review_changed",
        "factory_display_channel_changed",
        "factory_display_reset",
    ):
        assert hasattr(EventBus, name), f"Missing factory signal: {name}"


def test_factory_display_channel_changed_signal_contract():
    expected_argument_types = ("int", "int")

    assert (
        _normalized_signal_argument_types(EventBus.factory_display_channel_changed)
        == expected_argument_types
    )

    bus = EventBus()
    received = []
    bus.factory_display_channel_changed.connect(lambda config_id, channel: received.append((config_id, channel)))

    bus.factory_display_channel_changed.emit(101, 2)

    assert received == [(101, 2)]


def test_factory_runtime_payload_contract_includes_run_id():
    start_payload_cls = getattr(mod, "FactoryRuntimeStartPayload", None)
    runtime_payload_cls = getattr(mod, "FactoryRuntimeConfigPayload", None)
    abort_payload_cls = getattr(mod, "FactoryRuntimeAbortPayload", None)

    assert start_payload_cls is not None, "FactoryRuntimeStartPayload is missing"
    assert runtime_payload_cls is not None, "FactoryRuntimeConfigPayload is missing"
    assert abort_payload_cls is not None, "FactoryRuntimeAbortPayload is missing"
    assert is_dataclass(start_payload_cls)
    assert is_dataclass(runtime_payload_cls)
    assert is_dataclass(abort_payload_cls)
    assert [field.name for field in fields(start_payload_cls)] == [
        "config_id",
        "run_id",
        "trigger_source",
        "plc_id",
    ]
    assert [field.name for field in fields(runtime_payload_cls)] == [
        "config_id",
        "run_id",
    ]
    assert [field.name for field in fields(abort_payload_cls)] == [
        "config_id",
        "run_id",
        "trigger_source",
        "error_message",
    ]

    start_payload = start_payload_cls(
        config_id=101,
        run_id="run-101-1",
        trigger_source="manual",
    )
    runtime_payload = runtime_payload_cls(
        config_id=101,
        run_id="run-101-1",
    )
    abort_payload = abort_payload_cls(
        config_id=101,
        run_id="run-101-1",
        trigger_source="manual",
        error_message="runtime exploded",
    )

    assert start_payload.run_id == "run-101-1"
    assert start_payload.plc_id is None
    assert runtime_payload.run_id == "run-101-1"
    assert abort_payload.run_id == "run-101-1"


def test_factory_runtime_stopped_payload_contract_is_explicit():
    stopped_payload_cls = getattr(mod, "FactoryRuntimeStoppedPayload", None)

    assert stopped_payload_cls is not None, "FactoryRuntimeStoppedPayload is missing"
    assert is_dataclass(stopped_payload_cls)
    assert [field.name for field in fields(stopped_payload_cls)] == [
        "config_id",
        "run_id",
        "trigger_source",
    ]

    payload = stopped_payload_cls(
        config_id=101,
        run_id="run-101-1",
        trigger_source="manual",
    )

    assert payload.config_id == 101
    assert payload.run_id == "run-101-1"
    assert payload.trigger_source == "manual"


def test_factory_controller_state_contract_exposes_only_spec_fields():
    state_cls = getattr(mod, "FactoryControllerState", None)

    assert state_cls is not None, "FactoryControllerState is missing"
    assert is_dataclass(state_cls)
    assert [field.name for field in fields(state_cls)] == [
        "selected_config_ids",
        "displayed_config_id",
        "run_phase",
        "trigger_source",
        "selection_ready",
        "selection_ready_reason",
        "start_ready",
        "start_ready_reason",
    ]


def test_factory_controller_state_derives_readiness_from_ordered_selection_without_manual_primary():
    ready_state = mod.FactoryControllerState(selected_config_ids=(101,), run_phase="armed")
    blocked_state = mod.FactoryControllerState(selected_config_ids=(), run_phase="not_ready")
    not_ready_selected_state = mod.FactoryControllerState(
        selected_config_ids=(101,),
        run_phase="not_ready",
    )
    reviewing_state = mod.FactoryControllerState(selected_config_ids=(101, 202), run_phase="reviewing")
    stopped_state = mod.FactoryControllerState(selected_config_ids=(101,), run_phase="stopped")
    busy_state = mod.FactoryControllerState(selected_config_ids=(101,), run_phase="running")

    assert ready_state.selection_ready is True
    assert ready_state.selection_ready_reason is None
    assert ready_state.start_ready is True
    assert ready_state.start_ready_reason is None

    assert blocked_state.selection_ready is False
    assert blocked_state.selection_ready_reason == "请至少启用一个配置"
    assert blocked_state.start_ready is False
    assert blocked_state.start_ready_reason == "请至少启用一个配置"

    assert not_ready_selected_state.selection_ready is True
    assert not_ready_selected_state.selection_ready_reason is None
    assert not_ready_selected_state.start_ready is False
    assert not_ready_selected_state.start_ready_reason == "工厂模式未就绪"

    assert reviewing_state.selection_ready is True
    assert reviewing_state.start_ready is True
    assert reviewing_state.start_ready_reason is None

    assert stopped_state.selection_ready is True
    assert stopped_state.start_ready is True
    assert stopped_state.start_ready_reason is None

    assert busy_state.selection_ready is True
    assert busy_state.start_ready is False
    assert busy_state.start_ready_reason == "当前测试运行中"


def test_factory_start_blocked_payload_contract_is_explicit():
    payload_cls = getattr(mod, "FactoryStartBlockedPayload", None)

    assert payload_cls is not None, "FactoryStartBlockedPayload is missing"
    assert is_dataclass(payload_cls)
    assert [field.name for field in fields(payload_cls)] == [
        "reason",
        "trigger_source",
    ]

    payload = payload_cls(reason="请至少启用一个配置", trigger_source="manual")

    assert payload.reason == "请至少启用一个配置"
    assert payload.trigger_source == "manual"


def test_import_audio_warning_contract_is_explicit():
    warning_cls = getattr(mod, "ImportAudioWarning", None)
    assert warning_cls is not None
    assert is_dataclass(warning_cls)
    assert [field.name for field in fields(warning_cls)] == [
        "code",
        "message",
        "severity",
        "channel_index",
        "wav_channel_index",
        "metric",
    ]

    warning = warning_cls(
        code="nyquist_skipped",
        message="超出奈奎斯特频率的限制已跳过",
        severity="warning",
        channel_index=0,
        wav_channel_index=1,
        metric="fft",
    )

    assert warning.code == "nyquist_skipped"
    assert warning.severity == "warning"


def test_factory_review_payload_contract_is_explicit():
    review_payload_cls = getattr(mod, "FactoryReviewPayload", None)
    review_window_cls = getattr(mod, "FactoryReviewWindow", None)
    plc_payload_cls = getattr(mod, "FactoryPlcIngressPayload", None)
    abort_payload_cls = getattr(mod, "FactoryRuntimeAbortPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"
    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert plc_payload_cls is not None, "FactoryPlcIngressPayload is missing"
    assert abort_payload_cls is not None, "FactoryRuntimeAbortPayload is missing"
    assert is_dataclass(review_payload_cls)
    assert is_dataclass(plc_payload_cls)
    assert is_dataclass(abort_payload_cls)
    assert [field.name for field in fields(review_payload_cls)] == [
        "displayed_config_id",
        "config_payload",
        "run_id",
        "trigger_source",
        "captured_duration_sec",
        "default_window",
        "fallback_window",
        "manual_reanalysis_allowed",
        "no_result",
        "error_message",
        "judgment_payload",
        "summary_payload",
        "chart_payload",
        "import_warnings",
        "display_channel_labels",
    ]
    assert [field.name for field in fields(abort_payload_cls)] == [
        "config_id",
        "run_id",
        "trigger_source",
        "error_message",
    ]
    assert [field.name for field in fields(plc_payload_cls)] == [
        "plc_id",
        "trigger_monotonic_ns",
        "sn",
        "model",
        "rotation_speed",
    ]

    review_window = review_window_cls(start_sec=1.0, end_sec=3.0)
    payload = review_payload_cls(run_id="run-101-1", default_window=review_window)
    plc_payload = plc_payload_cls(
        plc_id=12,
        trigger_monotonic_ns=123,
    )
    abort_payload = abort_payload_cls(
        config_id=101,
        run_id="run-101-1",
        trigger_source="manual",
        error_message="runtime exploded",
    )

    assert payload.default_window == review_window
    assert payload.run_id == "run-101-1"
    assert review_payload_cls().import_warnings == ()
    assert review_payload_cls().display_channel_labels == {}
    assert plc_payload.plc_id == 12
    assert plc_payload.trigger_monotonic_ns == 123
    assert plc_payload.sn is None
    assert plc_payload.model is None
    assert plc_payload.rotation_speed is None
    assert abort_payload.config_id == 101
    assert abort_payload.run_id == "run-101-1"
    assert abort_payload.trigger_source == "manual"
    assert abort_payload.error_message == "runtime exploded"


def test_factory_review_payload_exposes_summary_and_chart_payload_fields():
    payload = mod.FactoryReviewPayload()

    assert hasattr(payload, "summary_payload")
    assert hasattr(payload, "chart_payload")
    assert payload.summary_payload is None
    assert payload.chart_payload is None


def test_testmanager_and_self_test_are_absent():
    source = Path(mod.__file__).read_text(encoding="utf-8")

    assert "TestManager" not in source
    assert "self.test" not in source


def test_service_container_owns_factory_mode_controller(monkeypatch, tmp_path):
    controller_mod = _load_factory_mode_controller_module()
    controller_cls = controller_mod.FactoryModeController

    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    services = mod.ServiceContainer(**_prepared_only_kwargs(tmp_path))

    assert isinstance(services.factory_mode, controller_cls)
    assert services.factory_mode.bus is services.bus
    assert services.factory_mode.services is services


def test_service_container_exposes_plc_start_hook(monkeypatch):
    payloads = []

    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    bus = mod.EventBus()
    bus.factory_plc_event.connect(payloads.append)

    services = mod.ServiceContainer(bus=bus)

    assert hasattr(services, "_start_from_plc")

    services._start_from_plc(42, {}, 123_456)

    assert payloads == [
        mod.FactoryPlcIngressPayload(
            plc_id=42,
            trigger_monotonic_ns=123_456,
        )
    ]


def test_submit_scan_is_sole_boundary_and_direct_bus_emit_is_forbidden():
    samples = []
    observed = []

    def monotonic_ns():
        samples.append("sample")
        return 222_333

    services = object.__new__(mod.ServiceContainer)
    services.bus = EventBus()
    services.monotonic_ns = monotonic_ns
    services.ingress_open = True
    services.bus.factory_scan_event.connect(observed.append)

    assert services.submit_scan("SN-44") is True
    assert samples == ["sample"]
    assert observed == [
        mod.FactoryScanIngressPayload(
            scan_text="SN-44",
            trigger_monotonic_ns=222_333,
        )
    ]

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert source.count("factory_scan_event.emit(") == 1


@pytest.mark.parametrize("invalid_kind", ["missing", "invalid"])
def test_missing_or_invalid_run_v2_keeps_ingress_closed(
    monkeypatch,
    tmp_path,
    invalid_kind,
):
    path = tmp_path / "run_v2.db"
    if invalid_kind == "invalid":
        path.write_bytes(b"not sqlite")

    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    failures = []
    bus = EventBus()
    bus.status.connect(failures.append)

    services = mod.ServiceContainer(
        bus=bus,
        run_persistence_path=path,
    )

    assert services.ingress_open is False
    assert services.startup_failure is not None
    assert len(failures) == 1
    assert not path.exists() if invalid_kind == "missing" else path.read_bytes() == b"not sqlite"


def test_service_container_exposes_factory_selection_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    services = mod.ServiceContainer(**_prepared_only_kwargs(tmp_path))

    assert isinstance(services.factory_selection_settings, FactorySelectionSettings)


def test_service_container_shutdown_is_cached_and_closes_prepared_graph_before_writers_and_databases(
    monkeypatch,
    tmp_path,
):
    calls = []
    ai_service = MagicMock()
    ai_service.shutdown.side_effect = lambda: calls.append("ai_shutdown")
    db_service = MagicMock()
    db_service.close.side_effect = lambda: calls.append("db_close")
    monkeypatch.setattr(mod, "DbConnectionService", MagicMock(return_value=db_service))
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=ai_service))

    kwargs = _prepared_only_kwargs(tmp_path)
    builder = kwargs["production_composition_builder"]
    builder.prepared.close = lambda: calls.append("prepared_close")
    runtime = kwargs["run_persistence_database"]
    original_run_close = runtime.close_after_upstream_stopped
    runtime.close_after_upstream_stopped = lambda: (
        calls.append("run_db_close"),
        original_run_close(),
    )[-1]
    services = mod.ServiceContainer(**kwargs)

    report = services.shutdown()
    repeated = services.shutdown()

    assert report.clean is True
    assert repeated is report
    assert calls == [
        "prepared_close",
        "ai_shutdown",
        "db_close",
        "run_db_close",
    ]


def test_service_container_shutdown_reports_one_aggregate_notice_when_ai_shutdown_raises(
    monkeypatch,
    tmp_path,
):
    calls = []
    shutdown_error = RuntimeError("resident cleanup failed")
    ai_service = MagicMock()

    def fail_ai_shutdown():
        calls.append("ai_shutdown")
        raise shutdown_error

    ai_service.shutdown.side_effect = fail_ai_shutdown
    db_service = MagicMock()
    db_service.close.side_effect = lambda: calls.append("db_close")
    monkeypatch.setattr(mod, "DbConnectionService", MagicMock(return_value=db_service))
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=ai_service))

    services = mod.ServiceContainer(**_prepared_only_kwargs(tmp_path))

    notices = []
    services.bus.status.connect(notices.append)
    report = services.shutdown()

    assert report.clean is False
    assert report.restart_required is True
    assert calls == ["ai_shutdown", "db_close"]
    assert len(notices) == 1
    assert "restart" in notices[0].lower()


def test_service_container_wires_config_package_service(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    config_service = MagicMock()
    ai_service = MagicMock()
    config_package_service = MagicMock()
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=config_service))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=ai_service))
    monkeypatch.setattr(
        mod,
        "ConfigPackageService",
        MagicMock(return_value=config_package_service),
    )

    services = mod.ServiceContainer(**_prepared_only_kwargs(tmp_path))

    mod.ConfigPackageService.assert_called_once_with(
        config_service=config_service,
        ai_service=ai_service,
    )
    assert services.config_package_service is config_package_service
