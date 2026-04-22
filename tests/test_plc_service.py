from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from PyQt6.QtWidgets import QApplication

if "snap7" not in sys.modules:
    snap7_module = ModuleType("snap7")
    snap7_client_module = ModuleType("snap7.client")
    snap7_util_module = ModuleType("snap7.util")

    class _Client:
        def connect(self, *_args, **_kwargs):
            return None

        def disconnect(self):
            return None

    snap7_client_module.Client = _Client
    snap7_util_module.get_bool = lambda *_args, **_kwargs: False
    snap7_util_module.set_bool = lambda *_args, **_kwargs: None
    snap7_module.client = snap7_client_module
    snap7_module.util = snap7_util_module
    sys.modules["snap7"] = snap7_module
    sys.modules["snap7.client"] = snap7_client_module
    sys.modules["snap7.util"] = snap7_util_module

from app.service.plc_service import PlcService


class _SignalProbe:
    def __init__(self):
        self.messages = []

    def emit(self, message: str) -> None:
        self.messages.append(message)


class _BusStub:
    def __init__(self):
        self.log = _SignalProbe()


def _qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_plc_service_start_and_stop_create_and_clear_timer(monkeypatch):
    _qapp()
    bus = _BusStub()
    start_requests = []
    state_changes = []
    connect_results = []

    service = PlcService(
        bus=bus,
        on_start_requested=lambda: start_requests.append("start"),
        ip="127.0.0.1",
        rack=0,
        slot=1,
        db_number=1,
        byte_index=0,
        bit_index=0,
    )

    def _connect_once():
        service.plc_client = SimpleNamespace(disconnect=lambda: None)

    monkeypatch.setattr(service, "_connect_once", _connect_once)
    service.state_changed.connect(state_changes.append)
    service.connect_result.connect(lambda success, message: connect_results.append((success, message)))

    service.start_plc_service()

    assert service.timer is not None
    assert state_changes[:1] == ["连接中"]
    assert connect_results == [(True, "")]
    assert any("PLC开始监听" in message for message in bus.log.messages)

    service.stop_plc_service()

    assert service.timer is None
    assert service.plc_client is None
    assert state_changes[-1] == "未连接"
    assert bus.log.messages[-1] == "PLC 停止监听"
    assert start_requests == []


def test_plc_service_rejects_missing_numeric_fields_without_crashing():
    _qapp()
    bus = _BusStub()
    state_changes = []
    connect_results = []

    service = PlcService(
        bus=bus,
        on_start_requested=lambda: None,
        ip="127.0.0.1",
        rack=0,
        slot=1,
        db_number=None,
        byte_index=0,
        bit_index=0,
    )
    service.state_changed.connect(state_changes.append)
    service.connect_result.connect(
        lambda success, message: connect_results.append((success, message))
    )

    service.start_plc_service()

    assert service.timer is None
    assert service.plc_client is None
    assert state_changes == ["未连接"]
    assert connect_results
    assert connect_results[0][0] is False
    assert "配置不完整" in connect_results[0][1]
    assert "数据块号" in connect_results[0][1]
    assert any(
        "配置不完整" in message and "数据块号" in message
        for message in bus.log.messages
    )


def test_plc_service_invokes_captured_plc_start_hook_on_rising_edge(monkeypatch):
    _qapp()
    bus = _BusStub()
    start_requests = []
    reset_writes = []

    service = PlcService(
        bus=bus,
        on_start_requested=lambda plc_id=7: start_requests.append(plc_id),
        ip="127.0.0.1",
        rack=0,
        slot=1,
        db_number=1,
        byte_index=0,
        bit_index=0,
        plc_id=7,
    )
    service.plc_client = SimpleNamespace(disconnect=lambda: None)
    service._last_value = False

    monkeypatch.setattr(service, "_read_start_bit", lambda: True)
    monkeypatch.setattr(
        service,
        "_write_start_bit",
        lambda value: reset_writes.append(value) or True,
    )

    service.poll_once()

    assert start_requests == [7]
    assert reset_writes == [False]
    assert bus.log.messages[-1] == "PLC 7 检测到开始信号"
