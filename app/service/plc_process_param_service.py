from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
import snap7
import snap7.util

from app.service.log_manager import LogManager


class PlcProcessParamService(QObject):
    """工艺参数 PLC 连接服务，只负责连接和断线重连。"""

    connect_result = pyqtSignal(bool, str)
    state_changed = pyqtSignal(str)

    def __init__(
        self,
        bus,
        ip: str,
        db_number: int = 1,
        reconnect_interval_ms: int = 3000,
        rack: int = 0,
        slot: int = 1,
    ):
        super().__init__()
        self.bus = bus
        self.logger = LogManager.set_log_handler("core")
        self._ip = ip
        self._rack = int(rack)
        self._slot = int(slot)
        self._db_number = int(db_number)
        self._reconnect_interval_ms = int(reconnect_interval_ms)

        self.plc_client = None
        self.timer = None
        self._reconnect_timer_ms = 0
        self._last_connect_error = ""

    @pyqtSlot()
    def start_service(self):
        # 服务运行在自己的 QThread 里；如果 timer 已存在，说明已经启动过。
        if self.timer is not None:
            return

        self.state_changed.emit("连接中")
        # 启动时先尝试连一次，失败也不退出，后面由 timer 持续重连。
        self._connect_once()

        # 固定每 1 秒检查一次是否需要重连；真正重连频率由 _reconnect_interval_ms 控制。
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll_once)
        self.timer.start()

        if self.plc_client is None:
            self.state_changed.emit("重连中")
            self.connect_result.emit(False, self._last_connect_error or "工艺 PLC 连接失败")
        else:
            self.connect_result.emit(True, "")

    @pyqtSlot()
    def stop_service(self):
        # 停止后台重连循环，并主动断开 PLC。
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        self._disconnect()
        self.state_changed.emit("未连接")
        self.bus.log.emit("工艺 PLC 停止连接")
        self.logger.info("工艺 PLC 停止连接")

    def poll_once(self):
        """
        每隔一秒检测通过读一个字节检测是否在线
        """
        if self.plc_client is None:
            self._try_reconnect()
            return

        try:
            # 简单心跳：定时读 1 个字节，读失败就认为断线并进入重连。
            self.plc_client.db_read(self._db_number, 0, 1)
        except Exception as exc:
            self._disconnect()
            self.state_changed.emit("重连中")
            self.bus.log.emit(f"工艺 PLC 读取失败，准备重连: {exc}")
            self.logger.error(f"工艺 PLC 读取失败，准备重连: {exc}")

    def _connect_once(self):
        """
        连接一次plc工艺表
        """
        # 单次连接尝试；失败只记录错误，不抛出，避免线程退出。
        if self.plc_client is not None:
            return

        try:
            client = snap7.client.Client()
            client.connect(self._ip, self._rack, self._slot)
            self.plc_client = client
            self._reconnect_timer_ms = 0
            self._last_connect_error = ""
            self.state_changed.emit("已连接")
            self.bus.log.emit(f"工艺 PLC 连接成功: {self._ip}, DB{self._db_number}")
            self.logger.info(f"工艺 PLC 连接成功: {self._ip}, DB{self._db_number}")
        except Exception as exc:
            self.plc_client = None
            self._last_connect_error = str(exc)
            self.state_changed.emit("重连中")
            self.bus.log.emit(f"工艺 PLC 连接失败: {exc}")
            self.logger.error(f"工艺 PLC 连接失败: {exc}")

    def _disconnect(self):
        # 安静断开，供手动停止或重启配置时使用。
        if self.plc_client is None:
            return
        try:
            self.plc_client.disconnect()
        except Exception:
            pass
        self.plc_client = None

    def read_process_params(self, offset_dict):
        """
        读工艺plc的各个位置的值
        """
        values = {}
        if self.plc_client is None:
            return values
        for field_name, offset in offset_dict.items():
            if not offset:
                continue
            byte_index = int(str(offset).split(".", 1)[0])
            data = self.plc_client.db_read(self._db_number, byte_index, 4)
            values[field_name] = round(snap7.util.get_real(data, 0), 3)
        return values

    def _try_reconnect(self):
        # 使用累计时间实现“每 N 毫秒重连一次”，避免每秒都打 PLC。
        if self.timer is None:
            return

        self._reconnect_timer_ms += self.timer.interval()
        if self._reconnect_timer_ms < self._reconnect_interval_ms:
            return
        self._reconnect_timer_ms = 0
        self._connect_once()
