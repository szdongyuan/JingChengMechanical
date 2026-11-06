from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
from threading import Event
from typing import Callable

from app.service.analysis.scheduler import (
    CancelReceipt,
    CancelStatus,
    SubmitReceipt,
    SubmitStatus,
)
from app.service.run_persistence import (
    BridgeAuthorityStatus,
    BridgeCancelRequest,
    BridgeCancelResult,
    BridgeCancelStatus,
    BridgeSubmissionResult,
    BridgeSubmissionStatus,
    InitialAnalysisRequest,
    ReanalysisRequest as BridgeReanalysisRequest,
    CurrentTrace,
)
from app.service.current_meter_service import CurrentSample
from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    AnalysisAttemptKind,
    AudioMetadataConstraints,
    AudioSaveOutcome,
    AudioSaveSourceKind,
    AudioSaveStatus,
    CapturedAudioSaveRequest,
    CapturedAudioValue,
    CapturedSourceFormat,
    CapturedStartRequest,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CaptureReservation,
    CaptureReserveResult,
    CaptureReserveStatus,
    CommitResult,
    FinalPathRequest,
    FrozenCapturedRun,
    FrozenImportedRun,
    FrozenPayload,
    HistoryFactKind,
    HistoryProjectionFact,
    ImportedAudioSaveRequest,
    ImportedSourceFormat,
    ImportedSourceReference,
    ImportedStartRequest,
    PersistedAudioFormat,
    PersistedAudioReference,
    PlcIngressTicket,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
    ProjectedHistoryEvent,
    ResultControlFacts,
    ResultProjection,
    ResultProjectionPhase,
    ResidentCancelResult,
    ResidentCancelStatus,
    SaveCancelResult,
    SaveCancelStatus,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
    SaveSubmission,
    SaveSubmitStatus,
    TerminalConclusion,
    TerminalErrorCategory,
    TerminalFact,
    TriggerSource,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    AnalysisJob,
    AnalysisOutcome,
    AnalysisOutcomeStatus,
    SchedulerCancelResult,
    SchedulerCancelStatus,
    SchedulerSubmission,
    SchedulerSubmitStatus,
)


RUN_IDS = tuple(
    f"00000000-0000-4000-8000-{value:012d}" for value in range(1, 33)
)
ATTEMPT_IDS = tuple(
    f"10000000-0000-4000-8000-{value:012d}" for value in range(1, 33)
)
SAVE_JOB_IDS = tuple(
    f"20000000-0000-4000-8000-{value:012d}" for value in range(1, 33)
)
AUDIO_IDS = tuple(
    f"30000000-0000-4000-8000-{value:012d}" for value in range(1, 33)
)
NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)


def payload(name: str) -> FrozenPayload:
    return FrozenPayload(value_id=name, canonical_bytes=name.encode("utf-8"))


def canonical_payload(value: object) -> FrozenPayload:
    canonical_bytes = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return FrozenPayload(
        value_id="sha256:" + hashlib.sha256(canonical_bytes).hexdigest(),
        canonical_bytes=canonical_bytes,
    )


def run_config_payload(
    *,
    current_enabled: bool = False,
    fault_types: tuple[str, ...] = (),
) -> FrozenPayload:
    return canonical_payload(
        {
            "schema": "jingcheng-run-config/v1",
            "config": {
                "config_id": 7,
                "config_name": "配置 A",
                "current_meter": {
                    "enabled": current_enabled,
                    "source": "modbus_tcp",
                },
                "fault_types": list(fault_types),
            },
        }
    )


def analysis_input_payload(
    channel_order: tuple[int, ...] = (0,),
) -> FrozenPayload:
    return canonical_payload(
        {
            "schema": "analysis-input/v1",
            "config": {
                "config_id": 7,
                "config_name": "配置 A",
                "fault_types": [],
            },
            "workorder": {},
            "channel_mapping": {
                "schema": "audio-channel-map/v1",
                "channel_order": list(channel_order),
            },
            "analysis": {
                "window_start_seconds": 0.0,
                "window_end_seconds": 1.0,
                "algorithm_revision": "test-r1",
                "execution_timeout_seconds": 30.0,
                "queue_timeout_seconds": None,
                "toolchain": None,
            },
        }
    )


def captured_request(
    run_id: str = RUN_IDS[0],
    *,
    source: TriggerSource = TriggerSource.MANUAL,
) -> CapturedStartRequest:
    return CapturedStartRequest(
        run_id=run_id,
        trigger_monotonic_ns=10,
        source=source,
        ingress_payload=payload("captured-ingress"),
        fallback_history_labels=payload("fallback-labels"),
        plc_id=1 if source is TriggerSource.PLC else None,
    )


def imported_request(run_id: str = RUN_IDS[0]) -> ImportedStartRequest:
    return ImportedStartRequest(
        run_id=run_id,
        trigger_monotonic_ns=10,
        source=TriggerSource.IMPORT,
        ingress_payload=payload("imported-ingress"),
        fallback_history_labels=payload("fallback-labels"),
    )


def captured_audio(
    source_identity: str = "capture-source",
    *,
    channel_indices: tuple[int, ...] = (0,),
) -> CapturedAudioValue:
    return CapturedAudioValue(
        source_identity=source_identity,
        sample_rate_hz=16_000,
        channel_count=1,
        frame_count=2,
        format=CapturedSourceFormat.INT16_INTERLEAVED,
        interleaved_bytes=b"\x00\x00\x01\x00",
        channel_indices=channel_indices,
    )


def imported_source(
    source_identity: str = "import-source",
) -> ImportedSourceReference:
    return ImportedSourceReference(
        source_identity=source_identity,
        absolute_path="C:/audio/import.wav",
        sample_rate_hz=16_000,
        channel_count=1,
        frame_count=2,
        format=ImportedSourceFormat.PCM16_WAV,
    )


def metadata() -> AudioMetadataConstraints:
    return AudioMetadataConstraints(
        expected_sample_rate_hz=16_000,
        expected_channel_count=1,
        expected_frame_count=2,
        format=PersistedAudioFormat.PCM16_WAV,
    )


def frozen_captured(
    request: CapturedStartRequest,
    *,
    config_id: str = "same-config",
    current_enabled: bool = False,
    channel_order: tuple[int, ...] = (0,),
    fault_types: tuple[str, ...] = (),
) -> FrozenCapturedRun:
    return FrozenCapturedRun(
        run_id=request.run_id,
        trigger_monotonic_ns=request.trigger_monotonic_ns,
        source=request.source,
        config_id=config_id,
        hardware_id="hardware-1",
        target_frame_count=2,
        config_payload=run_config_payload(
            current_enabled=current_enabled,
            fault_types=fault_types,
        ),
        hardware_payload=payload("hardware"),
        calibration_payload=payload("calibration"),
        work_order_payload=payload("work-order"),
        history_labels=payload("history-labels"),
        analysis_input=analysis_input_payload(channel_order),
        final_path_request=FinalPathRequest("C:/audio/final.wav"),
        metadata=metadata(),
    )


def frozen_imported(
    request: ImportedStartRequest,
    *,
    config_id: str = "same-config",
) -> FrozenImportedRun:
    return FrozenImportedRun(
        run_id=request.run_id,
        trigger_monotonic_ns=request.trigger_monotonic_ns,
        source=request.source,
        config_id=config_id,
        source_reference=imported_source(),
        config_payload=payload("config"),
        calibration_payload=payload("calibration"),
        work_order_payload=payload("work-order"),
        history_labels=payload("history-labels"),
        analysis_input=payload("analysis-input"),
        final_path_request=FinalPathRequest("C:/audio/final.wav"),
        metadata=metadata(),
    )


def persisted_audio(
    request: CapturedAudioSaveRequest | ImportedAudioSaveRequest,
    *,
    audio_id: str = AUDIO_IDS[0],
) -> PersistedAudioReference:
    return PersistedAudioReference(
        audio_id=audio_id,
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        final_path="C:/audio/final.wav",
        sample_rate_hz=16_000,
        channel_count=1,
        frame_count=2,
        byte_size=4,
        format=PersistedAudioFormat.PCM16_WAV,
        content_revision=1,
    )


def saved_outcome(
    request: CapturedAudioSaveRequest | ImportedAudioSaveRequest,
) -> AudioSaveOutcome:
    return AudioSaveOutcome(
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        status=AudioSaveStatus.SAVED,
        persisted_audio=persisted_audio(request),
        error_category=None,
        reason="",
    )


def completed_analysis(job: AnalysisJob) -> AnalysisOutcome:
    return AnalysisOutcome(
        attempt_id=job.attempt_id,
        run_id=job.run_id,
        attempt_kind=job.attempt_kind,
        audio_id=job.audio.audio_id,
        audio_content_revision=job.audio.content_revision,
        status=AnalysisOutcomeStatus.COMPLETED,
        conclusion=TerminalConclusion.OK,
        result_payload=payload("analysis-result"),
        error_category=None,
        reason="",
    )


class Hook:
    def __init__(self) -> None:
        self.entered: Event | None = None
        self.release: Event | None = None

    def wait(self) -> None:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            if not self.release.wait(5):
                raise AssertionError("test hook was not released")


class FakeCapturedFreezer:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[CapturedStartRequest] = []
        self.exception: Exception | None = None
        self.result_factory: Callable[[CapturedStartRequest], FrozenCapturedRun] = (
            frozen_captured
        )
        self.hook = Hook()

    def freeze(self, request: CapturedStartRequest) -> FrozenCapturedRun:
        self.trace.append("freeze")
        self.calls.append(request)
        self.hook.wait()
        if self.exception is not None:
            raise self.exception
        return self.result_factory(request)


class FakeImportedFreezer:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[ImportedStartRequest] = []
        self.exception: Exception | None = None
        self.result_factory: Callable[[ImportedStartRequest], FrozenImportedRun] = (
            frozen_imported
        )
        self.hook = Hook()

    def freeze(self, request: ImportedStartRequest) -> FrozenImportedRun:
        self.trace.append("freeze")
        self.calls.append(request)
        self.hook.wait()
        if self.exception is not None:
            raise self.exception
        return self.result_factory(request)


class FakeResident:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.reserve_calls: list[tuple[str, str]] = []
        self.register_calls: list[CaptureReservation] = []
        self.start_calls: list[tuple[CaptureReservation, int, int]] = []
        self.cancel_calls: list[CaptureReservation] = []
        self.reserve_results: deque[CaptureReserveResult] = deque()
        self.reserve_exception: Exception | None = None
        self.register_exception: Exception | None = None
        self.start_exception: Exception | None = None
        self.cancel_exception: Exception | None = None
        self.cancel_result: object | None = None
        self.synchronous_outcome: Callable[
            [CaptureReservation], CaptureOutcome
        ] | None = None
        self._sinks: dict[str, Callable[[CaptureOutcome], None]] = {}
        self.hooks = {
            "reserve": Hook(),
            "register": Hook(),
            "start": Hook(),
            "cancel": Hook(),
        }

    def reserve(self, run_id: str, hardware_id: str) -> CaptureReserveResult:
        self.trace.append("reserve")
        self.reserve_calls.append((run_id, hardware_id))
        self.hooks["reserve"].wait()
        if self.reserve_exception is not None:
            raise self.reserve_exception
        if self.reserve_results:
            return self.reserve_results.popleft()
        reservation = CaptureReservation(
            reservation_id=f"reservation-{run_id}",
            run_id=run_id,
            hardware_id=hardware_id,
        )
        return CaptureReserveResult(
            status=CaptureReserveStatus.RESERVED,
            run_id=run_id,
            hardware_id=hardware_id,
            reservation=reservation,
            reason="",
        )

    def register_completion(
        self,
        reservation: CaptureReservation,
        sink: Callable[[CaptureOutcome], None],
    ) -> None:
        self.trace.append("register")
        self.register_calls.append(reservation)
        self.hooks["register"].wait()
        if self.register_exception is not None:
            raise self.register_exception
        self._sinks[reservation.reservation_id] = sink

    def start(
        self,
        reservation: CaptureReservation,
        trigger_monotonic_ns: int,
        target_frame_count: int,
    ) -> None:
        self.trace.append("start")
        self.start_calls.append(
            (reservation, trigger_monotonic_ns, target_frame_count)
        )
        self.hooks["start"].wait()
        if self.synchronous_outcome is not None:
            self.trace.append("resident_permit_released")
            self._sinks[reservation.reservation_id](
                self.synchronous_outcome(reservation)
            )
        if self.start_exception is not None:
            raise self.start_exception

    def cancel(
        self, reservation: CaptureReservation
    ) -> ResidentCancelResult:
        self.trace.append("resident_cancel")
        self.cancel_calls.append(reservation)
        self.hooks["cancel"].wait()
        if self.cancel_exception is not None:
            raise self.cancel_exception
        if self.cancel_result is not None:
            return self.cancel_result
        return ResidentCancelResult(
            status=ResidentCancelStatus.CANCELLED,
            run_id=reservation.run_id,
            reservation_id=reservation.reservation_id,
            reason="",
        )

    def complete(self, outcome: CaptureOutcome) -> None:
        self.trace.append("resident_permit_released")
        self._sinks[outcome.reservation.reservation_id](outcome)


class FakePersistence:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.active_calls: list[FrozenCapturedRun | FrozenImportedRun] = []
        self.terminal_calls: list[TerminalFact] = []
        self.active_results: deque[CommitResult] = deque()
        self.terminal_results: deque[CommitResult] = deque()
        self.active_exception: Exception | None = None
        self.terminal_exception: Exception | None = None
        self.hooks = {
            "active": Hook(),
            "terminal": Hook(),
            "facts": Hook(),
            "manual": Hook(),
        }
        self.facts_calls: list[str] = []
        self.facts_by_run: dict[str, object] = {}
        self.facts_exception: Exception | None = None
        self.manual_calls: list[tuple[object, object]] = []
        self.manual_results: deque[object] = deque()
        self.manual_exception: Exception | None = None
        self.current_calls: list[tuple[str, CurrentTrace]] = []
        self.current_results: deque[CommitResult] = deque()
        self.current_exception: Exception | None = None

    def create_active(
        self, frozen_run: FrozenCapturedRun | FrozenImportedRun
    ) -> CommitResult:
        self.trace.append("create_active")
        self.active_calls.append(frozen_run)
        self.hooks["active"].wait()
        if self.active_exception is not None:
            raise self.active_exception
        return (
            self.active_results.popleft()
            if self.active_results
            else CommitResult.COMMITTED
        )

    def commit_initial_terminal(self, terminal_fact: TerminalFact) -> CommitResult:
        self.trace.append("commit_terminal")
        self.terminal_calls.append(terminal_fact)
        self.hooks["terminal"].wait()
        if self.terminal_exception is not None:
            raise self.terminal_exception
        return (
            self.terminal_results.popleft()
            if self.terminal_results
            else CommitResult.COMMITTED
        )

    def apply_manual_judgment(self, request, facts):
        self.trace.append("apply_manual")
        self.manual_calls.append((request, facts))
        self.hooks["manual"].wait()
        if self.manual_exception is not None:
            raise self.manual_exception
        if self.manual_results:
            return self.manual_results.popleft()
        from app.service.multi_run_coordinator_mvp import (
            ManualProjectionResult,
            ManualProjectionStatus,
        )

        return ManualProjectionResult(
            status=ManualProjectionStatus.COMMITTED,
            run_id=request.run_id,
            replacement_projection=ResultProjection(
                phase=ResultProjectionPhase.TERMINAL,
                conclusion=request.conclusion,
                error_category=None,
                reason="",
                history_labels=facts.current_projection.history_labels,
                result_payload=facts.current_projection.result_payload,
                fault_type=request.fault_type,
                manual_judgment_revision=request.expected_revision + 1,
            ),
            committed_revision=request.expected_revision + 1,
            reason="",
        )

    def attach_current_trace(
        self,
        run_id: str,
        trace: CurrentTrace,
    ) -> CommitResult:
        self.trace.append("attach_current")
        self.current_calls.append((run_id, trace))
        if self.current_exception is not None:
            raise self.current_exception
        return (
            self.current_results.popleft()
            if self.current_results
            else CommitResult.COMMITTED
        )

    def load_result_control_facts(self, run_id: str):
        self.trace.append("load_result_facts")
        self.facts_calls.append(run_id)
        self.hooks["facts"].wait()
        if self.facts_exception is not None:
            raise self.facts_exception
        return self.facts_by_run.get(run_id)


class FakeHistoryProjector:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[HistoryProjectionFact] = []
        self.exception: Exception | None = None
        self.hooks = {kind: Hook() for kind in HistoryFactKind}

    def project(self, fact: HistoryProjectionFact) -> ProjectedHistoryEvent:
        self.trace.append(f"project:{fact.kind.name}")
        self.calls.append(fact)
        self.hooks[fact.kind].wait()
        if self.exception is not None:
            raise self.exception
        proposed_revision = 1 if fact.expected_revision is ABSENT else fact.expected_revision + 1
        return ProjectedHistoryEvent(
            kind=fact.kind,
            run_id=fact.run_id,
            expected_revision=fact.expected_revision,
            operation_id=fact.operation_id,
            proposed_revision=proposed_revision,
            projection=fact.projection,
        )


class FakeHistorySink:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[ProjectedHistoryEvent] = []
        self.exception: Exception | None = None
        self.hook = Hook()
        self.hooks = {kind: Hook() for kind in HistoryFactKind}

    def publish(self, event: ProjectedHistoryEvent) -> None:
        self.trace.append(f"history:{event.kind.name}")
        self.calls.append(event)
        self.hook.wait()
        self.hooks[event.kind].wait()
        if self.exception is not None:
            raise self.exception


class FakePlc:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.register_calls: list[dict[str, object]] = []
        self.mark_calls: list[PlcIngressTicket] = []
        self.publish_calls: list[tuple[PlcIngressTicket, object]] = []
        self.reject_calls: list[tuple[PlcIngressTicket, object]] = []
        self.registration_disposition = PlcMutationDisposition.APPLIED
        self.mark_result = PlcMutationDisposition.APPLIED
        self.publish_result = PlcMutationDisposition.APPLIED
        self.reject_result = PlcMutationDisposition.APPLIED
        self.registration_result: object | None = None
        self._next_sequence_by_plc: dict[int, int] = {}
        self.register_exception: Exception | None = None
        self.mark_exception: Exception | None = None
        self.publish_exception: Exception | None = None
        self.reject_exception: Exception | None = None
        self.hooks = {
            "register": Hook(),
            "mark": Hook(),
            "publish": Hook(),
            "reject": Hook(),
        }

    def register(
        self,
        *,
        run_id: str,
        trigger_source: str,
        operation_kind: PlcOperationKind,
        plc_id: int | None,
    ) -> PlcRegistrationResult:
        self.trace.append("plc_register")
        self.register_calls.append(
            {
                "run_id": run_id,
                "trigger_source": trigger_source,
                "operation_kind": operation_kind,
                "plc_id": plc_id,
            }
        )
        self.hooks["register"].wait()
        if self.register_exception is not None:
            raise self.register_exception
        if self.registration_result is not None:
            return self.registration_result
        ticket = None
        if self.registration_disposition is PlcMutationDisposition.APPLIED:
            assert type(plc_id) is int
            sequence = self._next_sequence_by_plc.get(plc_id, 1)
            self._next_sequence_by_plc[plc_id] = sequence + 1
            ticket = PlcIngressTicket(
                run_id=run_id,
                plc_id=plc_id,
                plc_sequence=sequence,
            )
        return PlcRegistrationResult(
            disposition=self.registration_disposition,
            ticket=ticket,
        )

    def mark_accepted(
        self, ticket: PlcIngressTicket
    ) -> PlcMutationDisposition:
        self.trace.append("plc_mark")
        self.mark_calls.append(ticket)
        self.hooks["mark"].wait()
        if self.mark_exception is not None:
            raise self.mark_exception
        return self.mark_result

    def publish_terminal(
        self, ticket: PlcIngressTicket, payload
    ) -> PlcMutationDisposition:
        self.trace.append("plc_publish")
        self.publish_calls.append((ticket, payload))
        self.hooks["publish"].wait()
        if self.publish_exception is not None:
            raise self.publish_exception
        return self.publish_result

    def publish_rejection(
        self, ticket: PlcIngressTicket, payload
    ) -> PlcMutationDisposition:
        self.trace.append("plc_reject")
        self.reject_calls.append((ticket, payload))
        self.hooks["reject"].wait()
        if self.reject_exception is not None:
            raise self.reject_exception
        return self.reject_result


class FakeSaveDispatcher:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[
            tuple[
                CapturedAudioSaveRequest | ImportedAudioSaveRequest,
                Callable[[AudioSaveOutcome], None],
            ]
        ] = []
        self.status = SaveSubmitStatus.ACCEPTED
        self.exception: Exception | None = None
        self.synchronous_outcome: Callable[
            [CapturedAudioSaveRequest | ImportedAudioSaveRequest],
            AudioSaveOutcome,
        ] | None = None
        self.hook = Hook()
        self.cancel_calls: list[tuple[str, str]] = []
        self.cancel_hook = Hook()
        self.cancel_exception: Exception | None = None
        self.cancel_result: object | None = None
        self.shutdown_calls: list[float] = []
        self.shutdown_exception: Exception | None = None

    def try_submit(self, request, sink) -> SaveSubmission:
        self.trace.append("save_submit")
        self.calls.append((request, sink))
        self.hook.wait()
        if self.synchronous_outcome is not None:
            sink(self.synchronous_outcome(request))
        if self.exception is not None:
            raise self.exception
        return SaveSubmission(
            status=self.status,
            run_id=request.run_id,
            save_job_id=request.save_job_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            reason="" if self.status is SaveSubmitStatus.ACCEPTED else self.status.name,
        )

    def cancel(self, run_id: str, save_job_id: str) -> SaveCancelResult:
        self.trace.append("save_cancel")
        self.cancel_calls.append((run_id, save_job_id))
        self.cancel_hook.wait()
        if self.cancel_exception is not None:
            raise self.cancel_exception
        if self.cancel_result is not None:
            return self.cancel_result
        return SaveCancelResult(
            status=SaveCancelStatus.CANCEL_REQUESTED,
            run_id=run_id,
            save_job_id=save_job_id,
            reason="",
        )

    def shutdown(self, deadline_monotonic: float) -> SaveDispatcherShutdownReport:
        self.shutdown_calls.append(deadline_monotonic)
        if self.shutdown_exception is not None:
            raise self.shutdown_exception
        return SaveDispatcherShutdownReport(
            status=SaveDispatcherShutdownStatus.CLOSED,
            accepted_count=len(self.calls),
            canceled_count=0,
            completed_count=len(self.calls),
            remaining_save_job_ids=(),
            live_worker_ids=(),
            errors=(),
        )


class FakeScheduler:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[
            tuple[AnalysisJob, Callable[[AnalysisOutcome], None]]
        ] = []
        self.before_audio_load_calls: list[AnalysisJob] = []
        self.status = SchedulerSubmitStatus.ACCEPTED
        self.exception: Exception | None = None
        self.synchronous_outcome: Callable[
            [AnalysisJob], AnalysisOutcome
        ] | None = None
        self.hook = Hook()
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.cancel_requests: list[BridgeCancelRequest] = []
        self.cancel_hook = Hook()
        self.cancel_result: object | None = None
        self.cancel_exception: Exception | None = None
        self.initial_calls: list[tuple[InitialAnalysisRequest, Callable]] = []
        self.reanalysis_calls: list[
            tuple[BridgeReanalysisRequest, Callable]
        ] = []
        self.bridge_results: deque[BridgeSubmissionResult] = deque()
        self.synchronous_report = None

    def submit_initial(
        self,
        request: InitialAnalysisRequest,
        terminal_sink,
    ) -> BridgeSubmissionResult:
        self.trace.append("bridge_submit_initial")
        self.initial_calls.append((request, terminal_sink))
        self.hook.wait()
        result = (
            self.bridge_results.popleft()
            if self.bridge_results
            else self.accepted_bridge_result(request.run_id)
        )
        if self.synchronous_report is not None:
            terminal_sink(self.synchronous_report)
        return result

    @staticmethod
    def accepted_bridge_result(
        run_id: str,
        attempt_id: str = ATTEMPT_IDS[0],
        *,
        early: bool = False,
    ) -> BridgeSubmissionResult:
        return BridgeSubmissionResult(
            status=(
                BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL
                if early
                else BridgeSubmissionStatus.ACCEPTED
            ),
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=attempt_id,
            scheduler_accepted=True,
            terminal_delivered_before_return=early,
            restart_required=False,
            code=None,
            reason=None,
            submit_receipt=SubmitReceipt(
                status=SubmitStatus.ACCEPTED,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                submission_sequence=1,
                error=None,
            ),
        )

    def complete_report(self, report) -> None:
        for request, sink in reversed(
            [*self.initial_calls, *self.reanalysis_calls]
        ):
            if request.run_id == report.run_id:
                sink(report)
                return
        raise AssertionError("missing bridge sink")

    def submit_reanalysis(
        self,
        request: BridgeReanalysisRequest,
        terminal_sink,
    ) -> BridgeSubmissionResult:
        self.trace.append("bridge_submit_reanalysis")
        self.reanalysis_calls.append((request, terminal_sink))
        self.hook.wait()
        result = (
            self.bridge_results.popleft()
            if self.bridge_results
            else self.accepted_bridge_result(request.run_id)
        )
        if self.synchronous_report is not None:
            terminal_sink(self.synchronous_report)
        return result

    def submit(self, job: AnalysisJob, sink) -> SchedulerSubmission:
        self.trace.append("scheduler_submit")
        self.calls.append((job, sink))
        self.hook.wait()
        if self.synchronous_outcome is not None:
            sink(self.synchronous_outcome(job))
        if self.exception is not None:
            raise self.exception
        return SchedulerSubmission(
            status=self.status,
            run_id=job.run_id,
            attempt_id=job.attempt_id,
            audio_id=job.audio.audio_id,
            audio_content_revision=job.audio.content_revision,
            reason="" if self.status is SchedulerSubmitStatus.ACCEPTED else self.status.name,
        )

    def before_audio_load(self, job: AnalysisJob) -> bool:
        self.before_audio_load_calls.append(job)
        raise AssertionError("Coordinator must never call before_audio_load")

    def cancel(self, request: BridgeCancelRequest) -> BridgeCancelResult:
        self.trace.append("scheduler_cancel")
        self.cancel_requests.append(request)
        self.cancel_calls.append(
            (
                request.run_id,
                request.analysis_attempt_id,
                request.reason,
            )
        )
        self.cancel_hook.wait()
        if self.cancel_exception is not None:
            raise self.cancel_exception
        if self.cancel_result is not None:
            return self.cancel_result
        receipt = CancelReceipt(
            status=CancelStatus.CANCELED_PENDING,
            run_id=request.run_id,
            analysis_attempt_id=request.analysis_attempt_id,
            drain_pending=False,
            error=None,
        )
        return BridgeCancelResult(
            status=BridgeCancelStatus.CANCEL_REQUESTED,
            run_id=request.run_id,
            analysis_attempt_id=request.analysis_attempt_id,
            origin=request.origin,
            code=None,
            reason=None,
            receipt=receipt,
        )


class FakeDiagnostics:
    def __init__(self) -> None:
        self.calls = []

    def record(self, fact) -> None:
        self.calls.append(fact)


class IdFactory:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.values = deque(values)
        self.hook = Hook()

    def __call__(self) -> str:
        self.hook.wait()
        return self.values.popleft()


class FakeClock:
    def __init__(self) -> None:
        self.hook = Hook()
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        self.hook.wait()
        return NOW


class FakeMonotonic:
    def __init__(self) -> None:
        self.values: deque[float] = deque()
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.values.popleft() if self.values else 0.0


class FakeCurrentCollector:
    def __init__(
        self,
        trace: list[str],
        *,
        samples: list[CurrentSample] | None = None,
    ) -> None:
        self.trace = trace
        self.start_calls = 0
        self.stop_calls: list[float | None] = []
        self.samples_calls = 0
        self.start_result = True
        self.start_exception: Exception | None = None
        self.stop_exception: Exception | None = None
        self.samples_exception: Exception | None = None
        self.sample_values = list(samples or [])
        self.hooks = {
            "start": Hook(),
            "stop": Hook(),
        }

    def start(self) -> bool:
        self.trace.append("current_start")
        self.start_calls += 1
        self.hooks["start"].wait()
        if self.start_exception is not None:
            raise self.start_exception
        return self.start_result

    def stop(self, timeout: float | None = None) -> None:
        self.trace.append("current_stop")
        self.stop_calls.append(timeout)
        self.hooks["stop"].wait()
        if self.stop_exception is not None:
            raise self.stop_exception

    def samples(self) -> list[CurrentSample]:
        self.trace.append("current_samples")
        self.samples_calls += 1
        if self.samples_exception is not None:
            raise self.samples_exception
        return list(self.sample_values)


class FakeCurrentCollectorFactory:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls: list[tuple[str, FrozenCapturedRun]] = []
        self.exception: Exception | None = None
        self.collectors: deque[FakeCurrentCollector | None] = deque()

    def __call__(
        self,
        run_id: str,
        frozen: FrozenCapturedRun,
    ) -> FakeCurrentCollector | None:
        self.trace.append("current_factory")
        self.calls.append((run_id, frozen))
        if self.exception is not None:
            raise self.exception
        if self.collectors:
            return self.collectors.popleft()
        return None


class Harness:
    def __init__(self) -> None:
        self.trace: list[str] = []
        self.captured_freezer = FakeCapturedFreezer(self.trace)
        self.imported_freezer = FakeImportedFreezer(self.trace)
        self.resident = FakeResident(self.trace)
        self.persistence = FakePersistence(self.trace)
        self.save_dispatcher = FakeSaveDispatcher(self.trace)
        self.scheduler = FakeScheduler(self.trace)
        self.projector = FakeHistoryProjector(self.trace)
        self.history = FakeHistorySink(self.trace)
        self.plc = FakePlc(self.trace)
        self.diagnostics = FakeDiagnostics()
        self.attempt_ids = IdFactory(ATTEMPT_IDS)
        self.save_job_ids = IdFactory(SAVE_JOB_IDS)
        self.clock = FakeClock()
        self.monotonic = FakeMonotonic()
        self.current_factory = FakeCurrentCollectorFactory(self.trace)

    def coordinator(self):
        from app.service.multi_run_coordinator_mvp import MultiRunCoordinator

        return MultiRunCoordinator(
            captured_freezer=self.captured_freezer,
            imported_freezer=self.imported_freezer,
            resident=self.resident,
            persistence=self.persistence,
            save_dispatcher=self.save_dispatcher,
            scheduler=self.scheduler,
            history_projector=self.projector,
            history_sink=self.history,
            plc=self.plc,
            diagnostics=self.diagnostics,
            current_collector_factory=self.current_factory,
            save_job_id_factory=self.save_job_ids,
            wall_clock=self.clock,
            monotonic_clock=self.monotonic,
        )


def completed_capture(reservation: CaptureReservation) -> CaptureOutcome:
    return CaptureOutcome(
        run_id=reservation.run_id,
        reservation=reservation,
        status=CaptureOutcomeStatus.COMPLETED,
        audio=captured_audio(),
        error_category=None,
        reason="",
    )
