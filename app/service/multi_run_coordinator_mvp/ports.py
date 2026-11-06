from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from app.service.current_meter_service import CurrentSample

from app.service.plc_result_sequencer import (
    PlcIngressTicket,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
    PlcScalar,
)
from app.service.run_persistence import (
    BridgeCancelRequest,
    BridgeCancelResult,
    BridgeSubmissionResult,
    CoordinatorAnalysisTerminalReportSink,
    InitialAnalysisRequest,
    CurrentTrace,
    ReanalysisRequest as BridgeReanalysisRequest,
)

from .contracts import (
    AudioSaveOutcome,
    AudioSaveRequest,
    CaptureOutcome,
    CaptureReservation,
    CaptureReserveClaim,
    CaptureReservePending,
    CaptureReserveResult,
    CapturedAudioSaveRequest,
    CapturedStartRequest,
    CommitResult,
    DiagnosticFact,
    FrozenCapturedRun,
    FrozenImportedRun,
    HistoryProjectionFact,
    ImportedAudioSaveRequest,
    ImportedStartRequest,
    ManualJudgmentRequest,
    ManualProjectionResult,
    ProjectedHistoryEvent,
    ResidentCancelResult,
    ResultControlFacts,
    SaveCancelResult,
    SaveCancelToken,
    SaveDispatcherShutdownReport,
    SaveSubmission,
    TerminalFact,
)


class CapturedRunFreezer(Protocol):
    def freeze(self, request: CapturedStartRequest) -> FrozenCapturedRun: ...


class ImportedRunFreezer(Protocol):
    def freeze(self, request: ImportedStartRequest) -> FrozenImportedRun: ...


class RunCurrentCollector(Protocol):
    def start(self) -> bool: ...

    def stop(self, timeout: float | None = None) -> None: ...

    def samples(self) -> list[CurrentSample]: ...


CurrentCollectorFactory = Callable[
    [str, FrozenCapturedRun],
    RunCurrentCollector | None,
]


class CaptureOutcomeSink(Protocol):
    def __call__(self, outcome: CaptureOutcome) -> None: ...


class ResidentCapturePort(Protocol):
    def reserve(self, run_id: str, hardware_id: str) -> CaptureReserveResult: ...

    def begin_reserve(
        self,
        run_id: str,
        hardware_id: str,
        trigger_monotonic_ns: int,
        deadline_monotonic_ns: int,
    ) -> CaptureReserveResult | CaptureReservePending: ...

    def resume_reserve(
        self,
        claim: CaptureReserveClaim,
    ) -> CaptureReserveResult | CaptureReservePending: ...

    def cancel_reserve(self, claim: CaptureReserveClaim) -> bool: ...

    def register_completion(
        self,
        reservation: CaptureReservation,
        sink: CaptureOutcomeSink,
    ) -> None: ...

    def start(
        self,
        reservation: CaptureReservation,
        trigger_monotonic_ns: int,
        target_frame_count: int,
    ) -> None: ...

    def cancel(
        self,
        reservation: CaptureReservation,
    ) -> ResidentCancelResult: ...


class MultiRunPersistencePort(Protocol):
    def create_active(
        self,
        frozen_run: FrozenCapturedRun | FrozenImportedRun,
    ) -> CommitResult: ...

    def commit_initial_terminal(
        self,
        terminal_fact: TerminalFact,
    ) -> CommitResult: ...

    def apply_manual_judgment(
        self,
        request: ManualJudgmentRequest,
        facts: ResultControlFacts,
    ) -> ManualProjectionResult: ...

    def load_result_control_facts(
        self,
        run_id: str,
    ) -> ResultControlFacts | None: ...

    def attach_current_trace(
        self,
        run_id: str,
        trace: CurrentTrace,
    ) -> CommitResult: ...


class CapturedAudioSaver(Protocol):
    def save_captured(
        self,
        request: CapturedAudioSaveRequest,
        cancel_token: SaveCancelToken,
    ) -> AudioSaveOutcome: ...


class ImportedAudioSaver(Protocol):
    def save_imported(
        self,
        request: ImportedAudioSaveRequest,
        cancel_token: SaveCancelToken,
    ) -> AudioSaveOutcome: ...


class SaveOutcomeSink(Protocol):
    def __call__(self, outcome: AudioSaveOutcome) -> None: ...


class AudioSaveDispatcherPort(Protocol):
    def try_submit(
        self,
        request: AudioSaveRequest,
        sink: SaveOutcomeSink,
    ) -> SaveSubmission: ...

    def cancel(
        self,
        run_id: str,
        save_job_id: str,
    ) -> SaveCancelResult: ...

    def shutdown(
        self,
        deadline_monotonic: float,
    ) -> SaveDispatcherShutdownReport: ...


class AnalysisBridgePort(Protocol):
    def submit_initial(
        self,
        request: InitialAnalysisRequest,
        terminal_sink: CoordinatorAnalysisTerminalReportSink,
    ) -> BridgeSubmissionResult: ...

    def submit_reanalysis(
        self,
        request: BridgeReanalysisRequest,
        terminal_sink: CoordinatorAnalysisTerminalReportSink,
    ) -> BridgeSubmissionResult: ...

    def cancel(self, request: BridgeCancelRequest) -> BridgeCancelResult: ...


class HistoryEventProjector(Protocol):
    def project(
        self,
        fact: HistoryProjectionFact,
    ) -> ProjectedHistoryEvent: ...


class ResultHistoryEventSink(Protocol):
    def publish(
        self,
        event: ProjectedHistoryEvent,
    ) -> None: ...


class PlcSequencerPort(Protocol):
    def register(
        self,
        *,
        run_id: str,
        trigger_source: str,
        operation_kind: PlcOperationKind,
        plc_id: int | None,
    ) -> PlcRegistrationResult: ...

    def mark_accepted(
        self,
        ticket: PlcIngressTicket,
    ) -> PlcMutationDisposition: ...

    def publish_terminal(
        self,
        ticket: PlcIngressTicket,
        payload: Mapping[str, PlcScalar],
    ) -> PlcMutationDisposition: ...

    def publish_rejection(
        self,
        ticket: PlcIngressTicket,
        payload: Mapping[str, PlcScalar],
    ) -> PlcMutationDisposition: ...


class DiagnosticSink(Protocol):
    def record(self, fact: DiagnosticFact) -> None: ...
