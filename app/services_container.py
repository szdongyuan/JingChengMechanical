from __future__ import annotations

from dataclasses import dataclass, field
from PyQt6.QtCore import QObject, pyqtSignal

from app.service.ai_service import AiService
from app.service.analysis.test_manager import TestManager
from app.service.config_service import ConfigService
from app.service.db_connection_service import DbConnectionService
from app.service.hardwareselect_service import HardwareSelectService
from app.service.input_calibration_service import InputCalibrationService


@dataclass(frozen=True)
class FactoryControllerState:
    selected_config_ids: tuple[int, ...] = ()
    manual_primary_config_id: int | None = None
    active_config_id: int | None = None
    displayed_config_id: int | None = None
    run_phase: str = "not_ready"
    trigger_source: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.run_phase != "not_ready"


@dataclass(frozen=True)
class FactoryReviewWindow:
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class FactoryReviewPayload:
    displayed_config_id: int | None = None
    run_id: str | None = None
    trigger_source: str | None = None
    captured_duration_sec: float | None = None
    default_window: FactoryReviewWindow | None = None
    fallback_window: FactoryReviewWindow | None = None
    manual_reanalysis_allowed: bool = False
    no_result: bool = False
    error_message: str | None = None
    judgment_payload: dict | None = None


@dataclass(frozen=True)
class FactoryPlcIngressPayload:
    plc_device_key: str


@dataclass(frozen=True)
class FactoryScanIngressPayload:
    scan_text: str


@dataclass(frozen=True)
class FactoryRuntimeStartPayload:
    config_id: int
    run_id: str
    trigger_source: str


@dataclass(frozen=True)
class FactoryRuntimeConfigPayload:
    config_id: int
    run_id: str


@dataclass(frozen=True)
class FactoryRuntimeAbortPayload:
    config_id: int
    run_id: str
    trigger_source: str | None = None
    error_message: str | None = None


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

    # ── Analysis kernel real-time signals ──
    # Real-time analysis frames (~20 Hz per channel)
    stft_updated = pyqtSignal(int, int, object)     # config_id, channel_idx, StftFrame
    spl_updated = pyqtSignal(int, int, object)      # config_id, channel_idx, SplFrame
    octave_updated = pyqtSignal(int, int, object)   # config_id, channel_idx, OctaveFrame
    fft_updated = pyqtSignal(int, int, object)      # config_id, channel_idx, FftFrame

    # Session lifecycle
    recording_progress = pyqtSignal(int, float)     # config_id, elapsed_sec
    test_started = pyqtSignal(int)                  # config_id
    test_finished = pyqtSignal(int, dict)           # config_id, JudgmentResult as dict

    # ── Factory-mode ingress and controller contracts ──
    factory_plc_event = pyqtSignal(object)                  # FactoryPlcIngressPayload
    factory_scan_event = pyqtSignal(object)                 # FactoryScanIngressPayload
    factory_runtime_start_accepted = pyqtSignal(object)     # FactoryRuntimeStartPayload
    factory_runtime_analyzing_entered = pyqtSignal(object)  # FactoryRuntimeConfigPayload
    factory_runtime_auto_analysis_succeeded = pyqtSignal(object)  # FactoryReviewPayload
    factory_runtime_auto_analysis_no_result = pyqtSignal(object)  # FactoryReviewPayload
    factory_runtime_auto_analysis_error = pyqtSignal(object)      # FactoryReviewPayload
    factory_runtime_aborted = pyqtSignal(object)                  # FactoryRuntimeAbortPayload
    factory_runtime_reanalysis_completed = pyqtSignal(object)     # FactoryReviewPayload
    factory_state_changed = pyqtSignal(object)              # FactoryControllerState
    factory_review_changed = pyqtSignal(object)             # FactoryReviewPayload
    factory_display_reset = pyqtSignal(str)                 # reset reason


@dataclass
class ServiceContainer:
    """
    暂时不创建多个ServiceContainer
    """
    bus: EventBus = field(default_factory=EventBus)

    def __post_init__(self):
        self.test = TestManager(self.bus, self)
        # 数据库连接服务（长连接）
        self.db = DbConnectionService()
        self.db.connect()

        # 设备服务（刷新/枚举/保存输入输出设备）
        self.hss_service = HardwareSelectService()

        # 输入校准服务（后续扩展）
        self.input_calibration_service = InputCalibrationService(self.bus)

        # 配置服务
        self.config_service = ConfigService(self.bus)

        self.ai_service = AiService()

        from app.service.factory_mode_controller import FactoryModeController

        self.factory_mode = FactoryModeController(self.bus, self)
