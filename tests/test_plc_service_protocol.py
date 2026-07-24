from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from dataclasses import replace
from types import MethodType
from types import SimpleNamespace

import pytest
import snap7.util

from app.service.factory_mode_controller import FactoryModeController
from app.service.analysis.test_manager import TestManager
from app.service.plc_protocol import PLC_FIELD_BY_KEY
import app.service.plc_service as plc_service_module
from app.service.plc_service import PlcService
from app.services_container import EventBus, ServiceContainer
from tests.test_factory_mode_controller._support import (
    _accept_start,
    _make_config,
    _make_review_payload,
    _make_runtime_config_payload,
    _make_services,
    _run_id_for,
)


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
        self.plc_start_signal_reset_result = SignalStub()


class FakeS7Client:
    def __init__(self, size=512):
        self.memory = bytearray(size)
        self.reads = []
        self.writes = []
        self.read_failures = {}
        self.write_failures = {}
        self.connect_calls = []
        self.disconnect_calls = 0

    def connect(self, ip, rack, slot):
        self.connect_calls.append((ip, rack, slot))

    def disconnect(self):
        self.disconnect_calls += 1

    def db_read(self, db_number, start, size):
        self.reads.append((db_number, start, size))
        failure = self.read_failures.get((start, size))
        if failure is not None:
            raise failure
        return bytearray(self.memory[start:start + size])

    def db_write(self, db_number, start, data):
        payload = bytes(data)
        failures = self.write_failures.get((start, len(payload)))
        if failures:
            raise failures.pop(0)
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


def test_matching_start_signal_reset_writes_false_and_establishes_baseline():
    service, client, bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    snap7.util.set_bool(client.memory, 0, 0, True)
    service._last_value = True

    service.request_start_signal_reset(22)

    assert snap7.util.get_bool(client.memory, 0, 0) is False
    assert service._last_value is False
    assert service._start_signal_reset_pending is False
    assert bus.plc_start_signal_reset_result.calls[-1] == (22, True, "")
    assert any("启动信号已归位" in call[0] for call in bus.log.calls)


@pytest.mark.parametrize("plc_id", [23, True])
def test_start_signal_reset_ignores_foreign_and_boolean_plc_ids(plc_id):
    service, client, bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    snap7.util.set_bool(client.memory, 0, 0, True)

    service.request_start_signal_reset(plc_id)

    assert snap7.util.get_bool(client.memory, 0, 0) is True
    assert client.reads == []
    assert client.writes == []
    assert bus.plc_start_signal_reset_result.calls == []
    assert not any("启动信号" in call[0] for call in bus.log.calls)


def test_disconnected_start_signal_reset_remains_pending_without_acknowledgement():
    service, client, bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    service.plc_client = None

    service.request_start_signal_reset(22)

    assert service._start_signal_reset_pending is True
    assert client.reads == []
    assert client.writes == []
    assert bus.plc_start_signal_reset_result.calls == []


def test_connect_applies_retained_start_signal_reset(monkeypatch):
    service, client, bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    snap7.util.set_bool(client.memory, 0, 0, True)
    service.plc_client = None
    monkeypatch.setattr(snap7.client, "Client", lambda: client)

    service.request_start_signal_reset(22)
    service._connect_once()

    assert snap7.util.get_bool(client.memory, 0, 0) is False
    assert service._last_value is False
    assert service._start_signal_reset_pending is False
    assert bus.plc_start_signal_reset_result.calls == [(22, True, "")]


def test_failed_start_signal_reset_disconnects_and_retries_after_reconnect(
    monkeypatch,
):
    service, client, bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    snap7.util.set_bool(client.memory, 0, 0, True)
    client.write_failures[(0, 1)] = [RuntimeError("temporary reset failure")]

    service.request_start_signal_reset(22)

    assert service._start_signal_reset_pending is True
    assert service.plc_client is None
    assert client.disconnect_calls == 1
    assert bus.plc_start_signal_reset_result.calls[-1][0:2] == (22, False)
    assert "temporary reset failure" in bus.plc_start_signal_reset_result.calls[-1][2]
    assert any(
        "启动信号归位失败" in call[0] and "temporary reset failure" in call[0]
        for call in bus.log.calls
    )

    monkeypatch.setattr(snap7.client, "Client", lambda: client)
    service._connect_once()

    assert snap7.util.get_bool(client.memory, 0, 0) is False
    assert service._start_signal_reset_pending is False
    assert bus.plc_start_signal_reset_result.calls[-1] == (22, True, "")


@pytest.mark.parametrize(
    "offsets",
    [
        {},
        {"start_signal_offset": "invalid"},
    ],
)
def test_missing_or_invalid_start_signal_offset_retains_reset_request(offsets):
    service, client, bus = _build_service(offsets=offsets)

    service.request_start_signal_reset(22)

    assert service._start_signal_reset_pending is True
    assert service.plc_client is None
    assert client.disconnect_calls == 1
    assert bus.plc_start_signal_reset_result.calls[-1][0:2] == (22, False)
    assert "start_signal_offset" in bus.plc_start_signal_reset_result.calls[-1][2]
    assert any(
        "启动信号归位失败" in call[0] and "start_signal_offset" in call[0]
        for call in bus.log.calls
    )


def test_successful_start_signal_reset_makes_immediate_high_read_a_rising_edge():
    service, client, _bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    starts = []
    service._on_start_requested = (
        lambda plc_id, workorder: starts.append((plc_id, workorder))
    )
    service._last_value = True

    service.request_start_signal_reset(22)
    snap7.util.set_bool(client.memory, 0, 0, True)
    service.poll_once()

    assert starts == [(22, {})]


def test_first_high_read_without_reset_still_does_not_trigger_start():
    service, client, _bus = _build_service(
        offsets={"start_signal_offset": "0.0"}
    )
    starts = []
    service._on_start_requested = (
        lambda plc_id, workorder: starts.append((plc_id, workorder))
    )
    snap7.util.set_bool(client.memory, 0, 0, True)

    service.poll_once()

    assert service._last_value is True
    assert starts == []
    assert client.writes == []


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


def test_plc_rising_edges_deliver_fresh_atomic_workorder_snapshots_to_start():
    bus = EventBus()
    ingresses = []
    bus.factory_plc_event.connect(ingresses.append)
    bus.plc_workorder_changed.connect(
        lambda payload: payload.update(
            sn="UI-STALE",
            model="UI-STALE",
            rotation_speed=-1,
        )
    )
    services = _make_services((_make_config(101, plc_id=22),))
    _accept_start(services)
    services.bus = bus
    services._start_from_plc = MethodType(ServiceContainer._start_from_plc, services)
    controller = FactoryModeController(bus, services)
    controller.set_selection((101,))
    client = FakeS7Client()
    service = PlcService(
        bus,
        services._start_from_plc,
        "127.0.0.1",
        db_number=1002,
        field_offsets_dict={
            "start_signal_offset": "0.0",
            "sn_code_offset": "62.0",
            "product_model_offset": "90.0",
            "rotation_speed_offset": "130.0",
        },
        plc_id=22,
    )
    service.plc_client = client
    service._last_value = False
    _set_plc_string(client, 62, 20, "SN-0001")
    _set_plc_string(client, 90, 20, "MODEL-A")
    snap7.util.set_int(client.memory, 130, 1450)
    snap7.util.set_bool(client.memory, 0, 0, True)

    service.poll_once()

    first_start = services.test.start.call_args_list[0].args[0]
    assert first_start["sn"] == "SN-0001"
    assert first_start["model"] == "MODEL-A"
    assert first_start["rotation_speed"] == 1450
    assert type(first_start["rotation_speed"]) is int
    assert ingresses[0].sn == "SN-0001"
    assert ingresses[0].model == "MODEL-A"
    assert ingresses[0].rotation_speed == 1450
    with pytest.raises(FrozenInstanceError):
        ingresses[0].sn = "STALE"

    bus.factory_runtime_analyzing_entered.emit(
        _make_runtime_config_payload(config_id=101, run_id=_run_id_for(101))
    )
    bus.factory_runtime_auto_analysis_succeeded.emit(
        _make_review_payload(
            displayed_config_id=101,
            run_id=_run_id_for(101),
            trigger_source="plc",
        )
    )
    controller.clear_for_next_run()
    service.poll_once()
    _set_plc_string(client, 62, 20, "   ")
    client.read_failures[(90, 2)] = RuntimeError("model unavailable")
    snap7.util.set_int(client.memory, 130, 1500)
    snap7.util.set_bool(client.memory, 0, 0, True)

    service.poll_once()

    second_start = services.test.start.call_args_list[1].args[0]
    assert "sn" not in second_start
    assert "model" not in second_start
    assert second_start["rotation_speed"] == 1500
    assert ingresses[1].sn is None
    assert ingresses[1].model is None
    assert ingresses[1].rotation_speed == 1500


def test_write_testing_state_writes_matching_plc_bool_field():
    service, client, bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )

    service.write_testing_state(22, True)

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert len(client.writes) == 1
    assert bus.log.calls == []


def test_write_testing_state_ignores_different_plc_id():
    service, client, bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )

    service.write_testing_state(23, True)

    assert client.reads == []
    assert client.writes == []
    assert bus.log.calls == []


def test_write_testing_state_skips_missing_offset_without_warning():
    service, client, bus = _build_service()

    service.write_testing_state(22, True)

    assert client.reads == []
    assert client.writes == []
    assert bus.log.calls == []


def test_write_testing_state_repeated_false_writes_are_safe():
    service, client, bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )
    snap7.util.set_bool(client.memory, 8, 2, True)

    service.write_testing_state(22, False)
    service.write_testing_state(22, False)

    assert snap7.util.get_bool(client.memory, 8, 2) is False
    assert len(client.writes) == 2
    assert bus.log.calls == []


def test_disconnected_testing_false_is_replayed_after_reconnect(monkeypatch):
    service, client, _bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )
    snap7.util.set_bool(client.memory, 8, 2, True)
    service.plc_client = None
    monkeypatch.setattr(snap7.client, "Client", lambda: client)

    service.write_testing_state(22, False)

    assert client.writes == []

    service._connect_once()

    assert snap7.util.get_bool(client.memory, 8, 2) is False
    assert len(client.writes) == 1


def test_transient_testing_state_write_failure_retries_on_poll_without_log_spam():
    service, client, bus = _build_service(
        offsets={
            "start_signal_offset": "0.0",
            "res_testing_signal_offset": "8.2",
        }
    )
    client.write_failures[(8, 1)] = [
        RuntimeError("temporary write failure"),
        RuntimeError("temporary write failure"),
    ]

    service.write_testing_state(22, True)
    service.poll_once()
    service.poll_once()

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert len(client.writes) == 1
    testing_errors = [
        call[0]
        for call in bus.log.calls
        if "res_testing_signal_offset" in call[0]
    ]
    assert len(testing_errors) == 1
    assert "PLC 22" in testing_errors[0]
    assert "temporary write failure" in testing_errors[0]


def test_later_false_supersedes_undelivered_true(monkeypatch):
    service, client, _bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )
    snap7.util.set_bool(client.memory, 8, 2, True)
    service.plc_client = None
    monkeypatch.setattr(snap7.client, "Client", lambda: client)

    service.write_testing_state(22, True)
    service.write_testing_state(22, False)
    service._connect_once()

    assert snap7.util.get_bool(client.memory, 8, 2) is False
    assert len(client.writes) == 1


def test_testing_state_reconciliation_is_idempotent_after_success():
    service, client, _bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )

    service.write_testing_state(22, True)
    service._reconcile_testing_state()
    service._reconcile_testing_state()

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert len(client.writes) == 1


def test_test_manager_result_payload_writes_int_error_and_later_plc_fields():
    service, client, _bus = _build_service(
        offsets={
            "res_error_code_offset": "10.0",
            "res_result_offset": "12.0",
            "res_finish_signal_offset": "12.1",
            "res_score_offset": "14.0",
        }
    )
    session = SimpleNamespace(_workorder={"sn": "SN-0001"})
    review_payload = SimpleNamespace(
        summary_payload={
            "overall_status": "pass",
            "rows": [
                {
                    "item_key": "stft_score",
                    "actual_value": 91.5,
                }
            ],
        },
        judgment_payload={"channel_results": []},
        chart_payload={},
    )
    payload = TestManager._make_plc_test_result_payload(
        session,
        review_payload,
    )

    service.last_write_test_result_to_plc(22, payload)

    assert payload["error_code"] == 0
    assert type(payload["error_code"]) is int
    assert snap7.util.get_int(client.memory, 10) == 0
    assert snap7.util.get_bool(client.memory, 12, 0) is True
    assert math.isclose(snap7.util.get_real(client.memory, 14), 91.5)
    assert snap7.util.get_bool(client.memory, 12, 1) is True
    assert [start for _db, start, _data in client.writes] == [
        10,
        12,
        14,
        12,
    ]


def test_result_callback_writes_configured_fields_in_protocol_order():
    service, client, bus = _build_service(
        offsets={
            "res_testing_signal_offset": "8.2",
            "res_error_code_offset": "10.0",
            "res_sn_code_offset": "20.0",
            "res_result_offset": "40.0",
            "res_score_offset": "44.0",
            "res_finish_signal_offset": "48.0",
        }
    )
    client.memory[20] = 15

    service.last_write_test_result_to_plc(
        22,
        {
            "finished": True,
            "error_code": 0,
            "sn_code": "SN-0001",
            "test_result": True,
            "score": 91.5,
        },
    )

    assert [start for _db, start, _data in client.writes] == [
        8,
        10,
        20,
        40,
        44,
        48,
    ]
    assert snap7.util.get_bool(client.memory, 8, 2) is False
    assert snap7.util.get_int(client.memory, 10) == 0
    assert client.memory[20:29] == bytes([15, 7]) + b"SN-0001"
    assert snap7.util.get_bool(client.memory, 40, 0) is True
    assert math.isclose(snap7.util.get_real(client.memory, 44), 91.5)
    assert snap7.util.get_bool(client.memory, 48, 0) is True
    assert not any("offset" in call[0].lower() for call in bus.log.calls)


def test_result_callback_uses_protocol_result_payload_keys(monkeypatch):
    service, client, bus = _build_service(
        offsets={
            "res_score_offset": "44.0",
            "res_finish_signal_offset": "48.0",
        }
    )
    score_spec = replace(
        PLC_FIELD_BY_KEY["res_score_offset"],
        result_payload_key="metadata_score",
    )
    monkeypatch.setattr(
        plc_service_module,
        "PLC_RESULT_FIELDS",
        (score_spec,),
    )

    service.last_write_test_result_to_plc(
        22,
        {
            "finished": True,
            "metadata_score": 82.25,
            "score": "must not be used",
        },
    )

    assert math.isclose(snap7.util.get_real(client.memory, 44), 82.25)
    assert snap7.util.get_bool(client.memory, 48, 0) is True
    assert bus.log.calls


def test_result_callback_stops_before_finish_when_configured_write_fails():
    service, client, bus = _build_service(
        offsets={
            "res_testing_signal_offset": "8.2",
            "res_error_code_offset": "10.0",
            "res_score_offset": "44.0",
            "res_finish_signal_offset": "48.0",
        }
    )
    client.write_failures[(44, 4)] = [RuntimeError("score address unavailable")]

    service.last_write_test_result_to_plc(
        22,
        {
            "finished": True,
            "error_code": 0,
            "score": 91.5,
        },
    )

    assert [start for _db, start, _data in client.writes] == [8, 10]
    assert snap7.util.get_bool(client.memory, 48, 0) is False
    message = bus.log.calls[-1][0]
    assert "PLC 22" in message
    assert "res_score_offset" in message
    assert "score address unavailable" in message


@pytest.mark.parametrize("error_code", [32768, "0"])
def test_invalid_error_code_prevents_finish(error_code):
    service, client, bus = _build_service(
        offsets={
            "res_error_code_offset": "10.0",
            "res_finish_signal_offset": "48.0",
        }
    )

    service.last_write_test_result_to_plc(
        22,
        {
            "finished": True,
            "error_code": error_code,
        },
    )

    assert client.writes == []
    assert snap7.util.get_bool(client.memory, 48, 0) is False
    assert "res_error_code_offset" in bus.log.calls[-1][0]


def test_result_callback_ignores_different_plc_id():
    service, client, bus = _build_service(
        offsets={
            "res_error_code_offset": "10.0",
            "res_finish_signal_offset": "48.0",
        }
    )

    service.last_write_test_result_to_plc(
        23,
        {"finished": True, "error_code": 0},
    )

    assert client.reads == []
    assert client.writes == []
    assert bus.log.calls == []


def test_config_callback_writes_only_present_configured_metadata_fields():
    service, client, bus = _build_service(
        offsets={
            "res_spl_limit_up_offset": "60.0",
            "res_product_model_offset": "70.0",
        }
    )
    client.memory[70] = 12

    service.last_write_limit_to_plc(
        22,
        {
            "res_spl_limit_up_offset": 82.5,
            "res_spl_limit_down_offset": 55.0,
            "res_product_model_offset": "MODEL-A",
        },
    )

    assert [start for _db, start, _data in client.writes] == [60, 70]
    assert math.isclose(snap7.util.get_real(client.memory, 60), 82.5)
    assert client.memory[70:79] == bytes([12, 7]) + b"MODEL-A"
    assert len(client.writes[1][2]) == 14
    assert any("写入限值完成" in call[0] for call in bus.log.calls)
    assert not any("offset" in call[0].lower() for call in bus.log.calls)


def test_config_callback_failure_names_field_and_has_no_success_log():
    service, client, bus = _build_service(
        offsets={"res_spl_limit_up_offset": "60.0"}
    )

    service.last_write_limit_to_plc(
        22,
        {"res_spl_limit_up_offset": "not-a-real"},
    )

    assert client.writes == []
    assert any(
        "PLC 22" in call[0]
        and "res_spl_limit_up_offset" in call[0]
        and "not-a-real" in call[0]
        for call in bus.log.calls
    )
    assert not any("写入限值完成" in call[0] for call in bus.log.calls)
