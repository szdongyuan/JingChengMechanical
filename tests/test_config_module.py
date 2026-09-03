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
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QScrollArea,
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
from model.input_hardware_table import InputHardwareTable
from model.plc_table import PlcTable
from model.test_config_table import TestConfigTable as ConfigTable
from ui.modules.config import ConfigModule
from ui.widgets.collapsible_group_box import CollapsibleGroupBox
from ui.widgets.limit_table_editor import LimitTableEditor
from ui.widgets.result_dashboard_layout_editor import ResultDashboardLayoutEditor


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


class _ManagedHardwareServiceStub:
    def list_input_hardware(self):
        return [
            {
                "hardware_id": 11,
                "alias": "产线 A 声卡",
                "name": "Interface A",
                "hostapi": "ASIO",
                "default_samplerate": 48000.0,
                "samplerate": 48000.0,
                "max_input_channels": 2,
                "channel_count": 2,
                "mic_cal": {},
            },
            {
                "hardware_id": 22,
                "alias": "产线 B 声卡",
                "name": "Interface B",
                "hostapi": "ASIO",
                "default_samplerate": 96000.0,
                "samplerate": 96000.0,
                "max_input_channels": 4,
                "channel_count": 4,
                "mic_cal": {},
            },
        ]

    def query_devices_and_hostapis(self, refresh: bool = False):
        return (
            [
                {
                    "name": "Line Monitor",
                    "hostapi": 0,
                    "default_samplerate": 48000.0,
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                }
            ],
            [{"name": "WASAPI"}],
        )


def _create_valid_managed_input() -> InputHardwareTable:
    return InputHardwareTable.create(
        hardware_id=11,
        alias="Managed input",
        device_name="Interface A",
        hostapi="ASIO",
        samplerate=48_000,
        channel_count=2,
    )


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
        factory_mode=SimpleNamespace(
            state=SimpleNamespace(
                selected_config_ids=(),
                run_phase="idle",
                start_ready_reason="",
            ),
        ),
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
    fault_types: list[str] | None = None,
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
        "fault_types": list(fault_types or []),
        "input_device_name": "Mic 1",
        "input_device_hostapi": "ASIO",
        "input_device_samplerate": 48000,
        "output_device_name": "Speaker 1",
        "output_device_hostapi": "WASAPI",
        "output_device_samplerate": 44100,
        "selected_channels": [0],
        "channels": channels,
    }


def _fault_editor_text_with_length(length: int) -> str:
    names = [f"n{index}" for index in range(32)]
    base = ",".join(names)
    return names[0] + (" " * (length - len(base))) + "," + ",".join(names[1:])


def test_factory_selection_launcher_is_always_available(qapp):
    module = _make_module()

    assert module.btn_select_active_configs.text() == "选择使用配置"
    assert module.btn_select_active_configs.isEnabled() is True


def test_factory_selection_launcher_opens_modal_without_resolving_dirty_editor(
    qapp,
    monkeypatch,
):
    module = _make_module(
        config_service=_ConfigServiceStub(
            loaded_configs={7: _detail_payload([], config_id=7)}
        )
    )
    module.spin_record.setValue(module.spin_record.value() + 1.0)
    opened = []
    monkeypatch.setattr(
        "ui.modules.config.FactoryConfigSelectionDialog.exec",
        lambda dialog: opened.append(dialog),
    )

    module.btn_select_active_configs.click()

    assert opened
    assert module._has_unsaved_changes() is True


def test_factory_selection_launcher_reuses_one_modal_instance(qapp, monkeypatch):
    module = _make_module()
    events = []
    monkeypatch.setattr(
        "ui.modules.config.FactoryConfigSelectionDialog.prepare_for_open",
        lambda dialog: events.append(("prepare", id(dialog))),
    )
    monkeypatch.setattr(
        "ui.modules.config.FactoryConfigSelectionDialog.exec",
        lambda dialog: events.append(("exec", id(dialog))),
    )

    module.btn_select_active_configs.click()
    module.btn_select_active_configs.click()

    assert [event for event, _identity in events] == [
        "prepare",
        "prepare",
        "exec",
        "prepare",
        "exec",
    ]
    assert len({identity for _event, identity in events}) == 1


def test_fault_types_editor_is_single_global_comma_editor(qapp) -> None:
    module = _make_module()

    editor = module.findChild(QLineEdit, "fault_types_editor")

    assert editor is module.fault_types_editor
    assert _field_label_text(module, "fault_types_editor") == "故障类型"
    assert editor.maxLength() > 4096


def test_fault_types_editor_populates_collects_and_marks_dirty(qapp) -> None:
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload([], fault_types=["异响", "漏装"])
    )

    assert module.fault_types_editor.text() == "异响, 漏装"
    assert module._collect_data()["fault_types"] == ["异响", "漏装"]
    assert module._has_unsaved_changes() is False

    module.fault_types_editor.setText(" 异响 , 划伤 ")

    assert module._collect_data()["fault_types"] == ["异响", "划伤"]
    assert module._has_unsaved_changes() is True


def test_fault_types_editor_uses_python_code_point_bound_without_qt_truncation(
    qapp,
) -> None:
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([]))
    valid = _fault_editor_text_with_length(4096)
    module.fault_types_editor.setText(valid)

    assert module.fault_types_editor.text() == valid
    assert len(module._collect_data()["fault_types"]) == 32

    invalid = _fault_editor_text_with_length(4097)
    module.fault_types_editor.setText(invalid)
    assert module.fault_types_editor.text() == invalid
    with pytest.raises(ValueError):
        module._collect_data()


def test_fault_types_editor_preserves_text_beyond_qt_default_limit_for_python_validation(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ui.modules.config as config_module

    module = _make_module()
    module._current_config_id = 7
    original = (" " * 40_000) + "X"
    parser_inputs: list[str] = []
    real_parser = config_module.parse_fault_types_editor

    def _recording_parser(value: str) -> list[str]:
        parser_inputs.append(value)
        return real_parser(value)

    monkeypatch.setattr(
        config_module,
        "parse_fault_types_editor",
        _recording_parser,
    )

    module.fault_types_editor.setText(original)
    collected = module._collect_data()

    assert module.fault_types_editor.maxLength() > len(original)
    assert module.fault_types_editor.text() == original
    assert parser_inputs == [original]
    assert collected["fault_types"] == ["X"]


def test_fault_types_save_uses_authoritative_normalized_reload(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import model.base_mode as base_mod

    monkeypatch.setattr(
        base_mod,
        "db",
        SimpleNamespace(atomic=lambda: nullcontext()),
    )
    config_service = _ConfigServiceStub(
        loaded_configs={
            7: _detail_payload([], fault_types=["旧类型"]),
        }
    )
    module = _make_module(config_service=config_service)
    module._current_config_id = 7
    module._populate_detail(config_service.load_config(7))
    module.fault_types_editor.setText(" 异响 , 漏装 ")
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: None,
    )
    warnings = []
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.warning",
        lambda *args: warnings.append(args),
    )

    saved = module._save_current_config()

    assert saved is True, warnings
    assert warnings == []
    assert config_service.saved_configs[0]["fault_types"] == ["异响", "漏装"]
    assert module.fault_types_editor.text() == "异响, 漏装"
    assert module._has_unsaved_changes() is False


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


def _is_descendant_of(widget: QWidget, ancestor: QWidget) -> bool:
    current = widget
    while current is not None:
        if current is ancestor:
            return True
        current = current.parentWidget()
    return False


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
    box = CollapsibleGroupBox("FFT 参数", section_id="fft")

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


def test_scan_trigger_checkbox_is_removed_from_config_page(qapp):
    module = _make_module()
    payload = _detail_payload([])
    payload["scan_triggers_test"] = True

    module._populate_detail(payload)
    data = module._collect_data()

    assert not hasattr(module, "chk_scan")
    assert module.findChild(QWidget, "chk_scan") is None
    assert data["scan_triggers_test"] is False


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
            "fault_types",
            "monitor_channel_index",
        "vibration_view_channel_index",
        "monitor_gain_db",
        "current_meter",
        "result_dashboard_layout",
        "channels",
        "golden_editor_state",
    }


def test_top_level_config_launchers_open_cards_and_keep_plc_visible(qapp):
    plc = PlcTable.create(plc_name="PLC A")
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1", plc_id=plc.id),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    assert module.combo_plc.currentData() == plc.id
    assert _field_label_text(module, "combo_plc") == "PLC"
    assert module.detail.isVisible() is True
    assert module.combo_plc.isVisible() is True
    current = module.combo_plc.parentWidget()
    while current is not None:
        assert not isinstance(current, QDialog)
        current = current.parentWidget()

    launcher_panel = module.findChild(QWidget, "top_level_config_launcher_panel")
    assert launcher_panel is not None

    expected = {
            "open_audio_vibration_hardware_card": (
                "输入硬件与监听设置",
            "top_level_audio_vibration_hardware_card_dialog",
        ),
        "open_current_meter_card": (
            "电流表设置",
            "top_level_current_meter_card_dialog",
        ),
        "open_ai_binding_card": (
            "AI设置",
            "top_level_ai_binding_card_dialog",
        ),
    }
    for button_name, (title, dialog_name) in expected.items():
        button = module.findChild(QPushButton, button_name)
        dialog = module.findChild(QDialog, dialog_name)
        assert button is not None
        assert button.text() == title
        assert dialog is not None
        assert dialog.isVisible() is False

        button.click()
        qapp.processEvents()

        assert dialog.isVisible() is True
        dialog.hide()

    audio_dialog = module.findChild(
        QDialog, "top_level_audio_vibration_hardware_card_dialog"
    )
    current_meter_dialog = module.findChild(
        QDialog, "top_level_current_meter_card_dialog"
    )
    ai_dialog = module.findChild(QDialog, "top_level_ai_binding_card_dialog")

    assert _is_descendant_of(module.combo_monitor_channel, audio_dialog)
    assert _is_descendant_of(module.combo_vibration_view_channel, audio_dialog)
    assert _is_descendant_of(module.spin_current_meter_offset, current_meter_dialog)
    assert _is_descendant_of(module.ed_current_meter_host, current_meter_dialog)
    assert _is_descendant_of(module.chk_ai_enabled, ai_dialog)
    assert _is_descendant_of(module.combo_ai_toolchain, ai_dialog)


def test_top_level_config_launchers_include_result_dashboard_layout(qapp):
    loaded = {7: _detail_payload([], config_id=7, config_name="Cfg1")}
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    button = module.findChild(QPushButton, "open_result_dashboard_layout_card")
    dialog = module.findChild(QDialog, "top_level_result_dashboard_layout_card_dialog")

    assert button is not None
    assert button.text() == "结果展示布局"
    assert dialog is not None


def test_result_dashboard_layout_dialog_confirm_stages_without_saving(qapp):
    layout = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "primary_acoustic",
            }
        ],
    }
    loaded = {
        7: {
            **_detail_payload([], config_id=7, config_name="Cfg1"),
            "result_dashboard_layout": layout,
        }
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)

    module.findChild(QPushButton, "open_result_dashboard_layout_card").click()
    editor = module.findChild(
        ResultDashboardLayoutEditor, "result_dashboard_layout_editor"
    )
    editor.add_row()
    module.findChild(QPushButton, "TopLevelConfigCardConfirmButton").click()

    assert module._collect_data()["result_dashboard_layout"]["rows"] == 2
    assert config_service.begin_calls == 0


def test_result_dashboard_layout_confirm_marks_config_dirty_for_leave_prompt(
    qapp, monkeypatch
):
    layout = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "primary_acoustic",
            }
        ],
    }
    loaded = {
        7: {
            **_detail_payload([], config_id=7, config_name="Cfg1"),
            "result_dashboard_layout": layout,
        }
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)
    prompts = []
    monkeypatch.setattr(
        module,
        "_prompt_unsaved_config_changes",
        lambda action: prompts.append(action) or False,
    )

    module.findChild(QPushButton, "open_result_dashboard_layout_card").click()
    editor = module.findChild(
        ResultDashboardLayoutEditor, "result_dashboard_layout_editor"
    )
    editor.add_row()
    module.findChild(QPushButton, "TopLevelConfigCardConfirmButton").click()

    assert module.confirm_leave() is False
    assert prompts == ["leave"]
    assert config_service.begin_calls == 0


def test_result_dashboard_layout_dialog_x_discards_unconfirmed_edits(qapp):
    layout = {
        "version": 2,
        "columns": 1,
        "rows": 1,
        "panels": [
            {
                "id": "spectrum",
                "type": "spectrum",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "channel": "primary_acoustic",
            }
        ],
    }
    loaded = {
        7: {
            **_detail_payload([], config_id=7, config_name="Cfg1"),
            "result_dashboard_layout": layout,
        }
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)

    module.findChild(QPushButton, "open_result_dashboard_layout_card").click()
    editor = module.findChild(
        ResultDashboardLayoutEditor, "result_dashboard_layout_editor"
    )
    editor.add_row()
    dialog = module.findChild(QDialog, "top_level_result_dashboard_layout_card_dialog")
    dialog.findChild(QPushButton, "TopLevelConfigCardCloseButton").click()
    qapp.processEvents()

    data = module._collect_data()
    assert data["result_dashboard_layout"]["rows"] == 1
    assert [panel["id"] for panel in data["result_dashboard_layout"]["panels"]] == [
        "spectrum"
    ]
    assert config_service.begin_calls == 0


def test_malformed_loaded_result_dashboard_layout_stays_invalid_until_dialog_confirm(
    qapp,
):
    loaded = {
        7: {
            **_detail_payload([], config_id=7, config_name="Cfg1"),
            "result_dashboard_layout": "{bad json",
            "_result_dashboard_layout_error": "结果展示布局：JSON invalid",
        }
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)

    assert module._collect_data()["result_dashboard_layout"] == "{bad json"

    module.findChild(QPushButton, "open_result_dashboard_layout_card").click()
    editor = module.findChild(
        ResultDashboardLayoutEditor, "result_dashboard_layout_editor"
    )
    assert editor.layout_data()["panels"][0]["id"] == "stft_acoustic"
    module.findChild(QPushButton, "TopLevelConfigCardConfirmButton").click()

    data = module._collect_data()
    assert isinstance(data["result_dashboard_layout"], dict)
    assert data["result_dashboard_layout"]["panels"][0]["id"] == "stft_acoustic"


def _expected_centered_dialog_pos(dialog: QDialog, owner: QWidget) -> QPoint:
    owner_top_left = owner.mapToGlobal(QPoint(0, 0))
    return QPoint(
        owner_top_left.x() + (owner.width() - dialog.width()) // 2,
        owner_top_left.y() + (owner.height() - dialog.height()) // 2,
    )


def test_top_level_config_dialog_default_position_uses_window_center(qapp):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    button = module.findChild(QPushButton, "open_audio_vibration_hardware_card")
    dialog = module.findChild(
        QDialog, "top_level_audio_vibration_hardware_card_dialog"
    )
    assert button is not None
    assert dialog is not None
    assert dialog.parentWidget() is module

    button.click()
    qapp.processEvents()

    assert dialog.isVisible() is True
    assert dialog.pos() == _expected_centered_dialog_pos(dialog, module)


def test_top_level_config_dialogs_are_module_owned_and_hide_on_reload(qapp):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
        8: _detail_payload([], config_id=8, config_name="Cfg2"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    dialogs = [
        module.findChild(QDialog, "top_level_audio_vibration_hardware_card_dialog"),
        module.findChild(QDialog, "top_level_current_meter_card_dialog"),
        module.findChild(QDialog, "top_level_ai_binding_card_dialog"),
    ]
    for dialog in dialogs:
        dialog.show_card()
    qapp.processEvents()

    assert all(dialog is not None for dialog in dialogs)
    assert all(dialog.isVisible() for dialog in dialogs)

    module._populate_detail(loaded[8])
    qapp.processEvents()

    assert all(not dialog.isVisible() for dialog in dialogs)
    assert module.findChild(QComboBox, "combo_monitor_channel") is module.combo_monitor_channel
    assert module.findChild(QDoubleSpinBox, "spin_current_meter_offset") is module.spin_current_meter_offset


def test_top_level_config_buttons_show_only_one_dialog(qapp):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    audio_button = module.findChild(QPushButton, "open_audio_vibration_hardware_card")
    current_button = module.findChild(QPushButton, "open_current_meter_card")
    ai_button = module.findChild(QPushButton, "open_ai_binding_card")
    audio_dialog = module.findChild(
        QDialog, "top_level_audio_vibration_hardware_card_dialog"
    )
    current_dialog = module.findChild(
        QDialog, "top_level_current_meter_card_dialog"
    )
    ai_dialog = module.findChild(QDialog, "top_level_ai_binding_card_dialog")

    audio_button.click()
    qapp.processEvents()
    assert audio_dialog.isVisible() is True
    assert current_dialog.isVisible() is False
    assert ai_dialog.isVisible() is False

    current_button.click()
    qapp.processEvents()
    assert audio_dialog.isVisible() is False
    assert current_dialog.isVisible() is True
    assert ai_dialog.isVisible() is False

    ai_button.click()
    qapp.processEvents()
    assert audio_dialog.isVisible() is False
    assert current_dialog.isVisible() is False
    assert ai_dialog.isVisible() is True


def test_current_meter_dialog_opens_without_default_vertical_scroll(qapp):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    current_button = module.findChild(QPushButton, "open_current_meter_card")
    current_dialog = module.findChild(
        QDialog, "top_level_current_meter_card_dialog"
    )

    current_button.click()
    qapp.processEvents()

    body_scroll = current_dialog.findChild(
        QScrollArea,
        "ConfigSectionCardBodyScroll",
    )
    assert current_dialog.height() >= 620
    assert body_scroll.verticalScrollBar().maximum() == 0


def test_config_dialog_x_and_confirm_close_without_saving(qapp):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
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

    audio_button = module.findChild(QPushButton, "open_audio_vibration_hardware_card")
    dialog = module.findChild(
        QDialog, "top_level_audio_vibration_hardware_card_dialog"
    )
    close_button = dialog.findChild(QPushButton, "TopLevelConfigCardCloseButton")
    confirm_button = dialog.findChild(QPushButton, "TopLevelConfigCardConfirmButton")

    assert close_button is not None
    assert close_button.text() == "x"
    assert confirm_button is not None
    assert confirm_button.text() == "确定"

    audio_button.click()
    qapp.processEvents()
    close_button.click()
    qapp.processEvents()

    assert dialog.isVisible() is False
    assert config_service.begin_calls == 0
    assert bus.status.emissions == []

    audio_button.click()
    qapp.processEvents()
    confirm_button.click()
    qapp.processEvents()

    assert config_service.begin_calls == 0
    assert dialog.isVisible() is False
    assert bus.status.emissions == []


def test_config_confirm_leave_cancel_keeps_dirty_editor_without_saving(qapp, monkeypatch):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)
    qapp.processEvents()
    prompts = []

    monkeypatch.setattr(
        module,
        "_prompt_unsaved_config_changes",
        lambda action: prompts.append(action) or False,
    )

    original_record_duration = module.spin_record.value()
    module.spin_record.setValue(original_record_duration + 1.0)

    assert module.confirm_leave() is False
    assert prompts == ["leave"]
    assert config_service.begin_calls == 0
    assert module.spin_record.value() == pytest.approx(original_record_duration + 1.0)


def test_config_confirm_leave_save_persists_without_success_popup(qapp, monkeypatch):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    bus = SimpleNamespace(
        refresh_channels=_SignalStub(),
        status=_SignalStub(),
        config_changed=_SignalStub(),
    )
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service, bus=bus)
    qapp.processEvents()
    prompts = []
    infos = []

    monkeypatch.setattr(
        module,
        "_prompt_unsaved_config_changes",
        lambda action: prompts.append(action) or True,
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr(
        "ui.modules.config.QMessageBox.information",
        lambda *args: infos.append(args),
    )

    module.spin_record.setValue(module.spin_record.value() + 1.0)

    assert module.confirm_leave() is True
    assert prompts == ["leave"]
    assert config_service.begin_calls == 1
    assert infos == []
    assert bus.status.emissions == [(("配置已保存",), {})]

    assert module.confirm_leave() is True
    assert prompts == ["leave"]


def test_config_unsaved_prompt_uses_save_discard_and_cancel_buttons(qapp, monkeypatch):
    module = _make_module(
        config_service=_ConfigServiceStub(
            loaded_configs={7: _detail_payload([], config_id=7, config_name="Cfg1")}
        )
    )
    button_calls = []

    class _FakeMessageBox:
        class Icon:
            Warning = object()

        class ButtonRole:
            AcceptRole = object()
            DestructiveRole = object()
            RejectRole = object()

        def __init__(self, parent):
            self.parent = parent
            self.clicked = object()

        def setIcon(self, icon):
            self.icon = icon

        def setWindowTitle(self, title):
            self.title = title

        def setText(self, text):
            self.text = text

        def addButton(self, text, role):
            button = object()
            button_calls.append((text, role, button))
            return button

        def setDefaultButton(self, button):
            self.default_button = button

        def setEscapeButton(self, button):
            self.escape_button = button

        def exec(self):
            self.clicked = button_calls[1][2]

        def clickedButton(self):
            return self.clicked

    monkeypatch.setattr("ui.modules.config.QMessageBox", _FakeMessageBox)

    assert module._prompt_unsaved_config_changes("leave") == "discard"
    assert [text for text, _role, _button in button_calls] == [
        "保存并继续",
        "放弃保存",
        "取消",
    ]
    assert button_calls[1][1] is _FakeMessageBox.ButtonRole.DestructiveRole


def test_config_confirm_leave_discard_restores_loaded_config_without_saving(
    qapp,
    monkeypatch,
):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)
    qapp.processEvents()

    original_record_duration = module.spin_record.value()
    module.spin_record.setValue(original_record_duration + 1.0)

    monkeypatch.setattr(
        module,
        "_prompt_unsaved_config_changes",
        lambda action: "discard",
    )

    assert module.confirm_leave() is True
    assert config_service.begin_calls == 0
    assert module.spin_record.value() == pytest.approx(original_record_duration)
    assert module._has_unsaved_changes() is False


def test_config_list_switch_cancel_restores_previous_dirty_config(qapp, monkeypatch):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
        8: _detail_payload([], config_id=8, config_name="Cfg2"),
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)
    qapp.processEvents()
    prompts = []

    monkeypatch.setattr(
        module,
        "_prompt_unsaved_config_changes",
        lambda action: prompts.append(action) or False,
    )

    assert module._current_config_id == 7
    edited_record_duration = module.spin_record.value() + 1.0
    module.spin_record.setValue(edited_record_duration)

    module.config_list.setCurrentRow(1)
    qapp.processEvents()

    assert prompts == ["switch"]
    assert module._current_config_id == 7
    assert module.config_list.currentItem().data(Qt.ItemDataRole.UserRole) == 7
    assert module.spin_record.value() == pytest.approx(edited_record_duration)
    assert config_service.begin_calls == 0


def test_config_list_switch_save_then_loads_target_config(qapp, monkeypatch):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
        8: _detail_payload([], config_id=8, config_name="Cfg2"),
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)
    qapp.processEvents()
    prompts = []

    monkeypatch.setattr(
        module,
        "_prompt_unsaved_config_changes",
        lambda action: prompts.append(action) or True,
    )

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr("ui.modules.config.QMessageBox.information", lambda *args: None)

    module.spin_record.setValue(module.spin_record.value() + 1.0)

    module.config_list.setCurrentRow(1)
    qapp.processEvents()

    assert prompts == ["switch"]
    assert config_service.begin_calls == 1
    assert module._current_config_id == 8
    assert module.config_list.currentItem().data(Qt.ItemDataRole.UserRole) == 8
    assert module.ed_name.text() == "Cfg2"


def test_top_level_config_dialogs_hide_when_refresh_enters_empty_state(qapp):
    loaded = {
        7: _detail_payload([], config_id=7, config_name="Cfg1"),
    }
    config_service = _ConfigServiceStub(loaded_configs=loaded)
    module = _make_module(config_service=config_service)
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    dialogs = [
        module.findChild(QDialog, "top_level_audio_vibration_hardware_card_dialog"),
        module.findChild(QDialog, "top_level_current_meter_card_dialog"),
        module.findChild(QDialog, "top_level_ai_binding_card_dialog"),
    ]
    for dialog in dialogs:
        dialog.show_card()
    qapp.processEvents()

    assert all(dialog.isVisible() for dialog in dialogs)

    config_service._loaded_configs.clear()
    config_service.saved_configs = []
    module._refresh_list()
    qapp.processEvents()

    assert module.detail.isVisible() is False
    assert all(not dialog.isVisible() for dialog in dialogs)


def test_populate_detail_loads_default_current_meter_controls(qapp):
    module = _make_module()

    module._populate_detail(_detail_payload([]))

    assert module.combo_current_meter_source.currentData() == "modbus_tcp"
    assert module.spin_current_meter_port.value() == 502
    assert module.spin_current_meter_unit_id.value() == 1
    assert module.combo_current_meter_register_type.currentData() == "input"
    assert module.spin_current_meter_register_address.value() == 0
    assert module.combo_current_meter_data_type.currentData() == "uint16"
    assert module.combo_current_meter_byte_order.currentData() == "big"
    assert module.combo_current_meter_word_order.currentData() == "big"
    assert module.spin_current_meter_scale.value() == pytest.approx(0.001)
    assert module.spin_current_meter_offset.value() == pytest.approx(0.0)
    assert module.spin_current_meter_poll_interval_ms.value() == 200
    assert module.spin_current_meter_timeout_ms.value() == 1000
    assert _field_label_text(module, "spin_current_meter_offset") == "电流表偏移(mA)"


def test_populate_detail_preserves_current_meter_unit_id_zero():
    module = _make_module()
    payload = _detail_payload([])
    payload["current_meter"] = {
        "enabled": True,
        "source": "modbus_tcp",
        "host": "192.168.1.20",
        "unit_id": 0,
    }

    module._populate_detail(payload)

    assert module.spin_current_meter_unit_id.value() == 0
    assert module._collect_data()["current_meter"]["unit_id"] == 0


def test_populate_detail_uses_neutral_offset_label_for_generic_float32_config(qapp):
    module = _make_module()
    payload = _detail_payload([])
    payload["current_meter"] = {
        "enabled": True,
        "host": "192.168.1.20",
        "unit_id": 3,
        "register_type": "holding",
        "register_address": 8,
        "register_count": 2,
        "data_type": "float32",
        "byte_order": "little",
        "word_order": "big",
        "scale": 1.0,
        "offset": 1.5,
        "poll_interval_ms": 250,
        "timeout_ms": 1200,
    }

    module._populate_detail(payload)

    assert _field_label_text(module, "spin_current_meter_offset") == "电流表偏移"


def test_current_meter_offset_label_refreshes_when_controls_change(qapp):
    module = _make_module()

    module._populate_detail(_detail_payload([]))

    assert _field_label_text(module, "spin_current_meter_offset") == "电流表偏移(mA)"

    module.combo_current_meter_register_type.setCurrentIndex(
        module.combo_current_meter_register_type.findData("holding")
    )
    module.combo_current_meter_data_type.setCurrentIndex(
        module.combo_current_meter_data_type.findData("float32")
    )
    module.combo_current_meter_byte_order.setCurrentIndex(
        module.combo_current_meter_byte_order.findData("little")
    )
    module.combo_current_meter_word_order.setCurrentIndex(
        module.combo_current_meter_word_order.findData("little")
    )
    module.spin_current_meter_scale.setValue(1.0)

    assert _field_label_text(module, "spin_current_meter_offset") == "电流表偏移"

    module.combo_current_meter_register_type.setCurrentIndex(
        module.combo_current_meter_register_type.findData("input")
    )
    module.combo_current_meter_data_type.setCurrentIndex(
        module.combo_current_meter_data_type.findData("uint16")
    )
    module.combo_current_meter_byte_order.setCurrentIndex(
        module.combo_current_meter_byte_order.findData("big")
    )
    module.combo_current_meter_word_order.setCurrentIndex(
        module.combo_current_meter_word_order.findData("big")
    )
    module.spin_current_meter_scale.setValue(0.001)

    assert _field_label_text(module, "spin_current_meter_offset") == "电流表偏移(mA)"


def test_populate_detail_preserves_explicit_current_meter_values(qapp):
    module = _make_module()
    payload = _detail_payload([])
    payload["current_meter"] = {
        "enabled": True,
        "host": "192.168.1.20",
        "port": 1502,
        "unit_id": 3,
        "register_type": "holding",
        "register_address": 120,
        "register_count": 2,
        "data_type": "float32",
        "byte_order": "little",
        "word_order": "big",
        "scale": 1.0,
        "offset": 1.5,
        "poll_interval_ms": 250,
        "timeout_ms": 1200,
    }
    module._populate_detail(payload)

    data = module._collect_data()

    assert data["current_meter"] == {
        "enabled": True,
        "source": "modbus_tcp",
        "host": "192.168.1.20",
        "port": 1502,
        "unit_id": 3,
        "register_type": "holding",
        "register_address": 120,
        "register_count": 2,
        "data_type": "float32",
        "byte_order": "little",
        "word_order": "big",
        "scale": pytest.approx(1.0),
        "offset": pytest.approx(1.5),
        "poll_interval_ms": 250,
        "timeout_ms": 1200,
    }


def test_plc_current_source_disables_only_modbus_controls(qapp):
    module = _make_module()
    payload = _detail_payload([])
    payload["current_meter"] = {
        "enabled": True,
        "source": "plc",
        "host": "192.168.1.20",
    }

    module._populate_detail(payload)

    assert module.combo_current_meter_source.currentData() == "plc"
    assert module.chk_current_meter_enabled.isEnabled()
    assert module.combo_current_meter_source.isEnabled()
    assert all(
        not control.isEnabled()
        for control in module._current_meter_modbus_controls
    )
    assert module._collect_data()["current_meter"]["source"] == "plc"

    module.combo_current_meter_source.setCurrentIndex(
        module.combo_current_meter_source.findData("modbus_tcp")
    )

    assert all(
        control.isEnabled()
        for control in module._current_meter_modbus_controls
    )


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
    assert module.spin_monitor_gain_db.maximum() == pytest.approx(35.0)
    assert module.spin_monitor_gain_db.singleStep() == pytest.approx(1.0)
    assert module.spin_monitor_gain_db.decimals() == 1
    assert module.spin_monitor_gain_db.suffix() == " dB"


def test_managed_input_hardware_binding_uses_stable_id_and_no_output(qapp):
    module = _make_module(hardware_service=_ManagedHardwareServiceStub())
    payload = _detail_payload([_channel_payload(i) for i in range(4)])
    payload["input_hardware_device_id"] = 22
    payload["selected_channels"] = [0, 3]
    payload["output_device_name"] = None
    payload["output_device_hostapi"] = None
    payload["output_device_samplerate"] = None

    module._populate_detail(payload)

    selected = module.combo_input_dev.currentData()
    assert selected["hardware_id"] == 22
    assert module.combo_input_dev.currentText().startswith("产线 B 声卡")
    assert len(module._channel_checkboxes) == 4

    collected = module._collect_data()
    assert collected["input_hardware_device_id"] == 22
    assert collected["input_device_name"] == "Interface B"
    assert collected["output_device_name"] is None
    assert collected["output_device_hostapi"] is None
    assert collected["output_device_samplerate"] is None


def test_managed_input_hardware_keeps_config_level_monitor_device(qapp):
    module = _make_module(hardware_service=_ManagedHardwareServiceStub())
    payload = _detail_payload([_channel_payload(0), _channel_payload(1)])
    payload.update(
        {
            "input_hardware_device_id": 11,
            "selected_channels": [0],
            "output_device_name": "Line Monitor",
            "output_device_hostapi": "WASAPI",
            "output_device_samplerate": 48000.0,
            "monitor_channel_index": 0,
            "monitor_gain_db": -6.0,
        }
    )

    module._populate_detail(payload)
    collected = module._collect_data()

    assert module.combo_output_dev.currentText() == "Line Monitor (WASAPI)"
    assert collected["output_device_name"] == "Line Monitor"
    assert collected["output_device_hostapi"] == "WASAPI"
    assert collected["output_device_samplerate"] == 48000.0
    assert collected["monitor_channel_index"] == 0
    assert collected["monitor_gain_db"] == -6.0
    assert module.combo_monitor_channel.count() == 2


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


def test_collect_data_includes_current_meter_settings(qapp):
    module = _make_module()
    module._current_config_id = 7
    module.chk_current_meter_enabled.setChecked(True)
    module.combo_current_meter_source.setCurrentIndex(
        module.combo_current_meter_source.findData("modbus_tcp")
    )
    module.ed_current_meter_host.setText("192.168.1.20")
    module.spin_current_meter_port.setValue(1502)
    module.spin_current_meter_unit_id.setValue(3)
    module.combo_current_meter_register_type.setCurrentIndex(
        module.combo_current_meter_register_type.findData("input")
    )
    module.spin_current_meter_register_address.setValue(120)
    module.combo_current_meter_data_type.setCurrentIndex(
        module.combo_current_meter_data_type.findData("uint16")
    )
    module.combo_current_meter_byte_order.setCurrentIndex(
        module.combo_current_meter_byte_order.findData("little")
    )
    module.combo_current_meter_word_order.setCurrentIndex(
        module.combo_current_meter_word_order.findData("big")
    )
    module.spin_current_meter_scale.setValue(0.1)
    module.spin_current_meter_offset.setValue(1.5)
    module.spin_current_meter_poll_interval_ms.setValue(250)
    module.spin_current_meter_timeout_ms.setValue(1200)

    data = module._collect_data()

    assert data["current_meter"] == {
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
        "scale": pytest.approx(0.1),
        "offset": pytest.approx(1.5),
        "poll_interval_ms": 250,
        "timeout_ms": 1200,
    }


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


def test_channel_section_launchers_open_movable_card_dialogs(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    launcher_panel = container.findChild(QWidget, "channel_section_launcher_panel")
    assert launcher_panel is not None
    expected_buttons = {
        "stft": "STFT 参数",
        "stft_score": "STFT 评分",
        "spl": "声压级参数",
        "loudness": "响度参数",
        "golden_sample": "黄金样本",
        "fft": "FFT 参数",
        "octave": "1/3 倍频程参数",
    }
    for section_id, title in expected_buttons.items():
        button = container.findChild(QPushButton, f"open_{section_id}_section")
        assert button is not None
        assert button.text() == title

    fft_dialog = module._channel_section_dialogs[(0, "fft")]
    assert fft_dialog.isVisible() is False

    fft_button = container.findChild(QPushButton, "open_fft_section")
    fft_button.click()
    qapp.processEvents()

    assert fft_dialog.isVisible() is True
    assert fft_dialog.parentWidget() is container
    initial_pos = fft_dialog.pos()
    assert initial_pos == _expected_centered_dialog_pos(fft_dialog, module)
    assert fft_dialog.windowFlags() & Qt.WindowType.Tool
    assert fft_dialog.findChild(CollapsibleGroupBox).section_id() == "fft"
    assert fft_dialog.width() >= 760
    fft_editor = fft_dialog.findChild(LimitTableEditor, "fft_limit_table_editor")
    assert fft_editor is not None
    fft_editor.findChild(QPushButton, "fft_add_limit_table").click()
    fft_table = fft_dialog.findChild(QTableWidget, "fft_limit_points_0")
    assert fft_table.horizontalHeader().defaultSectionSize() == 72
    assert fft_table.verticalHeader().defaultSectionSize() == 30

    fft_dialog.hide()
    fft_button.click()
    qapp.processEvents()

    assert fft_dialog.pos() == initial_pos

    moved_pos = fft_dialog.pos() + QPoint(31, 17)
    fft_dialog.move(moved_pos)
    fft_dialog._user_moved = True
    fft_dialog.hide()
    fft_button.click()
    qapp.processEvents()

    assert fft_dialog.pos() == moved_pos

    close_button = fft_dialog.findChild(QPushButton, "ChannelSectionCardCloseButton")
    assert close_button.text() == "x"
    confirm_button = fft_dialog.findChild(QPushButton, "ChannelSectionCardConfirmButton")
    assert confirm_button is not None
    assert confirm_button.text() == "确定"
    close_button.click()
    qapp.processEvents()

    assert fft_dialog.isVisible() is False
    assert module._channel_section_dialogs[(0, "fft")] is fft_dialog


def test_fft_and_octave_dialogs_use_wide_compact_limit_tables(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    container = module._channel_containers[0]
    fft_button = container.findChild(QPushButton, "open_fft_section")
    octave_button = container.findChild(QPushButton, "open_octave_section")
    fft_dialog = module._channel_section_dialogs[(0, "fft")]
    octave_dialog = module._channel_section_dialogs[(0, "octave")]

    fft_button.click()
    qapp.processEvents()

    fft_editor = fft_dialog.findChild(LimitTableEditor, "fft_limit_table_editor")
    fft_editor.findChild(QPushButton, "fft_add_limit_table").click()
    fft_table = fft_dialog.findChild(QTableWidget, "fft_limit_points_0")
    assert fft_dialog.width() >= 760
    assert fft_table.horizontalHeader().defaultSectionSize() == 72
    assert fft_table.verticalHeader().defaultSectionSize() == 30

    octave_button.click()
    qapp.processEvents()

    oct_editor = octave_dialog.findChild(LimitTableEditor, "oct_limit_table_editor")
    oct_editor.findChild(QPushButton, "oct_add_limit_table").click()
    octave_table = octave_dialog.findChild(QTableWidget, "oct_limit_points_0")
    assert octave_dialog.width() >= 720
    assert octave_table.horizontalHeader().defaultSectionSize() == 72
    assert octave_table.verticalHeader().defaultSectionSize() == 30


def test_channel_config_dialog_confirm_closes_without_saving(qapp):
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

    fft_button = module._channel_containers[0].findChild(
        QPushButton,
        "open_fft_section",
    )
    fft_button.click()
    qapp.processEvents()

    fft_dialog = module._channel_section_dialogs[(0, "fft")]
    confirm_button = fft_dialog.findChild(
        QPushButton,
        "ChannelSectionCardConfirmButton",
    )
    assert confirm_button is not None
    assert confirm_button.text() == "确定"

    confirm_button.click()
    qapp.processEvents()

    assert config_service.begin_calls == 0
    assert fft_dialog.isVisible() is False
    assert bus.status.emissions == []


def test_config_card_buttons_show_only_one_dialog_across_top_level_and_channel(qapp):
    loaded = {
        7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    module.resize(1600, 1200)
    module.show()
    qapp.processEvents()

    audio_button = module.findChild(QPushButton, "open_audio_vibration_hardware_card")
    audio_dialog = module.findChild(
        QDialog, "top_level_audio_vibration_hardware_card_dialog"
    )
    container = module._channel_containers[0]
    fft_button = container.findChild(QPushButton, "open_fft_section")
    spl_button = container.findChild(QPushButton, "open_spl_section")
    fft_dialog = module._channel_section_dialogs[(0, "fft")]
    spl_dialog = module._channel_section_dialogs[(0, "spl")]

    audio_button.click()
    qapp.processEvents()
    assert audio_dialog.isVisible() is True
    assert fft_dialog.isVisible() is False
    assert spl_dialog.isVisible() is False

    fft_button.click()
    qapp.processEvents()
    assert audio_dialog.isVisible() is False
    assert fft_dialog.isVisible() is True
    assert spl_dialog.isVisible() is False

    spl_button.click()
    qapp.processEvents()
    assert audio_dialog.isVisible() is False
    assert fft_dialog.isVisible() is False
    assert spl_dialog.isVisible() is True


def test_channel_section_cards_do_not_stretch_after_repeated_collapses(qapp):
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

    module._channel_section_dialogs[(0, "stft")].show_card()
    module._channel_section_dialogs[(0, "spl")].show_card()
    qapp.processEvents()

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


def test_on_save_preserves_collapsed_section_state_in_card_dialogs(
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
    assert restored.findChild(QPushButton, "open_stft_section") is not None
    assert restored.findChild(QPushButton, "open_spl_section") is not None

    restored.findChild(QPushButton, "open_stft_section").click()
    qapp.processEvents()

    assert module._channel_section_dialogs[(0, "stft")].isVisible() is True
    assert restored_boxes["stft"].is_expanded() is False


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
    assert _ancestor_group_title(fft_resolution) == "FFT 参数"
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
    summary_combo = container.findChild(QComboBox, "stft_score_summary_mode")
    assert summary_combo.itemText(summary_combo.findData("percentile")) == "分位数"
    assert container.findChild(
        QDoubleSpinBox, "stft_score_percentile"
    ).value() == pytest.approx(20.0)
    assert container.findChild(
        QDoubleSpinBox, "stft_score_loudness_reference_sone"
    ).value() == pytest.approx(12.5)
    assert container.findChild(
        QDoubleSpinBox, "stft_score_modulation_reference"
    ).value() == pytest.approx(0.3)
    change_reference = container.findChild(
        QDoubleSpinBox, "stft_score_modulation_reference"
    )
    assert change_reference.decimals() == 5
    assert change_reference.minimum() == pytest.approx(0.00001)
    assert change_reference.singleStep() == pytest.approx(0.00001)
    assert (
        _field_label_text(container, "stft_score_modulation_reference")
        == "响度变化参考值（sone）"
    )
    assert (
        container.findChild(QDoubleSpinBox, "stft_score_harmonic_ratio_reference")
        is None
    )
    assert (
        container.findChild(
            QDoubleSpinBox, "stft_score_frequency_instability_reference"
        )
        is None
    )
    assert container.findChild(QSpinBox, "stft_score_harmonic_start_order") is None
    assert container.findChild(QSpinBox, "stft_score_harmonic_end_order") is None
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
    ]

    assert enabled.isChecked() is True
    assert all(control.isEnabled() for control in score_controls)

    enabled.setChecked(False)

    assert all(not control.isEnabled() for control in score_controls)

    enabled.setChecked(True)

    assert all(control.isEnabled() for control in score_controls)


def test_collect_data_includes_stft_score_fields(qapp):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    stft_score_harmonic_ratio_reference=0.45,
                    stft_score_frequency_instability_reference=0.09,
                    stft_score_harmonic_start_order=4,
                    stft_score_harmonic_end_order=90,
                )
            ]
        )
    )
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
    ).setValue(0.00001)

    data = module._collect_data()

    channel = data["channels"][0]
    assert channel["stft_score_enabled"] is False
    assert channel["stft_score_summary_mode"] == "min"
    assert channel["stft_score_percentile"] == pytest.approx(25.0)
    assert channel["stft_score_loudness_reference_sone"] == pytest.approx(11.5)
    assert channel["stft_score_modulation_reference"] == pytest.approx(0.00001)
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


def test_populate_detail_restores_fft_weighting(qapp):
    module = _make_module()
    payload = _detail_payload([_channel_payload(0, fft_weighting="C")])

    module._populate_detail(payload)
    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "fft_weighting")

    assert combo is not None
    assert combo.currentText() == "C"


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


def test_collect_data_includes_fft_weighting(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]
    combo = container.findChild(QComboBox, "fft_weighting")
    assert combo is not None
    combo.setCurrentIndex(combo.findText("A"))

    data = module._collect_data()

    assert data["channels"][0]["fft_weighting"] == "A"


def test_config_module_loads_legacy_fft_and_octave_limits_as_separate_limit_tables(
    qapp,
):
    module = _make_module()
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": 0.0, "upper": 100.0},
                        {"freq": 2000.0, "lower": 10.0, "upper": 90.0},
                    ],
                    oct_band_limits=[
                        {"freq": 125.0, "lower": 20.0, "upper": 70.0},
                        {"freq": 250.0, "lower": 30.0, "upper": 80.0},
                    ],
                )
            ]
        )
    )
    container = module._channel_containers[0]

    fft_editor = container.findChild(LimitTableEditor, "fft_limit_table_editor")
    oct_editor = container.findChild(LimitTableEditor, "oct_limit_table_editor")

    assert fft_editor is not None
    assert oct_editor is not None
    assert fft_editor.limits() == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 0.0},
                {"freq": 2000.0, "limit": 10.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        },
    ]
    assert oct_editor.limits() == [
        {
            "kind": "lower",
            "points": [
                {"freq": 125.0, "limit": 20.0},
                {"freq": 250.0, "limit": 30.0},
            ],
        },
        {
            "kind": "upper",
            "points": [
                {"freq": 125.0, "limit": 70.0},
                {"freq": 250.0, "limit": 80.0},
            ],
        },
    ]


def test_config_module_loads_and_collects_grouped_limit_tables(qapp):
    module = _make_module()
    fft_limits = [
        {
            "kind": "upper",
            "points": [
                {"freq": 2000.0, "limit": 90.0},
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 70.0},
            ],
        },
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 5.0},
                {"freq": 3000.0, "limit": 10.0},
            ],
        },
    ]
    oct_limits = [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 20.0},
                {"freq": 2000.0, "limit": 30.0},
            ],
        }
    ]
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=fft_limits,
                    oct_band_limits=oct_limits,
                )
            ]
        )
    )

    data = module._collect_data()

    assert data["channels"][0]["fft_band_limits"] == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
                {"freq": 2000.0, "limit": 70.0},
            ],
        },
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 5.0},
                {"freq": 3000.0, "limit": 10.0},
            ],
        },
    ]
    assert data["channels"][0]["oct_band_limits"] == oct_limits


def test_config_module_loads_invalid_legacy_fft_limits_for_validation(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {"freq": 1000.0, "lower": 0.0, "upper": "bad"},
                    ],
                )
            ]
        )
    )
    container = module._channel_containers[0]

    editor = container.findChild(LimitTableEditor, "fft_limit_table_editor")
    collected = module._collect_data()
    errors = ConfigService(SimpleNamespace(config_changed=_SignalStub())).validate(
        collected
    )

    assert editor is not None
    assert collected["channels"][0]["fft_band_limits"] == [
        {
            "kind": "lower",
            "points": [{"freq": 1000.0, "limit": 0.0}],
        },
        {
            "kind": "upper",
            "points": [{"freq": 1000.0, "limit": "bad"}],
        },
    ]
    assert any(
        "FFT限制表" in error and "限值必须是有限数字" in error
        for error in errors
    )


def test_config_module_loads_invalid_grouped_octave_limits_for_validation(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    oct_band_limits=[
                        {
                            "kind": "ceiling",
                            "points": [
                                {"freq": 1000.0, "limit": "bad"},
                                {"freq": "oops", "limit": 80.0},
                            ],
                        },
                        {"kind": "upper", "points": "bad-points"},
                    ],
                )
            ]
        )
    )
    container = module._channel_containers[0]

    editor = container.findChild(LimitTableEditor, "oct_limit_table_editor")
    kind_combo = editor.findChild(QComboBox, "oct_limit_kind_0")
    collected = module._collect_data()
    limits = collected["channels"][0]["oct_band_limits"]
    errors = ConfigService(SimpleNamespace(config_changed=_SignalStub())).validate(
        collected
    )

    assert editor is not None
    assert kind_combo.currentText() == "无效: ceiling"
    assert kind_combo.currentData() == "ceiling"
    assert limits == [
        {
            "kind": "ceiling",
            "points": [
                {"freq": 1000.0, "limit": "bad"},
                {"freq": "oops", "limit": 80.0},
            ],
        },
        {
            "kind": "upper",
            "points": [{"freq": "", "limit": "bad-points"}],
        },
    ]
    assert "通道 1: 倍频程限制表第 1 项类型必须是上限或下限" in errors
    assert any(
        "倍频程限制表" in error and "频率必须是有限数字" in error
        for error in errors
    )
    assert any(
        "倍频程限制表" in error and "限值必须是有限数字" in error
        for error in errors
    )


def test_limit_table_editor_invalid_grouped_kind_is_visible_and_collected_until_changed(
    qapp,
):
    editor = LimitTableEditor(object_name="fft_limit_table_editor", label="FFT")
    editor.set_limits(
        [
            {
                "kind": "ceiling",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 90.0},
                ],
            }
        ]
    )

    kind_combo = editor.findChild(QComboBox, "fft_limit_kind_0")
    assert kind_combo is not None
    assert kind_combo.currentText() == "无效: ceiling"
    assert kind_combo.currentData() == "ceiling"
    assert kind_combo.findData("ceiling") != -1
    assert editor.limits()[0]["kind"] == kind_combo.currentData()

    kind_combo.setCurrentIndex(kind_combo.findData("lower"))

    assert kind_combo.currentData() == "lower"
    assert editor.limits()[0]["kind"] == "lower"
    assert kind_combo.findData("ceiling") == -1

    kind_combo.setCurrentIndex(kind_combo.findData("ceiling"))

    assert editor.limits()[0]["kind"] in {"upper", "lower"}
    assert editor.limits()[0]["kind"] != "ceiling"


def test_limit_table_editor_add_and_delete_table_updates_collected_payload(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    container = module._channel_containers[0]
    fft_editor = container.findChild(LimitTableEditor, "fft_limit_table_editor")
    oct_editor = container.findChild(LimitTableEditor, "oct_limit_table_editor")
    assert fft_editor is not None
    assert oct_editor is not None

    fft_editor.findChild(QPushButton, "fft_add_limit_table").click()
    oct_editor.findChild(QPushButton, "oct_add_limit_table").click()

    assert fft_editor.limits() == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 100.0},
            ],
        }
    ]
    assert oct_editor.limits() == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 100.0},
            ],
        }
    ]

    kind_combo = fft_editor.findChild(QComboBox, "fft_limit_kind_0")
    kind_combo.setCurrentIndex(kind_combo.findData("lower"))

    assert fft_editor.limits() == [
        {
            "kind": "lower",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 100.0},
            ],
        }
    ]

    fft_editor.findChild(QPushButton, "fft_delete_limit_table_0").click()

    assert fft_editor.limits() == []
    assert module._collect_data()["channels"][0]["fft_band_limits"] == []


def test_limit_table_editor_point_actions_preserve_duplicate_frequencies(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {
                            "kind": "upper",
                            "points": [
                                {"freq": 1000.0, "limit": 100.0},
                                {"freq": 2000.0, "limit": 100.0},
                            ],
                        }
                    ],
                )
            ]
        )
    )
    editor = module._channel_containers[0].findChild(
        LimitTableEditor,
        "fft_limit_table_editor",
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    assert table is not None

    editor.findChild(QPushButton, "fft_add_limit_point_0").click()
    assert editor.limits()[0]["points"] == [
        {"freq": 1000.0, "limit": 100.0},
        {"freq": 2000.0, "limit": 100.0},
        {"freq": 3000.0, "limit": 100.0},
    ]

    table.selectColumn(0)
    editor.findChild(QPushButton, "fft_insert_limit_point_0").click()
    table.item(0, 1).setText("2000")
    table.item(1, 1).setText("50")

    table.clearSelection()
    table.selectColumn(3)
    editor.findChild(QPushButton, "fft_delete_limit_point_0").click()

    assert module._collect_data()["channels"][0]["fft_band_limits"] == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 50.0},
                {"freq": 2000.0, "limit": 100.0},
            ],
        }
    ]


def test_limit_table_editor_insert_after_final_point_copies_selected_point(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {
                            "kind": "upper",
                            "points": [
                                {"freq": 1000.0, "limit": 95.0},
                                {"freq": 2000.0, "limit": 82.5},
                            ],
                        }
                    ],
                )
            ]
        )
    )
    editor = module._channel_containers[0].findChild(
        LimitTableEditor,
        "fft_limit_table_editor",
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    assert table is not None

    table.selectColumn(1)
    editor.findChild(QPushButton, "fft_insert_limit_point_0").click()

    assert module._collect_data()["channels"][0]["fft_band_limits"] == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 95.0},
                {"freq": 2000.0, "limit": 82.5},
                {"freq": 2000.0, "limit": 82.5},
            ],
        }
    ]


def test_limit_table_editor_insert_copies_invalid_selected_point_raw_values(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {
                            "kind": "upper",
                            "points": [
                                {"freq": 1000.0, "limit": 95.0},
                                {"freq": 2000.0, "limit": 82.5},
                            ],
                        }
                    ],
                )
            ]
        )
    )
    editor = module._channel_containers[0].findChild(
        LimitTableEditor,
        "fft_limit_table_editor",
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    assert table is not None
    table.item(0, 0).setText("bad-freq")
    table.item(1, 0).setText("bad-limit")

    table.selectColumn(0)
    editor.findChild(QPushButton, "fft_insert_limit_point_0").click()

    assert module._collect_data()["channels"][0]["fft_band_limits"] == [
        {
            "kind": "upper",
            "points": [
                {"freq": "bad-freq", "limit": "bad-limit"},
                {"freq": "bad-freq", "limit": "bad-limit"},
                {"freq": 2000.0, "limit": 82.5},
            ],
        }
    ]


@pytest.mark.parametrize("selected_freq", [-100.0, 0.0])
def test_limit_table_editor_insert_copies_non_positive_selected_frequency(
    qapp,
    selected_freq,
):
    editor = LimitTableEditor(object_name="fft_limit_table_editor", label="FFT")
    editor.set_limits(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": selected_freq, "limit": 95.0},
                    {"freq": 2000.0, "limit": 82.5},
                ],
            }
        ]
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    assert table is not None

    table.selectColumn(0)
    editor.findChild(QPushButton, "fft_insert_limit_point_0").click()

    assert editor.limits() == [
        {
            "kind": "upper",
            "points": [
                {"freq": selected_freq, "limit": 95.0},
                {"freq": selected_freq, "limit": 95.0},
                {"freq": 2000.0, "limit": 82.5},
            ],
        }
    ]


def test_limit_table_editor_invalid_cells_remain_for_config_validation(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(
        _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {
                            "kind": "upper",
                            "points": [
                                {"freq": 1000.0, "limit": 100.0},
                                {"freq": 2000.0, "limit": 90.0},
                            ],
                        }
                    ],
                )
            ]
        )
    )
    editor = module._channel_containers[0].findChild(
        LimitTableEditor,
        "fft_limit_table_editor",
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    table.item(1, 0).setText("")

    collected = module._collect_data()
    errors = ConfigService(SimpleNamespace(config_changed=_SignalStub())).validate(collected)

    assert collected["channels"][0]["fft_band_limits"][0]["points"][0] == {
        "freq": 1000.0,
        "limit": "",
    }
    assert any("限值" in error for error in errors)


@pytest.mark.parametrize("raw_limits", [None, "", 0, False])
def test_limit_table_editor_falsey_non_list_limits_remain_invalid_for_validation(
    qapp,
    raw_limits,
):
    editor = LimitTableEditor(object_name="fft_limit_table_editor", label="FFT")

    editor.set_limits(raw_limits)
    collected_limits = editor.limits()
    payload = _detail_payload(
        [_channel_payload(0, fft_band_limits=collected_limits)]
    )
    errors = ConfigService(SimpleNamespace(config_changed=_SignalStub())).validate(
        payload
    )

    assert collected_limits != []
    assert collected_limits[0]["points"] != []
    assert any("FFT限制表" in error for error in errors)


def test_limit_table_editor_user_actions_emit_limits_changed_once(qapp):
    editor = LimitTableEditor(object_name="fft_limit_table_editor", label="FFT")
    emissions = []
    editor.limitsChanged.connect(lambda: emissions.append(editor.limits()))

    editor.set_limits([])
    assert emissions == []

    def assert_one_emission(action):
        before = len(emissions)
        action()
        assert len(emissions) == before + 1

    assert_one_emission(
        lambda: editor.findChild(QPushButton, "fft_add_limit_table").click()
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    assert table is not None
    assert table.columnCount() == 2

    assert_one_emission(
        lambda: editor.findChild(QPushButton, "fft_add_limit_point_0").click()
    )

    table.selectColumn(0)
    assert_one_emission(
        lambda: editor.findChild(QPushButton, "fft_insert_limit_point_0").click()
    )

    table.clearSelection()
    table.selectColumn(1)
    assert_one_emission(
        lambda: editor.findChild(QPushButton, "fft_delete_limit_point_0").click()
    )

    kind_combo = editor.findChild(QComboBox, "fft_limit_kind_0")
    assert kind_combo is not None
    assert_one_emission(
        lambda: kind_combo.setCurrentIndex(kind_combo.findData("lower"))
    )

    assert_one_emission(
        lambda: editor.findChild(QPushButton, "fft_delete_limit_table_0").click()
    )
    assert editor.limits() == []


def test_limit_table_editor_set_limits_does_not_emit_limits_changed(qapp):
    editor = LimitTableEditor(object_name="fft_limit_table_editor", label="FFT")
    emissions = []
    editor.limitsChanged.connect(lambda: emissions.append(editor.limits()))

    editor.set_limits(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 90.0},
                ],
            }
        ]
    )

    assert emissions == []
    assert editor.limits() == [
        {
            "kind": "upper",
            "points": [
                {"freq": 1000.0, "limit": 100.0},
                {"freq": 2000.0, "limit": 90.0},
            ],
        }
    ]


def test_limit_table_editor_delete_buttons_keep_danger_marker_after_renumbering(qapp):
    editor = LimitTableEditor(object_name="fft_limit_table_editor", label="FFT")
    editor.set_limits(
        [
            {
                "kind": "upper",
                "points": [
                    {"freq": 1000.0, "limit": 100.0},
                    {"freq": 2000.0, "limit": 90.0},
                ],
            },
            {
                "kind": "lower",
                "points": [
                    {"freq": 1000.0, "limit": 0.0},
                    {"freq": 2000.0, "limit": 10.0},
                ],
            },
        ]
    )

    editor.findChild(QPushButton, "fft_delete_limit_table_0").click()
    delete_table_button = editor.findChild(
        QPushButton,
        "fft_delete_limit_table_0",
    )
    delete_point_button = editor.findChild(
        QPushButton,
        "fft_delete_limit_point_0",
    )

    assert delete_table_button is not None
    assert delete_point_button is not None
    assert delete_table_button.objectName() == "fft_delete_limit_table_0"
    assert delete_point_button.objectName() == "fft_delete_limit_point_0"
    assert delete_table_button.property("danger") is True
    assert delete_point_button.property("danger") is True
    assert "#ef4444" in delete_table_button.styleSheet()
    assert "#ef4444" in delete_point_button.styleSheet()


def test_limit_table_editor_empty_table_is_omitted_from_collected_payload(qapp):
    module = _make_module()
    module._current_config_id = 7
    module._populate_detail(_detail_payload([_channel_payload(0)]))
    editor = module._channel_containers[0].findChild(
        LimitTableEditor,
        "fft_limit_table_editor",
    )
    editor.findChild(QPushButton, "fft_add_limit_table").click()
    table = editor.findChild(QTableWidget, "fft_limit_points_0")
    table.selectAll()
    editor.findChild(QPushButton, "fft_delete_limit_point_0").click()

    assert editor.limits() == []
    assert module._collect_data()["channels"][0]["fft_band_limits"] == []


def test_limit_table_editor_cell_changes_are_tracked_as_unsaved_changes(qapp):
    loaded = {
        7: _detail_payload(
            [
                _channel_payload(
                    0,
                    fft_band_limits=[
                        {
                            "kind": "upper",
                            "points": [
                                {"freq": 1000.0, "limit": 100.0},
                                {"freq": 2000.0, "limit": 90.0},
                            ],
                        }
                    ],
                )
            ],
            config_id=7,
            config_name="Cfg1",
        ),
    }
    module = _make_module(config_service=_ConfigServiceStub(loaded_configs=loaded))
    qapp.processEvents()
    editor = module._channel_containers[0].findChild(
        LimitTableEditor,
        "fft_limit_table_editor",
    )
    table = editor.findChild(QTableWidget, "fft_limit_points_0")

    assert module._has_unsaved_changes() is False

    table.item(1, 0).setText("99")

    assert module._has_unsaved_changes() is True


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


def test_selection_change_discards_unsaved_staged_files(qapp, tmp_path, monkeypatch):
    config_service = _ConfigServiceStub(
        loaded_configs={
            7: _detail_payload([_channel_payload(0)], config_id=7, config_name="Cfg1"),
            8: _detail_payload([_channel_payload(0)], config_id=8, config_name="Cfg2"),
        }
    )
    module = _make_module(config_service=config_service)
    monkeypatch.setattr(module, "_prompt_unsaved_config_changes", lambda _action: True)

    import model.base_mode as base_mod

    monkeypatch.setattr(base_mod, "db", SimpleNamespace(atomic=lambda: nullcontext()))
    monkeypatch.setattr("ui.modules.config.QMessageBox.information", lambda *args: None)

    staged_path = tmp_path / "staged.wav"
    staged_path.write_bytes(b"wav")
    module._golden_editor_state[0] = _staged_golden_binding(staged_path)

    module.config_list.setCurrentRow(1)
    qapp.processEvents()

    assert module._current_config_id == 8
    assert module.ed_name.text() == "Cfg2"
    assert module._golden_editor_state == {}
    assert config_service.golden_sample_service.discarded_paths == [str(staged_path)]
    assert config_service.begin_calls == 1


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
    managed_input = _create_valid_managed_input()

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=managed_input.hardware_id,
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
    payload["input_hardware_device_id"] = managed_input.hardware_id
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
    managed_input = _create_valid_managed_input()
    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=managed_input.hardware_id,
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
        hardware_service=_ManagedHardwareServiceStub(),
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
    managed_input = _create_valid_managed_input()
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_ManagedHardwareServiceStub(),
        bus=bus,
    )

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=managed_input.hardware_id,
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
    managed_input = _create_valid_managed_input()
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_ManagedHardwareServiceStub(),
        bus=bus,
    )

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=managed_input.hardware_id,
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
    managed_input = _create_valid_managed_input()
    module = _make_module(
        config_service=config_service,
        ai_service=_AiServiceStub(),
        hardware_service=_ManagedHardwareServiceStub(),
        bus=bus,
    )

    config_id = ConfigTable.create(
        config_name="Cfg1",
        input_hardware_device_id=managed_input.hardware_id,
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
