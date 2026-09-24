from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from app.service.plc_service import PlcService


class _SignalStub:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _BusStub:
    def __init__(self):
        self.log = _SignalStub()


class _TimerStub:
    def __init__(self, trace, name):
        self._trace = trace
        self._name = name

    def stop(self):
        self._trace.append(f"{self._name}.stop")

    def deleteLater(self):
        self._trace.append(f"{self._name}.delete")


def _service():
    bus = _BusStub()
    service = PlcService(
        bus,
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict={"start_signal_offset": "0.0"},
        plc_id=7,
    )
    service.logger = MagicMock()
    return service, bus


def test_staged_shutdown_methods_are_qt_slots():
    meta = PlcService.staticMetaObject

    assert meta.indexOfMethod(b"stop_ingress_polling()") >= 0
    assert meta.indexOfMethod(b"disconnect_plc_service()") >= 0


def test_stop_ingress_polling_stops_timers_without_disconnecting():
    service, bus = _service()
    trace = []
    client = MagicMock()
    service.timer = _TimerStub(trace, "poll")
    service.value_timer = _TimerStub(trace, "value")
    service.plc_client = client
    service._last_value = True

    service.stop_ingress_polling()

    assert trace == [
        "poll.stop",
        "poll.delete",
        "value.stop",
        "value.delete",
    ]
    assert service.timer is None
    assert service.value_timer is None
    assert service._last_value is None
    assert service.plc_client is client
    client.disconnect.assert_not_called()
    assert bus.log.calls == []


def test_disconnect_stage_disconnects_and_reports_stopped():
    service, bus = _service()
    client = MagicMock()
    service.plc_client = client
    states = []
    service.state_changed.connect(states.append)

    service.disconnect_plc_service()

    client.disconnect.assert_called_once_with()
    assert service.plc_client is None
    assert states[-1] == "未连接"
    assert bus.log.calls == [("PLC7停止监听",)]
    service.logger.info.assert_called_with("PLC7停止监听")


def test_legacy_stop_composes_staged_shutdown_in_order(monkeypatch):
    service, _bus = _service()
    trace = []
    monkeypatch.setattr(
        service,
        "stop_ingress_polling",
        lambda: trace.append("ingress"),
    )
    monkeypatch.setattr(
        service,
        "disconnect_plc_service",
        lambda: trace.append("disconnect"),
    )

    service.stop_plc_service()

    assert trace == ["ingress", "disconnect"]


def test_staged_shutdown_is_idempotent_without_resources():
    service, _bus = _service()
    states = []
    service.state_changed.connect(states.append)

    service.stop_ingress_polling()
    service.stop_ingress_polling()
    service.disconnect_plc_service()
    service.disconnect_plc_service()

    assert service.timer is None
    assert service.value_timer is None
    assert service.plc_client is None
    assert service._last_value is None
    assert states[-1] == "未连接"


def test_disconnect_acknowledgement_records_failure_and_resets_next_invocation():
    service, _bus = _service()
    client = MagicMock()
    client.disconnect.side_effect = RuntimeError("disconnect failed")
    service.plc_client = client

    service.disconnect_plc_service()

    failed = service.disconnect_acknowledgement
    assert failed.success is False
    assert failed.detail == "disconnect failed"
    assert service.plc_client is None
    with pytest.raises(FrozenInstanceError):
        failed.success = True

    service.disconnect_plc_service()

    acknowledged = service.disconnect_acknowledgement
    assert acknowledged is not failed
    assert acknowledged.success is True
    assert acknowledged.detail is None
