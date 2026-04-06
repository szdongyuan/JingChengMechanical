from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer
import snap7
import snap7.util

class PlcService(QObject):
    """
    用 python-snap7 轮询 PLC 开始信号。

    逻辑：
    1. 程序启动时先连接一次 PLC。
    2. 用 QTimer 周期读取 PLC 的开始位。
    3. 当该位从 0 变成 1 时，触发开始回调。
    """

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
        self._on_start_requested = on_start_requested

        # PLC 连接参数
        self._ip = ip
        self._rack = rack
        self._slot = slot

        # 开始信号位，例如 DB1.DBX0.0
        self._db_number = int(db_number)
        self._byte_index = int(byte_index)
        self._bit_index = int(bit_index)

        # 轮询周期 / 重连周期
        self._poll_interval_ms = int(poll_interval_ms)
        self._reconnect_interval_ms = int(reconnect_interval_ms)

        # 触发开始后，是否把 PLC 里的开始位自动写回 0
        self._auto_reset_bit = bool(auto_reset_bit)

        # 运行时状态
        self.plc_client = None
        self.timer = None
        self._last_value = None
        self._reconnect_timer_ms = 0  # 重连计时器

    def start(self):
        # 启动 PLC 监听；如果已经启动过，就直接返回，避免重复创建定时器。
        print("start")
        if self.timer is not None:
            return
        print("start1")
        # 启动时先尝试连一次 PLC。
        self._connect_once()

        # 创建轮询定时器，按固定周期读取 PLC 开始位。
        self.timer = QTimer(self)
        self.timer.setInterval(self._poll_interval_ms)
        self.timer.timeout.connect(self.poll_once)
        self.timer.start()

        self.bus.log.emit(
            f"PLC开始监听: ip={self._ip}, rack={self._rack}, slot={self._slot}, "
            f"DB{self._db_number}.DBX{self._byte_index}.{self._bit_index}"
        )

    def stop(self):
        # 停止监听，销毁定时器并主动断开 PLC 连接。
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        self._disconnect()
        self._last_value = None
        self.bus.log.emit("[PLC] 停止监听")

    def simulate_rising_edge(self):
        # 本地调试入口：不依赖真实 PLC，直接模拟一次“开始信号上升沿”。
        print("PLC一次开始信号")
        self.bus.log.emit("PLC一次开始信号")
        try:
            self._on_start_requested()
        except Exception as exc:
            self.bus.log.emit(f"[PLC] 开始回调失败: {exc}")

    def _connect_once(self):
        # 尝试建立一次 PLC 连接；如果当前已经连上，就不重复连接。
        if self.plc_client is not None:
            return

        try:
            client = snap7.client.Client()
            client.connect(self._ip, self._rack, self._slot)
            self.plc_client = client
            self._reconnect_timer_ms = 0
            self.bus.log.emit(f"[PLC] 连接成功: {self._ip}")
        except Exception as exc:
            self.plc_client = None
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

    def _try_reconnect(self):
        print("触发重新连接")
        # 按重连间隔尝试重新连接，避免每次轮询失败都立刻重连。
        if self.timer is None:
            return

        # 不要每次轮询都立刻重连，按配置的重连间隔节流。
        self._reconnect_timer_ms += self._poll_interval_ms
        print(self._reconnect_timer_ms)
        if self._reconnect_timer_ms < self._reconnect_interval_ms:
            return
        print("重新连接")
        self._reconnect_timer_ms = 0
        self._connect_once()

    def _read_start_bit(self) -> bool | None:
        # 读取 PLC 中配置的开始位；读失败时返回 None，并断开等待重连。
        if self.plc_client is None:
            return None

        try:
            data = self.plc_client.db_read(self._db_number, self._byte_index, 1)
            return bool(snap7.util.get_bool(data, 0, self._bit_index))
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
        print("轮询")
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
        print(f"rising: {rising}")
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

