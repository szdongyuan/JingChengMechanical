from __future__ import annotations

import threading
import time

import pytest

from app.service.current_meter_service import (
    CurrentMeterCollector,
    CurrentSample,
    build_current_trace_payload,
    current_trace_display_at_time,
    decode_current_registers,
    normalize_current_meter_config,
    validate_current_meter_config,
)


class _ReadResult:
    def __init__(self, registers):
        self.registers = registers

    def isError(self) -> bool:
        return False


class _ClientStub:
    def __init__(self, registers):
        self.registers = registers
        self.calls = []
        self.input_calls = []
        self.holding_calls = []

    def read_holding_registers(self, address, *, count, slave):
        call = (address, count, slave)
        self.calls.append(call)
        self.holding_calls.append(call)
        return _ReadResult(self.registers)

    def read_input_registers(self, address, *, count, slave):
        call = (address, count, slave)
        self.calls.append(call)
        self.input_calls.append(call)
        return _ReadResult(self.registers)

    def last_call(self):
        return self.calls[-1] if self.calls else None


class _SnapshotDetectingSamples(list):
    def __init__(self):
        super().__init__()
        self.snapshot_requested = False

    def __iter__(self):
        self.snapshot_requested = True
        return super().__iter__()


class _UnitOnlyClientStub:
    def __init__(self, registers, *, fail_with_unit: Exception | None = None):
        self.registers = registers
        self.calls = []
        self.fail_with_unit = fail_with_unit

    def read_input_registers(self, address, *, count, unit):
        call = (address, count, unit)
        self.calls.append(call)
        if self.fail_with_unit is None:
            return _ReadResult(self.registers)
        raise self.fail_with_unit


class _DeviceIdOnlyClientStub:
    def __init__(self, registers):
        self.registers = registers
        self.calls = []

    def read_input_registers(self, address, *, count, device_id):
        call = (address, count, device_id)
        self.calls.append(call)
        return _ReadResult(self.registers)


class _BlockingReadClientStub:
    def __init__(self, registers):
        self.registers = registers
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.close_called = threading.Event()

    def connect(self):
        return True

    def read_input_registers(self, address, *, count, slave):
        self.read_started.set()
        if not self.release_read.wait(timeout=5.0):
            raise TimeoutError("blocked read was not released")
        return _ReadResult(self.registers)

    def close(self):
        self.close_called.set()


def test_normalize_current_meter_config_defaults_and_validation():
    defaults = normalize_current_meter_config(None)

    assert defaults["enabled"] is False
    assert defaults["host"] == ""
    assert defaults["port"] == 502
    assert defaults["unit_id"] == 1
    assert defaults["register_type"] == "input"
    assert defaults["register_address"] == 0
    assert defaults["register_count"] == 1
    assert defaults["data_type"] == "uint16"
    assert defaults["byte_order"] == "big"
    assert defaults["word_order"] == "big"
    assert defaults["scale"] == pytest.approx(0.001)
    assert defaults["offset"] == pytest.approx(0.0)
    assert validate_current_meter_config(None) == []
    assert validate_current_meter_config({"enabled": True}) == [
        "电流表 IP/主机不能为空"
    ]


def test_normalize_current_meter_config_preserves_non_default_values():
    explicit = normalize_current_meter_config(
        {
            "port": 1502,
            "unit_id": 7,
            "register_type": "holding",
            "register_address": 8,
            "register_count": 2,
            "data_type": "float32",
            "byte_order": "little",
            "word_order": "little",
            "scale": 1.0,
            "offset": 1.5,
        }
    )

    assert explicit["register_type"] == "holding"
    assert explicit["register_count"] == 2
    assert explicit["data_type"] == "float32"
    assert explicit["byte_order"] == "little"
    assert explicit["word_order"] == "little"
    assert explicit["scale"] == pytest.approx(1.0)
    assert explicit["offset"] == pytest.approx(1.5)


def test_decode_current_registers_supports_float32_with_scale_and_offset():
    value = decode_current_registers(
        [0x4148, 0x0000],
        {
            "data_type": "float32",
            "byte_order": "big",
            "word_order": "big",
            "scale": 0.1,
            "offset": 1.0,
        },
    )

    assert value == pytest.approx(2.25)


@pytest.mark.parametrize(
    ("byte_order", "word_order", "registers"),
    [
        ("big", "big", [0x1234, 0x5678]),
        ("little", "big", [0x3412, 0x7856]),
        ("big", "little", [0x5678, 0x1234]),
        ("little", "little", [0x7856, 0x3412]),
    ],
)
def test_decode_current_registers_uint32_applies_byte_and_word_order_once(
    byte_order,
    word_order,
    registers,
):
    value = decode_current_registers(
        registers,
        {
            "data_type": "uint32",
            "byte_order": byte_order,
            "word_order": word_order,
            "scale": 1.0,
        },
    )

    assert value == pytest.approx(0x12345678)


@pytest.mark.parametrize(
    ("byte_order", "word_order", "registers"),
    [
        ("big", "big", [0x3F80, 0x0000]),
        ("little", "big", [0x803F, 0x0000]),
        ("big", "little", [0x0000, 0x3F80]),
        ("little", "little", [0x0000, 0x803F]),
    ],
)
def test_decode_current_registers_float32_applies_byte_and_word_order_once(
    byte_order,
    word_order,
    registers,
):
    value = decode_current_registers(
        registers,
        {
            "data_type": "float32",
            "byte_order": byte_order,
            "word_order": word_order,
            "scale": 1.0,
        },
    )

    assert value == pytest.approx(1.0)


def test_decode_current_registers_uint16_default_scale_supports_066a():
    assert decode_current_registers([0x066A], None) == pytest.approx(1.642)


def test_decode_current_registers_supports_signed_and_word_order():
    signed = decode_current_registers(
        [0xFFEC],
        {"data_type": "int16", "scale": 0.5, "offset": 2.0},
    )
    reordered = decode_current_registers(
        [0x5678, 0x1234],
        {"data_type": "uint32", "byte_order": "big", "word_order": "little", "scale": 1.0},
    )

    assert signed == pytest.approx(-8.0)
    assert reordered == pytest.approx(0x12345678)


def test_build_current_trace_payload_sorts_and_clips_analysis_window():
    payload = build_current_trace_payload(
        [
            {"time_sec": 2.0, "current_a": 3.5},
            CurrentSample(time_sec=1.0, current_a=1.5),
            {"time_sec": 3.0, "current_a": 10.0},
            {"time_sec": "bad", "current_a": 5.0},
        ],
        analysis_window=(0.5, 2.5),
    )

    assert payload == {
        "times_sec": [1.0, 2.0],
        "values_a": [1.5, 3.5],
        "average_a": 2.5,
        "max_a": 3.5,
        "latest_a": 3.5,
        "unit": "mA",
    }


def test_current_trace_display_at_time_interpolates_between_samples():
    payload = {
        "times_sec": [0.0, 1.0, 2.0],
        "values_a": [1.0, 2.0, 4.0],
        "average_a": 2.333333,
        "max_a": 4.0,
        "latest_a": 4.0,
        "unit": "mA",
    }

    assert current_trace_display_at_time(payload, 1.5) == "3.000 mA"


def test_current_trace_display_at_time_returns_exact_sample_value():
    payload = {
        "times_sec": [0.0, 1.0, 2.0],
        "values_a": [1.0, 2.0, 4.0],
        "unit": "mA",
    }

    assert current_trace_display_at_time(payload, 0.0) == "1.000 mA"


def test_current_trace_display_at_time_returns_none_outside_range():
    payload = {
        "times_sec": [0.0, 1.0, 2.0],
        "values_a": [1.0, 2.0, 4.0],
        "unit": "mA",
    }

    assert current_trace_display_at_time(payload, -0.1) is None
    assert current_trace_display_at_time(payload, 2.1) is None


@pytest.mark.parametrize(
    ("payload", "target_time"),
    [
        (None, 1.0),
        ([], 1.0),
        ({}, 1.0),
        ({"times_sec": [0.0], "values_a": ["bad"]}, 0.0),
        ({"times_sec": [0.0], "values_a": [1.0]}, "bad"),
    ],
)
def test_current_trace_display_at_time_returns_none_for_invalid_inputs(
    payload,
    target_time,
):
    assert current_trace_display_at_time(payload, target_time) is None


def test_current_trace_display_at_time_single_sample_requires_exact_match():
    payload = {
        "times_sec": [1.0],
        "values_a": [2.0],
        "unit": "mA",
    }

    assert current_trace_display_at_time(payload, 1.0) == "2.000 mA"
    assert current_trace_display_at_time(payload, 1.5) is None


def test_current_trace_display_at_time_omits_empty_unit():
    payload = {
        "times_sec": [0.0],
        "values_a": [1.0],
        "unit": " ",
    }

    assert current_trace_display_at_time(payload, 0.0) == "1.000"


def test_build_current_trace_payload_includes_default_lingli_current_unit():
    payload = build_current_trace_payload(
        [CurrentSample(time_sec=0.0, current_a=1.25)],
        config={},
    )

    assert payload is not None
    assert payload["unit"] == "mA"


def test_build_current_trace_payload_includes_signed_lingli_current_unit():
    payload = build_current_trace_payload(
        [CurrentSample(time_sec=0.0, current_a=-1.25)],
        config={"data_type": "int16", "scale": 0.001},
    )

    assert payload is not None
    assert payload["unit"] == "mA"


def test_build_current_trace_payload_omits_unit_for_generic_float32_config():
    payload = build_current_trace_payload(
        [CurrentSample(time_sec=0.0, current_a=1.25)],
        config={"data_type": "float32", "scale": 1.0},
    )

    assert payload is not None
    assert "unit" not in payload


def test_current_meter_collector_poll_once_records_decoded_current():
    client = _ClientStub([0x4148, 0x0000])
    collector = CurrentMeterCollector(
        {
            "enabled": True,
            "host": "192.168.1.20",
            "register_address": 8,
            "register_count": 2,
            "unit_id": 7,
            "data_type": "float32",
        },
        time_func=lambda: 1.25,
    )
    collector._start_time = 1.0

    collector._poll_once(client)

    samples = collector.samples()
    assert client.calls == [(8, 2, 7)]
    assert samples == [CurrentSample(time_sec=0.25, current_a=pytest.approx(0.0125))]


def test_current_meter_collector_sample_callback_receives_snapshot_after_storage():
    client = _ClientStub([0x066A])
    callback_calls = []
    collector = None

    def on_sample(sample, snapshot):
        callback_calls.append((sample, snapshot, collector.samples()))

    collector = CurrentMeterCollector(
        {"enabled": True, "host": "192.168.1.20"},
        time_func=lambda: 1.25,
        sample_callback=on_sample,
    )
    collector._start_time = 1.0

    collector._poll_once(client)

    samples = collector.samples()
    assert samples == [CurrentSample(time_sec=0.25, current_a=pytest.approx(1.642))]
    assert len(callback_calls) == 1
    sample, snapshot, samples_seen_inside_callback = callback_calls[0]
    assert sample == samples[0]
    assert isinstance(snapshot, tuple)
    assert snapshot == tuple(samples)
    assert samples_seen_inside_callback == samples


def test_current_meter_collector_without_sample_callback_does_not_snapshot_history():
    client = _ClientStub([0x066A])
    collector = CurrentMeterCollector(
        {"enabled": True, "host": "192.168.1.20"},
        time_func=lambda: 1.25,
    )
    samples = _SnapshotDetectingSamples()
    collector._samples = samples
    collector._start_time = 1.0

    collector._poll_once(client)

    assert len(samples) == 1
    assert samples[0] == CurrentSample(time_sec=0.25, current_a=pytest.approx(1.642))
    assert not samples.snapshot_requested


def test_current_meter_collector_sample_callback_error_is_logged_once_and_sampling_continues():
    client = _ClientStub([0x066A])
    times = iter([1.25, 1.5, 1.75])
    callback_invocations = 0
    logs = []

    def failing_callback(_sample, _snapshot):
        nonlocal callback_invocations
        callback_invocations += 1
        raise RuntimeError("callback boom")

    collector = CurrentMeterCollector(
        {"enabled": True, "host": "192.168.1.20"},
        time_func=lambda: next(times),
        log_callback=logs.append,
        sample_callback=failing_callback,
    )
    collector._start_time = 1.0

    collector._poll_once(client)
    collector._poll_once(client)
    collector._poll_once(client)

    assert collector.samples() == [
        CurrentSample(time_sec=0.25, current_a=pytest.approx(1.642)),
        CurrentSample(time_sec=0.5, current_a=pytest.approx(1.642)),
        CurrentSample(time_sec=0.75, current_a=pytest.approx(1.642)),
    ]
    assert callback_invocations == 3
    assert logs == ["电流表样本回调失败: callback boom"]


def test_current_meter_collector_poll_once_uses_input_register_read_by_default():
    client = _ClientStub([0x066A])
    collector = CurrentMeterCollector(
        {
            "enabled": True,
            "host": "192.168.1.20",
        },
        time_func=lambda: 1.25,
    )
    collector._start_time = 1.0

    collector._poll_once(client)

    assert client.calls == [(0, 1, 1)]
    assert client.input_calls == [(0, 1, 1)]
    assert client.holding_calls == []
    assert collector.samples() == [CurrentSample(time_sec=0.25, current_a=pytest.approx(1.642))]


def test_current_meter_collector_poll_once_records_from_holding_registers_with_keyword_count():
    client = _ClientStub([0x066A])
    collector = CurrentMeterCollector(
        {
            "enabled": True,
            "host": "192.168.1.20",
            "register_type": "holding",
        },
        time_func=lambda: 1.25,
    )
    collector._start_time = 1.0

    collector._poll_once(client)

    assert client.calls == [(0, 1, 1)]
    assert client.holding_calls == [(0, 1, 1)]
    assert client.input_calls == []
    assert collector.samples() == [CurrentSample(time_sec=0.25, current_a=pytest.approx(1.642))]


def test_current_meter_collector_poll_once_falls_back_to_unit_keyword_and_catches_unit_read_error():
    client = _UnitOnlyClientStub(
        [0x066A],
        fail_with_unit=RuntimeError("unit read failed"),
    )
    logs = []
    collector = CurrentMeterCollector(
        {
            "enabled": True,
            "host": "192.168.1.20",
        },
        time_func=lambda: 1.25,
        log_callback=logs.append,
    )
    collector._start_time = 1.0

    collector._poll_once(client)

    assert client.calls == [(0, 1, 1)]
    assert collector.samples() == []
    assert logs == ["电流表读取失败: unit read failed"]


def test_current_meter_collector_poll_once_supports_device_id_kwarg_read():
    client = _DeviceIdOnlyClientStub([0x066A])
    collector = CurrentMeterCollector(
        {
            "enabled": True,
            "host": "192.168.1.20",
        },
        time_func=lambda: 1.25,
    )
    collector._start_time = 1.0

    collector._poll_once(client)

    assert client.calls == [(0, 1, 1)]
    assert collector.samples() == [
        CurrentSample(time_sec=0.25, current_a=pytest.approx(1.642))
    ]


def test_current_meter_collector_stop_waits_for_in_flight_read_before_returning():
    client = _BlockingReadClientStub([0x066A])
    collector = CurrentMeterCollector(
        {
            "enabled": True,
            "host": "192.168.1.20",
            "poll_interval_ms": 20,
            "timeout_ms": 2000,
        },
        client_factory=lambda *args, **kwargs: client,
    )
    stop_entered = threading.Event()
    stop_done = threading.Event()
    stop_errors = []

    def stop_collector():
        stop_entered.set()
        try:
            collector.stop()
        except Exception as exc:  # pragma: no cover - surfaced by assertion below.
            stop_errors.append(exc)
        finally:
            stop_done.set()

    assert collector.start() is True
    assert client.read_started.wait(timeout=2.0)

    stopper = threading.Thread(target=stop_collector)
    stopper.start()
    try:
        assert stop_entered.wait(timeout=2.0)
        assert not stop_done.wait(timeout=1.15)
        assert collector.samples() == []

        client.release_read.set()
        stopper.join(timeout=2.0)

        assert stop_done.is_set()
        assert stop_errors == []
        samples_after_stop = collector.samples()
        assert len(samples_after_stop) == 1
        assert samples_after_stop[0].current_a == pytest.approx(1.642)
        assert client.close_called.is_set()

        time.sleep(0.05)
        assert collector.samples() == samples_after_stop
    finally:
        client.release_read.set()
        collector.stop(timeout=2.0)
        stopper.join(timeout=2.0)
