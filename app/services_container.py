from __future__ import annotations

from dataclasses import dataclass, field
from PyQt6.QtCore import QObject, pyqtSignal

from app.service.ai_service import AiService
from app.service.analysis.test_manager import TestManager
from app.service.config_service import ConfigService
from app.service.db_connection_service import DbConnectionService
from app.service.factory_selection_settings import FactorySelectionSettings
from app.service.hardwareselect_service import HardwareSelectService
from app.service.input_calibration_service import InputCalibrationService
from app.service.plc_service import PlcService


@dataclass(frozen=True)
class FactoryControllerState:
    selected_config_ids: tuple[int, ...] = ()
    active_config_id: int | None = None
    displayed_config_id: int | None = None
    run_phase: str = "not_ready"
    trigger_source: str | None = None
    selection_ready: bool | None = None
    selection_ready_reason: str | None = None
    start_ready: bool | None = None
    start_ready_reason: str | None = None

    _BLOCKED_START_PHASE_REASONS = {
        "running": "当前测试运行中",
        "analyzing": "当前测试分析中",
        "reanalyzing": "当前正在重新分析",
    }
    _START_READY_SELECTION_PHASES = frozenset({"idle", "armed", "reviewing"})

    def __post_init__(self):
        selection_ready = self.selection_ready
        selection_ready_reason = self.selection_ready_reason
        start_ready = self.start_ready
        start_ready_reason = self.start_ready_reason

        if selection_ready is None:
            selection_ready, derived_reason = self._derive_selection_readiness()
            if selection_ready_reason is None:
                selection_ready_reason = derived_reason
        elif not selection_ready and selection_ready_reason is None:
            _, selection_ready_reason = self._derive_selection_readiness()

        if start_ready is None:
            start_ready, derived_reason = self._derive_start_readiness(
                selection_ready=bool(selection_ready),
                selection_ready_reason=selection_ready_reason,
            )
            if start_ready_reason is None:
                start_ready_reason = derived_reason
        elif not start_ready and start_ready_reason is None:
            _, start_ready_reason = self._derive_start_readiness(
                selection_ready=bool(selection_ready),
                selection_ready_reason=selection_ready_reason,
            )

        object.__setattr__(self, "selection_ready", bool(selection_ready))
        object.__setattr__(
            self,
            "selection_ready_reason",
            None if selection_ready else selection_ready_reason,
        )
        object.__setattr__(self, "start_ready", bool(start_ready))
        object.__setattr__(
            self,
            "start_ready_reason",
            None if start_ready else start_ready_reason,
        )

    def _derive_selection_readiness(self) -> tuple[bool, str | None]:
        selected = tuple(self.selected_config_ids)
        if not selected:
            return False, "请至少启用一个配置"
        return True, None

    def _derive_start_readiness(
        self,
        *,
        selection_ready: bool,
        selection_ready_reason: str | None,
    ) -> tuple[bool, str | None]:
        blocked_phase_reason = self._BLOCKED_START_PHASE_REASONS.get(self.run_phase)
        if blocked_phase_reason is not None:
            return False, blocked_phase_reason
        if not selection_ready:
            return False, selection_ready_reason
        if self.run_phase in self._START_READY_SELECTION_PHASES:
            return True, None
        if self.run_phase not in {"armed", "reviewing"}:
            return False, "工厂模式未就绪"
        return True, None

    @property
    def is_ready(self) -> bool:
        return self.selection_ready


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
    summary_payload: dict | None = None
    chart_payload: dict | None = None


@dataclass(frozen=True)
class FactoryPlcIngressPayload:
    plc_device_key: str


@dataclass(frozen=True)
class FactoryScanIngressPayload:
    scan_text: str


@dataclass(frozen=True)
class FactoryStartBlockedPayload:
    reason: str
    trigger_source: str


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
    stft_updated = pyqtSignal(int, str, int, object)     # config_id, run_id, channel_idx, StftFrame
    spl_updated = pyqtSignal(int, str, int, object)      # config_id, run_id, channel_idx, SplFrame
    octave_updated = pyqtSignal(int, str, int, object)   # config_id, run_id, channel_idx, OctaveFrame
    fft_updated = pyqtSignal(int, str, int, object)      # config_id, run_id, channel_idx, FftFrame

    # Session lifecycle
    recording_progress = pyqtSignal(int, float)     # config_id, elapsed_sec
    test_started = pyqtSignal(int)                  # config_id
    test_finished = pyqtSignal(int, dict)           # config_id, JudgmentResult as dict

    # ── Factory-mode ingress and controller contracts ──
    factory_plc_event = pyqtSignal(object)                  # FactoryPlcIngressPayload
    factory_scan_event = pyqtSignal(object)                 # FactoryScanIngressPayload
    factory_start_blocked = pyqtSignal(object)              # FactoryStartBlockedPayload
    factory_selection_notice = pyqtSignal(str)
    factory_runtime_start_accepted = pyqtSignal(object)     # FactoryRuntimeStartPayload
    factory_runtime_analyzing_entered = pyqtSignal(object)  # FactoryRuntimeConfigPayload
    factory_runtime_auto_analysis_succeeded = pyqtSignal(object)  # FactoryReviewPayload
    factory_runtime_auto_analysis_no_result = pyqtSignal(object)  # FactoryReviewPayload
    factory_runtime_auto_analysis_error = pyqtSignal(object)      # FactoryReviewPayload
    factory_runtime_auto_analysis_cleanup_settled = pyqtSignal(object)  # FactoryRuntimeConfigPayload
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

        # 设备服务（刷新/枚举/保存输入监听设备）
        self.hss_service = HardwareSelectService()

        # 输入校准服务（后续扩展）
        self.input_calibration_service = InputCalibrationService(self.bus)

        # 配置服务
        self.config_service = ConfigService(self.bus)

        self.ai_service = AiService()
        self.factory_selection_settings = FactorySelectionSettings()

        from app.service.factory_mode_controller import FactoryModeController

        self.factory_mode = FactoryModeController(self.bus, self)

        # PLC 对接说明：
        # 1. 程序启动时自动尝试连接一次 PLC。
        # 2. 然后持续轮询下面这个“开始信号位”。
        # 3. 读到 0 -> 1 时，就触发 self.test.start(...)。
        # self.plc = PlcService(
        #     bus=self.bus,
        #     on_start_requested=self._start_from_plc,
        #     ip="192.168.0.1",  # PLC 的 IP 地址
        #     rack=0,  # S7-1200 常用 rack=0  机架号
        #     slot=1,  # S7-1200 常用 slot=1  槽号
        #     db_number=1,  # 数据块号DB，例如 DB1
        #     byte_index=0,  # 字节偏移，例如 DB1.DBB0
        #     bit_index=0,  # 位偏移，例如 DB1.DBX0.0
        #     poll_interval_ms=200,  # 每 200ms 轮询一次
        #     reconnect_interval_ms=3000,  # 断线后每 3 秒重连一次
        #     auto_reset_bit=True,  # 触发后自动写回 0
        # )
        # self.plc.start()

    def _start_from_plc(self):
        self.bus.log.emit("[PLC] 触发开始 -> 执行测试开始逻辑")
        self.test.start({})