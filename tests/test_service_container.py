"""Test that ServiceContainer wires AiService."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import importlib
import importlib.util
import inspect
from unittest.mock import MagicMock

import app.services_container as mod
from app.service.analysis.test_manager import TestManager
from app.services_container import EventBus


def _load_factory_mode_controller_module():
    spec = importlib.util.find_spec("app.service.factory_mode_controller")
    assert spec is not None, "app.service.factory_mode_controller module is missing"
    return importlib.import_module("app.service.factory_mode_controller")


def test_service_container_has_ai_service():
    source = inspect.getsource(mod.ServiceContainer.__post_init__)
    assert "ai_service" in source
    assert "AiService" in source


def test_analysis_signals_exist():
    """All analysis kernel signals should be defined on EventBus."""
    for name in (
        "stft_updated",
        "spl_updated",
        "octave_updated",
        "fft_updated",
        "recording_progress",
        "test_started",
        "test_finished",
    ):
        assert hasattr(EventBus, name), f"Missing signal: {name}"


def test_factory_mode_signals_exist():
    for name in (
        "factory_plc_event",
        "factory_scan_event",
        "factory_runtime_start_accepted",
        "factory_runtime_analyzing_entered",
        "factory_runtime_auto_analysis_succeeded",
        "factory_runtime_auto_analysis_no_result",
        "factory_runtime_auto_analysis_error",
        "factory_runtime_reanalysis_completed",
        "factory_state_changed",
        "factory_review_changed",
        "factory_display_reset",
    ):
        assert hasattr(EventBus, name), f"Missing factory signal: {name}"


def test_factory_review_payload_contract_is_explicit():
    review_payload_cls = getattr(mod, "FactoryReviewPayload", None)
    review_window_cls = getattr(mod, "FactoryReviewWindow", None)
    plc_payload_cls = getattr(mod, "FactoryPlcIngressPayload", None)

    assert review_payload_cls is not None, "FactoryReviewPayload is missing"
    assert review_window_cls is not None, "FactoryReviewWindow is missing"
    assert plc_payload_cls is not None, "FactoryPlcIngressPayload is missing"
    assert is_dataclass(review_payload_cls)
    assert [field.name for field in fields(review_payload_cls)] == [
        "displayed_config_id",
        "trigger_source",
        "captured_duration_sec",
        "default_window",
        "fallback_window",
        "manual_reanalysis_allowed",
        "no_result",
        "error_message",
        "judgment_payload",
    ]

    review_window = review_window_cls(start_sec=1.0, end_sec=3.0)
    payload = review_payload_cls(default_window=review_window)
    plc_payload = plc_payload_cls(plc_device_key="plc-a")

    assert payload.default_window == review_window
    assert plc_payload.plc_device_key == "plc-a"


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
