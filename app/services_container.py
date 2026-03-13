from __future__ import annotations

from dataclasses import dataclass
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from app.service.db_connection_service import DbConnectionService
from app.service.hardwareselect_service import HardwareSelectService
from app.service.input_calibration_service import InputCalibrationService
from app.service.config_service import ConfigService


class EventBus(QObject):
    """
    模块间通信的“信号总线”。

    约定：UI 模块不要直接互相调用/引用，而是通过 bus 发信号并连接到槽函数（connect），
    这样后续平铺/弹窗/删模块都不会牵一发动全身。
    """

    # 输出日志
    log = pyqtSignal(str)

    # 状态栏文本
    status = pyqtSignal(str)

    # 工单变化（ControlModule 监听该信号并在槽函数缓存工单；也可用于触发重置/重新计算）
    workorder_changed = pyqtSignal(dict)

    # 测试结果变化（ResultModule 监听该信号并在槽函数刷新结果摘要）
    result_changed = pyqtSignal(dict)
    # 刷新通道
    refresh_channels = pyqtSignal()

    # 配置变化（供外部模块监听，如工单模块刷新可选配置列表）
    config_changed = pyqtSignal()


class TestService(QObject):
    """
    演示用的“假测试服务”。
    目的：让 UI 联动跑起来（按钮 -> 状态/日志 -> 结果摘要刷新）。
    你接入真实流程时，一般会把 start/stop 替换成：
    - 启动采集/播放/分析线程（QThread/QRunnable）
    - 持续发日志、状态、分析结果到 bus
    """

    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        self._timer: QTimer | None = None
        self._tick_count = 0
        self._last_workorder: dict = {}

    def start(self, workorder: dict):
        # 重入保护：重复点击“开始”先停止上一次
        self.stop(silent=True)

        self._tick_count = 0
        self._last_workorder = workorder or {}

        # 通知 UI：进入测试状态 + 打印日志
        self.bus.status.emit("Testing...")
        self.bus.log.emit(f"[Test] started, workorder={self._last_workorder}")

        # 用定时器模拟“测试进行中”
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def _on_tick(self):
        self._tick_count += 1
        self.bus.log.emit(f"[Test] running... t={self._tick_count}")

        # 模拟 3 秒后产出一份结果
        if self._tick_count >= 6:
            result = {
                "overall": "不合格",
                "ai_score": 89.1,
                "spl_db": 72.5,
            }

            # 通知 UI：结果更新 + 状态恢复
            self.bus.result_changed.emit(result)
            self.bus.log.emit(
                f"[Test] stopped. Result: FAIL (Score: {result['ai_score']})"
            )
            self.bus.status.emit("Idle")

            self.stop(silent=True)

    def stop(self, silent: bool = False):
        # 停止模拟计时器（真实实现里一般是停止线程/设备）
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

        if not silent:
            self.bus.log.emit("[Test] stop")

        self.bus.status.emit("Stopped")

@dataclass
class ServiceContainer:
    """
    暂时不创建多个ServiceContainer
    """
    bus = EventBus()

    def __post_init__(self):
        self.test = TestService(self.bus)
        # 数据库连接服务（长连接）
        self.db = DbConnectionService()
        self.db.connect()

        # 设备服务（刷新/枚举/保存输入输出设备）
        self.hss_service = HardwareSelectService()

        # 输入校准服务（后续扩展）
        self.input_calibration_service = InputCalibrationService(self.bus)

        # 配置服务
        self.config_service = ConfigService(self.bus)