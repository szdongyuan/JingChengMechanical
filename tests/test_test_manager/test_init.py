from __future__ import annotations

from typing import get_type_hints

from app.service.analysis.test_manager import TestManager

from ._support import (
    _mock_bus,
    _mock_services,
)


class TestTestManagerInit:
    def test_init(self):
        bus = _mock_bus()
        services = _mock_services(bus)
        mgr = TestManager(bus, services)
        assert mgr.is_running() is False


class TestTestManagerStartStop:
    def test_factory_runtime_abort_payload_helper_accepts_optional_error_message(self):
            hints = get_type_hints(TestManager._make_factory_runtime_abort_payload)

            assert hints["error_message"] == str | None
