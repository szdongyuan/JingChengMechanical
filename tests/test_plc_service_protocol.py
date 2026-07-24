from __future__ import annotations

import math

import pytest
import snap7.util

from app.service.plc_service import PlcService


class SignalStub:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class BusStub:
    def __init__(self):
        self.log = SignalStub()
        self.plc_workorder_changed = SignalStub()
        self.plc_current_sampled = SignalStub()


class FakeS7Client:
    def __init__(self, size=512):
        self.memory = bytearray(size)
        self.reads = []
        self.writes = []
        self.read_failures = {}

    def db_read(self, db_number, start, size):
        self.reads.append((db_number, start, size))
        failure = self.read_failures.get((start, size))
        if failure is not None:
            raise failure
        return bytearray(self.memory[start:start + size])

    def db_write(self, db_number, start, data):
        payload = bytes(data)
        self.writes.append((db_number, start, payload))
        self.memory[start:start + len(payload)] = payload


def _build_service(*, offsets=None, plc_id=22):
    bus = BusStub()
    client = FakeS7Client()
    service = PlcService(
        bus,
        lambda _plc_id: None,
        "127.0.0.1",
        db_number=1002,
        field_offsets_dict=offsets or {},
        plc_id=plc_id,
    )
    service.plc_client = client
    return service, client, bus


def _set_plc_string(client, offset, maximum_length, value):
    encoded = value.encode("ascii")
    client.memory[offset] = maximum_length
    client.memory[offset + 1] = len(encoded)
    client.memory[offset + 2:offset + 2 + len(encoded)] = encoded


def test_int_field_codec_reads_and_writes_signed_values():
    service, client, _bus = _build_service(
        offsets={
            "res_error_code_offset": "10.0",
            "rotation_speed_offset": "10.0",
        }
    )

    assert service.write_field_value("res_error_code_offset", -321) is True

    assert snap7.util.get_int(client.memory, 10) == -321
    assert service.read_field_value("rotation_speed_offset") == -321
    assert client.writes == [(1002, 10, bytes(client.memory[10:12]))]
    assert client.reads == [(1002, 10, 2)]


@pytest.mark.parametrize("value", [-32768, 32767])
def test_int_field_codec_supports_signed_boundaries(value):
    service, client, _bus = _build_service(
        offsets={"res_error_code_offset": "10.0"}
    )

    assert service.write_field_value("res_error_code_offset", value) is True

    assert snap7.util.get_int(client.memory, 10) == value


@pytest.mark.parametrize("value", [-32769, 32768, 1.5, "1", True, None])
def test_int_field_codec_rejects_invalid_values_before_writing(value):
    service, client, _bus = _build_service(
        offsets={"res_error_code_offset": "10.0"}
    )

    with pytest.raises((TypeError, ValueError)):
        service.write_field_value("res_error_code_offset", value)

    assert client.reads == []
    assert client.writes == []


def test_missing_optional_field_is_skipped_without_logging():
    service, client, bus = _build_service()

    assert service.read_field_value("rotation_speed_offset") is None
    assert service.write_field_value("res_error_code_offset", 0) is False

    assert client.reads == []
    assert client.writes == []
    assert bus.log.calls == []


def test_bool_and_real_field_codecs_use_metadata_types():
    service, client, _bus = _build_service(
        offsets={
            "res_result_offset": "4.3",
            "res_score_offset": "8.0",
            "current_value_offset": "8.0",
        }
    )

    assert service.write_field_value("res_result_offset", True) is True
    assert service.write_field_value("res_score_offset", 91.5) is True

    assert snap7.util.get_bool(client.memory, 4, 3) is True
    assert math.isclose(service.read_field_value("current_value_offset"), 91.5)


@pytest.mark.parametrize(
    ("maximum_length", "value"),
    [
        (15, "SN-0001"),
        (36, "MODEL-CUSTOMER-36"),
        (48, "CUSTOMER-DEFINED-LENGTH"),
    ],
)
def test_string_field_reads_dynamic_plc_length(maximum_length, value):
    service, client, _bus = _build_service(
        offsets={"sn_code_offset": "62.0"}
    )
    _set_plc_string(client, 62, maximum_length, value)

    assert service.read_field_value("sn_code_offset") == value
    assert client.reads == [
        (1002, 62, 2),
        (1002, 64, len(value)),
    ]


def test_string_field_with_zero_current_length_reads_only_header():
    service, client, _bus = _build_service(
        offsets={"sn_code_offset": "62.0"}
    )
    client.memory[62] = 20
    client.memory[63] = 0

    assert service.read_field_value("sn_code_offset") == ""
    assert client.reads == [(1002, 62, 2)]


@pytest.mark.parametrize(
    ("maximum_length", "current_length", "reason"),
    [
        (0, 0, "maximum length"),
        (5, 6, "current length"),
    ],
)
def test_invalid_string_header_is_logged_with_plc_field_and_reason(
    maximum_length,
    current_length,
    reason,
):
    service, client, bus = _build_service(
        offsets={"sn_code_offset": "62.0"}
    )
    client.memory[62] = maximum_length
    client.memory[63] = current_length

    assert service.read_plc_values("sn_code_offset") == {}

    message = bus.log.calls[0][0]
    assert "PLC 22" in message
    assert "sn_code_offset" in message
    assert reason in message


def test_string_field_write_uses_plc_maximum_length():
    service, client, _bus = _build_service(
        offsets={"res_sn_code_offset": "90.0"}
    )
    client.memory[90] = 15

    assert service.write_field_value("res_sn_code_offset", "SN-0001") is True

    assert client.reads == [(1002, 90, 2)]
    assert len(client.writes) == 1
    assert client.writes[0][0:2] == (1002, 90)
    assert len(client.writes[0][2]) == 17
    assert client.writes[0][2][0:2] == bytes([15, 7])
    assert client.writes[0][2][2:9] == b"SN-0001"


def test_string_field_write_rejects_text_longer_than_plc_maximum():
    service, client, _bus = _build_service(
        offsets={"res_sn_code_offset": "90.0"}
    )
    client.memory[90] = 5

    with pytest.raises(ValueError, match="maximum length"):
        service.write_field_value("res_sn_code_offset", "TOO-LONG")

    assert client.reads == [(1002, 90, 2)]
    assert client.writes == []


def test_string_field_write_rejects_invalid_maximum_without_data_write():
    service, client, _bus = _build_service(
        offsets={"res_sn_code_offset": "90.0"}
    )
    client.memory[90] = 0

    with pytest.raises(ValueError, match="maximum length"):
        service.write_field_value("res_sn_code_offset", "SN")

    assert client.reads == [(1002, 90, 2)]
    assert client.writes == []


def test_workorder_uses_protocol_metadata_for_sn_model_and_speed():
    service, client, bus = _build_service(
        offsets={
            "sn_code_offset": "62.0",
            "product_model_offset": "90.0",
            "rotation_speed_offset": "130.0",
        }
    )
    _set_plc_string(client, 62, 20, "SN-0001")
    _set_plc_string(client, 90, 20, "MODEL-A")
    snap7.util.set_int(client.memory, 130, 1450)

    service._emit_plc_workorder()

    assert bus.plc_workorder_changed.calls == [
        (
            {
                "sn": "SN-0001",
                "model": "MODEL-A",
                "rotation_speed": 1450,
            },
        )
    ]


def test_failed_optional_workorder_read_does_not_block_other_fields():
    service, client, bus = _build_service(
        offsets={
            "sn_code_offset": "62.0",
            "product_model_offset": "90.0",
            "rotation_speed_offset": "130.0",
        }
    )
    client.read_failures[(62, 2)] = RuntimeError("address unavailable")
    _set_plc_string(client, 90, 20, "MODEL-A")
    snap7.util.set_int(client.memory, 130, 1450)

    service._emit_plc_workorder()

    assert bus.plc_workorder_changed.calls == [
        ({"model": "MODEL-A", "rotation_speed": 1450},)
    ]
    message = bus.log.calls[0][0]
    assert "PLC 22" in message
    assert "sn_code_offset" in message
    assert "address unavailable" in message


def test_absent_workorder_fields_do_not_emit_empty_payload_or_warning():
    service, client, bus = _build_service()

    service._emit_plc_workorder()

    assert client.reads == []
    assert bus.plc_workorder_changed.calls == []
    assert bus.log.calls == []
