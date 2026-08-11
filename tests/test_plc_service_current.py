from __future__ import annotations

import struct

from app.service.plc_service import PlcService


class _SignalStub:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _BusStub:
    def __init__(self):
        self.log = _SignalStub()
        self.plc_current_sampled = _SignalStub()
        self.plc_workorder_changed = _SignalStub()


class _PlcClientStub:
    def __init__(self, *, start_byte: int, current_offset: int | None, current_a: float = 0.0):
        self.start_byte = start_byte
        self.current_offset = current_offset
        self.current_a = current_a
        self.read_calls = []

    def db_read(self, db_number, byte_index, size):
        self.read_calls.append((db_number, byte_index, size))
        if byte_index == self.start_byte and size == 1:
            return bytes([1])
        if self.current_offset is not None and byte_index == self.current_offset and size == 4:
            return struct.pack(">f", self.current_a)
        raise RuntimeError("unexpected PLC read")


def _build_service(*, field_offsets, client, starts):
    bus = _BusStub()
    service = PlcService(
        bus,
        lambda plc_id, workorder, _trigger_ns: starts.append(
            (plc_id, workorder)
        ),
        "127.0.0.1",
        db_number=3,
        auto_reset_bit=False,
        field_offsets_dict=field_offsets,
        plc_id=7,
    )
    service.plc_client = client
    service._last_value = False
    return service, bus


def test_poll_once_reads_real_current_and_emits_zero_without_blocking_start(monkeypatch):
    starts = []
    client = _PlcClientStub(start_byte=8, current_offset=0, current_a=0.0)
    service, bus = _build_service(
        field_offsets={
            "start_signal_offset": "8.0",
            "current_value_offset": "0.0",
        },
        client=client,
        starts=starts,
    )
    monkeypatch.setattr("app.service.plc_service.time.monotonic", lambda: 123.5)

    service.poll_once()

    assert client.read_calls == [(3, 8, 1), (3, 0, 4)]
    assert bus.plc_current_sampled.calls == [(7, 123.5, 0.0)]
    assert starts == [(7, {})]


def test_poll_once_without_optional_current_field_keeps_start_signal_working():
    starts = []
    client = _PlcClientStub(start_byte=8, current_offset=None)
    service, bus = _build_service(
        field_offsets={"start_signal_offset": "8.0"},
        client=client,
        starts=starts,
    )

    service.poll_once()

    assert client.read_calls == [(3, 8, 1)]
    assert bus.plc_current_sampled.calls == []
    assert starts == [(7, {})]


def test_failed_optional_current_read_does_not_block_start_signal():
    starts = []
    client = _PlcClientStub(start_byte=8, current_offset=None)
    service, bus = _build_service(
        field_offsets={
            "start_signal_offset": "8.0",
            "current_value_offset": "4.0",
        },
        client=client,
        starts=starts,
    )

    service.poll_once()

    assert starts == [(7, {})]
    assert bus.plc_current_sampled.calls == []
    assert len(bus.log.calls) == 2
    assert "PLC 7" in bus.log.calls[0][0]
    assert "current_value_offset" in bus.log.calls[0][0]
    assert "unexpected PLC read" in bus.log.calls[0][0]
