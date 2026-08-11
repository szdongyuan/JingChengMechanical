from __future__ import annotations

import math
import threading
import time
from dataclasses import FrozenInstanceError
from types import MethodType

import pytest
import snap7.util

from app.service.factory_mode_controller import FactoryModeController
from app.service.plc_protocol import (
    PLC_FIELD_BY_KEY,
    PLC_NEW_RECORD_DEFAULT_OFFSETS,
)
from app.service.plc_result_sequencer import PlcResultBoundaryError
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


COMPLETE_PLC_RESULT = {
    "finished": True,
    "error_code": 3,
    "sn_code": "SN-0001",
    "test_result": False,
    "score": 91.5,
    "spl": 62.25,
    "loudness": 4.5,
    "vibrate": 0.125,
    "ai_score": 88.75,
}


COMPLETE_PLC_RESULT_OFFSETS = {
    "res_finish_signal_offset": "8.1",
    "res_error_code_offset": "10.0",
    "res_sn_code_offset": "40.0",
    "res_result_offset": "12.0",
    "res_score_offset": "14.0",
    "res_spl_offset": "18.0",
    "res_loudness_offset": "22.0",
    "res_vibrate_data_offset": "26.0",
    "res_ai_score_offset": "30.0",
}


def _build_service(
    *,
    offsets=None,
    plc_id=22,
    poll_interval_ms=50,
    auto_reset_bit=True,
):
    bus = BusStub()
    client = FakeS7Client()
    service = PlcService(
        bus,
        lambda _plc_id: None,
        "127.0.0.1",
        db_number=1002,
        poll_interval_ms=poll_interval_ms,
        auto_reset_bit=auto_reset_bit,
        field_offsets_dict=offsets or {},
        plc_id=plc_id,
    )
    service.plc_client = client
    return service, client, bus


def test_plc_service_default_poll_interval_is_50_ms():
    bus = BusStub()

    service = PlcService(bus, lambda *_args: None, "127.0.0.1")

    assert service._poll_interval_ms == 50


def test_ingress_polling_stop_preserves_connection_and_full_stop_disconnects_once():
    service, client, _bus = _build_service()

    class Timer:
        def __init__(self):
            self.stop_calls = 0
            self.delete_calls = 0

        def stop(self):
            self.stop_calls += 1

        def deleteLater(self):
            self.delete_calls += 1

    poll_timer = Timer()
    value_timer = Timer()
    service.timer = poll_timer
    service.value_timer = value_timer

    service.stop_ingress_polling()

    assert service.plc_client is client
    assert client.disconnect_calls == 0
    assert poll_timer.stop_calls == poll_timer.delete_calls == 1
    assert value_timer.stop_calls == value_timer.delete_calls == 1
    assert service.timer is None
    assert service.value_timer is None

    service.stop_plc_service()
    service.stop_plc_service()

    assert client.disconnect_calls == 1
    assert service.plc_client is None


def test_isolated_result_dispatch_does_not_block_plc_owner_thread():
    entered = threading.Event()
    release = threading.Event()
    executed_thread_ids = []

    class Boundary:
        def write_result(self, _plc_id, _payload):
            executed_thread_ids.append(threading.get_ident())
            entered.set()
            assert release.wait(timeout=2)

        def _disconnect(self):
            return None

    class Command:
        def execute(self, boundary):
            boundary.write_result(22, COMPLETE_PLC_RESULT)

        def fail_if_pending(self, _reason):
            pytest.fail("isolated writer rejected an accepted command")

    service = PlcService(
        BusStub(),
        lambda *_args: None,
        "127.0.0.1",
        plc_id=22,
        isolate_result_io=True,
        output_boundary_factory=Boundary,
    )
    service._start_isolated_result_io()
    owner_thread_id = threading.get_ident()

    started = time.perf_counter()
    service._execute_plc_dispatch(Command())
    elapsed = time.perf_counter() - started

    try:
        assert elapsed < 0.1
        assert entered.wait(timeout=2)
        assert executed_thread_ids != [owner_thread_id]
    finally:
        release.set()
        service.disconnect_plc_service()


@pytest.mark.parametrize(
    "poll_interval_ms",
    [20, 50, 2_147_483_647],
)
def test_plc_service_accepts_exact_qtimer_safe_poll_intervals(
    poll_interval_ms,
):
    service, _client, _bus = _build_service(
        poll_interval_ms=poll_interval_ms,
    )

    assert service._poll_interval_ms == poll_interval_ms


@pytest.mark.parametrize(
    "poll_interval_ms",
    [
        19,
        2_147_483_648,
        True,
        None,
        "20",
        20.0,
        object(),
    ],
)
def test_plc_service_rejects_invalid_poll_interval_before_timer_use(
    poll_interval_ms,
    monkeypatch,
):
    timer_constructions = []
    monkeypatch.setattr(
        plc_service_module,
        "QTimer",
        lambda *_args: timer_constructions.append(True),
    )

    with pytest.raises(
        ValueError,
        match=r"20.*2[,_]?147[,_]?483[,_]?647",
    ):
        PlcService(
            BusStub(),
            lambda *_args: None,
            "127.0.0.1",
            poll_interval_ms=poll_interval_ms,
        )

    assert timer_constructions == []


@pytest.mark.parametrize(
    "poll_interval_ms",
    [20, 50, 2_147_483_647],
)
def test_plc_service_forwards_exact_poll_interval_to_single_timer(
    poll_interval_ms,
    monkeypatch,
):
    class FakeTimeout:
        def __init__(self):
            self.callback = None

        def connect(self, callback):
            self.callback = callback

    class FakeTimer:
        def __init__(self, _parent):
            self.intervals = []
            self.start_calls = 0
            self.timeout = FakeTimeout()

        def setInterval(self, interval):
            self.intervals.append(interval)

        def start(self):
            self.start_calls += 1

    fake_timer = FakeTimer(None)
    monkeypatch.setattr(
        plc_service_module,
        "QTimer",
        lambda _parent: fake_timer,
    )
    service, _client, _bus = _build_service(
        poll_interval_ms=poll_interval_ms,
    )

    service.start_plc_service()

    assert fake_timer.intervals == [poll_interval_ms]
    assert fake_timer.start_calls == 1
    assert fake_timer.timeout.callback == service.poll_once


@pytest.mark.parametrize("poll_interval_ms", [50, 20])
def test_supported_poll_intervals_trigger_once_only_on_zero_to_one(
    poll_interval_ms,
):
    service, client, _bus = _build_service(
        offsets={"start_signal_offset": "0.0"},
        poll_interval_ms=poll_interval_ms,
        auto_reset_bit=False,
    )
    starts = []
    service._on_start_requested = (
        lambda plc_id, workorder, timestamp: starts.append(
            (plc_id, workorder, timestamp)
        )
    )

    snap7.util.set_bool(client.memory, 0, 0, False)
    service.poll_once()
    snap7.util.set_bool(client.memory, 0, 0, True)
    service.poll_once()
    service.poll_once()

    assert len(starts) == 1
    assert starts[0][:2] == (22, {})
    assert type(starts[0][2]) is int


def test_rising_edge_samples_once_before_workorder_and_forwards_timestamp():
    events = []
    samples = iter((987_654_321,))

    def monotonic_ns():
        events.append("sample")
        return next(samples)

    service, client, _bus = _build_service(
        offsets={"start_signal_offset": "0.0"},
        auto_reset_bit=False,
    )
    service._monotonic_ns = monotonic_ns
    service._emit_plc_workorder = lambda: events.append("workorder") or {
        "sn": "SN-1"
    }
    service._on_start_requested = (
        lambda plc_id, workorder, trigger_monotonic_ns: events.append(
            ("callback", plc_id, workorder, trigger_monotonic_ns)
        )
    )

    snap7.util.set_bool(client.memory, 0, 0, False)
    service.poll_once()
    snap7.util.set_bool(client.memory, 0, 0, True)
    service.poll_once()

    assert events == [
        "sample",
        "workorder",
        ("callback", 22, {"sn": "SN-1"}, 987_654_321),
    ]


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
        lambda plc_id, workorder, timestamp: starts.append(
            (plc_id, workorder, timestamp)
        )
    )
    service._last_value = True

    service.request_start_signal_reset(22)
    snap7.util.set_bool(client.memory, 0, 0, True)
    service.poll_once()

    assert len(starts) == 1
    assert starts[0][:2] == (22, {})
    assert type(starts[0][2]) is int


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


@pytest.mark.parametrize(
    "field_name",
    [
        "res_error_code_offset",
        "res_sn_code_offset",
        "res_result_offset",
        "res_score_offset",
        "res_spl_offset",
        "res_loudness_offset",
        "res_vibrate_data_offset",
        "res_ai_score_offset",
        "res_finish_signal_offset",
    ],
)
def test_each_field_failure_raises_plc_result_boundary_error(field_name):
    service, _client, _bus = _build_service(
        offsets=COMPLETE_PLC_RESULT_OFFSETS
    )
    def fail_selected_field(candidate, value):
        if candidate == field_name:
            raise RuntimeError("backend detail must become a boundary error")
        return True

    service.write_field_value = fail_selected_field

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_result(22, COMPLETE_PLC_RESULT)

    assert failure.value.field_name == field_name


def test_write_testing_state_writes_matching_plc_bool_field():
    service, client, bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )

    service.write_testing_state(22, True)

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert len(client.writes) == 1
    assert bus.log.calls == []


def test_write_testing_state_rejects_different_plc_id():
    service, client, bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_testing_state(23, True)

    assert failure.value.field_name == "plc_id"
    assert client.reads == []
    assert client.writes == []
    assert bus.log.calls == []


def test_write_testing_state_rejects_missing_offset():
    service, client, bus = _build_service()

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_testing_state(22, True)

    assert failure.value.field_name == "res_testing_signal_offset"
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


def test_disconnected_testing_false_is_not_retained_for_reconnect(monkeypatch):
    service, client, _bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )
    snap7.util.set_bool(client.memory, 8, 2, True)
    service.plc_client = None
    monkeypatch.setattr(snap7.client, "Client", lambda: client)

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_testing_state(22, False)

    assert failure.value.field_name == "testing_dispatch"
    assert client.writes == []

    service._connect_once()

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert client.writes == []


def test_transient_testing_state_write_failure_is_not_retried_on_poll():
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

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_testing_state(22, True)
    service.poll_once()
    service.poll_once()

    assert failure.value.field_name == "res_testing_signal_offset"
    assert snap7.util.get_bool(client.memory, 8, 2) is False
    assert client.writes == []
    assert bus.log.calls == []


def test_disconnected_testing_attempts_do_not_supersede_or_replay(monkeypatch):
    service, client, _bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )
    snap7.util.set_bool(client.memory, 8, 2, True)
    service.plc_client = None
    monkeypatch.setattr(snap7.client, "Client", lambda: client)

    with pytest.raises(PlcResultBoundaryError):
        service.write_testing_state(22, True)
    with pytest.raises(PlcResultBoundaryError):
        service.write_testing_state(22, False)
    service._connect_once()

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert client.writes == []


def test_testing_state_has_no_retained_delivery_state():
    service, client, _bus = _build_service(
        offsets={"res_testing_signal_offset": "8.2"}
    )

    service.write_testing_state(22, True)

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert len(client.writes) == 1
    assert not hasattr(service, "_pending_testing_state")
    assert not hasattr(service, "_reconcile_testing_state")


def test_complete_physical_result_writes_exact_types_and_offsets():
    service, client, _bus = _build_service(
        offsets=COMPLETE_PLC_RESULT_OFFSETS
    )
    client.memory[40] = 20

    service.write_result(22, COMPLETE_PLC_RESULT)

    assert snap7.util.get_int(client.memory, 10) == 3
    assert client.memory[40:49] == bytes([20, 7]) + b"SN-0001"
    assert snap7.util.get_bool(client.memory, 12, 0) is False
    assert math.isclose(snap7.util.get_real(client.memory, 14), 91.5)
    assert math.isclose(snap7.util.get_real(client.memory, 18), 62.25)
    assert math.isclose(snap7.util.get_real(client.memory, 22), 4.5)
    assert math.isclose(snap7.util.get_real(client.memory, 26), 0.125)
    assert math.isclose(snap7.util.get_real(client.memory, 30), 88.75)
    assert snap7.util.get_bool(client.memory, 8, 1) is True
    assert [start for _db, start, _data in client.writes] == [
        10,
        40,
        12,
        14,
        18,
        22,
        26,
        30,
        8,
    ]


def test_default_result_offsets_skip_omitted_sn_and_write_finish_last():
    service, client, bus = _build_service(
        offsets=PLC_NEW_RECORD_DEFAULT_OFFSETS
    )

    service.write_result(22, COMPLETE_PLC_RESULT)

    assert [start for _db, start, _data in client.writes] == [
        10,
        12,
        14,
        18,
        22,
        26,
        30,
        8,
    ]
    assert snap7.util.get_bool(client.memory, 8, 1) is True
    assert bus.log.calls == []


def test_result_writer_does_not_write_testing_state():
    service, client, bus = _build_service(
        offsets={
            **COMPLETE_PLC_RESULT_OFFSETS,
            "res_testing_signal_offset": "8.2",
        },
    )
    client.memory[40] = 20
    snap7.util.set_bool(client.memory, 8, 2, True)

    service.write_result(22, COMPLETE_PLC_RESULT)

    assert snap7.util.get_bool(client.memory, 8, 2) is True
    assert len([write for write in client.writes if write[1] == 8]) == 1
    assert bus.log.calls == []


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("finished", 1),
        ("error_code", True),
        ("sn_code", 7),
        ("test_result", 1),
        ("score", 91),
        ("spl", float("nan")),
        ("loudness", float("inf")),
        ("vibrate", "0.1"),
        ("ai_score", False),
    ],
)
def test_result_writer_rejects_inexact_scalar_types(field_name, invalid_value):
    service, client, _bus = _build_service(
        offsets=COMPLETE_PLC_RESULT_OFFSETS
    )
    payload = dict(COMPLETE_PLC_RESULT)
    payload[field_name] = invalid_value

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_result(22, payload)

    assert failure.value.field_name == field_name
    assert client.writes == []


def test_result_writer_stops_before_finish_when_field_write_fails():
    service, client, bus = _build_service(
        offsets=COMPLETE_PLC_RESULT_OFFSETS
    )
    client.memory[40] = 20
    client.write_failures[(14, 4)] = [RuntimeError("score address unavailable")]

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_result(22, COMPLETE_PLC_RESULT)

    assert failure.value.field_name == "res_score_offset"
    assert [start for _db, start, _data in client.writes] == [10, 40, 12]
    assert snap7.util.get_bool(client.memory, 8, 1) is False
    assert bus.log.calls == []


@pytest.mark.parametrize("error_code", [32768, "0"])
def test_invalid_error_code_prevents_finish(error_code):
    service, client, bus = _build_service(
        offsets=COMPLETE_PLC_RESULT_OFFSETS
    )
    payload = dict(COMPLETE_PLC_RESULT)
    payload["error_code"] = error_code

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_result(22, payload)

    expected_field = (
        "error_code" if error_code == "0" else "res_error_code_offset"
    )
    assert failure.value.field_name == expected_field
    assert client.writes == []
    assert snap7.util.get_bool(client.memory, 8, 1) is False
    assert bus.log.calls == []


def test_result_writer_rejects_different_plc_id():
    service, client, bus = _build_service(
        offsets=COMPLETE_PLC_RESULT_OFFSETS
    )

    with pytest.raises(PlcResultBoundaryError) as failure:
        service.write_result(23, COMPLETE_PLC_RESULT)

    assert failure.value.field_name == "plc_id"
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
