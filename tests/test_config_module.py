import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ui.modules.config import ConfigModule


class _FakeBus(QObject):
    refresh_channels = pyqtSignal()
    status = pyqtSignal(str)


class _FakeConfigService:
    def list_configs(self):
        return []


class _FakeHardwareService:
    def query_devices_and_hostapis(self, refresh=False):
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
                    "hostapi": 0,
                    "default_samplerate": 44100,
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                },
            ],
            [{"name": "ASIO"}],
        )


class _FakeAiService:
    def list_enabled_toolchains(self):
        return []

    def get_toolchain(self, toolchain_id):
        return None

    def resolve_binding(self, config_id):
        return None


class _FakeServices:
    def __init__(self):
        self.bus = _FakeBus()
        self.config_service = _FakeConfigService()
        self.hss_service = _FakeHardwareService()
        self.ai_service = _FakeAiService()


def _make_module():
    app = QApplication.instance() or QApplication([])
    services = _FakeServices()
    module = ConfigModule(services)
    return app, module


def test_collect_data_includes_plc_metadata_fields():
    _, module = _make_module()
    module._current_config_id = 7
    module.ed_name.setText("Cfg1")
    module.txt_plc_device_key.setText("  PLC-A  ")
    module.txt_plc_device_label.setText("  Line 1  ")

    data = module._collect_data()

    assert set(data.keys()) == {
        "config_id",
        "config_name",
        "record_duration",
        "analysis_duration",
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
    }
    assert data["plc_device_key"] == "  PLC-A  "
    assert data["plc_device_label"] == "  Line 1  "


def test_populate_hardware_card_loads_plc_metadata_fields():
    _, module = _make_module()

    module._populate_hardware_card(
        {
            "plc_device_key": "PLC-A",
            "plc_device_label": "Line 1",
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "output_device_name": None,
            "output_device_hostapi": None,
            "output_device_samplerate": None,
            "selected_channels": [],
        }
    )

    assert module.txt_plc_device_key.text() == "PLC-A"
    assert module.txt_plc_device_label.text() == "Line 1"
    assert module.chk_scan is not None
    assert not hasattr(module, "combo_trigger")


def test_populate_hardware_card_handles_unbound_plc_metadata():
    _, module = _make_module()
    module.txt_plc_device_key.setText("stale")
    module.txt_plc_device_label.setText("stale")

    module._populate_hardware_card(
        {
            "plc_device_key": None,
            "plc_device_label": None,
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "output_device_name": None,
            "output_device_hostapi": None,
            "output_device_samplerate": None,
            "selected_channels": [],
        }
    )

    assert module.txt_plc_device_key.text() == ""
    assert module.txt_plc_device_label.text() == ""


def test_scan_checkbox_behavior_is_preserved_in_collect_and_populate():
    _, module = _make_module()
    module._current_config_id = 7
    module.chk_scan.setChecked(True)

    data = module._collect_data()

    assert data["scan_triggers_test"] is True

    module.chk_scan.setChecked(False)
    module._populate_detail(
        {
            "config_id": 7,
            "config_name": "Cfg1",
            "record_duration": 5.0,
            "analysis_duration": 3.0,
            "scan_triggers_test": True,
            "plc_device_key": None,
            "plc_device_label": None,
            "input_device_name": None,
            "input_device_hostapi": None,
            "input_device_samplerate": None,
            "output_device_name": None,
            "output_device_hostapi": None,
            "output_device_samplerate": None,
            "selected_channels": [],
            "channels": [],
        }
    )

    assert module.chk_scan.isChecked() is True


def test_hardware_card_still_round_trips_devices_and_selected_channels():
    _, module = _make_module()

    module._populate_hardware_card(
        {
            "plc_device_key": "PLC-A",
            "plc_device_label": "Line 1",
            "input_device_name": "Mic 1",
            "input_device_hostapi": "ASIO",
            "input_device_samplerate": 48000,
            "output_device_name": "Speaker 1",
            "output_device_hostapi": "ASIO",
            "output_device_samplerate": 44100,
            "selected_channels": [0, 1],
        }
    )

    data = module._collect_data()

    assert data["input_device_name"] == "Mic 1"
    assert data["input_device_hostapi"] == "ASIO"
    assert data["input_device_samplerate"] == 48000
    assert data["output_device_name"] == "Speaker 1"
    assert data["output_device_hostapi"] == "ASIO"
    assert data["output_device_samplerate"] == 44100
    assert data["selected_channels"] == [0, 1]
    assert data["plc_device_key"] == "PLC-A"
