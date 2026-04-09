from __future__ import annotations

import os
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from app.service.config_service import ConfigService
from app.service.golden_sample_service import GoldenSampleService
from model.channel_config_table import ChannelConfigTable
from model.test_config_table import TestConfigTable as ConfigTable
from ui.modules.config import ConfigModule
from ui.widgets.collapsible_group_box import CollapsibleGroupBox


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
    def __init__(
        self,
        *,
        loaded_configs: dict[int, dict] | None = None,
        validation_errors=None,
        golden_validation_errors=None,
        save_error: Exception | None = None,
        load_error: Exception | None = None,
    ):
        self.saved_configs = []
        self._loaded_configs = {
            config_id: dict(payload)
            for config_id, payload in (loaded_configs or {}).items()
        }
        self._validation_errors = validation_errors
        self._golden_validation_errors = golden_validation_errors
        self._save_error = save_error
        self._load_error = load_error
        self.golden_sample_service = _GoldenSampleServiceStub()
        self.begin_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    def list_configs(self):
        if self._loaded_configs:
            return [
                {
                    "config_id": config_id,
                    "config_name": payload["config_name"],
                }
                for config_id, payload in sorted(self._loaded_configs.items())
            ]
        return [
            {
                "config_id": saved["config_id"],
                "config_name": saved["config_name"],
            }
            for saved in self.saved_configs
        ]

    def load_config(self, config_id: int):
        if self._load_error is not None:
            raise self._load_error
        return dict(self._loaded_configs[config_id])

    def validate(self, data):
        if callable(self._validation_errors):
            return list(self._validation_errors(data))
        if self._validation_errors is None:
            return []
        return list(self._validation_errors)

    def validate_golden_binding(
        self,
        *,
        channel: dict,
        staged_binding: dict | None,
        analysis_duration: float,
        sample_rate,
    ):
        if callable(self._golden_validation_errors):
            return list(
                self._golden_validation_errors(
                    channel=channel,
                    staged_binding=staged_binding,
                    analysis_duration=analysis_duration,
                    sample_rate=sample_rate,
                )
            )
        if self._golden_validation_errors is None:
            return []
        return list(self._golden_validation_errors)

    def save_config(self, data):
        pending = self.begin_save_config(data)
        self.commit_pending_save(pending)
        return pending.config_id

    def begin_save_config(self, data):
        self.begin_calls += 1
        if self._save_error is not None:
            raise self._save_error
        saved = dict(data)
        self.saved_configs = [saved]
        self._loaded_configs[saved["config_id"]] = saved
        return SimpleNamespace(config_id=saved["config_id"])

    def commit_pending_save(self, pending):
        self.commit_calls += 1
        return None

    def rollback_pending_save(self, pending):
        self.rollback_calls += 1
        return None

    def create_config(self, name: str):
        config_id = max(self._loaded_configs, default=0) + 1
        self._loaded_configs[config_id] = _detail_payload(
            [], config_id=config_id, config_name=name
        )
        return config_id

    def delete_config(self, config_id: int):
        self._loaded_configs.pop(config_id, None)
        self.saved_configs = [
            saved for saved in self.saved_configs if saved["config_id"] != config_id
        ]
        return True


class _GoldenSampleServiceStub:
    def __init__(self):
        self.discarded_paths: list[str] = []

    def discard_staged_samples(self, staged_paths: list[str]):
        self.discarded_paths.extend(staged_paths)


class _AiServiceStub:
    def __init__(self, binding: dict | None = None):
        self._binding = binding
        self.saved_bindings = []
        self.enabled_toolchains = [
            {"toolchain_id": "tc-1", "display_name": "Toolchain", "version": "1"}
        ]
        self.toolchains = {}

    def list_enabled_toolchains(self):
        return list(self.enabled_toolchains)

    def resolve_binding(self, _config_id: int):
        return self._binding

    def get_toolchain(self, toolchain_id: str):
        return self.toolchains.get(
            toolchain_id,
            SimpleNamespace(display_name=toolchain_id, version="1"),
        )

    def save_binding(self, config_id: int, data: dict):
        self.saved_bindings.append((config_id, dict(data)))
        return 1


class _HardwareServiceStub:
    def __init__(self, devices: list[dict] | None = None, hostapis: list[dict] | None = None):
        self._devices = devices or [
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
        ]
        self._hostapis = hostapis or [{"name": "ASIO"}, {"name": "WASAPI"}]

    def set_devices(self, devices: list[dict], hostapis: list[dict] | None = None):
        self._devices = devices
        if hostapis is not None:
            self._hostapis = hostapis

    def query_devices_and_hostapis(self, refresh: bool = False):
        return (list(self._devices), list(self._hostapis))


@contextmanager
def _capture_qt_messages():
    messages: list[str] = []

    def _handler(_message_type, _context, message):
        messages.append(str(message))

    previous_handler = qInstallMessageHandler(_handler)
    try:
        yield messages
    finally:
        qInstallMessageHandler(previous_handler)


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
    hardware_service: _HardwareServiceStub | None = None,
    bus=None,
) -> ConfigModule:
    services_bus = bus or SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    services = SimpleNamespace(
        config_service=config_service or _ConfigServiceStub(),
        ai_service=ai_service or _AiServiceStub(binding=binding),
        hss_service=hardware_service or _HardwareServiceStub(),
        bus=services_bus,
    )
    return ConfigModule(services)


def _channel_payload(channel_index: int, **updates) -> dict:
    payload = {
        "channel_index": channel_index,
        "stft_nfft": 1024,
        "fft_nfft": 4096,
        "stft_overlap": 0.5,
        "spl_weighting": "A",
        "spl_time_constant": 0.125,
        "spl_lower_limit": 40.0,
        "spl_upper_limit": 85.0,
        "loudness_unit": "sone",
        "loudness_lower_limit": 0.0,
        "loudness_upper_limit": 60.0,
        "fft_band_limits": [],
        "oct_weighting": "A",
        "oct_band_limits": [],
        "ai_config": {},
        "golden_sample_path": None,
        "golden_sample_label": None,
        "golden_sample_source_channel": None,
        "golden_sample_start_sample": None,
        "golden_sample_num_samples": None,
        "golden_sample_start_ms": None,
        "golden_sample_duration_ms": None,
        "golden_sample_sample_rate": None,
        "fft_golden_mode": "off",
        "fft_show_golden_overlay": False,
        "oct_golden_mode": "off",
        "oct_show_golden_overlay": False,
    }
    payload.update(updates)
    return payload


def _golden_binding(
    path: str = "D:/managed/golden.wav",
    *,
    label: str = "gold.wav",
    source_channel: int = 1,
    start_sample: int = 24_000,
    num_samples: int = 144_000,
    sample_rate: float = 48_000.0,
) -> dict:
    return {
        "golden_sample_path": path,
        "golden_sample_label": label,
        "golden_sample_source_channel": source_channel,
        "golden_sample_start_sample": start_sample,
        "golden_sample_num_samples": num_samples,
        "golden_sample_start_ms": round(start_sample * 1000 / sample_rate),
        "golden_sample_duration_ms": round(num_samples * 1000 / sample_rate),
        "golden_sample_sample_rate": sample_rate,
    }


def _staged_golden_binding(
    path: Path | str,
    *,
    label: str = "pending.wav",
    source_channel: int = 1,
    start_sample: int = 24_000,
    num_samples: int = 144_000,
    sample_rate: float = 48_000.0,
) -> dict:
    return {
        "staged_path": str(path),
        "label": label,
        "source_channel": source_channel,
        "start_sample": start_sample,
        "num_samples": num_samples,
        "start_ms": round(start_sample * 1000 / sample_rate),
        "duration_ms": round(num_samples * 1000 / sample_rate),
        "sample_rate": sample_rate,
    }


def _detail_payload(
    channels: list[dict],
    *,
    config_id: int = 7,
    config_name: str = "Cfg1",
) -> dict:
    return {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "analysis_window_offset_ms": 250,
        "allow_incomplete_recording_analysis": False,
        "scan_triggers_test": False,
        "plc_device_key": "plc-a",
        "plc_device_label": "PLC A",
        "input_device_name": "Mic 1",
        "input_device_hostapi": "ASIO",
        "input_device_samplerate": 48000,
        "output_device_name": "Speaker 1",
        "output_device_hostapi": "WASAPI",
        "output_device_samplerate": 44100,
        "selected_channels": [0],
        "channels": channels,
    }


def _field_label_text(container: QWidget, object_name: str) -> str | None:
    field = container.findChild(QWidget, object_name)
    assert field is not None
    parent = field.parentWidget()
    while parent is not None:
        layout = parent.layout()
        if isinstance(layout, QFormLayout):
            row, _role = layout.getWidgetPosition(field)
            if row >= 0:
                label_item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                label = label_item.widget() if label_item else None
                return label.text() if isinstance(label, QLabel) else None
        parent = parent.parentWidget()
    return None


def _ancestor_group_title(widget: QWidget | None) -> str | None:
    parent = widget.parentWidget() if widget is not None else None
    while parent is not None:
        if isinstance(parent, CollapsibleGroupBox):
            return getattr(parent, "_title", None)
        parent = parent.parentWidget()
    return None


def _write_temp_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    samples: int = 144_000,
    channels: int = 1,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = np.linspace(-0.8, 0.8, num=samples, dtype=np.float32)
    if channels <= 1:
        data = base
    else:
        data = np.column_stack([base + float(index) for index in range(channels)]).astype(
            np.float32
        )
    wavfile.write(path, sample_rate, data)
    return path


def test_collapsible_group_box_exposes_public_section_state_api(qapp):
    box = CollapsibleGroupBox("FFT 频带上下限", section_id="fft")

    assert box.section_id() == "fft"
    assert box.is_expanded() is True

    box.set_expanded(False)

    assert box.is_expanded() is False


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
    assert not hasattr(module, "combo_trigger")

    module._populate_detail(
        {
            "config_id": 7,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "analysis_window_offset_ms": 250,
            "scan_triggers_test": False,
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


def test_checkbox_uses_approved_visible_label_for_allow_incomplete_recording_analysis(
    qapp,
):
    module = _make_module()

    assert module.chk_allow_incomplete_recording_analysis.text() == "允许分析不完整录音"


def test_populate_detail_loads_allow_incomplete_recording_analysis_checkbox(qapp):
    module = _make_module()
    payload = _detail_payload([])
    payload["allow_incomplete_recording_analysis"] = True

    module._populate_detail(payload)

    assert module.chk_allow_incomplete_recording_analysis.isChecked() is True


def test_checkbox_defaults_to_unchecked_for_newly_loaded_config(qapp):
    module = _make_module()

    module._populate_detail(_detail_payload([]))

    assert module.chk_allow_incomplete_recording_analysis.isChecked() is False


def test_collect_data_returns_main_config_analysis_offset_and_plc_fields(qapp):
    module = _make_module()

    assert hasattr(module, "spin_analysis_window_offset")
    assert hasattr(module, "ed_plc_device_key")
    assert hasattr(module, "ed_plc_device_label")
    assert not hasattr(module, "combo_trigger")

    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.spin_record.setValue(5.0)
    module.spin_analysis.setValue(3.0)
    module.spin_analysis_window_offset.setValue(500)
    module.ed_plc_device_key.setText("plc-a")
    module.ed_plc_device_label.setText("PLC A")

    data = module._collect_data()

    assert data["analysis_window_offset_ms"] == 500
    assert data["plc_device_key"] == "plc-a"
    assert data["plc_device_label"] == "PLC A"
    assert data["allow_incomplete_recording_analysis"] is False
    assert data["golden_editor_state"] == {}
    assert set(data) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
        "analysis_window_offset_ms",
        "allow_incomplete_recording_analysis",
        "scan_triggers_test",
        "plc_device_key",
        "plc_device_label",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "channels",
        "golden_editor_state",
    }


def test_collect_data_includes_allow_incomplete_recording_analysis(qapp):
    module = _make_module()
    module._current_config_id = 7
    module.chk_allow_incomplete_recording_analysis.setChecked(True)

    data = module._collect_data()

    assert data["allow_incomplete_recording_analysis"] is True


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
        "ui.modules.config.QMessageBox.information",
        lambda *args: None,
    )
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


def test_on_save_persists_allow_incomplete_recording_analysis_without_touching_ai_binding(
    qapp, monkeypatch
):
    config_service = _ConfigServiceStub()
    ai_service = _AiServiceStub()
    module = _make_module(config_service=config_service, ai_service=ai_service)

    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.chk_allow_incomplete_recording_analysis.setChecked(True)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: None,
    )

    module._on_save()

    assert config_service.saved_configs[0]["allow_incomplete_recording_analysis"] is True
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


def test_ai_toolchains_changed_refreshes_clean_binding_editor(qapp):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "tc-1",
            "timeout_ms": 1000,
            "params_json": '{"mode": "v1"}',
        }
    )
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-1", "display_name": "Toolchain 1", "version": "1"},
    ]
    module = _make_module(ai_service=ai_service, bus=bus)
    module._current_config_id = 1
    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-2",
        "timeout_ms": 2000,
        "params_json": '{"mode": "v2"}',
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-2", "display_name": "Toolchain 2", "version": "2"},
    ]

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-2"
    assert module.spin_ai_timeout.value() == 2000
    assert module.txt_ai_params.toPlainText() == '{"mode": "v2"}'


def test_ai_toolchains_changed_preserves_dirty_local_ai_edits(qapp):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "tc-1",
            "timeout_ms": 1000,
            "params_json": '{"mode": "server"}',
        }
    )
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-1", "display_name": "Toolchain 1", "version": "1"},
        {
            "toolchain_id": "tc-local",
            "display_name": "Toolchain Local",
            "version": "9",
        },
    ]
    module = _make_module(ai_service=ai_service, bus=bus)
    module._current_config_id = 1
    module._populate_ai_binding(1)
    module.chk_ai_enabled.setChecked(True)
    module.combo_ai_toolchain.setCurrentIndex(
        module.combo_ai_toolchain.findData("tc-local")
    )
    module.spin_ai_timeout.setValue(9000)
    module.txt_ai_params.setPlainText('{"mode": "local"}')
    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-2",
        "timeout_ms": 2000,
        "params_json": '{"mode": "server-v2"}',
    }
    ai_service.enabled_toolchains = [
        {
            "toolchain_id": "tc-local",
            "display_name": "Toolchain Local",
            "version": "9",
        },
        {"toolchain_id": "tc-2", "display_name": "Toolchain 2", "version": "2"},
    ]

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-local"
    assert module.spin_ai_timeout.value() == 9000
    assert module.txt_ai_params.toPlainText() == '{"mode": "local"}'


def test_ai_toolchains_changed_reloads_after_local_ai_edits_revert_to_clean_state(qapp):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "tc-1",
            "timeout_ms": 1000,
            "params_json": '{"mode": "v1"}',
        }
    )
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-1", "display_name": "Toolchain 1", "version": "1"},
        {"toolchain_id": "tc-local", "display_name": "Toolchain Local", "version": "9"},
    ]
    module = _make_module(ai_service=ai_service, bus=bus)
    module._current_config_id = 1
    module._populate_ai_binding(1)

    module.combo_ai_toolchain.setCurrentIndex(
        module.combo_ai_toolchain.findData("tc-local")
    )
    module.spin_ai_timeout.setValue(9000)
    module.txt_ai_params.setPlainText('{"mode": "local"}')

    module.combo_ai_toolchain.setCurrentIndex(module.combo_ai_toolchain.findData("tc-1"))
    module.spin_ai_timeout.setValue(1000)
    module.txt_ai_params.setPlainText('{"mode": "v1"}')

    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-2",
        "timeout_ms": 2000,
        "params_json": '{"mode": "v2"}',
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-2", "display_name": "Toolchain 2", "version": "2"},
    ]

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-2"
    assert module.spin_ai_timeout.value() == 2000
    assert module.txt_ai_params.toPlainText() == '{"mode": "v2"}'


def test_unavailable_toolchain_loaded_binding_injects_unavailable_toolchain_option(qapp):
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "disabled-tc",
            "timeout_ms": 1000,
            "params_json": '{"mode": "v1"}',
        }
    )
    ai_service.enabled_toolchains = []
    ai_service.toolchains["disabled-tc"] = SimpleNamespace(
        display_name="Disabled Toolchain",
        version="2",
    )
    module = _make_module(ai_service=ai_service)

    module._populate_ai_binding(1)

    assert module.combo_ai_toolchain.currentData() == "disabled-tc"
    assert module.combo_ai_toolchain.itemText(1) == "Disabled Toolchain (v2) [不可用]"


def test_save_rejects_dirty_stale_toolchain_selection(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )

    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    ai_service = _AiServiceStub()
    ai_service.enabled_toolchains = []
    module = _make_module(
        config_service=config_service,
        ai_service=ai_service,
        bus=bus,
    )
    module._current_config_id = 1
    module.chk_ai_enabled.setChecked(True)
    module.combo_ai_toolchain.addItem("Ghost", "gone")
    module.combo_ai_toolchain.setCurrentIndex(module.combo_ai_toolchain.count() - 1)

    module._on_save()

    assert warnings == [("AI 配置错误", "AI 工具链不可用，请重新选择或清空绑定")]
    assert config_service.begin_calls == 0
    assert ai_service.saved_bindings == []


def test_ai_toolchains_changed_keeps_dirty_renamed_stale_toolchain_until_save(
    qapp,
    monkeypatch,
):
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args[1:3]),
    )

    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "tc-old",
            "timeout_ms": 1000,
            "params_json": '{"mode": "v1"}',
        }
    )
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-old", "display_name": "Toolchain Old", "version": "1"},
    ]
    module = _make_module(
        config_service=config_service,
        ai_service=ai_service,
        bus=bus,
    )
    module._current_config_id = 1
    module._populate_ai_binding(1)
    module.txt_ai_params.setPlainText('{"mode": "draft"}')
    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-new",
        "timeout_ms": 2000,
        "params_json": '{"mode": "v2"}',
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-new", "display_name": "Toolchain New", "version": "2"},
    ]
    ai_service.toolchains["tc-old"] = None

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-old"
    assert module.txt_ai_params.toPlainText() == '{"mode": "draft"}'

    module._on_save()

    assert warnings == [("AI 配置错误", "AI 工具链不可用，请重新选择或清空绑定")]
    assert config_service.begin_calls == 0
    assert ai_service.saved_bindings == []


def test_on_save_preserves_selected_channel_and_collapsed_sections_and_shows_success(
    qapp, monkeypatch
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service, bus=bus)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0), _channel_payload(1)]))
    first_container = module._channel_containers[0]
    module._on_channel_tab_clicked(1)

    selected_container = module.channel_stack.currentWidget()
    first_spl_box = next(
        box
        for box in first_container.findChildren(CollapsibleGroupBox)
        if box.section_id() == "spl"
    )
    fft_box = next(
        box
        for box in selected_container.findChildren(CollapsibleGroupBox)
        if box.section_id() == "fft"
    )
    spl_box = next(
        box
        for box in selected_container.findChildren(CollapsibleGroupBox)
        if box.section_id() == "spl"
    )
    first_spl_box.set_expanded(False)
    fft_box.set_expanded(False)
    spl_box.set_expanded(True)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    status_count_at_info = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: (
            status_count_at_info.append(len(bus.status.emissions)),
            infos.append(args),
        ),
    )

    module._on_save()

    restored_container = module.channel_stack.currentWidget()
    restored_first_container = next(
        container
        for container in module._channel_containers
        if container.property("channel_index") == 0
    )
    restored_first_boxes = {
        box.section_id(): box
        for box in restored_first_container.findChildren(CollapsibleGroupBox)
    }
    restored_boxes = {
        box.section_id(): box
        for box in restored_container.findChildren(CollapsibleGroupBox)
    }

    assert module._current_config_id == 7
    assert module.config_list.currentItem().data(Qt.ItemDataRole.UserRole) == 7
    assert set(restored_boxes) == {
        "stft",
        "spl",
        "loudness",
        "golden_sample",
        "fft",
        "octave",
    }
    assert restored_container.property("channel_index") == 1
    assert restored_first_boxes["spl"].is_expanded() is False
    assert restored_boxes["fft"].is_expanded() is False
    assert restored_boxes["spl"].is_expanded() is True
    assert bus.status.emissions == [(("配置已保存",), {})]
    assert status_count_at_info == [1]
    assert infos[-1][-2:] == ("保存成功", "配置保存成功！")


def test_on_save_falls_back_to_first_available_channel_when_selected_channel_disappears(
    qapp, monkeypatch
):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 1",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 2,
                "max_output_channels": 0,
            },
            {
                "name": "Mic Small",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 1,
                "max_output_channels": 0,
            },
            {
                "name": "Speaker 1",
                "hostapi": 1,
                "default_samplerate": 44100,
                "max_input_channels": 0,
                "max_output_channels": 2,
            },
        ]
    )
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    module = _make_module(
        config_service=config_service,
        hardware_service=hardware_service,
        bus=bus,
    )
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0), _channel_payload(1)]))
    module._on_channel_tab_clicked(1)

    monkeypatch.setattr(
        config_service,
        "load_config",
        lambda _config_id: {
            **_detail_payload([_channel_payload(0)]),
            "input_device_name": "Mic Small",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
        },
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr("ui.modules.config.QMessageBox.information", lambda *args: None)

    module._on_save()

    assert module.channel_stack.currentWidget().property("channel_index") == 0


def test_on_save_keeps_no_channel_state_when_reloaded_editor_has_no_channels(
    qapp, monkeypatch
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service, bus=bus)
    module.show()
    qapp.processEvents()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    monkeypatch.setattr(
        config_service,
        "load_config",
        lambda _config_id: {
            **_detail_payload([]),
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "selected_channels": [],
        },
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )

    module._on_save()
    qapp.processEvents()

    assert module.channel_host.isVisible() is False
    assert module.channel_warning.isVisible() is True
    assert module.channel_stack.count() == 0
    assert infos


def test_on_save_failure_does_not_show_success_feedback(qapp, monkeypatch):
    config_service = _ConfigServiceStub(save_error=ValueError("save exploded"))
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    module = _make_module(config_service=config_service, bus=bus)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    assert infos == []
    assert warnings
    assert bus.status.emissions == []


def test_populate_detail_loads_loudness_widgets(qapp):
    module = _make_module()

    module._populate_detail(
        _detail_payload(
            [
                {
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "A",
                    "spl_time_constant": 0.125,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 85.0,
                    "loudness_unit": "phon",
                    "loudness_lower_limit": 12.5,
                    "loudness_upper_limit": 70.0,
                    "fft_band_limits": [],
                    "oct_weighting": "A",
                    "oct_band_limits": [],
                    "ai_config": {},
                }
            ]
        )
    )

    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "loudness_unit")
    lower = container.findChild(QDoubleSpinBox, "loudness_lower_limit")
    upper = container.findChild(QDoubleSpinBox, "loudness_upper_limit")

    assert combo is not None
    assert lower is not None
    assert upper is not None
    assert combo.currentText() == "phon"
    assert lower.value() == pytest.approx(12.5)
    assert upper.value() == pytest.approx(70.0)


def test_collect_data_returns_loudness_fields(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([]))

    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "loudness_unit")
    lower = container.findChild(QDoubleSpinBox, "loudness_lower_limit")
    upper = container.findChild(QDoubleSpinBox, "loudness_upper_limit")

    combo.setCurrentText("phon")
    lower.setValue(10.0)
    upper.setValue(60.0)

    data = module._collect_data()

    assert data["channels"][0]["loudness_unit"] == "phon"
    assert data["channels"][0]["loudness_lower_limit"] == pytest.approx(10.0)
    assert data["channels"][0]["loudness_upper_limit"] == pytest.approx(60.0)


def test_populate_detail_loads_fft_resolution_in_fft_section(qapp):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    container = module._channel_containers[0]
    fft_resolution = container.findChild(QComboBox, "fft_nfft")
    stft_nfft = container.findChild(QComboBox, "stft_nfft")

    assert fft_resolution is not None
    assert fft_resolution.currentData() == 4096
    assert _field_label_text(container, "fft_nfft") == "FFT 分辨率"
    assert _ancestor_group_title(fft_resolution) == "FFT 频带上下限"
    assert _ancestor_group_title(stft_nfft) == "STFT 参数"


def test_collect_data_returns_fft_resolution_from_fft_section(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    container = module._channel_containers[0]
    fft_resolution = container.findChild(QComboBox, "fft_nfft")
    assert fft_resolution is not None
    fft_resolution.setCurrentIndex(fft_resolution.findData(2048))

    data = module._collect_data()

    assert data["channels"][0]["fft_nfft"] == 2048


def test_loudness_unit_change_keeps_numeric_limits_visible_as_is(qapp):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                {
                    "channel_index": 0,
                    "stft_nfft": 1024,
                    "stft_overlap": 0.5,
                    "spl_weighting": "A",
                    "spl_time_constant": 0.125,
                    "spl_lower_limit": 40.0,
                    "spl_upper_limit": 85.0,
                    "loudness_unit": "sone",
                    "loudness_lower_limit": 8.5,
                    "loudness_upper_limit": 42.0,
                    "fft_band_limits": [],
                    "oct_weighting": "A",
                    "oct_band_limits": [],
                    "ai_config": {},
                }
            ]
        )
    )

    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "loudness_unit")
    lower = container.findChild(QDoubleSpinBox, "loudness_lower_limit")
    upper = container.findChild(QDoubleSpinBox, "loudness_upper_limit")

    combo.setCurrentText("phon")

    assert lower.value() == pytest.approx(8.5)
    assert upper.value() == pytest.approx(42.0)


def test_config_module_build_channel_tabs_emit_no_unknown_cursor_warning(qapp):
    module = _make_module()

    with _capture_qt_messages() as messages:
        module._build_channel_tabs([0], None)

    assert module.channel_stack.count() == 1
    assert all("Unknown property cursor" not in message for message in messages)

    module.close()


def test_populate_detail_renders_golden_summary_and_controls(qapp):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    **_golden_binding(),
                    fft_golden_mode="absolute",
                    fft_show_golden_overlay=True,
                    oct_golden_mode="delta",
                    oct_show_golden_overlay=False,
                )
            ]
        )
    )

    container = module._channel_containers[0]

    label = container.findChild(QLabel, "golden_sample_label_value")
    sample_rate = container.findChild(QLabel, "golden_sample_sample_rate_value")
    source_channel = container.findChild(QLabel, "golden_sample_source_channel_value")
    start_ms = container.findChild(QLabel, "golden_sample_start_ms_value")
    duration_ms = container.findChild(QLabel, "golden_sample_duration_ms_value")
    status = container.findChild(QLabel, "golden_sample_status_value")
    import_button = container.findChild(QPushButton, "golden_sample_import_button")
    replace_button = container.findChild(QPushButton, "golden_sample_replace_button")
    clear_button = container.findChild(QPushButton, "golden_sample_clear_button")
    fft_mode = container.findChild(QComboBox, "fft_golden_mode")
    fft_overlay = container.findChild(QCheckBox, "fft_show_golden_overlay")
    oct_mode = container.findChild(QComboBox, "oct_golden_mode")
    oct_overlay = container.findChild(QCheckBox, "oct_show_golden_overlay")

    assert label is not None
    assert sample_rate is not None
    assert source_channel is not None
    assert start_ms is not None
    assert duration_ms is not None
    assert status is not None
    assert import_button is not None
    assert replace_button is not None
    assert clear_button is not None
    assert fft_mode is not None
    assert fft_overlay is not None
    assert oct_mode is not None
    assert oct_overlay is not None
    assert label.text() == "gold.wav"
    assert "48000" in sample_rate.text()
    assert source_channel.text() == "2"
    assert "500" in start_ms.text()
    assert "3000" in duration_ms.text()
    assert "有效" in status.text()
    assert import_button.text() == "Import WAV..."
    assert replace_button.text() == "Replace WAV..."
    assert clear_button.text() == "Clear"
    assert fft_mode.currentText() == "Absolute"
    assert fft_overlay.isChecked() is True
    assert oct_mode.currentText() == "Delta"
    assert oct_overlay.isChecked() is False
    assert oct_overlay.isEnabled() is False


@pytest.mark.parametrize(
    ("mode_name", "overlay_name"),
    [
        ("fft_golden_mode", "fft_show_golden_overlay"),
        ("oct_golden_mode", "oct_show_golden_overlay"),
    ],
)
def test_delta_mode_disables_overlay_checkbox(qapp, mode_name, overlay_name):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, mode_name)
    checkbox = container.findChild(QCheckBox, overlay_name)

    checkbox.setChecked(True)
    combo.setCurrentText("Delta")

    assert checkbox.isChecked() is False
    assert checkbox.isEnabled() is False


def test_collect_data_includes_golden_fields_and_editor_only_staged_metadata(qapp):
    module = _make_module()
    committed = _channel_payload(
        0,
        **_golden_binding(),
        fft_golden_mode="absolute",
        fft_show_golden_overlay=True,
        oct_golden_mode="off",
        oct_show_golden_overlay=False,
    )
    module._populate_detail(_detail_payload([committed]))
    module._golden_editor_state[0] = _staged_golden_binding(
        "D:/temp/staged.wav",
        label="pending.wav",
        source_channel=2,
        start_sample=12_000,
    )

    data = module._collect_data()
    channel = data["channels"][0]

    assert channel["golden_sample_path"] == committed["golden_sample_path"]
    assert channel["golden_sample_label"] == committed["golden_sample_label"]
    assert channel["golden_sample_source_channel"] == committed["golden_sample_source_channel"]
    assert channel["golden_sample_start_sample"] == committed["golden_sample_start_sample"]
    assert channel["golden_sample_num_samples"] == committed["golden_sample_num_samples"]
    assert channel["golden_sample_start_ms"] == committed["golden_sample_start_ms"]
    assert channel["golden_sample_duration_ms"] == committed["golden_sample_duration_ms"]
    assert channel["golden_sample_sample_rate"] == committed["golden_sample_sample_rate"]
    assert channel["fft_golden_mode"] == "absolute"
    assert channel["fft_show_golden_overlay"] is True
    assert channel["oct_golden_mode"] == "off"
    assert channel["oct_show_golden_overlay"] is False
    assert data["golden_editor_state"] == {
        0: _staged_golden_binding(
            "D:/temp/staged.wav",
            label="pending.wav",
            source_channel=2,
            start_sample=12_000,
        )
    }


def test_analysis_duration_change_marks_binding_invalid_without_rewriting_golden_settings(
    qapp,
):
    def _golden_errors(*, channel, analysis_duration, **_kwargs):
        if channel.get("golden_sample_path") and analysis_duration != 3.0:
            return ["黄金样本长度与当前分析时长不一致"]
        return []

    module = _make_module(
        config_service=_ConfigServiceStub(golden_validation_errors=_golden_errors)
    )
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    **_golden_binding(),
                    fft_golden_mode="absolute",
                    fft_show_golden_overlay=True,
                    oct_golden_mode="off",
                    oct_show_golden_overlay=False,
                )
            ]
        )
    )

    container = module._channel_containers[0]
    fft_mode = container.findChild(QComboBox, "fft_golden_mode")
    fft_overlay = container.findChild(QCheckBox, "fft_show_golden_overlay")
    status = container.findChild(QLabel, "golden_sample_status_value")

    module.spin_analysis.setValue(2.0)
    qapp.processEvents()

    assert fft_mode.currentText() == "Absolute"
    assert fft_overlay.isChecked() is True
    assert "无效" in status.text()
    assert "分析时长" in status.text()


def test_clear_discards_staged_file_and_preserves_existing_golden_settings(qapp, tmp_path):
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service)
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    **_golden_binding(),
                    fft_golden_mode="absolute",
                    fft_show_golden_overlay=True,
                    oct_golden_mode="delta",
                    oct_show_golden_overlay=False,
                )
            ]
        )
    )
    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(staged_path)

    container = module._channel_containers[0]
    clear_button = container.findChild(QPushButton, "golden_sample_clear_button")

    clear_button.click()

    data = module._collect_data()
    channel = data["channels"][0]

    assert channel["golden_sample_path"] is None
    assert channel["golden_sample_label"] is None
    assert channel["golden_sample_source_channel"] is None
    assert channel["golden_sample_start_sample"] is None
    assert channel["golden_sample_num_samples"] is None
    assert channel["golden_sample_start_ms"] is None
    assert channel["golden_sample_duration_ms"] is None
    assert channel["golden_sample_sample_rate"] is None
    assert channel["fft_golden_mode"] == "absolute"
    assert channel["fft_show_golden_overlay"] is True
    assert channel["oct_golden_mode"] == "delta"
    assert channel["oct_show_golden_overlay"] is False
    assert data["golden_editor_state"] == {}
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


def test_selection_change_discards_unsaved_staged_files(qapp, tmp_path):
    config_service = _ConfigServiceStub(
        loaded_configs={
            7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
            8: _detail_payload([_channel_payload(0)], config_id=8, config_name="Cfg2"),
        }
    )
    module = _make_module(config_service=config_service)
    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(staged_path)

    module.config_list.setCurrentRow(1)
    qapp.processEvents()

    assert module._current_config_id == 8
    assert module.ed_name.text() == "Cfg2"
    assert module._golden_editor_state == {}
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


def test_reloading_detail_discards_unsaved_staged_files(qapp, tmp_path):
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service)
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(staged_path)

    module._populate_detail(_detail_payload([_channel_payload(0)], config_name="Reloaded"))

    assert module.ed_name.text() == "Reloaded"
    assert module._golden_editor_state == {}
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


@pytest.mark.parametrize(
    "save_error",
    [ValueError("save exploded"), RuntimeError("save exploded")],
)
def test_save_failure_discards_staged_file_and_keeps_committed_binding_authoritative(
    qapp,
    tmp_path,
    monkeypatch,
    save_error,
):
    committed = _channel_payload(0, **_golden_binding())
    config_service = _ConfigServiceStub(save_error=save_error)
    module = _make_module(config_service=config_service)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([committed]))
    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(
        staged_path,
        label="replacement.wav",
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    data = module._collect_data()

    assert any("save exploded" in str(args[-1]) for args in warnings)
    assert data["channels"][0]["golden_sample_path"] == committed["golden_sample_path"]
    assert data["channels"][0]["golden_sample_label"] == committed["golden_sample_label"]
    assert data["golden_editor_state"] == {}
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


def test_validation_failure_discards_staged_file_and_keeps_committed_binding_authoritative(
    qapp,
    tmp_path,
    monkeypatch,
):
    committed = _channel_payload(0, **_golden_binding())
    config_service = _ConfigServiceStub(validation_errors=["配置无效"])
    module = _make_module(config_service=config_service)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([committed]))
    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(
        staged_path,
        label="replacement.wav",
    )

    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    data = module._collect_data()

    assert any("配置无效" in str(args[-1]) for args in warnings)
    assert data["channels"][0]["golden_sample_path"] == committed["golden_sample_path"]
    assert data["channels"][0]["golden_sample_label"] == committed["golden_sample_label"]
    assert data["golden_editor_state"] == {}
    assert config_service.begin_calls == 0
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


def test_invalid_ai_json_discards_staged_file_and_keeps_committed_binding_authoritative(
    qapp,
    tmp_path,
    monkeypatch,
):
    committed = _channel_payload(0, **_golden_binding())
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([committed]))
    module.txt_ai_params.setPlainText("{invalid")
    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(
        staged_path,
        label="replacement.wav",
    )

    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    data = module._collect_data()

    assert any("参数(JSON) 格式无效" in str(args[-1]) for args in warnings)
    assert data["channels"][0]["golden_sample_path"] == committed["golden_sample_path"]
    assert data["channels"][0]["golden_sample_label"] == committed["golden_sample_label"]
    assert data["golden_editor_state"] == {}
    assert config_service.begin_calls == 0
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


def test_outer_transaction_failure_after_begin_save_config_rolls_back_promotions_and_keeps_old_asset(
    tmp_path,
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    config_service = ConfigService(bus, golden_sample_service=golden_service)

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48_000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44_100,
        selected_channels="[0]",
    ).config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload(0, **_golden_binding(str(old_path))),
    )

    source_path = _write_temp_wav(
        tmp_path / "source.wav",
        samples=200_000,
        channels=2,
    )
    staged = golden_service.create_staged_sample(
        source_path=str(source_path),
        source_channel=1,
        start_sample=24_000,
        num_samples=144_000,
        sample_rate=48_000.0,
        label="replacement.wav",
    )
    staged_path = Path(staged.staged_path)
    payload = _detail_payload(
        [_channel_payload(0, **_golden_binding(str(old_path)))],
        config_id=config_id,
    )
    payload["selected_channels"] = [0]
    payload["golden_editor_state"] = {
        0: _staged_golden_binding(
            staged_path,
            label=staged.label,
            source_channel=staged.source_channel,
            start_sample=staged.start_sample,
            num_samples=staged.num_samples,
            sample_rate=staged.sample_rate,
        )
    }

    pending = None
    with pytest.raises(RuntimeError, match="outer exploded"):
        with ConfigTable._meta.database.atomic():
            pending = config_service.begin_save_config(payload)
            raise RuntimeError("outer exploded")

    config_service.rollback_pending_save(pending)

    loaded = config_service.load_config(config_id)
    committed_files = sorted((tmp_path / "managed").glob("config_*/*.wav"))

    assert bus.config_changed.emissions == []
    assert loaded["channels"][0]["golden_sample_path"] == str(old_path)
    assert old_path.exists()
    assert committed_files == [old_path]
    assert staged_path.exists()


def test_input_device_channel_reduction_discards_removed_channel_staged_bindings(
    qapp,
    tmp_path,
):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 1",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 2,
                "max_output_channels": 0,
            },
            {
                "name": "Mic Small",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 1,
                "max_output_channels": 0,
            },
            {
                "name": "Speaker 1",
                "hostapi": 1,
                "default_samplerate": 44100,
                "max_input_channels": 0,
                "max_output_channels": 2,
            },
        ]
    )
    config_service = _ConfigServiceStub()
    module = _make_module(
        config_service=config_service,
        hardware_service=hardware_service,
    )
    payload = _detail_payload(
        [_channel_payload(0), _channel_payload(1)]
    )
    payload["selected_channels"] = [0, 1]
    module._populate_detail(payload)

    staged_path = tmp_path / "removed-channel.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[1] = _staged_golden_binding(staged_path)

    mic_small_index = module.combo_input_dev.findText("Mic Small (ASIO)")
    assert mic_small_index >= 0

    module.combo_input_dev.setCurrentIndex(mic_small_index)
    qapp.processEvents()

    data = module._collect_data()

    assert [channel["channel_index"] for channel in data["channels"]] == [0]
    assert data["golden_editor_state"] == {}
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]


def test_post_commit_reload_failure_does_not_rollback_committed_pending_save(
    qapp,
    monkeypatch,
):
    config_service = _ConfigServiceStub(load_error=RuntimeError("reload exploded"))
    module = _make_module(config_service=config_service)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    assert infos == []
    assert module.services.bus.status.emissions == []
    assert config_service.begin_calls == 1
    assert config_service.commit_calls == 1
    assert config_service.rollback_calls == 0
    assert any("reload exploded" in str(args[-1]) for args in warnings)


def test_post_commit_failure_reloads_authoritative_config_list_and_detail(
    qapp,
    monkeypatch,
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48_000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44_100,
        selected_channels="[0]",
    ).config_id
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload(0),
    )
    config_service = ConfigService(bus)
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_HardwareServiceStub(),
        bus=bus,
    )

    module._current_config_id = config_id
    module._populate_detail(config_service.load_config(config_id))
    module.ed_name.setText("  Renamed  ")

    def _raise_post_commit(*_args, **_kwargs):
        raise RuntimeError("post-commit exploded")

    monkeypatch.setattr(bus.config_changed, "emit", _raise_post_commit)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    infos = []
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    assert infos == []
    assert bus.status.emissions == []
    assert any("post-commit exploded" in str(args[-1]) for args in warnings)
    assert config_service.load_config(config_id)["config_name"] == "Renamed"
    assert module.ed_name.text() == "Renamed"
    assert module.config_list.currentItem().text() == "Renamed"


def test_post_commit_commit_pending_save_failure_keeps_new_authoritative_replacement_binding(
    qapp,
    tmp_path,
    monkeypatch,
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    config_service = ConfigService(bus, golden_sample_service=golden_service)
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_HardwareServiceStub(),
        bus=bus,
    )

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48_000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44_100,
        selected_channels="[0]",
    ).config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload(0, **_golden_binding(str(old_path))),
    )

    module._current_config_id = config_id
    original_load_config = config_service.load_config
    module._populate_detail(original_load_config(config_id))

    source_path = _write_temp_wav(
        tmp_path / "source.wav",
        samples=200_000,
        channels=2,
    )
    staged = golden_service.create_staged_sample(
        source_path=str(source_path),
        source_channel=1,
        start_sample=24_000,
        num_samples=144_000,
        sample_rate=48_000.0,
        label="replacement.wav",
    )
    staged_path = Path(staged.staged_path)
    module._golden_editor_state[0] = _staged_golden_binding(
        staged_path,
        label=staged.label,
        source_channel=staged.source_channel,
        start_sample=staged.start_sample,
        num_samples=staged.num_samples,
        sample_rate=staged.sample_rate,
    )

    def _raise_post_commit(*_args, **_kwargs):
        raise RuntimeError("post-commit exploded")

    monkeypatch.setattr(bus.config_changed, "emit", _raise_post_commit)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    authoritative = original_load_config(config_id)
    editor_data = module._collect_data()
    editor_channel = editor_data["channels"][0]
    authoritative_path = authoritative["channels"][0]["golden_sample_path"]

    assert any("post-commit exploded" in str(args[-1]) for args in warnings)
    assert authoritative_path != str(old_path)
    assert Path(authoritative_path).exists()
    assert old_path.exists() is False
    assert staged_path.exists() is False
    assert editor_channel["golden_sample_path"] == authoritative_path
    assert editor_data["golden_editor_state"] == {}


def test_post_commit_reload_failure_keeps_new_authoritative_replacement_binding(
    qapp,
    tmp_path,
    monkeypatch,
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    config_service = ConfigService(bus, golden_sample_service=golden_service)
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_HardwareServiceStub(),
        bus=bus,
    )

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48_000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44_100,
        selected_channels="[0]",
    ).config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload(0, **_golden_binding(str(old_path))),
    )

    module._current_config_id = config_id
    original_load_config = config_service.load_config
    module._populate_detail(original_load_config(config_id))

    source_path = _write_temp_wav(
        tmp_path / "source.wav",
        samples=200_000,
        channels=2,
    )
    staged = golden_service.create_staged_sample(
        source_path=str(source_path),
        source_channel=1,
        start_sample=24_000,
        num_samples=144_000,
        sample_rate=48_000.0,
        label="replacement.wav",
    )
    staged_path = Path(staged.staged_path)
    module._golden_editor_state[0] = _staged_golden_binding(
        staged_path,
        label=staged.label,
        source_channel=staged.source_channel,
        start_sample=staged.start_sample,
        num_samples=staged.num_samples,
        sample_rate=staged.sample_rate,
    )

    def _raise_after_commit(_config_id: int):
        raise RuntimeError("reload exploded")

    monkeypatch.setattr(config_service, "load_config", _raise_after_commit)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    authoritative = original_load_config(config_id)
    editor_data = module._collect_data()
    editor_channel = editor_data["channels"][0]
    authoritative_path = authoritative["channels"][0]["golden_sample_path"]

    assert any("reload exploded" in str(args[-1]) for args in warnings)
    assert old_path.exists() is False
    assert staged_path.exists() is False
    assert authoritative_path != str(old_path)
    assert editor_channel["golden_sample_path"] == authoritative_path
    assert editor_channel["golden_sample_path"] != str(old_path)
    assert editor_data["golden_editor_state"] == {}


def test_post_commit_reload_failure_keeps_new_authoritative_cleared_binding(
    qapp,
    tmp_path,
    monkeypatch,
):
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    golden_service = GoldenSampleService(root_dir=str(tmp_path / "managed"))
    config_service = ConfigService(bus, golden_sample_service=golden_service)
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_HardwareServiceStub(),
        bus=bus,
    )

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_device_name="Mic 1",
        input_device_hostapi="ASIO",
        input_device_samplerate=48_000,
        output_device_name="Speaker 1",
        output_device_hostapi="WASAPI",
        output_device_samplerate=44_100,
        selected_channels="[0]",
    ).config_id
    old_path = _write_temp_wav(tmp_path / "managed" / "config_1" / "old.wav")
    ChannelConfigTable.create(
        config=config_id,
        **_channel_payload(0, **_golden_binding(str(old_path))),
    )

    module._current_config_id = config_id
    original_load_config = config_service.load_config
    module._populate_detail(original_load_config(config_id))
    container = module._channel_containers[0]
    clear_button = container.findChild(QPushButton, "golden_sample_clear_button")

    clear_button.click()

    def _raise_after_commit(_config_id: int):
        raise RuntimeError("reload exploded")

    monkeypatch.setattr(config_service, "load_config", _raise_after_commit)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_save()

    authoritative = original_load_config(config_id)
    editor_data = module._collect_data()
    editor_channel = editor_data["channels"][0]

    assert any("reload exploded" in str(args[-1]) for args in warnings)
    assert old_path.exists() is False
    assert authoritative["channels"][0]["golden_sample_path"] is None
    assert editor_channel["golden_sample_path"] is None
    assert editor_channel["golden_sample_label"] is None
    assert editor_data["golden_editor_state"] == {}
