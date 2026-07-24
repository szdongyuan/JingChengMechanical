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
from app.service.analysis.test_manager import TestManager
from app.services_container import EventBus


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
    monkeypatch.setattr(mod, "TestManager", lambda bus, services: object())
    monkeypatch.setattr(
        factory_mode_controller,
        "FactoryModeController",
        lambda bus, services: object(),
    )

    services = mod.ServiceContainer()

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
        "active_config_id",
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
        "sn",
        "model",
        "rotation_speed",
    ]

    review_window = review_window_cls(start_sec=1.0, end_sec=3.0)
    payload = review_payload_cls(run_id="run-101-1", default_window=review_window)
    plc_payload = plc_payload_cls(plc_id=12)
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


def test_service_container_uses_test_manager(monkeypatch):
    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    services = mod.ServiceContainer()

    assert isinstance(services.test, TestManager)
    assert services.test._bus is services.bus
    assert services.test._services is services


def test_service_container_owns_factory_mode_controller(monkeypatch):
    controller_mod = _load_factory_mode_controller_module()
    controller_cls = controller_mod.FactoryModeController

    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    services = mod.ServiceContainer()

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

    services._start_from_plc(42)

    assert payloads == [mod.FactoryPlcIngressPayload(plc_id=42)]


def test_service_container_exposes_factory_selection_settings(monkeypatch):
    monkeypatch.setattr(mod.DbConnectionService, "connect", lambda self: None)
    monkeypatch.setattr(mod, "HardwareSelectService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "InputCalibrationService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "ConfigService", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "AiService", MagicMock(return_value=MagicMock()))

    services = mod.ServiceContainer()

    assert isinstance(services.factory_selection_settings, FactorySelectionSettings)


def test_service_container_shutdown_stops_ai_before_closing_db(monkeypatch):
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

    services = mod.ServiceContainer()

    services.shutdown()

    assert calls == ["ai_shutdown", "db_close"]


def test_service_container_shutdown_closes_db_when_ai_shutdown_raises(monkeypatch):
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

    services = mod.ServiceContainer()

    with pytest.raises(RuntimeError, match="resident cleanup failed"):
        services.shutdown()

    assert calls == ["ai_shutdown", "db_close"]


def test_service_container_wires_config_package_service(monkeypatch):
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

    services = mod.ServiceContainer()

    mod.ConfigPackageService.assert_called_once_with(
        config_service=config_service,
        ai_service=ai_service,
    )
    assert services.config_package_service is config_package_service
