from __future__ import annotations

import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.modules.config import ConfigModule


class _SignalStub:
    def __init__(self):
        self._callbacks = []
        self.emissions = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))
        for callback in self._callbacks:
            callback(*args, **kwargs)


class _ConfigServiceStub:
    def __init__(self):
        self.saved_configs = []

    def list_configs(self):
        return [
            {
                "config_id": saved["config_id"],
                "config_name": saved["config_name"],
            }
            for saved in self.saved_configs
        ]

    def validate(self, _data):
        return []

    def save_config(self, data):
        saved = dict(data)
        self.saved_configs = [saved]
        return saved["config_id"]


class _AiServiceStub:
    def __init__(self, binding: dict | None = None):
        self._binding = binding
        self.saved_bindings = []

    def list_enabled_toolchains(self):
        return [{"toolchain_id": "tc-1", "display_name": "Toolchain", "version": "1"}]

    def resolve_binding(self, _config_id: int):
        return self._binding

    def get_toolchain(self, toolchain_id: str):
        return SimpleNamespace(display_name=toolchain_id, version="1")

    def save_binding(self, config_id: int, data: dict):
        self.saved_bindings.append((config_id, dict(data)))
        return 1


class _HardwareServiceStub:
    def query_devices_and_hostapis(self, refresh: bool = False):
        return (
            [
                {
                    "name": "Mic 1",
                    "hostapi": 0,
                    "default_samplerate": 48000,
                    "max_input_channels": 2,
                    "max_output_channels": 0,
                },
                {
                    "name": "Speaker 1",
                    "hostapi": 1,
                    "default_samplerate": 44100,
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                },
            ],
            [{"name": "ASIO"}, {"name": "WASAPI"}],
        )


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_module(
    binding: dict | None = None,
    config_service: _ConfigServiceStub | None = None,
    ai_service: _AiServiceStub | None = None,
) -> ConfigModule:
    services = SimpleNamespace(
        config_service=config_service or _ConfigServiceStub(),
        ai_service=ai_service or _AiServiceStub(binding=binding),
        hss_service=_HardwareServiceStub(),
        bus=SimpleNamespace(
            refresh_channels=_SignalStub(),
            status=_SignalStub(),
        ),
    )
    return ConfigModule(services)


def test_populate_detail_uses_main_config_analysis_offset_and_plc_fields(qapp):
    module = _make_module(
        binding={
            "enabled": True,
            "toolchain_id": "tc-1",
            "timeout_ms": 1000,
            "analysis_window_offset_ms": 900,
            "params_json": '{"legacy": true}',
        }
    )

    assert hasattr(module, "spin_analysis_window_offset")
    assert hasattr(module, "ed_plc_device_key")
    assert hasattr(module, "ed_plc_device_label")

    module._populate_detail(
        {
            "config_id": 7,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 250,
            "scan_triggers_test": False,
            "trigger_type": "plc",
            "plc_device_key": "plc-a",
            "plc_device_label": "PLC A",
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 44100,
            "selected_channels": [0],
            "channels": [],
        }
    )

    assert module.spin_analysis_window_offset.value() == 250
    assert module.ed_plc_device_key.text() == "plc-a"
    assert module.ed_plc_device_label.text() == "PLC A"


def test_collect_data_returns_main_config_analysis_offset_and_plc_fields(qapp):
    module = _make_module()

    assert hasattr(module, "spin_analysis_window_offset")
    assert hasattr(module, "ed_plc_device_key")
    assert hasattr(module, "ed_plc_device_label")

    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.spin_record.setValue(5.0)
    module.spin_analysis.setValue(3.0)
    module.spin_analysis_window_offset.setValue(500)
    module.ed_plc_device_key.setText("plc-a")
    module.ed_plc_device_label.setText("PLC A")
    module.combo_trigger.setCurrentIndex(module.combo_trigger.findData("plc"))

    data = module._collect_data()

    assert data["analysis_window_offset_ms"] == 500
    assert data["plc_device_key"] == "plc-a"
    assert data["plc_device_label"] == "PLC A"


def test_collect_ai_binding_does_not_include_analysis_window_offset(qapp):
    module = _make_module()

    ai_data = module._collect_ai_binding()

    assert "analysis_window_offset_ms" not in ai_data


def test_on_save_keeps_main_config_analysis_offset_out_of_ai_binding_payload(
    qapp, monkeypatch
):
    config_service = _ConfigServiceStub()
    ai_service = _AiServiceStub()
    module = _make_module(config_service=config_service, ai_service=ai_service)

    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.spin_analysis_window_offset.setValue(250)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    assert warnings == []
    assert config_service.saved_configs[0]["analysis_window_offset_ms"] == 250
    assert ai_service.saved_bindings == [
        (
            7,
            {
                "enabled": False,
                "toolchain_id": None,
                "timeout_ms": None,
                "params_json": "{}",
            },
        )
    ]
