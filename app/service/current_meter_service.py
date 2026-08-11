from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from app.service.run_persistence import CurrentTrace

try:  # pragma: no cover - exercised by integration with installed dependency.
    from pymodbus.client import ModbusTcpClient
except Exception:  # pragma: no cover - pymodbus may be absent in tests.
    ModbusTcpClient = None


_REGISTER_TYPES = {"holding", "input"}
_DATA_TYPES = {"uint16", "int16", "uint32", "int32", "float32"}
_BYTE_ORDERS = {"big", "little"}
_WORD_ORDERS = {"big", "little"}
_CURRENT_SOURCES = {"modbus_tcp", "plc"}


@dataclass(frozen=True)
class CurrentSample:
    time_sec: float
    current_a: float
    monotonic_time: float | None = None


def current_samples_to_trace(samples: list[CurrentSample]) -> CurrentTrace | None:
    """Convert one settled acquisition list without sorting or resampling."""

    if type(samples) is not list:
        raise ValueError("current samples must be an exact list")
    if not samples:
        return None
    pairs: list[tuple[float, float]] = []
    previous_time: float | int | None = None
    for sample in samples:
        if type(sample) is not CurrentSample:
            raise ValueError("current sample must be exact")
        time_sec = sample.time_sec
        current_a = sample.current_a
        if (
            type(time_sec) not in (int, float)
            or type(current_a) not in (int, float)
            or not math.isfinite(time_sec)
            or not math.isfinite(current_a)
            or time_sec < 0
            or (previous_time is not None and time_sec <= previous_time)
        ):
            raise ValueError("current sample shape is invalid")
        pairs.append((time_sec, current_a))
        previous_time = time_sec
    return CurrentTrace(samples=tuple(pairs))


def rebase_current_samples(
    samples: Iterable[CurrentSample],
    *,
    current_epoch: float,
) -> list[CurrentSample]:
    """Filter and rebase absolute current readings without changing their order."""

    try:
        epoch = float(current_epoch)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(epoch):
        return []

    rebased: list[CurrentSample] = []
    for sample in samples or ():
        if not isinstance(sample, CurrentSample):
            continue
        try:
            completed_monotonic = float(sample.monotonic_time)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(completed_monotonic) or completed_monotonic < epoch:
            continue
        rebased.append(
            CurrentSample(
                time_sec=completed_monotonic - epoch,
                current_a=sample.current_a,
                monotonic_time=completed_monotonic,
            )
        )
    return rebased


def normalize_current_meter_config(config: dict | None) -> dict:
    raw_config = config if isinstance(config, dict) else {}
    enabled = bool(raw_config.get("enabled", False))
    return {
        "enabled": enabled,
        "source": _choice_or_default(
            raw_config.get("source"),
            "modbus_tcp",
            _CURRENT_SOURCES,
        ),
        "host": str(raw_config.get("host") or "").strip(),
        "port": _int_or_default(raw_config.get("port"), 502, minimum=1, maximum=65535),
        "unit_id": _int_or_default(raw_config.get("unit_id"), 1, minimum=0, maximum=247),
        "register_type": _choice_or_default(
            raw_config.get("register_type"),
            "input",
            _REGISTER_TYPES,
        ),
        "register_address": _int_or_default(
            raw_config.get("register_address"),
            0,
            minimum=0,
            maximum=65535,
        ),
        "register_count": _int_or_default(
            raw_config.get("register_count"),
            1,
            minimum=1,
            maximum=4,
        ),
        "data_type": _choice_or_default(raw_config.get("data_type"), "uint16", _DATA_TYPES),
        "byte_order": _choice_or_default(raw_config.get("byte_order"), "big", _BYTE_ORDERS),
        "word_order": _choice_or_default(raw_config.get("word_order"), "big", _WORD_ORDERS),
        "scale": _float_or_default(raw_config.get("scale"), 0.001),
        "offset": _float_or_default(raw_config.get("offset"), 0.0),
        "poll_interval_ms": _int_or_default(
            raw_config.get("poll_interval_ms"),
            200,
            minimum=20,
            maximum=60000,
        ),
        "timeout_ms": _int_or_default(
            raw_config.get("timeout_ms"),
            1000,
            minimum=50,
            maximum=60000,
        ),
    }


def current_meter_display_unit(config: dict | None) -> str | None:
    raw_config = config if isinstance(config, dict) else {}
    normalized = normalize_current_meter_config(config)
    if normalized["source"] == "plc":
        return "A"

    explicit_unit = raw_config.get("unit")
    if isinstance(explicit_unit, str):
        explicit_unit = explicit_unit.strip()
        if explicit_unit:
            return explicit_unit

    if (
        normalized["data_type"] in {"uint16", "int16"}
        and normalized["register_type"] == "input"
        and normalized["register_count"] == 1
        and normalized["byte_order"] == "big"
        and normalized["word_order"] == "big"
        and math.isclose(normalized["scale"], 0.001, rel_tol=1e-9, abs_tol=1e-12)
    ):
        return "mA"
    return None


def validate_current_meter_config(config: dict | None) -> list[str]:
    normalized = normalize_current_meter_config(config)
    if not normalized["enabled"]:
        return []

    errors: list[str] = []
    if normalized["source"] == "modbus_tcp" and not normalized["host"]:
        errors.append("电流表 IP/主机不能为空")
    if (
        normalized["source"] == "modbus_tcp"
        and normalized["data_type"] in {"uint32", "int32", "float32"}
        and normalized["register_count"] < 2
    ):
        errors.append("电流表 32 位数据至少需要读取 2 个寄存器")
    return errors


def decode_current_registers(registers: Iterable[int], config: dict | None) -> float | None:
    normalized = normalize_current_meter_config(config)
    words = [int(value) & 0xFFFF for value in registers]
    data_type = normalized["data_type"]
    if data_type in {"uint16", "int16"}:
        if not words:
            return None
        raw_value = words[0]
        if data_type == "int16" and raw_value >= 0x8000:
            raw_value -= 0x10000
        value = float(raw_value)
    else:
        if len(words) < 2:
            return None
        pair = words[:2]
        if normalized["word_order"] == "little":
            pair = list(reversed(pair))
        canonical_bytes = b"".join(
            int(word).to_bytes(2, byteorder=normalized["byte_order"], signed=False)
            for word in pair
        )
        if data_type == "float32":
            try:
                value = float(struct.unpack(">f", canonical_bytes)[0])
            except struct.error:
                return None
        else:
            signed = data_type == "int32"
            value = float(int.from_bytes(canonical_bytes, byteorder="big", signed=signed))

    current = value * normalized["scale"] + normalized["offset"]
    return current if math.isfinite(current) else None


def build_current_trace_payload(
    samples: Iterable[CurrentSample | dict],
    *,
    analysis_window: tuple[float, float] | list[float] | None = None,
    config: dict | None = None,
) -> dict | None:
    normalized_samples = _normalize_samples(samples)
    if analysis_window is not None:
        try:
            start_sec = float(analysis_window[0])
            end_sec = float(analysis_window[1])
        except (TypeError, ValueError, IndexError):
            start_sec = 0.0
            end_sec = math.inf
        lower, upper = sorted((start_sec, end_sec))
        normalized_samples = [
            sample for sample in normalized_samples if lower <= sample.time_sec <= upper
        ]

    if not normalized_samples:
        return None

    times = [round(sample.time_sec, 6) for sample in normalized_samples]
    values = [round(sample.current_a, 6) for sample in normalized_samples]
    average = sum(values) / len(values)
    # The collector joins a run between two PLC polls.  For display, extend
    # the first observed value back to the run origin so the graph does not
    # begin one poll interval after the audio graph.  Raw samples remain
    # untouched for persistence and statistics.
    if analysis_window is None and times[0] > 0.0:
        times.insert(0, 0.0)
        values.insert(0, values[0])
    payload = {
        "times_sec": times,
        "values_a": values,
        "average_a": round(average, 6),
        "max_a": round(max(values), 6),
        "latest_a": values[-1],
    }
    unit = current_meter_display_unit(config)
    if unit:
        payload["unit"] = unit
    return payload


def current_trace_display_at_time(current_trace: dict | None, time_sec) -> str | None:
    value = current_trace_value_at_time(current_trace, time_sec)
    if value is None:
        return None
    unit = ""
    if isinstance(current_trace, dict):
        unit = str(current_trace.get("unit") or "").strip()
    text = f"{value:.3f}"
    return f"{text} {unit}" if unit else text


def current_trace_value_at_time(current_trace: dict | None, time_sec) -> float | None:
    if not isinstance(current_trace, dict):
        return None
    try:
        target_time = float(time_sec)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target_time):
        return None

    pairs = _normalize_current_trace_pairs(current_trace)
    if not pairs:
        return None

    for sample_time, value in pairs:
        if target_time == sample_time:
            return value

    first_time = pairs[0][0]
    last_time = pairs[-1][0]
    if target_time < first_time or target_time > last_time or len(pairs) == 1:
        return None

    for (left_time, left_value), (right_time, right_value) in zip(pairs, pairs[1:]):
        if left_time <= target_time <= right_time:
            fraction = (target_time - left_time) / (right_time - left_time)
            return left_value + (right_value - left_value) * fraction
    return None


class CurrentMeterCollector:
    def __init__(
        self,
        config: dict,
        *,
        client_factory: Callable[..., object] | None = None,
        time_func: Callable[[], float] | None = None,
        time_origin: float | None = None,
        log_callback: Callable[[str], None] | None = None,
        sample_callback: Callable[[CurrentSample, tuple[CurrentSample, ...]], None] | None = None,
    ):
        self._config = normalize_current_meter_config(config)
        self._client_factory = client_factory
        self._time = time_func or time.monotonic
        self._time_origin = (
            None if time_origin is None else float(time_origin)
        )
        self._log = log_callback
        self._sample_callback = sample_callback
        self._samples: list[CurrentSample] = []
        self._samples_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time = 0.0
        self._last_error = ""
        self._last_callback_error = ""

    def start(self) -> bool:
        if not self._config["enabled"]:
            return False
        if self._config["source"] != "modbus_tcp":
            return False
        if not self._config["host"]:
            self._log_message("电流表未配置 IP/主机，跳过采集")
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._start_time = (
            self._time()
            if self._time_origin is None
            else self._time_origin
        )
        self._thread = threading.Thread(
            target=self._run,
            name="CurrentMeterCollector",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("current collector stop timed out")

    def samples(self) -> list[CurrentSample]:
        with self._samples_lock:
            return list(self._samples)

    def trace_payload(self, analysis_window=None) -> dict | None:
        return build_current_trace_payload(
            self.samples(),
            analysis_window=analysis_window,
            config=self._config,
        )

    def _run(self) -> None:
        client = None
        try:
            factory = self._client_factory or ModbusTcpClient
            if factory is None:
                self._log_message("未安装 pymodbus，跳过电流表采集")
                return
            client = factory(
                self._config["host"],
                port=self._config["port"],
                timeout=self._config["timeout_ms"] / 1000.0,
            )
            connect = getattr(client, "connect", None)
            if callable(connect) and connect() is False:
                self._log_message("电流表 Modbus TCP 连接失败")
                return

            while not self._stop_event.is_set():
                self._poll_once(client)
                self._stop_event.wait(self._config["poll_interval_ms"] / 1000.0)
        except Exception as exc:
            self._log_message(f"电流表采集异常: {exc}")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _poll_once(self, client) -> None:
        read = (
            getattr(client, "read_input_registers", None)
            if self._config["register_type"] == "input"
            else getattr(client, "read_holding_registers", None)
        )
        if not callable(read):
            self._log_once("电流表 Modbus 客户端不支持读取寄存器")
            return
        try:
            result = self._read_modbus_registers(
                read,
                self._config["register_address"],
                self._config["register_count"],
                self._config["unit_id"],
            )
        except Exception as exc:
            self._log_once(f"电流表读取失败: {exc}")
            return

        is_error = getattr(result, "isError", None)
        if callable(is_error) and is_error():
            self._log_once("电流表读取返回 Modbus 错误")
            return
        value = decode_current_registers(getattr(result, "registers", []), self._config)
        if value is None:
            self._log_once("电流表读取值无效")
            return

        completed_monotonic = self._time()
        sample = CurrentSample(
            time_sec=max(0.0, completed_monotonic - self._start_time),
            current_a=float(value),
            monotonic_time=completed_monotonic,
        )
        has_sample_callback = callable(self._sample_callback)
        with self._samples_lock:
            self._samples.append(sample)
            if has_sample_callback:
                samples_snapshot = tuple(self._samples)
        self._last_error = ""
        if has_sample_callback:
            self._notify_sample_callback(sample, samples_snapshot)

    def _notify_sample_callback(
        self,
        sample: CurrentSample,
        samples_snapshot: tuple[CurrentSample, ...],
    ) -> None:
        if not callable(self._sample_callback):
            return
        try:
            self._sample_callback(sample, samples_snapshot)
            self._last_callback_error = ""
        except Exception as exc:
            message = f"电流表样本回调失败: {exc}"
            if message == self._last_callback_error:
                return
            self._last_callback_error = message
            self._log_message(message)

    @staticmethod
    def _read_modbus_registers(
        read,
        address: int,
        count: int,
        unit_id: int,
    ):
        attempts = (
            lambda: read(address, count=count, slave=unit_id),
            lambda: read(address, count=count, unit=unit_id),
            lambda: read(address, count=count, device_id=unit_id),
        )
        last_error: TypeError | None = None
        for attempt in attempts:
            try:
                return attempt()
            except TypeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error

    def _log_once(self, message: str) -> None:
        if message == self._last_error:
            return
        self._last_error = message
        self._log_message(message)

    def _log_message(self, message: str) -> None:
        if callable(self._log):
            try:
                self._log(message)
            except Exception:
                pass


class PlcCurrentCollector:
    """Collect current samples published by a :class:`PlcService` instance."""

    def __init__(
        self,
        config: dict,
        *,
        bus,
        plc_id: int,
        time_func: Callable[[], float] | None = None,
        time_origin: float | None = None,
        log_callback: Callable[[str], None] | None = None,
        sample_callback: Callable[[CurrentSample, tuple[CurrentSample, ...]], None] | None = None,
    ):
        self._config = normalize_current_meter_config(config)
        self._bus = bus
        self._plc_id = int(plc_id)
        self._time = time_func or time.monotonic
        self._time_origin = (
            None if time_origin is None else float(time_origin)
        )
        self._log = log_callback
        self._sample_callback = sample_callback
        self._samples: list[CurrentSample] = []
        self._samples_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self._start_time = 0.0
        self._last_callback_error = ""
        # Keep one stable callable object so PyQt can reliably disconnect it.
        self._signal_handler = self._on_plc_current_sampled

    def start(self) -> bool:
        if not self._config["enabled"] or self._config["source"] != "plc":
            return False
        signal = getattr(self._bus, "plc_current_sampled", None)
        if signal is None or not callable(getattr(signal, "connect", None)):
            self._log_message("PLC 电流采集信号不可用")
            return False

        with self._state_lock:
            if self._running:
                return True
            self._start_time = (
                self._time()
                if self._time_origin is None
                else self._time_origin
            )
            try:
                signal.connect(self._signal_handler)
            except Exception as exc:
                self._log_message(f"PLC 电流采集启动失败: {exc}")
                return False
            self._running = True
        return True

    def stop(self, timeout: float | None = None) -> None:
        del timeout  # Kept for API parity with CurrentMeterCollector.
        with self._state_lock:
            if not self._running:
                return
            self._running = False
            signal = getattr(self._bus, "plc_current_sampled", None)
            if signal is not None and callable(getattr(signal, "disconnect", None)):
                try:
                    signal.disconnect(self._signal_handler)
                except (TypeError, RuntimeError):
                    pass

    def samples(self) -> list[CurrentSample]:
        with self._samples_lock:
            return list(self._samples)

    def trace_payload(self, analysis_window=None) -> dict | None:
        return build_current_trace_payload(
            self.samples(),
            analysis_window=analysis_window,
            config=self._config,
        )

    def _on_plc_current_sampled(
        self,
        plc_id: int,
        monotonic_time: float,
        current_a: float,
    ) -> None:
        if plc_id != self._plc_id:
            return
        try:
            completed_monotonic = float(monotonic_time)
            current_value = float(current_a)
        except (TypeError, ValueError):
            return
        if not math.isfinite(completed_monotonic) or not math.isfinite(current_value):
            return

        has_sample_callback = callable(self._sample_callback)
        with self._state_lock:
            if not self._running or completed_monotonic < self._start_time:
                return
            sample = CurrentSample(
                time_sec=max(0.0, completed_monotonic - self._start_time),
                current_a=current_value,
                monotonic_time=completed_monotonic,
            )
            # Serialize storage with stop() so no sample can appear after stop returns.
            with self._samples_lock:
                self._samples.append(sample)
                if has_sample_callback:
                    samples_snapshot = tuple(self._samples)
        if has_sample_callback:
            self._notify_sample_callback(sample, samples_snapshot)

    def _notify_sample_callback(
        self,
        sample: CurrentSample,
        samples_snapshot: tuple[CurrentSample, ...],
    ) -> None:
        try:
            self._sample_callback(sample, samples_snapshot)
            self._last_callback_error = ""
        except Exception as exc:
            message = f"PLC 电流样本回调失败: {exc}"
            if message == self._last_callback_error:
                return
            self._last_callback_error = message
            self._log_message(message)

    def _log_message(self, message: str) -> None:
        if callable(self._log):
            try:
                self._log(message)
            except Exception:
                pass


def _normalize_samples(samples: Iterable[CurrentSample | dict]) -> list[CurrentSample]:
    normalized: list[CurrentSample] = []
    for sample in samples or []:
        if isinstance(sample, CurrentSample):
            time_sec = sample.time_sec
            current_a = sample.current_a
        elif isinstance(sample, dict):
            time_sec = sample.get("time_sec")
            current_a = sample.get("current_a")
        else:
            continue
        try:
            time_value = float(time_sec)
            current_value = float(current_a)
        except (TypeError, ValueError):
            continue
        if math.isfinite(time_value) and math.isfinite(current_value):
            normalized.append(CurrentSample(time_value, current_value))
    normalized.sort(key=lambda item: item.time_sec)
    return normalized


def _normalize_current_trace_pairs(current_trace: dict) -> list[tuple[float, float]]:
    times = current_trace.get("times_sec")
    values = current_trace.get("values_a")
    if not isinstance(times, Iterable) or isinstance(times, (str, bytes)):
        return []
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return []

    pairs_by_time: dict[float, float] = {}
    for time_sec, current_a in zip(times, values):
        try:
            time_value = float(time_sec)
            current_value = float(current_a)
        except (TypeError, ValueError):
            continue
        if (
            math.isfinite(time_value)
            and math.isfinite(current_value)
            and time_value not in pairs_by_time
        ):
            pairs_by_time[time_value] = current_value
    return sorted(pairs_by_time.items())


def _int_or_default(value, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if isinstance(value, bool):
        number = default
    return min(max(number, minimum), maximum)


def _float_or_default(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _choice_or_default(value, default: str, choices: set[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default
