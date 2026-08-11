from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ui.modules.hardware import HardwareModule


class _HardwareService:
    def __init__(self) -> None:
        self.backend_resets = 0
        self.queries = 0

    def refresh_available_device(self) -> None:
        self.backend_resets += 1

    def query_devices_and_hostapis(self, *, refresh: bool = False):
        assert refresh is False
        self.queries += 1
        return [], []

    @staticmethod
    def list_input_hardware():
        return []


def test_hardware_page_startup_does_not_reset_active_audio_backend(qapp):
    hardware = _HardwareService()
    services = SimpleNamespace(
        hss_service=hardware,
        production_selection_locked=True,
        bus=SimpleNamespace(
            log=SimpleNamespace(emit=MagicMock()),
            refresh_channels=SimpleNamespace(emit=MagicMock()),
        ),
    )

    widget = HardwareModule(services)
    try:
        assert hardware.backend_resets == 0
        assert hardware.queries == 1
    finally:
        widget.close()
        widget.deleteLater()
        qapp.processEvents()


def test_hardware_refresh_does_not_reset_leased_audio_backend(qapp):
    hardware = _HardwareService()
    log = MagicMock()
    services = SimpleNamespace(
        hss_service=hardware,
        production_selection_locked=True,
        bus=SimpleNamespace(
            log=SimpleNamespace(emit=log),
            refresh_channels=SimpleNamespace(emit=MagicMock()),
        ),
    )

    widget = HardwareModule(services)
    try:
        widget.force_refresh_devices()
        assert hardware.backend_resets == 0
        assert hardware.queries == 2
        assert "常驻录音运行中" in log.call_args.args[0]
    finally:
        widget.close()
        widget.deleteLater()
        qapp.processEvents()
