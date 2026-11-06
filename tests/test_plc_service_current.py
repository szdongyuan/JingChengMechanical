from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest
from snap7.error import S7Error

import app.service.plc_service as plc_service_module
from app.service.plc_service import PlcService


class _SignalStub:
    def __init__(self, *, error: BaseException | None = None):
        self.calls = []
        self.error = error

    def emit(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error


class _BusStub:
    def __init__(self):
        self.log = _SignalStub()
        self.plc_current_sampled = _SignalStub()
        self.plc_workorder_changed = _SignalStub()


class _LoggerStub:
    def __init__(self):
        self.errors = []

    def info(self, _message):
        return None

    def warning(self, _message):
        return None

    def error(self, message):
        self.errors.append(message)


_DEFAULT_CLIENT = object()


def _build_service(
    *,
    bus=None,
    on_start_requested=None,
    client=_DEFAULT_CLIENT,
    field_offsets=None,
    plc_id=7,
    auto_reset_bit=False,
):
    if bus is None:
        bus = _BusStub()
    if on_start_requested is None:
        on_start_requested = lambda *_args: None
    if field_offsets is None:
        field_offsets = {
            "start_signal_offset": "0.0",
            "current_value_offset": 4,
        }
    service = PlcService(
        bus,
        on_start_requested,
        "127.0.0.1",
        db_number=3,
        auto_reset_bit=auto_reset_bit,
        field_offsets_dict=field_offsets,
        plc_id=plc_id,
    )
    service.plc_client = object() if client is _DEFAULT_CLIENT else client
    service.logger = _LoggerStub()
    return service, bus


@pytest.mark.parametrize("current_a", [0.0, 1.25, -0.5])
@pytest.mark.parametrize("offset", [0, "0", 12])
def test_poll_once_publishes_finite_plc_current_before_start_processing(
    monkeypatch,
    current_a,
    offset,
):
    service, bus = _build_service(
        field_offsets={
            "start_signal_offset": "0.0",
            "current_value_offset": offset,
        }
    )
    calls = []
    service._read_start_bit = lambda: calls.append("read_start") or False

    def read_offset_value(actual_offset, value_type):
        calls.append(("read_current", actual_offset, value_type))
        return current_a

    service.read_offset_value = read_offset_value
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: calls.append("sample_clock") or 123.5,
            monotonic_ns=lambda: pytest.fail("baseline poll must not sample trigger time"),
        ),
    )

    service.poll_once()

    assert calls == [
        "read_start",
        "sample_clock",
        ("read_current", offset, "real"),
    ]
    assert bus.plc_current_sampled.calls == [(7, 123.5, current_a)]


@pytest.mark.parametrize("offset", [None, "", "   "])
def test_unconfigured_current_offset_is_silent(monkeypatch, offset):
    service, bus = _build_service(
        field_offsets={
            "start_signal_offset": "0.0",
            "current_value_offset": offset,
        }
    )
    service._last_current_read_error = "existing error"
    service._read_start_bit = lambda: False
    service.read_offset_value = lambda *_args: pytest.fail(
        "unconfigured current must not be read"
    )
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: pytest.fail("unconfigured current must not sample time"),
            monotonic_ns=lambda: pytest.fail("baseline poll must not sample trigger time"),
        ),
    )

    service.poll_once()

    assert bus.plc_current_sampled.calls == []
    assert bus.log.calls == []
    assert service._last_current_read_error == "existing error"


@pytest.mark.parametrize("current_a", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_current_is_silently_dropped_without_changing_error_state(
    monkeypatch,
    current_a,
):
    service, bus = _build_service()
    service._last_current_read_error = "existing error"
    service._read_start_bit = lambda: False
    service.read_offset_value = lambda offset, value_type: current_a
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(monotonic=lambda: 123.5, monotonic_ns=lambda: 456),
    )

    service.poll_once()

    assert bus.plc_current_sampled.calls == []
    assert bus.log.calls == []
    assert service._last_current_read_error == "existing error"


def test_finite_current_clears_error_state_only_after_emit_succeeds(monkeypatch):
    emit_error = RuntimeError("slot failed")
    bus = _BusStub()
    bus.plc_current_sampled = _SignalStub(error=emit_error)
    service, _ = _build_service(bus=bus)
    service._last_current_read_error = "existing error"
    service._read_start_bit = lambda: False
    service.read_offset_value = lambda offset, value_type: 2.5
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(monotonic=lambda: 123.5, monotonic_ns=lambda: 456),
    )

    with pytest.raises(RuntimeError, match="slot failed"):
        service.poll_once()

    assert service._last_current_read_error == "existing error"
    bus.plc_current_sampled.error = None

    service.poll_once()

    assert bus.plc_current_sampled.calls == [
        (7, 123.5, 2.5),
        (7, 123.5, 2.5),
    ]
    assert service._last_current_read_error == ""


@pytest.mark.parametrize(
    "error",
    [S7Error("offline"), struct.error("short REAL")],
)
def test_expected_plc_current_read_errors_are_logged_once_and_do_not_block_start(
    monkeypatch,
    error,
):
    starts = []
    service, bus = _build_service(
        on_start_requested=lambda *args: starts.append(args),
    )
    start_states = iter([False, True, True, True])
    current_results = iter([error, error, 2.0, error])
    service._read_start_bit = lambda: next(start_states)
    service._write_testing_signal = lambda testing: None
    service._emit_plc_workorder = lambda: {"sn": "SN-1"}

    def read_offset_value(offset, value_type):
        result = next(current_results)
        if isinstance(result, BaseException):
            raise result
        return result

    service.read_offset_value = read_offset_value
    current_times = iter([1.0, 1.1, 1.2, 1.3])
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(current_times),
            monotonic_ns=lambda: 1_100_000_000,
        ),
    )

    service.poll_once()
    service.poll_once()

    current_error_logs = [
        call[0]
        for call in bus.log.calls
        if "current_value_offset" in call[0]
    ]
    assert len(current_error_logs) == 1
    assert "PLC 7" in current_error_logs[0]
    assert "current_value_offset" in current_error_logs[0]
    assert str(error) in current_error_logs[0]
    assert starts == [(7, {"sn": "SN-1"}, 1_100_000_000)]
    assert bus.plc_current_sampled.calls == []

    service.poll_once()

    assert service._last_current_read_error == ""
    assert bus.plc_current_sampled.calls == [(7, 1.2, 2.0)]

    service.poll_once()

    current_error_logs = [
        call[0]
        for call in bus.log.calls
        if "current_value_offset" in call[0]
    ]
    assert current_error_logs == [current_error_logs[0], current_error_logs[0]]


@pytest.mark.parametrize("raw_value", [object(), "not-a-number"])
def test_current_conversion_errors_use_deduplicated_error_path(
    monkeypatch,
    raw_value,
):
    service, bus = _build_service()
    service._read_start_bit = lambda: False
    current_results = iter([raw_value, raw_value, 2.0, raw_value])
    service.read_offset_value = lambda offset, value_type: next(current_results)
    current_times = iter([1.0, 1.1, 1.2, 1.3])
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(current_times),
            monotonic_ns=lambda: 456,
        ),
    )

    service.poll_once()
    service.poll_once()
    assert len(bus.log.calls) == 1

    service.poll_once()
    assert service._last_current_read_error == ""

    service.poll_once()
    assert len(bus.log.calls) == 2
    assert bus.log.calls[0] == bus.log.calls[1]
    assert "PLC 7" in bus.log.calls[0][0]
    assert "current_value_offset" in bus.log.calls[0][0]
    assert bus.plc_current_sampled.calls == [(7, 1.2, 2.0)]


@pytest.mark.parametrize(
    ("case", "expected_exception"),
    [
        ("malformed_offset", ValueError),
        ("invalid_plc_id", ValueError),
        ("missing_signal", AttributeError),
        ("emit_failure", RuntimeError),
        ("unexpected_read_failure", LookupError),
    ],
)
def test_internal_current_contract_errors_propagate_without_changing_error_state(
    monkeypatch,
    case,
    expected_exception,
):
    field_offsets = {
        "start_signal_offset": "0.0",
        "current_value_offset": "bad.offset" if case == "malformed_offset" else 4,
    }
    service, bus = _build_service(
        field_offsets=field_offsets,
        plc_id="invalid" if case == "invalid_plc_id" else 7,
    )
    service._last_current_read_error = "original error state"
    service._read_start_bit = lambda: False
    if case != "malformed_offset":
        service.read_offset_value = lambda offset, value_type: 1.0
    if case == "missing_signal":
        del bus.plc_current_sampled
    elif case == "emit_failure":
        bus.plc_current_sampled = _SignalStub(error=RuntimeError("slot failed"))
    elif case == "unexpected_read_failure":
        def unexpected_failure(offset, value_type):
            raise LookupError("programming error")

        service.read_offset_value = unexpected_failure
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(monotonic=lambda: 123.5, monotonic_ns=lambda: 456),
    )

    with pytest.raises(expected_exception):
        service.poll_once()

    assert bus.log.calls == []
    assert service._last_current_read_error == "original error state"


@pytest.mark.parametrize(
    ("current_mode", "expected_emissions"),
    [
        ("no_offset", []),
        ("finite", [(7, 10.0, 1.25), (7, 10.1, 1.25)]),
        ("read_error", []),
        ("conversion_error", []),
    ],
)
def test_poll_once_preserves_baseline_and_rising_edge_sequence(
    monkeypatch,
    current_mode,
    expected_emissions,
):
    calls = []
    starts = []
    field_offsets = {"start_signal_offset": "0.0"}
    if current_mode != "no_offset":
        field_offsets["current_value_offset"] = 4
    service, bus = _build_service(
        field_offsets=field_offsets,
        auto_reset_bit=True,
        on_start_requested=lambda *args: (
            calls.append("on_start"),
            starts.append(args),
        ),
    )
    start_states = iter([False, True])
    service._read_start_bit = lambda: calls.append("read_start") or next(start_states)

    if current_mode == "finite":
        service.read_offset_value = lambda offset, value_type: 1.25
    elif current_mode == "read_error":
        def read_error(offset, value_type):
            raise S7Error("offline")

        service.read_offset_value = read_error
    elif current_mode == "conversion_error":
        service.read_offset_value = lambda offset, value_type: "bad"
    else:
        service.read_offset_value = lambda *_args: pytest.fail(
            "missing offset must not read current"
        )

    original_poll_current = service._poll_current_value

    def poll_current():
        calls.append("poll_current")
        return original_poll_current()

    service._poll_current_value = poll_current
    service._write_testing_signal = lambda testing: calls.append(
        ("write_testing", testing)
    )
    service._emit_plc_workorder = lambda: calls.append("read_workorder") or {
        "sn": "SN-1"
    }
    service.write_plc_value = lambda offset, value_type, value: calls.append(
        ("auto_reset", offset, value_type, value)
    )
    current_times = iter([10.0, 10.1])
    monkeypatch.setattr(
        plc_service_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(current_times),
            monotonic_ns=lambda: calls.append("trigger_clock") or 10_100_000_000,
        ),
    )

    service.poll_once()

    assert calls == ["read_start", "poll_current"]
    assert starts == []

    service.poll_once()

    assert calls == [
        "read_start",
        "poll_current",
        "read_start",
        "trigger_clock",
        "poll_current",
        ("write_testing", True),
        "read_workorder",
        "on_start",
        ("auto_reset", "0.0", "bool", False),
    ]
    assert starts == [(7, {"sn": "SN-1"}, 10_100_000_000)]
    assert bus.plc_current_sampled.calls == expected_emissions


def test_disconnected_poll_only_uses_reconnect_path():
    service, bus = _build_service(client=None)
    calls = []
    service._try_reconnect = lambda: calls.append("reconnect")
    service._read_start_bit = lambda: pytest.fail("disconnected poll must not read start")
    service._poll_current_value = lambda: pytest.fail(
        "disconnected poll must not read current"
    )

    service.poll_once()

    assert calls == ["reconnect"]
    assert bus.plc_current_sampled.calls == []


def test_failed_required_start_read_skips_current_and_start_processing():
    starts = []
    service, bus = _build_service(
        on_start_requested=lambda *args: starts.append(args),
    )
    calls = []
    service._read_start_bit = lambda: calls.append("read_start")
    service._poll_current_value = lambda: pytest.fail(
        "failed required start read must skip current"
    )

    service.poll_once()

    assert calls == ["read_start"]
    assert starts == []
    assert bus.plc_current_sampled.calls == []


def test_trigger_is_latched_before_slow_current_logging_and_testing_write(monkeypatch):
    now = [10.0]
    starts = []
    service, bus = _build_service(on_start_requested=lambda *args: starts.append(args))
    service._last_value = False
    service._read_start_bit = lambda: True
    def delay(*_args):
        now[0] += 0.25
        return 1.0
    service.read_offset_value = delay
    service._write_testing_signal = delay
    bus.log.emit = delay
    service.logger.info = delay
    service._emit_plc_workorder = lambda: {"sn": "original"}
    monkeypatch.setattr(plc_service_module, "time", SimpleNamespace(
        monotonic=lambda: now[0], monotonic_ns=lambda: int(now[0] * 1e9)))
    service.poll_once()
    assert starts == [(7, {"sn": "original"}, 10_000_000_000)]
    assert now[0] > 10.5
