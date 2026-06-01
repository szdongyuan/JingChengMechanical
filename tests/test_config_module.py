from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QWidget,
)

from app.service.config_service import ConfigService
from app.service.config_package_service import (
    ConfigPackageExportOptions,
    ConfigPackageImportDecision,
    ConfigPackageImportResult,
    ConfigPackagePreview,
)
from app.service.golden_sample_service import GoldenSampleService
from model.channel_config_table import ChannelConfigTable
from model.plc_table import PlcTable
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


class _ConfigPackageServiceStub:
    def __init__(
        self,
        *,
        preview: ConfigPackagePreview | None = None,
        import_result: ConfigPackageImportResult | None = None,
        export_error: Exception | None = None,
        inspect_error: Exception | None = None,
        import_error: Exception | None = None,
        on_import=None,
    ):
        self.preview = preview or ConfigPackagePreview(
            config_name="ImportedCfg",
            contents={
                "config": True,
                "golden_samples": False,
                "ai_toolchain": False,
                "ai_package": False,
            },
            config_conflict=False,
            ai_toolchain_id=None,
            ai_conflict=False,
            config_action_options=("create",),
            default_config_action="create",
            ai_action_options=(),
            default_ai_action=None,
            warnings=(),
        )
        self.import_result = import_result or ConfigPackageImportResult(
            status="created",
            config_id=101,
            config_name=self.preview.config_name,
            ai_binding_status="no_ai_metadata",
        )
        self.export_error = export_error
        self.inspect_error = inspect_error
        self.import_error = import_error
        self.on_import = on_import
        self.export_calls = []
        self.inspect_calls = []
        self.import_calls = []

    def export_package(self, *, config_id: int, target_path: str, options):
        self.export_calls.append(
            {
                "config_id": config_id,
                "target_path": target_path,
                "options": options,
            }
        )
        if self.export_error is not None:
            raise self.export_error

    def inspect_package(self, path: str):
        self.inspect_calls.append(path)
        if self.inspect_error is not None:
            raise self.inspect_error
        return self.preview

    def import_package(self, path: str, decision):
        self.import_calls.append((path, decision))
        if self.import_error is not None:
            raise self.import_error
        if callable(self.on_import):
            self.on_import(path, decision)
        return self.import_result


class _HardwareServiceStub:
    def __init__(self, devices: list[dict] | None = None, hostapis: list[dict] | None = None):
        self._devices = devices if devices is not None else [
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
        self._hostapis = hostapis if hostapis is not None else [{"name": "ASIO"}, {"name": "WASAPI"}]

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
    config_package_service: _ConfigPackageServiceStub | None = None,
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
        config_package_service=config_package_service or _ConfigPackageServiceStub(),
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
    plc_id: int | None = None,
) -> dict:
    return {
        "config_id": config_id,
        "config_name": config_name,
        "record_duration": 5.0,
        "analysis_duration": 3.0,
        "analysis_window_offset_ms": 250,
        "allow_incomplete_recording_analysis": False,
        "scan_triggers_test": False,
        "plc_id": plc_id,
        "input_device_name": "Mic 1",
        "input_device_hostapi": "ASIO",
        "input_device_samplerate": 48000,
        "output_device_name": "Speaker 1",
        "output_device_hostapi": "WASAPI",
        "output_device_samplerate": 44100,
        "selected_channels": [0],
        "channels": channels,
    }


def _package_preview(
    *,
    config_name: str = "ImportedCfg",
    config_conflict: bool = False,
    ai_toolchain_id: str | None = None,
    ai_conflict: bool = False,
    config_action_options: tuple[str, ...] = ("create",),
    default_config_action: str = "create",
    ai_action_options: tuple[str, ...] = (),
    default_ai_action: str | None = None,
    golden_samples: bool = False,
    ai_toolchain: bool = False,
    ai_package: bool = False,
    warnings: tuple[str, ...] = (),
) -> ConfigPackagePreview:
    return ConfigPackagePreview(
        config_name=config_name,
        contents={
            "config": True,
            "golden_samples": golden_samples,
            "ai_toolchain": ai_toolchain,
            "ai_package": ai_package,
        },
        config_conflict=config_conflict,
        ai_toolchain_id=ai_toolchain_id,
        ai_conflict=ai_conflict,
        config_action_options=config_action_options,
        default_config_action=default_config_action,
        ai_action_options=ai_action_options,
        default_ai_action=default_ai_action,
        warnings=warnings,
    )


def _field_label_text(container: QWidget, object_name: str) -> str | None:
    field = container.findChild(QWidget, object_name)
    assert field is not None
    current = field
    parent = current.parentWidget()
    while parent is not None:
        layout = parent.layout()
        if isinstance(layout, QFormLayout):
            row, _role = layout.getWidgetPosition(current)
            if row >= 0:
                label_item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                label = label_item.widget() if label_item else None
                return label.text() if isinstance(label, QLabel) else None
        current = parent
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


def test_config_module_renders_plc_selector_from_plc_table(qapp):
    plc_alpha = PlcTable.create(plc_name="Alpha")
    plc_blank = PlcTable.create(plc_name="")
    plc_beta = PlcTable.create(plc_name="Beta")
    module = _make_module(
        binding={
            "enabled": True,
            "toolchain_id": "tc-1",
            "timeout_ms": 1000,
            "analysis_window_offset_ms": 900,
        }
    )
    module._populate_detail(_detail_payload([]))

    assert hasattr(module, "spin_analysis_window_offset")
    assert hasattr(module, "combo_plc")
    assert not hasattr(module, "ed_plc_device_key")
    assert not hasattr(module, "ed_plc_device_label")
    assert not hasattr(module, "combo_trigger")
    assert module.combo_plc.count() == 4
    assert module.combo_plc.itemData(0) is None
    assert [module.combo_plc.itemData(i) for i in range(1, 4)] == [
        plc_alpha.id,
        plc_blank.id,
        plc_beta.id,
    ]
    assert module.combo_plc.itemText(1) == f"ID {plc_alpha.id} - Alpha"
    assert module.combo_plc.itemText(2) == f"ID {plc_blank.id}"
    assert module.combo_plc.itemText(3) == f"ID {plc_beta.id} - Beta"


def test_config_module_loads_valid_saved_plc_id_into_selector(qapp):
    plc = PlcTable.create(plc_name="PLC A")
    module = _make_module()

    module._populate_detail(_detail_payload([], plc_id=plc.id))

    assert module.spin_analysis_window_offset.value() == 250
    assert module.combo_plc.currentData() == plc.id


def test_config_module_loads_stale_saved_plc_id_into_unbound_selector(qapp):
    module = _make_module()

    module._populate_detail(_detail_payload([], plc_id=9999))

    assert module.combo_plc.currentData() is None


def test_config_module_loads_explicit_unbound_config_after_bound_config_in_same_session(
    qapp,
):
    plc = PlcTable.create(plc_name="PLC A")
    module = _make_module()

    module._populate_detail(_detail_payload([], plc_id=plc.id))
    assert module.combo_plc.currentData() == plc.id

    module._populate_detail(
        _detail_payload([], config_id=8, config_name="Cfg2", plc_id=None)
    )

    assert module.combo_plc.currentData() is None


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


def test_collect_data_returns_main_config_analysis_offset_and_plc_id(qapp):
    plc = PlcTable.create(plc_name="PLC A")
    module = _make_module()
    module._populate_detail(_detail_payload([]))

    assert hasattr(module, "spin_analysis_window_offset")
    assert hasattr(module, "combo_plc")
    assert not hasattr(module, "ed_plc_device_key")
    assert not hasattr(module, "ed_plc_device_label")
    assert not hasattr(module, "combo_trigger")

    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.spin_record.setValue(5.0)
    module.spin_analysis.setValue(3.0)
    module.spin_analysis_window_offset.setValue(500)
    module.combo_plc.setCurrentIndex(module.combo_plc.findData(plc.id))

    data = module._collect_data()

    assert data["analysis_window_offset_ms"] == 500
    assert data["plc_id"] == plc.id
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
        "plc_id",
        "input_device_name",
        "input_device_hostapi",
        "input_device_samplerate",
        "output_device_name",
        "output_device_hostapi",
        "output_device_samplerate",
        "selected_channels",
        "monitor_channel_index",
        "vibration_view_channel_index",
        "monitor_gain_db",
        "channels",
        "golden_editor_state",
    }


def test_hardware_card_exposes_monitor_controls_and_channel_options(qapp):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 4",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 4,
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
    module = _make_module(hardware_service=hardware_service)
    module._populate_detail(
        {
            **_detail_payload([_channel_payload(i) for i in range(4)]),
            "input_device_name": "Mic 4",
            "input_device_hostapi": "ASIO",
        }
    )

    assert hasattr(module, "combo_monitor_channel")
    assert hasattr(module, "spin_monitor_gain_db")
    assert module.findChild(QComboBox, "combo_monitor_channel") is module.combo_monitor_channel
    assert module.findChild(QDoubleSpinBox, "spin_monitor_gain_db") is module.spin_monitor_gain_db
    assert _field_label_text(module, "combo_monitor_channel") == "监听通道"
    assert _field_label_text(module, "spin_monitor_gain_db") == "监听增益"
    assert module.combo_monitor_channel.count() == 4
    assert [module.combo_monitor_channel.itemText(i) for i in range(4)] == [
        "输入1",
        "输入2",
        "输入3",
        "输入4",
    ]
    assert [module.combo_monitor_channel.itemData(i) for i in range(4)] == [0, 1, 2, 3]
    assert module.spin_monitor_gain_db.minimum() == pytest.approx(-60.0)
    assert module.spin_monitor_gain_db.maximum() == pytest.approx(24.0)
    assert module.spin_monitor_gain_db.singleStep() == pytest.approx(1.0)
    assert module.spin_monitor_gain_db.decimals() == 1
    assert module.spin_monitor_gain_db.suffix() == " dB"


def test_hardware_card_exposes_vibration_view_channel_selector(qapp):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 4",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 4,
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
    module = _make_module(hardware_service=hardware_service)
    payload = _detail_payload([_channel_payload(i) for i in range(4)])
    payload["input_device_name"] = "Mic 4"
    payload["selected_channels"] = [0, 2]

    module._populate_detail(payload)

    assert hasattr(module, "combo_vibration_view_channel")
    assert (
        module.findChild(QComboBox, "combo_vibration_view_channel")
        is module.combo_vibration_view_channel
    )
    assert _field_label_text(module, "combo_vibration_view_channel") == "振动视图通道"
    assert [module.combo_vibration_view_channel.itemText(i) for i in range(3)] == [
        "无",
        "输入1",
        "输入3",
    ]
    assert [module.combo_vibration_view_channel.itemData(i) for i in range(3)] == [
        None,
        0,
        2,
    ]


def test_populate_detail_loads_saved_monitor_values(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0), _channel_payload(1)])
    payload["monitor_channel_index"] = 1
    payload["monitor_gain_db"] = -3.5

    module._populate_detail(payload)

    assert module.combo_monitor_channel.currentData() == 1
    assert module.spin_monitor_gain_db.value() == pytest.approx(-3.5)


def test_populate_detail_loads_saved_vibration_view_channel(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0), _channel_payload(1)])
    payload["selected_channels"] = [0, 1]
    payload["vibration_view_channel_index"] = 1

    module._populate_detail(payload)

    assert module.combo_vibration_view_channel.currentData() == 1


def test_populate_detail_falls_back_monitor_channel_from_selected_channels_then_zero(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0), _channel_payload(1)])
    payload["selected_channels"] = [1]

    module._populate_detail(payload)
    assert module.combo_monitor_channel.currentData() == 1

    payload["monitor_channel_index"] = None
    module._populate_detail(payload)
    assert module.combo_monitor_channel.currentData() == 1

    payload["monitor_channel_index"] = 99
    module._populate_detail(payload)
    assert module.combo_monitor_channel.currentData() == 1

    payload["selected_channels"] = []
    module._populate_detail(payload)
    assert module.combo_monitor_channel.currentData() == 0


def test_input_device_change_rebuilds_monitor_options_and_preserves_or_falls_back(qapp):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 4",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 4,
                "max_output_channels": 0,
            },
            {
                "name": "Mic 2",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 2,
                "max_output_channels": 0,
            },
            {
                "name": "Mic 1",
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
    module = _make_module(hardware_service=hardware_service)
    payload = {
        **_detail_payload([_channel_payload(i) for i in range(4)]),
        "input_device_name": "Mic 4",
        "input_device_hostapi": "ASIO",
        "monitor_channel_index": 1,
    }
    module._populate_detail(payload)

    mic_2_index = module.combo_input_dev.findText("Mic 2 (ASIO)")
    module.combo_input_dev.setCurrentIndex(mic_2_index)

    assert module.combo_monitor_channel.count() == 2
    assert module.combo_monitor_channel.currentData() == 1

    mic_1_index = module.combo_input_dev.findText("Mic 1 (ASIO)")
    module.combo_input_dev.setCurrentIndex(mic_1_index)

    assert module.combo_monitor_channel.count() == 1
    assert module.combo_monitor_channel.currentData() == 0


def test_analysis_channel_change_updates_default_monitor_but_not_explicit_selection(qapp):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 3",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 3,
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
    module = _make_module(hardware_service=hardware_service)
    module._populate_detail(
        {
            **_detail_payload([_channel_payload(i) for i in range(3)]),
            "input_device_name": "Mic 3",
            "input_device_hostapi": "ASIO",
            "selected_channels": [0],
        }
    )

    module._channel_checkboxes[0].setChecked(False)
    module._channel_checkboxes[1].setChecked(True)

    assert module.combo_monitor_channel.currentData() == 1

    module.combo_monitor_channel.setCurrentIndex(
        module.combo_monitor_channel.findData(2)
    )
    module._channel_checkboxes[1].setChecked(False)
    module._channel_checkboxes[0].setChecked(True)

    assert module.combo_monitor_channel.currentData() == 2


def test_collect_data_includes_monitor_channel_and_gain(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0), _channel_payload(1)])
    payload["monitor_channel_index"] = 1
    payload["monitor_gain_db"] = 6.0
    module._populate_detail(payload)

    data = module._collect_data()

    assert data["monitor_channel_index"] == 1
    assert isinstance(data["monitor_channel_index"], int)
    assert data["monitor_gain_db"] == pytest.approx(6.0)
    assert isinstance(data["monitor_gain_db"], float)


def test_collect_data_includes_vibration_view_channel(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0), _channel_payload(1)])
    payload["selected_channels"] = [0, 1]
    payload["vibration_view_channel_index"] = 1
    module._populate_detail(payload)

    data = module._collect_data()

    assert data["vibration_view_channel_index"] == 1
    assert isinstance(data["vibration_view_channel_index"], int)


def test_collect_data_uses_null_monitor_channel_when_no_input_channels(qapp):
    hardware_service = _HardwareServiceStub(devices=[], hostapis=[])
    module = _make_module(hardware_service=hardware_service)
    module._populate_detail(
        {
            **_detail_payload([]),
            "input_device_name": None,
            "input_device_hostapi": None,
            "selected_channels": [],
        }
    )

    data = module._collect_data()

    assert data["monitor_channel_index"] is None
    assert data["monitor_gain_db"] == pytest.approx(0.0)


def test_vibration_view_channel_options_follow_selected_channels(qapp):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic 3",
                "hostapi": 0,
                "default_samplerate": 48000,
                "max_input_channels": 3,
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
    module = _make_module(hardware_service=hardware_service)
    payload = _detail_payload([_channel_payload(i) for i in range(3)])
    payload["input_device_name"] = "Mic 3"
    payload["selected_channels"] = [0, 1]
    payload["vibration_view_channel_index"] = 1
    module._populate_detail(payload)

    module._channel_checkboxes[1].setChecked(False)
    module._channel_checkboxes[2].setChecked(True)

    assert [module.combo_vibration_view_channel.itemData(i) for i in range(3)] == [
        None,
        0,
        2,
    ]
    assert module.combo_vibration_view_channel.currentData() is None


def test_config_module_refreshes_open_selector_after_plc_changed_signal(qapp):
    plc_alpha = PlcTable.create(plc_name="Alpha")
    plc_beta = PlcTable.create(plc_name="Beta")
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        ai_toolchains_changed=_SignalStub(),
        plc_changed=_SignalStub(),
    )
    module = _make_module(bus=bus)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([], plc_id=plc_beta.id))

    plc_gamma = PlcTable.create(plc_name="Gamma")
    bus.plc_changed.emit()

    assert [module.combo_plc.itemData(i) for i in range(module.combo_plc.count())] == [
        None,
        plc_alpha.id,
        plc_beta.id,
        plc_gamma.id,
    ]
    assert module.combo_plc.currentData() == plc_beta.id

    plc_beta.delete_instance()
    bus.plc_changed.emit()

    assert [module.combo_plc.itemData(i) for i in range(module.combo_plc.count())] == [
        None,
        plc_alpha.id,
        plc_gamma.id,
    ]
    assert module.combo_plc.currentData() is None


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
    assert "params_json" not in ai_data


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
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-2", "display_name": "Toolchain 2", "version": "2"},
    ]

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-2"
    assert module.spin_ai_timeout.value() == 2000


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
    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-2",
        "timeout_ms": 2000,
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

    module.combo_ai_toolchain.setCurrentIndex(module.combo_ai_toolchain.findData("tc-1"))
    module.spin_ai_timeout.setValue(1000)

    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-2",
        "timeout_ms": 2000,
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-2", "display_name": "Toolchain 2", "version": "2"},
    ]

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-2"
    assert module.spin_ai_timeout.value() == 2000


def test_unavailable_toolchain_loaded_binding_injects_unavailable_toolchain_option(qapp):
    ai_service = _AiServiceStub(
        binding={
            "enabled": True,
            "toolchain_id": "disabled-tc",
            "timeout_ms": 1000,
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
    module.spin_ai_timeout.setValue(1500)
    ai_service._binding = {
        "enabled": True,
        "toolchain_id": "tc-new",
        "timeout_ms": 2000,
    }
    ai_service.enabled_toolchains = [
        {"toolchain_id": "tc-new", "display_name": "Toolchain New", "version": "2"},
    ]
    ai_service.toolchains["tc-old"] = None

    bus.ai_toolchains_changed.emit()

    assert module.combo_ai_toolchain.currentData() == "tc-old"

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
        "stft_score",
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


def test_channel_section_layout_does_not_stretch_after_repeated_collapses(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    baseline = {
        key: boxes[key].sizeHint().height()
        for key in ("loudness", "golden_sample", "fft", "octave")
    }

    for _ in range(5):
        boxes["stft"].set_expanded(False)
        qapp.processEvents()
        boxes["spl"].set_expanded(False)
        qapp.processEvents()
        boxes["stft"].set_expanded(True)
        qapp.processEvents()
        boxes["spl"].set_expanded(True)
        qapp.processEvents()

    boxes["stft"].set_expanded(False)
    qapp.processEvents()
    boxes["spl"].set_expanded(False)
    qapp.processEvents()

    assert boxes["stft"].height() == boxes["stft"].sizeHint().height()
    assert boxes["spl"].height() == boxes["spl"].sizeHint().height()
    for key, expected_height in baseline.items():
        assert boxes[key].height() == expected_height
        assert boxes[key].height() == boxes[key].sizeHint().height()


def test_channel_section_layout_preserves_scroll_reachability_in_short_viewport(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(900, 700)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    scroll_bar = module.detail_scroll.verticalScrollBar()

    assert scroll_bar.maximum() > 0

    module.detail_scroll.ensureWidgetVisible(boxes["octave"], 0, 0)
    qapp.processEvents()

    top_in_viewport = boxes["octave"].mapTo(
        module.detail_scroll.viewport(),
        QPoint(0, 0),
    ).y()
    bottom_in_viewport = boxes["octave"].mapTo(
        module.detail_scroll.viewport(),
        QPoint(0, boxes["octave"].height()),
    ).y()

    assert top_in_viewport >= 0
    assert bottom_in_viewport <= module.detail_scroll.viewport().height()


def test_on_save_preserves_collapsed_section_geometry_in_tall_viewport(
    qapp, monkeypatch
):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service, bus=bus)
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    boxes = {
        box.section_id(): box
        for box in container.findChildren(CollapsibleGroupBox)
    }
    boxes["stft"].set_expanded(False)
    qapp.processEvents()
    boxes["spl"].set_expanded(False)
    qapp.processEvents()

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr("ui.modules.config.QMessageBox.information", lambda *args: None)

    module._on_save()
    qapp.processEvents()

    restored = module._channel_containers[0]
    restored_boxes = {
        box.section_id(): box
        for box in restored.findChildren(CollapsibleGroupBox)
    }

    assert restored_boxes["stft"].is_expanded() is False
    assert restored_boxes["spl"].is_expanded() is False
    assert restored_boxes["stft"].height() == restored_boxes["stft"].sizeHint().height()
    assert restored_boxes["spl"].height() == restored_boxes["spl"].sizeHint().height()
    for key in ("loudness", "golden_sample", "fft", "octave"):
        assert restored_boxes[key].height() == restored_boxes[key].sizeHint().height()


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


def test_unknown_host_api_device_displays_chinese_fallback_but_collects_na(qapp):
    hardware_service = _HardwareServiceStub(
        devices=[
            {
                "name": "Mic Unknown",
                "hostapi": 99,
                "default_samplerate": 48000,
                "max_input_channels": 1,
                "max_output_channels": 0,
            }
        ],
        hostapis=[],
    )
    module = _make_module(hardware_service=hardware_service)
    module._populate_detail(
        {
            **_detail_payload([_channel_payload(0)]),
            "input_device_name": "Mic Unknown",
            "input_device_hostapi": "N/A",
            "output_device_name": None,
            "output_device_hostapi": None,
            "selected_channels": [0],
        }
    )

    assert "未知" in module.combo_input_dev.currentText()
    assert module._collect_data()["input_device_hostapi"] == "N/A"


def test_unavailable_saved_host_api_displays_chinese_fallback_but_collects_na(qapp):
    hardware_service = _HardwareServiceStub(devices=[], hostapis=[])
    module = _make_module(hardware_service=hardware_service)
    module._populate_detail(
        {
            **_detail_payload([_channel_payload(0)]),
            "input_device_name": "Missing Mic",
            "input_device_hostapi": "N/A",
            "input_device_samplerate": 48000,
            "output_device_name": None,
            "output_device_hostapi": None,
            "selected_channels": [0],
        }
    )

    assert "未知" in module.combo_input_dev.currentText()
    assert "N/A" not in module.combo_input_dev.currentText()
    assert module._collect_data()["input_device_hostapi"] == "N/A"


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


def test_populate_detail_restores_stft_color_scale_controls(qapp):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    stft_color_scale_enabled=True,
                    stft_color_scale_min_db=-95.5,
                    stft_color_scale_max_db=-12.2,
                )
            ]
        )
    )

    container = module._channel_containers[0]
    enabled = container.findChild(QCheckBox, "stft_color_scale_enabled")
    min_spin = container.findChild(QDoubleSpinBox, "stft_color_scale_min_db")
    max_spin = container.findChild(QDoubleSpinBox, "stft_color_scale_max_db")

    assert enabled.isChecked() is True
    assert min_spin.value() == pytest.approx(-95.5)
    assert max_spin.value() == pytest.approx(-12.2)
    assert min_spin.isEnabled() is True
    assert max_spin.isEnabled() is True


def test_stft_color_scale_inputs_disable_when_unchecked(qapp):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    container = module._channel_containers[0]
    enabled = container.findChild(QCheckBox, "stft_color_scale_enabled")
    min_spin = container.findChild(QDoubleSpinBox, "stft_color_scale_min_db")
    max_spin = container.findChild(QDoubleSpinBox, "stft_color_scale_max_db")

    assert enabled.isChecked() is False
    assert min_spin.isEnabled() is False
    assert max_spin.isEnabled() is False

    enabled.setChecked(True)

    assert min_spin.isEnabled() is True
    assert max_spin.isEnabled() is True


def test_collect_data_includes_stft_color_scale_fields(qapp):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]

    container.findChild(QCheckBox, "stft_color_scale_enabled").setChecked(True)
    container.findChild(QDoubleSpinBox, "stft_color_scale_min_db").setValue(-88.0)
    container.findChild(QDoubleSpinBox, "stft_color_scale_max_db").setValue(-8.0)

    data = module._collect_data()

    channel = data["channels"][0]
    assert channel["stft_color_scale_enabled"] is True
    assert channel["stft_color_scale_min_db"] == pytest.approx(-88.0)
    assert channel["stft_color_scale_max_db"] == pytest.approx(-8.0)


def test_populate_detail_restores_stft_score_controls(qapp):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    stft_score_enabled=False,
                    stft_score_summary_mode="mean",
                    stft_score_percentile=20.0,
                    stft_score_loudness_reference_sone=12.5,
                    stft_score_modulation_reference=0.3,
                    stft_score_harmonic_ratio_reference=0.4,
                    stft_score_frequency_instability_reference=0.08,
                    stft_score_harmonic_start_order=3,
                    stft_score_harmonic_end_order=80,
                )
            ]
        )
    )

    container = module._channel_containers[0]

    assert container.findChild(QCheckBox, "stft_score_enabled").isChecked() is False
    assert (
        container.findChild(QComboBox, "stft_score_summary_mode").currentData()
        == "mean"
    )
    assert container.findChild(
        QDoubleSpinBox, "stft_score_percentile"
    ).value() == pytest.approx(20.0)
    assert container.findChild(
        QDoubleSpinBox, "stft_score_loudness_reference_sone"
    ).value() == pytest.approx(12.5)
    assert container.findChild(
        QDoubleSpinBox, "stft_score_modulation_reference"
    ).value() == pytest.approx(0.3)
    assert container.findChild(
        QDoubleSpinBox, "stft_score_harmonic_ratio_reference"
    ).value() == pytest.approx(0.4)
    assert container.findChild(
        QDoubleSpinBox, "stft_score_frequency_instability_reference"
    ).value() == pytest.approx(0.08)
    assert (
        container.findChild(QSpinBox, "stft_score_harmonic_start_order").value()
        == 3
    )
    assert (
        container.findChild(QSpinBox, "stft_score_harmonic_end_order").value()
        == 80
    )
    assert (
        _ancestor_group_title(
            container.findChild(QComboBox, "stft_score_summary_mode")
        )
        == "STFT 评分"
    )


def test_stft_score_inputs_disable_when_unchecked(qapp):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))

    container = module._channel_containers[0]
    enabled = container.findChild(QCheckBox, "stft_score_enabled")
    score_controls = [
        container.findChild(QComboBox, "stft_score_summary_mode"),
        container.findChild(QDoubleSpinBox, "stft_score_percentile"),
        container.findChild(QDoubleSpinBox, "stft_score_loudness_reference_sone"),
        container.findChild(QDoubleSpinBox, "stft_score_modulation_reference"),
        container.findChild(QDoubleSpinBox, "stft_score_harmonic_ratio_reference"),
        container.findChild(
            QDoubleSpinBox, "stft_score_frequency_instability_reference"
        ),
        container.findChild(QSpinBox, "stft_score_harmonic_start_order"),
        container.findChild(QSpinBox, "stft_score_harmonic_end_order"),
    ]

    assert enabled.isChecked() is True
    assert all(control.isEnabled() for control in score_controls)

    enabled.setChecked(False)

    assert all(not control.isEnabled() for control in score_controls)

    enabled.setChecked(True)

    assert all(control.isEnabled() for control in score_controls)


def test_collect_data_includes_stft_score_fields(qapp):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]

    container.findChild(QCheckBox, "stft_score_enabled").setChecked(False)
    combo = container.findChild(QComboBox, "stft_score_summary_mode")
    combo.setCurrentIndex(combo.findData("min"))
    container.findChild(QDoubleSpinBox, "stft_score_percentile").setValue(25.0)
    container.findChild(
        QDoubleSpinBox, "stft_score_loudness_reference_sone"
    ).setValue(11.5)
    container.findChild(
        QDoubleSpinBox, "stft_score_modulation_reference"
    ).setValue(0.35)
    container.findChild(
        QDoubleSpinBox, "stft_score_harmonic_ratio_reference"
    ).setValue(0.45)
    container.findChild(
        QDoubleSpinBox, "stft_score_frequency_instability_reference"
    ).setValue(0.09)
    container.findChild(QSpinBox, "stft_score_harmonic_start_order").setValue(4)
    container.findChild(QSpinBox, "stft_score_harmonic_end_order").setValue(90)

    data = module._collect_data()

    channel = data["channels"][0]
    assert channel["stft_score_enabled"] is False
    assert channel["stft_score_summary_mode"] == "min"
    assert channel["stft_score_percentile"] == pytest.approx(25.0)
    assert channel["stft_score_loudness_reference_sone"] == pytest.approx(11.5)
    assert channel["stft_score_modulation_reference"] == pytest.approx(0.35)
    assert channel["stft_score_harmonic_ratio_reference"] == pytest.approx(0.45)
    assert channel["stft_score_frequency_instability_reference"] == pytest.approx(
        0.09
    )
    assert channel["stft_score_harmonic_start_order"] == 4
    assert channel["stft_score_harmonic_end_order"] == 90


def test_populate_detail_restores_fft_x_axis_scale(qapp):
    module = _make_module()
    payload = _detail_payload(
        [_channel_payload(0, fft_x_axis_scale="log")]
    )

    module._populate_detail(payload)
    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "fft_x_axis_scale")

    assert combo is not None
    assert combo.currentData() == "log"


def test_collect_data_includes_fft_x_axis_scale(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "fft_x_axis_scale")
    assert combo is not None
    combo.setCurrentIndex(combo.findData("log"))

    data = module._collect_data()

    assert data["channels"][0]["fft_x_axis_scale"] == "log"


def test_fft_band_table_insert_adds_column_after_selected(qapp):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {"freq": 100.0, "lower": 30.0, "upper": 70.0},
                        {"freq": 300.0, "lower": 50.0, "upper": 80.0},
                    ],
                )
            ]
        )
    )
    table = module._channel_containers[0].findChild(QTableWidget, "fft_band_table")
    assert table is not None
    table.selectColumn(0)

    module._insert_band_after_selection(table)

    assert table.columnCount() == 3
    assert table.item(0, 1).text() == "200.0"
    assert table.item(1, 1).text() == "30.0"
    assert table.item(2, 1).text() == "70.0"
    assert {index.column() for index in table.selectedIndexes()} == {1}


def test_fft_band_table_insert_after_invalid_selected_frequency_uses_append_defaults(
    qapp,
):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {"freq": -100.0, "lower": 30.0, "upper": 70.0},
                        {"freq": 300.0, "lower": 50.0, "upper": 80.0},
                    ],
                )
            ]
        )
    )
    table = module._channel_containers[0].findChild(QTableWidget, "fft_band_table")
    assert table is not None
    table.selectColumn(0)

    module._insert_band_after_selection(table)

    assert table.columnCount() == 3
    assert table.item(0, 1).text() == "1000"
    assert table.item(1, 1).text() == "0"
    assert table.item(2, 1).text() == "100"
    assert {index.column() for index in table.selectedIndexes()} == {1}


def test_octave_band_table_does_not_get_fft_insert_controls(qapp):
    module = _make_module()
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]
    oct_table = container.findChild(QTableWidget, "oct_band_table")
    assert oct_table is not None

    buttons = oct_table.parent().findChildren(QPushButton)
    assert [button.text() for button in buttons] == ["添加频带", "删除选中"]


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
    assert module._channel_tab_buttons[0].text() == "输入1（通道 1）"
    assert module._channel_checkboxes[0].text() == "输入1"
    assert _ancestor_group_title(import_button) == "黄金样本"
    assert _ancestor_group_title(container.findChild(QDoubleSpinBox, "spl_time_constant")) == "声压级参数"
    assert _field_label_text(container, "spl_lower_limit") == "声压级"
    assert _field_label_text(container, "spl_average_lower_limit") == "平均声压级"
    assert _ancestor_group_title(container.findChild(QComboBox, "loudness_unit")) == "响度参数"
    assert import_button.text() == "导入 WAV..."
    assert replace_button.text() == "替换 WAV..."
    assert clear_button.text() == "清除"
    assert fft_mode.currentText() == "绝对值"
    assert fft_mode.currentData() == "absolute"
    assert _field_label_text(container, "fft_golden_mode") == "黄金样本模式"
    assert _field_label_text(container, "fft_show_golden_overlay") == "黄金曲线"
    assert fft_overlay.text() == "显示黄金曲线"
    assert fft_overlay.isChecked() is True
    assert oct_mode.currentText() == "差值"
    assert oct_mode.currentData() == "delta"
    assert _field_label_text(container, "oct_golden_mode") == "黄金样本模式"
    assert _field_label_text(container, "oct_show_golden_overlay") == "黄金曲线"
    assert oct_overlay.text() == "显示黄金曲线"
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
    combo.setCurrentText("差值")

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

    assert fft_mode.currentText() == "绝对值"
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


def test_invalid_ai_binding_selection_discards_staged_file_and_keeps_committed_binding_authoritative(
    qapp,
    tmp_path,
    monkeypatch,
):
    committed = _channel_payload(0, **_golden_binding())
    config_service = _ConfigServiceStub()
    module = _make_module(config_service=config_service)
    module._current_config_id = 7
    module._populate_detail(_detail_payload([committed]))
    module.chk_ai_enabled.setChecked(True)
    module.combo_ai_toolchain.setCurrentIndex(0)
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

    assert any("AI" in str(args[-1]) for args in warnings)
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


def test_import_export_buttons_render_on_config_list_actions(qapp):
    module = _make_module()

    assert module.btn_import.text() == "导入"
    assert module.btn_export.text() == "导出"


def test_import_export_warns_when_export_without_selected_config(qapp, monkeypatch):
    module = _make_module()
    module._current_config_id = None
    module.config_list.clear()

    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_export_package()

    assert any("请选择" in str(args[-1]) for args in warnings)


def test_dy_package_export_dialog_disables_ai_package_without_toolchain(qapp):
    dialogs = importlib.import_module("ui.popups.config_package_dialogs")
    dialog = dialogs.ConfigPackageExportDialog()
    buttons = dialog.findChild(QDialogButtonBox)

    assert dialog.chk_include_ai_package.isEnabled() is False
    assert dialog.chk_include_ai_package.isChecked() is False
    assert buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "确定"
    assert buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == "取消"

    dialog.chk_include_ai_toolchain.setChecked(True)
    dialog.chk_include_ai_package.setChecked(True)

    assert dialog.chk_include_ai_package.isEnabled() is True
    assert dialog.chk_include_ai_package.isChecked() is True

    dialog.chk_include_ai_toolchain.setChecked(False)

    assert dialog.chk_include_ai_package.isEnabled() is False
    assert dialog.chk_include_ai_package.isChecked() is False


def test_dy_package_export_flow_uses_dy_filter_and_suffix(
    qapp,
    tmp_path,
    monkeypatch,
):
    dialogs = importlib.import_module("ui.popups.config_package_dialogs")
    captured = {}

    def _fake_get_save_file_name(parent, title, default_name, selected_filter):
        captured["title"] = title
        captured["default_name"] = default_name
        captured["filter"] = selected_filter
        return str(tmp_path / "exported_config"), selected_filter

    monkeypatch.setattr(
        dialogs.QFileDialog,
        "getSaveFileName",
        staticmethod(_fake_get_save_file_name),
    )

    chosen = dialogs.choose_dy_export_path(None, default_name="Cfg1")

    assert captured["filter"] == "DY 配置包 (*.dy)"
    assert chosen == str(tmp_path / "exported_config.dy")


def test_import_export_export_flow_collects_options_and_calls_package_service(
    qapp,
    tmp_path,
    monkeypatch,
):
    config_service = _ConfigServiceStub(
        loaded_configs={
            7: _detail_payload([], config_id=7, config_name="Cfg1"),
        }
    )
    package_service = _ConfigPackageServiceStub()
    module = _make_module(
        config_service=config_service,
        config_package_service=package_service,
    )
    options = ConfigPackageExportOptions(
        include_golden_samples=True,
        include_ai_toolchain=True,
        include_ai_package=False,
    )
    option_prompts = []
    path_prompts = []
    infos = []

    monkeypatch.setattr(
        "ui.modules.config.config_package_dialogs.choose_export_options",
        lambda parent: option_prompts.append(parent) or options,
    )
    monkeypatch.setattr(
        "ui.modules.config.config_package_dialogs.choose_dy_export_path",
        lambda parent, default_name: (
            path_prompts.append((parent, default_name))
            or str(tmp_path / "Cfg1.dy")
        ),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )

    module._on_export_package()

    assert option_prompts == [module]
    assert path_prompts == [(module, "Cfg1")]
    assert package_service.export_calls == [
        {
            "config_id": 7,
            "target_path": str(tmp_path / "Cfg1.dy"),
            "options": options,
        }
    ]
    assert any("Cfg1.dy" in str(args[-1]) for args in infos)


def test_dy_package_metadata_only_ai_conflict_uses_required_actions_and_default(
    qapp,
    monkeypatch,
):
    dialogs = importlib.import_module("ui.popups.config_package_dialogs")
    preview = _package_preview(
        config_name="ImportedCfg",
        ai_toolchain_id="tc-1",
        ai_conflict=True,
        ai_action_options=("use_existing_toolchain", "clear_binding", "cancel"),
        default_ai_action="use_existing_toolchain",
        golden_samples=True,
        ai_toolchain=True,
        ai_package=False,
    )
    captured = {}

    def _fake_exec(self):
        captured["buttons"] = [button.text() for button in self.buttons()]
        captured["default"] = self.defaultButton().text()
        captured["text"] = self.text()
        self._clicked_button = next(
            button for button in self.buttons() if button.text() == "使用旧工具链"
        )
        return 0

    monkeypatch.setattr(dialogs.QMessageBox, "exec", _fake_exec)
    monkeypatch.setattr(
        dialogs.QMessageBox,
        "clickedButton",
        lambda self: getattr(self, "_clicked_button", None),
    )

    decision = dialogs.choose_import_decision(None, preview)

    assert captured["buttons"] == ["使用旧工具链", "清空 AI 绑定", "取消"]
    assert captured["default"] == "使用旧工具链"
    assert "源配置名称：ImportedCfg" in captured["text"]
    assert "包含黄金样本：是" in captured["text"]
    assert "包含 AI 工具链：是" in captured["text"]
    assert "包含 AI 包：否" in captured["text"]
    assert decision == ConfigPackageImportDecision(
        config_action="create",
        ai_action="use_existing_toolchain",
    )


def test_import_export_import_flow_inspects_imports_refreshes_and_selects_config(
    qapp,
    monkeypatch,
):
    config_service = _ConfigServiceStub(
        loaded_configs={
            7: _detail_payload([], config_id=7, config_name="ExistingCfg"),
        }
    )
    preview = _package_preview(
        config_name="ImportedCfg",
        ai_toolchain_id="tc-1",
        ai_conflict=True,
        ai_action_options=("use_existing_toolchain", "clear_binding", "cancel"),
        default_ai_action="use_existing_toolchain",
        golden_samples=True,
        ai_toolchain=True,
        ai_package=False,
    )
    import_result = ConfigPackageImportResult(
        status="created",
        config_id=8,
        config_name="ImportedCfg",
        ai_binding_status="used_existing_toolchain",
        warnings=("AI 绑定已沿用旧工具链。",),
        info_message="导入完成。",
    )
    package_service = _ConfigPackageServiceStub(
        preview=preview,
        import_result=import_result,
        on_import=lambda _path, _decision: config_service._loaded_configs.__setitem__(
            8,
            _detail_payload([], config_id=8, config_name="ImportedCfg"),
        ),
    )
    module = _make_module(
        config_service=config_service,
        config_package_service=package_service,
    )
    chosen_decisions = []
    infos = []
    warnings = []

    monkeypatch.setattr(
        "ui.modules.config.config_package_dialogs.choose_dy_import_path",
        lambda parent: "D:/packages/imported.dy",
    )
    monkeypatch.setattr(
        "ui.modules.config.config_package_dialogs.choose_import_decision",
        lambda parent, incoming_preview: (
            chosen_decisions.append(incoming_preview)
            or ConfigPackageImportDecision(
                config_action="create",
                ai_action="use_existing_toolchain",
            )
        ),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    module._on_import_package()

    assert package_service.inspect_calls == ["D:/packages/imported.dy"]
    assert package_service.import_calls == [
        (
            "D:/packages/imported.dy",
            ConfigPackageImportDecision(
                config_action="create",
                ai_action="use_existing_toolchain",
            ),
        )
    ]
    assert chosen_decisions == [preview]
    assert module.config_list.currentItem().data(Qt.ItemDataRole.UserRole) == 8
    assert any("AI 绑定已沿用旧工具链。" in str(args[-1]) for args in warnings)
    assert any("导入完成。" in str(args[-1]) for args in infos)


def test_import_export_kept_existing_config_shows_info_message(qapp, monkeypatch):
    preview = _package_preview(
        config_name="ExistingCfg",
        config_conflict=True,
        config_action_options=("overwrite", "use_existing_config", "cancel"),
        default_config_action="use_existing_config",
    )
    package_service = _ConfigPackageServiceStub(
        preview=preview,
        import_result=ConfigPackageImportResult(
            status="kept_existing",
            config_id=7,
            config_name="ExistingCfg",
            ai_binding_status="unchanged",
            info_message="保留现有配置。",
        ),
    )
    module = _make_module(config_package_service=package_service)
    infos = []

    monkeypatch.setattr(
        "ui.modules.config.config_package_dialogs.choose_dy_import_path",
        lambda parent: "D:/packages/existing.dy",
    )
    monkeypatch.setattr(
        "ui.modules.config.config_package_dialogs.choose_import_decision",
        lambda parent, incoming_preview: ConfigPackageImportDecision(
            config_action="use_existing_config",
            ai_action="create",
        ),
    )
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )

    module._on_import_package()

    assert package_service.import_calls == [
        (
            "D:/packages/existing.dy",
            ConfigPackageImportDecision(
                config_action="use_existing_config",
                ai_action="create",
            ),
        )
    ]
    assert any("保留现有配置。" in str(args[-1]) for args in infos)
