from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Mapping
from collections.abc import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
import snap7
import snap7.util

from app.service.log_manager import LogManager
from app.service.multi_run_integration.plc_dispatch import (
    validate_plc_id,
    validate_plc_result_payload,
)
from app.service.plc_protocol import (
    PLC_CONFIG_RETURN_FIELDS,
    PLC_FIELD_BY_KEY,
    PLC_RESULT_FIELDS,
    PLC_WORKORDER_FIELDS,
    PlcValueType,
    parse_plc_offset,
)
from app.service.plc_result_sequencer import PlcResultBoundaryError, PlcScalar

DEFAULT_PLC_POLL_INTERVAL_MS = 50
MIN_PLC_POLL_INTERVAL_MS = 20
MAX_PLC_POLL_INTERVAL_MS = 2_147_483_647


class _IsolatedPlcOutputWorker:
    """Own the result Snap7 client away from the ingress polling thread."""

    _STOP = object()

    def __init__(self, boundary_factory: Callable[[], object]) -> None:
        self._boundary_factory = boundary_factory
        self._commands: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._accepting = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._accepting = True
            self._thread = threading.Thread(
                target=self._run,
                name="plc-result-output",
                daemon=False,
            )
            self._thread.start()

    def submit(self, command: object) -> bool:
        with self._lock:
            if not self._accepting:
                return False
            self._commands.put(command)
            return True

    def close(self, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None:
                return True
            if self._accepting:
                self._accepting = False
                self._commands.put(self._STOP)
        if thread is threading.current_thread():
            return False
        thread.join(timeout_seconds)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
        return stopped

    @staticmethod
    def _fail(command: object, reason: str) -> None:
        fail = getattr(command, "fail_if_pending", None)
        if callable(fail):
            fail(reason)

    def _run(self) -> None:
        boundary: object | None = None
        boundary_error: Exception | None = None
        try:
            try:
                boundary = self._boundary_factory()
                connect = getattr(boundary, "_connect_once", None)
                if callable(connect):
                    connect()
            except Exception as exc:
                boundary_error = exc

            while True:
                command = self._commands.get()
                if command is self._STOP:
                    break
                if boundary is None:
                    self._fail(
                        command,
                        "PLC isolated result connection is unavailable",
                    )
                    continue
                connect = getattr(boundary, "_connect_once", None)
                if (
                    callable(connect)
                    and getattr(boundary, "plc_client", None) is None
                ):
                    connect()
                execute = getattr(command, "execute", None)
                if callable(execute):
                    execute(boundary)
                else:
                    self._fail(command, "PLC output command is invalid")
        finally:
            if boundary is not None:
                disconnect = getattr(boundary, "_disconnect", None)
                if callable(disconnect):
                    try:
                        disconnect()
                    except Exception:
                        pass
            reason = "PLC isolated result writer stopped"
            if boundary_error is not None:
                reason = "PLC isolated result connection is unavailable"
            while True:
                try:
                    pending = self._commands.get_nowait()
                except queue.Empty:
                    break
                if pending is not self._STOP:
                    self._fail(pending, reason)


def validate_plc_poll_interval_ms(value: object) -> int:
    if (
        type(value) is not int
        or not MIN_PLC_POLL_INTERVAL_MS
        <= value
        <= MAX_PLC_POLL_INTERVAL_MS
    ):
        raise ValueError(
            "PLC poll interval must be an integer from "
            f"{MIN_PLC_POLL_INTERVAL_MS} to "
            f"{MAX_PLC_POLL_INTERVAL_MS} ms"
        )
    return value


class PlcService(QObject):
    """
    用 python-snap7 轮询 PLC 开始信号。

    逻辑：
    1. 程序启动时先连接一次 PLC。
    2. 用 QTimer 周期读取 PLC 的开始位。
    3. 当该位从 0 变成 1 时，触发开始回调。
    """
    # 连接结果通知界面信号，包含“连接 PLC”的结果是True or False
    connect_result = pyqtSignal(bool, str)
    # 连接状态通知界面信号
    state_changed = pyqtSignal(str)
    plc_dispatch_requested = pyqtSignal(object)
    read_time_ms = 60000
    def __init__(self,
        bus,
        on_start_requested,
        ip: str,
        rack: int = 0,
        slot: int = 1,
        db_number: int = 1,
        poll_interval_ms = DEFAULT_PLC_POLL_INTERVAL_MS,
        reconnect_interval_ms = 3000,
        auto_reset_bit = True,
        field_offsets_dict = None,
        plc_id = None,
        monotonic_ns=time.monotonic_ns,
        isolate_result_io: bool = False,
        output_boundary_factory: Callable[[], object] | None = None,
    ):
        validated_poll_interval_ms = validate_plc_poll_interval_ms(
            poll_interval_ms
        )
        # 初始化 PLC 服务，保存连接参数、轮询参数和运行时状态。
        super().__init__()
        self.bus = bus
        self.logger = LogManager.set_log_handler("core")
        self._on_start_requested = on_start_requested
        if not callable(monotonic_ns):
            raise ValueError("monotonic_ns must be callable")
        self._monotonic_ns = monotonic_ns
        self._plc_id = plc_id
        self._field_offsets_dict = field_offsets_dict or {}
        self._isolate_result_io = bool(isolate_result_io)
        self._output_boundary_factory = output_boundary_factory
        self._output_worker: _IsolatedPlcOutputWorker | None = None

        # PLC 连接参数(IP，机架号，槽号）
        self._ip = ip
        self._rack = rack
        self._slot = slot

        # 数据块号DB
        self._db_number = int(db_number)
        # 轮询周期
        self._poll_interval_ms = validated_poll_interval_ms
        # 重连周期
        self._reconnect_interval_ms = int(reconnect_interval_ms)

        # 触发开始后，是否把 PLC 里的开始位自动写回 0
        self._auto_reset_bit = bool(auto_reset_bit)

        # 与PLC 通信的客户端实例
        self.plc_client = None
        self.timer = None
        self.value_timer = None
        self._last_value = None
        self._reconnect_timer_ms = 0  # 重连计时器
        self._last_connect_error = ""
        self._last_current_read_error = ""
        self._start_signal_reset_pending = False
        self.plc_dispatch_requested.connect(self._execute_plc_dispatch)

    @pyqtSlot()
    def start_plc_service(self):
        # 启动 PLC 监听；如果已经启动过，就直接返回，避免重复创建定时器。
        self.logger.info(f"启动 PLC{self._plc_id} 服务")
        if self.timer is not None:
            return
        self._start_isolated_result_io()
        self.state_changed.emit("连接中")
        # 启动定时器前 先尝试连一次 PLC；失败也启动定时器，后面继续自动重连。
        self._connect_once()

        # 创建轮询定时器，按固定周期读取 PLC 开始位。
        self.timer = QTimer(self)
        self.timer.setInterval(self._poll_interval_ms)
        self.timer.timeout.connect(self.poll_once)
        self.timer.start()

        if self.plc_client is None:
            self.state_changed.emit("重连中")
            return

        self.logger.info(f"PLC{self._plc_id}开始监听: ip={self._ip}, rack={self._rack}, slot={self._slot}, "
            f"DB{self._db_number}")
        self.bus.log.emit(
            f"PLC{self._plc_id}开始监听: ip={self._ip}, rack={self._rack}, slot={self._slot}, "
            f"DB{self._db_number}"
        )
        # self._start_value_timer()

    @pyqtSlot()
    def stop_ingress_polling(self):
        self.logger.info(f"停止 PLC 服务")
        # Stop ingress timers while preserving the connection for ordered drain.
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        if self.value_timer is not None:
            self.value_timer.stop()
            self.value_timer.deleteLater()
            self.value_timer = None
        self._last_value = None
        self.bus.log.emit(f"PLC{self._plc_id}停止监听")
        self.logger.info(f"PLC{self._plc_id}停止监听")

    @pyqtSlot()
    def disconnect_plc_service(self):
        self._stop_isolated_result_io()
        self._disconnect()

    @pyqtSlot()
    def stop_plc_service(self):
        self.stop_ingress_polling()
        self.disconnect_plc_service()

    def _connect_once(self):
        """
        连接一次plc
        """
        self.logger.info(f"开始尝试连接 PLC {self._plc_id}")
        # 尝试建立一次 PLC 连接；如果当前已经连上，就不重复连接。
        if self.plc_client is not None:
            self.logger.info("已有 PLC 客户端，不重复连接")
            return

        try:
            client = snap7.client.Client()
            client.connect(self._ip, self._rack, self._slot)
            self.logger.info(f"PLC{self._plc_id}连接参数: ip={self._ip}, rack={self._rack}, slot={self._slot}")
            self.logger.info(f"PLC{self._plc_id}地址参数: db_number数据块号={self._db_number}")
            self.bus.log.emit(f"PLC{self._plc_id}连接参数: ip={self._ip}, rack={self._rack}, slot={self._slot}")
            self.bus.log.emit(f"PLC{self._plc_id}地址参数: db_number数据块号={self._db_number}")
            self.plc_client = client
            self._reconnect_timer_ms = 0
            self._last_connect_error = ""
            self.state_changed.emit("已连接")
            self.logger.info(f"PLC{self._plc_id}连接成功: {self._ip}")
            self.bus.log.emit(f"PLC{self._plc_id}连接成功: {self._ip}")
            self.connect_result.emit(True, "")
            self._apply_start_signal_reset()
        except Exception as exc:
            self.plc_client = None
            self._last_connect_error = str(exc)
            self.state_changed.emit("重连中")
            self.logger.error(f"PLC{self._plc_id}连接失败: {exc}")
            self.bus.log.emit(f"PLC{self._plc_id}连接失败: {exc}")

    def _disconnect(self):
        # 断开当前 PLC 连接，并清空 plc_client，供后续重连使用。
        if self.plc_client is None:
            return
        try:
            self.plc_client.disconnect()
        except Exception:
            pass
        self.plc_client = None
        if self.timer is not None:
            self.state_changed.emit("重连中")
        else:
            self.state_changed.emit("未连接")

    @pyqtSlot(int)
    def request_start_signal_reset(self, plc_id: int) -> None:
        if type(plc_id) is not int or plc_id != self._plc_id:
            return
        self._start_signal_reset_pending = True
        self._apply_start_signal_reset()

    def _apply_start_signal_reset(self) -> None:
        if not self._start_signal_reset_pending or self.plc_client is None:
            return

        try:
            if not self.write_field_value("start_signal_offset", False):
                raise ValueError("start_signal_offset is not configured")
        except Exception as exc:
            message = (
                f"PLC {self._plc_id} 启动信号归位失败 "
                f"(start_signal_offset): {exc}"
            )
            self.bus.plc_start_signal_reset_result.emit(
                self._plc_id,
                False,
                message,
            )
            self.bus.log.emit(message)
            self.logger.error(message)
            self._disconnect()
            return

        self._last_value = False
        self._start_signal_reset_pending = False
        message = f"PLC {self._plc_id} 启动信号已归位"
        self.bus.log.emit(message)
        self.logger.info(message)
        self.bus.plc_start_signal_reset_result.emit(self._plc_id, True, "")

    def _try_reconnect(self):
        self.logger.info(f"{self._plc_id}触发重新连接")
        # 按重连间隔尝试重新连接，避免每次轮询失败都立刻重连。
        if self.timer is None:
            return

        # 不要每次轮询都立刻重连，按配置的重连间隔节流。
        self._reconnect_timer_ms += self._poll_interval_ms
        if self._reconnect_timer_ms < self._reconnect_interval_ms:
            return
        self._reconnect_timer_ms = 0
        self._connect_once()

    def _read_start_bit(self) -> bool | None:
        # 读取 PLC 中配置的开始位；读失败时返回 None，并断开等待重连。
        if self.plc_client is None:
            return None

        try:
            value = self.read_field_value("start_signal_offset")
            if value is None:
                raise ValueError("start_signal_offset is not configured")
            return bool(value)
        except Exception as exc:
            self._disconnect()
            self.bus.log.emit(f"PLC{self._plc_id}读取失败: {exc}")
            self.logger.error(f"PLC{self._plc_id}读取失败: {exc}")
            return None

    def _poll_current_value(self) -> None:
        """Publish the optional Siemens REAL current field without blocking start detection."""

        if (
            self.plc_client is None
            or self._configured_offset("current_value_offset") is None
        ):
            return

        try:
            # The PLC value belongs to this poll request.  Timestamping after
            # the network round trip made the current curve appear late next
            # to the sample-clock audio timeline.
            sampled_monotonic = time.monotonic()
            value = self.read_field_value("current_value_offset")
            current_a = float(value)
            if not math.isfinite(current_a):
                return
            self.bus.plc_current_sampled.emit(
                int(self._plc_id),
                sampled_monotonic,
                current_a,
            )
            self._last_current_read_error = ""
        except Exception as exc:
            message = self._field_failure_message(
                "read",
                "current_value_offset",
                exc,
            )
            if message == self._last_current_read_error:
                return
            self._last_current_read_error = message
            self.bus.log.emit(message)
            self.logger.error(message)

    def _configured_offset(self, field_name: str) -> str | None:
        offset = self._field_offsets_dict.get(field_name)
        if offset is None or str(offset).strip() == "":
            return None
        return str(offset).strip()

    def read_field_value(self, field_name: str):
        spec = PLC_FIELD_BY_KEY[field_name]
        offset = self._configured_offset(field_name)
        if offset is None:
            return None
        return self._read_value(offset, spec.value_type)

    def write_field_value(self, field_name: str, value) -> bool:
        spec = PLC_FIELD_BY_KEY[field_name]
        offset = self._configured_offset(field_name)
        if offset is None:
            return False
        self._write_value(offset, spec.value_type, value)
        return True

    def _read_value(self, offset: str, value_type: PlcValueType):
        byte_index, bit_index = parse_plc_offset(offset, value_type)

        if value_type is PlcValueType.STRING:
            header = self.plc_client.db_read(self._db_number, byte_index, 2)
            if len(header) != 2:
                raise ValueError(
                    f"invalid STRING header size: expected 2, got {len(header)}"
                )
            maximum_length, current_length = header
            if maximum_length <= 0:
                raise ValueError("invalid STRING maximum length: must be positive")
            if current_length > maximum_length:
                raise ValueError(
                    "invalid STRING current length: "
                    f"{current_length} exceeds maximum length {maximum_length}"
                )
            if current_length == 0:
                return ""
            data = self.plc_client.db_read(
                self._db_number,
                byte_index + 2,
                current_length,
            )
            return bytes(data).decode("ascii", errors="ignore").strip()

        if value_type is PlcValueType.BOOL:
            data = self.plc_client.db_read(self._db_number, byte_index, 1)
            return snap7.util.get_bool(data, 0, bit_index)

        if value_type is PlcValueType.INT:
            data = self.plc_client.db_read(self._db_number, byte_index, 2)
            return snap7.util.get_int(data, 0)

        if value_type is PlcValueType.REAL:
            data = self.plc_client.db_read(self._db_number, byte_index, 4)
            return snap7.util.get_real(data, 0)

        raise ValueError(f"unsupported PLC value type: {value_type!r}")

    def _write_value(
        self,
        offset: str,
        value_type: PlcValueType,
        value,
    ) -> None:
        byte_index, bit_index = parse_plc_offset(offset, value_type)

        if value_type is PlcValueType.STRING:
            if not isinstance(value, str):
                raise TypeError("Siemens STRING value must be a string")
            header = self.plc_client.db_read(self._db_number, byte_index, 2)
            if len(header) != 2:
                raise ValueError(
                    f"invalid STRING header size: expected 2, got {len(header)}"
                )
            maximum_length = header[0]
            if maximum_length <= 0:
                raise ValueError("invalid STRING maximum length: must be positive")
            if len(value) > maximum_length:
                raise ValueError(
                    f"STRING value length {len(value)} exceeds PLC maximum length "
                    f"{maximum_length}"
                )
            data = bytearray(maximum_length + 2)
            snap7.util.set_string(data, 0, value, maximum_length)
            self.plc_client.db_write(self._db_number, byte_index, data)
            return

        if value_type is PlcValueType.BOOL:
            data = bytearray(
                self.plc_client.db_read(self._db_number, byte_index, 1)
            )
            snap7.util.set_bool(data, 0, bit_index, bool(value))
            self.plc_client.db_write(self._db_number, byte_index, data)
            return

        if value_type is PlcValueType.INT:
            if type(value) is not int:
                raise TypeError("Siemens Int value must be an integer")
            if not -32768 <= value <= 32767:
                raise ValueError(
                    "Siemens Int value must be between -32768 and 32767"
                )
            data = bytearray(2)
            snap7.util.set_int(data, 0, value)
            self.plc_client.db_write(self._db_number, byte_index, data)
            return

        if value_type is PlcValueType.REAL:
            data = bytearray(4)
            snap7.util.set_real(data, 0, float(value))
            self.plc_client.db_write(self._db_number, byte_index, data)
            return

        raise ValueError(f"unsupported PLC value type: {value_type!r}")

    def _field_failure_message(
        self,
        operation: str,
        field_name: str,
        exc: Exception,
    ) -> str:
        return (
            f"PLC {self._plc_id} {operation} field {field_name} failed: {exc}"
        )

    def _log_field_failure(
        self,
        operation: str,
        field_name: str,
        exc: Exception,
    ) -> None:
        message = self._field_failure_message(operation, field_name, exc)
        self.bus.log.emit(message)
        self.logger.error(message)

    def _write_configured_field(self, field_name: str, value) -> bool:
        if self._configured_offset(field_name) is None:
            return True
        self.write_field_value(field_name, value)
        return True

    @pyqtSlot(object)
    def _execute_plc_dispatch(self, command: object) -> None:
        worker = self._output_worker
        if worker is not None:
            if worker.submit(command):
                return
            fail = getattr(command, "fail_if_pending", None)
            if callable(fail):
                fail("PLC isolated result writer is not accepting commands")
            return
        execute = getattr(command, "execute", None)
        if callable(execute):
            execute(self)

    def _start_isolated_result_io(self) -> None:
        if not self._isolate_result_io or self._output_worker is not None:
            return
        factory = self._output_boundary_factory
        if factory is None:
            factory = self._make_isolated_output_boundary
        worker = _IsolatedPlcOutputWorker(factory)
        self._output_worker = worker
        worker.start()

    def _stop_isolated_result_io(self) -> None:
        worker = self._output_worker
        if worker is None:
            return
        if not worker.close():
            self.logger.error(
                f"PLC {self._plc_id} isolated result writer did not stop promptly"
            )
            return
        self._output_worker = None

    def _make_isolated_output_boundary(self) -> object:
        return PlcService(
            bus=self.bus,
            on_start_requested=lambda *_args: None,
            ip=self._ip,
            rack=self._rack,
            slot=self._slot,
            db_number=self._db_number,
            poll_interval_ms=self._poll_interval_ms,
            reconnect_interval_ms=self._reconnect_interval_ms,
            auto_reset_bit=False,
            field_offsets_dict=dict(self._field_offsets_dict),
            plc_id=self._plc_id,
            monotonic_ns=self._monotonic_ns,
            isolate_result_io=False,
        )

    def _require_matching_plc_id(self, plc_id: object) -> int:
        validated = validate_plc_id(plc_id)
        if validated != self._plc_id:
            raise PlcResultBoundaryError(
                "plc_id",
                "plc_id does not match the target PLC service",
            )
        return validated

    def _write_boundary_field(
        self,
        field_name: str,
        value: PlcScalar,
    ) -> None:
        if self._configured_offset(field_name) is None:
            raise PlcResultBoundaryError(
                field_name,
                "PLC field offset is not configured",
            )
        try:
            wrote = self.write_field_value(field_name, value)
        except Exception:
            raise PlcResultBoundaryError(
                field_name,
                "PLC field write failed",
            ) from None
        if wrote is not True:
            raise PlcResultBoundaryError(
                field_name,
                "PLC field write did not complete",
            )

    def write_result(
        self,
        plc_id: int,
        payload: Mapping[str, PlcScalar],
    ) -> None:
        self._require_matching_plc_id(plc_id)
        copied_payload = validate_plc_result_payload(payload)
        if self.plc_client is None:
            raise PlcResultBoundaryError(
                "result_dispatch",
                "PLC result target is not connected",
            )

        for spec in PLC_RESULT_FIELDS:
            assert spec.result_payload_key is not None
            if self._configured_offset(spec.key) is None:
                continue
            self._write_boundary_field(
                spec.key,
                copied_payload[spec.result_payload_key],
            )
        if self._configured_offset("res_finish_signal_offset") is not None:
            self._write_boundary_field(
                "res_finish_signal_offset",
                copied_payload["finished"],
            )

    @pyqtSlot(int, bool)
    def write_testing_state(self, plc_id: int, testing: bool) -> None:
        self._require_matching_plc_id(plc_id)
        if type(testing) is not bool:
            raise PlcResultBoundaryError(
                "testing",
                "testing must be a bool",
            )
        if self.plc_client is None:
            raise PlcResultBoundaryError(
                "testing_dispatch",
                "PLC testing target is not connected",
            )
        self._write_boundary_field(
            "res_testing_signal_offset",
            testing,
        )

    def last_write_test_result_to_plc(self, plc_id, test_result):
        """Legacy direct entrypoint retained for compatibility but not wired."""
        self.write_result(plc_id, test_result)

    def last_write_limit_to_plc(self, plc_id, limit_values):
        """
        按 plc_id 写入当前 PLC 的机种和限值。
        limit_values = {
            "res_product_model_offset": "xxx",
            "res_spl_limit_up_offset": 0.0,
            "res_spl_limit_down_offset": 0.0,
            "res_loudness_limit_up_offset": 0.0,
            "res_loudness_limit_down_offset": 0.0,
            "res_vibrate_limit_up_offset": 0.0,
            "res_vibrate_limit_down_offset": 0.0,
            "res_ai_score_limit_down_offset": 0.0,
        }
        """
        if plc_id != self._plc_id:
            return
        if self.plc_client is None:
            return

        for spec in PLC_CONFIG_RETURN_FIELDS:
            if spec.key not in limit_values:
                continue
            if self._configured_offset(spec.key) is None:
                continue
            try:
                self._write_configured_field(
                    spec.key,
                    limit_values[spec.key],
                )
            except Exception as exc:
                self._log_field_failure(
                    "write configuration return",
                    spec.key,
                    exc,
                )
                return

        self.bus.log.emit(f"PLC {self._plc_id} 写入限值完成")

    def read_plc_values(self, *field_names) -> dict:
        values = {}

        for field_name in field_names:
            if self._configured_offset(field_name) is None:
                continue

            try:
                value = self.read_field_value(field_name)
            except Exception as exc:
                self._log_field_failure("read", field_name, exc)
                continue

            # 字符串为空或读取失败时不放进结果；False 和 0.0 是有效值，需要保留。
            if value == "" or value is None:
                continue
            values[field_name] = value
        return values

    def read_plc_limit(self):
        # PLC 连接成功后读取一次机种和各项上下限，后续测试可直接按 plc_id 取用。
        values = self.read_plc_values(
            *(spec.key for spec in PLC_CONFIG_RETURN_FIELDS)
        )
        self.bus.plc_limit_values_dict[self._plc_id] = values
        self.bus.log.emit(f"PLC {self._plc_id} 读取限值完成: {values}")

    def _start_value_timer(self):
        if self.value_timer is not None:
            return
        self.value_timer = QTimer(self)
        self.value_timer.setInterval(self.read_time_ms)
        self.value_timer.timeout.connect(self.read_plc_limit)
        self.value_timer.start()

    def _emit_plc_workorder(self):
        values = self.read_plc_values(
            *(spec.key for spec in PLC_WORKORDER_FIELDS)
        )
        payload = {
            spec.workorder_key: values[spec.key]
            for spec in PLC_WORKORDER_FIELDS
            if values.get(spec.key) not in (None, "")
        }
        snapshot = dict(payload)
        if payload:
            self.bus.plc_workorder_changed.emit(payload)
        return snapshot

    def poll_once(self):
        """
        轮询一次
        """
        # 单次轮询逻辑：未连接则尝试重连，已连接则读取位并判断是否出现上升沿。
        if self.plc_client is None:
            self._try_reconnect()
            return

        v = self._read_start_bit()
        if v is None:
            return

        self._poll_current_value()

        if self._last_value is None:
            # 第一次读到值时只记录，不触发“开始”。
            self._last_value = v
            return

        # 只在 0 -> 1 时触发一次，避免 PLC 保持 1 时重复触发。
        rising = (self._last_value is False) and (v is True)
        self._last_value = v

        if not rising:
            # 未检测到信号
            return
        trigger_monotonic_ns = self._monotonic_ns()
        if type(trigger_monotonic_ns) is not int or trigger_monotonic_ns <= 0:
            self.bus.log.emit(
                f"PLC{self._plc_id}开始触发时间无效，已拒绝本次启动"
            )
            self.logger.error(
                f"PLC{self._plc_id} invalid trigger_monotonic_ns"
            )
            return
        self.bus.log.emit(f"PLC {self._plc_id} 检测到开始信号")
        self.logger.info(f"PLC {self._plc_id} 检测到开始信号")
        workorder = self._emit_plc_workorder()
        try:
            self._on_start_requested(
                self._plc_id,
                workorder,
                trigger_monotonic_ns,
            )
        except Exception as exc:
            self.bus.log.emit(f"PLC{self._plc_id}开始回调失败: {exc}")
            self.logger.error(f"PLC{self._plc_id}开始回调失败: {exc}")

        # 如果需要，触发后自动清零，方便 PLC 下次再次置 1 触发。
        if self._auto_reset_bit:
            self.write_field_value("start_signal_offset", False)

