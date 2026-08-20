from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PyQt6.QtCore import QMetaObject, QObject, Qt, pyqtSignal

from app.service.ai_service import AiService
from app.service.audio_data_service import AudioDataService
from app.service.config_package_service import ConfigPackageService
from app.service.config_service import ConfigService
from app.service.db_connection_service import DbConnectionService
from app.service.factory_selection_settings import FactorySelectionSettings
from app.service.fault_types import (
    decode_fault_types_json,
    encode_history_labels_payload,
)
from app.service.hardwareselect_service import HardwareSelectService
from app.service.factory_review_payloads import (
    FactoryReviewPayload,
    FactoryReviewWindow,
    ImportAudioWarning,
)
from app.service.historical_metric_report_service import HistoricalMetricReportService
from app.service.input_calibration_service import InputCalibrationService
from app.service.multi_run_coordinator_mvp.contracts import (
    ABSENT,
    CapturedStartRequest,
    FrozenPayload,
    HistoryFactKind,
    ImportedStartRequest,
    ProjectedHistoryEvent,
    StartStatus,
    TriggerSource,
)
from app.service.multi_run_integration.captured_start_dispatcher import (
    CapturedStartDispatcher,
)
from app.service.multi_run_integration.shutdown import (
    ApplicationShutdownOwner,
    ApplicationShutdownResources,
    PlcServiceThreadShutdownOwner,
)
from app.service.plc_result_sequencer import (
    PlcAttemptOutcome,
    PlcResultAttemptReport,
    PlcResultSequencer,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.data_management import RunDataManagementService
from app.service.run_persistence.export_reader import RunExportReader
from app.service.run_persistence.startup import settle_startup
from app.service.run_record_export.archive import RunArchiveService
from app.service.result_history import ResultHistoryStore
from consts.running_consts import base_dir


def _require_positive_monotonic_ns(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a strict positive integer")
    return value


def _canonical_payload(value_id: str, value: object) -> FrozenPayload:
    return FrozenPayload(
        value_id=value_id,
        canonical_bytes=json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class _LivePlcMapping(MutableMapping[int, object]):
    """Stable mapping identity whose contents follow UI replacements."""

    def __init__(self, initial: Mapping[int, object]) -> None:
        self._values = dict(initial)

    def __getitem__(self, key: int) -> object:
        return self._values[key]

    def __setitem__(self, key: int, value: object) -> None:
        self._values[key] = value

    def __delitem__(self, key: int) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[int]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def replace(self, values: Mapping[int, object]) -> None:
        if not isinstance(values, Mapping):
            raise TypeError("PLC owner replacement must be a mapping")
        replacement = dict(values)
        self._values.clear()
        self._values.update(replacement)


class _CommittedHistoryDelivery(QObject):
    """Queue one observer delivery onto the observer's construction thread."""

    _queued_event = pyqtSignal(object)

    def __init__(
        self,
        observer: Callable[[object], None],
        diagnostic: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._observer = observer
        self._diagnostic = diagnostic
        self._queued_event.connect(self._deliver)

    def publish(self, event: ProjectedHistoryEvent) -> None:
        try:
            self._queued_event.emit(event)
        except Exception as exc:
            self._diagnose(exc)

    def _deliver(self, event: object) -> None:
        try:
            self._observer(event)
        except Exception as exc:
            self._diagnose(exc)

    def _diagnose(self, error: Exception) -> None:
        detail = str(error).replace("\x00", "")
        message = (
            "committed history observer failed: "
            f"{type(error).__name__}: {detail}"
        )
        bounded = message.encode("utf-8")[:512].decode(
            "utf-8",
            errors="ignore",
        )
        try:
            self._diagnostic(bounded)
        except Exception:
            pass


_COMMITTED_STATISTICS_HISTORY_KINDS = frozenset(
    {
        HistoryFactKind.RUN_TERMINATED,
        HistoryFactKind.TERMINAL_RESULT_REPLACED,
    }
)


class _PostDurableHistorySink:
    """Fan out committed statistics events without allowing it to gate history."""

    def __init__(
        self,
        primary: object,
        observer: Callable[[object], None],
        diagnostic: Callable[[str], None],
    ) -> None:
        self._primary = primary
        self._delivery = _CommittedHistoryDelivery(observer, diagnostic)

    def publish(self, event: object) -> None:
        self._primary.publish(event)
        if (
            type(event) is not ProjectedHistoryEvent
            or event.kind not in _COMMITTED_STATISTICS_HISTORY_KINDS
            or event.expected_revision is ABSENT
        ):
            return
        self._delivery.publish(event)


def _invoke_plc_thread_slot(
    service: object,
    thread: object,
    method_name: str,
) -> None:
    is_running = getattr(thread, "isRunning", None)
    if not callable(is_running) or is_running() is not True:
        raise RuntimeError("PLC thread is not running")
    QMetaObject.invokeMethod(
        service,
        method_name,
        Qt.ConnectionType.BlockingQueuedConnection,
    )


@dataclass(frozen=True)
class FactoryControllerState:
    selected_config_ids: tuple[int, ...] = ()
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
    _START_READY_SELECTION_PHASES = frozenset({"idle", "armed", "reviewing", "stopped"})

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
class FactoryPlcIngressPayload:
    plc_id: int
    trigger_monotonic_ns: int
    sn: str | None = None
    model: str | None = None
    rotation_speed: int | None = None

    def __post_init__(self) -> None:
        _require_positive_monotonic_ns(
            self.trigger_monotonic_ns,
            "trigger_monotonic_ns",
        )


@dataclass(frozen=True)
class FactoryScanIngressPayload:
    scan_text: str
    trigger_monotonic_ns: int

    def __post_init__(self) -> None:
        if type(self.scan_text) is not str or not self.scan_text.strip():
            raise ValueError("scan_text must be nonempty text")
        _require_positive_monotonic_ns(
            self.trigger_monotonic_ns,
            "trigger_monotonic_ns",
        )


@dataclass(frozen=True)
class FactoryStartBlockedPayload:
    reason: str
    trigger_source: str


@dataclass(frozen=True)
class FactoryRuntimeStartPayload:
    config_id: int
    run_id: str
    trigger_source: str
    plc_id: int | None = None


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


@dataclass(frozen=True)
class FactoryRuntimeStoppedPayload:
    config_id: int
    run_id: str
    trigger_source: str


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
    # 受管输入硬件资产发生变化 / 当前在硬件页选择的设备
    input_hardware_changed = pyqtSignal()
    input_hardware_selected = pyqtSignal(int)
    # 写入plc信号：plc_id, test_result
    write_test_result_sign = pyqtSignal(int, dict)
    # 写入 PLC 限值信号：plc_id, limit_values
    write_limit_sign = pyqtSignal(int, dict)
    request_plc_start_signal_reset = pyqtSignal(int)
    plc_start_signal_reset_result = pyqtSignal(int, bool, str)

    # 配置变化（供外部模块监听，如工单模块刷新可选配置列表）
    config_changed = pyqtSignal()
    plc_changed = pyqtSignal()
    plc_workorder_changed = pyqtSignal(dict)
    ai_toolchains_changed = pyqtSignal()

    # ── Analysis kernel real-time signals ──
    # Real-time analysis frames (~20 Hz per channel)
    stft_updated = pyqtSignal(int, str, int, object)     # config_id, run_id, channel_idx, StftFrame
    spl_updated = pyqtSignal(int, str, int, object)      # config_id, run_id, channel_idx, SplFrame
    loudness_updated = pyqtSignal(int, str, int, object)  # config_id, run_id, channel_idx, LoudnessFrame
    octave_updated = pyqtSignal(int, str, int, object)   # config_id, run_id, channel_idx, OctaveFrame
    fft_updated = pyqtSignal(int, str, int, object)      # config_id, run_id, channel_idx, FftFrame
    current_trace_updated = pyqtSignal(int, str, object)  # config_id, run_id, current_trace payload
    plc_current_sampled = pyqtSignal(int, float, float)  # plc_id, monotonic_time, current_a

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
    factory_runtime_stopped = pyqtSignal(object)                  # FactoryRuntimeStoppedPayload
    factory_runtime_aborted = pyqtSignal(object)                  # FactoryRuntimeAbortPayload
    factory_runtime_reanalysis_completed = pyqtSignal(object)     # FactoryReviewPayload
    factory_state_changed = pyqtSignal(object)              # FactoryControllerState
    factory_review_changed = pyqtSignal(object)             # FactoryReviewPayload
    factory_display_channel_changed = pyqtSignal(int, int)  # config_id, channel_index
    factory_display_reset = pyqtSignal(str)                 # reset reason
    run_records_deleted = pyqtSignal(object)


@dataclass
class ServiceContainer:
    """The single non-widget production composition root."""

    bus: EventBus = field(default_factory=EventBus)
    run_persistence_database: RunPersistenceDatabase | None = None
    run_persistence_path: str | Path | None = None
    monotonic_ns: Callable[[], int] = time.monotonic_ns
    production_frames_per_buffer: int = 512
    semantic_config: Mapping[str, object] | None = None
    production_composition_builder: object | None = None
    plc_services: Mapping[int, object] = field(default_factory=dict)
    plc_threads: Mapping[int, object] = field(default_factory=dict)
    plc_report_attempt: Callable[[object], None] | None = None
    result_history_store: ResultHistoryStore = field(
        default_factory=ResultHistoryStore,
        init=False,
    )
    result_history_adapter: object | None = field(default=None, init=False)

    preaccept_terminal_statuses = (
        StartStatus.PREFLIGHT_REJECTED,
        StartStatus.INVALID_FROZEN_INPUT,
        StartStatus.CAPTURE_CAPACITY,
        StartStatus.RESIDENT_UNAVAILABLE,
        StartStatus.ACTIVE_COMMIT_REJECTED,
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"plc_services", "plc_threads"}:
            current = self.__dict__.get(name)
            if type(current) is _LivePlcMapping:
                current.replace(value)
                return
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        self._application_shutdown_owner: ApplicationShutdownOwner | None = None
        self._captured_start_dispatcher: CapturedStartDispatcher | None = None
        self._shutdown_registration_closed = False
        self._registered_independent_shutdown_resources: list[object] = []
        self._committed_history_observer: Callable[[object], None] | None = None
        self.plc_services = _LivePlcMapping(self.plc_services)
        self.plc_threads = _LivePlcMapping(self.plc_threads)
        if not callable(self.monotonic_ns):
            raise ValueError("monotonic_ns must be callable")
        if self.production_frames_per_buffer != 512:
            raise ValueError("production frames_per_buffer must be exactly 512")
        if (
            self.run_persistence_database is not None
            and self.run_persistence_path is not None
        ):
            raise ValueError(
                "provide run_persistence_database or run_persistence_path, not both"
            )
        self.ingress_open = False
        self.startup_failure: str | None = None
        self.production_unavailable_reason: str | None = None
        self.final_composition: object | None = None
        self.prepared_production_composition: object | None = None
        self._production_cleanup_owner: object | None = None
        self.multi_run_coordinator: object | None = None
        self._prepared_config_routes: tuple[
            tuple[int, int | None], ...
        ] = ()
        self._production_composition_state = "unprepared"
        self._semantic_config = copy.deepcopy(dict(self.semantic_config or {}))
        self.bus.plc_limit_values_dict = {}
        self.pending_plc_start_signal_reset_ids: set[int] = set()
        self.bus.plc_start_signal_reset_result.connect(
            self._on_plc_start_signal_reset_result
        )
        # 数据库连接服务（长连接）
        self.db = DbConnectionService()
        try:
            self.db.connect()
        except Exception as exc:
            self._fail_startup(f"application configuration database: {exc}")
            return

        if self.run_persistence_database is None:
            run_path = (
                Path(self.run_persistence_path)
                if self.run_persistence_path is not None
                else Path(base_dir) / "data" / "run_v2.db"
            )
            self.run_persistence_database = RunPersistenceDatabase(
                run_path
            )
        runtime = self.run_persistence_database
        if runtime.ready is not True:
            settlement = settle_startup(runtime, _utc_now())
            if settlement.ready is not True or runtime.ready is not True:
                self._fail_startup(
                    settlement.reason or "run persistence startup did not settle"
                )
                return
        self.run_export_reader = RunExportReader(
            runtime
        )
        self.run_archive_service = RunArchiveService(self.run_export_reader)
        self.analysis_result_artifact_store = AnalysisResultArtifactStore(
            runtime.path.parent / "run_results"
        )
        self.run_data_management_service = RunDataManagementService(
            runtime,
            result_artifact_store=self.analysis_result_artifact_store,
        )

        # 设备服务（刷新/枚举/保存输入监听设备）
        self.hss_service = HardwareSelectService()

        # 输入校准服务（后续扩展）
        self.input_calibration_service = InputCalibrationService(self.bus)

        # 配置服务
        self.config_service = ConfigService(self.bus)
        self.audio_data_service = AudioDataService()

        # 把 plc工艺表里的读取方法，挂到 services 上
        self.get_process_param_values = None

        self.ai_service = AiService()

        self.historical_metric_report_service = HistoricalMetricReportService(
            audio_data_service=self.audio_data_service,
            hss_service=self.hss_service,
            ai_service=self.ai_service,
            golden_sample_service=self.config_service.golden_sample_service,
        )
        self.config_package_service = ConfigPackageService(
            config_service=self.config_service,
            ai_service=self.ai_service,
        )
        self.factory_selection_settings = FactorySelectionSettings()

        self.plc_dispatch_ports = None
        report_attempt = self._dispatch_plc_attempt_report
        self._plc_report_attempt = report_attempt
        self.plc_result_sequencer = PlcResultSequencer(
            write_result=self.bus.write_test_result_sign.emit,
            report_attempt=report_attempt,
        )
        builder = self.production_composition_builder
        prepared = None
        try:
            if builder is None:
                from app.service.multi_run_integration.production_composition import (
                    build_production_composition_builder,
                )

                builder = build_production_composition_builder(self)
                self.production_composition_builder = builder
            prepared = builder.prepare()
            prepared_routes = getattr(prepared, "config_routes", ())
            if type(prepared_routes) is not tuple:
                raise ValueError(
                    "prepared config routes must be an exact tuple"
                )
        except Exception as exc:
            self._mark_production_unavailable(
                "生产准备失败",
                exc,
                prepared=prepared,
            )
        else:
            if getattr(prepared, "coordinator", None) is not None:
                self._mark_production_unavailable(
                    "生产准备失败",
                    "final composition prepare constructed Coordinator",
                    prepared=prepared,
                )
                prepared = None
            else:
                self.prepared_production_composition = prepared
                self._prepared_config_routes = prepared_routes
                self._production_composition_state = "prepared"

        from app.service.factory_mode_controller import FactoryModeController

        class ProductionAvailabilityFactoryModeController(
            FactoryModeController
        ):
            def _production_unavailable_reason(self) -> str | None:
                reason = getattr(
                    self.services,
                    "production_unavailable_reason",
                    None,
                )
                if type(reason) is not str:
                    return None
                reason = reason.strip()
                return reason or None

            def _selection_readiness(
                self,
                selected: tuple[int, ...],
            ) -> tuple[bool, str | None]:
                reason = self._production_unavailable_reason()
                if reason is not None:
                    return False, reason
                return super()._selection_readiness(selected)

            def _preserve_production_unavailable_state(self) -> None:
                reason = self._production_unavailable_reason()
                if reason is None:
                    return
                self._state = replace(
                    self._state,
                    run_phase="not_ready",
                    selection_ready=False,
                    selection_ready_reason=reason,
                    start_ready=False,
                    start_ready_reason=reason,
                )

            def _emit_state(self) -> None:
                self._preserve_production_unavailable_state()
                super()._emit_state()

        prepared_for_controller = self.prepared_production_composition
        self.factory_mode = ProductionAvailabilityFactoryModeController(
            self.bus,
            self,
            monotonic_ns=self.monotonic_ns,
            prepared_selected_config_ids=tuple(
                getattr(prepared_for_controller, "selected_config_ids", ())
            ),
            prepared_config_routes=self._prepared_config_routes,
        )
        self.factory_mode._preserve_production_unavailable_state()
        self.ingress_open = self.multi_run_coordinator is not None

    @property
    def production_composition_state(self) -> str:
        return self._production_composition_state

    @property
    def production_selection_locked(self) -> bool:
        return self._production_composition_state in {"prepared", "active"}

    def _fail_startup(self, reason: object) -> None:
        if self.startup_failure is not None:
            return
        text = str(reason).strip() or "startup failed"
        self.startup_failure = text
        self.ingress_open = False
        self.bus.status.emit(text)

    def _mark_production_unavailable(
        self,
        context: str,
        error: object,
        *,
        prepared: object | None = None,
    ) -> None:
        detail = str(error).strip() or type(error).__name__
        suffix = "。请修复设备或配置后重启软件。"
        prefix = f"{context}："
        available = max(0, 512 - len(prefix) - len(suffix))
        self.production_unavailable_reason = (
            prefix + detail[:available] + suffix
        )
        owned = (
            prepared
            if prepared is not None
            else self.prepared_production_composition
        )
        close = getattr(owned, "close", None)
        cleanup_failed = False
        if callable(close):
            try:
                close()
            except Exception as close_error:
                cleanup_failed = True
                self.bus.log.emit(
                    f"production unavailable cleanup failed: {close_error}"
                )
        elif owned is not None:
            cleanup_failed = True
            self.bus.log.emit(
                "production unavailable cleanup owner has no close method"
            )
        self._production_cleanup_owner = (
            owned if cleanup_failed else None
        )
        self.prepared_production_composition = None
        self.final_composition = None
        self.multi_run_coordinator = None
        self._prepared_config_routes = ()
        self.ingress_open = False
        self._production_composition_state = "failed"

        controller = getattr(self, "factory_mode", None)
        if controller is None:
            return
        state = getattr(controller, "state", FactoryControllerState())
        selected = tuple(getattr(state, "selected_config_ids", ()) or ())
        controller._prepared_config_routes = ()
        controller._state = FactoryControllerState(
            selected_config_ids=selected,
            run_phase="not_ready",
            selection_ready=bool(selected),
            start_ready=False,
            start_ready_reason=self.production_unavailable_reason,
        )
        self.bus.factory_state_changed.emit(controller._state)
        self.bus.status.emit(self.production_unavailable_reason)

    def _report_plc_attempt(self, report: object) -> None:
        if getattr(report, "outcome", None) is PlcAttemptOutcome.BOUNDARY_FAILED:
            self.bus.log.emit(
                f"PLC physical boundary failure: {getattr(report, 'reason', '')}"
            )

    def _dispatch_plc_attempt_report(self, report: object) -> None:
        self._report_plc_attempt(report)
        if self.plc_report_attempt is not None:
            self.plc_report_attempt(report)

    def _history_labels(
        self,
        *,
        source: TriggerSource,
        workorder: Mapping[str, object],
        config_id: int | None,
    ) -> tuple[dict[str, object], FrozenPayload]:
        name, fault_types = self._history_config(config_id)
        trigger_source = {
            TriggerSource.PLC: "plc",
            TriggerSource.MANUAL: "manual",
            TriggerSource.SCANNER: "scan",
            TriggerSource.IMPORT: "import_audio",
        }[source]
        sn = workorder.get("sn")
        value_id, canonical = encode_history_labels_payload(
            config_id=config_id,
            name=name,
            fault_types=fault_types,
            details=[],
            sn=sn if type(sn) is str else "",
            trigger_source=trigger_source,
        )
        return (
            json.loads(canonical),
            FrozenPayload(value_id=value_id, canonical_bytes=canonical),
        )

    def _history_config(
        self,
        config_id: int | None,
    ) -> tuple[str, list[str]]:
        if config_id is None:
            return "未解析配置", []
        prepared = self.prepared_production_composition
        prerequisites = getattr(prepared, "_prerequisites", None)
        for candidate in tuple(getattr(prerequisites, "configs", ()) or ()):
            if getattr(candidate, "config_id", None) != config_id:
                continue
            values = dict(getattr(candidate, "values", {}) or {})
            name = values.get("config_name")
            raw_fault_types = values.get("fault_types_json")
            fault_types = (
                decode_fault_types_json(raw_fault_types)
                if type(raw_fault_types) is str
                else []
            )
            return (
                name
                if type(name) is str and name
                else f"Config {config_id}",
                fault_types,
            )
        semantic = self._semantic_config
        name = semantic.get("config_name") or semantic.get("name")
        if not isinstance(name, str) or not name:
            semantic_name = semantic.get("config_id")
            name = (
                semantic_name
                if isinstance(semantic_name, str) and semantic_name
                else f"Config {config_id}"
            )
        raw_fault_types = semantic.get("fault_types", [])
        fault_types = (
            sorted(
                {
                    value.strip()
                    for value in raw_fault_types
                    if type(value) is str and value.strip()
                }
            )
            if isinstance(raw_fault_types, (list, tuple))
            else []
        )
        return name, fault_types

    def build_captured_start_request(
        self,
        *,
        source: TriggerSource,
        trigger_monotonic_ns: int,
        workorder: Mapping[str, object] | None = None,
        plc_id: int | None = None,
        config_id: int,
    ) -> CapturedStartRequest:
        _require_positive_monotonic_ns(
            trigger_monotonic_ns,
            "trigger_monotonic_ns",
        )
        if source not in {
            TriggerSource.PLC,
            TriggerSource.MANUAL,
            TriggerSource.SCANNER,
        }:
            raise ValueError("captured source is invalid")
        if type(config_id) is not int or config_id <= 0:
            raise ValueError("captured config identity is invalid")
        if config_id not in {
            route_config_id
            for route_config_id, _route_plc_id in self._prepared_config_routes
        }:
            raise ValueError("captured config identity was not prepared")
        work_order = dict(workorder or {})
        labels, history_payload = self._history_labels(
            source=source,
            workorder=work_order,
            config_id=config_id,
        )
        frozen_identity = {"config_id": config_id}
        analysis_input = {
            "schema": "analysis-input/v1",
            "config": frozen_identity,
        }
        ingress = {
            "schema": "jingcheng-run-config/v1",
            "config": frozen_identity,
            "analysis_input": analysis_input,
            "history_labels": labels,
            "work_order": work_order,
        }
        run_id = str(uuid4())
        return CapturedStartRequest(
            run_id=run_id,
            trigger_monotonic_ns=trigger_monotonic_ns,
            source=source,
            ingress_payload=_canonical_payload(
                f"captured-ingress:{run_id}",
                ingress,
            ),
            fallback_history_labels=history_payload,
            plc_id=plc_id,
        )

    def build_imported_start_request(
        self,
        *,
        trigger_monotonic_ns: int,
        source_path: str | Path,
    ) -> ImportedStartRequest:
        _require_positive_monotonic_ns(
            trigger_monotonic_ns,
            "trigger_monotonic_ns",
        )
        config_id = (
            self._prepared_config_routes[0][0]
            if len(self._prepared_config_routes) == 1
            else None
        )
        labels, history_payload = self._history_labels(
            source=TriggerSource.IMPORT,
            workorder={},
            config_id=config_id,
        )
        analysis_input = {
            "schema": "analysis-input/v1",
            "config": self._semantic_config,
        }
        ingress = {
            "schema": "jingcheng-run-config/v1",
            "config": self._semantic_config,
            "analysis_input": analysis_input,
            "history_labels": labels,
            "source_path": str(Path(source_path)),
        }
        run_id = str(uuid4())
        return ImportedStartRequest(
            run_id=run_id,
            trigger_monotonic_ns=trigger_monotonic_ns,
            source=TriggerSource.IMPORT,
            ingress_payload=_canonical_payload(
                f"imported-ingress:{run_id}",
                ingress,
            ),
            fallback_history_labels=history_payload,
        )

    def submit_imported(self, request: ImportedStartRequest):
        if not self.ingress_open:
            return SimpleNamespace(status=None)
        return self.multi_run_coordinator.start_imported(request)

    def submit_scan(self, scan_text: str) -> bool:
        if not self.ingress_open:
            return False
        trigger_monotonic_ns = _require_positive_monotonic_ns(
            self.monotonic_ns(),
            "trigger_monotonic_ns",
        )
        self.bus.factory_scan_event.emit(
            FactoryScanIngressPayload(
                scan_text=scan_text,
                trigger_monotonic_ns=trigger_monotonic_ns,
            )
        )
        return True

    def publish_plc_semantic_result(
        self,
        *,
        plc_id: int,
        run_id: str,
        semantic_result: Mapping[str, object],
    ) -> bool:
        required_physical = frozenset(
            {
                "finished",
                "error_code",
                "sn_code",
                "test_result",
                "score",
                "spl",
                "loudness",
                "vibrate",
                "ai_score",
            }
        )
        if set(semantic_result) != required_physical:
            self._plc_report_attempt(
                PlcResultAttemptReport(
                    run_id=run_id,
                    plc_id=plc_id,
                    plc_sequence=1,
                    outcome=PlcAttemptOutcome.BOUNDARY_FAILED,
                    field_name="physical_payload",
                    reason=(
                        "semantic result has no complete defined physical mapping"
                    ),
                )
            )
            return False
        return False

    def attach_committed_history_observer(
        self,
        observer: Callable[[object], None],
    ) -> None:
        if not callable(observer):
            raise TypeError("committed history observer must be callable")
        if self.result_history_adapter is not None:
            raise RuntimeError(
                "committed history observer must be attached before dashboard"
            )
        if self._committed_history_observer is not None:
            raise RuntimeError("committed history observer is already attached")
        self._committed_history_observer = observer

    def attach_result_dashboard(self, dashboard: object):
        if self.result_history_adapter is not None:
            return self.result_history_adapter
        if getattr(dashboard, "_history_store", None) is not self.result_history_store:
            raise ValueError(
                "dashboard must use the ServiceContainer result history store"
            )
        from app.service.multi_run_integration import HistoryQtAdapter

        def history_evicted(notice):
            coordinator = getattr(self, "multi_run_coordinator", None)
            callback = getattr(coordinator, "history_evicted", None)
            if callable(callback):
                callback(notice)

        def history_diagnostic(diagnostic):
            self.bus.log.emit(
                f"result history {diagnostic.code.value}: {diagnostic.detail}"
            )

        self.result_history_adapter = HistoryQtAdapter(
            dashboard,
            eviction_callback=history_evicted,
            diagnostic_callback=history_diagnostic,
        )
        prepared = self.prepared_production_composition
        if prepared is not None and self.final_composition is None:
            try:
                history_sink = self.result_history_adapter
                if self._committed_history_observer is not None:
                    history_sink = _PostDurableHistorySink(
                        self.result_history_adapter,
                        self._committed_history_observer,
                        lambda message: self.bus.log.emit(message),
                    )
                active = prepared.activate(history_sink)
                coordinator = getattr(active, "coordinator")
                streams = tuple(getattr(active, "streams"))
                if not 1 <= len(streams) <= 6:
                    raise ValueError(
                        "active production graph requires one through six "
                        "Resident streams"
                    )
                start_dispatcher = CapturedStartDispatcher(capacity=2)
            except Exception as exc:
                self._mark_production_unavailable(
                    "生产功能启动失败",
                    exc,
                    prepared=prepared,
                )
                return self.result_history_adapter
            self.final_composition = active
            self.multi_run_coordinator = coordinator
            self._captured_start_dispatcher = start_dispatcher
            self._production_composition_state = "active"
            self.ingress_open = True
        return self.result_history_adapter

    def _start_from_plc(
        self,
        plc_id: int,
        workorder: dict | None,
        trigger_monotonic_ns: int,
    ) -> None:
        snapshot = dict(workorder or {})
        if self.ingress_open and self._captured_start_dispatcher is not None:
            matches = tuple(
                config_id
                for config_id, route_plc_id in self._prepared_config_routes
                if route_plc_id == plc_id
            )
            if not matches:
                return
            self.submit_captured_start(
                source=TriggerSource.PLC,
                trigger_monotonic_ns=trigger_monotonic_ns,
                workorder=snapshot,
                plc_id=plc_id,
                config_id=matches[0],
            )
            return
        self.bus.factory_plc_event.emit(
            FactoryPlcIngressPayload(
                plc_id=plc_id,
                trigger_monotonic_ns=trigger_monotonic_ns,
                sn=snapshot.get("sn"),
                model=snapshot.get("model"),
                rotation_speed=snapshot.get("rotation_speed"),
            )
        )

    def submit_captured_start(
        self,
        *,
        source: TriggerSource,
        trigger_monotonic_ns: int,
        workorder: Mapping[str, object] | None,
        plc_id: int | None,
        config_id: int,
    ) -> bool:
        dispatcher = self._captured_start_dispatcher
        coordinator = self.multi_run_coordinator
        start = getattr(coordinator, "start_captured", None)
        if not self.ingress_open or dispatcher is None or not callable(start):
            return False
        captured_workorder = dict(workorder or {})

        def start_captured() -> None:
            request = self.build_captured_start_request(
                source=source,
                trigger_monotonic_ns=trigger_monotonic_ns,
                workorder=captured_workorder,
                plc_id=plc_id,
                config_id=config_id,
            )
            start(request)

        route_plc_id = next(
            (
                route_plc_id
                for route_config_id, route_plc_id in self._prepared_config_routes
                if route_config_id == config_id
            ),
            None,
        )
        lane_key = (
            ("plc", route_plc_id)
            if type(route_plc_id) is int and route_plc_id > 0
            else ("config", config_id)
        )
        accepted = dispatcher.try_submit(
            start_captured,
            lane_key=lane_key,
        )
        if not accepted:
            source_name = source.name.lower()
            reason = "captured start admission queue is full (capacity=2)"
            self.bus.factory_start_blocked.emit(
                FactoryStartBlockedPayload(
                    reason=reason,
                    trigger_source=source_name,
                )
            )
            self.bus.log.emit(f"{source_name} start rejected: {reason}")
        return accepted

    def request_plc_start_signal_resets_for_configs(
        self,
        config_ids: tuple[int, ...] | list[int],
    ) -> tuple[int, ...]:
        plc_ids: list[int] = []
        seen_plc_ids: set[int] = set()

        for config_id in config_ids:
            try:
                config = self.config_service.load_config(config_id)
            except Exception as exc:
                self.bus.log.emit(
                    f"配置 {config_id} 加载失败，跳过 PLC 启动信号归位: {exc}"
                )
                continue

            plc_id = config.get("plc_id")
            if plc_id is None:
                self.bus.log.emit(
                    f"配置 {config_id} 未绑定 PLC，跳过启动信号归位"
                )
                continue
            if not isinstance(plc_id, int) or isinstance(plc_id, bool):
                self.bus.log.emit(
                    f"配置 {config_id} 的 PLC ID 无效，跳过启动信号归位"
                )
                continue
            if plc_id in seen_plc_ids:
                continue

            seen_plc_ids.add(plc_id)
            plc_ids.append(plc_id)
            is_new_request = plc_id not in self.pending_plc_start_signal_reset_ids
            self.pending_plc_start_signal_reset_ids.add(plc_id)
            if is_new_request and plc_id in getattr(self, "plc_services", {}):
                self.bus.request_plc_start_signal_reset.emit(plc_id)

        return tuple(plc_ids)

    def dispatch_pending_plc_start_signal_reset(self, plc_id: int) -> bool:
        if plc_id not in self.pending_plc_start_signal_reset_ids:
            return False
        if plc_id not in getattr(self, "plc_services", {}):
            return False

        self.bus.request_plc_start_signal_reset.emit(plc_id)
        return True

    def _on_plc_start_signal_reset_result(
        self,
        plc_id: int,
        success: bool,
        message: str,
    ) -> None:
        if success:
            self.pending_plc_start_signal_reset_ids.discard(plc_id)
            return
        self.bus.log.emit(message or f"PLC {plc_id} 启动信号归位失败")

    def _shutdown_resources(self) -> ApplicationShutdownResources:
        active = getattr(self, "final_composition", None)
        plc_bindings = tuple(
            PlcServiceThreadShutdownOwner(
                service,
                self.plc_threads.get(plc_id),
                _invoke_plc_thread_slot,
            )
            for plc_id, service in sorted(self.plc_services.items())
            if service is not None
        )
        if active is not None:
            return ApplicationShutdownResources(
                coordinator=getattr(active, "coordinator", None),
                scheduler_bridge=getattr(active, "scheduler_bridge", None),
                external_producers=plc_bindings,
                plc_sequencer=getattr(active, "plc_result_sequencer", None),
                plc_owners=plc_bindings,
                resident_capture=getattr(active, "resident_capture", None),
                managed_resident=getattr(active, "managed_resident", None),
                playback=getattr(active, "playback", None),
                independent_writers=tuple(
                    item
                    for item in (
                        getattr(self, "ai_service", None),
                        *self._registered_independent_shutdown_resources,
                    )
                    if item is not None
                ),
                application_config_database=getattr(self, "db", None),
                shared_audio_backend=getattr(active, "shared_backend", None),
                active_completion=active,
                run_persistence_database=getattr(
                    self,
                    "run_persistence_database",
                    None,
                ),
            )
        return ApplicationShutdownResources(
            external_producers=plc_bindings,
            plc_sequencer=getattr(self, "plc_result_sequencer", None),
            plc_owners=plc_bindings,
            independent_writers=tuple(
                item
                for item in (
                    getattr(self, "ai_service", None),
                    *self._registered_independent_shutdown_resources,
                )
                if item is not None
            ),
            application_config_database=getattr(self, "db", None),
            prepared_composition=getattr(
                self,
                "prepared_production_composition",
                None,
            ) or getattr(self, "_production_cleanup_owner", None),
            run_persistence_database=getattr(
                self,
                "run_persistence_database",
                None,
            ),
        )

    def _emit_shutdown_notice(self, report) -> None:
        if report.clean:
            message = "Application shutdown completed cleanly."
        else:
            message = "Application shutdown incomplete; restart required."
        self.bus.status.emit(message)

    @property
    def application_shutdown_owner(self) -> ApplicationShutdownOwner:
        owner = self._application_shutdown_owner
        if owner is None:
            owner = ApplicationShutdownOwner(
                self._shutdown_resources,
                monotonic=time.monotonic,
                timeout_seconds=5.0,
                aggregate_notice=self._emit_shutdown_notice,
            )
            self._application_shutdown_owner = owner
        return owner

    def shutdown(self, timeout_seconds: float | None = None):
        self._shutdown_registration_closed = True
        self.ingress_open = False
        dispatcher = self._captured_start_dispatcher
        if dispatcher is not None:
            close_timeout = 5.0 if timeout_seconds is None else timeout_seconds
            if dispatcher.close(timeout_seconds=close_timeout):
                self._captured_start_dispatcher = None
            else:
                self.bus.log.emit(
                    "captured start dispatcher did not stop before shutdown"
                )
        report = self.application_shutdown_owner.shutdown(timeout_seconds)
        if report.clean:
            self._production_composition_state = "closed"
        return report

    def register_independent_shutdown_resource(self, resource: object) -> None:
        if resource is None:
            raise ValueError("shutdown resource is required")
        if self._shutdown_registration_closed:
            raise RuntimeError("application shutdown has already started")
        if any(
            item is resource
            for item in self._registered_independent_shutdown_resources
        ):
            return
        self._registered_independent_shutdown_resources.append(resource)
