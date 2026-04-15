from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
import snap7
import snap7.util

from app.service.log_manager import LogManager


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

    @classmethod
    def validate_connection_config(
        cls,
        *,
        ip,
        rack,
        slot,
        db_number,
        byte_index,
        bit_index,
        poll_interval_ms,
        reconnect_interval_ms,
    ) -> tuple[dict[str, int | str] | None, str | None]:
        missing_fields = []
        invalid_fields = []
        normalized = {}

        ip_text = "" if ip is None else str(ip).strip()
        if not ip_text:
            missing_fields.append("IP")
        else:
            normalized["ip"] = ip_text

        numeric_fields = (
            ("机架号", "rack", rack),
            ("槽号", "slot", slot),
            ("数据块号", "db_number", db_number),
            ("字节偏移", "byte_index", byte_index),
            ("位偏移", "bit_index", bit_index),
            ("轮询时间", "poll_interval_ms", poll_interval_ms),
            ("断线重连时间", "reconnect_interval_ms", reconnect_interval_ms),
        )
        for label, key, value in numeric_fields:
            if value is None or (isinstance(value, str) and not value.strip()):
                missing_fields.append(label)
                continue
            try:
                normalized[key] = int(value)
            except (TypeError, ValueError):
                invalid_fields.append(label)

        if missing_fields:
            return None, f"PLC 配置不完整: {'、'.join(missing_fields)}"
        if invalid_fields:
            return None, f"PLC 配置无效: {'、'.join(invalid_fields)}"
        return normalized, None

    def __init__(self,
        bus,
        on_start_requested,
        ip: str,
        rack: int = 0,
        slot: int = 1,
        db_number: int = 1,
        byte_index: int = 0,
        bit_index: int = 0,
        poll_interval_ms = 200,
        reconnect_interval_ms = 3000,
        auto_reset_bit = True,
    ):
        # 初始化 PLC 服务，保存连接参数、轮询参数和运行时状态。
        super().__init__()
        self.bus = bus
        self.logger = LogManager.set_log_handler("core")
        self._on_start_requested = on_start_requested

        # PLC 连接参数(IP，机架号，槽号）
        self._ip = ip
        self._rack = rack
        self._slot = slot

        # 数据块号DB
        self._db_number = db_number
        # 数据块里的第几个字节
        self._byte_index = byte_index
        # 字节里的第几位
        self._bit_index = bit_index
        # 轮询周期
        self._poll_interval_ms = poll_interval_ms
        # 重连周期
        self._reconnect_interval_ms = reconnect_interval_ms

        # 触发开始后，是否把 PLC 里的开始位自动写回 0
        self._auto_reset_bit = bool(auto_reset_bit)

        # 与PLC 通信的客户端实例
        self.plc_client = None
        self.timer = None
        self._last_value = None
        self._reconnect_timer_ms = 0  # 重连计时器
        self._last_connect_error = ""

    def _apply_normalized_connection_config(self) -> str | None:
        normalized, error = self.validate_connection_config(
            ip=self._ip,
            rack=self._rack,
            slot=self._slot,
            db_number=self._db_number,
            byte_index=self._byte_index,
            bit_index=self._bit_index,
            poll_interval_ms=self._poll_interval_ms,
            reconnect_interval_ms=self._reconnect_interval_ms,
        )
        if error is not None:
            return error

        self._ip = normalized["ip"]
        self._rack = normalized["rack"]
        self._slot = normalized["slot"]
        self._db_number = normalized["db_number"]
        self._byte_index = normalized["byte_index"]
        self._bit_index = normalized["bit_index"]
        self._poll_interval_ms = normalized["poll_interval_ms"]
        self._reconnect_interval_ms = normalized["reconnect_interval_ms"]
        return None

    @pyqtSlot()
    def start_plc_service(self):
        # 启动 PLC 监听；如果已经启动过，就直接返回，避免重复创建定时器。
        self.logger.info(f"启动 PLC 服务")
        if self.timer is not None:
            return
        config_error = self._apply_normalized_connection_config()
        if config_error is not None:
            self._last_connect_error = config_error
            self.logger.warning(f"[PLC] 启动失败: {config_error}")
            self.bus.log.emit(f"[PLC] 启动失败: {config_error}")
            self.state_changed.emit("未连接")
            self.connect_result.emit(False, config_error)
            return
        self.state_changed.emit("连接中")
        # 启动时先尝试连一次 PLC。
        self._connect_once()
        if self.plc_client is None:
            self.state_changed.emit("未连接")
            self.connect_result.emit(False, self._last_connect_error or "PLC 连接失败")
            return

        # 创建轮询定时器，按固定周期读取 PLC 开始位。
        self.timer = QTimer(self)
        self.timer.setInterval(self._poll_interval_ms)
        self.timer.timeout.connect(self.poll_once)
        self.timer.start()
        self.connect_result.emit(True, "")

        self.bus.log.emit(
            f"PLC开始监听: ip={self._ip}, rack={self._rack}, slot={self._slot}, "
            f"DB{self._db_number}.DBX{self._byte_index}.{self._bit_index}"
        )

    @pyqtSlot()
    def stop_plc_service(self):
        self.logger.info(f"停止 PLC 服务")
        # 停止监听，销毁定时器并主动断开 PLC 连接。
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        self._disconnect()
        self.state_changed.emit("未连接")
        self._last_value = None
        self.bus.log.emit("[PLC] 停止监听")

    def _connect_once(self):
        self.logger.info("开始尝试连接 PLC")
        # 尝试建立一次 PLC 连接；如果当前已经连上，就不重复连接。
        if self.plc_client is not None:
            self.logger.info("已有 PLC 客户端，不重复连接")
            return

        try:
            client = snap7.client.Client()
            client.connect(self._ip, self._rack, self._slot)
            self.logger.info(
                f"PLC连接参数: ip={self._ip}, rack={self._rack}, slot={self._slot}"
            )
            self.logger.info(
                f"PLC地址参数: db_number数据块号={self._db_number}, 字节偏移={self._byte_index}, 位偏移={self._bit_index}"
            )
            self.bus.log.emit(
                f"PLC连接参数: ip={self._ip}, rack={self._rack}, slot={self._slot}"
            )
            self.bus.log.emit(
                 f"PLC地址参数: db_number数据块号={self._db_number}, 字节偏移={self._byte_index}, 位偏移={self._bit_index}"
            )
            self.plc_client = client
            self._reconnect_timer_ms = 0
            self._last_connect_error = ""
            self.state_changed.emit("已连接")
            self.logger.info(f"[PLC] 连接成功: {self._ip}")
            self.bus.log.emit(f"[PLC] 连接成功: {self._ip}")
        except Exception as exc:
            self.plc_client = None
            self._last_connect_error = str(exc)
            self.logger.error(f"[PLC] 连接失败: {exc}")
            self.bus.log.emit(f"[PLC] 连接失败: {exc}")

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

    def _try_reconnect(self):
        self.logger.info("触发重新连接")
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
            bytes = self.plc_client.db_read(self._db_number, self._byte_index, 1)
            # 取第几位
            data = snap7.util.get_bool(bytes, 0, self._bit_index)
            return bool(data)
        except Exception as exc:
            self._disconnect()
            self.bus.log.emit(f"[PLC] 读取失败: {exc}")
            return None

    def _write_start_bit(self, value: bool) -> bool:
        # 向 PLC 写入开始位，通常用于触发后自动复位为 0。
        if self.plc_client is None:
            return False

        try:
            data = bytearray(1)
            snap7.util.set_bool(data, 0, self._bit_index, bool(value))
            self.plc_client.db_write(self._db_number, self._byte_index, data)
            return True
        except Exception as exc:
            self._disconnect()
            self.bus.log.emit(f"[PLC] 写入失败: {exc}")
            return False

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

        self.bus.log.emit("[PLC] 检测到开始信号")
        try:
            self._on_start_requested()
        except Exception as exc:
            self.bus.log.emit(f"[PLC] 开始回调失败: {exc}")

        # 如果需要，触发后自动清零，方便 PLC 下次再次置 1 触发。
        if self._auto_reset_bit:
            self._write_start_bit(False)

