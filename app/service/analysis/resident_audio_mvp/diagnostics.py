"""Stable bounded diagnostics for Resident lifecycle decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


MAX_DIAGNOSTIC_DETAIL_CHARS = 256


class DiagnosticStage(str, Enum):
    ADMISSION = "admission"
    MAP_TRIGGER = "map_trigger"
    LIVENESS = "liveness"
    RETIRE_OLD = "retire_old"
    RESOLVE_ENDPOINT = "resolve_endpoint"
    VALIDATE_CANDIDATE = "validate_candidate"
    OPEN_CANDIDATE = "open_candidate"
    START_CANDIDATE = "start_candidate"
    AWAIT_FIRST_SAMPLE = "await_first_sample"
    PUBLISH_CANDIDATE = "publish_candidate"
    BACKGROUND_RETRY = "background_retry"
    SHUTDOWN_CLEANUP = "shutdown_cleanup"
    INTERNAL = "internal"
    LIFECYCLE = "lifecycle"


class DiagnosticReason(str, Enum):
    DUPLICATE_RUN_ID = "duplicate_run_id"
    CAPTURE_CAPACITY = "capture_capacity"
    TRIGGER_FUTURE_INVALID = "trigger_future_invalid"
    TRIGGER_EXPIRED = "trigger_expired"
    TIMELINE_GAP = "timeline_gap"
    HEARTBEAT_STALE = "heartbeat_stale"
    INGRESS_OVERFLOW = "ingress_overflow"
    PORTAUDIO_INPUT_OVERFLOW = "portaudio_input_overflow"
    CALLBACK_CONTINUITY_LOST = "callback_continuity_lost"
    BACKEND_FAULT = "backend_fault"
    ABORT_FAILED = "abort_failed"
    ABORT_TIMEOUT = "abort_timeout"
    STOP_FAILED = "stop_failed"
    STOP_TIMEOUT = "stop_timeout"
    CLOSE_FAILED = "close_failed"
    CLOSE_TIMEOUT = "close_timeout"
    CLOSE_UNCONFIRMED = "close_unconfirmed"
    ENDPOINT_SCAN_FAILED = "endpoint_scan_failed"
    ENDPOINT_MISSING = "endpoint_missing"
    ENDPOINT_AMBIGUOUS = "endpoint_ambiguous"
    ENDPOINT_IDENTITY_MISMATCH = "endpoint_identity_mismatch"
    FORMAT_MISMATCH = "format_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    OPEN_FAILED = "open_failed"
    OPEN_TIMEOUT = "open_timeout"
    START_FAILED = "start_failed"
    START_TIMEOUT = "start_timeout"
    FIRST_SAMPLE_TIMEOUT = "first_sample_timeout"
    CANDIDATE_SUPERSEDED = "candidate_superseded"
    GENERATION_REPLACED_FOR_TRIGGER = "generation_replaced_for_trigger"
    RETRY_SCHEDULED = "retry_scheduled"
    ORPHAN_LIMIT_REACHED = "orphan_limit_reached"
    INVARIANT_VIOLATION = "invariant_violation"
    CLOSING = "closing"


_REASONS_BY_STAGE = {
    DiagnosticStage.ADMISSION: frozenset(
        {
            DiagnosticReason.DUPLICATE_RUN_ID,
            DiagnosticReason.CAPTURE_CAPACITY,
            DiagnosticReason.TRIGGER_FUTURE_INVALID,
        }
    ),
    DiagnosticStage.MAP_TRIGGER: frozenset(
        {DiagnosticReason.TRIGGER_EXPIRED, DiagnosticReason.TIMELINE_GAP}
    ),
    DiagnosticStage.LIVENESS: frozenset(
        {
            DiagnosticReason.HEARTBEAT_STALE,
            DiagnosticReason.INGRESS_OVERFLOW,
            DiagnosticReason.PORTAUDIO_INPUT_OVERFLOW,
            DiagnosticReason.CALLBACK_CONTINUITY_LOST,
            DiagnosticReason.BACKEND_FAULT,
        }
    ),
    DiagnosticStage.RETIRE_OLD: frozenset(
        {
            DiagnosticReason.ABORT_FAILED,
            DiagnosticReason.ABORT_TIMEOUT,
            DiagnosticReason.STOP_FAILED,
            DiagnosticReason.STOP_TIMEOUT,
            DiagnosticReason.CLOSE_FAILED,
            DiagnosticReason.CLOSE_TIMEOUT,
            DiagnosticReason.CLOSE_UNCONFIRMED,
        }
    ),
    DiagnosticStage.RESOLVE_ENDPOINT: frozenset(
        {
            DiagnosticReason.ENDPOINT_SCAN_FAILED,
            DiagnosticReason.ENDPOINT_MISSING,
            DiagnosticReason.ENDPOINT_AMBIGUOUS,
            DiagnosticReason.ENDPOINT_IDENTITY_MISMATCH,
        }
    ),
    DiagnosticStage.VALIDATE_CANDIDATE: frozenset(
        {
            DiagnosticReason.FORMAT_MISMATCH,
            DiagnosticReason.CAPABILITY_MISMATCH,
        }
    ),
    DiagnosticStage.OPEN_CANDIDATE: frozenset(
        {DiagnosticReason.OPEN_FAILED, DiagnosticReason.OPEN_TIMEOUT}
    ),
    DiagnosticStage.START_CANDIDATE: frozenset(
        {DiagnosticReason.START_FAILED, DiagnosticReason.START_TIMEOUT}
    ),
    DiagnosticStage.AWAIT_FIRST_SAMPLE: frozenset(
        {DiagnosticReason.FIRST_SAMPLE_TIMEOUT}
    ),
    DiagnosticStage.PUBLISH_CANDIDATE: frozenset(
        {
            DiagnosticReason.CANDIDATE_SUPERSEDED,
            DiagnosticReason.GENERATION_REPLACED_FOR_TRIGGER,
        }
    ),
    DiagnosticStage.BACKGROUND_RETRY: frozenset(
        {
            DiagnosticReason.RETRY_SCHEDULED,
            DiagnosticReason.ORPHAN_LIMIT_REACHED,
        }
    ),
    DiagnosticStage.SHUTDOWN_CLEANUP: frozenset(
        {DiagnosticReason.CLOSE_UNCONFIRMED}
    ),
    DiagnosticStage.INTERNAL: frozenset(
        {DiagnosticReason.INVARIANT_VIOLATION}
    ),
    DiagnosticStage.LIFECYCLE: frozenset({DiagnosticReason.CLOSING}),
}


def _invalid(summary: str) -> Exception:
    # Imported lazily to keep the diagnostic vocabulary independent while
    # retaining the package's established bounded request-error contract.
    from .contracts import ResidentError, ResidentErrorCode, ResidentRequestError

    return ResidentRequestError(
        ResidentError(code=ResidentErrorCode.INVALID, summary=summary)
    )


@dataclass(frozen=True, slots=True)
class ResidentDiagnostic:
    stage: DiagnosticStage
    reason_code: DiagnosticReason
    detail: str

    def __post_init__(self) -> None:
        if type(self.stage) is not DiagnosticStage:
            raise _invalid("diagnostic stage must be an exact DiagnosticStage")
        if type(self.reason_code) is not DiagnosticReason:
            raise _invalid(
                "diagnostic reason_code must be an exact DiagnosticReason"
            )
        if self.reason_code not in _REASONS_BY_STAGE[self.stage]:
            raise _invalid("diagnostic reason_code does not match its stage")
        if (
            type(self.detail) is not str
            or not self.detail
            or len(self.detail) > MAX_DIAGNOSTIC_DETAIL_CHARS
        ):
            raise _invalid("diagnostic detail must be a non-empty bounded string")
