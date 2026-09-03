from __future__ import annotations

import dataclasses
import inspect
import itertools
import math
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import get_args, get_type_hints

import pytest
from app.service.plc_result_sequencer import (
    PlcIngressTicket as FrozenPlcIngressTicket,
    PlcMutationDisposition as FrozenPlcMutationDisposition,
    PlcOperationKind as FrozenPlcOperationKind,
    PlcRegistrationResult as FrozenPlcRegistrationResult,
    PlcScalar,
)

import app.service.multi_run_coordinator_mvp as package
import app.service.multi_run_coordinator_mvp.contracts as contracts_module
import app.service.multi_run_coordinator_mvp.ports as ports_module
from app.service.current_meter_service import CurrentSample
from app.service.multi_run_coordinator_mvp import (
    ABSENT,
    AnalysisBridgePort,
    AnalysisAttemptKind,
    AudioMetadataConstraints,
    AudioSaveDispatcherPort,
    AudioSaveOutcome,
    AudioSaveSourceKind,
    AudioSaveStatus,
    CapturedAudioSaveRequest,
    CapturedAudioSaver,
    CapturedAudioValue,
    CapturedRunFreezer,
    CapturedSourceFormat,
    CapturedStartRequest,
    CaptureOutcome,
    CaptureOutcomeSink,
    CaptureOutcomeStatus,
    CaptureReservation,
    CaptureReserveResult,
    CaptureReserveStatus,
    CommitResult,
    CoordinatorLifecycle,
    CoordinatorPhase,
    CoordinatorShutdownReport,
    CoordinatorShutdownStatus,
    CoordinatorSnapshot,
    DiagnosticFact,
    DiagnosticSink,
    FinalPathRequest,
    FrozenCapturedRun,
    FrozenImportedRun,
    FrozenPayload,
    HistoryEvictionNotice,
    HistoryEventProjector,
    HistoryFactKind,
    HistoryProjectionFact,
    ImportedAudioSaveRequest,
    ImportedAudioSaver,
    ImportedRunFreezer,
    ImportedSourceFormat,
    ImportedSourceReference,
    ImportedStartRequest,
    ManualJudgmentRequest,
    ManualProjectionResult,
    ManualProjectionStatus,
    MultiRunPersistencePort,
    OperationResult,
    OperationStatus,
    PersistedAudioFormat,
    PersistedAudioReference,
    PlcIngressTicket,
    PlcMutationDisposition,
    PlcOperationKind,
    PlcRegistrationResult,
    PlcSequencerPort,
    PreacceptTerminalFact,
    ProjectedHistoryEvent,
    ReanalysisCancelRequest,
    ReanalysisRequest,
    ResidentCancelResult,
    ResidentCancelStatus,
    ResidentCapturePort,
    ResultControlFacts,
    ResultEnrichmentProjection,
    ResultHistoryEventSink,
    ResultProjection,
    ResultProjectionPhase,
    SaveCancelResult,
    SaveCancelStatus,
    SaveCancelToken,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
    SaveOutcomeSink,
    SaveSubmission,
    SaveSubmitStatus,
    StartResult,
    StartStatus,
    StopReport,
    StopStatus,
    TerminalConclusion,
    TerminalErrorCategory,
    TerminalFact,
    TriggerSource,
)
from app.service.multi_run_coordinator_mvp.contracts import (
    AnalysisJob,
    AnalysisOutcome,
    AnalysisOutcomeStatus,
    PreparedAnalysisAttempt,
    SchedulerCancelResult,
    SchedulerCancelStatus,
    SchedulerSubmission,
    SchedulerSubmitStatus,
)


RUN_ID = "00000000-0000-4000-8000-000000000001"
OTHER_RUN_ID = "00000000-0000-4000-8000-000000000002"
ATTEMPT_ID = "00000000-0000-4000-8000-000000000003"
SAVE_JOB_ID = "00000000-0000-4000-8000-000000000004"
AUDIO_ID = "00000000-0000-4000-8000-000000000005"

PUBLIC_DATACLASS_NAMES = {
    "AudioMetadataConstraints",
    "AudioSaveOutcome",
    "CapturedAudioSaveRequest",
    "CapturedAudioValue",
    "CapturedStartRequest",
    "CaptureOutcome",
    "CaptureReservation",
    "CaptureReserveResult",
    "CoordinatorShutdownReport",
    "CoordinatorSnapshot",
    "DiagnosticFact",
    "ExternalAction",
    "FinalPathRequest",
    "FrozenCapturedRun",
    "FrozenImportedRun",
    "FrozenPayload",
    "HistoryEvictionNotice",
    "HistoryProjectionFact",
    "ImportedAudioSaveRequest",
    "ImportedSourceReference",
    "ImportedStartRequest",
    "ManualJudgmentRequest",
    "ManualProjectionResult",
    "OperationResult",
    "PendingTerminalContender",
    "PersistedAudioReference",
    "PreacceptTerminalFact",
    "ProjectedHistoryEvent",
    "ReanalysisCancelRequest",
    "ReanalysisRequest",
    "ResidentCancelResult",
    "ResultControlFacts",
    "ResultEnrichmentProjection",
    "ResultProjection",
    "SaveCancelResult",
    "SaveDispatcherShutdownReport",
    "SaveSubmission",
    "StartResult",
    "StopReport",
    "TerminalFact",
}

ENUM_MEMBERS = {
    "AnalysisAttemptKind": {
        "INITIAL_CAPTURED",
        "INITIAL_IMPORTED",
        "REANALYSIS",
    },
    "AudioSaveSourceKind": {"CAPTURED_AUDIO", "IMPORTED_SOURCE"},
    "AudioSaveStatus": {"SAVED", "CANCELLED", "FAILED", "PATH_EXISTS"},
    "CapturedSourceFormat": {"FLOAT32_INTERLEAVED", "INT16_INTERLEAVED"},
    "CaptureOutcomeStatus": {"COMPLETED", "CANCELLED", "FAILED"},
    "CaptureReserveStatus": {
        "RESERVED",
        "CAPACITY",
        "NOT_READY",
        "FAULT",
        "CLOSING",
    },
    "CommitResult": {"COMMITTED", "REJECTED", "UNAVAILABLE"},
    "CoordinatorLifecycle": {"OPEN", "STOPPING", "CLOSED"},
    "CoordinatorPhase": {
        "PREFLIGHT",
        "RESERVING",
        "ACTIVE_COMMITTING",
        "REGISTERING",
        "STARTING",
        "RECORDING",
        "SAVE_SUBMITTING",
        "SAVING",
        "ATTEMPT_COMMITTING",
        "ANALYSIS_SUBMITTING",
        "ANALYSIS_PENDING",
        "TERMINAL_COMMITTING",
        "PUBLISHING",
    },
    "CoordinatorShutdownStatus": {"CLOSED", "INCOMPLETE"},
    "ExternalActionKind": {
        "PLC_REGISTER",
        "FREEZE",
        "RESIDENT_RESERVE",
        "ACTIVE_COMMIT",
        "PLC_MARK",
        "CAPTURE_REGISTER",
        "ACTIVE_HISTORY",
        "CAPTURE_START",
        "SAVE_SUBMIT",
        "ATTEMPT_COMMIT",
        "WAITING_HISTORY",
        "SCHEDULER_SUBMIT",
        "ANALYZING_HISTORY",
        "TERMINAL_COMMIT",
        "RESULT_FACTS_LOAD",
        "MANUAL_COMMIT",
        "REANALYSIS_CANCEL",
        "CHILD_CANCEL",
        "TERMINAL_HISTORY",
        "PLC_SETTLEMENT",
    },
    "HistoryFactKind": {
        "RECORDING",
        "WAITING_SAVE",
        "WAITING_ANALYSIS",
        "ANALYZING",
        "RUN_TERMINATED",
        "REANALYSIS_STARTED",
        "REANALYSIS_COMPLETED",
        "REANALYSIS_CANCELLED",
        "TERMINAL_RESULT_REPLACED",
    },
    "ImportedSourceFormat": {"PCM16_WAV"},
    "ManualProjectionStatus": {"COMMITTED", "REJECTED", "UNAVAILABLE"},
    "OperationStatus": {
        "COMPLETED",
        "SUBMITTED",
        "INVALID_REQUEST",
        "INGRESS_CLOSED",
        "NOT_FOUND",
        "NOT_ALLOWED",
        "OPERATION_BUSY",
        "STALE_REVISION",
        "QUEUE_FULL",
        "PERSISTENCE_REJECTED",
        "SCHEDULER_REJECTED",
        "EVICTED",
    },
    "PersistedAudioFormat": {"PCM16_WAV"},
    "ResidentCancelStatus": {"CANCELLED", "ALREADY_TERMINAL", "INVALID"},
    "ResultOperationPhase": {
        "LOADING_FACTS",
        "MANUAL_COMMITTING",
        "ATTEMPT_QUEUE_COMMITTING",
        "SCHEDULER_SUBMITTING",
        "ANALYSIS_PENDING",
        "CANCEL_COMMITTING",
        "TERMINAL_COMMITTING",
    },
    "ResultProjectionPhase": {
        "RECORDING",
        "WAITING_SAVE",
        "WAITING_ANALYSIS",
        "ANALYZING",
        "REANALYZING",
        "TERMINAL",
    },
    "SaveCancelStatus": {
        "CANCEL_REQUESTED",
        "ALREADY_TERMINAL",
        "NOT_FOUND",
    },
    "SaveDispatcherShutdownStatus": {"CLOSED", "INCOMPLETE"},
    "SaveSubmitStatus": {"ACCEPTED", "SATURATED", "CLOSED", "INVALID"},
    "StartStatus": {
        "ACCEPTED_ACTIVE",
        "ACCEPTED_TERMINAL",
        "INVALID_REQUEST",
        "DUPLICATE_RUN_ID",
        "INGRESS_CLOSED",
        "PLC_INGRESS_REJECTED",
        "PREFLIGHT_REJECTED",
        "INVALID_FROZEN_INPUT",
        "CAPTURE_CAPACITY",
        "RESIDENT_UNAVAILABLE",
        "ACTIVE_COMMIT_REJECTED",
    },
    "StopStatus": {"SETTLED", "IN_PROGRESS", "INCOMPLETE", "CLOSED"},
    "TerminalConclusion": {
        "OK",
        "NG",
        "NO_RESULT",
        "ERROR",
        "CAPACITY_REJECTED",
        "MANUAL_STOPPED",
    },
    "TerminalContenderKind": {
        "CAPTURE_OUTCOME",
        "SAVE_OUTCOME",
        "ANALYSIS_OUTCOME",
        "STOP",
        "SHUTDOWN",
        "EXTERNAL_FAILURE",
    },
    "TerminalErrorCategory": {
        "INVALID_REQUEST",
        "INVALID_FROZEN_INPUT",
        "PREFLIGHT_REJECTED",
        "PLC_INGRESS_REJECTED",
        "CAPTURE_CAPACITY",
        "RESIDENT_UNAVAILABLE",
        "ACTIVE_COMMIT_FAILED",
        "PLC_ACTIVATION_FAILED",
        "CAPTURE_REGISTRATION_FAILED",
        "CAPTURE_START_FAILED",
        "CAPTURE_FAILED",
        "CAPTURE_CANCELLED",
        "CURRENT_TRACE_INVALID",
        "CURRENT_TRACE_PERSIST_FAILED",
        "AUDIO_SAVE_SATURATED",
        "AUDIO_SAVE_FAILED",
        "AUDIO_SAVE_CANCELLED",
        "AUDIO_PATH_EXISTS",
        "ATTEMPT_ID_FAILED",
        "ATTEMPT_QUEUE_COMMIT_FAILED",
        "ATTEMPT_RUNNING_COMMIT_FAILED",
        "ANALYSIS_QUEUE_FULL",
        "ANALYSIS_NOT_ACCEPTING",
        "ANALYSIS_INVALID",
        "ANALYSIS_FAILED",
        "ANALYSIS_CANCELLED",
        "MANUAL_STOPPED",
        "SHUTDOWN_CANCELLED",
        "PERSISTENCE_TERMINAL_FAILED",
    },
    "TriggerSource": {"PLC", "MANUAL", "SCANNER", "IMPORT"},
}

PROTOCOL_NAMES = {
    "AnalysisBridgePort",
    "AudioSaveDispatcherPort",
    "CapturedAudioSaver",
    "CapturedRunFreezer",
    "CaptureOutcomeSink",
    "DiagnosticSink",
    "HistoryEventProjector",
    "ImportedAudioSaver",
    "ImportedRunFreezer",
    "MultiRunPersistencePort",
    "PlcSequencerPort",
    "ResidentCapturePort",
    "ResultHistoryEventSink",
    "SaveOutcomeSink",
}

TASK1_EXPORT_NAMES = (
    PUBLIC_DATACLASS_NAMES
    | set(ENUM_MEMBERS)
    | PROTOCOL_NAMES
    | {
        "ABSENT",
        "AudioSaveRequest",
        "SaveCancelToken",
        "PlcIngressTicket",
        "PlcMutationDisposition",
        "PlcOperationKind",
        "PlcRegistrationResult",
    }
)

PREACCEPT_ERROR_CATEGORIES = {
    TerminalErrorCategory.INVALID_FROZEN_INPUT,
    TerminalErrorCategory.PREFLIGHT_REJECTED,
    TerminalErrorCategory.PLC_INGRESS_REJECTED,
    TerminalErrorCategory.RESIDENT_UNAVAILABLE,
    TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
}
PREACCEPT_ONLY_CATEGORIES = PREACCEPT_ERROR_CATEGORIES | {
    TerminalErrorCategory.CAPTURE_CAPACITY
}
CAPTURE_POSTACCEPT_CATEGORIES = {
    TerminalErrorCategory.CAPTURE_REGISTRATION_FAILED,
    TerminalErrorCategory.CAPTURE_START_FAILED,
    TerminalErrorCategory.CAPTURE_FAILED,
    TerminalErrorCategory.CAPTURE_CANCELLED,
}
SAVE_ERROR_CATEGORIES = {
    TerminalErrorCategory.AUDIO_SAVE_SATURATED,
    TerminalErrorCategory.AUDIO_SAVE_FAILED,
    TerminalErrorCategory.AUDIO_SAVE_CANCELLED,
    TerminalErrorCategory.AUDIO_PATH_EXISTS,
}
ATTEMPT_AND_ANALYSIS_ERROR_CATEGORIES = {
    TerminalErrorCategory.ATTEMPT_ID_FAILED,
    TerminalErrorCategory.ATTEMPT_QUEUE_COMMIT_FAILED,
    TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
    TerminalErrorCategory.ANALYSIS_QUEUE_FULL,
    TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING,
    TerminalErrorCategory.ANALYSIS_INVALID,
    TerminalErrorCategory.ANALYSIS_FAILED,
    TerminalErrorCategory.ANALYSIS_CANCELLED,
}
ATTEMPT_ID_REQUIRED_ERROR_CATEGORIES = {
    TerminalErrorCategory.ATTEMPT_QUEUE_COMMIT_FAILED,
    TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
    TerminalErrorCategory.ANALYSIS_QUEUE_FULL,
    TerminalErrorCategory.ANALYSIS_NOT_ACCEPTING,
    TerminalErrorCategory.ANALYSIS_INVALID,
    TerminalErrorCategory.ANALYSIS_FAILED,
    TerminalErrorCategory.ANALYSIS_CANCELLED,
}
REANALYSIS_ERROR_CATEGORIES = {
    TerminalErrorCategory.ATTEMPT_RUNNING_COMMIT_FAILED,
    TerminalErrorCategory.ANALYSIS_INVALID,
    TerminalErrorCategory.ANALYSIS_FAILED,
    TerminalErrorCategory.ANALYSIS_CANCELLED,
}
PREACCEPT_STATUS_FACTS = {
    StartStatus.PLC_INGRESS_REJECTED: (
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PLC_INGRESS_REJECTED,
        TriggerSource.PLC,
    ),
    StartStatus.PREFLIGHT_REJECTED: (
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PREFLIGHT_REJECTED,
        TriggerSource.MANUAL,
    ),
    StartStatus.INVALID_FROZEN_INPUT: (
        TerminalConclusion.ERROR,
        TerminalErrorCategory.INVALID_FROZEN_INPUT,
        TriggerSource.MANUAL,
    ),
    StartStatus.CAPTURE_CAPACITY: (
        TerminalConclusion.CAPACITY_REJECTED,
        TerminalErrorCategory.CAPTURE_CAPACITY,
        TriggerSource.MANUAL,
    ),
    StartStatus.RESIDENT_UNAVAILABLE: (
        TerminalConclusion.ERROR,
        TerminalErrorCategory.RESIDENT_UNAVAILABLE,
        TriggerSource.MANUAL,
    ),
    StartStatus.ACTIVE_COMMIT_REJECTED: (
        TerminalConclusion.ERROR,
        TerminalErrorCategory.ACTIVE_COMMIT_FAILED,
        TriggerSource.MANUAL,
    ),
}
INITIAL_IMPORTED_ERROR_CATEGORIES = (
    SAVE_ERROR_CATEGORIES
    | ATTEMPT_AND_ANALYSIS_ERROR_CATEGORIES
    | {TerminalErrorCategory.SHUTDOWN_CANCELLED}
)
INITIAL_CAPTURED_NON_PLC_ERROR_CATEGORIES = (
    CAPTURE_POSTACCEPT_CATEGORIES
    | SAVE_ERROR_CATEGORIES
    | ATTEMPT_AND_ANALYSIS_ERROR_CATEGORIES
    | {TerminalErrorCategory.SHUTDOWN_CANCELLED}
)
INITIAL_CAPTURED_PLC_ONLY_ERROR_CATEGORIES = {
    TerminalErrorCategory.PLC_ACTIVATION_FAILED,
    TerminalErrorCategory.PERSISTENCE_TERMINAL_FAILED,
}


def frozen(name: str) -> FrozenPayload:
    return FrozenPayload(value_id=name, canonical_bytes=name.encode("utf-8"))


def metadata() -> AudioMetadataConstraints:
    return AudioMetadataConstraints(
        expected_sample_rate_hz=8_000,
        expected_channel_count=1,
        expected_frame_count=2,
        format=PersistedAudioFormat.PCM16_WAV,
    )


def captured_audio() -> CapturedAudioValue:
    return CapturedAudioValue(
        source_identity="resident:1",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=2,
        format=CapturedSourceFormat.INT16_INTERLEAVED,
        interleaved_bytes=b"\x00\x00\x01\x00",
        channel_indices=(0,),
    )


def imported_source() -> ImportedSourceReference:
    return ImportedSourceReference(
        source_identity="import:1",
        absolute_path="C:\\input\\source.wav",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=2,
        format=ImportedSourceFormat.PCM16_WAV,
    )


def persisted_audio(
    *,
    source_kind: AudioSaveSourceKind = AudioSaveSourceKind.CAPTURED_AUDIO,
    source_identity: str = "resident:1",
) -> PersistedAudioReference:
    return PersistedAudioReference(
        audio_id=AUDIO_ID,
        save_job_id=SAVE_JOB_ID,
        run_id=RUN_ID,
        source_kind=source_kind,
        source_identity=source_identity,
        final_path="C:\\final\\audio.wav",
        sample_rate_hz=8_000,
        channel_count=1,
        frame_count=2,
        byte_size=4,
        format=PersistedAudioFormat.PCM16_WAV,
        content_revision=1,
    )


def terminal_projection(
    conclusion: TerminalConclusion = TerminalConclusion.NO_RESULT,
) -> ResultProjection:
    return ResultProjection(
        phase=ResultProjectionPhase.TERMINAL,
        conclusion=conclusion,
        error_category=None,
        reason="",
        history_labels=frozen("labels"),
        result_payload=None,
    )


def captured_request(**changes: object) -> CapturedStartRequest:
    values = {
        "run_id": RUN_ID,
        "trigger_monotonic_ns": 1,
        "source": TriggerSource.MANUAL,
        "ingress_payload": frozen("ingress"),
        "fallback_history_labels": frozen("labels"),
        "plc_id": None,
    }
    values.update(changes)
    return CapturedStartRequest(**values)


def valid_plc_ticket() -> PlcIngressTicket:
    return PlcIngressTicket(RUN_ID, 1, 1)


def valid_capture_reservation() -> CaptureReservation:
    return CaptureReservation("reservation:1", RUN_ID, "hardware:1")


def valid_terminal_fact() -> TerminalFact:
    return TerminalFact(
        RUN_ID,
        TriggerSource.MANUAL,
        AnalysisAttemptKind.INITIAL_CAPTURED,
        ATTEMPT_ID,
        TerminalConclusion.NO_RESULT,
        None,
        "",
        None,
        frozen("labels"),
        datetime.now(timezone.utc),
    )


def valid_preaccept_terminal_fact() -> PreacceptTerminalFact:
    return PreacceptTerminalFact(
        RUN_ID,
        TriggerSource.MANUAL,
        None,
        TerminalConclusion.ERROR,
        TerminalErrorCategory.PREFLIGHT_REJECTED,
        "failed",
        frozen("labels"),
        datetime.now(timezone.utc),
    )


_DEFAULT_ATTEMPT = object()


def terminal_error_fact(
    category,
    *,
    attempt_kind=AnalysisAttemptKind.INITIAL_CAPTURED,
    source=TriggerSource.MANUAL,
    attempt_id=_DEFAULT_ATTEMPT,
) -> TerminalFact:
    if attempt_id is _DEFAULT_ATTEMPT:
        attempt_id = (
            ATTEMPT_ID
            if (
                attempt_kind is AnalysisAttemptKind.REANALYSIS
                or category in ATTEMPT_ID_REQUIRED_ERROR_CATEGORIES
            )
            else None
        )
    return TerminalFact(
        RUN_ID,
        source,
        attempt_kind,
        attempt_id,
        TerminalConclusion.ERROR,
        category,
        "failed",
        None,
        frozen("labels"),
        datetime.now(timezone.utc),
    )


def completed_terminal_fact(
    conclusion,
    *,
    attempt_kind=AnalysisAttemptKind.INITIAL_CAPTURED,
    source=TriggerSource.MANUAL,
    attempt_id=ATTEMPT_ID,
) -> TerminalFact:
    return TerminalFact(
        RUN_ID,
        source,
        attempt_kind,
        attempt_id,
        conclusion,
        None,
        "",
        frozen("result")
        if conclusion in {TerminalConclusion.OK, TerminalConclusion.NG}
        else None,
        frozen("labels"),
        datetime.now(timezone.utc),
    )


def preaccept_error_fact(
    category,
    *,
    source=TriggerSource.MANUAL,
) -> PreacceptTerminalFact:
    return PreacceptTerminalFact(
        RUN_ID,
        source,
        None,
        TerminalConclusion.ERROR,
        category,
        "failed",
        frozen("labels"),
        datetime.now(timezone.utc),
    )


def preaccept_capacity_fact(
    *,
    source=TriggerSource.MANUAL,
) -> PreacceptTerminalFact:
    return PreacceptTerminalFact(
        RUN_ID,
        source,
        None,
        TerminalConclusion.CAPACITY_REJECTED,
        TerminalErrorCategory.CAPTURE_CAPACITY,
        "full",
        frozen("labels"),
        datetime.now(timezone.utc),
    )


def valid_frozen_captured_run() -> FrozenCapturedRun:
    return FrozenCapturedRun(
        RUN_ID,
        1,
        TriggerSource.MANUAL,
        "config:1",
        "hardware:1",
        2,
        frozen("config"),
        frozen("hardware"),
        frozen("calibration"),
        frozen("work-order"),
        frozen("labels"),
        frozen("analysis"),
        FinalPathRequest("C:\\final\\audio.wav"),
        metadata(),
    )


def valid_frozen_imported_run() -> FrozenImportedRun:
    return FrozenImportedRun(
        RUN_ID,
        1,
        TriggerSource.IMPORT,
        "config:1",
        imported_source(),
        frozen("config"),
        frozen("calibration"),
        frozen("work-order"),
        frozen("labels"),
        frozen("analysis"),
        FinalPathRequest("C:\\final\\audio.wav"),
        metadata(),
    )


def valid_captured_save_request() -> CapturedAudioSaveRequest:
    return CapturedAudioSaveRequest(
        SAVE_JOB_ID,
        RUN_ID,
        AudioSaveSourceKind.CAPTURED_AUDIO,
        "resident:1",
        captured_audio(),
        FinalPathRequest("C:\\final\\audio.wav"),
        metadata(),
    )


def valid_imported_save_request() -> ImportedAudioSaveRequest:
    return ImportedAudioSaveRequest(
        SAVE_JOB_ID,
        RUN_ID,
        AudioSaveSourceKind.IMPORTED_SOURCE,
        "import:1",
        imported_source(),
        FinalPathRequest("C:\\final\\audio.wav"),
        metadata(),
    )


def valid_closed_dispatcher_report() -> SaveDispatcherShutdownReport:
    return SaveDispatcherShutdownReport(
        SaveDispatcherShutdownStatus.CLOSED,
        0,
        0,
        0,
        [],
        [],
        [],
    )


def valid_result_control_facts() -> ResultControlFacts:
    return ResultControlFacts(
        RUN_ID,
        frozen("config"),
        frozen("calibration"),
        frozen("analysis"),
        persisted_audio(),
        True,
        True,
        terminal_projection(),
        1,
    )


CONSTRUCTOR_CASES = {
    "AnalysisJob": (
        ("attempt_id", "run_id", "attempt_kind", "audio", "analysis_input"),
        lambda: AnalysisJob(
            ATTEMPT_ID,
            RUN_ID,
            AnalysisAttemptKind.INITIAL_CAPTURED,
            persisted_audio(),
            frozen("analysis"),
        ),
        (
            lambda: AnalysisJob(
                ATTEMPT_ID,
                OTHER_RUN_ID,
                AnalysisAttemptKind.INITIAL_CAPTURED,
                persisted_audio(),
                frozen("analysis"),
            ),
        ),
    ),
    "AnalysisOutcome": (
        (
            "attempt_id",
            "run_id",
            "attempt_kind",
            "audio_id",
            "audio_content_revision",
            "status",
            "conclusion",
            "result_payload",
            "error_category",
            "reason",
        ),
        lambda: AnalysisOutcome(
            ATTEMPT_ID,
            RUN_ID,
            AnalysisAttemptKind.INITIAL_CAPTURED,
            AUDIO_ID,
            1,
            AnalysisOutcomeStatus.COMPLETED,
            TerminalConclusion.NO_RESULT,
            None,
            None,
            "",
        ),
        (
            lambda: AnalysisOutcome(
                ATTEMPT_ID,
                RUN_ID,
                AnalysisAttemptKind.INITIAL_CAPTURED,
                AUDIO_ID,
                1,
                AnalysisOutcomeStatus.FAILED,
                TerminalConclusion.ERROR,
                None,
                TerminalErrorCategory.ANALYSIS_CANCELLED,
                "failed",
            ),
        ),
    ),
    "AudioMetadataConstraints": (
        (
            "expected_sample_rate_hz",
            "expected_channel_count",
            "expected_frame_count",
            "format",
        ),
        metadata,
        (lambda: dataclasses.replace(metadata(), expected_frame_count=0),),
    ),
    "AudioSaveOutcome": (
        (
            "save_job_id",
            "run_id",
            "source_kind",
            "source_identity",
            "status",
            "persisted_audio",
            "error_category",
            "reason",
        ),
        lambda: AudioSaveOutcome(
            SAVE_JOB_ID,
            RUN_ID,
            AudioSaveSourceKind.CAPTURED_AUDIO,
            "resident:1",
            AudioSaveStatus.SAVED,
            persisted_audio(),
            None,
            "",
        ),
        (
            lambda: AudioSaveOutcome(
                SAVE_JOB_ID,
                RUN_ID,
                AudioSaveSourceKind.CAPTURED_AUDIO,
                "resident:other",
                AudioSaveStatus.SAVED,
                persisted_audio(),
                None,
                "",
            ),
        ),
    ),
    "CapturedAudioSaveRequest": (
        (
            "save_job_id",
            "run_id",
            "source_kind",
            "source_identity",
            "audio",
            "final_path_request",
            "metadata",
        ),
        valid_captured_save_request,
        (
            lambda: dataclasses.replace(
                valid_captured_save_request(), source_identity="resident:other"
            ),
        ),
    ),
    "CapturedAudioValue": (
        (
            "source_identity",
            "sample_rate_hz",
            "channel_count",
            "frame_count",
            "format",
            "interleaved_bytes",
            "channel_indices",
        ),
        captured_audio,
        (
            lambda: dataclasses.replace(
                captured_audio(), interleaved_bytes=b"\x00"
            ),
        ),
    ),
    "CapturedStartRequest": (
        (
            "run_id",
            "trigger_monotonic_ns",
            "source",
            "ingress_payload",
            "fallback_history_labels",
            "plc_id",
        ),
        captured_request,
        (
            lambda: captured_request(source=TriggerSource.PLC, plc_id=None),
        ),
    ),
    "CaptureOutcome": (
        (
            "run_id",
            "reservation",
            "status",
            "audio",
            "error_category",
            "reason",
        ),
        lambda: CaptureOutcome(
            RUN_ID,
            valid_capture_reservation(),
            CaptureOutcomeStatus.COMPLETED,
            captured_audio(),
            None,
            "",
        ),
        (
            lambda: CaptureOutcome(
                RUN_ID,
                valid_capture_reservation(),
                CaptureOutcomeStatus.COMPLETED,
                None,
                None,
                "",
            ),
        ),
    ),
    "CaptureReservation": (
        ("reservation_id", "run_id", "hardware_id"),
        valid_capture_reservation,
        (
            lambda: CaptureReservation(
                "reservation:1", "not-a-uuid", "hardware:1"
            ),
        ),
    ),
    "CaptureReserveResult": (
        ("status", "run_id", "hardware_id", "reservation", "reason"),
        lambda: CaptureReserveResult(
            CaptureReserveStatus.RESERVED,
            RUN_ID,
            "hardware:1",
            valid_capture_reservation(),
            "",
        ),
        (
            lambda: CaptureReserveResult(
                CaptureReserveStatus.RESERVED,
                RUN_ID,
                "hardware:1",
                None,
                "",
            ),
        ),
    ),
    "CoordinatorShutdownReport": (
        (
            "status",
            "remaining_ingress_run_ids",
            "remaining_run_ids",
            "remaining_attempt_ids",
            "remaining_save_job_ids",
            "pending_publication_run_ids",
            "save_dispatcher_report",
            "errors",
            "restart_required",
        ),
        lambda: CoordinatorShutdownReport(
            CoordinatorShutdownStatus.CLOSED,
            [],
            [],
            [],
            [],
            [],
            valid_closed_dispatcher_report(),
            [],
        ),
        (
            lambda: CoordinatorShutdownReport(
                CoordinatorShutdownStatus.INCOMPLETE,
                [],
                [],
                [],
                [],
                [],
                valid_closed_dispatcher_report(),
                [],
            ),
        ),
    ),
    "CoordinatorSnapshot": (
        (
            "lifecycle",
            "ingress_run_ids",
            "accepted_active_run_ids",
            "analysis_attempt_ids",
            "result_operation_attempt_ids",
            "pending_publication_run_ids",
            "restart_required",
        ),
        lambda: CoordinatorSnapshot(
            CoordinatorLifecycle.OPEN, [], [], [], [], []
        ),
        (
            lambda: CoordinatorSnapshot("OPEN", [], [], [], [], []),
        ),
    ),
    "DiagnosticFact": (
        (
            "stage",
            "run_id",
            "attempt_id",
            "save_job_id",
            "category",
            "message",
        ),
        lambda: DiagnosticFact(
            "save",
            RUN_ID,
            ATTEMPT_ID,
            SAVE_JOB_ID,
            TerminalErrorCategory.AUDIO_SAVE_FAILED,
            "failed",
        ),
        (
            lambda: DiagnosticFact(
                "save",
                RUN_ID,
                ATTEMPT_ID,
                SAVE_JOB_ID,
                TerminalErrorCategory.AUDIO_SAVE_FAILED,
                "bad\x00message",
            ),
        ),
    ),
    "ExternalAction": (
        (
            "kind",
            "run_id",
            "expected_phase",
            "attempt_id",
            "save_job_id",
            "reservation_id",
        ),
        lambda: package.ExternalAction(
            package.ExternalActionKind.SAVE_SUBMIT,
            RUN_ID,
            CoordinatorPhase.SAVE_SUBMITTING,
            None,
            SAVE_JOB_ID,
            None,
        ),
        (
            lambda: package.ExternalAction(
                package.ExternalActionKind.SAVE_SUBMIT,
                RUN_ID,
                "SAVE_SUBMITTING",
            ),
        ),
    ),
    "FinalPathRequest": (
        ("final_path",),
        lambda: FinalPathRequest("C:\\final\\audio.wav"),
        (lambda: FinalPathRequest("relative.wav"),),
    ),
    "FrozenCapturedRun": (
        (
            "run_id",
            "trigger_monotonic_ns",
            "source",
            "config_id",
            "hardware_id",
            "target_frame_count",
            "config_payload",
            "hardware_payload",
            "calibration_payload",
            "work_order_payload",
            "history_labels",
            "analysis_input",
            "final_path_request",
            "metadata",
        ),
        valid_frozen_captured_run,
        (
            lambda: dataclasses.replace(
                valid_frozen_captured_run(), source=TriggerSource.IMPORT
            ),
            lambda: dataclasses.replace(
                valid_frozen_captured_run(), target_frame_count=3
            ),
        ),
    ),
    "FrozenImportedRun": (
        (
            "run_id",
            "trigger_monotonic_ns",
            "source",
            "config_id",
            "source_reference",
            "config_payload",
            "calibration_payload",
            "work_order_payload",
            "history_labels",
            "analysis_input",
            "final_path_request",
            "metadata",
        ),
        valid_frozen_imported_run,
        (
            lambda: dataclasses.replace(
                valid_frozen_imported_run(), source=TriggerSource.MANUAL
            ),
            lambda: dataclasses.replace(
                valid_frozen_imported_run(),
                metadata=dataclasses.replace(
                    metadata(), expected_frame_count=3
                ),
            ),
        ),
    ),
    "FrozenPayload": (
        ("value_id", "canonical_bytes"),
        lambda: frozen("payload"),
        (lambda: FrozenPayload("", b"payload"),),
    ),
    "HistoryProjectionFact": (
        (
            "kind",
            "run_id",
            "expected_revision",
            "operation_id",
            "projection",
        ),
        lambda: HistoryProjectionFact(
            HistoryFactKind.RUN_TERMINATED,
            RUN_ID,
            1,
            ATTEMPT_ID,
            terminal_projection(),
        ),
        (
            lambda: HistoryProjectionFact(
                HistoryFactKind.RECORDING,
                RUN_ID,
                1,
                None,
                terminal_projection(),
            ),
        ),
    ),
    "HistoryEvictionNotice": (
        ("run_id",),
        lambda: HistoryEvictionNotice(RUN_ID),
        (
            lambda: HistoryEvictionNotice(f"{{{RUN_ID}}}"),
        ),
    ),
    "ImportedAudioSaveRequest": (
        (
            "save_job_id",
            "run_id",
            "source_kind",
            "source_identity",
            "source",
            "final_path_request",
            "metadata",
        ),
        valid_imported_save_request,
        (
            lambda: dataclasses.replace(
                valid_imported_save_request(),
                source_kind=AudioSaveSourceKind.CAPTURED_AUDIO,
            ),
        ),
    ),
    "ImportedSourceReference": (
        (
            "source_identity",
            "absolute_path",
            "sample_rate_hz",
            "channel_count",
            "frame_count",
            "format",
        ),
        imported_source,
        (
            lambda: dataclasses.replace(
                imported_source(), absolute_path="relative.wav"
            ),
        ),
    ),
    "ImportedStartRequest": (
        (
            "run_id",
            "trigger_monotonic_ns",
            "source",
            "ingress_payload",
            "fallback_history_labels",
        ),
        lambda: ImportedStartRequest(
            RUN_ID,
            1,
            TriggerSource.IMPORT,
            frozen("ingress"),
            frozen("labels"),
        ),
        (
            lambda: ImportedStartRequest(
                RUN_ID,
                1,
                TriggerSource.MANUAL,
                frozen("ingress"),
                frozen("labels"),
            ),
        ),
    ),
    "ManualJudgmentRequest": (
        ("run_id", "expected_revision", "conclusion", "fault_type", "bounded_note"),
        lambda: ManualJudgmentRequest(
            RUN_ID, 1, TerminalConclusion.OK, None, "operator"
        ),
        (
            lambda: ManualJudgmentRequest(
                RUN_ID, 1, TerminalConclusion.NO_RESULT, None, "operator"
            ),
        ),
    ),
    "ManualProjectionResult": (
        (
            "status",
            "run_id",
            "replacement_projection",
            "committed_revision",
            "reason",
        ),
        lambda: ManualProjectionResult(
            ManualProjectionStatus.COMMITTED,
            RUN_ID,
            terminal_projection(),
            2,
            "",
        ),
        (
            lambda: ManualProjectionResult(
                ManualProjectionStatus.COMMITTED,
                RUN_ID,
                None,
                2,
                "",
            ),
        ),
    ),
    "OperationResult": (
        ("run_id", "status", "attempt_id", "reason"),
        lambda: OperationResult(
            RUN_ID, OperationStatus.COMPLETED, None, ""
        ),
        (
            lambda: OperationResult(
                RUN_ID, OperationStatus.SUBMITTED, None, ""
            ),
        ),
    ),
    "PendingTerminalContender": (
        (
            "kind",
            "run_id",
            "attempt_id",
            "save_job_id",
            "conclusion",
            "error_category",
            "reason",
            "result_payload",
        ),
        lambda: package.PendingTerminalContender(
            package.TerminalContenderKind.EXTERNAL_FAILURE,
            RUN_ID,
            ATTEMPT_ID,
            SAVE_JOB_ID,
            TerminalConclusion.ERROR,
            TerminalErrorCategory.ANALYSIS_FAILED,
            "failed",
            None,
        ),
        (
            lambda: package.PendingTerminalContender(
                package.TerminalContenderKind.EXTERNAL_FAILURE,
                RUN_ID,
                ATTEMPT_ID,
                SAVE_JOB_ID,
                TerminalConclusion.ERROR,
                None,
                "failed",
                None,
            ),
        ),
    ),
    "PersistedAudioReference": (
        (
            "audio_id",
            "save_job_id",
            "run_id",
            "source_kind",
            "source_identity",
            "final_path",
            "sample_rate_hz",
            "channel_count",
            "frame_count",
            "byte_size",
            "format",
            "content_revision",
        ),
        persisted_audio,
        (
            lambda: dataclasses.replace(
                persisted_audio(), audio_id="not-a-uuid"
            ),
        ),
    ),
    "PreacceptTerminalFact": (
        (
            "run_id",
            "source",
            "config_id",
            "conclusion",
            "error_category",
            "reason",
            "history_labels",
            "terminal_at",
        ),
        valid_preaccept_terminal_fact,
        (
            lambda: dataclasses.replace(
                valid_preaccept_terminal_fact(),
                conclusion=TerminalConclusion.NO_RESULT,
                error_category=None,
                reason="",
            ),
        ),
    ),
    "PreparedAnalysisAttempt": (
        ("attempt_id", "run_id", "attempt_kind", "audio", "analysis_input"),
        lambda: PreparedAnalysisAttempt(
            ATTEMPT_ID,
            RUN_ID,
            AnalysisAttemptKind.INITIAL_CAPTURED,
            persisted_audio(),
            frozen("analysis"),
        ),
        (
            lambda: PreparedAnalysisAttempt(
                ATTEMPT_ID,
                OTHER_RUN_ID,
                AnalysisAttemptKind.INITIAL_CAPTURED,
                persisted_audio(),
                frozen("analysis"),
            ),
        ),
    ),
    "ProjectedHistoryEvent": (
        (
            "kind",
            "run_id",
            "expected_revision",
            "operation_id",
            "proposed_revision",
            "projection",
        ),
        lambda: ProjectedHistoryEvent(
            HistoryFactKind.RUN_TERMINATED,
            RUN_ID,
            1,
            ATTEMPT_ID,
            2,
            terminal_projection(),
        ),
        (
            lambda: ProjectedHistoryEvent(
                HistoryFactKind.RUN_TERMINATED,
                RUN_ID,
                1,
                ATTEMPT_ID,
                3,
                terminal_projection(),
            ),
        ),
    ),
    "ReanalysisCancelRequest": (
        ("run_id", "attempt_id", "expected_revision"),
        lambda: ReanalysisCancelRequest(RUN_ID, ATTEMPT_ID, 1),
        (
            lambda: ReanalysisCancelRequest(RUN_ID, "not-a-uuid", 1),
        ),
    ),
    "ReanalysisRequest": (
        ("run_id", "expected_revision", "start_sec", "end_sec"),
        lambda: ReanalysisRequest(RUN_ID, 1, 0.25, 0.75),
        (lambda: ReanalysisRequest(RUN_ID, 0, 0.25, 0.75),),
    ),
    "ResidentCancelResult": (
        ("status", "run_id", "reservation_id", "reason"),
        lambda: ResidentCancelResult(
            ResidentCancelStatus.CANCELLED, RUN_ID, "reservation:1", ""
        ),
        (
            lambda: ResidentCancelResult(
                ResidentCancelStatus.INVALID, RUN_ID, "reservation:1", ""
            ),
        ),
    ),
    "ResultControlFacts": (
        (
            "run_id",
            "config_payload",
            "calibration_payload",
            "analysis_input",
            "audio",
            "manual_judgment_allowed",
            "manual_reanalysis_allowed",
            "current_projection",
            "causal_revision",
        ),
        valid_result_control_facts,
        (
            lambda: dataclasses.replace(
                valid_result_control_facts(),
                audio=dataclasses.replace(
                    persisted_audio(), run_id=OTHER_RUN_ID
                ),
            ),
            lambda: dataclasses.replace(
                valid_result_control_facts(), manual_judgment_allowed=1
            ),
        ),
    ),
    "ResultProjection": (
        (
            "phase",
            "conclusion",
            "error_category",
            "reason",
            "history_labels",
            "result_payload",
            "fault_type",
            "manual_judgment_revision",
            "terminal_revision",
        ),
        terminal_projection,
        (
            lambda: ResultProjection(
                ResultProjectionPhase.RECORDING,
                None,
                None,
                None,
                frozen("labels"),
                None,
            ),
        ),
    ),
    "ResultEnrichmentProjection": (
        (
            "run_id",
            "attempt_id",
            "terminal_revision",
            "expected_revision",
            "result_payload",
        ),
        lambda: ResultEnrichmentProjection(
            RUN_ID,
            ATTEMPT_ID,
            "0" * 64,
            1,
            frozen("result"),
        ),
        (
            lambda: ResultEnrichmentProjection(
                RUN_ID,
                ATTEMPT_ID,
                "not-a-revision",
                1,
                frozen("result"),
            ),
        ),
    ),
    "SaveCancelResult": (
        ("status", "run_id", "save_job_id", "reason"),
        lambda: SaveCancelResult(
            SaveCancelStatus.CANCEL_REQUESTED, RUN_ID, SAVE_JOB_ID, ""
        ),
        (
            lambda: SaveCancelResult(
                SaveCancelStatus.CANCEL_REQUESTED,
                RUN_ID,
                SAVE_JOB_ID,
                "unexpected",
            ),
        ),
    ),
    "SaveDispatcherShutdownReport": (
        (
            "status",
            "accepted_count",
            "canceled_count",
            "completed_count",
            "remaining_save_job_ids",
            "live_worker_ids",
            "errors",
        ),
        valid_closed_dispatcher_report,
        (
            lambda: SaveDispatcherShutdownReport(
                SaveDispatcherShutdownStatus.INCOMPLETE,
                0,
                0,
                0,
                [],
                [],
                [],
            ),
        ),
    ),
    "SaveSubmission": (
        (
            "status",
            "run_id",
            "save_job_id",
            "source_kind",
            "source_identity",
            "reason",
        ),
        lambda: SaveSubmission(
            SaveSubmitStatus.ACCEPTED,
            RUN_ID,
            SAVE_JOB_ID,
            AudioSaveSourceKind.CAPTURED_AUDIO,
            "resident:1",
            "",
        ),
        (
            lambda: SaveSubmission(
                SaveSubmitStatus.ACCEPTED,
                RUN_ID,
                SAVE_JOB_ID,
                AudioSaveSourceKind.CAPTURED_AUDIO,
                "resident:1",
                "unexpected",
            ),
        ),
    ),
    "SchedulerCancelResult": (
        ("status", "run_id", "attempt_id", "reason"),
        lambda: SchedulerCancelResult(
            SchedulerCancelStatus.CANCELED_PENDING,
            RUN_ID,
            ATTEMPT_ID,
            "",
        ),
        (
            lambda: SchedulerCancelResult(
                SchedulerCancelStatus.RUN_ID_MISMATCH,
                RUN_ID,
                ATTEMPT_ID,
                "",
            ),
        ),
    ),
    "SchedulerSubmission": (
        (
            "status",
            "run_id",
            "attempt_id",
            "audio_id",
            "audio_content_revision",
            "reason",
        ),
        lambda: SchedulerSubmission(
            SchedulerSubmitStatus.ACCEPTED,
            RUN_ID,
            ATTEMPT_ID,
            AUDIO_ID,
            1,
            "",
        ),
        (
            lambda: SchedulerSubmission(
                SchedulerSubmitStatus.ACCEPTED,
                RUN_ID,
                ATTEMPT_ID,
                AUDIO_ID,
                0,
                "",
            ),
        ),
    ),
    "StartResult": (
        ("run_id", "status", "accepted", "phase", "terminal", "reason"),
        lambda: StartResult(
            RUN_ID,
            StartStatus.ACCEPTED_ACTIVE,
            True,
            CoordinatorPhase.RECORDING,
            None,
            "",
        ),
        (
            lambda: StartResult(
                RUN_ID,
                StartStatus.ACCEPTED_ACTIVE,
                True,
                CoordinatorPhase.PREFLIGHT,
                None,
                "",
            ),
        ),
    ),
    "StopReport": (
        (
            "status",
            "pending_ingress_run_ids",
            "pending_publication_run_ids",
            "canceled_accepted_run_ids",
            "canceled_reanalysis_attempt_ids",
            "timed_out",
            "errors",
            "pending_analysis_attempt_ids",
            "restart_required",
        ),
        lambda: StopReport(
            StopStatus.SETTLED, [], [], [], [], False, [], [], False
        ),
        (
            lambda: StopReport(
                StopStatus.INCOMPLETE,
                [],
                [],
                [],
                [],
                True,
                [],
                [],
                True,
            ),
        ),
    ),
    "TerminalFact": (
        (
            "run_id",
            "source",
            "attempt_kind",
            "attempt_id",
            "conclusion",
            "error_category",
            "reason",
            "result_payload",
            "history_labels",
            "terminal_at",
        ),
        valid_terminal_fact,
        (
            lambda: dataclasses.replace(
                valid_terminal_fact(), terminal_at=datetime.now()
            ),
        ),
    ),
}

for _obsolete_contract_name in (
    "AnalysisJob",
    "AnalysisOutcome",
    "PreparedAnalysisAttempt",
    "SchedulerCancelResult",
    "SchedulerSubmission",
):
    CONSTRUCTOR_CASES.pop(_obsolete_contract_name)


@pytest.mark.parametrize("dataclass_name", sorted(CONSTRUCTOR_CASES))
def test_public_dataclass_constructor_matrix(dataclass_name):
    expected_fields, valid_factory, invalid_factories = CONSTRUCTOR_CASES[
        dataclass_name
    ]
    dataclass_type = getattr(package, dataclass_name)

    assert tuple(
        field.name for field in dataclasses.fields(dataclass_type)
    ) == expected_fields
    assert type(valid_factory()) is dataclass_type
    assert invalid_factories
    for invalid_factory in invalid_factories:
        with pytest.raises(ValueError):
            invalid_factory()


def test_constructor_matrix_covers_every_public_dataclass():
    assert set(CONSTRUCTOR_CASES) == PUBLIC_DATACLASS_NAMES


@pytest.mark.parametrize(
    "run_id",
    [
        "not-a-uuid",
        str(uuid.uuid1()),
        str(uuid.uuid4()).upper(),
        7,
    ],
)
def test_captured_request_rejects_noncanonical_uuid4(run_id):
    with pytest.raises(ValueError):
        captured_request(run_id=run_id)


@pytest.mark.parametrize("trigger", [0, -1, True, 1.0])
def test_start_requests_require_positive_integer_monotonic_time(trigger):
    with pytest.raises(ValueError):
        captured_request(trigger_monotonic_ns=trigger)


def test_start_request_source_and_plc_id_are_coherent():
    assert captured_request(source=TriggerSource.PLC, plc_id=7).plc_id == 7
    with pytest.raises(ValueError):
        captured_request(source=TriggerSource.PLC, plc_id=None)
    with pytest.raises(ValueError):
        captured_request(source=TriggerSource.MANUAL, plc_id=7)
    with pytest.raises(ValueError):
        captured_request(source=TriggerSource.IMPORT)
    with pytest.raises(ValueError):
        ImportedStartRequest(
            run_id=RUN_ID,
            trigger_monotonic_ns=1,
            source=TriggerSource.MANUAL,
            ingress_payload=frozen("ingress"),
            fallback_history_labels=frozen("labels"),
        )


@pytest.mark.parametrize("revision", [0, -1, True, 1.5])
def test_result_operation_requests_require_positive_integer_revision(revision):
    with pytest.raises(ValueError):
        ReanalysisRequest(
            run_id=RUN_ID,
            expected_revision=revision,
            start_sec=0.25,
            end_sec=0.75,
        )


def test_manual_request_has_closed_conclusion_and_bounded_note():
    request = ManualJudgmentRequest(
        run_id=RUN_ID,
        expected_revision=1,
        conclusion=TerminalConclusion.OK,
        fault_type=None,
        bounded_note="operator",
    )
    assert request.bounded_note == "operator"
    with pytest.raises(ValueError):
        dataclasses.replace(request, conclusion=TerminalConclusion.NO_RESULT)
    with pytest.raises(ValueError):
        dataclasses.replace(request, bounded_note="a" * 513)
    with pytest.raises(ValueError):
        dataclasses.replace(request, bounded_note="bad\x00note")


def test_payload_is_exact_immutable_bytes_and_all_dataclasses_are_frozen_slotted():
    payload = frozen("payload")
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        payload.value_id = "changed"
    with pytest.raises(AttributeError):
        payload.extra = 1
    with pytest.raises(ValueError):
        FrozenPayload(value_id="", canonical_bytes=b"x")
    with pytest.raises(ValueError):
        FrozenPayload(value_id="x", canonical_bytes=bytearray(b"x"))


@pytest.mark.parametrize(
    ("format_", "byte_count"),
    [
        (CapturedSourceFormat.INT16_INTERLEAVED, 4),
        (CapturedSourceFormat.FLOAT32_INTERLEAVED, 8),
    ],
)
def test_captured_audio_validates_exact_interleaved_byte_length(format_, byte_count):
    value = dataclasses.replace(
        captured_audio(),
        format=format_,
        interleaved_bytes=b"\x00" * byte_count,
    )
    assert len(value.interleaved_bytes) == byte_count
    with pytest.raises(ValueError):
        dataclasses.replace(value, interleaved_bytes=b"\x00" * (byte_count - 1))


def test_paths_and_metadata_are_closed_and_positive():
    assert imported_source().absolute_path == "C:\\input\\source.wav"
    with pytest.raises(ValueError):
        dataclasses.replace(imported_source(), absolute_path="relative.wav")
    with pytest.raises(ValueError):
        dataclasses.replace(metadata(), expected_frame_count=0)
    with pytest.raises(ValueError):
        dataclasses.replace(metadata(), expected_frame_count=True)


@pytest.mark.parametrize(
    ("status", "audio", "category", "reason", "valid"),
    [
        (CaptureOutcomeStatus.COMPLETED, captured_audio(), None, "", True),
        (
            CaptureOutcomeStatus.CANCELLED,
            None,
            TerminalErrorCategory.CAPTURE_CANCELLED,
            "cancelled",
            True,
        ),
        (
            CaptureOutcomeStatus.FAILED,
            None,
            TerminalErrorCategory.CAPTURE_FAILED,
            "failed",
            True,
        ),
        (CaptureOutcomeStatus.COMPLETED, None, None, "", False),
        (
            CaptureOutcomeStatus.CANCELLED,
            captured_audio(),
            TerminalErrorCategory.CAPTURE_CANCELLED,
            "cancelled",
            False,
        ),
        (
            CaptureOutcomeStatus.FAILED,
            None,
            TerminalErrorCategory.CAPTURE_FAILED,
            "",
            False,
        ),
    ],
)
def test_capture_outcome_matrix(status, audio, category, reason, valid):
    reservation = CaptureReservation(
        reservation_id="reservation:1",
        run_id=RUN_ID,
        hardware_id="hardware:1",
    )
    kwargs = dict(
        run_id=RUN_ID,
        reservation=reservation,
        status=status,
        audio=audio,
        error_category=category,
        reason=reason,
    )
    if valid:
        assert CaptureOutcome(**kwargs).status is status
    else:
        with pytest.raises(ValueError):
            CaptureOutcome(**kwargs)


def test_capture_reserve_result_requires_exact_reservation_coherence():
    reservation = CaptureReservation("reservation:1", RUN_ID, "hardware:1")
    assert (
        CaptureReserveResult(
            CaptureReserveStatus.RESERVED,
            RUN_ID,
            "hardware:1",
            reservation,
            "",
        ).reservation
        is reservation
    )
    with pytest.raises(ValueError):
        CaptureReserveResult(
            CaptureReserveStatus.CAPACITY,
            RUN_ID,
            "hardware:1",
            reservation,
            "full",
        )
    with pytest.raises(ValueError):
        CaptureReserveResult(
            CaptureReserveStatus.RESERVED,
            OTHER_RUN_ID,
            "hardware:1",
            reservation,
            "",
        )


@pytest.mark.parametrize(
    ("constructor", "nested_value"),
    [
        pytest.param(
            lambda reservation: CaptureReserveResult(
                CaptureReserveStatus.RESERVED,
                RUN_ID,
                "hardware:1",
                reservation,
                "",
            ),
            object(),
            id="capture-reservation-wrong-type",
        ),
        pytest.param(
            lambda reservation: CaptureReserveResult(
                CaptureReserveStatus.RESERVED,
                RUN_ID,
                "hardware:1",
                reservation,
                "",
            ),
            type(
                "DuckCaptureReservation",
                (),
                {"run_id": RUN_ID, "hardware_id": "hardware:1"},
            )(),
            id="capture-reservation-duck-type",
        ),
    ],
)
def test_nested_identity_contracts_reject_wrong_and_duck_types(
    constructor,
    nested_value,
):
    with pytest.raises(ValueError):
        constructor(nested_value)


@pytest.mark.parametrize(
    "identity_kwargs",
    [
        {},
        {"attempt_id": ATTEMPT_ID},
        {"save_job_id": SAVE_JOB_ID},
        {"reservation_id": "reservation:1"},
    ],
    ids=["none", "attempt", "save-job", "reservation"],
)
def test_external_action_accepts_at_most_one_single_identity(identity_kwargs):
    action = package.ExternalAction(
        package.ExternalActionKind.CHILD_CANCEL,
        RUN_ID,
        CoordinatorPhase.REGISTERING,
        **identity_kwargs,
    )

    assert sum(
        identity is not None
        for identity in (
            action.attempt_id,
            action.save_job_id,
            action.reservation_id,
        )
    ) == len(identity_kwargs)


@pytest.mark.parametrize(
    "conflicting_fields",
    list(
        itertools.combinations(
            ("attempt_id", "save_job_id", "reservation_id"),
            2,
        )
    ),
)
def test_external_action_rejects_every_conflicting_identity_pair(
    conflicting_fields,
):
    values = {
        "attempt_id": ATTEMPT_ID,
        "save_job_id": SAVE_JOB_ID,
        "reservation_id": "reservation:1",
    }
    identity_kwargs = {
        field_name: values[field_name] for field_name in conflicting_fields
    }

    with pytest.raises(ValueError):
        package.ExternalAction(
            package.ExternalActionKind.CHILD_CANCEL,
            RUN_ID,
            CoordinatorPhase.REGISTERING,
            **identity_kwargs,
        )


@pytest.mark.parametrize(
    ("conclusion", "payload", "category", "reason", "valid"),
    [
        (TerminalConclusion.OK, frozen("result"), None, "", True),
        (TerminalConclusion.NG, frozen("result"), None, "", True),
        (TerminalConclusion.NO_RESULT, None, None, "", True),
        (
            TerminalConclusion.ERROR,
            None,
            TerminalErrorCategory.ANALYSIS_FAILED,
            "failed",
            True,
        ),
        (
            TerminalConclusion.CAPACITY_REJECTED,
            None,
            TerminalErrorCategory.CAPTURE_CAPACITY,
            "",
            False,
        ),
        (
            TerminalConclusion.MANUAL_STOPPED,
            None,
            TerminalErrorCategory.MANUAL_STOPPED,
            "",
            True,
        ),
        (TerminalConclusion.OK, None, None, "", False),
        (
            TerminalConclusion.ERROR,
            frozen("result"),
            TerminalErrorCategory.ANALYSIS_FAILED,
            "failed",
            False,
        ),
        (
            TerminalConclusion.CAPACITY_REJECTED,
            None,
            TerminalErrorCategory.CAPTURE_FAILED,
            "",
            False,
        ),
    ],
)
def test_terminal_fact_conclusion_matrix(conclusion, payload, category, reason, valid):
    kwargs = dict(
        run_id=RUN_ID,
        source=TriggerSource.MANUAL,
        attempt_kind=AnalysisAttemptKind.INITIAL_CAPTURED,
        attempt_id=ATTEMPT_ID,
        conclusion=conclusion,
        error_category=category,
        reason=reason,
        result_payload=payload,
        history_labels=frozen("labels"),
        terminal_at=datetime.now(timezone.utc),
    )
    if valid:
        assert TerminalFact(**kwargs).conclusion is conclusion
    else:
        with pytest.raises(ValueError):
            TerminalFact(**kwargs)


def test_terminal_fact_requires_aware_datetime_and_attempt_identity():
    fact = TerminalFact(
        run_id=RUN_ID,
        source=TriggerSource.MANUAL,
        attempt_kind=AnalysisAttemptKind.INITIAL_CAPTURED,
        attempt_id=ATTEMPT_ID,
        conclusion=TerminalConclusion.NO_RESULT,
        error_category=None,
        reason="",
        result_payload=None,
        history_labels=frozen("labels"),
        terminal_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError):
        dataclasses.replace(fact, terminal_at=datetime.now())
    with pytest.raises(ValueError):
        dataclasses.replace(
            fact,
            attempt_kind=AnalysisAttemptKind.REANALYSIS,
            attempt_id=None,
        )


@pytest.mark.parametrize(
    "category",
    sorted(PREACCEPT_ERROR_CATEGORIES, key=lambda item: item.name),
)
def test_preaccept_terminal_allows_each_exact_error_category(category):
    source = (
        TriggerSource.PLC
        if category is TerminalErrorCategory.PLC_INGRESS_REJECTED
        else TriggerSource.MANUAL
    )
    assert preaccept_error_fact(category, source=source).error_category is category


def test_preaccept_terminal_allows_capacity_rejection_for_captured_source():
    assert (
        preaccept_capacity_fact().error_category
        is TerminalErrorCategory.CAPTURE_CAPACITY
    )


@pytest.mark.parametrize(
    "category",
    sorted(
        set(TerminalErrorCategory)
        - PREACCEPT_ERROR_CATEGORIES
        - {TerminalErrorCategory.CAPTURE_CAPACITY},
        key=lambda item: item.name,
    ),
)
def test_preaccept_terminal_rejects_every_postaccept_category(category):
    with pytest.raises(ValueError):
        preaccept_error_fact(category)


@pytest.mark.parametrize(
    ("category", "source", "capacity_conclusion"),
    [
        (
            TerminalErrorCategory.PLC_INGRESS_REJECTED,
            TriggerSource.MANUAL,
            False,
        ),
        (
            TerminalErrorCategory.CAPTURE_CAPACITY,
            TriggerSource.IMPORT,
            True,
        ),
        (
            TerminalErrorCategory.RESIDENT_UNAVAILABLE,
            TriggerSource.IMPORT,
            False,
        ),
    ],
)
def test_preaccept_terminal_rejects_source_category_mismatch(
    category,
    source,
    capacity_conclusion,
):
    with pytest.raises(ValueError):
        if capacity_conclusion:
            preaccept_capacity_fact(source=source)
        else:
            preaccept_error_fact(category, source=source)


@pytest.mark.parametrize(
    "category",
    sorted(
        INITIAL_CAPTURED_NON_PLC_ERROR_CATEGORIES,
        key=lambda item: item.name,
    ),
)
def test_initial_captured_terminal_allows_each_non_plc_error_category(category):
    assert terminal_error_fact(category).error_category is category


@pytest.mark.parametrize(
    "category",
    sorted(
        INITIAL_CAPTURED_PLC_ONLY_ERROR_CATEGORIES,
        key=lambda item: item.name,
    ),
)
def test_initial_captured_plc_terminal_allows_each_plc_only_category(category):
    fact = terminal_error_fact(category, source=TriggerSource.PLC)
    assert fact.error_category is category


@pytest.mark.parametrize(
    "category",
    sorted(INITIAL_IMPORTED_ERROR_CATEGORIES, key=lambda item: item.name),
)
def test_initial_imported_terminal_allows_each_exact_error_category(category):
    fact = terminal_error_fact(
        category,
        attempt_kind=AnalysisAttemptKind.INITIAL_IMPORTED,
        source=TriggerSource.IMPORT,
    )
    assert fact.error_category is category


@pytest.mark.parametrize(
    ("attempt_kind", "source"),
    [
        (AnalysisAttemptKind.INITIAL_CAPTURED, TriggerSource.MANUAL),
        (AnalysisAttemptKind.INITIAL_IMPORTED, TriggerSource.IMPORT),
    ],
)
def test_each_initial_kind_allows_manual_stopped_terminal(attempt_kind, source):
    fact = TerminalFact(
        RUN_ID,
        source,
        attempt_kind,
        None,
        TerminalConclusion.MANUAL_STOPPED,
        TerminalErrorCategory.MANUAL_STOPPED,
        "",
        None,
        frozen("labels"),
        datetime.now(timezone.utc),
    )
    assert fact.conclusion is TerminalConclusion.MANUAL_STOPPED


@pytest.mark.parametrize(
    "category",
    sorted(PREACCEPT_ONLY_CATEGORIES, key=lambda item: item.name),
)
def test_postaccept_terminal_rejects_every_preaccept_only_category(category):
    with pytest.raises(ValueError):
        terminal_error_fact(category)


@pytest.mark.parametrize(
    "category",
    sorted(
        CAPTURE_POSTACCEPT_CATEGORIES
        | INITIAL_CAPTURED_PLC_ONLY_ERROR_CATEGORIES,
        key=lambda item: item.name,
    ),
)
def test_initial_imported_terminal_rejects_plc_and_capture_categories(category):
    with pytest.raises(ValueError):
        terminal_error_fact(
            category,
            attempt_kind=AnalysisAttemptKind.INITIAL_IMPORTED,
            source=TriggerSource.IMPORT,
        )


@pytest.mark.parametrize(
    "category",
    sorted(
        INITIAL_CAPTURED_PLC_ONLY_ERROR_CATEGORIES,
        key=lambda item: item.name,
    ),
)
def test_initial_captured_non_plc_terminal_rejects_plc_only_category(category):
    with pytest.raises(ValueError):
        terminal_error_fact(category, source=TriggerSource.MANUAL)


@pytest.mark.parametrize(
    "category",
    sorted(REANALYSIS_ERROR_CATEGORIES, key=lambda item: item.name),
)
def test_reanalysis_terminal_allows_each_exact_error_category(category):
    fact = terminal_error_fact(
        category,
        attempt_kind=AnalysisAttemptKind.REANALYSIS,
    )
    assert fact.error_category is category


@pytest.mark.parametrize("source", list(TriggerSource))
def test_reanalysis_terminal_preserves_every_initial_trigger_source(source):
    fact = terminal_error_fact(
        TerminalErrorCategory.ANALYSIS_FAILED,
        attempt_kind=AnalysisAttemptKind.REANALYSIS,
        source=source,
    )
    assert fact.source is source


@pytest.mark.parametrize(
    "conclusion",
    [
        TerminalConclusion.OK,
        TerminalConclusion.NG,
        TerminalConclusion.NO_RESULT,
    ],
)
def test_reanalysis_terminal_allows_each_completed_conclusion(conclusion):
    fact = completed_terminal_fact(
        conclusion,
        attempt_kind=AnalysisAttemptKind.REANALYSIS,
    )
    assert fact.conclusion is conclusion


@pytest.mark.parametrize(
    "category",
    sorted(
        set(TerminalErrorCategory) - REANALYSIS_ERROR_CATEGORIES,
        key=lambda item: item.name,
    ),
)
def test_reanalysis_terminal_rejects_every_other_error_category(category):
    with pytest.raises(ValueError):
        terminal_error_fact(
            category,
            attempt_kind=AnalysisAttemptKind.REANALYSIS,
        )


def test_reanalysis_terminal_rejects_manual_stopped_conclusion():
    with pytest.raises(ValueError):
        TerminalFact(
            RUN_ID,
            TriggerSource.MANUAL,
            AnalysisAttemptKind.REANALYSIS,
            ATTEMPT_ID,
            TerminalConclusion.MANUAL_STOPPED,
            TerminalErrorCategory.MANUAL_STOPPED,
            "",
            None,
            frozen("labels"),
            datetime.now(timezone.utc),
        )


@pytest.mark.parametrize(
    "conclusion",
    [
        TerminalConclusion.OK,
        TerminalConclusion.NG,
        TerminalConclusion.NO_RESULT,
    ],
)
def test_completed_terminal_requires_attempt_id(conclusion):
    with pytest.raises(ValueError):
        completed_terminal_fact(conclusion, attempt_id=None)


@pytest.mark.parametrize(
    "category",
    sorted(
        ATTEMPT_ID_REQUIRED_ERROR_CATEGORIES,
        key=lambda item: item.name,
    ),
)
def test_attempt_stage_terminal_error_requires_attempt_id(category):
    with pytest.raises(ValueError):
        terminal_error_fact(category, attempt_id=None)


def test_attempt_id_failure_requires_no_attempt_id():
    assert (
        terminal_error_fact(TerminalErrorCategory.ATTEMPT_ID_FAILED).attempt_id
        is None
    )
    with pytest.raises(ValueError):
        terminal_error_fact(
            TerminalErrorCategory.ATTEMPT_ID_FAILED,
            attempt_id=ATTEMPT_ID,
        )


@pytest.mark.parametrize(
    "category",
    [
        TerminalErrorCategory.CAPTURE_FAILED,
        TerminalErrorCategory.AUDIO_SAVE_FAILED,
    ],
)
def test_pre_attempt_terminal_error_allows_missing_attempt_id(category):
    assert terminal_error_fact(category, attempt_id=None).attempt_id is None


@pytest.mark.parametrize(
    ("status", "conclusion", "payload", "category", "valid"),
    [
        (
            AnalysisOutcomeStatus.COMPLETED,
            TerminalConclusion.OK,
            frozen("result"),
            None,
            True,
        ),
        (
            AnalysisOutcomeStatus.COMPLETED,
            TerminalConclusion.NO_RESULT,
            None,
            None,
            True,
        ),
        (
            AnalysisOutcomeStatus.FAILED,
            TerminalConclusion.ERROR,
            None,
            TerminalErrorCategory.ANALYSIS_FAILED,
            True,
        ),
        (
            AnalysisOutcomeStatus.CANCELLED,
            TerminalConclusion.ERROR,
            None,
            TerminalErrorCategory.ANALYSIS_CANCELLED,
            True,
        ),
        (
            AnalysisOutcomeStatus.COMPLETED,
            TerminalConclusion.ERROR,
            None,
            TerminalErrorCategory.ANALYSIS_FAILED,
            False,
        ),
        (
            AnalysisOutcomeStatus.FAILED,
            TerminalConclusion.ERROR,
            None,
            TerminalErrorCategory.ANALYSIS_CANCELLED,
            False,
        ),
    ],
)
def test_analysis_outcome_matrix(status, conclusion, payload, category, valid):
    kwargs = dict(
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        attempt_kind=AnalysisAttemptKind.INITIAL_CAPTURED,
        audio_id=AUDIO_ID,
        audio_content_revision=1,
        status=status,
        conclusion=conclusion,
        result_payload=payload,
        error_category=category,
        reason="",
    )
    if valid:
        assert AnalysisOutcome(**kwargs).status is status
    else:
        with pytest.raises(ValueError):
            AnalysisOutcome(**kwargs)


def test_analysis_job_and_prepared_attempt_preserve_exact_lightweight_identity():
    audio = persisted_audio()
    prepared = PreparedAnalysisAttempt(
        ATTEMPT_ID,
        RUN_ID,
        AnalysisAttemptKind.INITIAL_CAPTURED,
        audio,
        frozen("analysis"),
    )
    job = AnalysisJob(
        ATTEMPT_ID,
        RUN_ID,
        AnalysisAttemptKind.INITIAL_CAPTURED,
        audio,
        frozen("analysis"),
    )
    assert prepared.audio is audio
    assert job.audio is audio
    with pytest.raises(ValueError):
        dataclasses.replace(job, run_id=OTHER_RUN_ID)


def test_save_requests_require_embedded_source_and_metadata_coherence():
    captured = CapturedAudioSaveRequest(
        SAVE_JOB_ID,
        RUN_ID,
        AudioSaveSourceKind.CAPTURED_AUDIO,
        "resident:1",
        captured_audio(),
        FinalPathRequest("C:\\final\\audio.wav"),
        metadata(),
    )
    assert captured.source_identity == captured.audio.source_identity
    with pytest.raises(ValueError):
        dataclasses.replace(captured, source_identity="resident:other")
    with pytest.raises(ValueError):
        dataclasses.replace(
            captured,
            metadata=dataclasses.replace(metadata(), expected_frame_count=3),
        )

    imported = ImportedAudioSaveRequest(
        SAVE_JOB_ID,
        RUN_ID,
        AudioSaveSourceKind.IMPORTED_SOURCE,
        "import:1",
        imported_source(),
        FinalPathRequest("C:\\final\\audio.wav"),
        metadata(),
    )
    assert imported.source_identity == imported.source.source_identity
    with pytest.raises(ValueError):
        dataclasses.replace(imported, source_kind=AudioSaveSourceKind.CAPTURED_AUDIO)


def test_audio_save_outcome_and_persisted_reference_identity_are_coherent():
    reference = persisted_audio()
    outcome = AudioSaveOutcome(
        SAVE_JOB_ID,
        RUN_ID,
        AudioSaveSourceKind.CAPTURED_AUDIO,
        "resident:1",
        AudioSaveStatus.SAVED,
        reference,
        None,
        "",
    )
    assert outcome.persisted_audio is reference
    with pytest.raises(ValueError):
        dataclasses.replace(outcome, source_identity="resident:other")
    with pytest.raises(ValueError):
        dataclasses.replace(
            outcome,
            status=AudioSaveStatus.FAILED,
            persisted_audio=None,
            error_category=TerminalErrorCategory.AUDIO_SAVE_CANCELLED,
            reason="failed",
        )


def test_persisted_audio_reference_requires_byte_size_constructor_field():
    reference = persisted_audio()
    kwargs = {
        field.name: getattr(reference, field.name)
        for field in dataclasses.fields(PersistedAudioReference)
        if field.name != "byte_size"
    }

    with pytest.raises(TypeError):
        PersistedAudioReference(**kwargs)


@pytest.mark.parametrize(
    "byte_size",
    [None, True, False, 0, -1, 1.0, "4"],
)
def test_persisted_audio_reference_rejects_invalid_byte_size(
    byte_size,
) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(persisted_audio(), byte_size=byte_size)


@pytest.mark.parametrize(
    ("phase", "conclusion", "payload", "category", "valid"),
    [
        (ResultProjectionPhase.RECORDING, None, None, None, True),
        (ResultProjectionPhase.WAITING_SAVE, None, None, None, True),
        (ResultProjectionPhase.WAITING_ANALYSIS, None, None, None, True),
        (ResultProjectionPhase.ANALYZING, None, None, None, True),
        (ResultProjectionPhase.TERMINAL, TerminalConclusion.OK, frozen("result"), None, True),
        (
            ResultProjectionPhase.REANALYZING,
            TerminalConclusion.ERROR,
            None,
            TerminalErrorCategory.ANALYSIS_FAILED,
            True,
        ),
        (ResultProjectionPhase.RECORDING, TerminalConclusion.NO_RESULT, None, None, False),
        (ResultProjectionPhase.TERMINAL, None, None, None, False),
    ],
)
def test_result_projection_phase_matrix(phase, conclusion, payload, category, valid):
    kwargs = dict(
        phase=phase,
        conclusion=conclusion,
        error_category=category,
        reason="failed" if category else "",
        history_labels=frozen("labels"),
        result_payload=payload,
    )
    if valid:
        assert ResultProjection(**kwargs).phase is phase
    else:
        with pytest.raises(ValueError):
            ResultProjection(**kwargs)


@pytest.mark.parametrize("reason", [None, False, 0, b""])
def test_active_result_projection_rejects_non_string_falsy_reason(reason):
    with pytest.raises(ValueError):
        ResultProjection(
            phase=ResultProjectionPhase.RECORDING,
            conclusion=None,
            error_category=None,
            reason=reason,
            history_labels=frozen("labels"),
            result_payload=None,
        )


def test_history_proposed_revision_is_exact():
    projection = terminal_projection()
    absent = ProjectedHistoryEvent(
        HistoryFactKind.RUN_TERMINATED,
        RUN_ID,
        ABSENT,
        None,
        1,
        projection,
    )
    current = dataclasses.replace(absent, expected_revision=7, proposed_revision=8)
    assert absent.proposed_revision == 1
    assert current.proposed_revision == 8
    with pytest.raises(ValueError):
        dataclasses.replace(absent, proposed_revision=2)
    with pytest.raises(ValueError):
        dataclasses.replace(current, proposed_revision=9)


@pytest.mark.parametrize(
    ("result_type", "valid_status", "invalid_status"),
    [
        (
            ResidentCancelResult,
            ResidentCancelStatus.CANCELLED,
            ResidentCancelStatus.INVALID,
        ),
        (
            SaveCancelResult,
            SaveCancelStatus.CANCEL_REQUESTED,
            None,
        ),
        (
            SchedulerCancelResult,
            SchedulerCancelStatus.CANCELED_PENDING,
            SchedulerCancelStatus.RUN_ID_MISMATCH,
        ),
    ],
)
def test_child_cancel_result_reason_coherence(result_type, valid_status, invalid_status):
    identity = "reservation:1" if result_type is ResidentCancelResult else (
        SAVE_JOB_ID if result_type is SaveCancelResult else ATTEMPT_ID
    )
    assert result_type(valid_status, RUN_ID, identity, "").reason == ""
    if invalid_status is not None:
        with pytest.raises(ValueError):
            result_type(invalid_status, RUN_ID, identity, "")


def test_report_inputs_are_copied_sorted_and_errors_capped():
    report = StopReport(
        status=StopStatus.INCOMPLETE,
        pending_ingress_run_ids=[OTHER_RUN_ID, RUN_ID],
        pending_publication_run_ids=[],
        canceled_accepted_run_ids=[OTHER_RUN_ID, RUN_ID],
        canceled_reanalysis_attempt_ids=[ATTEMPT_ID],
        timed_out=True,
        errors=(f"error-{i}" for i in range(40)),
        pending_analysis_attempt_ids=[ATTEMPT_ID],
        restart_required=True,
    )
    assert report.pending_ingress_run_ids == (RUN_ID, OTHER_RUN_ID)
    assert isinstance(report.pending_ingress_run_ids, tuple)
    assert len(report.errors) == 32


@pytest.mark.parametrize(
    (
        "status",
        "accepted_count",
        "completed_count",
        "remaining_job_ids",
        "live_worker_ids",
        "valid",
    ),
    [
        (SaveDispatcherShutdownStatus.CLOSED, 1, 0, [], [], False),
        (
            SaveDispatcherShutdownStatus.INCOMPLETE,
            0,
            0,
            [SAVE_JOB_ID],
            [],
            False,
        ),
        (SaveDispatcherShutdownStatus.CLOSED, 2, 2, [], [], True),
        (
            SaveDispatcherShutdownStatus.INCOMPLETE,
            3,
            2,
            [SAVE_JOB_ID],
            [],
            True,
        ),
    ],
)
def test_save_dispatcher_shutdown_accounting_partitions(
    status,
    accepted_count,
    completed_count,
    remaining_job_ids,
    live_worker_ids,
    valid,
):
    kwargs = dict(
        status=status,
        accepted_count=accepted_count,
        canceled_count=accepted_count,
        completed_count=completed_count,
        remaining_save_job_ids=remaining_job_ids,
        live_worker_ids=live_worker_ids,
        errors=[],
    )
    if valid:
        report = SaveDispatcherShutdownReport(**kwargs)
        assert report.accepted_count == (
            report.completed_count + len(report.remaining_save_job_ids)
        )
    else:
        with pytest.raises(ValueError):
            SaveDispatcherShutdownReport(**kwargs)


@pytest.mark.parametrize(
    ("contract_name", "identity_field"),
    [
        ("SaveDispatcherShutdownReport", "remaining_save_job_ids"),
        ("SaveDispatcherShutdownReport", "live_worker_ids"),
        ("StopReport", "pending_ingress_run_ids"),
        ("StopReport", "pending_publication_run_ids"),
        ("StopReport", "canceled_accepted_run_ids"),
        ("StopReport", "canceled_reanalysis_attempt_ids"),
        ("CoordinatorShutdownReport", "remaining_ingress_run_ids"),
        ("CoordinatorShutdownReport", "remaining_run_ids"),
        ("CoordinatorShutdownReport", "remaining_attempt_ids"),
        ("CoordinatorShutdownReport", "remaining_save_job_ids"),
        ("CoordinatorShutdownReport", "pending_publication_run_ids"),
        ("CoordinatorSnapshot", "ingress_run_ids"),
        ("CoordinatorSnapshot", "accepted_active_run_ids"),
        ("CoordinatorSnapshot", "analysis_attempt_ids"),
        ("CoordinatorSnapshot", "result_operation_attempt_ids"),
        ("CoordinatorSnapshot", "pending_publication_run_ids"),
    ],
)
def test_every_report_and_snapshot_identity_tuple_rejects_duplicates(
    contract_name,
    identity_field,
):
    identity = (
        "worker-1"
        if identity_field == "live_worker_ids"
        else ATTEMPT_ID
        if "attempt" in identity_field
        else SAVE_JOB_ID
        if "save_job" in identity_field
        else RUN_ID
    )
    duplicate_values = [identity, identity]

    if contract_name == "SaveDispatcherShutdownReport":
        remaining_save_job_ids = (
            duplicate_values
            if identity_field == "remaining_save_job_ids"
            else []
        )
        live_worker_ids = (
            duplicate_values if identity_field == "live_worker_ids" else []
        )
        with pytest.raises(ValueError):
            SaveDispatcherShutdownReport(
                SaveDispatcherShutdownStatus.INCOMPLETE,
                accepted_count=len(remaining_save_job_ids),
                canceled_count=0,
                completed_count=0,
                remaining_save_job_ids=remaining_save_job_ids,
                live_worker_ids=live_worker_ids,
                errors=[],
            )
    elif contract_name == "StopReport":
        values = {
            "pending_ingress_run_ids": [],
            "pending_publication_run_ids": [],
            "canceled_accepted_run_ids": [],
            "canceled_reanalysis_attempt_ids": [],
        }
        values[identity_field] = duplicate_values
        with pytest.raises(ValueError):
            StopReport(
                StopStatus.INCOMPLETE,
                timed_out=True,
                errors=[],
                pending_analysis_attempt_ids=[],
                restart_required=True,
                **values,
            )
    elif contract_name == "CoordinatorShutdownReport":
        values = {
            "remaining_ingress_run_ids": [],
            "remaining_run_ids": [],
            "remaining_attempt_ids": [],
            "remaining_save_job_ids": [],
            "pending_publication_run_ids": [],
        }
        values[identity_field] = duplicate_values
        with pytest.raises(ValueError):
            CoordinatorShutdownReport(
                CoordinatorShutdownStatus.INCOMPLETE,
                save_dispatcher_report=valid_closed_dispatcher_report(),
                errors=[],
                **values,
            )
    else:
        values = {
            "ingress_run_ids": [],
            "accepted_active_run_ids": [],
            "analysis_attempt_ids": [],
            "result_operation_attempt_ids": [],
            "pending_publication_run_ids": [],
        }
        values[identity_field] = duplicate_values
        with pytest.raises(ValueError):
            CoordinatorSnapshot(
                CoordinatorLifecycle.OPEN,
                **values,
            )


@pytest.mark.parametrize("scalar", ["scalar", b"scalar"])
def test_report_iterable_fields_reject_scalar_text_and_bytes(scalar):
    with pytest.raises(ValueError):
        StopReport(
            status=StopStatus.INCOMPLETE,
            pending_ingress_run_ids=scalar,
            pending_publication_run_ids=[],
            canceled_accepted_run_ids=[],
            canceled_reanalysis_attempt_ids=[],
            timed_out=True,
            errors=[],
            pending_analysis_attempt_ids=[],
            restart_required=True,
        )
    with pytest.raises(ValueError):
        SaveDispatcherShutdownReport(
            status=SaveDispatcherShutdownStatus.INCOMPLETE,
            accepted_count=0,
            canceled_count=0,
            completed_count=0,
            remaining_save_job_ids=[],
            live_worker_ids=scalar,
            errors=[],
        )
    with pytest.raises(ValueError):
        StopReport(
            status=StopStatus.INCOMPLETE,
            pending_ingress_run_ids=[RUN_ID],
            pending_publication_run_ids=[],
            canceled_accepted_run_ids=[],
            canceled_reanalysis_attempt_ids=[],
            timed_out=True,
            errors=scalar,
            pending_analysis_attempt_ids=[],
            restart_required=True,
        )


def test_bounded_errors_consumes_exactly_first_32_generator_entries():
    consumed = []

    def guarded_errors():
        for index in range(32):
            consumed.append(index)
            yield f"error-{index}"
        raise AssertionError("the 33rd error must not be consumed")

    report = StopReport(
        status=StopStatus.INCOMPLETE,
        pending_ingress_run_ids=[RUN_ID],
        pending_publication_run_ids=[],
        canceled_accepted_run_ids=[],
        canceled_reanalysis_attempt_ids=[],
        timed_out=True,
        errors=guarded_errors(),
        pending_analysis_attempt_ids=[],
        restart_required=True,
    )

    assert len(report.errors) == 32
    assert consumed == list(range(32))


def test_bounded_errors_accepts_infinite_iterator_promptly():
    report = StopReport(
        status=StopStatus.INCOMPLETE,
        pending_ingress_run_ids=[RUN_ID],
        pending_publication_run_ids=[],
        canceled_accepted_run_ids=[],
        canceled_reanalysis_attempt_ids=[],
        timed_out=True,
        errors=itertools.repeat("error"),
        pending_analysis_attempt_ids=[],
        restart_required=True,
    )

    assert report.errors == ("error",) * 32


@pytest.mark.parametrize(
    (
        "status",
        "pending_ingress",
        "pending_publication",
        "timed_out",
    ),
    [
        (StopStatus.SETTLED, [RUN_ID], [], False),
        (StopStatus.SETTLED, [], [RUN_ID], False),
        (StopStatus.SETTLED, [], [], True),
        (StopStatus.IN_PROGRESS, [], [], True),
        (StopStatus.CLOSED, [], [], True),
        (StopStatus.INCOMPLETE, [], [], False),
    ],
)
def test_stop_report_rejects_incoherent_status_matrix(
    status,
    pending_ingress,
    pending_publication,
    timed_out,
):
    with pytest.raises(ValueError):
        StopReport(
            status=status,
            pending_ingress_run_ids=pending_ingress,
            pending_publication_run_ids=pending_publication,
            canceled_accepted_run_ids=[],
            canceled_reanalysis_attempt_ids=[],
            timed_out=timed_out,
            errors=[],
            pending_analysis_attempt_ids=[],
            restart_required=status is StopStatus.INCOMPLETE,
        )


@pytest.mark.parametrize(
    (
        "status",
        "pending_ingress",
        "pending_publication",
        "timed_out",
    ),
    [
        (StopStatus.SETTLED, [], [], False),
        (StopStatus.IN_PROGRESS, [RUN_ID], [], False),
        (StopStatus.CLOSED, [], [], False),
        (StopStatus.INCOMPLETE, [RUN_ID], [OTHER_RUN_ID], True),
    ],
)
def test_stop_report_accepts_coherent_status_matrix(
    status,
    pending_ingress,
    pending_publication,
    timed_out,
):
    report = StopReport(
        status=status,
        pending_ingress_run_ids=pending_ingress,
        pending_publication_run_ids=pending_publication,
        canceled_accepted_run_ids=[],
        canceled_reanalysis_attempt_ids=[],
        timed_out=timed_out,
        errors=[],
        pending_analysis_attempt_ids=[],
        restart_required=status is StopStatus.INCOMPLETE,
    )

    assert report.status is status


def test_incomplete_stop_report_requires_exact_blocker_evidence():
    with pytest.raises(ValueError):
        StopReport(
            status=StopStatus.INCOMPLETE,
            pending_ingress_run_ids=[],
            pending_publication_run_ids=[],
            canceled_accepted_run_ids=[],
            canceled_reanalysis_attempt_ids=[],
            timed_out=True,
            errors=[],
            pending_analysis_attempt_ids=[],
            restart_required=True,
        )


def test_incomplete_stop_report_accepts_exact_identity_free_error_shape():
    report = StopReport(
        status=StopStatus.INCOMPLETE,
        pending_ingress_run_ids=[],
        pending_publication_run_ids=[],
        canceled_accepted_run_ids=[],
        canceled_reanalysis_attempt_ids=[],
        timed_out=False,
        errors=["stop condition wait: RuntimeError: failed"],
        pending_analysis_attempt_ids=[],
        restart_required=True,
    )

    assert report.pending_ingress_run_ids == ()
    assert report.pending_publication_run_ids == ()
    assert report.canceled_accepted_run_ids == ()
    assert report.canceled_reanalysis_attempt_ids == ()
    assert report.pending_analysis_attempt_ids == ()
    assert report.timed_out is False
    assert report.errors == (
        "stop condition wait: RuntimeError: failed",
    )
    assert report.restart_required is True


@pytest.mark.parametrize(
    ("timed_out", "errors", "restart_required"),
    [
        (False, [], True),
        (False, ["stop condition wait failed"], False),
        (True, ["stop condition wait failed"], True),
    ],
)
def test_incomplete_stop_report_rejects_other_identity_free_shapes(
    timed_out,
    errors,
    restart_required,
):
    with pytest.raises(ValueError):
        StopReport(
            status=StopStatus.INCOMPLETE,
            pending_ingress_run_ids=[],
            pending_publication_run_ids=[],
            canceled_accepted_run_ids=[],
            canceled_reanalysis_attempt_ids=[],
            timed_out=timed_out,
            errors=errors,
            pending_analysis_attempt_ids=[],
            restart_required=restart_required,
        )


@pytest.mark.parametrize("omitted_field", ["errors", "restart_required"])
def test_identity_free_incomplete_stop_report_has_no_defaults(
    omitted_field,
):
    values = {
        "status": StopStatus.INCOMPLETE,
        "pending_ingress_run_ids": [],
        "pending_publication_run_ids": [],
        "canceled_accepted_run_ids": [],
        "canceled_reanalysis_attempt_ids": [],
        "timed_out": False,
        "errors": ["stop condition wait failed"],
        "pending_analysis_attempt_ids": [],
        "restart_required": True,
    }
    values.pop(omitted_field)

    with pytest.raises(TypeError):
        StopReport(**values)


@pytest.mark.parametrize(
    ("evidence_field", "evidence_id"),
    [
        ("pending_ingress_run_ids", RUN_ID),
        ("pending_publication_run_ids", RUN_ID),
        ("canceled_accepted_run_ids", RUN_ID),
        ("canceled_reanalysis_attempt_ids", ATTEMPT_ID),
        ("pending_analysis_attempt_ids", ATTEMPT_ID),
    ],
)
def test_incomplete_stop_report_accepts_each_exact_blocker_evidence(
    evidence_field,
    evidence_id,
):
    values = {
        "pending_ingress_run_ids": [],
        "pending_publication_run_ids": [],
        "canceled_accepted_run_ids": [],
        "canceled_reanalysis_attempt_ids": [],
        "pending_analysis_attempt_ids": [],
    }
    values[evidence_field] = [evidence_id]

    report = StopReport(
        status=StopStatus.INCOMPLETE,
        timed_out=True,
        errors=[],
        restart_required=True,
        **values,
    )

    assert getattr(report, evidence_field) == (evidence_id,)


@pytest.mark.parametrize(
    ("status", "remaining_jobs", "live_workers", "valid"),
    [
        (SaveDispatcherShutdownStatus.CLOSED, [], [], True),
        (SaveDispatcherShutdownStatus.CLOSED, [SAVE_JOB_ID], [], False),
        (SaveDispatcherShutdownStatus.CLOSED, [], ["worker-1"], False),
        (SaveDispatcherShutdownStatus.INCOMPLETE, [], [], False),
        (SaveDispatcherShutdownStatus.INCOMPLETE, [SAVE_JOB_ID], [], True),
        (SaveDispatcherShutdownStatus.INCOMPLETE, [], ["worker-1"], True),
    ],
)
def test_save_dispatcher_shutdown_report_status_matrix(
    status,
    remaining_jobs,
    live_workers,
    valid,
):
    kwargs = dict(
        status=status,
        accepted_count=1 if remaining_jobs else 0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=remaining_jobs,
        live_worker_ids=live_workers,
        errors=[],
    )
    if valid:
        assert SaveDispatcherShutdownReport(**kwargs).status is status
    else:
        with pytest.raises(ValueError):
            SaveDispatcherShutdownReport(**kwargs)


def test_coordinator_shutdown_report_rejects_closed_with_incomplete_dispatcher():
    dispatcher = SaveDispatcherShutdownReport(
        SaveDispatcherShutdownStatus.INCOMPLETE,
        accepted_count=1,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=[SAVE_JOB_ID],
        live_worker_ids=[],
        errors=[],
    )
    with pytest.raises(ValueError):
        CoordinatorShutdownReport(
            CoordinatorShutdownStatus.CLOSED,
            remaining_ingress_run_ids=[],
            remaining_run_ids=[],
            remaining_attempt_ids=[],
            remaining_save_job_ids=[],
            pending_publication_run_ids=[],
            save_dispatcher_report=dispatcher,
            errors=[],
        )


def test_coordinator_shutdown_report_rejects_incomplete_without_blocker():
    dispatcher = SaveDispatcherShutdownReport(
        SaveDispatcherShutdownStatus.CLOSED,
        accepted_count=0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=[],
        live_worker_ids=[],
        errors=[],
    )
    with pytest.raises(ValueError):
        CoordinatorShutdownReport(
            CoordinatorShutdownStatus.INCOMPLETE,
            remaining_ingress_run_ids=[],
            remaining_run_ids=[],
            remaining_attempt_ids=[],
            remaining_save_job_ids=[],
            pending_publication_run_ids=[],
            save_dispatcher_report=dispatcher,
            errors=[],
            restart_required=False,
        )


@pytest.mark.parametrize("blocker", ["run", "dispatcher"])
def test_coordinator_shutdown_report_accepts_exact_incomplete_evidence(blocker):
    dispatcher = SaveDispatcherShutdownReport(
        (
            SaveDispatcherShutdownStatus.INCOMPLETE
            if blocker == "dispatcher"
            else SaveDispatcherShutdownStatus.CLOSED
        ),
        accepted_count=1 if blocker == "dispatcher" else 0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=[SAVE_JOB_ID] if blocker == "dispatcher" else [],
        live_worker_ids=[],
        errors=[],
    )
    report = CoordinatorShutdownReport(
        CoordinatorShutdownStatus.INCOMPLETE,
        remaining_ingress_run_ids=[],
        remaining_run_ids=[RUN_ID] if blocker == "run" else [],
        remaining_attempt_ids=[],
        remaining_save_job_ids=[],
        pending_publication_run_ids=[],
        save_dispatcher_report=dispatcher,
        errors=[],
        restart_required=True,
    )

    assert report.status is CoordinatorShutdownStatus.INCOMPLETE


def test_coordinator_shutdown_report_accepts_error_only_incomplete_evidence():
    dispatcher = SaveDispatcherShutdownReport(
        SaveDispatcherShutdownStatus.CLOSED,
        accepted_count=0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=[],
        live_worker_ids=[],
        errors=["save shutdown failed"],
    )
    report = CoordinatorShutdownReport(
        CoordinatorShutdownStatus.INCOMPLETE,
        remaining_ingress_run_ids=[],
        remaining_run_ids=[],
        remaining_attempt_ids=[],
        remaining_save_job_ids=[],
        pending_publication_run_ids=[],
        save_dispatcher_report=dispatcher,
        errors=["save shutdown failed"],
        restart_required=True,
    )

    assert report.restart_required is True


def test_coordinator_shutdown_report_rejects_closed_restart_required():
    dispatcher = SaveDispatcherShutdownReport(
        SaveDispatcherShutdownStatus.CLOSED,
        accepted_count=0,
        canceled_count=0,
        completed_count=0,
        remaining_save_job_ids=[],
        live_worker_ids=[],
        errors=[],
    )

    with pytest.raises(ValueError):
        CoordinatorShutdownReport(
            CoordinatorShutdownStatus.CLOSED,
            remaining_ingress_run_ids=[],
            remaining_run_ids=[],
            remaining_attempt_ids=[],
            remaining_save_job_ids=[],
            pending_publication_run_ids=[],
            save_dispatcher_report=dispatcher,
            errors=[],
            restart_required=True,
        )


def test_shutdown_and_snapshot_iterables_are_sorted_tuples():
    dispatcher = SaveDispatcherShutdownReport(
        SaveDispatcherShutdownStatus.CLOSED,
        accepted_count=2,
        canceled_count=1,
        completed_count=2,
        remaining_save_job_ids=[],
        live_worker_ids=[],
        errors=[],
    )
    shutdown = CoordinatorShutdownReport(
        CoordinatorShutdownStatus.CLOSED,
        remaining_ingress_run_ids=[],
        remaining_run_ids=[],
        remaining_attempt_ids=[],
        remaining_save_job_ids=[],
        pending_publication_run_ids=[],
        save_dispatcher_report=dispatcher,
        errors=[],
    )
    snapshot = CoordinatorSnapshot(
        CoordinatorLifecycle.OPEN,
        ingress_run_ids=[OTHER_RUN_ID, RUN_ID],
        accepted_active_run_ids=[],
        analysis_attempt_ids=[],
        result_operation_attempt_ids=[],
        pending_publication_run_ids=[],
    )
    assert shutdown.remaining_run_ids == ()
    assert snapshot.ingress_run_ids == (RUN_ID, OTHER_RUN_ID)


def test_closed_status_result_coherence():
    reservation = CaptureReservation("reservation:1", RUN_ID, "hardware:1")
    with pytest.raises(ValueError):
        SaveSubmission(
            SaveSubmitStatus.ACCEPTED,
            RUN_ID,
            SAVE_JOB_ID,
            AudioSaveSourceKind.CAPTURED_AUDIO,
            "resident:1",
            "unexpected",
        )
    with pytest.raises(ValueError):
        SchedulerSubmission(
            SchedulerSubmitStatus.ACCEPTED,
            RUN_ID,
            ATTEMPT_ID,
            AUDIO_ID,
            1,
            "unexpected",
        )
    with pytest.raises(ValueError):
        StartResult(
            RUN_ID,
            StartStatus.ACCEPTED_ACTIVE,
            True,
            CoordinatorPhase.RECORDING,
            PreacceptTerminalFact(
                RUN_ID,
                TriggerSource.MANUAL,
                None,
                TerminalConclusion.ERROR,
                TerminalErrorCategory.PREFLIGHT_REJECTED,
                "failed",
                frozen("labels"),
                datetime.now(timezone.utc),
            ),
            "",
        )
    assert reservation.run_id == RUN_ID


@pytest.mark.parametrize(
    "phase",
    [
        CoordinatorPhase.PREFLIGHT,
        CoordinatorPhase.RESERVING,
        CoordinatorPhase.ACTIVE_COMMITTING,
    ],
)
def test_accepted_active_start_result_rejects_nonactive_phase(phase):
    with pytest.raises(ValueError):
        StartResult(
            RUN_ID,
            StartStatus.ACCEPTED_ACTIVE,
            True,
            phase,
            None,
            "",
        )


@pytest.mark.parametrize(
    ("status", "conclusion", "category", "source"),
    [
        (status, conclusion, category, source)
        for status, (conclusion, category, source) in PREACCEPT_STATUS_FACTS.items()
    ],
)
def test_start_result_accepts_each_exact_preaccept_status_fact(
    status,
    conclusion,
    category,
    source,
):
    terminal = (
        preaccept_capacity_fact(source=source)
        if conclusion is TerminalConclusion.CAPACITY_REJECTED
        else preaccept_error_fact(category, source=source)
    )

    result = StartResult(
        RUN_ID,
        status,
        False,
        CoordinatorPhase.PUBLISHING,
        terminal,
        terminal.reason,
    )

    assert result.terminal is terminal


@pytest.mark.parametrize(
    ("status", "terminal_status"),
    [
        (status, terminal_status)
        for status in PREACCEPT_STATUS_FACTS
        for terminal_status in PREACCEPT_STATUS_FACTS
        if status is not terminal_status
    ],
)
def test_start_result_rejects_every_mismatched_preaccept_status_fact(
    status,
    terminal_status,
):
    conclusion, category, source = PREACCEPT_STATUS_FACTS[terminal_status]
    terminal = (
        preaccept_capacity_fact(source=source)
        if conclusion is TerminalConclusion.CAPACITY_REJECTED
        else preaccept_error_fact(category, source=source)
    )

    with pytest.raises(ValueError):
        StartResult(
            RUN_ID,
            status,
            False,
            CoordinatorPhase.PUBLISHING,
            terminal,
            terminal.reason,
        )


@pytest.mark.parametrize(
    "status",
    [
        StartStatus.INVALID_REQUEST,
        StartStatus.DUPLICATE_RUN_ID,
        StartStatus.INGRESS_CLOSED,
    ],
)
def test_immediate_start_rejection_statuses_require_no_terminal(status):
    with pytest.raises(ValueError):
        StartResult(
            RUN_ID,
            status,
            False,
            CoordinatorPhase.PUBLISHING,
            valid_preaccept_terminal_fact(),
            "failed",
        )


def test_start_result_rejects_mismatched_accepted_terminal_run_id():
    terminal = TerminalFact(
        run_id=OTHER_RUN_ID,
        source=TriggerSource.MANUAL,
        attempt_kind=AnalysisAttemptKind.INITIAL_CAPTURED,
        attempt_id=ATTEMPT_ID,
        conclusion=TerminalConclusion.NO_RESULT,
        error_category=None,
        reason="",
        result_payload=None,
        history_labels=frozen("labels"),
        terminal_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError):
        StartResult(
            RUN_ID,
            StartStatus.ACCEPTED_TERMINAL,
            True,
            CoordinatorPhase.PUBLISHING,
            terminal,
            "",
        )


def test_start_result_rejects_mismatched_preaccept_terminal_run_id():
    terminal = PreacceptTerminalFact(
        run_id=OTHER_RUN_ID,
        source=TriggerSource.MANUAL,
        config_id=None,
        conclusion=TerminalConclusion.ERROR,
        error_category=TerminalErrorCategory.PREFLIGHT_REJECTED,
        reason="failed",
        history_labels=frozen("labels"),
        terminal_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError):
        StartResult(
            RUN_ID,
            StartStatus.PREFLIGHT_REJECTED,
            False,
            CoordinatorPhase.PUBLISHING,
            terminal,
            "failed",
        )


def test_save_cancel_token_has_only_read_only_public_surface():
    token = SaveCancelToken()
    assert token.is_cancel_requested() is False
    assert [name for name in dir(token) if not name.startswith("_")] == [
        "is_cancel_requested"
    ]


def test_package_exports_exact_approved_task3_surface():
    assert len(package.__all__) == len(set(package.__all__))
    assert set(package.__all__) == TASK1_EXPORT_NAMES | {
        "BoundedAudioSaveDispatcher",
        "ManagedResidentCaptureCloseReport",
        "ManagedResidentCaptureCloseStatus",
        "ManagedResidentCapturePort",
        "ManagedResidentCaptureRoute",
        "MultiRunCoordinator",
    }
    assert hasattr(package, "BoundedAudioSaveDispatcher")
    assert hasattr(package, "MultiRunCoordinator")


def assert_protocol_method(protocol, method, parameters, return_type):
    signature = inspect.signature(getattr(protocol, method))
    assert tuple(signature.parameters) == ("self", *parameters)
    hints = get_type_hints(getattr(protocol, method))
    assert hints["return"] == return_type


def test_freezer_and_resident_protocol_signatures():
    assert_protocol_method(
        CapturedRunFreezer, "freeze", ("request",), FrozenCapturedRun
    )
    assert_protocol_method(
        ImportedRunFreezer, "freeze", ("request",), FrozenImportedRun
    )
    assert_protocol_method(
        ResidentCapturePort,
        "reserve",
        ("run_id", "hardware_id"),
        CaptureReserveResult,
    )
    assert_protocol_method(
        ResidentCapturePort,
        "register_completion",
        ("reservation", "sink"),
        type(None),
    )
    assert_protocol_method(
        ResidentCapturePort,
        "start",
        ("reservation", "trigger_monotonic_ns", "target_frame_count"),
        type(None),
    )
    assert_protocol_method(
        ResidentCapturePort, "cancel", ("reservation",), ResidentCancelResult
    )


def test_current_collection_port_surface_is_exact():
    protocol = ports_module.RunCurrentCollector
    methods = {
        name
        for name, member in inspect.getmembers(protocol, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods == {"start", "stop", "samples"}
    assert_protocol_method(protocol, "start", (), bool)
    assert_protocol_method(protocol, "stop", ("timeout",), type(None))
    assert_protocol_method(protocol, "samples", (), list[CurrentSample])
    stop_hints = get_type_hints(protocol.stop)
    assert stop_hints["timeout"] == float | None
    factory_args = get_args(ports_module.CurrentCollectorFactory)
    assert factory_args == (
        [str, FrozenCapturedRun],
        protocol | None,
    )


def test_persistence_protocol_signatures():
    expected = {
        "create_active": (("frozen_run",), CommitResult),
        "commit_initial_terminal": (("terminal_fact",), CommitResult),
        "apply_manual_judgment": (
            ("request", "facts"),
            ManualProjectionResult,
        ),
        "load_result_control_facts": (("run_id",), ResultControlFacts | None),
        "attach_current_trace": (
            ("run_id", "trace"),
            CommitResult,
        ),
    }
    for method, (parameters, return_type) in expected.items():
        assert_protocol_method(
            MultiRunPersistencePort, method, parameters, return_type
        )


def test_saver_dispatcher_and_bridge_protocol_signatures():
    from app.service.run_persistence import (
        BridgeCancelRequest,
        BridgeCancelResult,
        BridgeSubmissionResult,
        InitialAnalysisRequest,
        ReanalysisRequest as BridgeReanalysisRequest,
    )

    assert_protocol_method(
        CapturedAudioSaver,
        "save_captured",
        ("request", "cancel_token"),
        AudioSaveOutcome,
    )
    assert_protocol_method(
        ImportedAudioSaver,
        "save_imported",
        ("request", "cancel_token"),
        AudioSaveOutcome,
    )
    assert_protocol_method(
        AudioSaveDispatcherPort,
        "try_submit",
        ("request", "sink"),
        SaveSubmission,
    )
    assert_protocol_method(
        AudioSaveDispatcherPort,
        "cancel",
        ("run_id", "save_job_id"),
        SaveCancelResult,
    )
    assert_protocol_method(
        AudioSaveDispatcherPort,
        "shutdown",
        ("deadline_monotonic",),
        SaveDispatcherShutdownReport,
    )
    assert_protocol_method(
        AnalysisBridgePort,
        "submit_initial",
        ("request", "terminal_sink"),
        BridgeSubmissionResult,
    )
    assert_protocol_method(
        AnalysisBridgePort,
        "submit_reanalysis",
        ("request", "terminal_sink"),
        BridgeSubmissionResult,
    )
    assert_protocol_method(
        AnalysisBridgePort,
        "cancel",
        ("request",),
        BridgeCancelResult,
    )


def test_history_plc_diagnostic_and_callback_protocol_signatures():
    assert_protocol_method(
        HistoryEventProjector,
        "project",
        ("fact",),
        ProjectedHistoryEvent,
    )
    assert_protocol_method(
        ResultHistoryEventSink,
        "publish",
        ("event",),
        type(None),
    )
    expected_plc = {
        "register": (
            ("run_id", "trigger_source", "operation_kind", "plc_id"),
            PlcRegistrationResult,
        ),
        "mark_accepted": (("ticket",), PlcMutationDisposition),
        "publish_terminal": (
            ("ticket", "payload"),
            PlcMutationDisposition,
        ),
        "publish_rejection": (
            ("ticket", "payload"),
            PlcMutationDisposition,
        ),
    }
    for method, (parameters, return_type) in expected_plc.items():
        assert_protocol_method(PlcSequencerPort, method, parameters, return_type)
    register_signature = inspect.signature(PlcSequencerPort.register)
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in tuple(register_signature.parameters.values())[1:]
    )
    hints = get_type_hints(PlcSequencerPort.publish_terminal)
    assert hints["payload"] == Mapping[str, PlcScalar]
    assert_protocol_method(DiagnosticSink, "record", ("fact",), type(None))
    assert_protocol_method(
        CaptureOutcomeSink, "__call__", ("outcome",), type(None)
    )
    assert_protocol_method(SaveOutcomeSink, "__call__", ("outcome",), type(None))


def test_task3c_reuses_exact_frozen_plc_contract_without_compatibility_aliases():
    assert package.PlcIngressTicket is FrozenPlcIngressTicket
    assert package.PlcMutationDisposition is FrozenPlcMutationDisposition
    assert package.PlcOperationKind is FrozenPlcOperationKind
    assert package.PlcRegistrationResult is FrozenPlcRegistrationResult
    assert not hasattr(package, "PlcSequenceSlot")
    assert not hasattr(package, "PlcIngressRegistration")
    assert not hasattr(package, "PlcIngressRegistrationStatus")
    assert not hasattr(package, "PlcTransitionResult")


def test_task3c_captured_plc_request_has_positive_integer_plc_id():
    request = CapturedStartRequest(
        run_id=RUN_ID,
        trigger_monotonic_ns=1,
        source=TriggerSource.PLC,
        ingress_payload=frozen("ingress"),
        fallback_history_labels=frozen("labels"),
        plc_id=7,
    )

    assert request.plc_id == 7
    for invalid in (None, True, False, 0, -1, "7"):
        with pytest.raises(ValueError):
            CapturedStartRequest(
                run_id=RUN_ID,
                trigger_monotonic_ns=1,
                source=TriggerSource.PLC,
                ingress_payload=frozen("ingress"),
                fallback_history_labels=frozen("labels"),
                plc_id=invalid,
            )


@pytest.mark.parametrize(
    ("enum_name", "expected_members"),
    sorted(ENUM_MEMBERS.items()),
)
def test_every_public_enum_has_exact_closed_members(enum_name, expected_members):
    enum_type = getattr(package, enum_name)
    assert {item.name for item in enum_type} == expected_members


@pytest.mark.parametrize("dataclass_name", sorted(PUBLIC_DATACLASS_NAMES))
def test_every_public_dataclass_is_frozen_and_slotted(dataclass_name):
    dataclass_type = getattr(package, dataclass_name)

    assert dataclasses.is_dataclass(dataclass_type)
    assert dataclass_type.__dataclass_params__.frozen is True
    assert set(dataclass_type.__slots__) == {
        field.name for field in dataclasses.fields(dataclass_type)
    }


def test_introspection_lists_cover_every_public_contract_dataclass_and_enum():
    actual_dataclasses = {
        name
        for name, value in vars(contracts_module).items()
        if (
            not name.startswith("_")
            and dataclasses.is_dataclass(value)
            and value.__module__ == contracts_module.__name__
            and name in package.__all__
        )
    }
    actual_enums = {
        name
        for name, value in vars(contracts_module).items()
        if (
            not name.startswith("_")
            and inspect.isclass(value)
            and value.__module__ == contracts_module.__name__
            and issubclass(value, contracts_module.Enum)
            and name in package.__all__
        )
    }

    assert actual_dataclasses == PUBLIC_DATACLASS_NAMES
    assert actual_enums == set(ENUM_MEMBERS)


def test_numeric_contracts_reject_bool_nan_and_infinity():
    for value in (True, math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            SchedulerSubmission(
                SchedulerSubmitStatus.INVALID,
                RUN_ID,
                ATTEMPT_ID,
                AUDIO_ID,
                value,
                "invalid",
            )


def test_task3b_history_public_surface_is_one_way_and_has_ordered_eviction():
    assert_protocol_method(
        ResultHistoryEventSink,
        "publish",
        ("event",),
        type(None),
    )
    notice_type = package.HistoryEvictionNotice
    assert dataclasses.is_dataclass(notice_type)
    assert notice_type.__dataclass_params__.frozen is True
    assert set(notice_type.__slots__) == {"run_id"}
    assert tuple(inspect.signature(notice_type).parameters) == ("run_id",)
    assert tuple(
        inspect.signature(package.MultiRunCoordinator.history_evicted).parameters
    ) == ("self", "notice")
    assert get_type_hints(package.MultiRunCoordinator.history_evicted) == {
        "notice": notice_type,
        "return": bool,
    }

    obsolete_names = {
        "HistoryPublishStatus",
        "HistoryPublishResult",
        "HistoryPublishCompletion",
        "HistoryPublicationCompletion",
        "BlockingQueuedConnection",
    }
    for module in (package, contracts_module, ports_module):
        assert obsolete_names.isdisjoint(vars(module))
    assert obsolete_names.isdisjoint(package.__all__)


@pytest.mark.parametrize(
    ("kind", "expected_revision", "proposed_revision"),
    [
        (HistoryFactKind.TERMINAL_RESULT_REPLACED, 7, 8),
        (HistoryFactKind.REANALYSIS_STARTED, 7, 8),
        (HistoryFactKind.REANALYSIS_COMPLETED, 8, 9),
        (HistoryFactKind.REANALYSIS_CANCELLED, 8, 9),
    ],
)
def test_task3b_reserves_exact_result_operation_revision_matrix(
    kind,
    expected_revision,
    proposed_revision,
):
    phase = {
        HistoryFactKind.TERMINAL_RESULT_REPLACED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.REANALYSIS_STARTED: ResultProjectionPhase.REANALYZING,
        HistoryFactKind.REANALYSIS_COMPLETED: ResultProjectionPhase.TERMINAL,
        HistoryFactKind.REANALYSIS_CANCELLED: ResultProjectionPhase.TERMINAL,
    }[kind]
    event = ProjectedHistoryEvent(
        kind=kind,
        run_id=RUN_ID,
        expected_revision=expected_revision,
        operation_id=ATTEMPT_ID,
        proposed_revision=proposed_revision,
        projection=ResultProjection(
            phase=phase,
            conclusion=TerminalConclusion.OK,
            error_category=None,
            reason="",
            history_labels=frozen("labels"),
            result_payload=frozen("result"),
        ),
    )

    assert event.expected_revision == expected_revision
    assert event.proposed_revision == proposed_revision


def test_task3_coordinator_constructor_has_no_attempt_id_factory() -> None:
    parameters = inspect.signature(package.MultiRunCoordinator).parameters

    assert "attempt_id_factory" not in parameters


def test_task3_bridge_port_uses_exact_bridge_values() -> None:
    from app.service.run_persistence import (
        BridgeCancelRequest,
        BridgeCancelResult,
        BridgeSubmissionResult,
        CoordinatorAnalysisTerminalReportSink,
        InitialAnalysisRequest,
        ReanalysisRequest as BridgeReanalysisRequest,
    )

    assert get_type_hints(package.AnalysisBridgePort.submit_initial) == {
        "request": InitialAnalysisRequest,
        "terminal_sink": CoordinatorAnalysisTerminalReportSink,
        "return": BridgeSubmissionResult,
    }
    assert get_type_hints(package.AnalysisBridgePort.submit_reanalysis) == {
        "request": BridgeReanalysisRequest,
        "terminal_sink": CoordinatorAnalysisTerminalReportSink,
        "return": BridgeSubmissionResult,
    }
    assert get_type_hints(package.AnalysisBridgePort.cancel) == {
        "request": BridgeCancelRequest,
        "return": BridgeCancelResult,
    }


def test_task3_persistence_port_has_only_retained_authority() -> None:
    methods = {
        name
        for name, member in inspect.getmembers(
            package.MultiRunPersistencePort,
            inspect.isfunction,
        )
        if not name.startswith("_")
    }

    assert methods == {
        "create_active",
        "commit_initial_terminal",
        "apply_manual_judgment",
        "load_result_control_facts",
        "attach_current_trace",
    }


def test_task3_restart_required_is_monotonic_contract_fact() -> None:
    assert tuple(
        field.name for field in dataclasses.fields(CoordinatorSnapshot)
    )[-1] == "restart_required"
    assert tuple(
        field.name for field in dataclasses.fields(CoordinatorShutdownReport)
    )[-1] == "restart_required"


def test_task3_obsolete_scheduler_surface_is_not_public() -> None:
    obsolete = {
        "AnalysisJob",
        "AnalysisOutcome",
        "AnalysisOutcomeSink",
        "AnalysisOutcomeStatus",
        "PreparedAnalysisAttempt",
        "SchedulerCancelResult",
        "SchedulerCancelStatus",
        "SchedulerPersistenceBridgePort",
        "SchedulerSubmission",
        "SchedulerSubmitStatus",
    }

    assert obsolete.isdisjoint(package.__all__)
    assert not hasattr(package, "AnalysisBridgePortAlias")
