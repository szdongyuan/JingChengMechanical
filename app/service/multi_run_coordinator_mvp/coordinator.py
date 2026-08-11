from __future__ import annotations

import hashlib
import json
import math
import threading
import ntpath
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Callable

from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AudioStorageKind,
    PersistedAudioReference as BridgeAudioReference,
)
from app.service.run_persistence import (
    AttemptKind as BridgeAttemptKind,
    AttemptState as BridgeAttemptState,
    BridgeAuthorityStatus,
    BridgeCancelRequest,
    BridgeCancelResult,
    BridgeCancelStatus,
    BridgeSubmissionResult,
    BridgeSubmissionStatus,
    BridgeTerminalReport,
    BridgeTerminalStatus,
    InitialAnalysisRequest,
    Judgment as BridgeJudgment,
    ReanalysisRequest as BridgeReanalysisRequest,
    decode_analysis_envelope,
    freeze_analysis_envelope,
)
from app.service.current_meter_service import current_samples_to_trace
from app.service.fault_types import (
    decode_history_labels_payload,
    decode_run_config_payload,
)
from app.service.plc_result_sequencer import (
    PlcIngressTicket,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
)

from .contracts import (
    ABSENT,
    AnalysisAttemptKind,
    AudioSaveSourceKind,
    AudioSaveStatus,
    AudioSaveOutcome,
    CapturedAudioSaveRequest,
    CapturedAudioValue,
    CapturedSourceFormat,
    CapturedStartRequest,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CaptureReserveStatus,
    CommitResult,
    CoordinatorLifecycle,
    CoordinatorPhase,
    CoordinatorShutdownReport,
    CoordinatorShutdownStatus,
    CoordinatorSnapshot,
    DiagnosticFact,
    ExternalAction,
    ExternalActionKind,
    FrozenCapturedRun,
    FrozenImportedRun,
    FrozenPayload,
    HistoryEvictionNotice,
    HistoryFactKind,
    HistoryProjectionFact,
    ProjectedHistoryEvent,
    ImportedAudioSaveRequest,
    ImportedStartRequest,
    ManualJudgmentRequest,
    ManualProjectionResult,
    ManualProjectionStatus,
    OperationResult,
    OperationStatus,
    PendingTerminalContender,
    PersistedAudioReference,
    PreacceptTerminalFact,
    ReanalysisCancelRequest,
    ReanalysisRequest,
    ResidentCancelResult,
    ResidentCancelStatus,
    ResultControlFacts,
    ResultOperationPhase,
    ResultProjection,
    ResultProjectionPhase,
    SaveCancelResult,
    SaveCancelStatus,
    SaveSubmitStatus,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
    StartResult,
    StartStatus,
    StopReport,
    StopStatus,
    TerminalConclusion,
    TerminalErrorCategory,
    TerminalFact,
    TerminalContenderKind,
    TriggerSource,
    require_bounded_text,
    require_uuid4,
)
from .ports import (
    AnalysisBridgePort,
    AudioSaveDispatcherPort,
    CapturedRunFreezer,
    CurrentCollectorFactory,
    DiagnosticSink,
    HistoryEventProjector,
    ImportedRunFreezer,
    MultiRunPersistencePort,
    PlcSequencerPort,
    ResidentCapturePort,
    ResultHistoryEventSink,
    RunCurrentCollector,
)

# Used only when the injected terminal wall clock raises or is invalid. This
# preserves typed cleanup/PLC settlement without retrying or inventing runtime
# capture/analysis time.
_WALL_CLOCK_FAILURE_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)

_INITIAL_BRIDGE_FAILED_CODES = frozenset(
    (
        "ANALYSIS_INVALID_JOB",
        "ANALYSIS_DUPLICATE_ATTEMPT_ID",
        "ANALYSIS_QUEUE_FULL",
        "ANALYSIS_SCHEDULER_NOT_ACCEPTING",
        "ANALYSIS_SCHEDULER_SUBMIT_FAILED",
        "SCHEDULER_RECEIPT_INVALID",
        "SCHEDULER_RECEIPT_IDENTITY_MISMATCH",
        "ANALYSIS_QUEUE_TIMEOUT",
        "ATTEMPT_START_NOT_QUEUED",
        "ATTEMPT_START_DB_FAILED",
        "ATTEMPT_START_CONTEXT_INVALID",
        "ATTEMPT_START_TIMESTAMP_FAILED",
        "ATTEMPT_START_RESULT_INVALID",
        "ATTEMPT_START_GATE_EXCEPTION",
        "ANALYSIS_RESULT_PAYLOAD_INVALID",
        "AUDIO_REFERENCE_UNREADABLE",
        "AUDIO_REFERENCE_CHANGED",
        "AUDIO_FORMAT_MISMATCH",
        "AUDIO_LOAD_FAILED",
        "ANALYSIS_TIMEOUT",
        "ANALYSIS_EXECUTION_FAILED",
    )
)


class _PlcSettlementKind(Enum):
    NONE = auto()
    REJECT = auto()
    PUBLISH_TERMINAL = auto()


class _ResultOperationKind(Enum):
    MANUAL = auto()
    REANALYSIS = auto()


@dataclass(frozen=True, slots=True)
class _PublicationFact:
    terminal: PreacceptTerminalFact | TerminalFact
    history: HistoryProjectionFact | None


@dataclass(frozen=True, slots=True)
class _HistoryDispatchResult:
    may_continue: bool


@dataclass(frozen=True, slots=True)
class _TerminalClaim:
    requested: PendingTerminalContender
    terminal_attempt_id: str | None
    terminal_attempt_kind: AnalysisAttemptKind
    terminal_history_labels: FrozenPayload
    fail_closed_category: TerminalErrorCategory
    reservation_to_cancel: object | None
    save_job_to_cancel: str | None
    scheduler_attempt_to_cancel: str | None
    clock_action: ExternalAction


@dataclass(frozen=True, slots=True)
class _BridgeTerminalQuarantineFact:
    run_id: str
    stage: str
    reason: str


@dataclass(slots=True)
class _PublishingContext:
    run_id: str
    ticket: PlcIngressTicket | None
    plc_accepted: bool
    reservation: object | None
    durable_active: bool
    fact: _PublicationFact
    last_published_revision: object
    history_in_flight: bool
    plc_settlement: _PlcSettlementKind
    plc_in_flight: bool
    plc_unsettled: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _RunContext:
    request: CapturedStartRequest | ImportedStartRequest
    phase: CoordinatorPhase = CoordinatorPhase.PREFLIGHT
    action: ExternalAction | None = None
    frozen: FrozenCapturedRun | FrozenImportedRun | None = None
    ticket: PlcIngressTicket | None = None
    reservation: object | None = None
    reservation_cancel_attempted: bool = False
    reservation_cancel_in_flight: bool = False
    durable_active: bool = False
    capture_lease_committed: bool = False
    current_collector: RunCurrentCollector | None = None
    current_start_in_progress: bool = False
    current_collector_started: bool = False
    current_settlement_claimed: bool = False
    current_settlement_in_progress: bool = False
    plc_accepted: bool = False
    history_allowed: bool = True
    last_published_revision: object = ABSENT
    activation_ready: bool = False
    early_capture_outcome: CaptureOutcome | None = None
    save_job_id: str | None = None
    save_request: CapturedAudioSaveRequest | ImportedAudioSaveRequest | None = None
    save_source_kind: AudioSaveSourceKind | None = None
    save_source_identity: str | None = None
    save_metadata: object | None = None
    early_save_outcome: AudioSaveOutcome | None = None
    save_child_accepted: bool = False
    save_cancel_attempted: bool = False
    save_cancel_in_flight: bool = False
    persisted_audio: object | None = None
    attempt_id: str | None = None
    attempt_kind: AnalysisAttemptKind | None = None
    early_analysis_report: BridgeTerminalReport | None = None
    bridge_submission_reconciled: bool = False
    bridge_terminal_consumed: bool = False
    bridge_terminal_consumption_in_progress: bool = False
    bridge_terminal_duplicate_claimed: bool = False
    bridge_terminal_quarantine: _BridgeTerminalQuarantineFact | None = None
    scheduler_child_accepted: bool = False
    scheduler_cancel_attempted: bool = False
    cancel_requested: bool = False
    pending_terminal: PendingTerminalContender | None = None
    terminal_claimed: bool = False
    terminal_fact: TerminalFact | None = None
    @property
    def run_id(self) -> str:
        return self.request.run_id


@dataclass(slots=True)
class _ResultOperationContext:
    run_id: str
    kind: _ResultOperationKind
    request_revision: int
    terminal_expected_revision: int
    last_published_revision: int
    phase: ResultOperationPhase = ResultOperationPhase.LOADING_FACTS
    action: ExternalAction | None = None
    facts: ResultControlFacts | None = None
    audio: object | None = None
    started_projection: ResultProjection | None = None
    attempt_id: str | None = None
    early_report: BridgeTerminalReport | None = None
    bridge_submission_result: BridgeSubmissionResult | None = None
    submission_reconciled: bool = False
    started_dispatched: bool = False
    report_delivery_seen: bool = False
    report_consumed: bool = False
    report_channel_invalid: bool = False
    report_violation_diagnosed: bool = False
    channel_retired: bool = False
    user_cancel_attempted: bool = False
    eviction_cancel_attempted: bool = False
    lifecycle_cancel_requested: bool = False
    eviction_requested: bool = False
    history_suppressed: bool = False


@dataclass(frozen=True, slots=True)
class _ResultReportViolationDiagnostic:
    stage: str
    reason: str
    run_id: str
    attempt_id: str | None


class MultiRunCoordinator:
    def __init__(
        self,
        captured_freezer: CapturedRunFreezer,
        imported_freezer: ImportedRunFreezer,
        resident: ResidentCapturePort,
        persistence: MultiRunPersistencePort,
        save_dispatcher: AudioSaveDispatcherPort,
        scheduler: AnalysisBridgePort,
        history_projector: HistoryEventProjector,
        history_sink: ResultHistoryEventSink,
        plc: PlcSequencerPort,
        diagnostics: DiagnosticSink,
        current_collector_factory: CurrentCollectorFactory,
        save_job_id_factory: Callable[[], str],
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float],
    ) -> None:
        self._captured_freezer = captured_freezer
        self._imported_freezer = imported_freezer
        self._resident = resident
        self._persistence = persistence
        self._save_dispatcher = save_dispatcher
        self._scheduler = scheduler
        self._history_projector = history_projector
        self._history_sink = history_sink
        self._plc = plc
        self._diagnostics = diagnostics
        self._current_collector_factory = current_collector_factory
        self._save_job_id_factory = save_job_id_factory
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._lifecycle = CoordinatorLifecycle.OPEN
        self._runs: dict[str, _RunContext | _PublishingContext] = {}
        self._attempts: dict[str, _RunContext] = {}
        self._save_jobs: dict[str, _RunContext] = {}
        self._result_operations: dict[str, _ResultOperationContext] = {}
        self._restart_required = False
        self._shutdown_begun = False
        self._shutdown_begin_in_progress = False
        self._shutdown_report: CoordinatorShutdownReport | None = None
        self._shutdown_finish_in_progress = False
        self._stop_errors: list[str] = []
        self._restart_errors: list[str] = []
        self._shutdown_errors: list[str] = []
        self._child_cancel_actions: set[ExternalAction] = set()
        self._child_cancel_action_durable: dict[ExternalAction, bool] = {}
        self._result_history_actions: set[ExternalAction] = set()

    def snapshot(self) -> CoordinatorSnapshot:
        with self._lock:
            return CoordinatorSnapshot(
                lifecycle=self._lifecycle,
                ingress_run_ids=tuple(
                    run_id
                    for run_id, context in self._runs.items()
                    if (
                        isinstance(context, _RunContext)
                        and not context.durable_active
                    )
                    or (
                        isinstance(context, _PublishingContext)
                        and context.reservation is not None
                        and not context.durable_active
                    )
                ),
                accepted_active_run_ids=tuple(
                    run_id
                    for run_id, context in self._runs.items()
                    if (
                        isinstance(context, _RunContext)
                        and context.durable_active
                    )
                    or (
                        isinstance(context, _PublishingContext)
                        and context.reservation is not None
                        and context.durable_active
                    )
                ),
                analysis_attempt_ids=tuple(self._attempts),
                result_operation_attempt_ids=tuple(
                    context.attempt_id
                    for context in self._result_operations.values()
                    if context.attempt_id is not None
                ),
                pending_publication_run_ids=tuple(
                    run_id
                    for run_id, context in self._runs.items()
                    if isinstance(context, _PublishingContext)
                    and (
                        context.history_in_flight
                        or context.plc_in_flight
                        or context.plc_unsettled
                    )
                ),
                restart_required=self._restart_required,
            )

    def stop_all(self, timeout_seconds: float) -> StopReport:
        self._require_nonnegative_finite_float(
            timeout_seconds,
            "timeout_seconds",
        )
        started_at = self._read_monotonic()
        deadline = started_at + timeout_seconds
        with self._lock:
            if self._lifecycle is CoordinatorLifecycle.CLOSED:
                return self._stop_report_locked(
                    StopStatus.CLOSED,
                    (),
                    (),
                    timed_out=False,
                )
            if self._lifecycle is CoordinatorLifecycle.STOPPING:
                return self._stop_report_locked(
                    StopStatus.IN_PROGRESS,
                    (),
                    (),
                    timed_out=False,
                )
            self._stop_errors.clear()
            self._lifecycle = CoordinatorLifecycle.STOPPING
            runs = tuple(
                context
                for context in self._runs.values()
                if isinstance(context, _RunContext)
            )
            operations = tuple(self._result_operations.values())
            canceled_runs = tuple(
                context.run_id for context in runs if context.durable_active
            )
            canceled_reanalysis = tuple(
                context.attempt_id
                for context in operations
                if context.kind is _ResultOperationKind.REANALYSIS
                and context.attempt_id is not None
            )
            for context in runs:
                context.cancel_requested = True
            for context in operations:
                context.lifecycle_cancel_requested = True
                context.history_suppressed = True

        for context in runs:
            self._request_stop_for_run(context)
        for context in operations:
            self._request_stop_for_operation(context)

        with self._lock:
            while self._stop_has_blockers_locked():
                try:
                    remaining = max(
                        0.0,
                        deadline - self._read_monotonic(),
                    )
                except Exception as exc:
                    return self._stop_failure_report_locked(
                        "stop monotonic clock",
                        exc,
                        canceled_runs,
                        canceled_reanalysis,
                    )
                if remaining == 0.0:
                    self._restart_required = True
                    self._lifecycle = CoordinatorLifecycle.CLOSED
                    self._promote_stop_errors_locked()
                    self._condition.notify_all()
                    return self._stop_report_locked(
                        StopStatus.INCOMPLETE,
                        canceled_runs,
                        canceled_reanalysis,
                        timed_out=True,
                    )
                try:
                    self._condition.wait(remaining)
                except Exception as exc:
                    return self._stop_failure_report_locked(
                        "stop condition wait",
                        exc,
                        canceled_runs,
                        canceled_reanalysis,
                    )
            if (
                self._lifecycle is CoordinatorLifecycle.STOPPING
                and not self._restart_required
            ):
                self._lifecycle = CoordinatorLifecycle.OPEN
                self._condition.notify_all()
                report = self._stop_report_locked(
                    StopStatus.SETTLED,
                    canceled_runs,
                    canceled_reanalysis,
                    timed_out=False,
                )
                self._stop_errors.clear()
                return report
            self._promote_stop_errors_locked()
            return self._stop_report_locked(
                StopStatus.CLOSED,
                canceled_runs,
                canceled_reanalysis,
                timed_out=False,
            )

    def _stop_failure_report_locked(
        self,
        stage: str,
        error: Exception,
        canceled_runs: tuple[str, ...],
        canceled_reanalysis: tuple[str, ...],
    ) -> StopReport:
        has_blockers = self._stop_has_blockers_locked()
        self._record_lifecycle_error_locked(
            self._exception_reason(stage, error)
        )
        self._restart_required = True
        self._lifecycle = CoordinatorLifecycle.CLOSED
        self._promote_stop_errors_locked()
        self._condition.notify_all()
        return self._stop_report_locked(
            StopStatus.INCOMPLETE,
            canceled_runs if has_blockers else (),
            canceled_reanalysis if has_blockers else (),
            timed_out=has_blockers,
        )

    def begin_shutdown(self) -> None:
        with self._lock:
            if self._shutdown_begun:
                return
            self._shutdown_begun = True
            self._shutdown_begin_in_progress = True
            self._shutdown_errors = list(self._restart_errors)
            self._lifecycle = CoordinatorLifecycle.CLOSED
            runs = tuple(
                context
                for context in self._runs.values()
                if isinstance(context, _RunContext)
            )
            operations = tuple(self._result_operations.values())
            for context in runs:
                context.cancel_requested = True
                context.history_allowed = False
            for context in operations:
                context.lifecycle_cancel_requested = True
                context.history_suppressed = True
            actions = self._snapshot_shutdown_cancel_actions_locked(
                runs,
                operations,
            )
            self._condition.notify_all()
        try:
            for owner, context, identity, action, durable_run in actions:
                self._dispatch_shutdown_cancel(
                    owner,
                    context,
                    identity,
                    action,
                    durable_run=durable_run,
                )
        finally:
            with self._lock:
                self._shutdown_begin_in_progress = False
                self._condition.notify_all()

    def finish_shutdown(
        self,
        deadline_monotonic: float,
    ) -> CoordinatorShutdownReport:
        self._require_finite_float(
            deadline_monotonic,
            "deadline_monotonic",
        )
        with self._lock:
            if (
                self._shutdown_begin_in_progress
                or self._shutdown_finish_in_progress
            ):
                raise RuntimeError(
                    "finish_shutdown cannot overlap begin_shutdown "
                    "or finish_shutdown"
                )
            if not self._shutdown_begun:
                raise RuntimeError("begin_shutdown must be called first")
            if self._shutdown_report is not None:
                return self._shutdown_report
            self._shutdown_finish_in_progress = True

        try:
            dispatcher_report = self._save_dispatcher.shutdown(
                deadline_monotonic
            )
            if type(dispatcher_report) is not SaveDispatcherShutdownReport:
                raise TypeError(
                    "save dispatcher returned an invalid shutdown report"
                )
            dispatcher_report.__post_init__()
        except Exception as exc:
            error = self._exception_reason("save dispatcher shutdown", exc)
            with self._lock:
                self._append_bounded_error(self._shutdown_errors, error)
                remaining_save_job_ids = tuple(self._save_jobs)
            dispatcher_report = SaveDispatcherShutdownReport(
                status=(
                    SaveDispatcherShutdownStatus.INCOMPLETE
                    if remaining_save_job_ids
                    else SaveDispatcherShutdownStatus.CLOSED
                ),
                accepted_count=len(remaining_save_job_ids),
                canceled_count=0,
                completed_count=0,
                remaining_save_job_ids=remaining_save_job_ids,
                live_worker_ids=(),
                errors=(error,),
            )

        with self._lock:
            try:
                if self._shutdown_report is not None:
                    return self._shutdown_report
                while self._shutdown_has_context_blockers_locked():
                    try:
                        remaining = max(
                            0.0,
                            deadline_monotonic - self._read_monotonic(),
                        )
                    except Exception as exc:
                        return self._cache_finish_failure_locked(
                            "finish_shutdown monotonic clock",
                            exc,
                            dispatcher_report,
                        )
                    if remaining == 0.0:
                        break
                    try:
                        self._condition.wait(remaining)
                    except Exception as exc:
                        return self._cache_finish_failure_locked(
                            "finish_shutdown condition wait",
                            exc,
                            dispatcher_report,
                        )
                blockers = self._shutdown_blockers_locked()
                clean = (
                    not any(blockers)
                    and dispatcher_report.status
                    is SaveDispatcherShutdownStatus.CLOSED
                    and not self._shutdown_errors
                    and not self._restart_required
                )
                if clean:
                    report = CoordinatorShutdownReport(
                        status=CoordinatorShutdownStatus.CLOSED,
                        remaining_ingress_run_ids=(),
                        remaining_run_ids=(),
                        remaining_attempt_ids=(),
                        remaining_save_job_ids=(),
                        pending_publication_run_ids=(),
                        save_dispatcher_report=dispatcher_report,
                        errors=(),
                        restart_required=False,
                    )
                else:
                    self._restart_required = True
                    report = CoordinatorShutdownReport(
                        status=CoordinatorShutdownStatus.INCOMPLETE,
                        remaining_ingress_run_ids=blockers[0],
                        remaining_run_ids=blockers[1],
                        remaining_attempt_ids=blockers[2],
                        remaining_save_job_ids=blockers[3],
                        pending_publication_run_ids=blockers[4],
                        save_dispatcher_report=dispatcher_report,
                        errors=tuple(self._shutdown_errors),
                        restart_required=True,
                    )
                self._shutdown_report = report
                return report
            finally:
                self._shutdown_finish_in_progress = False
                self._condition.notify_all()

    def _cache_finish_failure_locked(
        self,
        stage: str,
        error: Exception,
        dispatcher_report: SaveDispatcherShutdownReport,
    ) -> CoordinatorShutdownReport:
        self._append_bounded_error(
            self._shutdown_errors,
            self._exception_reason(stage, error),
        )
        self._restart_required = True
        blockers = self._shutdown_blockers_locked()
        report = CoordinatorShutdownReport(
            status=CoordinatorShutdownStatus.INCOMPLETE,
            remaining_ingress_run_ids=blockers[0],
            remaining_run_ids=blockers[1],
            remaining_attempt_ids=blockers[2],
            remaining_save_job_ids=blockers[3],
            pending_publication_run_ids=blockers[4],
            save_dispatcher_report=dispatcher_report,
            errors=tuple(self._shutdown_errors),
            restart_required=True,
        )
        self._shutdown_report = report
        return report

    @staticmethod
    def _require_finite_float(value: float, field_name: str) -> None:
        if type(value) is not float or not math.isfinite(value):
            raise ValueError(f"{field_name} must be an exact finite float")

    @classmethod
    def _require_nonnegative_finite_float(
        cls,
        value: float,
        field_name: str,
    ) -> None:
        cls._require_finite_float(value, field_name)
        if value < 0.0:
            raise ValueError(f"{field_name} must be nonnegative")

    def _read_monotonic(self) -> float:
        value = self._monotonic_clock()
        self._require_finite_float(value, "monotonic clock result")
        return value

    @staticmethod
    def _decode_run_config(frozen: FrozenPayload) -> dict[str, object]:
        return decode_run_config_payload(
            value_id=frozen.value_id,
            canonical_bytes=frozen.canonical_bytes,
        )

    @classmethod
    def _current_source_enabled(cls, frozen: FrozenCapturedRun) -> bool:
        decoded = cls._decode_run_config(frozen.config_payload)
        config = decoded["config"]
        assert type(config) is dict
        current_meter = config.get("current_meter")
        if type(current_meter) is not dict:
            raise ValueError("frozen current_meter must be an object")
        enabled = current_meter.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("frozen current_meter.enabled must be a bool")
        return enabled

    @classmethod
    def _frozen_fault_types(cls, facts: ResultControlFacts) -> tuple[str, ...]:
        decoded = cls._decode_run_config(facts.config_payload)
        config = decoded["config"]
        assert type(config) is dict
        fault_types = config["fault_types"]
        if type(fault_types) is not list:
            raise ValueError("frozen fault_types must be a list")
        return tuple(fault_types)

    @staticmethod
    def _frozen_channel_order(frozen: FrozenCapturedRun) -> tuple[int, ...]:
        try:
            text = frozen.analysis_input.canonical_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("analysis input is not UTF-8") from exc
        decoded = decode_analysis_envelope(text)
        if freeze_analysis_envelope(
            json.loads(text)
        ).encode("utf-8") != frozen.analysis_input.canonical_bytes:
            raise ValueError("analysis input is not canonical")
        expected_id = (
            "sha256:"
            + hashlib.sha256(frozen.analysis_input.canonical_bytes).hexdigest()
        )
        if frozen.analysis_input.value_id != expected_id:
            raise ValueError("analysis input identity mismatch")
        mapping = decoded.channel_mapping
        if (
            set(mapping) != {"schema", "channel_order"}
            or mapping["schema"] != "audio-channel-map/v1"
            or type(mapping["channel_order"]) is not tuple
        ):
            raise ValueError("frozen channel order is invalid")
        channel_order = mapping["channel_order"]
        if (
            not channel_order
            or len(set(channel_order)) != len(channel_order)
            or any(type(value) is not int or value < 0 for value in channel_order)
        ):
            raise ValueError("frozen channel order is invalid")
        return channel_order

    @staticmethod
    def _select_captured_channels(
        audio: CapturedAudioValue,
        channel_order: tuple[int, ...],
    ) -> CapturedAudioValue:
        if audio.channel_indices == channel_order:
            return audio
        try:
            positions = tuple(
                audio.channel_indices.index(channel)
                for channel in channel_order
            )
        except ValueError as exc:
            raise ValueError("channel-order mismatch") from exc
        sample_width = (
            4
            if audio.format is CapturedSourceFormat.FLOAT32_INTERLEAVED
            else 2
        )
        source_frame_width = audio.channel_count * sample_width
        selected_frame_width = len(channel_order) * sample_width
        source = memoryview(audio.interleaved_bytes)
        selected = bytearray(audio.frame_count * selected_frame_width)
        for frame_index in range(audio.frame_count):
            source_frame_start = frame_index * source_frame_width
            selected_frame_start = frame_index * selected_frame_width
            for selected_index, source_position in enumerate(positions):
                source_start = (
                    source_frame_start + source_position * sample_width
                )
                selected_start = (
                    selected_frame_start + selected_index * sample_width
                )
                selected[
                    selected_start : selected_start + sample_width
                ] = source[source_start : source_start + sample_width]
        return CapturedAudioValue(
            source_identity=audio.source_identity,
            sample_rate_hz=audio.sample_rate_hz,
            channel_count=len(channel_order),
            frame_count=audio.frame_count,
            format=audio.format,
            interleaved_bytes=bytes(selected),
            channel_indices=channel_order,
        )

    def _current_stop_timeout(self, context: _RunContext) -> float:
        frozen = context.frozen
        if not isinstance(frozen, FrozenCapturedRun):
            raise ValueError("captured frozen facts are unavailable")
        deadline = (
            context.request.trigger_monotonic_ns / 1_000_000_000.0
            + frozen.target_frame_count / frozen.metadata.expected_sample_rate_hz
        )
        return max(0.0, deadline - self._read_monotonic())

    def _request_stop_for_run(self, context: _RunContext) -> None:
        with self._lock:
            if not self._is_current(context):
                return
            acquired = bool(
                context.attempt_id is not None
                and self._attempts.get(context.attempt_id) is context
                and not context.bridge_terminal_consumed
            )
            if acquired:
                if context.scheduler_cancel_attempted or context.action is not None:
                    return
                context.scheduler_cancel_attempted = True
                attempt_id = context.attempt_id
                action = self._set_child_cancel_action_locked(
                    context,
                    attempt_id=attempt_id,
                )
                durable_run = context.durable_active
            else:
                attempt_id = None
                action = None
                durable_run = False
            save_submit_in_flight = bool(
                context.action is not None
                and context.action.kind is ExternalActionKind.SAVE_SUBMIT
                and context.phase is CoordinatorPhase.SAVE_SUBMITTING
                and context.save_job_id is not None
            )
        if acquired:
            assert attempt_id is not None and action is not None
            self._dispatch_shutdown_cancel(
                "bridge-initial",
                context,
                attempt_id,
                action,
                durable_run=durable_run,
            )
            return
        if save_submit_in_flight:
            return
        if context.durable_active:
            self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator stop requested",
            )
            return
        with self._lock:
            idle_preaccept = (
                self._is_current(context) and context.action is None
            )
        if idle_preaccept:
            self._publish_preaccept(
                context,
                StartStatus.PREFLIGHT_REJECTED,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                "Coordinator stop requested",
            )

    def _request_stop_for_operation(
        self,
        context: _ResultOperationContext,
    ) -> None:
        with self._lock:
            if not self._result_operation_is_current(context):
                return
            context.lifecycle_cancel_requested = True
            acquired = bool(
                context.kind is _ResultOperationKind.REANALYSIS
                and context.attempt_id is not None
                and context.submission_reconciled
                and not context.report_consumed
                and not context.channel_retired
            )
            if acquired:
                if context.user_cancel_attempted or context.action is not None:
                    return
                context.user_cancel_attempted = True
                attempt_id = context.attempt_id
                action = self._set_result_operation_action(
                    context,
                    ExternalActionKind.REANALYSIS_CANCEL,
                )
            elif context.action is None:
                self._result_operations.pop(context.run_id, None)
                context.channel_retired = True
                self._condition.notify_all()
                return
            else:
                return
        assert attempt_id is not None
        self._dispatch_shutdown_cancel(
            "bridge-reanalysis",
            context,
            attempt_id,
            action,
            durable_run=True,
        )

    def _snapshot_shutdown_cancel_actions_locked(
        self,
        runs: tuple[_RunContext, ...],
        operations: tuple[_ResultOperationContext, ...],
    ) -> tuple[
        tuple[
            str,
            _RunContext | _ResultOperationContext,
            object,
            ExternalAction,
            bool,
        ],
        ...,
    ]:
        actions: list[
            tuple[
                str,
                _RunContext | _ResultOperationContext,
                object,
                ExternalAction,
                bool,
            ]
        ] = []
        for context in runs:
            if (
                context.attempt_id is not None
                and self._attempts.get(context.attempt_id) is context
                and not context.bridge_terminal_consumed
                and not context.scheduler_cancel_attempted
            ):
                context.scheduler_cancel_attempted = True
                actions.append(
                    (
                        "bridge-initial",
                        context,
                        context.attempt_id,
                        self._detached_lifecycle_cancel_action_locked(
                            context,
                            attempt_id=context.attempt_id,
                        ),
                        context.durable_active,
                    )
                )
            elif (
                context.save_child_accepted
                and context.save_job_id is not None
                and not context.save_cancel_attempted
            ):
                context.save_cancel_attempted = True
                actions.append(
                    (
                        "save",
                        context,
                        context.save_job_id,
                        self._detached_lifecycle_cancel_action_locked(
                            context,
                            save_job_id=context.save_job_id,
                        ),
                        context.durable_active,
                    )
                )
            elif (
                context.reservation is not None
                and not context.reservation_cancel_attempted
            ):
                context.reservation_cancel_attempted = True
                actions.append(
                    (
                        "resident",
                        context,
                        context.reservation,
                        self._detached_lifecycle_cancel_action_locked(
                            context,
                            reservation_id=context.reservation.reservation_id,
                        ),
                        context.durable_active,
                    )
                )
        for context in operations:
            if (
                context.kind is _ResultOperationKind.REANALYSIS
                and context.attempt_id is not None
                and context.submission_reconciled
                and not context.report_consumed
                and not context.channel_retired
                and not context.user_cancel_attempted
            ):
                context.user_cancel_attempted = True
                actions.append(
                    (
                        "bridge-reanalysis",
                        context,
                        context.attempt_id,
                        self._detached_lifecycle_cancel_action_locked(
                            context,
                            attempt_id=context.attempt_id,
                            reanalysis=True,
                        ),
                        True,
                    )
                )
        return tuple(actions)

    def _dispatch_shutdown_cancel(
        self,
        owner: str,
        context: _RunContext | _ResultOperationContext,
        identity: object,
        action: ExternalAction,
        *,
        durable_run: bool,
    ) -> None:
        with self._lock:
            self._track_child_cancel_action_locked(
                action,
                durable_run=durable_run,
            )
            if isinstance(context, _RunContext):
                if owner == "resident":
                    context.reservation_cancel_in_flight = True
                elif owner == "save":
                    context.save_cancel_in_flight = True
        resident_settled = False
        try:
            if owner == "resident":
                result = self._resident.cancel(identity)
            elif owner == "save":
                result = self._save_dispatcher.cancel(
                    context.run_id,
                    identity,
                )
            else:
                attempt_id = identity
                request = BridgeCancelRequest(
                    run_id=context.run_id,
                    analysis_attempt_id=attempt_id,
                    origin=AnalysisCancelOrigin.USER_STOP,
                    reason="Coordinator shutdown requested",
                )
                result = self._scheduler.cancel(request)
        except Exception as exc:
            dispatch_error = exc
            validation_error = None
        else:
            dispatch_error = None
            try:
                if owner == "resident":
                    self._validate_resident_cancel_result(
                        result,
                        context.run_id,
                        identity,
                    )
                    resident_settled = True
                elif owner == "save":
                    self._validate_save_cancel_result(
                        result,
                        context.run_id,
                        identity,
                    )
                else:
                    if type(result) is not BridgeCancelResult:
                        raise TypeError(
                            "bridge cancel returned an invalid result"
                        )
                    result.__post_init__()
                    if (
                        result.run_id != context.run_id
                        or result.analysis_attempt_id != attempt_id
                        or result.origin is not AnalysisCancelOrigin.USER_STOP
                        or result.status
                        not in {
                            BridgeCancelStatus.CANCEL_REQUESTED,
                            BridgeCancelStatus.ALREADY_TERMINAL,
                        }
                    ):
                        raise ValueError(
                            "bridge cancel result is mismatched or rejected"
                        )
            except Exception as exc:
                validation_error = exc
            else:
                validation_error = None
        error = dispatch_error or validation_error
        if dispatch_error is not None:
            stage = f"{owner} shutdown cancel"
            bounded = self._exception_reason(stage, dispatch_error)
            with self._lock:
                self._record_lifecycle_error_locked(bounded)
                if self._shutdown_begun:
                    self._restart_required = True
                    self._lifecycle = CoordinatorLifecycle.CLOSED
                    self._promote_stop_errors_locked()
        if error is not None:
            stage = f"{owner} shutdown cancel"
            if isinstance(context, _RunContext):
                self._diagnose(
                    stage,
                    context,
                    TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                    error,
                )
            else:
                self._diagnose_result_operation(stage, context, error)
        with self._lock:
            reconciled = False
            if isinstance(context, _RunContext):
                if context.action is action:
                    reconciled = self._reconcile_action(context, action)
                    current = reconciled
                else:
                    current = self._is_current(context)
                if owner == "resident":
                    context.reservation_cancel_in_flight = False
                    if (
                        current
                        and resident_settled
                        and context.reservation == identity
                    ):
                        context.reservation = None
                elif owner == "save":
                    context.save_cancel_in_flight = False
            else:
                if context.action is action:
                    reconciled = self._reconcile_result_operation_action(
                        context,
                        action,
                    )
                    current = reconciled
                else:
                    current = self._result_operation_is_current(context)
            self._retire_child_cancel_action_locked(action)
            if not reconciled:
                self._condition.notify_all()
        if isinstance(context, _RunContext):
            self._continue_lifecycle_after_child_cancel(context)

    @staticmethod
    def _validate_resident_cancel_result(
        result: object,
        run_id: str,
        reservation: object,
    ) -> None:
        if type(result) is not ResidentCancelResult:
            raise TypeError("Resident cancel returned an invalid result")
        result.__post_init__()
        if (
            result.run_id != run_id
            or result.reservation_id != reservation.reservation_id
        ):
            raise ValueError("Resident cancel result identity does not match")
        if result.status not in {
            ResidentCancelStatus.CANCELLED,
            ResidentCancelStatus.ALREADY_TERMINAL,
        }:
            raise RuntimeError(
                result.reason
                or f"Resident cancel returned {result.status.name}"
            )

    @staticmethod
    def _validate_save_cancel_result(
        result: object,
        run_id: str,
        save_job_id: object,
    ) -> None:
        if type(result) is not SaveCancelResult:
            raise TypeError("save cancel returned an invalid result")
        result.__post_init__()
        if result.run_id != run_id or result.save_job_id != save_job_id:
            raise ValueError("save cancel result identity does not match")
        if result.status not in {
            SaveCancelStatus.CANCEL_REQUESTED,
            SaveCancelStatus.ALREADY_TERMINAL,
        }:
            raise RuntimeError(
                f"save cancel returned {result.status.name}"
            )

    def _continue_lifecycle_after_child_cancel(
        self,
        context: _RunContext,
    ) -> None:
        with self._lock:
            if (
                not self._is_current(context)
                or not context.cancel_requested
                or context.action is not None
                or context.reservation_cancel_in_flight
                or context.save_cancel_in_flight
                or context.terminal_claimed
            ):
                return
            contender = self._pop_pending_terminal_locked(context)
            capture_outcome = context.early_capture_outcome
            save_outcome = context.early_save_outcome
            if capture_outcome is not None:
                context.early_capture_outcome = None
                context.reservation = None
            if save_outcome is not None:
                context.early_save_outcome = None
                if context.save_job_id is not None:
                    self._save_jobs.pop(context.save_job_id, None)
                context.save_job_id = None
                context.save_child_accepted = False
                context.save_source_kind = None
                context.save_source_identity = None
                context.save_metadata = None
            bridge_owned = bool(
                context.attempt_id is not None
                and self._attempts.get(context.attempt_id) is context
                and not context.bridge_terminal_consumed
            )
            save_owned = bool(
                context.save_child_accepted
                and context.save_job_id is not None
            )
            lifecycle_terminal = bool(
                contender is None
                and capture_outcome is None
                and save_outcome is None
                and context.durable_active
                and not bridge_owned
                and not save_owned
                and context.reservation is None
            )
        if contender is not None:
            self._terminalize_contender(context, contender)
            return
        if capture_outcome is not None:
            self._terminalize(
                context,
                (
                    TerminalConclusion.MANUAL_STOPPED
                    if capture_outcome.status is CaptureOutcomeStatus.COMPLETED
                    else TerminalConclusion.ERROR
                ),
                (
                    TerminalErrorCategory.MANUAL_STOPPED
                    if capture_outcome.status is CaptureOutcomeStatus.COMPLETED
                    else capture_outcome.error_category
                ),
                (
                    "Coordinator lifecycle stop requested"
                    if capture_outcome.status is CaptureOutcomeStatus.COMPLETED
                    else capture_outcome.reason
                ),
            )
            return
        if save_outcome is not None:
            self._terminalize(
                context,
                (
                    TerminalConclusion.MANUAL_STOPPED
                    if save_outcome.status is AudioSaveStatus.SAVED
                    else TerminalConclusion.ERROR
                ),
                (
                    TerminalErrorCategory.MANUAL_STOPPED
                    if save_outcome.status is AudioSaveStatus.SAVED
                    else save_outcome.error_category
                ),
                (
                    "Coordinator lifecycle stop requested"
                    if save_outcome.status is AudioSaveStatus.SAVED
                    else save_outcome.reason
                ),
            )
            return
        if lifecycle_terminal:
            self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator lifecycle stop requested",
            )

    def _stop_has_blockers_locked(self) -> bool:
        if self._child_cancel_actions:
            return True
        if self._save_jobs:
            return True
        if self._result_history_actions:
            return True
        if self._attempts:
            return True
        if self._result_operations:
            return True
        return any(
            isinstance(context, _RunContext)
            or (
                isinstance(context, _PublishingContext)
                and (
                    context.history_in_flight
                    or context.plc_in_flight
                    or context.plc_unsettled
                    or context.reservation is not None
                )
            )
            for context in self._runs.values()
        )

    def _stop_report_locked(
        self,
        status: StopStatus,
        canceled_runs: tuple[str, ...],
        canceled_reanalysis: tuple[str, ...],
        *,
        timed_out: bool,
    ) -> StopReport:
        return StopReport(
            status=status,
            pending_ingress_run_ids=tuple(
                {
                    *(
                        context.run_id
                        for context in self._runs.values()
                        if isinstance(context, _RunContext)
                        and not context.durable_active
                    ),
                    *(
                        context.run_id
                        for context in self._runs.values()
                        if isinstance(context, _PublishingContext)
                        and context.reservation is not None
                        and not context.durable_active
                    ),
                    *(
                        context.run_id
                        for context in self._result_operations.values()
                        if context.attempt_id is None
                    ),
                    *(
                        action.run_id
                        for action in self._child_cancel_actions
                        if not self._child_cancel_action_durable[action]
                    ),
                }
            ),
            pending_publication_run_ids=tuple(
                {
                    *(
                        context.run_id
                        for context in self._runs.values()
                        if isinstance(context, _PublishingContext)
                        and (
                            context.history_in_flight
                            or context.plc_in_flight
                            or context.plc_unsettled
                        )
                    ),
                    *(
                        action.run_id
                        for action in self._result_history_actions
                    ),
                }
            ),
            canceled_accepted_run_ids=tuple(
                {
                    *canceled_runs,
                    *(
                        context.run_id
                        for context in self._runs.values()
                        if isinstance(context, _RunContext)
                        and context.durable_active
                        and context.cancel_requested
                    ),
                }
            ),
            canceled_reanalysis_attempt_ids=canceled_reanalysis,
            timed_out=timed_out,
            errors=tuple(
                self._restart_errors
                if self._restart_required
                else self._stop_errors
            ),
            pending_analysis_attempt_ids=tuple(
                {
                    *self._attempts,
                    *(
                        action.attempt_id
                        for action in self._child_cancel_actions
                        if action.attempt_id is not None
                    ),
                    *(
                        context.attempt_id
                        for context in self._result_operations.values()
                        if context.attempt_id is not None
                        and not context.report_consumed
                    ),
                }
            ),
            restart_required=self._restart_required,
        )

    def _shutdown_has_context_blockers_locked(self) -> bool:
        return any(self._shutdown_blockers_locked())

    def _shutdown_blockers_locked(
        self,
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        run_contexts = tuple(
            context
            for context in self._runs.values()
            if isinstance(context, _RunContext)
        )
        publishing_contexts = tuple(
            context
            for context in self._runs.values()
            if isinstance(context, _PublishingContext)
        )
        operations = tuple(self._result_operations.values())
        cancel_actions = tuple(self._child_cancel_actions)
        return (
            tuple(
                {
                    *(
                        context.run_id
                        for context in run_contexts
                        if not context.durable_active
                    ),
                    *(
                        context.run_id
                        for context in publishing_contexts
                        if context.reservation is not None
                        and not context.durable_active
                    ),
                    *(
                        action.run_id
                        for action in cancel_actions
                        if not self._child_cancel_action_durable[action]
                    ),
                }
            ),
            tuple(
                {
                    *(
                        context.run_id
                        for context in run_contexts
                        if context.durable_active
                    ),
                    *(
                        context.run_id
                        for context in publishing_contexts
                        if context.reservation is not None
                        and context.durable_active
                    ),
                    *(context.run_id for context in operations),
                    *(
                        action.run_id
                        for action in cancel_actions
                        if self._child_cancel_action_durable[action]
                    ),
                }
            ),
            tuple(
                {
                    *self._attempts,
                    *(
                        action.attempt_id
                        for action in cancel_actions
                        if action.attempt_id is not None
                    ),
                    *(
                        context.attempt_id
                        for context in operations
                        if context.attempt_id is not None
                        and not context.report_consumed
                    ),
                }
            ),
            tuple(
                {
                    *self._save_jobs,
                    *(
                        action.save_job_id
                        for action in cancel_actions
                        if action.save_job_id is not None
                    ),
                }
            ),
            tuple(
                {
                    *(
                        context.run_id
                        for context in publishing_contexts
                        if (
                            context.history_in_flight
                            or context.plc_in_flight
                            or context.plc_unsettled
                        )
                    ),
                    *(
                        action.run_id
                        for action in self._result_history_actions
                    ),
                }
            ),
        )

    def start_captured(self, request: CapturedStartRequest) -> StartResult:
        if not isinstance(request, CapturedStartRequest):
            return self._immediate_rejection(
                self._request_run_id(request),
                StartStatus.INVALID_REQUEST,
                "invalid captured request",
            )
        return self._start_captured(request)

    def start_imported(self, request: ImportedStartRequest) -> StartResult:
        if not isinstance(request, ImportedStartRequest):
            return self._immediate_rejection(
                self._request_run_id(request),
                StartStatus.INVALID_REQUEST,
                "invalid imported request",
            )
        return self._start_imported(request)

    def apply_manual(self, request: ManualJudgmentRequest) -> OperationResult:
        if type(request) is not ManualJudgmentRequest:
            return self._operation_rejection(
                self._request_run_id(request),
                OperationStatus.INVALID_REQUEST,
                "invalid manual judgment request",
            )
        try:
            request.__post_init__()
        except Exception as exc:
            return self._operation_rejection(
                self._request_run_id(request),
                OperationStatus.INVALID_REQUEST,
                self._exception_reason("manual judgment request", exc),
            )
        context, rejection = self._register_result_operation(
            request.run_id,
            request.expected_revision,
            _ResultOperationKind.MANUAL,
        )
        if rejection is not None:
            return rejection
        assert context is not None
        facts, rejection = self._load_result_operation_facts(context)
        if rejection is not None:
            return rejection
        assert facts is not None
        if not facts.manual_judgment_allowed:
            return self._retire_result_operation(
                context,
                OperationStatus.NOT_ALLOWED,
                "manual judgment is not allowed",
            )
        if facts.causal_revision != request.expected_revision:
            return self._retire_result_operation(
                context,
                OperationStatus.STALE_REVISION,
                "manual judgment revision is stale",
            )
        try:
            fault_types = self._frozen_fault_types(facts)
            valid_fault = (
                request.fault_type is None
                if request.conclusion is TerminalConclusion.OK
                else (
                    request.fault_type in fault_types
                    if fault_types
                    else request.fault_type is None
                )
            )
            if not valid_fault:
                raise ValueError(
                    "manual fault_type does not match frozen fault_types"
                )
        except Exception as exc:
            return self._retire_result_operation(
                context,
                OperationStatus.INVALID_REQUEST,
                self._exception_reason("manual fault validation", exc),
            )

        with self._lock:
            if not self._result_operation_is_current(context):
                return self._operation_rejection(
                    context.run_id,
                    OperationStatus.EVICTED,
                    "manual operation is no longer current",
                )
            if context.history_suppressed:
                return self._retire_result_operation(
                    context,
                    OperationStatus.EVICTED,
                    "manual result was evicted before durable commit",
                )
            context.facts = facts
            context.phase = ResultOperationPhase.MANUAL_COMMITTING
            action = self._set_result_operation_action(
                context,
                ExternalActionKind.MANUAL_COMMIT,
            )
        try:
            result = self._persistence.apply_manual_judgment(request, facts)
            commit_error = None
        except Exception as exc:
            result = None
            commit_error = exc
        with self._lock:
            current = self._reconcile_result_operation_action(context, action)
            suppressed = context.history_suppressed if current else True
        if not current:
            return self._operation_rejection(
                context.run_id,
                OperationStatus.EVICTED,
                "manual operation was evicted",
            )
        if commit_error is not None:
            self._diagnose_result_operation(
                "manual persistence", context, commit_error
            )
            return self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                self._exception_reason("manual persistence", commit_error),
            )
        try:
            self._validate_manual_projection_result(result, request)
        except Exception as exc:
            self._diagnose_result_operation("manual persistence", context, exc)
            return self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                self._exception_reason("manual persistence", exc),
            )
        assert isinstance(result, ManualProjectionResult)
        if (
            result.status is not ManualProjectionStatus.COMMITTED
            or result.replacement_projection is None
        ):
            return self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                result.reason or "manual persistence rejected",
            )
        if not suppressed:
            self._dispatch_result_history(
                context,
                HistoryProjectionFact(
                    kind=HistoryFactKind.TERMINAL_RESULT_REPLACED,
                    run_id=context.run_id,
                    expected_revision=context.request_revision,
                    operation_id=None,
                    projection=result.replacement_projection,
                ),
                "manual replacement history",
            )
        return self._retire_result_operation(
            context,
            OperationStatus.COMPLETED,
            "",
        )

    def start_reanalysis(self, request: ReanalysisRequest) -> OperationResult:
        if type(request) is not ReanalysisRequest:
            return self._operation_rejection(
                self._request_run_id(request),
                OperationStatus.INVALID_REQUEST,
                "invalid reanalysis request",
            )
        # Re-run the frozen value validator before any fact I/O, including for
        # callers that bypassed normal dataclass construction.
        try:
            request.__post_init__()
        except Exception as exc:
            return self._operation_rejection(
                self._request_run_id(request),
                OperationStatus.INVALID_REQUEST,
                self._exception_reason("reanalysis request", exc),
            )
        context, rejection = self._register_result_operation(
            request.run_id,
            request.expected_revision,
            _ResultOperationKind.REANALYSIS,
        )
        if rejection is not None:
            return rejection
        assert context is not None
        facts, rejection = self._load_result_operation_facts(context)
        if rejection is not None:
            return rejection
        assert facts is not None
        if not facts.manual_reanalysis_allowed:
            return self._retire_result_operation(
                context,
                OperationStatus.NOT_ALLOWED,
                "manual reanalysis is not allowed",
            )
        if facts.audio is None:
            return self._retire_result_operation(
                context,
                OperationStatus.NOT_FOUND,
                "persisted audio is unavailable",
            )
        try:
            started_projection = self._prepare_reanalysis_started_projection(
                facts
            )
        except Exception as exc:
            self._diagnose_result_operation("reanalysis facts", context, exc)
            return self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                self._exception_reason("reanalysis facts", exc),
            )

        with self._lock:
            if not self._result_operation_is_current(context):
                return self._operation_rejection(
                    context.run_id,
                    OperationStatus.EVICTED,
                    "reanalysis operation is no longer current",
                )
            if context.history_suppressed:
                return self._retire_result_operation(
                    context,
                    OperationStatus.EVICTED,
                    "reanalysis result was evicted before bridge submission",
                )
            context.facts = facts
            context.audio = facts.audio
            context.started_projection = started_projection
            context.phase = ResultOperationPhase.SCHEDULER_SUBMITTING
            action = self._set_result_operation_action(
                context,
                ExternalActionKind.SCHEDULER_SUBMIT,
            )
        bridge_request = BridgeReanalysisRequest(
            run_id=context.run_id,
            analysis_window_start_sec=request.start_sec,
            analysis_window_end_sec=request.end_sec,
        )

        def terminal_sink(report: BridgeTerminalReport) -> None:
            self._on_bridge_reanalysis_report(context, report)

        try:
            result = self._scheduler.submit_reanalysis(
                bridge_request,
                terminal_sink,
            )
            submit_error = None
        except Exception as exc:
            result = None
            submit_error = exc
        return self._reconcile_reanalysis_submission(
            context,
            action,
            result,
            submit_error,
        )

    def cancel_reanalysis(
        self,
        request: ReanalysisCancelRequest,
    ) -> OperationResult:
        if type(request) is not ReanalysisCancelRequest:
            return self._operation_rejection(
                self._request_run_id(request),
                OperationStatus.INVALID_REQUEST,
                "invalid reanalysis cancel request",
            )
        with self._lock:
            context = self._result_operations.get(request.run_id)
            if (
                context is None
                or context.kind is not _ResultOperationKind.REANALYSIS
                or context.attempt_id != request.attempt_id
            ):
                return self._operation_rejection(
                    request.run_id,
                    OperationStatus.NOT_FOUND,
                    "reanalysis operation was not found",
                )
            if context.report_consumed:
                return self._operation_rejection(
                    request.run_id,
                    OperationStatus.NOT_FOUND,
                    "reanalysis operation is already terminal",
                )
            if request.expected_revision != context.terminal_expected_revision:
                return self._operation_rejection(
                    request.run_id,
                    OperationStatus.STALE_REVISION,
                    "reanalysis cancel revision is stale",
                    attempt_id=context.attempt_id,
                )
            if context.user_cancel_attempted:
                return OperationResult(
                    run_id=context.run_id,
                    status=OperationStatus.SUBMITTED,
                    attempt_id=context.attempt_id,
                    reason="",
                )
            context.user_cancel_attempted = True
            attempt_id = context.attempt_id
            action = self._set_result_operation_action(
                context,
                ExternalActionKind.REANALYSIS_CANCEL,
            )
            self._track_child_cancel_action_locked(
                action,
                durable_run=True,
            )
        assert attempt_id is not None
        valid = self._request_bridge_reanalysis_cancel(
            context,
            attempt_id,
            AnalysisCancelOrigin.USER_STOP,
            "user requested reanalysis stop",
            action=action,
        )
        if not valid:
            return self._operation_rejection(
                context.run_id,
                OperationStatus.SCHEDULER_REJECTED,
                "bridge reanalysis cancel failed",
                attempt_id=attempt_id,
            )
        return OperationResult(
            run_id=context.run_id,
            status=OperationStatus.SUBMITTED,
            attempt_id=attempt_id,
            reason="",
        )

    def history_evicted(self, notice: HistoryEvictionNotice) -> bool:
        if not isinstance(notice, HistoryEvictionNotice):
            return False
        with self._lock:
            context = self._result_operations.get(notice.run_id)
            if context is None or context.history_suppressed:
                return False
            context.eviction_requested = True
            context.history_suppressed = True
            if (
                context.kind is _ResultOperationKind.REANALYSIS
                and context.attempt_id is not None
                and not context.report_consumed
                and not context.eviction_cancel_attempted
            ):
                context.eviction_cancel_attempted = True
                attempt_id = context.attempt_id
                action = self._set_result_operation_action(
                    context,
                    ExternalActionKind.REANALYSIS_CANCEL,
                )
                self._track_child_cancel_action_locked(
                    action,
                    durable_run=True,
                )
            else:
                attempt_id = None
                action = None
        if attempt_id is not None:
            self._request_bridge_reanalysis_cancel(
                context,
                attempt_id,
                AnalysisCancelOrigin.RESULT_EVICTION,
                "result history evicted",
                action=action,
            )
        return True

    def _register_result_operation(
        self,
        run_id: str,
        expected_revision: int,
        kind: _ResultOperationKind,
    ) -> tuple[_ResultOperationContext | None, OperationResult | None]:
        with self._lock:
            if (
                self._lifecycle is not CoordinatorLifecycle.OPEN
                or self._restart_required
            ):
                return None, self._operation_rejection(
                    run_id,
                    OperationStatus.INGRESS_CLOSED,
                    "coordinator result-operation admission is closed",
                )
            existing = self._result_operations.get(run_id)
            if existing is not None:
                return None, self._operation_rejection(
                    run_id,
                    OperationStatus.OPERATION_BUSY,
                    "a result operation is already in progress",
                    attempt_id=existing.attempt_id,
                )
            context = _ResultOperationContext(
                run_id=run_id,
                kind=kind,
                request_revision=expected_revision,
                terminal_expected_revision=expected_revision + 1,
                last_published_revision=expected_revision,
            )
            self._result_operations[run_id] = context
            context.action = ExternalAction(
                kind=ExternalActionKind.RESULT_FACTS_LOAD,
                run_id=run_id,
                expected_phase=ResultOperationPhase.LOADING_FACTS,
            )
            return context, None

    def _load_result_operation_facts(
        self,
        context: _ResultOperationContext,
    ) -> tuple[ResultControlFacts | None, OperationResult | None]:
        action = context.action
        assert action is not None
        try:
            facts = self._persistence.load_result_control_facts(context.run_id)
            load_error = None
        except Exception as exc:
            facts = None
            load_error = exc
        with self._lock:
            current = self._reconcile_result_operation_action(context, action)
        if not current:
            return None, self._operation_rejection(
                context.run_id,
                OperationStatus.EVICTED,
                "result operation was evicted while loading facts",
            )
        if load_error is not None:
            self._diagnose_result_operation(
                "result facts load", context, load_error
            )
            return None, self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                self._exception_reason("result facts load", load_error),
            )
        if facts is None:
            return None, self._retire_result_operation(
                context,
                OperationStatus.NOT_FOUND,
                "result-control facts were not found",
            )
        try:
            self._validate_result_control_facts(facts, context.run_id)
        except Exception as exc:
            self._diagnose_result_operation("result facts load", context, exc)
            return None, self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                self._exception_reason("result facts load", exc),
            )
        assert isinstance(facts, ResultControlFacts)
        return facts, None

    @staticmethod
    def _validate_frozen_payload(value: object, field_name: str) -> None:
        if type(value) is not FrozenPayload:
            raise TypeError(f"{field_name} must be an exact FrozenPayload")
        value.__post_init__()

    @classmethod
    def _validate_result_projection(
        cls,
        value: object,
        field_name: str,
    ) -> None:
        if type(value) is not ResultProjection:
            raise TypeError(f"{field_name} must be an exact ResultProjection")
        value.__post_init__()
        cls._validate_frozen_payload(
            value.history_labels,
            f"{field_name}.history_labels",
        )
        if value.result_payload is not None:
            cls._validate_frozen_payload(
                value.result_payload,
                f"{field_name}.result_payload",
            )

    @classmethod
    def _validate_result_control_facts(
        cls,
        value: object,
        expected_run_id: str,
    ) -> None:
        if type(value) is not ResultControlFacts:
            raise TypeError("result-control facts must be exact")
        value.__post_init__()
        if value.run_id != expected_run_id:
            raise ValueError("result-control facts run identity does not match")
        cls._validate_frozen_payload(
            value.config_payload,
            "result_control_facts.config_payload",
        )
        cls._validate_frozen_payload(
            value.calibration_payload,
            "result_control_facts.calibration_payload",
        )
        cls._validate_frozen_payload(
            value.analysis_input,
            "result_control_facts.analysis_input",
        )
        if value.audio is not None:
            if type(value.audio) is not PersistedAudioReference:
                raise TypeError(
                    "result_control_facts.audio must be an exact "
                    "PersistedAudioReference"
                )
            value.audio.__post_init__()
        cls._validate_result_projection(
            value.current_projection,
            "result_control_facts.current_projection",
        )

    @classmethod
    def _validate_manual_projection_result(
        cls,
        value: object,
        request: ManualJudgmentRequest,
    ) -> None:
        if type(value) is not ManualProjectionResult:
            raise TypeError("manual persistence result must be exact")
        value.__post_init__()
        if value.run_id != request.run_id:
            raise ValueError("manual persistence result run identity does not match")
        if value.status is ManualProjectionStatus.COMMITTED:
            if value.committed_revision != request.expected_revision + 1:
                raise ValueError("manual committed revision does not match request")
            projection = value.replacement_projection
            if (
                projection is None
                or projection.conclusion is not request.conclusion
                or projection.fault_type != request.fault_type
                or projection.manual_judgment_revision
                != value.committed_revision
            ):
                raise ValueError("manual replacement projection does not match request")
        if value.replacement_projection is not None:
            cls._validate_result_projection(
                value.replacement_projection,
                "manual_projection_result.replacement_projection",
            )

    @classmethod
    def _prepare_reanalysis_started_projection(
        cls,
        facts: ResultControlFacts,
    ) -> ResultProjection:
        prior = facts.current_projection
        if prior.phase is not ResultProjectionPhase.TERMINAL:
            raise ValueError(
                "reanalysis requires a terminal prior result projection"
            )
        projection = ResultProjection(
            phase=ResultProjectionPhase.REANALYZING,
            conclusion=prior.conclusion,
            error_category=prior.error_category,
            reason=prior.reason,
            history_labels=prior.history_labels,
            result_payload=prior.result_payload,
            fault_type=prior.fault_type,
            manual_judgment_revision=prior.manual_judgment_revision,
        )
        cls._validate_result_projection(
            projection,
            "reanalysis_started_projection",
        )
        return projection

    def _reconcile_reanalysis_submission(
        self,
        context: _ResultOperationContext,
        action: ExternalAction,
        result: object,
        submit_error: Exception | None,
    ) -> OperationResult:
        invalid_reason = None
        if submit_error is not None:
            invalid_reason = self._exception_reason(
                "bridge reanalysis submit", submit_error
            )
        elif type(result) is not BridgeSubmissionResult:
            invalid_reason = "bridge returned an invalid reanalysis result"
        else:
            try:
                result.__post_init__()
            except Exception as exc:
                invalid_reason = self._exception_reason(
                    "bridge reanalysis result", exc
                )
            else:
                if result.run_id != context.run_id:
                    invalid_reason = (
                        "bridge reanalysis result run identity does not match"
                    )
        with self._lock:
            current = self._reconcile_result_operation_action(context, action)
            diagnostic = None
            settled_result = None
            held = None
            suppressed = False
            collision = False
            attempt_id = None
            if current and context.report_channel_invalid:
                context.submission_reconciled = True
                settled_result = self._retire_result_operation(
                    context,
                    OperationStatus.PERSISTENCE_REJECTED,
                    "bridge reanalysis report channel is quarantined",
                )
            elif current and invalid_reason is not None:
                context.submission_reconciled = True
                if context.early_report is not None:
                    diagnostic = (
                        self._quarantine_result_report_channel_locked(
                            context,
                            "bridge reanalysis report",
                            "terminal report contradicted invalid submission result",
                        )
                    )
                else:
                    diagnostic = self._capture_result_restart_diagnostic_locked(
                        context,
                        "bridge reanalysis submit",
                        invalid_reason,
                    )
                settled_result = self._retire_result_operation(
                    context,
                    OperationStatus.PERSISTENCE_REJECTED,
                    invalid_reason,
                )
            elif current:
                assert isinstance(result, BridgeSubmissionResult)
                if result.authority is BridgeAuthorityStatus.NOT_ACQUIRED:
                    context.submission_reconciled = True
                    if context.early_report is not None:
                        diagnostic = (
                            self._quarantine_result_report_channel_locked(
                                context,
                                "bridge reanalysis report",
                                "terminal report contradicted NOT_ACQUIRED authority",
                            )
                        )
                    elif result.restart_required:
                        diagnostic = (
                            self._capture_result_restart_diagnostic_locked(
                                context,
                                "bridge reanalysis restart required",
                                result.reason
                                or result.code
                                or "bridge reanalysis requires restart",
                            )
                        )
                    settled_result = self._retire_result_operation(
                        context,
                        OperationStatus.SCHEDULER_REJECTED,
                        result.reason
                        or result.code
                        or "bridge did not acquire authority",
                    )
                elif result.authority is BridgeAuthorityStatus.UNKNOWN:
                    context.submission_reconciled = True
                    if context.early_report is not None:
                        diagnostic = (
                            self._quarantine_result_report_channel_locked(
                                context,
                                "bridge reanalysis report",
                                "terminal report contradicted UNKNOWN authority",
                            )
                        )
                    else:
                        diagnostic = (
                            self._capture_result_restart_diagnostic_locked(
                                context,
                                "bridge reanalysis submit",
                                result.reason
                                or result.code
                                or "bridge authority is unknown",
                            )
                        )
                    settled_result = self._retire_result_operation(
                        context,
                        OperationStatus.PERSISTENCE_REJECTED,
                        result.reason
                        or result.code
                        or "bridge authority is unknown",
                    )
                else:
                    attempt_id = result.analysis_attempt_id
                    assert attempt_id is not None
                    collision = attempt_id in self._attempts or any(
                        other is not context and other.attempt_id == attempt_id
                        for other in self._result_operations.values()
                    )
                    if collision:
                        context.submission_reconciled = True
                        diagnostic = (
                            self._capture_result_restart_diagnostic_locked(
                                context,
                                "bridge reanalysis submit",
                                "bridge attempt identity collides with a live context",
                            )
                        )
                        settled_result = self._retire_result_operation(
                            context,
                            OperationStatus.PERSISTENCE_REJECTED,
                            "bridge attempt identity collision",
                        )
                    else:
                        context.attempt_id = attempt_id
                        context.phase = ResultOperationPhase.ANALYSIS_PENDING
                        context.bridge_submission_result = result
                        context.submission_reconciled = True
                        held = context.early_report
                        context.early_report = None
                        suppressed = context.history_suppressed
                        if result.terminal_delivered_before_return and held is None:
                            diagnostic = (
                                self._quarantine_result_report_channel_locked(
                                    context,
                                    "bridge reanalysis submit",
                                    "early-delivery result has no matching held report",
                                )
                            )
                            settled_result = self._retire_result_operation(
                                context,
                                OperationStatus.PERSISTENCE_REJECTED,
                                "bridge early-delivery result is contradictory",
                            )
                        elif held is not None:
                            try:
                                self._validate_reanalysis_terminal_report(held)
                                held_validation_error = None
                            except Exception as exc:
                                held_validation_error = self._exception_reason(
                                    "bridge terminal report",
                                    exc,
                                )
                            if held_validation_error is not None:
                                diagnostic = (
                                    self._quarantine_result_report_channel_locked(
                                        context,
                                        "bridge reanalysis report",
                                        held_validation_error,
                                    )
                                )
                                settled_result = self._retire_result_operation(
                                    context,
                                    OperationStatus.PERSISTENCE_REJECTED,
                                    "bridge terminal report is malformed",
                                )
                            elif not (
                                self._reanalysis_submission_report_is_coherent(
                                    result,
                                    held,
                                )
                            ):
                                diagnostic = (
                                    self._quarantine_result_report_channel_locked(
                                        context,
                                        "bridge reanalysis report",
                                        "bridge submission result contradicts held terminal report",
                                    )
                                )
                                settled_result = self._retire_result_operation(
                                    context,
                                    OperationStatus.PERSISTENCE_REJECTED,
                                    "bridge submission result contradicts terminal report",
                                )
        if not current:
            return self._operation_rejection(
                context.run_id,
                OperationStatus.EVICTED,
                "reanalysis operation is no longer current",
            )
        self._emit_result_report_violation(diagnostic)
        if settled_result is not None:
            return settled_result
        assert isinstance(result, BridgeSubmissionResult)
        assert result.authority is BridgeAuthorityStatus.ACQUIRED
        assert attempt_id is not None

        started_projection = context.started_projection
        assert started_projection is not None
        if not suppressed:
            self._dispatch_result_history(
                context,
                HistoryProjectionFact(
                    kind=HistoryFactKind.REANALYSIS_STARTED,
                    run_id=context.run_id,
                    expected_revision=context.request_revision,
                    operation_id=attempt_id,
                    projection=started_projection,
                ),
                "reanalysis started history",
            )
        with self._lock:
            if not self._result_operation_is_current(context):
                return OperationResult(
                    run_id=context.run_id,
                    status=OperationStatus.SUBMITTED,
                    attempt_id=attempt_id,
                    reason="",
                )
            if context.report_channel_invalid:
                invalid_after_history = True
                request_user_cancel = False
                request_eviction_cancel = False
            else:
                invalid_after_history = False
            context.started_dispatched = True
            if context.early_report is not None:
                if held is None:
                    held = context.early_report
                context.early_report = None
            if not invalid_after_history:
                request_user_cancel = (
                    held is None
                    and context.lifecycle_cancel_requested
                    and not context.user_cancel_attempted
                )
                if request_user_cancel:
                    context.user_cancel_attempted = True
                request_eviction_cancel = (
                    held is None
                    and not request_user_cancel
                    and context.eviction_requested
                    and not context.eviction_cancel_attempted
                )
                if request_eviction_cancel:
                    context.eviction_cancel_attempted = True
                if request_user_cancel or request_eviction_cancel:
                    cancel_action = self._set_result_operation_action(
                        context,
                        ExternalActionKind.REANALYSIS_CANCEL,
                    )
                    self._track_child_cancel_action_locked(
                        cancel_action,
                        durable_run=True,
                    )
                else:
                    cancel_action = None
        if invalid_after_history:
            return self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                "bridge reanalysis report channel is quarantined",
            )
        if request_user_cancel:
            self._request_bridge_reanalysis_cancel(
                context,
                attempt_id,
                AnalysisCancelOrigin.USER_STOP,
                "Coordinator lifecycle stop requested",
                action=cancel_action,
                lifecycle_requested=True,
            )
        elif request_eviction_cancel:
            self._request_bridge_reanalysis_cancel(
                context,
                attempt_id,
                AnalysisCancelOrigin.RESULT_EVICTION,
                "result history evicted",
                action=cancel_action,
            )
        if held is not None:
            self._consume_bridge_reanalysis_report(context, held)
        return OperationResult(
            run_id=context.run_id,
            status=OperationStatus.SUBMITTED,
            attempt_id=attempt_id,
            reason="",
        )

    def _on_bridge_reanalysis_report(
        self,
        context: _ResultOperationContext,
        report: object,
    ) -> None:
        violation_diagnostic = None
        violation = False
        with self._lock:
            if context.report_channel_invalid:
                return
            try:
                self._validate_reanalysis_terminal_report(report)
                validation_error = None
            except Exception as exc:
                validation_error = self._exception_reason(
                    "bridge terminal report",
                    exc,
                )
            if validation_error is not None:
                context.report_delivery_seen = True
                violation = True
                violation_diagnostic = (
                    self._quarantine_result_report_channel_locked(
                        context,
                        "bridge reanalysis report",
                        validation_error,
                    )
                )
                if context.submission_reconciled and context.started_dispatched:
                    self._retire_result_operation(
                        context,
                        OperationStatus.PERSISTENCE_REJECTED,
                        "bridge terminal report is malformed",
                    )
            else:
                if context.report_delivery_seen:
                    duplicate = True
                    violation_reason = "duplicate bridge terminal report delivery"
                else:
                    context.report_delivery_seen = True
                    if (
                        context.report_consumed
                        or context.channel_retired
                        or not self._result_operation_is_current(context)
                    ):
                        duplicate = True
                        violation_reason = (
                            "bridge terminal report delivered after channel retirement"
                        )
                    else:
                        duplicate = False
                        violation_reason = ""
                if (
                    not duplicate
                    and context.submission_reconciled
                    and (
                        context.bridge_submission_result is None
                        or not self._reanalysis_submission_report_is_coherent(
                            context.bridge_submission_result,
                            report,
                        )
                    )
                ):
                    duplicate = True
                    violation_reason = (
                        "bridge submission result contradicts terminal report"
                    )
                if (
                    not duplicate
                    and (
                        not context.submission_reconciled
                        or not context.started_dispatched
                    )
                ):
                    if context.early_report is None:
                        context.early_report = report
                        return
                    duplicate = True
                    violation_reason = "bridge terminal report slot is occupied"
                if duplicate:
                    violation = True
                    violation_diagnostic = (
                        self._quarantine_result_report_channel_locked(
                            context,
                            "bridge reanalysis report",
                            violation_reason,
                        )
                    )
        if violation:
            self._emit_result_report_violation(violation_diagnostic)
            return
        self._consume_bridge_reanalysis_report(context, report)

    def _consume_bridge_reanalysis_report(
        self,
        context: _ResultOperationContext,
        report: object,
    ) -> None:
        with self._lock:
            diagnostic = None
            try:
                self._validate_reanalysis_terminal_report(report)
                validation_error = None
            except Exception as exc:
                validation_error = self._exception_reason(
                    "bridge terminal report",
                    exc,
                )
            if validation_error is not None:
                valid = False
                diagnostic = self._quarantine_result_report_channel_locked(
                    context,
                    "bridge reanalysis report",
                    validation_error,
                )
                self._retire_result_operation(
                    context,
                    OperationStatus.PERSISTENCE_REJECTED,
                    "bridge terminal report is malformed",
                )
            else:
                valid = (
                    self._result_operation_is_current(context)
                    and not context.report_channel_invalid
                    and not context.report_consumed
                    and context.submission_reconciled
                    and context.started_dispatched
                    and context.bridge_submission_result is not None
                    and self._reanalysis_submission_report_is_coherent(
                        context.bridge_submission_result,
                        report,
                    )
                )
                if valid:
                    context.report_consumed = True
                    suppressed = context.history_suppressed
                else:
                    diagnostic = (
                        self._quarantine_result_report_channel_locked(
                            context,
                            "bridge reanalysis report",
                            "bridge terminal report identity or phase does not match",
                        )
                    )
                    self._retire_result_operation(
                        context,
                        OperationStatus.PERSISTENCE_REJECTED,
                        "bridge terminal report identity or phase does not match",
                    )
        self._emit_result_report_violation(diagnostic)
        if not valid:
            return
        assert isinstance(report, BridgeTerminalReport)
        if report.status is not BridgeTerminalStatus.COMMITTED:
            self._latch_result_operation_restart(
                context,
                "bridge reanalysis report",
                report.summary or report.code,
            )
            self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                report.summary or report.code,
            )
            return
        try:
            kind, projection = self._map_committed_reanalysis_report(
                context,
                report,
            )
        except Exception as exc:
            self._latch_result_operation_restart(
                context,
                "bridge reanalysis report",
                self._exception_reason("bridge report mapping", exc),
            )
            self._retire_result_operation(
                context,
                OperationStatus.PERSISTENCE_REJECTED,
                "bridge reanalysis report mapping failed",
            )
            return
        if not suppressed:
            self._dispatch_result_history(
                context,
                HistoryProjectionFact(
                    kind=kind,
                    run_id=context.run_id,
                    expected_revision=context.terminal_expected_revision,
                    operation_id=context.attempt_id,
                    projection=projection,
                ),
                "reanalysis terminal history",
            )
        self._retire_result_operation(
            context,
            OperationStatus.COMPLETED,
            "",
        )

    @staticmethod
    def _validate_reanalysis_terminal_report(report: object) -> None:
        if type(report) is not BridgeTerminalReport:
            raise TypeError("bridge terminal report type is invalid")
        report.__post_init__()

    @staticmethod
    def _reanalysis_submission_report_is_coherent(
        result: BridgeSubmissionResult,
        report: object,
    ) -> bool:
        if (
            type(report) is not BridgeTerminalReport
            or report.run_id != result.run_id
            or report.analysis_attempt_id != result.analysis_attempt_id
            or report.attempt_kind is not BridgeAttemptKind.REANALYSIS
        ):
            return False
        if result.status is BridgeSubmissionStatus.ACCEPTED:
            # The sink may run after the bridge's locked acceptance point but
            # before submit_reanalysis returns to this caller.
            return True
        if result.status is BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL:
            return report.status is BridgeTerminalStatus.COMMITTED
        if result.status is BridgeSubmissionStatus.REJECTED:
            return (
                report.status is BridgeTerminalStatus.COMMITTED
                and report.code == result.code
                and report.summary == result.reason
            )
        if result.status is BridgeSubmissionStatus.STATE_UNKNOWN:
            return (
                report.status is not BridgeTerminalStatus.COMMITTED
                and report.code == result.code
                and report.summary == result.reason
            )
        return False

    @staticmethod
    def _map_committed_reanalysis_report(
        context: _ResultOperationContext,
        report: BridgeTerminalReport,
    ) -> tuple[HistoryFactKind, ResultProjection]:
        facts = context.facts
        if facts is None:
            raise ValueError("reanalysis facts are unavailable")
        prior = facts.current_projection
        if report.attempt_state is BridgeAttemptState.SUCCEEDED:
            if (
                report.code != "ANALYSIS_SUCCEEDED"
                or report.result_payload_json is None
            ):
                raise ValueError("successful reanalysis report is invalid")
            conclusion = {
                BridgeJudgment.PASS: TerminalConclusion.OK,
                BridgeJudgment.FAIL: TerminalConclusion.NG,
            }.get(report.judgment)
            if conclusion is None:
                raise ValueError("successful reanalysis judgment is invalid")
            projection = ResultProjection(
                phase=ResultProjectionPhase.TERMINAL,
                conclusion=conclusion,
                error_category=None,
                reason="",
                history_labels=prior.history_labels,
                result_payload=FrozenPayload(
                    value_id=f"bridge-result:{report.analysis_attempt_id}",
                    canonical_bytes=report.result_payload_json.encode("utf-8"),
                ),
            )
            return HistoryFactKind.REANALYSIS_COMPLETED, projection
        if report.attempt_state is BridgeAttemptState.NO_RESULT:
            if report.code != "ANALYSIS_NO_RESULT":
                raise ValueError("no-result reanalysis report is invalid")
            return (
                HistoryFactKind.REANALYSIS_COMPLETED,
                ResultProjection(
                    phase=ResultProjectionPhase.TERMINAL,
                    conclusion=TerminalConclusion.NO_RESULT,
                    error_category=None,
                    reason="",
                    history_labels=prior.history_labels,
                    result_payload=None,
                ),
            )
        if report.attempt_state is BridgeAttemptState.FAILED:
            if report.code not in _INITIAL_BRIDGE_FAILED_CODES:
                raise ValueError("failed reanalysis report code is invalid")
            return HistoryFactKind.REANALYSIS_COMPLETED, prior
        if report.attempt_state is BridgeAttemptState.CANCELED:
            if report.code not in {
                "REANALYSIS_CANCELED",
                "REANALYSIS_EVICTED",
                "ANALYSIS_SCHEDULER_SHUTDOWN",
            }:
                raise ValueError("canceled reanalysis report is invalid")
            return HistoryFactKind.REANALYSIS_CANCELLED, prior
        raise ValueError("reanalysis terminal state is invalid")

    def _request_bridge_reanalysis_cancel(
        self,
        context: _ResultOperationContext,
        attempt_id: str,
        origin: AnalysisCancelOrigin,
        reason: str,
        *,
        action: ExternalAction,
        lifecycle_requested: bool = False,
    ) -> bool:
        request = BridgeCancelRequest(
            run_id=context.run_id,
            analysis_attempt_id=attempt_id,
            origin=origin,
            reason=self._bounded(reason),
        )
        try:
            result = self._scheduler.cancel(request)
        except Exception as exc:
            dispatch_error = exc
            valid = False
        else:
            dispatch_error = None
            try:
                if type(result) is not BridgeCancelResult:
                    raise TypeError("bridge cancel returned an invalid result")
                result.__post_init__()
                if (
                    result.run_id != context.run_id
                    or result.analysis_attempt_id != attempt_id
                    or result.origin is not origin
                    or result.status
                    not in {
                        BridgeCancelStatus.CANCEL_REQUESTED,
                        BridgeCancelStatus.ALREADY_TERMINAL,
                    }
                ):
                    raise ValueError(
                        "bridge cancel result is mismatched or rejected"
                    )
            except Exception as exc:
                self._diagnose_result_operation(
                    "bridge reanalysis cancel", context, exc
                )
                valid = False
            else:
                valid = True
        if dispatch_error is not None:
            if lifecycle_requested:
                with self._lock:
                    self._record_lifecycle_error_locked(
                        self._exception_reason(
                            "bridge reanalysis cancel",
                            dispatch_error,
                        )
                    )
                    if self._shutdown_begun:
                        self._restart_required = True
                        self._lifecycle = CoordinatorLifecycle.CLOSED
                        self._promote_stop_errors_locked()
            self._diagnose_result_operation(
                "bridge reanalysis cancel",
                context,
                dispatch_error,
            )
        with self._lock:
            reconciled = self._reconcile_result_operation_action(
                context,
                action,
            )
            self._retire_child_cancel_action_locked(action)
            if not reconciled:
                self._condition.notify_all()
        return valid

    def _dispatch_result_history(
        self,
        context: _ResultOperationContext,
        fact: HistoryProjectionFact,
        stage: str,
    ) -> None:
        with self._lock:
            if (
                not self._result_operation_is_current(context)
                or context.history_suppressed
                or context.report_channel_invalid
            ):
                return
            history_action = ExternalAction(
                kind=(
                    ExternalActionKind.ANALYZING_HISTORY
                    if fact.kind is HistoryFactKind.REANALYSIS_STARTED
                    else ExternalActionKind.TERMINAL_HISTORY
                ),
                run_id=context.run_id,
                expected_phase=context.phase,
                attempt_id=context.attempt_id,
            )
            self._result_history_actions.add(history_action)
        try:
            try:
                event = self._history_projector.project(fact)
                event = self._validate_projected_history_event(fact, event)
            except Exception as exc:
                self._diagnose_result_operation(
                    f"{stage} projector",
                    context,
                    exc,
                )
                return
            with self._lock:
                if (
                    not self._result_operation_is_current(context)
                    or context.history_suppressed
                    or context.report_channel_invalid
                ):
                    return
            try:
                self._history_sink.publish(event)
                sink_error = None
            except Exception as exc:
                sink_error = exc
            with self._lock:
                context.last_published_revision = event.proposed_revision
            if sink_error is not None:
                self._diagnose_result_operation(
                    f"{stage} sink",
                    context,
                    sink_error,
                )
        finally:
            with self._lock:
                self._result_history_actions.discard(history_action)
                self._condition.notify_all()

    def _set_result_operation_action(
        self,
        context: _ResultOperationContext,
        kind: ExternalActionKind,
    ) -> ExternalAction:
        if context.action is not None:
            raise RuntimeError("result operation already has an external action")
        action = ExternalAction(
            kind=kind,
            run_id=context.run_id,
            expected_phase=context.phase,
            attempt_id=context.attempt_id,
        )
        context.action = action
        return action

    def _reconcile_result_operation_action(
        self,
        context: _ResultOperationContext,
        action: ExternalAction,
    ) -> bool:
        if (
            not self._result_operation_is_current(context)
            or context.action is not action
        ):
            return False
        context.action = None
        self._condition.notify_all()
        return True

    def _result_operation_is_current(
        self,
        context: _ResultOperationContext,
    ) -> bool:
        return self._result_operations.get(context.run_id) is context

    def _retire_result_operation(
        self,
        context: _ResultOperationContext,
        status: OperationStatus,
        reason: str,
    ) -> OperationResult:
        attempt_id = context.attempt_id
        with self._lock:
            if self._result_operation_is_current(context):
                self._result_operations.pop(context.run_id, None)
                context.action = None
                context.facts = None
                context.audio = None
                context.started_projection = None
                context.early_report = None
                context.bridge_submission_result = None
                context.channel_retired = True
                self._condition.notify_all()
        return OperationResult(
            run_id=context.run_id,
            status=status,
            attempt_id=(
                attempt_id
                if status in {
                    OperationStatus.SUBMITTED,
                    OperationStatus.OPERATION_BUSY,
                }
                else None
            ),
            reason=self._bounded(reason),
        )

    def _latch_result_operation_restart(
        self,
        context: _ResultOperationContext,
        stage: str,
        reason: str,
    ) -> None:
        with self._lock:
            self._restart_required = True
            self._lifecycle = CoordinatorLifecycle.CLOSED
            self._condition.notify_all()
        self._diagnose_result_operation(
            stage,
            context,
            RuntimeError(reason),
        )

    def _quarantine_result_report_channel(
        self,
        context: _ResultOperationContext,
        stage: str,
        reason: str,
    ) -> None:
        with self._lock:
            diagnostic = self._quarantine_result_report_channel_locked(
                context,
                stage,
                reason,
            )
        self._emit_result_report_violation(diagnostic)

    def _quarantine_result_report_channel_locked(
        self,
        context: _ResultOperationContext,
        stage: str,
        reason: str,
    ) -> _ResultReportViolationDiagnostic | None:
        if context.report_channel_invalid:
            return None
        context.report_channel_invalid = True
        context.early_report = None
        context.history_suppressed = True
        self._restart_required = True
        self._lifecycle = CoordinatorLifecycle.CLOSED
        should_diagnose = not context.report_violation_diagnosed
        context.report_violation_diagnosed = True
        self._condition.notify_all()
        if not should_diagnose:
            return None
        return _ResultReportViolationDiagnostic(
            stage=self._bounded(stage),
            reason=self._bounded(reason),
            run_id=context.run_id,
            attempt_id=context.attempt_id,
        )

    def _capture_result_restart_diagnostic_locked(
        self,
        context: _ResultOperationContext,
        stage: str,
        reason: str,
    ) -> _ResultReportViolationDiagnostic:
        self._restart_required = True
        self._lifecycle = CoordinatorLifecycle.CLOSED
        context.report_violation_diagnosed = True
        self._condition.notify_all()
        return _ResultReportViolationDiagnostic(
            stage=self._bounded(stage),
            reason=self._bounded(reason),
            run_id=context.run_id,
            attempt_id=context.attempt_id,
        )

    def _emit_result_report_violation(
        self,
        diagnostic: _ResultReportViolationDiagnostic | None,
    ) -> None:
        if diagnostic is None:
            return
        self._diagnose_ids(
            diagnostic.stage,
            TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
            RuntimeError(diagnostic.reason),
            run_id=diagnostic.run_id,
            attempt_id=diagnostic.attempt_id,
            save_job_id=None,
        )

    def _diagnose_result_operation(
        self,
        stage: str,
        context: _ResultOperationContext,
        error: Exception,
    ) -> None:
        self._diagnose_ids(
            stage,
            TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
            error,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            save_job_id=None,
        )

    def _operation_rejection(
        self,
        run_id: str,
        status: OperationStatus,
        reason: str,
        *,
        attempt_id: str | None = None,
    ) -> OperationResult:
        return OperationResult(
            run_id=run_id,
            status=status,
            attempt_id=attempt_id,
            reason=self._bounded(reason),
        )

    @staticmethod
    def _request_run_id(request: object) -> str:
        run_id = getattr(request, "run_id", "")
        try:
            return require_uuid4(run_id, "run_id")
        except (ValueError, TypeError, AttributeError):
            # StartResult itself requires a canonical identity. This fixed value is
            # used only when an untyped caller violates the request contract.
            return "ffffffff-ffff-4fff-8fff-ffffffffffff"

    def _immediate_rejection(
        self, run_id: str, status: StartStatus, reason: str
    ) -> StartResult:
        return StartResult(
            run_id=run_id,
            status=status,
            accepted=False,
            phase=None,
            terminal=None,
            reason=self._bounded(reason),
        )

    def _register_context(
        self, request: CapturedStartRequest | ImportedStartRequest
    ) -> tuple[_RunContext | None, StartResult | None]:
        with self._lock:
            if self._lifecycle is not CoordinatorLifecycle.OPEN:
                return None, self._immediate_rejection(
                    request.run_id,
                    StartStatus.INGRESS_CLOSED,
                    "coordinator ingress is closed",
                )
            if request.run_id in self._runs:
                return None, self._immediate_rejection(
                    request.run_id,
                    StartStatus.DUPLICATE_RUN_ID,
                    "run ID already has a live context",
                )
            context = _RunContext(request=request)
            self._runs[request.run_id] = context
            return context, None

    def _start_captured(self, request: CapturedStartRequest) -> StartResult:
        context, rejection = self._register_context(request)
        if rejection is not None:
            return rejection
        assert context is not None

        if request.source is TriggerSource.PLC:
            with self._lock:
                action = self._try_set_next_action_locked(
                    context,
                    ExternalActionKind.PLC_REGISTER,
                    CoordinatorPhase.PREFLIGHT,
                )
            if action is None:
                return self._current_start_result(context)
            try:
                registration = self._plc.register(
                    run_id=request.run_id,
                    trigger_source=request.source.name.lower(),
                    operation_kind=PlcOperationKind.INITIAL,
                    plc_id=request.plc_id,
                )
                register_error = None
            except Exception as exc:
                registration = None
                register_error = exc
            with self._lock:
                current = self._reconcile_action(context, action)
                if (
                    current
                    and type(registration) is PlcRegistrationResult
                    and registration.disposition
                    is PlcMutationDisposition.APPLIED
                    and type(registration.ticket) is PlcIngressTicket
                    and registration.ticket.run_id == request.run_id
                    and type(registration.ticket.plc_id) is int
                    and registration.ticket.plc_id == request.plc_id
                    and registration.ticket.plc_id > 0
                    and type(registration.ticket.plc_sequence) is int
                    and registration.ticket.plc_sequence > 0
                ):
                    context.ticket = registration.ticket
                acquired = context.ticket is not None
                canceled = context.cancel_requested
            if not current:
                return self._current_start_result(context)
            if register_error is not None or not acquired:
                reason = self._exception_reason(
                    "PLC register",
                    register_error
                    if register_error is not None
                    else RuntimeError(
                        "PLC registration returned a non-APPLIED or malformed result"
                    ),
                )
                with self._lock:
                    if self._is_current(context):
                        self._runs.pop(context.run_id, None)
                        self._condition.notify_all()
                self._diagnose(
                    "PLC registration",
                    context,
                    TerminalErrorCategory.PLC_INGRESS_REJECTED,
                    register_error
                    if register_error is not None
                    else RuntimeError(reason),
                )
                return self._immediate_rejection(
                    context.run_id,
                    StartStatus.PLC_INGRESS_REJECTED,
                    reason,
                )
            if canceled:
                return self._publish_preaccept(
                    context,
                    StartStatus.PREFLIGHT_REJECTED,
                    TerminalErrorCategory.PREFLIGHT_REJECTED,
                    "ingress canceled during PLC registration",
                )

        frozen, failure = self._freeze_captured(context)
        if failure is not None:
            return failure
        assert frozen is not None

        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.RESIDENT_RESERVE,
                CoordinatorPhase.PREFLIGHT,
                next_phase=CoordinatorPhase.RESERVING,
            )
        if action is None:
            return self._current_start_result(context)
        try:
            reserve = self._resident.reserve(
                request.run_id, frozen.hardware_id
            )
            reserve_error = None
        except Exception as exc:
            reserve = None
            reserve_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            if (
                current
                and reserve is not None
                and reserve.status is CaptureReserveStatus.RESERVED
                and reserve.run_id == request.run_id
                and reserve.hardware_id == frozen.hardware_id
                and reserve.reservation is not None
            ):
                context.reservation = reserve.reservation
            preaccept_reason = self._pop_preaccept_contender_locked(
                context, "Resident reserve"
            )
        if not current:
            return self._current_start_result(context)
        if preaccept_reason is not None:
            self._cancel_reservation_once(context)
            return self._publish_preaccept(
                context,
                StartStatus.PREFLIGHT_REJECTED,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                preaccept_reason,
            )
        if reserve_error is not None:
            return self._publish_preaccept(
                context,
                StartStatus.RESIDENT_UNAVAILABLE,
                TerminalErrorCategory.RESIDENT_UNAVAILABLE,
                self._exception_reason("Resident reserve", reserve_error),
            )
        assert reserve is not None
        if reserve.status is CaptureReserveStatus.CAPACITY:
            return self._publish_preaccept(
                context,
                StartStatus.CAPTURE_CAPACITY,
                TerminalErrorCategory.CAPTURE_CAPACITY,
                reserve.reason,
            )
        if context.reservation is None:
            return self._publish_preaccept(
                context,
                StartStatus.RESIDENT_UNAVAILABLE,
                TerminalErrorCategory.RESIDENT_UNAVAILABLE,
                reserve.reason or "Resident reservation unavailable",
            )

        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.ACTIVE_COMMIT,
                CoordinatorPhase.RESERVING,
                next_phase=CoordinatorPhase.ACTIVE_COMMITTING,
            )
        if action is None:
            return self._current_start_result(context)
        try:
            active_result = self._persistence.create_active(frozen)
            active_error = None
        except Exception as exc:
            active_result = CommitResult.UNAVAILABLE
            active_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            if current and active_result is CommitResult.COMMITTED:
                context.durable_active = True
                if (
                    context.reservation is not None
                    and context.reservation.run_id == context.run_id
                    and context.reservation.hardware_id == frozen.hardware_id
                ):
                    context.capture_lease_committed = True
            contender = (
                self._pop_pending_terminal_locked(context)
                if current and context.durable_active
                else None
            )
            lifecycle_cancel = bool(
                current
                and context.durable_active
                and contender is None
                and context.cancel_requested
            )
        if not current:
            return self._current_start_result(context)
        if contender is not None:
            return self._terminalize_contender(context, contender)
        if lifecycle_cancel:
            return self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator lifecycle stop requested",
            )
        if active_result is not CommitResult.COMMITTED:
            self._cancel_reservation_once(context)
            return self._publish_preaccept(
                context,
                StartStatus.ACTIVE_COMMIT_REJECTED,
                TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
                self._exception_reason("ACTIVE commit", active_error)
                if active_error is not None
                else "ACTIVE commit rejected",
            )

        current_failure = self._prepare_current_collector(context, frozen)
        if current_failure is not None:
            return current_failure
        return self._activate_captured(context, frozen)

    def _prepare_current_collector(
        self,
        context: _RunContext,
        frozen: FrozenCapturedRun,
    ) -> StartResult | None:
        try:
            collector = self._current_collector_factory(context.run_id, frozen)
        except Exception as exc:
            return self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CURRENT_TRACE_INVALID,
                self._exception_reason("create current collector", exc),
            )
        try:
            enabled = self._current_source_enabled(frozen)
        except Exception as exc:
            return self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CURRENT_TRACE_INVALID,
                self._exception_reason("create current collector", exc),
            )
        if collector is None:
            if enabled:
                return self._terminalize(
                    context,
                    TerminalConclusion.ERROR,
                    TerminalErrorCategory.CURRENT_TRACE_INVALID,
                    "create current collector returned None",
                )
            return None
        with self._lock:
            if not self._is_current(context) or context.terminal_claimed:
                return self._current_start_result(context)
            contender = self._pop_pending_terminal_locked(context)
            if contender is not None or context.cancel_requested:
                should_start = False
            else:
                should_start = True
                context.current_collector = collector
                context.current_start_in_progress = True
        if not should_start:
            if contender is not None:
                return self._terminalize_contender(context, contender)
            return self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator lifecycle stop requested",
            )
        try:
            started = collector.start()
        except Exception as exc:
            started = False
            start_error = exc
        else:
            start_error = None
        with self._lock:
            context.current_start_in_progress = False
            if started is True:
                context.current_collector_started = True
            current = self._is_current(context)
            contender = (
                self._pop_pending_terminal_locked(context)
                if current
                else None
            )
            lifecycle_cancel = bool(
                current
                and contender is None
                and not context.terminal_claimed
                and context.cancel_requested
            )
            self._condition.notify_all()
        if contender is not None:
            return self._terminalize_contender(context, contender)
        if lifecycle_cancel:
            return self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator lifecycle stop requested",
            )
        if not current:
            if started is True:
                self._cleanup_terminal_current(context)
            return self._current_start_result(context)
        if start_error is not None:
            return self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CURRENT_TRACE_INVALID,
                self._exception_reason(
                    "start current collector",
                    start_error,
                ),
            )
        if started is not True:
            return self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CURRENT_TRACE_INVALID,
                "start current collector returned false",
            )
        return None

    def _freeze_captured(
        self, context: _RunContext
    ) -> tuple[FrozenCapturedRun | None, StartResult | None]:
        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.FREEZE,
                CoordinatorPhase.PREFLIGHT,
            )
        if action is None:
            return None, self._current_start_result(context)
        try:
            frozen = self._captured_freezer.freeze(context.request)
            error = None
        except Exception as exc:
            frozen = None
            error = exc
        frozen_error = None
        if isinstance(frozen, FrozenCapturedRun):
            try:
                self._current_source_enabled(frozen)
                self._frozen_channel_order(frozen)
            except Exception as exc:
                frozen_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            valid = (
                isinstance(frozen, FrozenCapturedRun)
                and frozen.run_id == context.request.run_id
                and frozen.source is context.request.source
                and frozen.trigger_monotonic_ns
                == context.request.trigger_monotonic_ns
                and frozen_error is None
            )
            if current and valid:
                context.frozen = frozen
            preaccept_reason = self._pop_preaccept_contender_locked(
                context, "captured freeze"
            )
        if not current:
            return None, self._current_start_result(context)
        if preaccept_reason is not None:
            return None, self._publish_preaccept(
                context,
                StartStatus.PREFLIGHT_REJECTED,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                preaccept_reason,
            )
        if error is not None:
            return None, self._publish_preaccept(
                context,
                StartStatus.PREFLIGHT_REJECTED,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                self._exception_reason("captured freeze", error),
            )
        if not valid:
            return None, self._publish_preaccept(
                context,
                StartStatus.INVALID_FROZEN_INPUT,
                TerminalErrorCategory.INVALID_FROZEN_INPUT,
                (
                    self._exception_reason(
                        "captured frozen facts",
                        frozen_error,
                    )
                    if frozen_error is not None
                    else "captured freezer returned mismatched identity"
                ),
            )
        return frozen, None

    def _activate_captured(
        self, context: _RunContext, frozen: FrozenCapturedRun
    ) -> StartResult:
        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.CAPTURE_REGISTER,
                CoordinatorPhase.ACTIVE_COMMITTING,
                next_phase=CoordinatorPhase.REGISTERING,
            )
            reservation = context.reservation
        if action is None:
            return self._current_start_result(context)
        assert reservation is not None
        try:
            self._resident.register_completion(
                reservation,
                lambda outcome: self._on_capture_outcome(context.run_id, outcome),
            )
            register_error = None
        except Exception as exc:
            register_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            contender = (
                self._pop_pending_terminal_locked(context)
                if current
                else None
            )
            canceled = context.cancel_requested if current else False
        if not current:
            return self._current_start_result(context)
        if contender is not None:
            return self._terminalize_contender(context, contender)
        if register_error is not None:
            return self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CAPTURE_REGISTRATION_FAILED,
                self._exception_reason(
                    "capture completion registration", register_error
                ),
            )
        if canceled:
            self._continue_lifecycle_after_child_cancel(context)
            return self._current_start_result(context)

        history = self._publish_active(
            context,
            HistoryFactKind.RECORDING,
            ResultProjectionPhase.RECORDING,
            ExternalActionKind.ACTIVE_HISTORY,
            CoordinatorPhase.REGISTERING,
        )
        if not history.may_continue:
            return self._current_start_result(context)

        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.CAPTURE_START,
                CoordinatorPhase.REGISTERING,
                next_phase=CoordinatorPhase.STARTING,
            )
            if action is not None:
                context.activation_ready = False
        if action is None:
            return self._current_start_result(context)
        try:
            self._resident.start(
                reservation,
                context.request.trigger_monotonic_ns,
                frozen.target_frame_count,
            )
            start_error = None
        except Exception as exc:
            start_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            contender = (
                self._pop_pending_terminal_locked(context)
                if current
                else None
            )
            canceled = context.cancel_requested if current else False
        if not current:
            return self._current_start_result(context)
        if contender is not None:
            return self._terminalize_contender(context, contender)
        if start_error is not None:
            return self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CAPTURE_START_FAILED,
                self._exception_reason("capture start", start_error),
            )
        if canceled:
            self._continue_lifecycle_after_child_cancel(context)
            return self._current_start_result(context)

        if context.request.source is TriggerSource.PLC:
            with self._lock:
                action = self._try_set_next_action_locked(
                    context,
                    ExternalActionKind.PLC_MARK,
                    CoordinatorPhase.STARTING,
                )
                ticket = context.ticket
            if action is None:
                return self._current_start_result(context)
            assert ticket is not None
            try:
                transition = self._plc.mark_accepted(ticket)
                mark_error = None
            except Exception as exc:
                transition = None
                mark_error = exc
            valid_transition = (
                type(transition) is PlcMutationDisposition
                and transition is PlcMutationDisposition.APPLIED
            )
            with self._lock:
                current = self._reconcile_action(context, action)
                if current and valid_transition:
                    context.plc_accepted = True
                    context.activation_ready = True
                    context.phase = CoordinatorPhase.RECORDING
                    if context.cancel_requested:
                        early = None
                    else:
                        early = context.early_capture_outcome
                        context.early_capture_outcome = None
                else:
                    early = None
                contender = (
                    self._pop_pending_terminal_locked(context)
                    if current
                    else None
                )
                canceled = context.cancel_requested if current else False
            if not current:
                return self._current_start_result(context)
            if contender is not None:
                return self._terminalize_contender(context, contender)
            if not valid_transition:
                failure = (
                    mark_error
                    if mark_error is not None
                    else RuntimeError(
                        "PLC mark accepted returned a non-APPLIED "
                        "or malformed result"
                    )
                )
                self._diagnose(
                    "PLC mark accepted",
                    context,
                    TerminalErrorCategory.PLC_ACTIVATION_FAILED,
                    failure,
                )
                return self._terminalize(
                    context,
                    TerminalConclusion.ERROR,
                    TerminalErrorCategory.PLC_ACTIVATION_FAILED,
                    self._exception_reason("PLC mark accepted", failure),
                )
            if canceled:
                self._continue_lifecycle_after_child_cancel(context)
                return self._current_start_result(context)
        else:
            with self._lock:
                if not self._is_current(context):
                    return self._current_start_result(context)
                context.activation_ready = True
                context.phase = CoordinatorPhase.RECORDING
                early = context.early_capture_outcome
                context.early_capture_outcome = None
        if early is not None:
            self._process_capture_outcome(context, early)
            return self._current_start_result(context)
        return self._current_start_result(context)

    def _start_imported(self, request: ImportedStartRequest) -> StartResult:
        context, rejection = self._register_context(request)
        if rejection is not None:
            return rejection
        assert context is not None
        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.FREEZE,
                CoordinatorPhase.PREFLIGHT,
            )
        if action is None:
            return self._current_start_result(context)
        try:
            frozen = self._imported_freezer.freeze(request)
            error = None
        except Exception as exc:
            frozen = None
            error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            valid = (
                isinstance(frozen, FrozenImportedRun)
                and frozen.run_id == request.run_id
                and frozen.source is request.source
                and frozen.trigger_monotonic_ns == request.trigger_monotonic_ns
            )
            if current and valid:
                context.frozen = frozen
            preaccept_reason = self._pop_preaccept_contender_locked(
                context, "imported freeze"
            )
        if not current:
            return self._current_start_result(context)
        if preaccept_reason is not None:
            return self._publish_preaccept(
                context,
                StartStatus.PREFLIGHT_REJECTED,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                preaccept_reason,
            )
        if error is not None:
            return self._publish_preaccept(
                context,
                StartStatus.PREFLIGHT_REJECTED,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                self._exception_reason("imported freeze", error),
            )
        if not valid:
            return self._publish_preaccept(
                context,
                StartStatus.INVALID_FROZEN_INPUT,
                TerminalErrorCategory.INVALID_FROZEN_INPUT,
                "imported freezer returned mismatched identity",
            )
        assert frozen is not None
        with self._lock:
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.ACTIVE_COMMIT,
                CoordinatorPhase.PREFLIGHT,
                next_phase=CoordinatorPhase.ACTIVE_COMMITTING,
            )
        if action is None:
            return self._current_start_result(context)
        try:
            result = self._persistence.create_active(frozen)
            active_error = None
        except Exception as exc:
            result = CommitResult.UNAVAILABLE
            active_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            if current and result is CommitResult.COMMITTED:
                context.durable_active = True
            contender = (
                self._pop_pending_terminal_locked(context)
                if current and context.durable_active
                else None
            )
            lifecycle_cancel = bool(
                current
                and context.durable_active
                and contender is None
                and context.cancel_requested
            )
        if not current:
            return self._current_start_result(context)
        if contender is not None:
            return self._terminalize_contender(context, contender)
        if lifecycle_cancel:
            return self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator lifecycle stop requested",
            )
        if result is not CommitResult.COMMITTED:
            return self._publish_preaccept(
                context,
                StartStatus.ACTIVE_COMMIT_REJECTED,
                TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
                self._exception_reason("ACTIVE commit", active_error)
                if active_error is not None
                else "ACTIVE commit rejected",
            )
        with self._lock:
            may_begin_save = self._next_action_allowed_locked(
                context, CoordinatorPhase.ACTIVE_COMMITTING
            )
            if may_begin_save:
                context.phase = CoordinatorPhase.SAVE_SUBMITTING
        if not may_begin_save:
            return self._current_start_result(context)
        self._begin_save(context, frozen.source_reference)
        return self._current_start_result(context)

    def _publish_active(
        self,
        context: _RunContext,
        kind: HistoryFactKind,
        projection_phase: ResultProjectionPhase,
        action_kind: ExternalActionKind,
        expected_phase: CoordinatorPhase,
    ) -> _HistoryDispatchResult:
        with self._lock:
            contender = self._pop_pending_terminal_locked(context)
            if contender is not None:
                may_continue = False
                action = None
            elif not context.history_allowed:
                may_continue = (
                    self._next_action_allowed_locked(
                        context, expected_phase
                    )
                )
                action = None
            else:
                may_continue = False
                action = self._try_set_next_action_locked(
                    context,
                    action_kind,
                    expected_phase,
                )
                expected_revision = context.last_published_revision
                frozen = context.frozen
        if contender is not None:
            self._terminalize_contender(context, contender)
            return _HistoryDispatchResult(False)
        if action is None:
            if not may_continue:
                self._continue_lifecycle_after_child_cancel(context)
            return _HistoryDispatchResult(may_continue)
        assert frozen is not None
        fact = HistoryProjectionFact(
            kind=kind,
            run_id=context.run_id,
            expected_revision=expected_revision,
            operation_id=context.attempt_id,
            projection=ResultProjection(
                phase=projection_phase,
                conclusion=None,
                error_category=None,
                reason="",
                history_labels=frozen.history_labels,
                result_payload=None,
            ),
        )
        event = None
        projector_error = None
        try:
            event = self._history_projector.project(fact)
            event = self._validate_projected_history_event(fact, event)
        except Exception as exc:
            projector_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            if not current:
                return _HistoryDispatchResult(False)
            contender = self._pop_pending_terminal_locked(context)
            canceled = context.cancel_requested
            if (
                contender is None
                and projector_error is None
                and not canceled
            ):
                sink_action = self._try_set_next_action_locked(
                    context,
                    action_kind,
                    expected_phase,
                )
            else:
                sink_action = None
        if projector_error is not None:
            self._diagnose(
                "active history projector",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                projector_error,
            )
        if contender is not None:
            self._terminalize_contender(context, contender)
            return _HistoryDispatchResult(False)
        if canceled:
            self._continue_lifecycle_after_child_cancel(context)
            return _HistoryDispatchResult(False)
        if projector_error is not None:
            return _HistoryDispatchResult(True)
        assert event is not None and sink_action is not None
        sink_error = None
        try:
            self._history_sink.publish(event)
        except Exception as exc:
            sink_error = exc
        with self._lock:
            current = self._reconcile_action(context, sink_action)
            if not current:
                return _HistoryDispatchResult(False)
            contender = self._pop_pending_terminal_locked(context)
            context.last_published_revision = event.proposed_revision
            canceled = context.cancel_requested
        if sink_error is not None:
            self._diagnose(
                "active history sink",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                sink_error,
            )
        if contender is not None:
            self._terminalize_contender(context, contender)
            return _HistoryDispatchResult(False)
        if canceled:
            self._continue_lifecycle_after_child_cancel(context)
            return _HistoryDispatchResult(False)
        return _HistoryDispatchResult(True)

    def _publish_preaccept(
        self,
        context: _RunContext,
        status: StartStatus,
        category: TerminalErrorCategory,
        reason: str,
    ) -> StartResult:
        reason = self._bounded(reason)
        conclusion = (
            TerminalConclusion.CAPACITY_REJECTED
            if category is TerminalErrorCategory.CAPTURE_CAPACITY
            else TerminalConclusion.ERROR
        )
        with self._lock:
            if not self._is_current(context):
                return StartResult(
                    run_id=context.run_id,
                    status=status,
                    accepted=False,
                    phase=None,
                    terminal=None,
                    reason=reason,
                )
            frozen = context.frozen
            source = context.request.source
            fallback_history_labels = (
                context.request.fallback_history_labels
            )
            clock_action = self._set_action(
                context,
                ExternalActionKind.TERMINAL_COMMIT,
                context.phase,
            )
        try:
            terminal_at = self._wall_clock()
            clock_error = None
        except Exception as exc:
            terminal_at = _WALL_CLOCK_FAILURE_AT
            clock_error = exc
        if clock_error is None and not self._is_aware_datetime(terminal_at):
            terminal_at = _WALL_CLOCK_FAILURE_AT
            clock_error = ValueError(
                "wall clock returned a non-aware datetime"
            )
        if clock_error is not None:
            reason = self._exception_reason(
                "preaccept wall clock", clock_error
            )
        assert self._is_aware_datetime(terminal_at)
        try:
            fact = PreacceptTerminalFact(
                run_id=context.run_id,
                source=source,
                config_id=frozen.config_id if frozen is not None else None,
                conclusion=conclusion,
                error_category=category,
                reason=reason,
                history_labels=(
                    frozen.history_labels
                    if frozen is not None
                    else fallback_history_labels
                ),
                terminal_at=terminal_at,
            )
            fact_error = None
        except Exception as exc:
            fact_error = exc
            reason = self._exception_reason(
                "preaccept fact validation", exc
            )
            fact = PreacceptTerminalFact(
                run_id=context.run_id,
                source=source,
                config_id=None,
                conclusion=conclusion,
                error_category=category,
                reason=reason,
                history_labels=fallback_history_labels,
                terminal_at=terminal_at,
            )
        with self._lock:
            current = self._reconcile_action(context, clock_action)
        if not current:
            return StartResult(
                run_id=context.run_id,
                status=status,
                accepted=False,
                phase=None,
                terminal=fact,
                reason=reason,
            )
        if clock_error is not None:
            self._diagnose(
                "preaccept wall clock",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                clock_error,
            )
        if fact_error is not None:
            self._diagnose(
                "preaccept fact validation",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                fact_error,
            )
        with self._lock:
            if not self._is_current(context):
                return StartResult(
                    run_id=context.run_id,
                    status=status,
                    accepted=False,
                    phase=None,
                    terminal=fact,
                    reason=reason,
                )
            publication = self._replace_with_publication(
                context,
                fact,
                _PlcSettlementKind.REJECT
                if context.ticket is not None
                else _PlcSettlementKind.NONE,
            )

        if publication.history_in_flight:
            self._publish_terminal_history(publication)
        if publication.plc_in_flight:
            self._publish_plc_settlement(publication)
        self._retire_if_publication_complete(publication)
        return StartResult(
            run_id=context.run_id,
            status=status,
            accepted=False,
            phase=None,
            terminal=fact,
            reason=reason,
        )

    def _replace_with_publication(
        self,
        context: _RunContext,
        terminal: PreacceptTerminalFact | TerminalFact,
        plc_settlement: _PlcSettlementKind,
    ) -> _PublishingContext:
        history_allowed = context.history_allowed
        history_fact = (
            self._terminal_history_fact(context, terminal)
            if history_allowed
            else None
        )
        plc_in_flight = plc_settlement is not _PlcSettlementKind.NONE
        publication = _PublishingContext(
            run_id=context.run_id,
            ticket=context.ticket,
            plc_accepted=context.plc_accepted,
            reservation=context.reservation,
            durable_active=context.durable_active,
            fact=_PublicationFact(
                terminal=terminal,
                history=history_fact,
            ),
            last_published_revision=context.last_published_revision,
            history_in_flight=history_allowed,
            plc_settlement=plc_settlement,
            plc_in_flight=plc_in_flight,
        )
        self._runs[context.run_id] = publication
        self._condition.notify_all()
        return publication

    @staticmethod
    def _terminal_history_fact(
        context: _RunContext,
        fact: PreacceptTerminalFact | TerminalFact,
    ) -> HistoryProjectionFact:
        return HistoryProjectionFact(
            kind=HistoryFactKind.RUN_TERMINATED,
            run_id=context.run_id,
            expected_revision=context.last_published_revision,
            operation_id=getattr(fact, "attempt_id", None),
            projection=ResultProjection(
                phase=ResultProjectionPhase.TERMINAL,
                conclusion=fact.conclusion,
                error_category=fact.error_category,
                reason=fact.reason,
                history_labels=fact.history_labels,
                result_payload=getattr(fact, "result_payload", None),
            ),
        )

    def _publish_terminal_history(
        self,
        context: _PublishingContext,
    ) -> None:
        history_fact = context.fact.history
        assert history_fact is not None
        try:
            event = self._history_projector.project(history_fact)
            event = self._validate_projected_history_event(
                history_fact,
                event,
            )
        except Exception as exc:
            self._finish_publication_part(
                context,
                "history",
                exc,
                diagnostic_stage="history projector",
            )
            return
        try:
            self._history_sink.publish(event)
            error = None
        except Exception as exc:
            error = exc
        self._finish_publication_part(
            context,
            "history",
            error,
            proposed_revision=event.proposed_revision,
        )

    @staticmethod
    def _validate_projected_history_event(
        fact: HistoryProjectionFact,
        event: object,
    ) -> ProjectedHistoryEvent:
        if type(event) is not ProjectedHistoryEvent:
            raise ValueError(
                "history projector returned a non-ProjectedHistoryEvent"
            )
        assert isinstance(event, ProjectedHistoryEvent)
        expected_revision_matches = (
            event.expected_revision is ABSENT
            if fact.expected_revision is ABSENT
            else (
                type(event.expected_revision) is int
                and event.expected_revision == fact.expected_revision
            )
        )
        expected_proposed_revision = (
            1
            if fact.expected_revision is ABSENT
            else fact.expected_revision + 1
        )
        if (
            event.kind is not fact.kind
            or type(event.run_id) is not str
            or event.run_id != fact.run_id
            or not expected_revision_matches
            or (
                event.operation_id is not None
                and type(event.operation_id) is not str
            )
            or event.operation_id != fact.operation_id
            or type(event.proposed_revision) is not int
            or event.proposed_revision != expected_proposed_revision
            or type(event.projection) is not ResultProjection
            or event.projection != fact.projection
        ):
            raise ValueError(
                "history projector returned an incoherent projected event"
            )
        return event

    def _publish_plc_settlement(
        self, context: _PublishingContext
    ) -> None:
        ticket = context.ticket
        assert ticket is not None
        error = None
        try:
            if context.plc_settlement is _PlcSettlementKind.REJECT:
                terminal = context.fact.terminal
                assert isinstance(terminal, PreacceptTerminalFact)
                disposition = self._plc.publish_rejection(
                    ticket,
                    self._plc_payload(terminal),
                )
            else:
                terminal = context.fact.terminal
                assert isinstance(terminal, TerminalFact)
                disposition = self._plc.publish_terminal(
                    ticket,
                    self._plc_payload(terminal),
                )
            if (
                type(disposition) is not PlcMutationDisposition
                or disposition is not PlcMutationDisposition.APPLIED
            ):
                raise RuntimeError(
                    "PLC settlement returned a non-APPLIED or malformed result"
                )
        except Exception as exc:
            error = exc
        self._finish_publication_part(context, "plc", error)

    @staticmethod
    def _plc_payload(
        terminal: PreacceptTerminalFact | TerminalFact,
    ) -> dict[str, bool | int | float | str]:
        successful_analysis = terminal.conclusion in {
            TerminalConclusion.OK,
            TerminalConclusion.NG,
        }
        metrics = (
            MultiRunCoordinator._plc_metrics(terminal)
            if successful_analysis
            else {
                "score": 0.0,
                "spl": 0.0,
                "loudness": 0.0,
                "vibrate": 0.0,
                "ai_score": 0.0,
            }
        )
        category = terminal.error_category
        return {
            "finished": True,
            "error_code": (
                0
                if successful_analysis
                else category.value if category is not None else 1
            ),
            "sn_code": MultiRunCoordinator._plc_sn_code(terminal),
            "test_result": terminal.conclusion is TerminalConclusion.OK,
            **metrics,
        }

    @staticmethod
    def _plc_sn_code(
        terminal: PreacceptTerminalFact | TerminalFact,
    ) -> str:
        try:
            labels = decode_history_labels_payload(
                value_id=terminal.history_labels.value_id,
                canonical_bytes=terminal.history_labels.canonical_bytes,
            )
        except (TypeError, ValueError, UnicodeError):
            return ""
        sn_code = labels.get("sn")
        return sn_code if type(sn_code) is str else ""

    @staticmethod
    def _plc_metrics(
        terminal: PreacceptTerminalFact | TerminalFact,
    ) -> dict[str, float]:
        payload = getattr(terminal, "result_payload", None)
        try:
            decoded = json.loads(payload.canonical_bytes.decode("utf-8"))
            result = decoded["result"]
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
        ):
            result = {}
        if type(result) is not dict:
            result = {}
        judgment = result.get("judgment_payload")
        summary = result.get("summary_payload")
        chart = result.get("chart_payload")

        score = MultiRunCoordinator._plc_summary_actual(
            summary,
            "stft_score",
        )
        if score is None:
            score = MultiRunCoordinator._plc_chart_value(
                chart,
                "stft_score",
                "summary_score",
                first=True,
            )
        ai_score = MultiRunCoordinator._plc_mapping_float(
            judgment,
            "ai_score",
        )
        if ai_score is None:
            ai_score = MultiRunCoordinator._plc_summary_actual(summary, "ai")
        return {
            "score": score if score is not None else 0.0,
            "spl": MultiRunCoordinator._plc_max_channel_value(
                judgment,
                "spl_max_db",
            ),
            "loudness": MultiRunCoordinator._plc_max_channel_value(
                judgment,
                "loudness_max_sone",
            ),
            "vibrate": (
                MultiRunCoordinator._plc_chart_value(
                    chart,
                    "vibration",
                    "overall_rms",
                    first=False,
                )
                or 0.0
            ),
            "ai_score": ai_score if ai_score is not None else 0.0,
        }

    @staticmethod
    def _plc_summary_actual(
        summary: object,
        item_key: str,
    ) -> float | None:
        if type(summary) is not dict or type(summary.get("rows")) is not list:
            return None
        for row in summary["rows"]:
            if type(row) is dict and row.get("item_key") == item_key:
                return MultiRunCoordinator._plc_mapping_float(
                    row,
                    "actual_value",
                )
        return None

    @staticmethod
    def _plc_max_channel_value(
        judgment: object,
        field_name: str,
    ) -> float:
        if (
            type(judgment) is not dict
            or type(judgment.get("channel_results")) is not list
        ):
            return 0.0
        values = tuple(
            value
            for channel in judgment["channel_results"]
            if (
                value := MultiRunCoordinator._plc_mapping_float(
                    channel,
                    field_name,
                )
            )
            is not None
        )
        return max(values) if values else 0.0

    @staticmethod
    def _plc_chart_value(
        chart: object,
        section_name: str,
        field_name: str,
        *,
        first: bool,
    ) -> float | None:
        if type(chart) is not dict:
            return None
        values = []
        for channel in chart.values():
            if type(channel) is not dict:
                continue
            value = MultiRunCoordinator._plc_mapping_float(
                channel.get(section_name),
                field_name,
            )
            if value is not None:
                if first:
                    return value
                values.append(value)
        return max(values) if values else None

    @staticmethod
    def _plc_mapping_float(
        mapping: object,
        field_name: str,
    ) -> float | None:
        if type(mapping) is not dict:
            return None
        value = mapping.get(field_name)
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return numeric if math.isfinite(numeric) else None

    def _finish_publication_part(
        self,
        context: _PublishingContext,
        part: str,
        error: Exception | None,
        *,
        diagnostic_stage: str | None = None,
        proposed_revision: int | None = None,
    ) -> None:
        with self._lock:
            if not self._is_current(context):
                return
            if part == "history":
                context.history_in_flight = False
                if proposed_revision is not None:
                    context.last_published_revision = proposed_revision
            else:
                context.plc_in_flight = False
            if error is not None:
                context.errors.append(self._bounded(str(error)))
            self._condition.notify_all()
        if error is not None:
            self._diagnose(
                diagnostic_stage or f"{part} publication",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                error,
            )
        self._retire_if_publication_complete(context)

    def _retire_if_publication_complete(
        self, context: _PublishingContext
    ) -> None:
        with self._lock:
            if (
                self._is_current(context)
                and not context.history_in_flight
                and not context.plc_in_flight
                and not context.plc_unsettled
                and context.reservation is None
            ):
                self._runs.pop(context.run_id, None)
                self._condition.notify_all()

    def _cancel_reservation_once(self, context: _RunContext) -> None:
        with self._lock:
            if (
                not self._is_current(context)
                or context.reservation is None
                or context.reservation_cancel_attempted
                or context.action is not None
            ):
                return
            context.reservation_cancel_attempted = True
            reservation = context.reservation
        self._cancel_frozen_reservation(context, reservation)

    def _cancel_frozen_reservation(
        self, context: _RunContext, reservation: object
    ) -> None:
        with self._lock:
            if not self._is_current(context) or context.action is not None:
                return
            action = self._set_child_cancel_action_locked(
                context,
                reservation_id=reservation.reservation_id,
            )
            self._track_child_cancel_action_locked(
                action,
                durable_run=context.durable_active,
            )
            lifecycle_cancel = context.cancel_requested
            context.reservation_cancel_in_flight = True
        settled = False
        try:
            result = self._resident.cancel(reservation)
        except Exception as exc:
            dispatch_error = exc
            validation_error = None
        else:
            dispatch_error = None
            try:
                self._validate_resident_cancel_result(
                    result,
                    context.run_id,
                    reservation,
                )
                settled = True
            except Exception as exc:
                validation_error = exc
            else:
                validation_error = None
        error = dispatch_error or validation_error
        if error is not None and lifecycle_cancel:
            if dispatch_error is not None:
                with self._lock:
                    self._record_lifecycle_error_locked(
                        self._exception_reason(
                            "Resident cancel",
                            dispatch_error,
                        )
                    )
                    if self._shutdown_begun:
                        self._restart_required = True
                        self._lifecycle = CoordinatorLifecycle.CLOSED
                        self._promote_stop_errors_locked()
            self._diagnose(
                "Resident cancel",
                context,
                TerminalErrorCategory.RESIDENT_UNAVAILABLE,
                error,
            )
        with self._lock:
            current = self._reconcile_action(context, action)
            if current:
                context.reservation_cancel_in_flight = False
                completed_during_cancel = (
                    context.early_capture_outcome is not None
                    and context.early_capture_outcome.run_id == context.run_id
                    and context.early_capture_outcome.reservation == reservation
                )
                if completed_during_cancel:
                    context.early_capture_outcome = None
                if (
                    completed_during_cancel
                    or (
                        settled
                        and context.reservation == reservation
                    )
                ):
                    context.reservation = None
                if error is not None and not lifecycle_cancel:
                    self._restart_required = True
                    self._lifecycle = CoordinatorLifecycle.CLOSED
            self._retire_child_cancel_action_locked(action)
            if not current:
                self._condition.notify_all()
        if error is not None and current and not lifecycle_cancel:
            self._diagnose(
                "Resident cancel",
                context,
                TerminalErrorCategory.RESIDENT_UNAVAILABLE,
                error,
            )

    def _cancel_frozen_save_job(
        self, context: _RunContext, save_job_id: str
    ) -> None:
        with self._lock:
            if not self._is_current(context) or context.action is not None:
                return
            action = self._set_child_cancel_action_locked(
                context,
                save_job_id=save_job_id,
            )
            lifecycle_cancel = context.cancel_requested
            context.save_cancel_in_flight = True
        try:
            result = self._save_dispatcher.cancel(
                context.run_id,
                save_job_id,
            )
        except Exception as exc:
            dispatch_error = exc
            validation_error = None
        else:
            dispatch_error = None
            if lifecycle_cancel:
                try:
                    self._validate_save_cancel_result(
                        result,
                        context.run_id,
                        save_job_id,
                    )
                except Exception as exc:
                    validation_error = exc
                else:
                    validation_error = None
            else:
                validation_error = None
        error = dispatch_error or validation_error
        if error is not None and lifecycle_cancel:
            if dispatch_error is not None:
                with self._lock:
                    self._record_lifecycle_error_locked(
                        self._exception_reason(
                            "save dispatcher cancel",
                            dispatch_error,
                        )
                    )
                    if self._shutdown_begun:
                        self._restart_required = True
                        self._lifecycle = CoordinatorLifecycle.CLOSED
                        self._promote_stop_errors_locked()
            self._diagnose(
                "save dispatcher cancel",
                context,
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                error,
            )
        with self._lock:
            current = self._reconcile_action(context, action)
            if current:
                context.save_cancel_in_flight = False
                completed_during_cancel = (
                    context.early_save_outcome is not None
                    and self._valid_save_outcome(
                        context,
                        context.early_save_outcome,
                    )
                )
                if completed_during_cancel:
                    context.early_save_outcome = None
                if completed_during_cancel or not lifecycle_cancel:
                    self._save_jobs.pop(save_job_id, None)
                    if context.save_job_id == save_job_id:
                        context.save_job_id = None
                    context.save_child_accepted = False
                    context.save_source_kind = None
                    context.save_source_identity = None
                    context.save_metadata = None
            if not current:
                self._condition.notify_all()
        if error is not None and current and not lifecycle_cancel:
            self._diagnose(
                "save dispatcher cancel",
                context,
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                error,
            )

    def _cancel_frozen_scheduler_attempt(
        self,
        context: _RunContext,
        attempt_id: str,
        reason: str,
        *,
        preserve_report_blocker_on_failure: bool = False,
    ) -> None:
        with self._lock:
            if not self._is_current(context) or context.action is not None:
                return
            action = self._set_child_cancel_action_locked(
                context,
                attempt_id=attempt_id,
            )
            self._track_child_cancel_action_locked(
                action,
                durable_run=context.durable_active,
            )
        try:
            result = self._scheduler.cancel(
                BridgeCancelRequest(
                    run_id=context.run_id,
                    analysis_attempt_id=attempt_id,
                    origin=AnalysisCancelOrigin.USER_STOP,
                    reason=reason or "Coordinator requested user stop",
                )
            )
        except Exception as exc:
            dispatch_error = exc
            validation_error = None
        else:
            dispatch_error = None
            try:
                if type(result) is not BridgeCancelResult:
                    raise TypeError(
                        "bridge cancel returned an invalid result"
                    )
                result.__post_init__()
                if (
                    result.run_id != context.run_id
                    or result.analysis_attempt_id != attempt_id
                    or result.origin is not AnalysisCancelOrigin.USER_STOP
                ):
                    raise ValueError(
                        "bridge cancel result identity does not match request"
                    )
                if result.status not in {
                    BridgeCancelStatus.CANCEL_REQUESTED,
                    BridgeCancelStatus.ALREADY_TERMINAL,
                }:
                    raise RuntimeError(
                        result.reason
                        or result.code
                        or f"bridge cancel returned {result.status.value}"
                    )
            except Exception as exc:
                validation_error = exc
            else:
                validation_error = None
        error = dispatch_error or validation_error
        if error is not None and preserve_report_blocker_on_failure:
            if dispatch_error is not None:
                with self._lock:
                    self._record_lifecycle_error_locked(
                        self._exception_reason(
                            "Scheduler cancel",
                            dispatch_error,
                        )
                    )
                    if self._shutdown_begun:
                        self._restart_required = True
                        self._lifecycle = CoordinatorLifecycle.CLOSED
                        self._promote_stop_errors_locked()
            self._diagnose(
                "Scheduler cancel",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                error,
            )
        with self._lock:
            current = self._reconcile_action(context, action)
            self._retire_child_cancel_action_locked(action)
            if not current:
                self._condition.notify_all()
        if error is not None:
            if preserve_report_blocker_on_failure:
                return
            if current:
                self._fail_closed_bridge_context(
                    context,
                    "Scheduler cancel",
                    error,
                )

    def _on_capture_outcome(
        self, run_id: str, outcome: CaptureOutcome
    ) -> None:
        try:
            ignored_reason = None
            process_context = None
            settled_publication = None
            handled = False
            with self._lock:
                candidate = self._runs.get(run_id)
                context = (
                    candidate
                    if isinstance(candidate, _RunContext)
                    else None
                )
                publication = (
                    candidate
                    if isinstance(candidate, _PublishingContext)
                    else None
                )
                diagnostic_ids = self._diagnostic_ids(
                    context or publication,
                    run_id=run_id,
                )
                if not isinstance(outcome, CaptureOutcome):
                    ignored_reason = "capture outcome type is invalid"
                elif publication is not None:
                    if publication.reservation is None:
                        ignored_reason = (
                            "capture reservation is no longer active"
                        )
                    elif (
                        outcome.run_id != run_id
                        or outcome.reservation != publication.reservation
                    ):
                        ignored_reason = (
                            "capture outcome identity does not match"
                        )
                    else:
                        publication.reservation = None
                        settled_publication = publication
                        self._condition.notify_all()
                        handled = True
                elif context is None:
                    ignored_reason = "capture run context is missing"
                elif context.reservation is None:
                    ignored_reason = "capture reservation is no longer active"
                elif (
                    outcome.run_id != run_id
                    or outcome.reservation != context.reservation
                ):
                    ignored_reason = "capture outcome identity does not match"
                elif context.reservation_cancel_in_flight:
                    if context.early_capture_outcome is None:
                        context.early_capture_outcome = outcome
                        handled = True
                    else:
                        ignored_reason = (
                            "duplicate capture outcome during cancel"
                        )
                elif context.terminal_claimed:
                    ignored_reason = "capture outcome arrived after terminal claim"
                elif (
                    context.phase is CoordinatorPhase.STARTING
                    and not context.activation_ready
                ):
                    if context.early_capture_outcome is None:
                        context.early_capture_outcome = outcome
                        context.reservation = None
                        handled = True
                    else:
                        ignored_reason = "duplicate early capture outcome"
                elif context.action is not None:
                    if (
                        outcome.status is not CaptureOutcomeStatus.COMPLETED
                        and context.pending_terminal is None
                    ):
                        context.reservation = None
                        context.pending_terminal = PendingTerminalContender(
                            kind=TerminalContenderKind.CAPTURE_OUTCOME,
                            run_id=run_id,
                            attempt_id=None,
                            save_job_id=None,
                            conclusion=TerminalConclusion.ERROR,
                            error_category=outcome.error_category,
                            reason=outcome.reason,
                            result_payload=None,
                        )
                        handled = True
                    else:
                        ignored_reason = (
                            "capture outcome is stale during external action"
                        )
                elif context.phase not in {
                    CoordinatorPhase.RECORDING,
                    CoordinatorPhase.STARTING,
                }:
                    ignored_reason = "capture outcome is stale for current phase"
                else:
                    context.reservation = None
                    process_context = context
            if ignored_reason is not None:
                self._diagnose_ids(
                    "capture callback ignored",
                    TerminalErrorCategory.CAPTURE_FAILED,
                    ignored_reason,
                    **diagnostic_ids,
                )
                return
            if settled_publication is not None:
                self._retire_if_publication_complete(settled_publication)
                return
            if handled:
                return
            assert process_context is not None
            self._process_capture_outcome(process_context, outcome)
        except Exception as exc:
            with self._lock:
                candidate = self._runs.get(run_id)
                context = (
                    candidate
                    if isinstance(candidate, _RunContext)
                    else None
                )
                diagnostic_ids = self._diagnostic_ids(
                    context,
                    run_id=run_id,
                )
            self._diagnose_ids(
                "capture callback",
                TerminalErrorCategory.CAPTURE_FAILED,
                exc,
                **diagnostic_ids,
            )

    def _process_capture_outcome(
        self, context: _RunContext, outcome: CaptureOutcome
    ) -> None:
        with self._lock:
            if (
                not self._is_current(context)
                or context.terminal_claimed
                or context.phase
                not in {CoordinatorPhase.RECORDING, CoordinatorPhase.STARTING}
            ):
                return
            if outcome.status is CaptureOutcomeStatus.COMPLETED:
                context.phase = CoordinatorPhase.SAVE_SUBMITTING
            else:
                context.phase = CoordinatorPhase.RECORDING
        if outcome.status is CaptureOutcomeStatus.COMPLETED:
            assert outcome.audio is not None
            self._settle_completed_current(context, outcome.audio)
        else:
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                outcome.error_category,
                outcome.reason,
            )

    def _read_settled_current(
        self,
        context: _RunContext,
    ) -> tuple[list[object], tuple[tuple[str, Exception], ...]]:
        with self._lock:
            collector = context.current_collector
            if (
                collector is None
                or not context.current_collector_started
                or context.current_settlement_claimed
            ):
                return [], ()
            context.current_settlement_claimed = True
            context.current_settlement_in_progress = True
        errors: list[tuple[str, Exception]] = []
        try:
            timeout = self._current_stop_timeout(context)
            collector.stop(timeout)
        except Exception as exc:
            errors.append(("stop", exc))
        try:
            samples = collector.samples()
        except Exception as exc:
            errors.append(("samples", exc))
            samples = []
        finally:
            with self._lock:
                context.current_settlement_in_progress = False
                self._condition.notify_all()
        return samples, tuple(errors)

    def _cleanup_terminal_current(self, context: _RunContext) -> None:
        _, errors = self._read_settled_current(context)
        if not errors:
            return
        stage, error = errors[0]
        detail = self._bounded(
            f"{stage}: {self._exception_reason('current cleanup', error)}"
        )
        with self._lock:
            self._restart_required = True
            self._lifecycle = CoordinatorLifecycle.CLOSED
            self._record_lifecycle_error_locked(detail)
            self._condition.notify_all()
        self._diagnose(
            "current cleanup",
            context,
            TerminalErrorCategory.CAPTURE_FAILED,
            RuntimeError(detail),
        )

    def _settle_completed_current(
        self,
        context: _RunContext,
        audio: object,
    ) -> None:
        samples, errors = self._read_settled_current(context)
        with self._lock:
            contender = self._pop_pending_terminal_locked(context)
            lifecycle_cancel = bool(
                self._is_current(context)
                and contender is None
                and not context.terminal_claimed
                and context.cancel_requested
            )
        if contender is not None:
            self._terminalize_contender(context, contender)
            return
        if lifecycle_cancel:
            self._terminalize(
                context,
                TerminalConclusion.MANUAL_STOPPED,
                TerminalErrorCategory.MANUAL_STOPPED,
                "Coordinator lifecycle stop requested",
            )
            return
        if errors:
            stage, error = errors[0]
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CURRENT_TRACE_INVALID,
                self._exception_reason(f"current {stage}", error),
            )
            return
        frozen = context.frozen
        assert isinstance(frozen, FrozenCapturedRun)
        channel_order = self._frozen_channel_order(frozen)
        try:
            if not isinstance(audio, CapturedAudioValue):
                raise ValueError("capture result is not audio")
            selected_audio = self._select_captured_channels(
                audio,
                channel_order,
            )
        except (TypeError, ValueError):
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CAPTURE_FAILED,
                "channel-order mismatch",
            )
            return
        try:
            trace = current_samples_to_trace(samples)
        except Exception as exc:
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.CURRENT_TRACE_INVALID,
                self._exception_reason("current validate", exc),
            )
            return
        if trace is not None:
            try:
                result = self._persistence.attach_current_trace(
                    context.run_id,
                    trace,
                )
                attach_error = None
            except Exception as exc:
                result = None
                attach_error = exc
            if result is not CommitResult.COMMITTED:
                self._terminalize(
                    context,
                    TerminalConclusion.ERROR,
                    TerminalErrorCategory.CURRENT_TRACE_PERSIST_FAILED,
                    (
                        self._exception_reason(
                            "current attach",
                            attach_error,
                        )
                        if attach_error is not None
                        else "current attach was not committed"
                    ),
                )
                return
        self._begin_save(context, selected_audio)

    def _begin_save(self, context: _RunContext, source: object) -> None:
        history = self._publish_active(
            context,
            HistoryFactKind.WAITING_SAVE,
            ResultProjectionPhase.WAITING_SAVE,
            ExternalActionKind.WAITING_HISTORY,
            CoordinatorPhase.SAVE_SUBMITTING,
        )
        if not history.may_continue:
            return
        with self._lock:
            id_action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.SAVE_SUBMIT,
                CoordinatorPhase.SAVE_SUBMITTING,
            )
        if id_action is None:
            return
        try:
            save_job_id = require_uuid4(
                self._save_job_id_factory(), "save_job_id"
            )
            id_error = None
        except Exception as exc:
            save_job_id = None
            id_error = exc
        with self._lock:
            current = self._reconcile_action(context, id_action)
            contender = (
                self._pop_pending_terminal_locked(context)
                if current
                else None
            )
            lifecycle_cancel = bool(
                current
                and contender is None
                and context.cancel_requested
            )
        if not current:
            return
        if contender is not None:
            self._terminalize_contender(context, contender)
            return
        if lifecycle_cancel:
            self._continue_lifecycle_after_child_cancel(context)
            return
        if id_error is not None:
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                self._exception_reason("save job ID", id_error),
            )
            return
        assert save_job_id is not None
        frozen = context.frozen
        assert frozen is not None
        if isinstance(frozen, FrozenCapturedRun):
            request = CapturedAudioSaveRequest(
                save_job_id=save_job_id,
                run_id=context.run_id,
                source_kind=AudioSaveSourceKind.CAPTURED_AUDIO,
                source_identity=source.source_identity,
                audio=source,
                final_path_request=frozen.final_path_request,
                metadata=frozen.metadata,
            )
        else:
            request = ImportedAudioSaveRequest(
                save_job_id=save_job_id,
                run_id=context.run_id,
                source_kind=AudioSaveSourceKind.IMPORTED_SOURCE,
                source_identity=source.source_identity,
                source=source,
                final_path_request=frozen.final_path_request,
                metadata=frozen.metadata,
            )
        with self._lock:
            may_submit = self._next_action_allowed_locked(
                context, CoordinatorPhase.SAVE_SUBMITTING
            )
            if not may_submit:
                return
            collision = self._save_job_id_is_live_locked(
                save_job_id, context
            )
            if not collision:
                context.save_job_id = save_job_id
                context.save_request = request
                context.save_source_kind = request.source_kind
                context.save_source_identity = request.source_identity
                context.save_metadata = request.metadata
                self._save_jobs[save_job_id] = context
                action = self._try_set_next_action_locked(
                    context,
                    ExternalActionKind.SAVE_SUBMIT,
                    CoordinatorPhase.SAVE_SUBMITTING,
                )
            else:
                action = None
        if collision:
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                "save job ID collision",
            )
            return
        assert action is not None
        try:
            submission = self._save_dispatcher.try_submit(
                request, self._on_save_outcome
            )
            submit_error = None
        except Exception as exc:
            submission = None
            submit_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
            early = context.early_save_outcome
            context.early_save_outcome = None
            authority_transferred = (
                current
                and submit_error is None
                and submission is not None
                and submission.status is SaveSubmitStatus.ACCEPTED
            )
            accepted = (
                authority_transferred
                and submission.run_id == context.run_id
                and submission.save_job_id == save_job_id
                and submission.source_kind is request.source_kind
                and submission.source_identity == request.source_identity
            )
            # The dispatcher either owns the immutable request after ACCEPTED,
            # or no saver owns it after rejection. In neither case retain audio.
            context.save_request = None
            if authority_transferred:
                context.save_child_accepted = True
                if accepted:
                    context.phase = CoordinatorPhase.SAVING
            else:
                context.save_child_accepted = False
                self._save_jobs.pop(save_job_id, None)
                context.save_job_id = None
            contender = (
                self._pop_pending_terminal_locked(context)
                if current
                else None
            )
            request_lifecycle_cancel = bool(
                current
                and accepted
                and early is None
                and contender is None
                and context.cancel_requested
                and not context.save_cancel_attempted
            )
            if request_lifecycle_cancel:
                context.save_cancel_attempted = True
                cancel_action = self._set_child_cancel_action_locked(
                    context,
                    save_job_id=save_job_id,
                )
                self._track_child_cancel_action_locked(
                    cancel_action,
                    durable_run=context.durable_active,
                )
            else:
                cancel_action = None
        if not current:
            return
        if contender is not None:
            self._terminalize_contender(context, contender)
            return
        if not accepted:
            category = (
                TerminalErrorCategory.AUDIO_SAVE_SATURATED
                if submission is not None
                and submission.status is SaveSubmitStatus.SATURATED
                else TerminalErrorCategory.AUDIO_SAVE_FAILED
            )
            reason = (
                self._exception_reason("save submit", submit_error)
                if submit_error is not None
                else submission.reason or "save submission rejected"
            )
            self._terminalize(
                context, TerminalConclusion.ERROR, category, reason
            )
            return
        if request_lifecycle_cancel:
            assert cancel_action is not None
            self._dispatch_shutdown_cancel(
                "save",
                context,
                save_job_id,
                cancel_action,
                durable_run=context.durable_active,
            )
            return
        if early is not None:
            self._process_save_outcome(context, early)

    def _on_save_outcome(self, outcome: AudioSaveOutcome) -> None:
        try:
            ignored_reason = None
            process_context = None
            handled = False
            save_job_id = getattr(outcome, "save_job_id", "")
            with self._lock:
                context = self._save_jobs.get(save_job_id)
                diagnostic_ids = self._diagnostic_ids(
                    context,
                    run_id=getattr(outcome, "run_id", None),
                    save_job_id=save_job_id,
                )
                if context is None:
                    ignored_reason = "save job context is missing"
                elif not isinstance(outcome, AudioSaveOutcome):
                    ignored_reason = "save outcome type is invalid"
                elif not self._valid_save_outcome(context, outcome):
                    ignored_reason = "save outcome identity does not match"
                elif context.terminal_claimed:
                    if not context.cancel_requested:
                        ignored_reason = "save outcome arrived after terminal claim"
                    elif (
                        context.save_cancel_in_flight
                        and context.early_save_outcome is None
                    ):
                        context.early_save_outcome = outcome
                    elif context.save_cancel_in_flight:
                        ignored_reason = "duplicate early save outcome"
                    else:
                        self._save_jobs.pop(save_job_id, None)
                        if context.save_job_id == save_job_id:
                            context.save_job_id = None
                        context.save_child_accepted = False
                        context.save_source_kind = None
                        context.save_source_identity = None
                        context.save_metadata = None
                        self._condition.notify_all()
                    handled = ignored_reason is None
                elif (
                    context.phase is CoordinatorPhase.SAVE_SUBMITTING
                    and context.action is not None
                    and context.action.kind is ExternalActionKind.SAVE_SUBMIT
                ):
                    if context.early_save_outcome is None:
                        context.early_save_outcome = outcome
                        handled = True
                    else:
                        ignored_reason = "duplicate early save outcome"
                elif context.phase is not CoordinatorPhase.SAVING:
                    ignored_reason = "save outcome is stale for current phase"
                else:
                    process_context = context
            if ignored_reason is not None:
                self._diagnose_ids(
                    "save callback ignored",
                    TerminalErrorCategory.AUDIO_SAVE_FAILED,
                    ignored_reason,
                    **diagnostic_ids,
                )
                return
            if handled:
                return
            assert process_context is not None
            self._process_save_outcome(process_context, outcome)
        except Exception as exc:
            save_job_id = getattr(outcome, "save_job_id", "")
            with self._lock:
                context = self._save_jobs.get(save_job_id)
                diagnostic_ids = self._diagnostic_ids(
                    context,
                    run_id=getattr(outcome, "run_id", None),
                    save_job_id=save_job_id,
                )
            self._diagnose_ids(
                "save callback",
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                exc,
                **diagnostic_ids,
            )

    @staticmethod
    def _valid_save_outcome(
        context: _RunContext, outcome: AudioSaveOutcome
    ) -> bool:
        if (
            outcome.run_id != context.run_id
            or outcome.save_job_id != context.save_job_id
            or outcome.source_kind is not context.save_source_kind
            or outcome.source_identity != context.save_source_identity
        ):
            return False
        if outcome.status is not AudioSaveStatus.SAVED:
            return True
        audio = outcome.persisted_audio
        metadata = context.save_metadata
        return bool(
            audio is not None
            and audio.run_id == context.run_id
            and audio.save_job_id == context.save_job_id
            and audio.source_kind is context.save_source_kind
            and audio.source_identity == context.save_source_identity
            and metadata is not None
            and audio.sample_rate_hz == metadata.expected_sample_rate_hz
            and audio.channel_count == metadata.expected_channel_count
            and audio.frame_count == metadata.expected_frame_count
            and audio.format is metadata.format
        )

    def _process_save_outcome(
        self, context: _RunContext, outcome: AudioSaveOutcome
    ) -> None:
        with self._lock:
            if (
                not self._is_current(context)
                or context.terminal_claimed
                or context.phase is not CoordinatorPhase.SAVING
                or not self._valid_save_outcome(context, outcome)
            ):
                return
            save_job_id = context.save_job_id
            if save_job_id is not None:
                self._save_jobs.pop(save_job_id, None)
            context.save_child_accepted = False
            context.save_job_id = None
            context.save_source_kind = None
            context.save_source_identity = None
            context.save_metadata = None
            if outcome.status is AudioSaveStatus.SAVED:
                context.persisted_audio = outcome.persisted_audio
                context.phase = CoordinatorPhase.ATTEMPT_COMMITTING
        if outcome.status is AudioSaveStatus.SAVED:
            self._begin_bridge_attempt(context)
            return
        self._terminalize(
            context,
            TerminalConclusion.ERROR,
            outcome.error_category,
            outcome.reason,
        )

    def _begin_bridge_attempt(self, context: _RunContext) -> None:
        history = self._publish_active(
            context,
            HistoryFactKind.WAITING_ANALYSIS,
            ResultProjectionPhase.WAITING_ANALYSIS,
            ExternalActionKind.WAITING_HISTORY,
            CoordinatorPhase.ATTEMPT_COMMITTING,
        )
        if not history.may_continue:
            return
        with self._lock:
            if not self._next_action_allowed_locked(
                context,
                CoordinatorPhase.ATTEMPT_COMMITTING,
            ):
                return
            audio = context.persisted_audio
            assert audio is not None
            bridge_audio = BridgeAudioReference(
                reference_id=audio.audio_id,
                storage_kind=(
                    AudioStorageKind.IMPORTED_FINAL_AUDIO
                    if context.request.source is TriggerSource.IMPORT
                    else AudioStorageKind.CAPTURED_FINAL_WAV
                ),
                absolute_path=ntpath.normpath(audio.final_path),
                sample_rate_hz=audio.sample_rate_hz,
                channel_count=audio.channel_count,
                frame_count=audio.frame_count,
                dtype="<i2",
                content_revision=f"{audio.audio_id}:{audio.byte_size}",
                audio_data_id=audio.audio_id,
            )
            request = InitialAnalysisRequest(
                run_id=context.run_id,
                audio_reference=bridge_audio,
            )
            action = self._try_set_next_action_locked(
                context,
                ExternalActionKind.SCHEDULER_SUBMIT,
                CoordinatorPhase.ATTEMPT_COMMITTING,
                next_phase=CoordinatorPhase.ANALYSIS_SUBMITTING,
            )
        assert action is not None

        def terminal_sink(report: BridgeTerminalReport) -> None:
            self._on_bridge_initial_report(context, context.run_id, report)

        try:
            result = self._scheduler.submit_initial(request, terminal_sink)
            submit_error = None
        except Exception as exc:
            result = None
            submit_error = exc
        self._reconcile_initial_bridge_result(
            context,
            action,
            result,
            submit_error,
        )

    def _reconcile_initial_bridge_result(
        self,
        context: _RunContext,
        action: ExternalAction,
        result: object,
        submit_error: Exception | None,
    ) -> None:
        invalid_reason = None
        if submit_error is not None:
            invalid_reason = self._exception_reason(
                "bridge initial submit",
                submit_error,
            )
        elif type(result) is not BridgeSubmissionResult:
            invalid_reason = "bridge returned an invalid submission result"
        else:
            try:
                result.__post_init__()
            except Exception as exc:
                invalid_reason = self._exception_reason(
                    "bridge submission result",
                    exc,
                )
            else:
                if result.run_id != context.run_id:
                    invalid_reason = (
                        "bridge submission run identity does not match"
                    )
        if invalid_reason is None:
            assert isinstance(result, BridgeSubmissionResult)
            attempt_id = result.analysis_attempt_id
            if result.authority is BridgeAuthorityStatus.ACQUIRED:
                assert attempt_id is not None
                self._install_acquired_initial_result(
                    context,
                    action,
                    result,
                    attempt_id,
                )
                return
        with self._lock:
            current = self._reconcile_action(context, action)
            if not current:
                return
            quarantined = context.bridge_terminal_quarantine is not None
        if quarantined:
            return
        if invalid_reason is not None:
            with self._lock:
                if self._is_current(context):
                    context.bridge_submission_reconciled = True
            self._fail_closed_bridge_context(
                context,
                "bridge initial submit",
                invalid_reason,
            )
            return
        assert isinstance(result, BridgeSubmissionResult)
        if result.authority is BridgeAuthorityStatus.NOT_ACQUIRED:
            with self._lock:
                held = context.early_analysis_report
                context.early_analysis_report = None
                context.bridge_submission_reconciled = True
                if result.restart_required:
                    self._restart_required = True
                    self._lifecycle = CoordinatorLifecycle.CLOSED
                    self._condition.notify_all()
            if held is not None:
                self._fail_closed_bridge_context(
                    context,
                    "bridge initial report",
                    "terminal report contradicted NOT_ACQUIRED authority",
                )
                return
            if result.restart_required:
                self._diagnose(
                    "bridge initial restart required",
                    context,
                    TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                    RuntimeError(result.reason or result.code),
                )
            self._terminalize(
                context,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING,
                result.reason or result.code or "bridge did not acquire authority",
            )
            return
        if result.authority is BridgeAuthorityStatus.UNKNOWN:
            with self._lock:
                context.early_analysis_report = None
                context.bridge_submission_reconciled = True
            self._fail_closed_bridge_context(
                context,
                "bridge initial submit",
                result.reason or result.code or "bridge authority is unknown",
            )
            return

    def _install_acquired_initial_result(
        self,
        context: _RunContext,
        action: ExternalAction,
        result: BridgeSubmissionResult,
        attempt_id: str,
    ) -> None:
        held_claimed = False
        preserve_report_blocker_on_cancel_failure = False
        with self._lock:
            current = self._reconcile_action(context, action)
            if (
                not current
                or context.bridge_terminal_quarantine is not None
            ):
                return
            held = context.early_analysis_report
            context.early_analysis_report = None
            cancel_reason = None
            if self._attempt_id_is_live_locked(attempt_id, context):
                collision = True
                context.bridge_submission_reconciled = True
                context.terminal_claimed = True
            else:
                collision = False
                context.attempt_id = attempt_id
                context.attempt_kind = (
                    AnalysisAttemptKind.INITIAL_IMPORTED
                    if context.request.source is TriggerSource.IMPORT
                    else AnalysisAttemptKind.INITIAL_CAPTURED
                )
                context.scheduler_child_accepted = True
                context.phase = CoordinatorPhase.ANALYSIS_PENDING
                self._attempts[attempt_id] = context
                context.bridge_submission_reconciled = True
                if held is not None:
                    context.bridge_terminal_consumed = True
                    context.bridge_terminal_consumption_in_progress = True
                    context.terminal_claimed = True
                    context.scheduler_child_accepted = False
                    self._attempts.pop(attempt_id, None)
                    held_claimed = True
                pending = context.pending_terminal
                context.pending_terminal = None
                if held is None and (
                    pending is not None or context.cancel_requested
                ):
                    preserve_report_blocker_on_cancel_failure = (
                        context.cancel_requested
                    )
                    context.cancel_requested = True
                    if not context.scheduler_cancel_attempted:
                        context.scheduler_cancel_attempted = True
                        cancel_reason = (
                            pending.reason
                            if pending is not None
                            else "Coordinator lifecycle stop requested"
                        )
        if collision:
            self._fail_closed_bridge_context(
                context,
                "bridge initial submit",
                "bridge attempt identity collides with a live context",
            )
            return
        if result.terminal_delivered_before_return and held is None:
            self._fail_closed_bridge_context(
                context,
                "bridge initial submit",
                "early-delivery result has no matching held report",
            )
            return
        if held is not None:
            if (
                not held_claimed
                or not self._held_initial_report_is_coherent(result, held)
            ):
                self._fail_closed_bridge_context(
                    context,
                    "bridge initial submit",
                    "bridge submission result contradicts held terminal report",
                )
                return
            self._consume_bridge_initial_report(context, held)
            return
        if cancel_reason is not None:
            self._cancel_frozen_scheduler_attempt(
                context,
                attempt_id,
                cancel_reason,
                preserve_report_blocker_on_failure=(
                    preserve_report_blocker_on_cancel_failure
                ),
            )
            return
        self._publish_active(
            context,
            HistoryFactKind.ANALYZING,
            ResultProjectionPhase.ANALYZING,
            ExternalActionKind.ANALYZING_HISTORY,
            CoordinatorPhase.ANALYSIS_PENDING,
        )

    @staticmethod
    def _held_initial_report_is_coherent(
        result: BridgeSubmissionResult,
        report: BridgeTerminalReport,
    ) -> bool:
        if (
            report.run_id != result.run_id
            or report.analysis_attempt_id != result.analysis_attempt_id
            or report.attempt_kind is not BridgeAttemptKind.INITIAL
        ):
            return False
        if result.status is BridgeSubmissionStatus.ACCEPTED:
            # The sink may run after the bridge's locked acceptance point but
            # before submit_initial returns to this caller.
            return True
        if result.status is BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL:
            return report.status is BridgeTerminalStatus.COMMITTED
        if result.status is BridgeSubmissionStatus.REJECTED:
            return (
                report.status is BridgeTerminalStatus.COMMITTED
                and report.code == result.code
                and report.summary == result.reason
            )
        if result.status is BridgeSubmissionStatus.STATE_UNKNOWN:
            return (
                report.status is not BridgeTerminalStatus.COMMITTED
                and report.code == result.code
                and report.summary == result.reason
            )
        return False

    def _on_bridge_initial_report(
        self,
        context: _RunContext,
        bound_run_id: str,
        report: object,
    ) -> None:
        with self._lock:
            if context.bridge_terminal_quarantine is not None:
                return
            if context.bridge_terminal_consumed:
                if context.bridge_terminal_duplicate_claimed:
                    return
                context.bridge_terminal_duplicate_claimed = True
                self._restart_required = True
                self._lifecycle = CoordinatorLifecycle.CLOSED
                self._condition.notify_all()
                duplicate_consumed = True
            else:
                duplicate_consumed = False
        if duplicate_consumed:
            self._diagnose(
                "bridge initial duplicate report",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                RuntimeError(
                    "duplicate bridge terminal report after consumption"
                ),
            )
            return
        if type(report) is not BridgeTerminalReport:
            self._fail_closed_bridge_context(
                context,
                "bridge initial report",
                "bridge terminal report type is invalid",
            )
            return
        try:
            report.__post_init__()
        except Exception as exc:
            self._fail_closed_bridge_context(
                context,
                "bridge initial report",
                self._exception_reason("bridge terminal report", exc),
            )
            return
        with self._lock:
            if context.bridge_terminal_quarantine is not None:
                return
            if (
                not self._is_current(context)
                or bound_run_id != context.run_id
                or context.bridge_terminal_consumed
            ):
                invalid_reason = "bridge terminal report is stale or duplicate"
                held = False
            elif not context.bridge_submission_reconciled:
                if context.early_analysis_report is None:
                    context.early_analysis_report = report
                    return
                invalid_reason = "duplicate early bridge terminal report"
                held = False
            else:
                invalid_reason = None
                held = False
        if invalid_reason is not None:
            self._fail_closed_bridge_context(
                context,
                "bridge initial report",
                invalid_reason,
            )
            return
        if not held:
            self._consume_bridge_initial_report(context, report)

    def _consume_bridge_initial_report(
        self,
        context: _RunContext,
        report: BridgeTerminalReport,
    ) -> None:
        with self._lock:
            preclaimed = context.bridge_terminal_consumption_in_progress
            valid = (
                self._is_current(context)
                and context.bridge_terminal_quarantine is None
                and (preclaimed or not context.bridge_terminal_consumed)
                and context.phase is CoordinatorPhase.ANALYSIS_PENDING
                and report.run_id == context.run_id
                and report.analysis_attempt_id == context.attempt_id
                and report.attempt_kind is BridgeAttemptKind.INITIAL
            )
            if valid:
                context.bridge_terminal_consumption_in_progress = False
                context.bridge_terminal_consumed = True
                context.terminal_claimed = True
                context.scheduler_child_accepted = False
                if context.attempt_id is not None:
                    self._attempts.pop(context.attempt_id, None)
        if not valid:
            self._fail_closed_bridge_context(
                context,
                "bridge initial report",
                "bridge terminal report identity or phase does not match",
            )
            return
        if report.status is not BridgeTerminalStatus.COMMITTED:
            self._fail_closed_bridge_context(
                context,
                "bridge initial report",
                report.summary or report.code,
            )
            return
        try:
            terminal = self._terminal_from_bridge_report(context, report)
        except Exception as exc:
            self._fail_closed_bridge_context(
                context,
                "bridge initial report",
                self._exception_reason("bridge report mapping", exc),
            )
            return
        with self._lock:
            if not self._is_current(context):
                return
            context.terminal_fact = terminal
            publication = self._replace_with_publication(
                context,
                terminal,
                _PlcSettlementKind.PUBLISH_TERMINAL
                if context.ticket is not None
                else _PlcSettlementKind.NONE,
            )
        if publication.history_in_flight:
            self._publish_terminal_history(publication)
        if publication.plc_in_flight:
            self._publish_plc_settlement(publication)
        self._retire_if_publication_complete(publication)

    @staticmethod
    def _terminal_from_bridge_report(
        context: _RunContext,
        report: BridgeTerminalReport,
    ) -> TerminalFact:
        if report.terminal_at is None:
            raise ValueError("committed report requires terminal timestamp")
        terminal_at = datetime.fromisoformat(
            report.terminal_at.replace("Z", "+00:00")
        )
        conclusion, category, reason, payload = (
            MultiRunCoordinator._map_committed_initial_report(report)
        )
        return TerminalFact(
            run_id=context.run_id,
            source=context.request.source,
            attempt_kind=(
                AnalysisAttemptKind.INITIAL_IMPORTED
                if context.request.source is TriggerSource.IMPORT
                else AnalysisAttemptKind.INITIAL_CAPTURED
            ),
            attempt_id=report.analysis_attempt_id,
            conclusion=conclusion,
            error_category=category,
            reason=reason,
            result_payload=payload,
            history_labels=context.frozen.history_labels,
            terminal_at=terminal_at,
        )

    @staticmethod
    def _map_committed_initial_report(
        report: BridgeTerminalReport,
    ) -> tuple[
        TerminalConclusion,
        TerminalErrorCategory | None,
        str,
        FrozenPayload | None,
    ]:
        state = report.attempt_state
        code = report.code
        if state is BridgeAttemptState.SUCCEEDED:
            if code != "ANALYSIS_SUCCEEDED":
                raise ValueError("successful report code is invalid")
            conclusion = {
                BridgeJudgment.PASS: TerminalConclusion.OK,
                BridgeJudgment.FAIL: TerminalConclusion.NG,
            }.get(report.judgment)
            if conclusion is None or report.result_payload_json is None:
                raise ValueError("successful report payload is invalid")
            return (
                conclusion,
                None,
                "",
                FrozenPayload(
                    value_id=f"bridge-result:{report.analysis_attempt_id}",
                    canonical_bytes=report.result_payload_json.encode("utf-8"),
                ),
            )
        if state is BridgeAttemptState.NO_RESULT:
            if code != "ANALYSIS_NO_RESULT":
                raise ValueError("no-result report code is invalid")
            return TerminalConclusion.NO_RESULT, None, "", None
        if state is BridgeAttemptState.FAILED:
            if code not in _INITIAL_BRIDGE_FAILED_CODES:
                raise ValueError("failed report code is invalid")
            return (
                TerminalConclusion.ERROR,
                TerminalErrorCategory.ANALYSIS_FAILED,
                MultiRunCoordinator._bounded(report.summary or code),
                None,
            )
        if state is BridgeAttemptState.CANCELED:
            cancellation = {
                "USER_CANCELED": (
                    TerminalConclusion.MANUAL_STOPPED,
                    TerminalErrorCategory.MANUAL_STOPPED,
                ),
                "ANALYSIS_SCHEDULER_SHUTDOWN": (
                    TerminalConclusion.ERROR,
                    TerminalErrorCategory.SHUTDOWN_CANCELLED,
                ),
            }.get(code)
            if cancellation is None:
                raise ValueError("canceled report code is invalid")
            return (
                cancellation[0],
                cancellation[1],
                MultiRunCoordinator._bounded(report.summary or code),
                None,
            )
        raise ValueError("committed report terminal state is invalid")

    def _fail_closed_bridge_context(
        self,
        context: _RunContext,
        stage: str,
        reason: object,
    ) -> None:
        quarantine_reason = (
            self._exception_reason(stage, reason)
            if isinstance(reason, Exception)
            else self._bounded(str(reason))
        )
        with self._lock:
            if context.bridge_terminal_quarantine is not None:
                return
            context.bridge_terminal_quarantine = (
                _BridgeTerminalQuarantineFact(
                    run_id=context.run_id,
                    stage=self._bounded(stage),
                    reason=quarantine_reason,
                )
            )
            self._restart_required = True
            self._lifecycle = CoordinatorLifecycle.CLOSED
            context.early_analysis_report = None
            context.bridge_submission_reconciled = True
            context.bridge_terminal_consumed = True
            context.bridge_terminal_consumption_in_progress = False
            if context.attempt_id is not None:
                self._attempts.pop(context.attempt_id, None)
            context.scheduler_child_accepted = False
            context.cancel_requested = True
            context.pending_terminal = None
            context.terminal_claimed = True
            context.history_allowed = False
            self._condition.notify_all()
        self._diagnose(
            stage,
            context,
            TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
            reason if isinstance(reason, Exception) else RuntimeError(str(reason)),
        )

    def _terminalize(
        self,
        context: _RunContext,
        conclusion: TerminalConclusion,
        category: TerminalErrorCategory | None,
        reason: str,
        result_payload=None,
    ) -> StartResult:
        with self._lock:
            bridge_owned = bool(
                self._is_current(context)
                and context.bridge_submission_reconciled
                and context.attempt_id is not None
                and not context.bridge_terminal_consumed
            )
            request_cancel = bool(
                bridge_owned and not context.scheduler_cancel_attempted
            )
            if request_cancel:
                context.scheduler_cancel_attempted = True
                attempt_id = context.attempt_id
            else:
                attempt_id = None
        if bridge_owned:
            if request_cancel:
                assert attempt_id is not None
                self._cancel_frozen_scheduler_attempt(
                    context,
                    attempt_id,
                    reason,
                )
            return self._current_start_result(context)
        with self._lock:
            if not self._is_current(context) or context.terminal_claimed:
                return self._current_start_result(context)
            requested = PendingTerminalContender(
                kind=TerminalContenderKind.EXTERNAL_FAILURE,
                run_id=context.run_id,
                attempt_id=context.attempt_id,
                save_job_id=context.save_job_id,
                conclusion=conclusion,
                error_category=category,
                reason=self._bounded(reason),
                result_payload=result_payload,
            )
            if (
                context.action is not None
                or context.current_start_in_progress
                or context.current_settlement_in_progress
            ):
                if context.pending_terminal is None:
                    context.pending_terminal = requested
                return self._current_start_result(context)
            claim = self._claim_terminal_locked(context, requested)
        assert claim is not None
        return self._finish_terminal_claim_after_current_cleanup(
            context,
            claim,
        )

    def _finish_terminal_claim_after_current_cleanup(
        self,
        context: _RunContext,
        claim: _TerminalClaim,
    ) -> StartResult:
        self._cleanup_terminal_current(context)
        return self._finish_terminal_claim(context, claim)

    def _claim_terminal_locked(
        self,
        context: _RunContext,
        requested: PendingTerminalContender,
    ) -> _TerminalClaim | None:
        if (
            not self._is_current(context)
            or context.terminal_claimed
            or context.action is not None
        ):
            return None
        context.terminal_claimed = True
        terminal_attempt_id = context.attempt_id
        reservation_to_cancel = None
        save_job_to_cancel = None
        scheduler_attempt_to_cancel = None
        if (
            context.reservation is not None
            and not context.reservation_cancel_attempted
        ):
            context.reservation_cancel_attempted = True
            reservation_to_cancel = context.reservation
        if (
            context.save_child_accepted
            and context.save_job_id is not None
            and not context.save_cancel_attempted
        ):
            context.save_cancel_attempted = True
            save_job_to_cancel = context.save_job_id
        if (
            context.scheduler_child_accepted
            and context.attempt_id is not None
            and not context.scheduler_cancel_attempted
        ):
            context.scheduler_cancel_attempted = True
            scheduler_attempt_to_cancel = context.attempt_id
        return _TerminalClaim(
            requested=requested,
            terminal_attempt_id=terminal_attempt_id,
            terminal_attempt_kind=(
                AnalysisAttemptKind.INITIAL_IMPORTED
                if context.request.source is TriggerSource.IMPORT
                else AnalysisAttemptKind.INITIAL_CAPTURED
            ),
            terminal_history_labels=(
                context.frozen.history_labels
                if context.frozen is not None
                else context.request.fallback_history_labels
            ),
            fail_closed_category=self._terminal_cleanup_category(
                context, terminal_attempt_id
            ),
            reservation_to_cancel=reservation_to_cancel,
            save_job_to_cancel=save_job_to_cancel,
            scheduler_attempt_to_cancel=scheduler_attempt_to_cancel,
            clock_action=self._set_action(
                context,
                ExternalActionKind.TERMINAL_COMMIT,
                context.phase,
            ),
        )

    def _finish_terminal_claim(
        self,
        context: _RunContext,
        claim: _TerminalClaim,
    ) -> StartResult:
        requested = claim.requested
        try:
            terminal_at = self._wall_clock()
            clock_error = None
        except Exception as exc:
            terminal_at = _WALL_CLOCK_FAILURE_AT
            clock_error = exc
        if clock_error is None and not self._is_aware_datetime(terminal_at):
            terminal_at = _WALL_CLOCK_FAILURE_AT
            clock_error = ValueError(
                "wall clock returned a non-aware datetime"
            )
        if clock_error is not None:
            terminal_conclusion = TerminalConclusion.ERROR
            terminal_category = claim.fail_closed_category
            terminal_reason = self._exception_reason(
                "terminal wall clock", clock_error
            )
            terminal_payload = None
        else:
            terminal_conclusion = requested.conclusion
            terminal_category = requested.error_category
            terminal_reason = requested.reason
            terminal_payload = requested.result_payload
        assert self._is_aware_datetime(terminal_at)
        try:
            terminal = TerminalFact(
                run_id=context.run_id,
                source=context.request.source,
                attempt_kind=claim.terminal_attempt_kind,
                attempt_id=claim.terminal_attempt_id,
                conclusion=terminal_conclusion,
                error_category=terminal_category,
                reason=terminal_reason,
                result_payload=terminal_payload,
                history_labels=claim.terminal_history_labels,
                terminal_at=terminal_at,
            )
            fact_error = None
        except Exception as exc:
            fact_error = exc
            terminal = TerminalFact(
                run_id=context.run_id,
                source=context.request.source,
                attempt_kind=claim.terminal_attempt_kind,
                attempt_id=claim.terminal_attempt_id,
                conclusion=TerminalConclusion.ERROR,
                error_category=claim.fail_closed_category,
                reason=self._exception_reason(
                    "terminal fact validation", exc
                ),
                result_payload=None,
                history_labels=claim.terminal_history_labels,
                terminal_at=terminal_at,
            )
        with self._lock:
            current = self._reconcile_action(context, claim.clock_action)
            if current:
                context.terminal_fact = terminal
                context.phase = CoordinatorPhase.TERMINAL_COMMITTING
        if not current:
            return self._current_start_result(context)
        if clock_error is not None:
            self._diagnose(
                "terminal wall clock",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                clock_error,
            )
        if fact_error is not None:
            self._diagnose(
                "terminal fact validation",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                fact_error,
            )
        if claim.reservation_to_cancel is not None:
            self._cancel_frozen_reservation(
                context, claim.reservation_to_cancel
            )
        if claim.save_job_to_cancel is not None:
            self._cancel_frozen_save_job(context, claim.save_job_to_cancel)
        if claim.scheduler_attempt_to_cancel is not None:
            self._cancel_frozen_scheduler_attempt(
                context,
                claim.scheduler_attempt_to_cancel,
                terminal.reason,
            )
        with self._lock:
            if not self._is_current(context):
                return StartResult(
                    run_id=context.run_id,
                    status=StartStatus.ACCEPTED_TERMINAL,
                    accepted=True,
                    phase=None,
                    terminal=terminal,
                    reason=terminal.reason,
                )
            action = self._set_action(
                context,
                ExternalActionKind.TERMINAL_COMMIT,
                CoordinatorPhase.TERMINAL_COMMITTING,
            )
        try:
            commit = self._persistence.commit_initial_terminal(terminal)
            commit_error = None
        except Exception as exc:
            commit = CommitResult.UNAVAILABLE
            commit_error = exc
        with self._lock:
            current = self._reconcile_action(context, action)
        if not current:
            return StartResult(
                run_id=context.run_id,
                status=StartStatus.ACCEPTED_TERMINAL,
                accepted=True,
                phase=None,
                terminal=terminal,
                reason=terminal.reason,
            )
        if commit is CommitResult.COMMITTED:
            with self._lock:
                if context.attempt_id is not None:
                    self._attempts.pop(context.attempt_id, None)
                if (
                    context.save_job_id is not None
                    and (
                        not context.save_child_accepted
                        or not context.cancel_requested
                    )
                ):
                    self._save_jobs.pop(context.save_job_id, None)
                publication = self._replace_with_publication(
                    context,
                    terminal,
                    _PlcSettlementKind.PUBLISH_TERMINAL
                    if context.ticket is not None
                    else _PlcSettlementKind.NONE,
                )
            if publication.history_in_flight:
                self._publish_terminal_history(publication)
            if publication.plc_in_flight:
                self._publish_plc_settlement(publication)
            self._retire_if_publication_complete(publication)
        else:
            failure_reason = (
                self._exception_reason("initial terminal commit", commit_error)
                if commit_error is not None
                else "initial terminal commit rejected"
            )
            persistence_failure = TerminalFact(
                run_id=context.run_id,
                source=context.request.source,
                attempt_kind=terminal.attempt_kind,
                attempt_id=context.attempt_id,
                conclusion=TerminalConclusion.ERROR,
                error_category=(
                    TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED
                ),
                reason=self._bounded(failure_reason),
                result_payload=None,
                history_labels=terminal.history_labels,
                terminal_at=terminal.terminal_at,
            )
            with self._lock:
                if context.attempt_id is not None:
                    self._attempts.pop(context.attempt_id, None)
                if (
                    context.save_job_id is not None
                    and (
                        not context.save_child_accepted
                        or not context.cancel_requested
                    )
                ):
                    self._save_jobs.pop(context.save_job_id, None)
                if context.ticket is not None:
                    context.history_allowed = False
                    publication = self._replace_with_publication(
                        context,
                        persistence_failure,
                        _PlcSettlementKind.PUBLISH_TERMINAL,
                    )
                else:
                    publication = None
                    self._runs.pop(context.run_id, None)
                    self._condition.notify_all()
            self._diagnose(
                "initial terminal commit",
                context,
                TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
                commit_error
                if commit_error is not None
                else RuntimeError(failure_reason),
            )
            if publication is not None:
                self._publish_plc_settlement(publication)
                self._retire_if_publication_complete(publication)
        return StartResult(
            run_id=context.run_id,
            status=StartStatus.ACCEPTED_TERMINAL,
            accepted=True,
            phase=None,
            terminal=terminal,
            reason=terminal.reason,
        )

    def _pop_pending_terminal_locked(
        self, context: _RunContext
    ) -> _TerminalClaim | None:
        if (
            not self._is_current(context)
            or context.action is not None
            or context.terminal_claimed
        ):
            return None
        contender = context.pending_terminal
        if contender is None:
            return None
        context.pending_terminal = None
        claim = self._claim_terminal_locked(context, contender)
        assert claim is not None
        return claim

    def _pop_preaccept_contender_locked(
        self, context: _RunContext, stage: str
    ) -> str | None:
        contender = (
            context.pending_terminal
            if self._is_current(context)
            and context.action is None
            and not context.terminal_claimed
            else None
        )
        if contender is not None:
            context.pending_terminal = None
            return contender.reason
        if (
            self._is_current(context)
            and context.action is None
            and context.cancel_requested
        ):
            return self._bounded(f"ingress canceled during {stage}")
        return None

    def _terminalize_contender(
        self,
        context: _RunContext,
        contender: _TerminalClaim,
    ) -> StartResult:
        return self._finish_terminal_claim_after_current_cleanup(
            context,
            contender,
        )

    def _current_start_result(self, context: _RunContext) -> StartResult:
        with self._lock:
            if context.terminal_fact is not None:
                return StartResult(
                    run_id=context.run_id,
                    status=StartStatus.ACCEPTED_TERMINAL,
                    accepted=True,
                    phase=None,
                    terminal=context.terminal_fact,
                    reason=context.terminal_fact.reason,
                )
            phase = context.phase
            if phase in {
                CoordinatorPhase.PREFLIGHT,
                CoordinatorPhase.RESERVING,
                CoordinatorPhase.ACTIVE_COMMITTING,
            }:
                if context.durable_active:
                    phase = CoordinatorPhase.REGISTERING
                else:
                    return self._immediate_rejection(
                        context.run_id,
                        StartStatus.PREFLIGHT_REJECTED,
                        "ingress action has not reached durable acceptance",
                    )
            return StartResult(
                run_id=context.run_id,
                status=StartStatus.ACCEPTED_ACTIVE,
                accepted=True,
                phase=phase,
                terminal=None,
                reason="",
            )

    def _set_action(
        self,
        context: _RunContext,
        kind: ExternalActionKind,
        phase: CoordinatorPhase,
    ) -> ExternalAction:
        if context.action is not None:
            raise RuntimeError("external action is already installed")
        attempt_id = (
            context.attempt_id
            if kind
            in {
                ExternalActionKind.ATTEMPT_COMMIT,
                ExternalActionKind.WAITING_HISTORY,
                ExternalActionKind.SCHEDULER_SUBMIT,
                ExternalActionKind.ANALYZING_HISTORY,
                ExternalActionKind.TERMINAL_COMMIT,
            }
            else None
        )
        save_job_id = (
            context.save_job_id
            if kind is ExternalActionKind.SAVE_SUBMIT
            else None
        )
        reservation_id = (
            context.reservation.reservation_id
            if context.reservation is not None
            and kind
            in {
                ExternalActionKind.RESIDENT_RESERVE,
                ExternalActionKind.ACTIVE_COMMIT,
                ExternalActionKind.CAPTURE_REGISTER,
                ExternalActionKind.ACTIVE_HISTORY,
                ExternalActionKind.CAPTURE_START,
                ExternalActionKind.CHILD_CANCEL,
            }
            else None
        )
        action = ExternalAction(
            kind=kind,
            run_id=context.run_id,
            expected_phase=phase,
            attempt_id=attempt_id,
            save_job_id=save_job_id,
            reservation_id=reservation_id,
        )
        context.action = action
        return action

    def _next_action_allowed_locked(
        self,
        context: _RunContext,
        expected_phase: CoordinatorPhase,
    ) -> bool:
        return bool(
            self._is_current(context)
            and context.phase is expected_phase
            and context.action is None
            and not context.terminal_claimed
            and not context.cancel_requested
            and context.pending_terminal is None
        )

    def _try_set_next_action_locked(
        self,
        context: _RunContext,
        kind: ExternalActionKind,
        expected_phase: CoordinatorPhase,
        *,
        next_phase: CoordinatorPhase | None = None,
    ) -> ExternalAction | None:
        if not self._next_action_allowed_locked(context, expected_phase):
            return None
        action_phase = (
            expected_phase if next_phase is None else next_phase
        )
        if next_phase is not None:
            context.phase = next_phase
        return self._set_action(context, kind, action_phase)

    def _set_child_cancel_action_locked(
        self,
        context: _RunContext,
        *,
        attempt_id: str | None = None,
        save_job_id: str | None = None,
        reservation_id: str | None = None,
    ) -> ExternalAction:
        if context.action is not None:
            raise RuntimeError("external action is already installed")
        action = ExternalAction(
            kind=ExternalActionKind.CHILD_CANCEL,
            run_id=context.run_id,
            expected_phase=context.phase,
            attempt_id=attempt_id,
            save_job_id=save_job_id,
            reservation_id=reservation_id,
        )
        context.action = action
        return action

    @staticmethod
    def _detached_lifecycle_cancel_action_locked(
        context: _RunContext | _ResultOperationContext,
        *,
        attempt_id: str | None = None,
        save_job_id: str | None = None,
        reservation_id: str | None = None,
        reanalysis: bool = False,
    ) -> ExternalAction:
        return ExternalAction(
            kind=(
                ExternalActionKind.REANALYSIS_CANCEL
                if reanalysis
                else ExternalActionKind.CHILD_CANCEL
            ),
            run_id=context.run_id,
            expected_phase=context.phase,
            attempt_id=attempt_id,
            save_job_id=save_job_id,
            reservation_id=reservation_id,
        )

    def _track_child_cancel_action_locked(
        self,
        action: ExternalAction,
        *,
        durable_run: bool,
    ) -> None:
        prior = self._child_cancel_action_durable.get(action)
        if prior is not None and prior is not durable_run:
            raise RuntimeError(
                "child cancel action run classification changed"
            )
        self._child_cancel_action_durable[action] = durable_run
        self._child_cancel_actions.add(action)

    def _retire_child_cancel_action_locked(
        self,
        action: ExternalAction,
    ) -> None:
        self._child_cancel_actions.discard(action)
        self._child_cancel_action_durable.pop(action, None)

    def _reconcile_action(
        self, context: _RunContext, action: ExternalAction
    ) -> bool:
        if not self._is_current(context) or context.action is not action:
            return False
        context.action = None
        self._condition.notify_all()
        return True

    def _is_current(self, context: _RunContext) -> bool:
        return self._runs.get(context.run_id) is context

    def _save_job_id_is_live_locked(
        self, save_job_id: str, context: _RunContext
    ) -> bool:
        owner = self._save_jobs.get(save_job_id)
        if owner is not None and owner is not context:
            return True
        return any(
            isinstance(candidate, _RunContext)
            and candidate is not context
            and candidate.save_job_id == save_job_id
            for candidate in self._runs.values()
        )

    def _attempt_id_is_live_locked(
        self, attempt_id: str, context: _RunContext
    ) -> bool:
        owner = self._attempts.get(attempt_id)
        if owner is not None and owner is not context:
            return True
        return any(
            isinstance(candidate, _RunContext)
            and candidate is not context
            and candidate.attempt_id == attempt_id
            for candidate in self._runs.values()
        )

    @staticmethod
    def _terminal_cleanup_category(
        context: _RunContext, attempt_id: str | None
    ) -> TerminalErrorCategory:
        if attempt_id is not None:
            return TerminalErrorCategory.ANALYSIS_FAILED
        if (
            context.request.source is TriggerSource.IMPORT
            or context.save_job_id is not None
            or context.persisted_audio is not None
            or context.phase
            in {
                CoordinatorPhase.SAVE_SUBMITTING,
                CoordinatorPhase.SAVING,
                CoordinatorPhase.ATTEMPT_COMMITTING,
                CoordinatorPhase.ANALYSIS_SUBMITTING,
                CoordinatorPhase.ANALYSIS_PENDING,
            }
        ):
            return TerminalErrorCategory.AUDIO_SAVE_FAILED
        return TerminalErrorCategory.CAPTURE_FAILED

    def _diagnose(
        self,
        stage: str,
        context: _RunContext | _PublishingContext | None,
        category: TerminalErrorCategory,
        error: Exception,
    ) -> None:
        self._diagnose_ids(
            stage,
            category,
            error,
            **self._diagnostic_ids(context),
        )

    @staticmethod
    def _append_bounded_error(errors: list[str], error: str) -> None:
        if len(errors) < 32:
            errors.append(error)

    def _record_lifecycle_error_locked(self, error: str) -> None:
        if self._shutdown_begun:
            self._append_bounded_error(self._shutdown_errors, error)
        elif self._restart_required:
            self._append_bounded_error(self._restart_errors, error)
        else:
            self._append_bounded_error(self._stop_errors, error)

    def _promote_stop_errors_locked(self) -> None:
        for error in self._stop_errors:
            self._append_bounded_error(self._restart_errors, error)
        self._stop_errors.clear()

    def _diagnose_ids(
        self,
        stage: str,
        category: TerminalErrorCategory,
        detail: Exception | str,
        *,
        run_id: str | None,
        attempt_id: str | None,
        save_job_id: str | None,
    ) -> None:
        try:
            self._diagnostics.record(
                DiagnosticFact(
                    stage=stage,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    save_job_id=save_job_id,
                    category=category,
                    message=(
                        self._exception_reason(stage, detail)
                        if isinstance(detail, Exception)
                        else self._bounded(detail)
                    ),
                )
            )
        except Exception:
            pass

    @classmethod
    def _diagnostic_ids(
        cls,
        context: _RunContext | _PublishingContext | None,
        *,
        run_id: object = None,
        attempt_id: object = None,
        save_job_id: object = None,
    ) -> dict[str, str | None]:
        return {
            "run_id": cls._optional_uuid(
                getattr(context, "run_id", None) or run_id
            ),
            "attempt_id": cls._optional_uuid(
                getattr(context, "attempt_id", None) or attempt_id
            ),
            "save_job_id": cls._optional_uuid(
                getattr(context, "save_job_id", None) or save_job_id
            ),
        }

    @staticmethod
    def _optional_uuid(value: object) -> str | None:
        try:
            return require_uuid4(value, "diagnostic identity")
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _is_aware_datetime(value: object) -> bool:
        try:
            return bool(
                isinstance(value, datetime)
                and value.tzinfo is not None
                and value.utcoffset() is not None
            )
        except Exception:
            return False

    @staticmethod
    def _bounded(value: str) -> str:
        value = str(value).replace("\x00", " ")
        while len(value.encode("utf-8")) > 512:
            value = value[:-1]
        require_bounded_text(value, "reason")
        return value

    def _exception_reason(self, stage: str, error: Exception | None) -> str:
        if error is None:
            return self._bounded(f"{stage} failed")
        return self._bounded(f"{stage} failed: {type(error).__name__}: {error}")
