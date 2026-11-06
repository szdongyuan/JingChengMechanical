from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.service.analysis.test_manager import TestManager
from app.service.config_service import ConfigService
from app.service.hardwareselect_service import HardwareSelectService
from app.service.input_calibration_service import InputCalibrationService
from model.input_hardware_table import InputHardwareTable
from model.test_config_table import TestConfigTable as ConfigTable


DEVICES = [
    {
        "index": 7,
        "name": "Factory Interface",
        "hostapi": 0,
        "default_samplerate": 48000.0,
        "max_input_channels": 4,
        "max_output_channels": 0,
    }
]
HOSTAPIS = [{"name": "ASIO"}]


def test_adds_whole_input_device_with_per_channel_calibration():
    service = HardwareSelectService()

    hardware = service.add_input_hardware(
        7,
        "产线 A 声卡",
        devices=DEVICES,
        hostapis=HOSTAPIS,
    )

    assert hardware["alias"] == "产线 A 声卡"
    assert hardware["name"] == "Factory Interface"
    assert hardware["channel_count"] == 4
    assert list(hardware["mic_cal"]) == ["In1", "In2", "In3", "In4"]
    assert hardware["mic_cal"]["In4"] == {
        "deviation_value": "0",
        "binding_index": 3,
        "is_selected": 1,
    }


def test_rejects_duplicate_physical_device_and_duplicate_alias():
    service = HardwareSelectService()
    service.add_input_hardware(7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS)

    with pytest.raises(ValueError, match="已经添加"):
        service.add_input_hardware(
            7, "另一个别名", devices=DEVICES, hostapis=HOSTAPIS
        )

    second_devices = [dict(DEVICES[0], index=8, name="Backup Interface")]
    with pytest.raises(ValueError, match="别名"):
        service.add_input_hardware(
            8, "主声卡", devices=second_devices, hostapis=HOSTAPIS
        )


def test_renames_device_and_persists_calibration_per_hardware():
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "旧别名", devices=DEVICES, hostapis=HOSTAPIS
    )
    hardware_id = hardware["hardware_id"]
    mic_cal = hardware["mic_cal"]
    mic_cal["In2"]["deviation_value"] = "1.25"

    service.rename_input_hardware(hardware_id, "新别名")
    assert service.save_input_hardware_calibration(hardware_id, mic_cal) is True

    loaded = service.get_input_hardware(hardware_id)
    assert loaded["alias"] == "新别名"
    assert loaded["mic_cal"]["In2"]["deviation_value"] == "1.25"


def test_config_resolution_requires_managed_hardware_id_without_legacy_fallback():
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )

    assert service.load_input_device_for_config(
        {
            "input_device_name": "Factory Interface",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
        }
    ) == {}

    resolved = service.load_input_device_for_config(
        {"input_hardware_device_id": hardware["hardware_id"]}
    )
    assert resolved["input"]["name"] == "Factory Interface"
    assert resolved["input"]["channel_count"] == 4
    assert resolved["mic_cal"]["In1"]["binding_index"] == 0


def test_delete_is_blocked_while_config_references_hardware():
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )
    ConfigTable.create(
        config_name="Config A",
        input_hardware_device_id=hardware["hardware_id"],
    )

    with pytest.raises(ValueError, match="测试配置"):
        service.delete_input_hardware(hardware["hardware_id"])

    assert InputHardwareTable.select().count() == 1


def test_delete_removes_device_and_its_channel_calibration():
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )

    service.delete_input_hardware(hardware["hardware_id"])

    assert service.list_input_hardware() == []


def test_channel_calibration_updates_only_selected_hardware():
    service = HardwareSelectService()
    first = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )
    second_devices = [dict(DEVICES[0], index=8, name="Backup Interface")]
    second = service.add_input_hardware(
        8, "备用声卡", devices=second_devices, hostapis=HOSTAPIS
    )

    ok, value = InputCalibrationService().save_mic_cal_data(
        first["hardware_id"], "In2", "1.75 Pa/V"
    )

    assert (ok, value) == (True, "1.75")
    assert (
        service.get_input_hardware(first["hardware_id"])["mic_cal"]["In2"][
            "deviation_value"
        ]
        == "1.75"
    )
    assert (
        service.get_input_hardware(second["hardware_id"])["mic_cal"]["In2"][
            "deviation_value"
        ]
        == "0"
    )


def test_test_manager_resolves_the_device_bound_to_each_config():
    service = HardwareSelectService()
    first = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )
    second_devices = [dict(DEVICES[0], index=8, name="Backup Interface")]
    second = service.add_input_hardware(
        8, "备用声卡", devices=second_devices, hostapis=HOSTAPIS
    )
    manager = SimpleNamespace(
        _services=SimpleNamespace(hss_service=service)
    )

    resolved_first = TestManager._load_input_device_for_config(
        manager,
        {"input_hardware_device_id": first["hardware_id"]}
    )
    resolved_second = TestManager._load_input_device_for_config(
        manager,
        {"input_hardware_device_id": second["hardware_id"]}
    )

    assert resolved_first["input"]["name"] == "Factory Interface"
    assert resolved_second["input"]["name"] == "Backup Interface"


def test_config_load_exposes_managed_hardware_id():
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )
    config = ConfigTable.create(
        config_name="Config A",
        input_hardware_device_id=hardware["hardware_id"],
        input_device_name=hardware["name"],
        input_device_hostapi=hardware["hostapi"],
        input_device_samplerate=hardware["samplerate"],
    )

    loaded = ConfigService(SimpleNamespace()).load_config(config.config_id)

    assert loaded["input_hardware_device_id"] == hardware["hardware_id"]


def test_availability_check_reports_disconnected_managed_input():
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )

    error = service.input_hardware_availability_error(
        {"input_hardware_device_id": hardware["hardware_id"]},
        devices=[],
        hostapis=HOSTAPIS,
    )

    assert "主声卡" in error
    assert "未连接" in error


def test_availability_check_verifies_stream_settings(monkeypatch):
    service = HardwareSelectService()
    hardware = service.add_input_hardware(
        7, "主声卡", devices=DEVICES, hostapis=HOSTAPIS
    )
    calls = []
    monkeypatch.setattr(
        "app.service.hardwareselect_service.sd.check_input_settings",
        lambda **kwargs: calls.append(kwargs),
    )

    error = service.input_hardware_availability_error(
        {"input_hardware_device_id": hardware["hardware_id"]},
        devices=DEVICES,
        hostapis=HOSTAPIS,
    )

    assert error is None
    assert calls == [
        {
            "device": 7,
            "channels": 4,
            "samplerate": 48000.0,
            "dtype": "float32",
        }
    ]
