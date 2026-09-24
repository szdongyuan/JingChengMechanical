"""Pure contracts and mappings for the run-persistence Scheduler bridge."""

from __future__ import annotations
from app.service.analysis.import_mapping import is_import_mapping, shape_import_config, validate_import_mapping

import base64
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
import math
from pathlib import Path
import threading
import time
import zlib
from typing import Callable, Protocol

from app.service.analysis.scheduler import (
    MAX_CANCEL_REASON_UTF8_BYTES,
    MAX_ERROR_SUMMARY_UTF8_BYTES,
    AiToolchainSnapshot,
    AnalysisCancelOrigin,
    AnalysisBundleIdentity,
    AnalysisEnrichmentReport,
    AnalysisError,
    AnalysisErrorCategory,
    AnalysisInputSnapshot,
    AnalysisJob,
    AnalysisKind,
    AnalysisOutcome,
    AnalysisSchedulerConfig,
    AnalysisStage,
    AnalysisStartGateCode,
    AnalysisStartGateError,
    AnalysisStartGateFailure,
    AnalysisStatus,
    AudioStorageKind,
    CancelReceipt,
    CancelStatus,
    FrozenResultObject,
    FrozenResultPayload,
    PersistedAudioReference,
    REQUIRED_ENRICHMENT_CHART_METRICS,
    ShutdownReport,
    ShutdownStatus,
    SubmitReceipt,
    SubmitStatus,
    validate_frozen_result_payload,
    canonical_toolchain_lane_key,
)
from app.service.analysis.staged_result_payload import (
    decode_and_validate_canonical_result,
)
from app.service.run_persistence.attempt_repository import (
    AttemptOutcome,
    AttemptRepository,
)
from app.service.run_persistence.analysis_envelope import (
    AnalysisEnvelopeError,
    canonicalize_json_object,
    decode_analysis_envelope,
)
from app.service.run_persistence.audio_repository import AudioRecord, AudioRepository
from app.service.run_persistence.contracts import (
    AttemptInsert,
    AttemptKind,
    AttemptState,
    ContractValidationError,
    Judgment,
    RunState,
    TransactionResult,
    TransactionState,
    new_uuid4,
    validate_utc_timestamp,
    validate_uuid4,
)
from app.service.run_persistence.database import (
    DatabaseLifecycleCode,
    DatabaseUnavailableError,
    RunPersistenceDatabase,
)
from app.service.run_persistence.run_repository import RunRecord, RunRepository


# Bounded best-effort protection for impossible or late duplicate callbacks.
_TERMINAL_REPLAY_CAPACITY = 64


def _require_exact_enum(value: object, enum_type: type[Enum], name: str) -> None:
    if type(value) is not enum_type:
        raise TypeError(f"{name} must be an exact {enum_type.__name__}")


def _require_exact_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{name} must be an exact bool")


def _identity_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bundle_identity_for_job(job: AnalysisJob) -> AnalysisBundleIdentity:
    return AnalysisBundleIdentity(
        run_id=job.run_id,
        analysis_attempt_id=job.analysis_attempt_id,
        content_revision=job.audio.content_revision,
        algorithm_revision=job.snapshot.algorithm_revision,
        config_identity=_identity_digest(job.snapshot.config_snapshot_json),
        calibration_identity=_identity_digest(
            job.snapshot.calibration_snapshot_json
        ),
        channel_mapping_identity=_identity_digest(
            job.snapshot.channel_mapping_json
        ),
        window_start_seconds=float(job.snapshot.analysis_window_start_seconds),
        window_end_seconds=float(job.snapshot.analysis_window_end_seconds),
    )


def _outcome_replay_digest(outcome: AnalysisOutcome) -> bytes:
    if (
        outcome.status is AnalysisStatus.SUCCEEDED
        and outcome.result_payload is not None
    ):
        try:
            canonical = decode_analysis_result_payload(
                outcome.result_payload
            ).result_payload_json
        except (TypeError, ValueError):
            canonical = ""
        return hashlib.sha256(
            b"SUCCEEDED\0" + canonical.encode("utf-8")
        ).digest()
    return hashlib.sha256(repr(outcome).encode("utf-8")).digest()


def _expected_chart_manifest_for_job(
    job: AnalysisJob,
) -> tuple[tuple[int, tuple[str, ...]], ...] | None:
    try:
        config = json.loads(
            job.snapshot.config_snapshot_json,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if type(config) is not dict:
        return None
    try:
        mapping = json.loads(job.snapshot.channel_mapping_json)
        if is_import_mapping(mapping):
            config = shape_import_config(config, mapping, job.audio.sample_rate_hz, job.audio.channel_count)["chart_config"]
    except (TypeError, ValueError):
        return None
    selected = config.get("selected_channels")
    if (
        type(selected) is not list
        or not selected
        or any(type(channel) is not int or channel < 0 for channel in selected)
        or len(set(selected)) != len(selected)
    ):
        return None
    vibration_channel = config.get("vibration_view_channel_index")
    if vibration_channel is not None and (
        type(vibration_channel) is not int
        or vibration_channel not in selected
    ):
        return None
    vibration_metrics = ("fft", "octave", "stft")
    return tuple(
        (
            channel,
            (
                vibration_metrics
                if channel == vibration_channel
                else REQUIRED_ENRICHMENT_CHART_METRICS
            ),
        )
        for channel in sorted(selected)
    )


def _require_uuid4(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    try:
        return validate_uuid4(value)
    except ContractValidationError:
        raise ContractValidationError(
            f"{name} must be lowercase canonical UUIDv4 text"
        ) from None


def _require_nonempty_string(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact str")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _require_bounded_string(
    value: object,
    name: str,
    maximum_utf8_bytes: int,
) -> str:
    text = _require_nonempty_string(value, name)
    if len(text.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit")
    return text


def _require_optional_bounded_string(
    value: object,
    name: str,
    maximum_utf8_bytes: int = MAX_ERROR_SUMMARY_UTF8_BYTES,
) -> None:
    if value is not None:
        _require_bounded_string(value, name, maximum_utf8_bytes)


def _require_code(value: object) -> str:
    return _require_nonempty_string(value, "code")


def _require_optional_code(value: object) -> None:
    if value is not None:
        _require_code(value)


def _require_optional_uuid4(value: object, name: str) -> None:
    if value is not None:
        _require_uuid4(value, name)


def _validate_selected_analysis_window(
    analysis_window_start_sec: object,
    analysis_window_end_sec: object,
) -> None:
    if type(analysis_window_start_sec) is not float:
        raise TypeError("analysis_window_start_sec must be an exact float")
    if type(analysis_window_end_sec) is not float:
        raise TypeError("analysis_window_end_sec must be an exact float")
    if (
        not math.isfinite(analysis_window_start_sec)
        or not math.isfinite(analysis_window_end_sec)
        or analysis_window_start_sec < 0.0
        or analysis_window_end_sec <= analysis_window_start_sec
    ):
        raise ValueError(
            "analysis window must be finite and satisfy 0 <= start < end"
        )


def _validate_analysis_relationship(
    attempt_kind: AttemptKind,
    scheduler_kind: AnalysisKind,
    storage_kind: AudioStorageKind,
) -> None:
    _require_exact_enum(attempt_kind, AttemptKind, "attempt_kind")
    _require_exact_enum(scheduler_kind, AnalysisKind, "scheduler_kind")
    _require_exact_enum(storage_kind, AudioStorageKind, "storage_kind")
    if attempt_kind is AttemptKind.INITIAL:
        valid = (
            scheduler_kind is AnalysisKind.AUTOMATIC
            and storage_kind is AudioStorageKind.CAPTURED_FINAL_WAV
        ) or (
            scheduler_kind is AnalysisKind.IMPORTED
            and storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO
        )
    else:
        valid = (
            scheduler_kind is AnalysisKind.REANALYSIS
            and storage_kind
            in (
                AudioStorageKind.CAPTURED_FINAL_WAV,
                AudioStorageKind.IMPORTED_FINAL_AUDIO,
            )
        )
    if not valid:
        raise ValueError(
            "attempt kind, Scheduler kind, and audio storage kind are incoherent"
        )


def _validate_transaction_result(result: object) -> TransactionResult[bool]:
    if type(result) is not TransactionResult:
        raise TypeError("persistence_result must be an exact TransactionResult")
    _require_exact_enum(result.state, TransactionState, "persistence_result.state")
    _require_exact_bool(
        result.restart_required,
        "persistence_result.restart_required",
    )
    if result.state is TransactionState.COMMITTED:
        if type(result.value) is not bool:
            raise TypeError("committed persistence result value must be an exact bool")
        if result.reason is not None or result.restart_required:
            raise ValueError("committed persistence result is incoherent")
    elif result.state is TransactionState.ROLLED_BACK:
        if result.value is not None:
            raise ValueError("rolled-back persistence result cannot carry a value")
        _require_nonempty_string(result.reason, "persistence_result.reason")
        if result.restart_required:
            raise ValueError("rolled-back persistence result cannot require restart")
    else:
        if result.value is not None:
            raise ValueError("unknown persistence result cannot carry a value")
        _require_nonempty_string(result.reason, "persistence_result.reason")
        if not result.restart_required:
            raise ValueError("unknown persistence result must require restart")
    return result


class BridgeSubmissionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_EARLY_TERMINAL = "ACCEPTED_EARLY_TERMINAL"
    REJECTED = "REJECTED"
    STATE_UNKNOWN = "STATE_UNKNOWN"


class BridgeAuthorityStatus(str, Enum):
    NOT_ACQUIRED = "NOT_ACQUIRED"
    ACQUIRED = "ACQUIRED"
    UNKNOWN = "UNKNOWN"


_NOT_ACQUIRED_PRE_ALLOCATION_CODES = frozenset(
    (
        "ANALYSIS_ADMISSION_CLOSED",
        "ANALYSIS_DB_CLOSED",
        "ANALYSIS_RUN_NOT_FOUND",
        "ANALYSIS_RUN_NOT_ACTIVE",
        "REANALYSIS_RUN_NOT_FOUND",
        "REANALYSIS_RUN_NOT_TERMINAL",
        "ANALYSIS_AUDIO_NOT_FOUND",
        "ANALYSIS_AUDIO_IDENTITY_MISMATCH",
        "REANALYSIS_AUDIO_UNREADABLE",
        "ANALYSIS_ENVELOPE_INVALID",
        "ANALYSIS_KIND_INVALID",
        "ANALYSIS_QUEUE_TIMESTAMP_FAILED",
    )
)
_NOT_ACQUIRED_CANDIDATE_CODES = frozenset(
    (
        "ANALYSIS_JOB_INVALID",
        "ATTEMPT_QUEUE_DB_FAILED",
    )
)
_QUEUE_UNKNOWN_CODES = frozenset(
    (
        "ATTEMPT_QUEUE_CALL_FAILED",
        "ATTEMPT_QUEUE_RESULT_INVALID",
        "ANALYSIS_DB_STATE_UNKNOWN",
    )
)
_DATABASE_LIFECYCLE_CODES = frozenset(
    code.value for code in DatabaseLifecycleCode
)
_SCHEDULER_REJECTION_CODES = {
    SubmitStatus.INVALID_JOB: "ANALYSIS_INVALID_JOB",
    SubmitStatus.DUPLICATE_ANALYSIS_ATTEMPT_ID:
        "ANALYSIS_DUPLICATE_ATTEMPT_ID",
    SubmitStatus.QUEUE_FULL: "ANALYSIS_QUEUE_FULL",
    SubmitStatus.NOT_ACCEPTING: "ANALYSIS_SCHEDULER_NOT_ACCEPTING",
}
_SCHEDULER_NO_RECEIPT_FAILURE_CODES = frozenset(
    (
        "ANALYSIS_SCHEDULER_SUBMIT_FAILED",
        "SCHEDULER_RECEIPT_INVALID",
        "SCHEDULER_RECEIPT_IDENTITY_MISMATCH",
    )
)
_COMMON_TERMINAL_FAILURE_CODES = frozenset(
    (
        "LATE_OUTCOME_IGNORED",
        "ATTEMPT_TERMINAL_DB_FAILED",
        "ANALYSIS_DB_STATE_UNKNOWN",
        "ATTEMPT_TERMINAL_RESULT_INVALID",
        "ATTEMPT_TERMINAL_CALL_FAILED",
        "ANALYSIS_TERMINAL_TIMESTAMP_FAILED",
    )
) | (_DATABASE_LIFECYCLE_CODES - {"DATABASE_CLOSED"})
_ACCEPTED_EARLY_TERMINAL_FAILURE_CODES = (
    _COMMON_TERMINAL_FAILURE_CODES
    | {
        "ATTEMPT_START_RESULT_INVALID",
        "UNSUPPORTED_SCHEDULER_OUTCOME",
    }
)
_POST_REJECTION_TERMINAL_FAILURE_CODES = _COMMON_TERMINAL_FAILURE_CODES
_TERMINAL_FAILURE_REASON_FORBIDDEN_CODES = frozenset(
    (
        "ATTEMPT_TERMINAL_RESULT_INVALID",
        "ATTEMPT_TERMINAL_CALL_FAILED",
        "ANALYSIS_TERMINAL_TIMESTAMP_FAILED",
        "UNSUPPORTED_SCHEDULER_OUTCOME",
    )
)
_TERMINAL_FAILURE_REASON_REQUIRED_CODES = frozenset(
    ("ATTEMPT_TERMINAL_DB_FAILED", "ATTEMPT_START_RESULT_INVALID")
)
_DEFAULT_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS = (
    AnalysisSchedulerConfig().shutdown_timeout_seconds
)


def _terminal_failure_reason_is_coherent(
    code: str,
    reason: str | None,
    *,
    scheduler_accepted: bool,
) -> bool:
    if code in _TERMINAL_FAILURE_REASON_FORBIDDEN_CODES:
        return reason is None
    if code in _TERMINAL_FAILURE_REASON_REQUIRED_CODES:
        return reason is not None
    if code == "LATE_OUTCOME_IGNORED":
        return scheduler_accepted or reason is None
    return True


class BridgeCancelStatus(str, Enum):
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NOT_FOUND = "NOT_FOUND"
    RUN_ID_MISMATCH = "RUN_ID_MISMATCH"
    REJECTED = "REJECTED"


class BridgeTerminalStatus(str, Enum):
    COMMITTED = "COMMITTED"
    LATE_IGNORED = "LATE_IGNORED"
    DB_FAILED = "DB_FAILED"
    DB_UNKNOWN = "DB_UNKNOWN"
    TERMINAL_WRITE_FAILED = "TERMINAL_WRITE_FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class BridgeSchedulerShutdownStatus(str, Enum):
    SCHEDULER_CLOSED = "SCHEDULER_CLOSED"
    SCHEDULER_IN_PROGRESS = "SCHEDULER_IN_PROGRESS"
    SCHEDULER_INCOMPLETE = "SCHEDULER_INCOMPLETE"


class BridgeDatabaseCloseStatus(str, Enum):
    DATABASE_CLOSED = "DATABASE_CLOSED"
    DATABASE_ALREADY_CLOSED = "DATABASE_ALREADY_CLOSED"
    BLOCKED = "BLOCKED"


class AnalysisResultDecodeError(ValueError):
    """A Scheduler result payload does not satisfy the closed bridge contract."""


class BridgeInputError(ValueError):
    """A facade request does not satisfy the exact bridge input contract."""


class AnalysisEnrichmentPersistenceError(RuntimeError):
    """A valid Enrichment could not be durably committed."""


@dataclass(frozen=True, slots=True)
class DecodedAnalysisResult:
    judgment: Judgment
    result_payload_json: str
    result_payload_zlib: bytes | None = None

    def __post_init__(self) -> None:
        _require_exact_enum(self.judgment, Judgment, "judgment")
        _require_nonempty_string(
            self.result_payload_json,
            "result_payload_json",
        )
        if (
            self.result_payload_zlib is not None
            and (
                type(self.result_payload_zlib) is not bytes
                or not self.result_payload_zlib
            )
        ):
            raise TypeError(
                "result_payload_zlib must be nonempty exact bytes or None"
            )


@dataclass(frozen=True, slots=True)
class EnrichmentCommittedEvent:
    """Immutable non-terminal fact emitted after artifact replacement commits."""

    identity: AnalysisBundleIdentity
    judgment: Judgment
    result_payload_json: str
    terminal_revision: str
    committed_at: str

    def __post_init__(self) -> None:
        if type(self.identity) is not AnalysisBundleIdentity:
            raise TypeError("identity must be an exact AnalysisBundleIdentity")
        _require_exact_enum(self.judgment, Judgment, "judgment")
        _require_nonempty_string(
            self.result_payload_json,
            "result_payload_json",
        )
        if (
            type(self.terminal_revision) is not str
            or len(self.terminal_revision) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.terminal_revision
            )
        ):
            raise ValueError("terminal_revision must be a lowercase sha256 digest")
        validate_utc_timestamp(self.committed_at)


@dataclass(frozen=True, slots=True)
class PreparedAnalysisFacts:
    run_id: str
    audio_id: str
    attempt_kind: AttemptKind
    scheduler_kind: AnalysisKind
    audio_reference: PersistedAudioReference
    snapshot: AnalysisInputSnapshot
    execution_timeout_seconds: float
    queue_timeout_seconds: float | None

    def __post_init__(self) -> None:
        _require_uuid4(self.run_id, "run_id")
        _require_uuid4(self.audio_id, "audio_id")
        _require_exact_enum(self.attempt_kind, AttemptKind, "attempt_kind")
        _require_exact_enum(self.scheduler_kind, AnalysisKind, "scheduler_kind")
        if type(self.audio_reference) is not PersistedAudioReference:
            raise TypeError(
                "audio_reference must be an exact PersistedAudioReference"
            )
        if type(self.snapshot) is not AnalysisInputSnapshot:
            raise TypeError("snapshot must be an exact AnalysisInputSnapshot")
        if self.audio_reference.reference_id != self.audio_id:
            raise ValueError("audio_reference does not match audio_id")
        _validate_analysis_relationship(
            self.attempt_kind,
            self.scheduler_kind,
            self.audio_reference.storage_kind,
        )
        if (
            type(self.execution_timeout_seconds) is not float
            or not math.isfinite(self.execution_timeout_seconds)
            or self.execution_timeout_seconds <= 0.0
        ):
            raise ValueError(
                "execution_timeout_seconds must be a finite positive float"
            )
        if self.queue_timeout_seconds is not None and (
            type(self.queue_timeout_seconds) is not float
            or not math.isfinite(self.queue_timeout_seconds)
            or self.queue_timeout_seconds <= 0.0
        ):
            raise ValueError(
                "queue_timeout_seconds must be a finite positive float or None"
            )


@dataclass(frozen=True, slots=True)
class PreparedAnalysisSubmission:
    attempt_insert: AttemptInsert
    job: AnalysisJob

    def __post_init__(self) -> None:
        if type(self.attempt_insert) is not AttemptInsert:
            raise TypeError("attempt_insert must be an exact AttemptInsert")
        if type(self.job) is not AnalysisJob:
            raise TypeError("job must be an exact AnalysisJob")
        if (
            self.attempt_insert.attempt_id != self.job.analysis_attempt_id
            or self.attempt_insert.run_id != self.job.run_id
            or self.attempt_insert.audio_id != self.job.audio.reference_id
        ):
            raise ValueError("attempt_insert and job identities must match")
        _validate_analysis_relationship(
            self.attempt_insert.attempt_kind,
            self.job.kind,
            self.job.audio.storage_kind,
        )


@dataclass(frozen=True, slots=True)
class InitialAnalysisRequest:
    run_id: str
    audio_reference: PersistedAudioReference

    def __post_init__(self) -> None:
        _require_uuid4(self.run_id, "run_id")
        if type(self.audio_reference) is not PersistedAudioReference:
            raise TypeError(
                "audio_reference must be an exact PersistedAudioReference"
            )


@dataclass(frozen=True, slots=True)
class ReanalysisRequest:
    run_id: str
    analysis_window_start_sec: float
    analysis_window_end_sec: float

    def __post_init__(self) -> None:
        _require_uuid4(self.run_id, "run_id")
        _validate_selected_analysis_window(
            self.analysis_window_start_sec,
            self.analysis_window_end_sec,
        )


@dataclass(frozen=True, slots=True)
class BridgeCancelRequest:
    run_id: str
    analysis_attempt_id: str
    origin: AnalysisCancelOrigin
    reason: str

    def __post_init__(self) -> None:
        _require_uuid4(self.run_id, "run_id")
        _require_uuid4(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.origin, AnalysisCancelOrigin, "origin")
        _require_bounded_string(
            self.reason,
            "reason",
            MAX_CANCEL_REASON_UTF8_BYTES,
        )


@dataclass(frozen=True, slots=True)
class BridgeTerminalReport:
    status: BridgeTerminalStatus
    run_id: str
    analysis_attempt_id: str
    attempt_kind: AttemptKind
    attempt_state: AttemptState | None
    judgment: Judgment | None
    result_payload_json: str | None
    code: str
    summary: str | None
    terminal_at: str | None
    persistence_result: TransactionResult[bool] | None
    restart_required: bool

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, BridgeTerminalStatus, "status")
        _require_uuid4(self.run_id, "run_id")
        _require_uuid4(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.attempt_kind, AttemptKind, "attempt_kind")
        _require_exact_bool(self.restart_required, "restart_required")
        _require_code(self.code)
        _require_optional_bounded_string(self.summary, "summary")
        if self.attempt_state is not None:
            _require_exact_enum(self.attempt_state, AttemptState, "attempt_state")
            if self.attempt_state not in _TERMINAL_ATTEMPT_STATES:
                raise ValueError("attempt_state must be terminal")
        if self.terminal_at is not None:
            validate_utc_timestamp(self.terminal_at)

        self._validate_outcome_payload()
        self._validate_persistence_status()

    def _validate_outcome_payload(self) -> None:
        if self.attempt_state is AttemptState.SUCCEEDED:
            _require_exact_enum(self.judgment, Judgment, "judgment")
            if type(self.result_payload_json) is not str:
                raise TypeError(
                    "successful terminal report requires exact result JSON"
                )
            decoded = _decode_canonical_analysis_result_json(
                self.result_payload_json
            )
            if decoded.judgment is not self.judgment:
                raise ValueError(
                    "successful terminal report judgment does not match result JSON"
                )
        elif self.judgment is not None or self.result_payload_json is not None:
            raise ValueError(
                "non-successful terminal report cannot carry judgment or result"
            )

    def _validate_persistence_status(self) -> None:
        result = self.persistence_result
        if self.status is BridgeTerminalStatus.UNSUPPORTED:
            if (
                result is not None
                or self.attempt_state is not None
                or self.terminal_at is not None
                or self.restart_required
            ):
                raise ValueError("unsupported terminal report is incoherent")
            return

        if self.status is BridgeTerminalStatus.TERMINAL_WRITE_FAILED:
            if result is not None:
                _validate_transaction_result(result)
            if not self.restart_required:
                raise ValueError("terminal write failure must require restart")
            return

        if self.status is BridgeTerminalStatus.DB_UNKNOWN:
            if result is not None:
                checked = _validate_transaction_result(result)
                if checked.state is not TransactionState.UNKNOWN:
                    raise ValueError("DB_UNKNOWN requires UNKNOWN persistence")
            if not self.restart_required:
                raise ValueError("DB_UNKNOWN must require restart")
            return

        if result is None:
            raise ValueError(f"{self.status.value} requires persistence_result")
        checked = _validate_transaction_result(result)
        expected = {
            BridgeTerminalStatus.COMMITTED: (
                TransactionState.COMMITTED,
                True,
            ),
            BridgeTerminalStatus.LATE_IGNORED: (
                TransactionState.COMMITTED,
                False,
            ),
            BridgeTerminalStatus.DB_FAILED: (
                TransactionState.ROLLED_BACK,
                None,
            ),
        }[self.status]
        if checked.state is not expected[0] or (
            expected[1] is not None and checked.value is not expected[1]
        ):
            raise ValueError(
                f"{self.status.value} persistence result is incoherent"
            )
        if self.restart_required:
            raise ValueError(f"{self.status.value} cannot require restart")
        if (
            self.status
            in (BridgeTerminalStatus.COMMITTED, BridgeTerminalStatus.LATE_IGNORED)
            and (self.attempt_state is None or self.terminal_at is None)
        ):
            raise ValueError(
                f"{self.status.value} requires terminal state and timestamp"
            )


_TERMINAL_ATTEMPT_STATES = frozenset(
    (
        AttemptState.SUCCEEDED,
        AttemptState.NO_RESULT,
        AttemptState.FAILED,
        AttemptState.CANCELED,
        AttemptState.INTERRUPTED,
    )
)


@dataclass(frozen=True, slots=True)
class BridgeSubmissionResult:
    status: BridgeSubmissionStatus
    authority: BridgeAuthorityStatus
    run_id: str
    analysis_attempt_id: str | None
    scheduler_accepted: bool
    terminal_delivered_before_return: bool
    restart_required: bool
    code: str | None
    reason: str | None
    submit_receipt: SubmitReceipt | None

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, BridgeSubmissionStatus, "status")
        _require_exact_enum(self.authority, BridgeAuthorityStatus, "authority")
        _require_uuid4(self.run_id, "run_id")
        _require_optional_uuid4(
            self.analysis_attempt_id,
            "analysis_attempt_id",
        )
        _require_exact_bool(self.scheduler_accepted, "scheduler_accepted")
        _require_exact_bool(
            self.terminal_delivered_before_return,
            "terminal_delivered_before_return",
        )
        _require_exact_bool(self.restart_required, "restart_required")
        _require_optional_code(self.code)
        _require_optional_bounded_string(self.reason, "reason")
        if self.submit_receipt is not None and type(self.submit_receipt) is not SubmitReceipt:
            raise TypeError("submit_receipt must be an exact SubmitReceipt or None")
        self._validate_closed_matrix()

    def _validate_closed_matrix(self) -> None:
        receipt = self.submit_receipt
        if receipt is not None:
            try:
                receipt.__post_init__()
            except Exception:
                raise ValueError("submit receipt is invalid") from None
            if (
                receipt.run_id != self.run_id
                or receipt.analysis_attempt_id != self.analysis_attempt_id
            ):
                raise ValueError("submit receipt does not match bridge result")
        accepted_receipt = (
            receipt is not None and receipt.status is SubmitStatus.ACCEPTED
        )
        nonaccepted_receipt = (
            receipt is not None and receipt.status is not SubmitStatus.ACCEPTED
        )

        legal = False
        if self.status is BridgeSubmissionStatus.ACCEPTED:
            legal = (
                self.authority is BridgeAuthorityStatus.ACQUIRED
                and self.analysis_attempt_id is not None
                and self.scheduler_accepted
                and not self.terminal_delivered_before_return
                and not self.restart_required
                and self.code is None
                and self.reason is None
                and accepted_receipt
            )
        elif self.status is BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL:
            legal = (
                self.authority is BridgeAuthorityStatus.ACQUIRED
                and self.analysis_attempt_id is not None
                and self.scheduler_accepted
                and self.terminal_delivered_before_return
                and not self.restart_required
                and self.code is None
                and self.reason is None
                and accepted_receipt
            )
        elif (
            self.status is BridgeSubmissionStatus.REJECTED
            and self.authority is BridgeAuthorityStatus.NOT_ACQUIRED
        ):
            database_failure = self.code in _DATABASE_LIFECYCLE_CODES
            preallocation_failure = (
                self.code in _NOT_ACQUIRED_PRE_ALLOCATION_CODES
            )
            candidate_failure = self.code in _NOT_ACQUIRED_CANDIDATE_CODES
            candidate_reason_coherent = (
                self.code == "ANALYSIS_JOB_INVALID"
                and self.reason is None
            ) or (
                self.code == "ATTEMPT_QUEUE_DB_FAILED"
                and self.reason is not None
            )
            shape_coherent = (
                (
                    database_failure
                    and self.restart_required
                )
                or (
                    preallocation_failure
                    and self.analysis_attempt_id is None
                    and self.restart_required
                    is (self.code == "ANALYSIS_DB_CLOSED")
                    and self.reason is None
                )
                or (
                    candidate_failure
                    and self.analysis_attempt_id is not None
                    and not self.restart_required
                    and candidate_reason_coherent
                )
            )
            legal = (
                not self.scheduler_accepted
                and not self.terminal_delivered_before_return
                and self.code is not None
                and receipt is None
                and shape_coherent
            )
        elif (
            self.status is BridgeSubmissionStatus.STATE_UNKNOWN
            and self.authority is BridgeAuthorityStatus.UNKNOWN
        ):
            legal = (
                self.analysis_attempt_id is not None
                and not self.scheduler_accepted
                and not self.terminal_delivered_before_return
                and self.restart_required
                and self.code is not None
                and self.code in _QUEUE_UNKNOWN_CODES
                and receipt is None
            )
        elif (
            self.status is BridgeSubmissionStatus.REJECTED
            and self.authority is BridgeAuthorityStatus.ACQUIRED
        ):
            rejection_code_coherent = (
                self.code == _SCHEDULER_REJECTION_CODES.get(receipt.status)
                if nonaccepted_receipt
                else (
                    receipt is None
                    and self.code in _SCHEDULER_NO_RECEIPT_FAILURE_CODES
                )
            )
            legal = (
                self.analysis_attempt_id is not None
                and not self.scheduler_accepted
                and self.terminal_delivered_before_return
                and not self.restart_required
                and self.code is not None
                and (receipt is None or nonaccepted_receipt)
                and rejection_code_coherent
                and self.reason is None
            )
        elif (
            self.status is BridgeSubmissionStatus.STATE_UNKNOWN
            and self.authority is BridgeAuthorityStatus.ACQUIRED
        ):
            terminal_codes = (
                _ACCEPTED_EARLY_TERMINAL_FAILURE_CODES
                if self.scheduler_accepted
                else _POST_REJECTION_TERMINAL_FAILURE_CODES
            )
            legal = (
                self.analysis_attempt_id is not None
                and self.terminal_delivered_before_return
                and self.restart_required
                and self.code is not None
                and self.code in terminal_codes
                and _terminal_failure_reason_is_coherent(
                    self.code,
                    self.reason,
                    scheduler_accepted=self.scheduler_accepted,
                )
                and (
                    (self.scheduler_accepted and accepted_receipt)
                    or (
                        not self.scheduler_accepted
                        and (receipt is None or nonaccepted_receipt)
                    )
                )
            )
        if not legal:
            raise ValueError("submission result is outside the closed matrix")


@dataclass(frozen=True, slots=True)
class BridgeCancelResult:
    status: BridgeCancelStatus
    run_id: str
    analysis_attempt_id: str
    origin: AnalysisCancelOrigin
    code: str | None
    reason: str | None
    receipt: CancelReceipt | None

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, BridgeCancelStatus, "status")
        _require_uuid4(self.run_id, "run_id")
        _require_uuid4(self.analysis_attempt_id, "analysis_attempt_id")
        _require_exact_enum(self.origin, AnalysisCancelOrigin, "origin")
        _require_optional_code(self.code)
        _require_optional_bounded_string(self.reason, "reason")
        if self.receipt is not None and type(self.receipt) is not CancelReceipt:
            raise TypeError("receipt must be an exact CancelReceipt or None")

        if self.status is BridgeCancelStatus.REJECTED:
            if self.code is None or self.receipt is not None:
                raise ValueError("REJECTED cancel result is incoherent")
            return
        if self.code is not None or self.reason is not None or self.receipt is None:
            raise ValueError("mapped cancel result is incoherent")
        if (
            self.receipt.run_id != self.run_id
            or self.receipt.analysis_attempt_id != self.analysis_attempt_id
        ):
            raise ValueError("cancel receipt does not match bridge result")
        expected_statuses = {
            BridgeCancelStatus.CANCEL_REQUESTED: (
                CancelStatus.CANCELED_PENDING,
                CancelStatus.CANCEL_REQUESTED_RUNNING,
            ),
            BridgeCancelStatus.ALREADY_TERMINAL: (
                CancelStatus.ALREADY_TERMINAL,
            ),
            BridgeCancelStatus.NOT_FOUND: (CancelStatus.NOT_FOUND,),
            BridgeCancelStatus.RUN_ID_MISMATCH: (
                CancelStatus.RUN_ID_MISMATCH,
            ),
        }[self.status]
        if self.receipt.status not in expected_statuses:
            raise ValueError("cancel receipt status does not match bridge result")


@dataclass(frozen=True, slots=True)
class BridgeSchedulerShutdownResult:
    status: BridgeSchedulerShutdownStatus
    scheduler_report: ShutdownReport
    code: str | None

    def __post_init__(self) -> None:
        _require_exact_enum(
            self.status,
            BridgeSchedulerShutdownStatus,
            "status",
        )
        if type(self.scheduler_report) is not ShutdownReport:
            raise TypeError("scheduler_report must be an exact ShutdownReport")
        _require_optional_code(self.code)
        expected_status = {
            BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED:
                ShutdownStatus.CLOSED,
            BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS:
                ShutdownStatus.IN_PROGRESS,
            BridgeSchedulerShutdownStatus.SCHEDULER_INCOMPLETE:
                ShutdownStatus.SHUTDOWN_INCOMPLETE,
        }[self.status]
        expected_codes = {
            BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED: (None,),
            BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS: (
                "SCHEDULER_IN_PROGRESS",
                "BRIDGE_SUBMIT_IN_FLIGHT",
            ),
            BridgeSchedulerShutdownStatus.SCHEDULER_INCOMPLETE: (
                "SCHEDULER_INCOMPLETE",
            ),
        }[self.status]
        if (
            self.scheduler_report.status is not expected_status
            or self.code not in expected_codes
        ):
            raise ValueError("scheduler shutdown result is incoherent")


@dataclass(frozen=True, slots=True)
class BridgeDatabaseCloseResult:
    status: BridgeDatabaseCloseStatus
    database_closed: bool
    restart_required: bool
    code: str | None

    def __post_init__(self) -> None:
        _require_exact_enum(self.status, BridgeDatabaseCloseStatus, "status")
        _require_exact_bool(self.database_closed, "database_closed")
        _require_exact_bool(self.restart_required, "restart_required")
        _require_optional_code(self.code)
        if self.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED:
            expected = (True, False, None)
            if (
                self.database_closed,
                self.restart_required,
                self.code,
            ) != expected:
                raise ValueError("DATABASE_CLOSED result is incoherent")
        elif self.status is BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED:
            if (
                not self.database_closed
                or not self.restart_required
                or self.code
                not in (
                    "DATABASE_QUARANTINED",
                    "DATABASE_CLOSED_UNEXPECTED",
                )
            ):
                raise ValueError("DATABASE_ALREADY_CLOSED result is incoherent")
        elif (
            self.database_closed
            or self.restart_required
            or self.code != "SCHEDULER_NOT_CLOSED"
        ):
            raise ValueError("BLOCKED database close result is incoherent")


class AnalysisSchedulerPort(Protocol):
    def submit(self, job: AnalysisJob) -> SubmitReceipt: ...

    def cancel(
        self,
        run_id: str,
        analysis_attempt_id: str,
        origin: AnalysisCancelOrigin,
        reason: str,
    ) -> CancelReceipt: ...

    def shutdown(
        self,
        timeout_seconds: float | None = None,
    ) -> ShutdownReport: ...


class CoordinatorAnalysisTerminalReportSink(Protocol):
    def __call__(self, report: BridgeTerminalReport) -> None: ...


class CoordinatorAnalysisEnrichmentCommittedSink(Protocol):
    def __call__(self, event: EnrichmentCommittedEvent) -> None: ...


@dataclass(slots=True)
class _AttemptContext:
    job: AnalysisJob
    attempt_kind: AttemptKind
    terminal_sink: CoordinatorAnalysisTerminalReportSink
    receipt_reconciled: bool = False
    submission_return_prepared: bool = False
    submission_return_linearized: bool = False
    start_in_flight: bool = False
    terminal_claimed: bool = False
    delivery_claimed: bool = False
    held_outcome: AnalysisOutcome | None = None
    cancel_origin: AnalysisCancelOrigin | None = None
    durable_started: bool = False

    def __post_init__(self) -> None:
        if type(self.job) is not AnalysisJob:
            raise TypeError("job must be an exact AnalysisJob")
        _require_exact_enum(self.attempt_kind, AttemptKind, "attempt_kind")
        if not callable(self.terminal_sink):
            raise TypeError("terminal_sink must be callable")


@dataclass(slots=True)
class _CommittedDecision:
    identity: AnalysisBundleIdentity
    attempt_kind: AttemptKind
    judgment: Judgment
    payload_revision: str
    expected_chart_manifest: tuple[tuple[int, tuple[str, ...]], ...]
    enrichment_inflight: bool = False
    enrichment_failure_count: int = 0


@dataclass(frozen=True, slots=True)
class _TerminalReplay:
    run_id: str
    outcome_digest: bytes
    terminal_report: BridgeTerminalReport


class AnalysisPersistenceBoundary:
    """Direct Scheduler start-gate and terminal-outcome Persistence boundary."""

    def __init__(
        self,
        *,
        runtime: RunPersistenceDatabase,
        attempt_repository: AttemptRepository,
        now_utc: Callable[[], str],
        enrichment_sink: CoordinatorAnalysisEnrichmentCommittedSink | None = None,
    ) -> None:
        if runtime is None:
            raise BridgeInputError("runtime is required")
        if attempt_repository is None:
            raise BridgeInputError("attempt_repository is required")
        if not callable(now_utc):
            raise BridgeInputError("now_utc must be callable")
        if enrichment_sink is not None and not callable(enrichment_sink):
            raise BridgeInputError("enrichment_sink must be callable or None")
        self._runtime = runtime
        self._attempt_repository = attempt_repository
        self._now_utc = now_utc
        self._enrichment_sink = enrichment_sink
        self._staged_executor = None
        self._lock = threading.Lock()
        self._submit_condition = threading.Condition(self._lock)
        self._submission_admission_open = True
        self._inflight_submit_claims: set[object] = set()
        self._contexts: dict[str, _AttemptContext] = {}
        self._committed_decisions: dict[str, _CommittedDecision] = {}
        self._terminal_replays: OrderedDict[str, _TerminalReplay] = OrderedDict()
        self._persistence_quarantined = False
        self._quarantine_claimed = False
        self._scheduler_shutdown_result: BridgeSchedulerShutdownResult | None = None
        self._database_close_result: BridgeDatabaseCloseResult | None = None
        self.logger = logging.getLogger("core")

    def attach_staged_executor(self, staged_executor: object) -> None:
        if not callable(
            getattr(staged_executor, "mark_decision_committed", None)
        ) or not callable(
            getattr(staged_executor, "retire_staged_attempt", None)
        ):
            raise TypeError("staged_executor must provide decision lifecycle hooks")
        self._staged_executor = staged_executor

    @property
    def _context_count(self) -> int:
        with self._lock:
            return len(self._contexts)

    @property
    def _pending_enrichment_count(self) -> int:
        with self._lock:
            return len(self._committed_decisions)

    @property
    def _terminal_replay_count(self) -> int:
        with self._lock:
            return len(self._terminal_replays)

    def _register_context(self, context: _AttemptContext) -> None:
        if type(context) is not _AttemptContext:
            raise TypeError("context must be an exact _AttemptContext")
        attempt_id = context.job.analysis_attempt_id
        with self._lock:
            if attempt_id in self._contexts:
                raise RuntimeError("attempt context already registered")
            if context.attempt_kind is AttemptKind.REANALYSIS:
                retired = [
                    decision_attempt_id
                    for decision_attempt_id, decision
                    in self._committed_decisions.items()
                    if decision.identity.run_id == context.job.run_id
                ]
                for decision_attempt_id in retired:
                    self._committed_decisions.pop(decision_attempt_id, None)
            self._contexts[attempt_id] = context

    def _claim_submission(self) -> object | None:
        with self._submit_condition:
            if not self._submission_admission_open:
                return None
            claim = object()
            self._inflight_submit_claims.add(claim)
            return claim

    def _retire_submission(self, claim: object) -> None:
        with self._submit_condition:
            if claim not in self._inflight_submit_claims:
                raise RuntimeError("submit admission claim already retired")
            self._inflight_submit_claims.remove(claim)
            self._submit_condition.notify_all()

    def _close_submission_admission_and_wait(
        self,
        deadline_monotonic: float,
    ) -> bool:
        with self._submit_condition:
            self._submission_admission_open = False
            if not self._inflight_submit_claims:
                return True
            while self._inflight_submit_claims:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._submit_condition.wait(remaining)
            return True

    def _latch_repository_quarantine(self) -> None:
        with self._lock:
            self._persistence_quarantined = True

    def _claim_malformed_quarantine(self) -> bool:
        with self._lock:
            self._persistence_quarantined = True
            if self._quarantine_claimed:
                return False
            self._quarantine_claimed = True
            return True

    def _remove_context(self, attempt_id: str, context: _AttemptContext) -> None:
        with self._lock:
            if self._contexts.get(attempt_id) is context:
                del self._contexts[attempt_id]

    def _discard_context(self, context: _AttemptContext) -> None:
        self._remove_context(context.job.analysis_attempt_id, context)

    def _record_enrichment_failure(
        self,
        attempt_id: str,
        decision: _CommittedDecision,
    ) -> None:
        with self._lock:
            current = self._committed_decisions.get(attempt_id)
            if current is not decision:
                return
            current.enrichment_inflight = False
            current.enrichment_failure_count += 1
            if current.enrichment_failure_count >= 2:
                self._committed_decisions.pop(attempt_id, None)

    def _retire_enrichment(
        self,
        attempt_id: str,
        decision: _CommittedDecision,
    ) -> None:
        with self._lock:
            if self._committed_decisions.get(attempt_id) is decision:
                self._committed_decisions.pop(attempt_id, None)

    def retire_enrichment(
        self,
        identity: AnalysisBundleIdentity,
        reason_code: str,
    ) -> bool:
        """Retire only the exact active Enrichment identity, without a terminal."""

        if type(identity) is not AnalysisBundleIdentity:
            raise TypeError("identity must be an exact AnalysisBundleIdentity")
        _require_code(reason_code)
        attempt_id = identity.analysis_attempt_id
        with self._lock:
            decision = self._committed_decisions.get(attempt_id)
            if decision is None or decision.identity != identity:
                return False
            self._committed_decisions.pop(attempt_id, None)
        self.logger.warning(
            "enrichment retired: "
            f"run_id={identity.run_id}, attempt_id={attempt_id}, "
            f"code={reason_code}"
        )
        return True

    def accept_enrichment(
        self,
        report: AnalysisEnrichmentReport,
    ) -> EnrichmentCommittedEvent | None:
        """Commit a matching full payload without creating another terminal."""

        if type(report) is not AnalysisEnrichmentReport:
            raise TypeError("report must be an exact AnalysisEnrichmentReport")
        if self._persistence_is_unusable():
            return None
        judgment = Judgment(report.judgment)
        attempt_id = report.identity.analysis_attempt_id
        with self._lock:
            decision = self._committed_decisions.get(attempt_id)
            if decision is None or decision.identity != report.identity:
                return None
            if (
                decision.judgment is not judgment
                or decision.expected_chart_manifest != report.chart_manifest
            ):
                raise AnalysisEnrichmentPersistenceError(
                    "enrichment report differs from the committed Decision"
                )
            if decision.enrichment_inflight:
                return None
            decision.enrichment_inflight = True
            expected_revision = decision.payload_revision
        try:
            committed_at = self._now_utc()
            validate_utc_timestamp(committed_at)
            result = self._attempt_repository.replace_succeeded_result(
                attempt_id=attempt_id,
                run_id=report.identity.run_id,
                judgment=judgment,
                expected_payload_revision=expected_revision,
                result_payload_json=report.canonical_result_json,
                result_payload_zlib=report.result_payload_zlib,
                now=committed_at,
            )
            checked = _validate_transaction_result(result)
        except Exception:
            if self._persistence_is_unusable():
                self._retire_enrichment(attempt_id, decision)
            else:
                self._record_enrichment_failure(attempt_id, decision)
            raise

        if checked.state is TransactionState.UNKNOWN:
            self._latch_repository_quarantine()
            self._retire_enrichment(attempt_id, decision)
            raise AnalysisEnrichmentPersistenceError(
                "enrichment database state is unknown"
            )
        if checked.state is TransactionState.ROLLED_BACK:
            self._record_enrichment_failure(attempt_id, decision)
            raise AnalysisEnrichmentPersistenceError(
                checked.reason or "enrichment database update rolled back"
            )
        if checked.value is not True:
            self._retire_enrichment(attempt_id, decision)
            return None
        self._retire_enrichment(attempt_id, decision)
        event = EnrichmentCommittedEvent(
            identity=report.identity,
            judgment=judgment,
            result_payload_json=report.canonical_result_json,
            terminal_revision=expected_revision,
            committed_at=committed_at,
        )
        sink = self._enrichment_sink
        if sink is not None:
            try:
                sink(event)
            except Exception:
                pass
        return event

    def _remember_committed_decision(
        self,
        context: _AttemptContext,
        scheduler_outcome: AnalysisOutcome,
        report: BridgeTerminalReport,
    ) -> bool:
        if (
            report.status is not BridgeTerminalStatus.COMMITTED
            or report.attempt_state is not AttemptState.SUCCEEDED
            or report.judgment is None
            or report.result_payload_json is None
        ):
            return False
        try:
            decode_and_validate_canonical_result(
                report.result_payload_json,
                "decision result_payload_json",
                require_empty_chart_payload=True,
            )
        except (TypeError, ValueError):
            return False
        payload_revision = hashlib.sha256(
            report.result_payload_json.encode("utf-8")
        ).hexdigest()
        identity = _bundle_identity_for_job(context.job)
        expected_chart_manifest = _expected_chart_manifest_for_job(context.job)
        decision = (
            None
            if expected_chart_manifest is None
            else _CommittedDecision(
                identity=identity,
                attempt_kind=context.attempt_kind,
                judgment=report.judgment,
                payload_revision=payload_revision,
                expected_chart_manifest=expected_chart_manifest,
            )
        )
        replay = _TerminalReplay(
            run_id=context.job.run_id,
            outcome_digest=_outcome_replay_digest(scheduler_outcome),
            terminal_report=report,
        )
        with self._lock:
            attempt_id = identity.analysis_attempt_id
            if decision is not None:
                self._committed_decisions[attempt_id] = decision
            self._terminal_replays[attempt_id] = replay
            while len(self._terminal_replays) > _TERMINAL_REPLAY_CAPACITY:
                self._terminal_replays.popitem(last=False)
        return decision is not None

    def _settle_staged_decision(
        self,
        attempt_id: str,
        report: BridgeTerminalReport,
        *,
        committed_decision: bool,
    ) -> None:
        staged_executor = self._staged_executor
        if staged_executor is None:
            return
        try:
            if committed_decision:
                staged_executor.mark_decision_committed(attempt_id)
            else:
                staged_executor.retire_staged_attempt(
                    attempt_id,
                    report.code,
                )
        except Exception:
            self.logger.exception(
                "staged analysis lifecycle failed: attempt_id=%s",
                attempt_id,
            )

    def _reconcile_accepted(
        self,
        job: AnalysisJob,
    ) -> tuple[_AttemptContext, AnalysisOutcome] | None:
        with self._lock:
            context = self._contexts.get(job.analysis_attempt_id)
            if context is None or context.job is not job:
                raise RuntimeError("active attempt context identity changed")
            context.receipt_reconciled = True
            if context.held_outcome is None:
                return None
            outcome = context.held_outcome
            context.held_outcome = None
            context.terminal_claimed = True
            return context, outcome

    def _linearize_accepted_return(
        self,
        job: AnalysisJob,
    ) -> tuple[_AttemptContext, AnalysisOutcome] | None:
        with self._lock:
            context = self._contexts.get(job.analysis_attempt_id)
            if (
                context is None
                or context.job is not job
                or not context.receipt_reconciled
                or context.terminal_claimed
                or context.submission_return_prepared
                or context.submission_return_linearized
            ):
                raise RuntimeError("accepted return context is invalid")
            if context.held_outcome is None:
                context.submission_return_prepared = True
                return None
            outcome = context.held_outcome
            context.held_outcome = None
            context.terminal_claimed = True
            return context, outcome

    def _complete_accepted_return(
        self,
        job: AnalysisJob,
    ) -> tuple[_AttemptContext, AnalysisOutcome] | None:
        with self._lock:
            context = self._contexts.get(job.analysis_attempt_id)
            if (
                context is None
                or context.job is not job
                or not context.receipt_reconciled
                or not context.submission_return_prepared
                or context.terminal_claimed
                or context.submission_return_linearized
            ):
                raise RuntimeError("accepted return context is invalid")
            if context.held_outcome is None:
                context.submission_return_linearized = True
                return None
            outcome = context.held_outcome
            context.held_outcome = None
            context.terminal_claimed = True
            return context, outcome

    def _claim_direct_submit_failure(
        self,
        job: AnalysisJob,
    ) -> bool:
        with self._submit_condition:
            context = self._contexts.get(job.analysis_attempt_id)
            if (
                context is None
                or context.job is not job
                or context.terminal_claimed
            ):
                raise RuntimeError("direct submit failure context is invalid")
            context.terminal_claimed = True
            context.held_outcome = None
            while context.start_in_flight:
                self._submit_condition.wait()
            return context.durable_started

    def before_audio_load(self, job: AnalysisJob) -> None:
        if type(job) is not AnalysisJob:
            self._raise_start_gate(
                AnalysisStartGateCode.ATTEMPT_START_CONTEXT_INVALID,
                "analysis attempt start context is invalid",
            )
        with self._lock:
            context = self._contexts.get(job.analysis_attempt_id)
            if (
                context is None
                or context.job is not job
                or context.terminal_claimed
                or context.start_in_flight
                or context.durable_started
            ):
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_CONTEXT_INVALID,
                    "analysis attempt start context is invalid",
                )
            if self._persistence_quarantined:
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN,
                    "analysis attempt start database state is unknown",
                )
            context.start_in_flight = True
        try:
            try:
                started_at = self._now_utc()
                validate_utc_timestamp(started_at)
            except Exception:
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_TIMESTAMP_FAILED,
                    "analysis attempt start timestamp failed",
                )
            try:
                result = self._attempt_repository.mark_running(
                    job.analysis_attempt_id,
                    started_at,
                )
            except Exception:
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_GATE_EXCEPTION,
                    "analysis start gate failed",
                )
            if type(result) is not TransactionResult:
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_RESULT_INVALID,
                    "analysis attempt start result is invalid",
                )
            try:
                checked = _validate_transaction_result(result)
            except (TypeError, ValueError):
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_RESULT_INVALID,
                    "analysis attempt start result is invalid",
                )
            if checked.state is TransactionState.ROLLED_BACK:
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_DB_FAILED,
                    _bounded_reason(checked.reason)
                    or "analysis attempt start database write failed",
                )
            if checked.state is TransactionState.UNKNOWN:
                self._latch_repository_quarantine()
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN,
                    "analysis attempt start database state is unknown",
                )
            if checked.value is not True:
                self._raise_start_gate(
                    AnalysisStartGateCode.ATTEMPT_START_NOT_QUEUED,
                    "analysis attempt is not durably queued",
                )
            with self._lock:
                active = self._contexts.get(job.analysis_attempt_id)
                if active is not context or active.job is not job:
                    self._raise_start_gate(
                        AnalysisStartGateCode.ATTEMPT_START_CONTEXT_INVALID,
                        "analysis attempt start context is invalid",
                    )
                active.durable_started = True
                self.logger.info(
                    f"开始分析: run_id={job.run_id}, "
                    f"attempt_id={job.analysis_attempt_id}"
                )
        finally:
            with self._submit_condition:
                context.start_in_flight = False
                self._submit_condition.notify_all()

    @staticmethod
    def _raise_start_gate(
        code: AnalysisStartGateCode,
        summary: str,
    ) -> None:
        raise AnalysisStartGateError(
            AnalysisStartGateFailure(
                code=code,
                summary=summary,
            )
        )

    def __call__(self, outcome: AnalysisOutcome) -> BridgeTerminalReport | None:
        if type(outcome) is not AnalysisOutcome:
            return None
        claimed: tuple[_AttemptContext, AnalysisOutcome] | None = None
        try:
            with self._lock:
                context = self._contexts.get(outcome.analysis_attempt_id)
                if context is None:
                    replay = self._terminal_replays.get(
                        outcome.analysis_attempt_id
                    )
                    if (
                        replay is not None
                        and replay.run_id == outcome.run_id
                        and replay.outcome_digest
                        == _outcome_replay_digest(outcome)
                    ):
                        return replay.terminal_report
                    return None
                if (
                    context.job.run_id != outcome.run_id
                    or context.job.kind is not outcome.kind
                    or context.job.audio.reference_id
                    != outcome.audio_reference_id
                    or context.terminal_claimed
                ):
                    return None
                if (
                    not context.receipt_reconciled
                    or not context.submission_return_linearized
                ):
                    if context.held_outcome is None:
                        context.held_outcome = outcome
                    return None
                context.terminal_claimed = True
                claimed = context, outcome
            report = self._process_claimed_outcome(*claimed)
            self._deliver_terminal_once(context, report)
            return report
        except Exception:
            if claimed is not None:
                context, claimed_outcome = claimed
                staged_executor = self._staged_executor
                if staged_executor is not None:
                    try:
                        staged_executor.retire_staged_attempt(
                            claimed_outcome.analysis_attempt_id,
                            "PERSISTENCE_BOUNDARY_FAILED",
                        )
                    except Exception:
                        pass
                self._remove_context(
                    claimed_outcome.analysis_attempt_id,
                    context,
                )
        return None

    def _deliver_terminal_once(
        self,
        context: _AttemptContext,
        report: BridgeTerminalReport,
    ) -> bool:
        with self._lock:
            if context.delivery_claimed:
                return False
            context.delivery_claimed = True
        try:
            context.terminal_sink(report)
        except Exception:
            pass
        if context.durable_started:
            status = (
                report.attempt_state.value
                if report.attempt_state is not None
                else report.status.value
            )
            self.logger.info(
                f"结束分析: run_id={context.job.run_id}, 状态={status}"
            )
        self._discard_context(context)
        return True

    def _process_claimed_outcome(
        self,
        context: _AttemptContext,
        outcome: AnalysisOutcome,
    ) -> BridgeTerminalReport:
        if not self._scheduler_outcome_is_coherent(outcome):
            report = self._unsupported_scheduler_outcome(context)
            self._settle_staged_decision(
                context.job.analysis_attempt_id,
                report,
                committed_decision=False,
            )
            return report
        if (
            outcome.status is AnalysisStatus.FAILED
            and outcome.error is not None
            and outcome.error.category
            is AnalysisErrorCategory.ATTEMPT_START_FAILED
            and outcome.error.details
            == (
                (
                    "code",
                    AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN.value,
                ),
            )
            and not context.durable_started
        ):
            self._latch_repository_quarantine()
            report = self._terminal_failure_report(
                context,
                status=BridgeTerminalStatus.DB_UNKNOWN,
                code="ANALYSIS_DB_STATE_UNKNOWN",
                summary=outcome.error.summary,
                restart_required=True,
            )
            self._settle_staged_decision(
                context.job.analysis_attempt_id,
                report,
                committed_decision=False,
            )
            return report
        start_result_invalid = (
            outcome.status is AnalysisStatus.FAILED
            and outcome.error is not None
            and outcome.error.category
            is AnalysisErrorCategory.ATTEMPT_START_FAILED
            and outcome.error.details
            == (
                (
                    "code",
                    AnalysisStartGateCode.ATTEMPT_START_RESULT_INVALID.value,
                ),
            )
            and not context.durable_started
        )
        mapped = self._map_scheduler_outcome(context, outcome)
        if mapped is not None:
            persistence_outcome, queued = mapped
            report = self._write_terminal(
                context,
                persistence_outcome,
                queued=queued,
            )
            if start_result_invalid:
                report = BridgeTerminalReport(
                    status=BridgeTerminalStatus.DB_UNKNOWN,
                    run_id=context.job.run_id,
                    analysis_attempt_id=context.job.analysis_attempt_id,
                    attempt_kind=context.attempt_kind,
                    attempt_state=None,
                    judgment=None,
                    result_payload_json=None,
                    code="ATTEMPT_START_RESULT_INVALID",
                    summary=outcome.error.summary,
                    terminal_at=report.terminal_at,
                    persistence_result=None,
                    restart_required=True,
                )
                self._settle_staged_decision(
                    context.job.analysis_attempt_id,
                    report,
                    committed_decision=False,
                )
                return report
            committed_decision = self._remember_committed_decision(
                context,
                outcome,
                report,
            )
            self._settle_staged_decision(
                context.job.analysis_attempt_id,
                report,
                committed_decision=committed_decision,
            )
            return report
        report = self._unsupported_scheduler_outcome(context)
        self._settle_staged_decision(
            context.job.analysis_attempt_id,
            report,
            committed_decision=False,
        )
        return report

    @staticmethod
    def _scheduler_outcome_is_coherent(outcome: AnalysisOutcome) -> bool:
        try:
            if outcome.error is not None:
                AnalysisError.__post_init__(outcome.error)
            AnalysisOutcome.__post_init__(outcome)
        except Exception:
            return False
        return True

    def _unsupported_scheduler_outcome(
        self,
        context: _AttemptContext,
    ) -> BridgeTerminalReport:
        report = BridgeTerminalReport(
            status=BridgeTerminalStatus.UNSUPPORTED,
            run_id=context.job.run_id,
            analysis_attempt_id=context.job.analysis_attempt_id,
            attempt_kind=context.attempt_kind,
            attempt_state=None,
            judgment=None,
            result_payload_json=None,
            code="UNSUPPORTED_SCHEDULER_OUTCOME",
            summary=None,
            terminal_at=None,
            persistence_result=None,
            restart_required=False,
        )
        return report

    def _map_scheduler_outcome(
        self,
        context: _AttemptContext,
        outcome: AnalysisOutcome,
    ) -> tuple[AttemptOutcome, bool] | None:
        started = context.durable_started
        if outcome.status is AnalysisStatus.SUCCEEDED:
            if not started:
                return None
            try:
                decoded = decode_analysis_result_payload(outcome.result_payload)
            except Exception:
                return (
                    AttemptOutcome(
                        state=AttemptState.FAILED,
                        judgment=None,
                        result_payload_json=None,
                        code="ANALYSIS_RESULT_PAYLOAD_INVALID",
                        summary="analysis result payload is invalid",
                    ),
                    False,
                )
            return (
                AttemptOutcome(
                    state=AttemptState.SUCCEEDED,
                    judgment=decoded.judgment,
                    result_payload_json=decoded.result_payload_json,
                    code=None,
                    summary=None,
                    result_payload_zlib=decoded.result_payload_zlib,
                ),
                False,
            )
        if outcome.status is AnalysisStatus.NO_RESULT:
            if not started or outcome.error is None:
                return None
            return (
                AttemptOutcome(
                    state=AttemptState.NO_RESULT,
                    judgment=None,
                    result_payload_json=None,
                    code="ANALYSIS_NO_RESULT",
                    summary=outcome.error.summary,
                ),
                False,
            )
        if outcome.status is AnalysisStatus.FAILED:
            if outcome.error is None:
                return None
            category = outcome.error.category
            if category is AnalysisErrorCategory.QUEUE_WAIT_TIMEOUT:
                if started:
                    return None
                code = "ANALYSIS_QUEUE_TIMEOUT"
                queued = True
            elif category is AnalysisErrorCategory.ATTEMPT_START_FAILED:
                if started or outcome.error.stage is not AnalysisStage.START_GATE:
                    return None
                details = outcome.error.details
                if (
                    len(details) != 1
                    or details[0][0] != "code"
                    or details[0][1]
                    not in {item.value for item in AnalysisStartGateCode}
                ):
                    return None
                code = details[0][1]
                if code == AnalysisStartGateCode.ATTEMPT_START_DB_UNKNOWN.value:
                    self._latch_repository_quarantine()
                    return None
                queued = True
            else:
                running_codes = {
                    AnalysisErrorCategory.AUDIO_REFERENCE_UNREADABLE:
                        "AUDIO_REFERENCE_UNREADABLE",
                    AnalysisErrorCategory.AUDIO_REFERENCE_CHANGED:
                        "AUDIO_REFERENCE_CHANGED",
                    AnalysisErrorCategory.AUDIO_FORMAT_MISMATCH:
                        "AUDIO_FORMAT_MISMATCH",
                    AnalysisErrorCategory.AUDIO_LOAD_FAILED:
                        "AUDIO_LOAD_FAILED",
                    AnalysisErrorCategory.ANALYSIS_TIMEOUT:
                        "ANALYSIS_TIMEOUT",
                    AnalysisErrorCategory.ANALYSIS_EXECUTION_FAILED:
                        "ANALYSIS_EXECUTION_FAILED",
                }
                if not started or category not in running_codes:
                    return None
                code = running_codes[category]
                queued = False
            return (
                AttemptOutcome(
                    state=AttemptState.FAILED,
                    judgment=None,
                    result_payload_json=None,
                    code=code,
                    summary=outcome.error.summary,
                ),
                queued,
            )
        if outcome.status is AnalysisStatus.CANCELED:
            origin = outcome.cancel_origin
            if origin is AnalysisCancelOrigin.USER_STOP:
                code = (
                    "USER_CANCELED"
                    if context.attempt_kind is AttemptKind.INITIAL
                    else "REANALYSIS_CANCELED"
                )
            elif origin is AnalysisCancelOrigin.RESULT_EVICTION:
                if context.attempt_kind is not AttemptKind.REANALYSIS:
                    return None
                code = "REANALYSIS_EVICTED"
            elif origin is AnalysisCancelOrigin.SCHEDULER_SHUTDOWN:
                code = "ANALYSIS_SCHEDULER_SHUTDOWN"
            else:
                return None
            return (
                AttemptOutcome(
                    state=AttemptState.CANCELED,
                    judgment=None,
                    result_payload_json=None,
                    code=code,
                    summary=(
                        outcome.error.summary
                        if outcome.error is not None
                        else None
                    ),
                ),
                not started,
            )
        return None

    def _write_terminal(
        self,
        context: _AttemptContext,
        outcome: AttemptOutcome,
        *,
        queued: bool,
    ) -> BridgeTerminalReport:
        job = context.job
        if self._persistence_is_unusable():
            report = self._terminal_failure_report(
                context,
                status=BridgeTerminalStatus.DB_UNKNOWN,
                code="ANALYSIS_DB_STATE_UNKNOWN",
                restart_required=True,
            )
            return report
        try:
            terminal_at = self._now_utc()
            validate_utc_timestamp(terminal_at)
        except Exception:
            report = self._terminal_failure_report(
                context,
                status=BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
                code="ANALYSIS_TERMINAL_TIMESTAMP_FAILED",
                restart_required=True,
            )
            return report
        try:
            if queued:
                result = self._attempt_repository.finish_queued_and_project(
                    job.analysis_attempt_id,
                    outcome,
                    terminal_at,
                )
            else:
                result = self._attempt_repository.finish_running_and_project(
                    job.analysis_attempt_id,
                    outcome,
                    terminal_at,
                )
        except DatabaseUnavailableError as exc:
            if exc.code is DatabaseLifecycleCode.DATABASE_CLOSED:
                status = BridgeTerminalStatus.DB_UNKNOWN
                code = "ANALYSIS_DB_STATE_UNKNOWN"
            else:
                status = BridgeTerminalStatus.TERMINAL_WRITE_FAILED
                code = exc.code.value
            report = self._terminal_failure_report(
                context,
                status=status,
                code=code,
                summary=_bounded_reason(exc.args[0] if exc.args else None),
                terminal_at=terminal_at,
                restart_required=True,
            )
            return report
        except Exception:
            report = self._terminal_failure_report(
                context,
                status=BridgeTerminalStatus.TERMINAL_WRITE_FAILED,
                code="ATTEMPT_TERMINAL_CALL_FAILED",
                terminal_at=terminal_at,
                restart_required=True,
            )
            return report

        checked: TransactionResult[bool] | None
        if type(result) is not TransactionResult:
            checked = None
        else:
            try:
                checked = _validate_transaction_result(result)
            except (TypeError, ValueError):
                checked = None
        if checked is None:
            quarantine = self._claim_malformed_quarantine()
            if quarantine:
                try:
                    self._runtime.quarantine_unknown()
                except Exception:
                    pass
            report = self._terminal_failure_report(
                context,
                status=BridgeTerminalStatus.DB_UNKNOWN,
                code="ATTEMPT_TERMINAL_RESULT_INVALID",
                terminal_at=terminal_at,
                restart_required=True,
            )
        elif checked.state is TransactionState.COMMITTED:
            if checked.value:
                status = BridgeTerminalStatus.COMMITTED
                code = outcome.code or "ANALYSIS_SUCCEEDED"
            else:
                status = BridgeTerminalStatus.LATE_IGNORED
                code = "LATE_OUTCOME_IGNORED"
            report = BridgeTerminalReport(
                status=status,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                attempt_kind=context.attempt_kind,
                attempt_state=outcome.state,
                judgment=outcome.judgment,
                result_payload_json=outcome.result_payload_json,
                code=code,
                summary=outcome.summary,
                terminal_at=terminal_at,
                persistence_result=checked,
                restart_required=False,
            )
        elif checked.state is TransactionState.ROLLED_BACK:
            report = BridgeTerminalReport(
                status=BridgeTerminalStatus.DB_FAILED,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                attempt_kind=context.attempt_kind,
                attempt_state=outcome.state,
                judgment=outcome.judgment,
                result_payload_json=outcome.result_payload_json,
                code="ATTEMPT_TERMINAL_DB_FAILED",
                summary=_bounded_reason(checked.reason),
                terminal_at=terminal_at,
                persistence_result=checked,
                restart_required=False,
            )
        else:
            self._latch_repository_quarantine()
            report = BridgeTerminalReport(
                status=BridgeTerminalStatus.DB_UNKNOWN,
                run_id=job.run_id,
                analysis_attempt_id=job.analysis_attempt_id,
                attempt_kind=context.attempt_kind,
                attempt_state=outcome.state,
                judgment=outcome.judgment,
                result_payload_json=outcome.result_payload_json,
                code="ANALYSIS_DB_STATE_UNKNOWN",
                summary=_bounded_reason(checked.reason),
                terminal_at=terminal_at,
                persistence_result=checked,
                restart_required=True,
            )
        return report

    def _persistence_is_unusable(self) -> bool:
        with self._lock:
            quarantined = self._persistence_quarantined
        if quarantined:
            return True
        try:
            return self._runtime.closed
        except Exception:
            return True

    @staticmethod
    def _terminal_failure_report(
        context: _AttemptContext,
        *,
        status: BridgeTerminalStatus,
        code: str,
        restart_required: bool,
        summary: str | None = None,
        terminal_at: str | None = None,
    ) -> BridgeTerminalReport:
        return BridgeTerminalReport(
            status=status,
            run_id=context.job.run_id,
            analysis_attempt_id=context.job.analysis_attempt_id,
            attempt_kind=context.attempt_kind,
            attempt_state=None,
            judgment=None,
            result_payload_json=None,
            code=code,
            summary=summary,
            terminal_at=terminal_at,
            persistence_result=None,
            restart_required=restart_required,
        )


class RunPersistenceSchedulerBridge:
    """Synchronous facade that durably queues work before Scheduler admission."""

    def __init__(
        self,
        *,
        runtime: RunPersistenceDatabase,
        run_repository: RunRepository,
        audio_repository: AudioRepository,
        attempt_repository: AttemptRepository,
        boundary: AnalysisPersistenceBoundary,
        scheduler: AnalysisSchedulerPort,
        now_utc: Callable[[], str],
    ) -> None:
        if (
            runtime is None
            or run_repository is None
            or audio_repository is None
            or attempt_repository is None
            or boundary is None
            or scheduler is None
        ):
            raise BridgeInputError("all bridge dependencies are required")
        if type(boundary) is not AnalysisPersistenceBoundary:
            raise BridgeInputError("boundary must be an exact AnalysisPersistenceBoundary")
        if not callable(now_utc):
            raise BridgeInputError("now_utc must be callable")
        if boundary._runtime is not runtime:
            raise BridgeInputError("boundary and facade runtime must be identical")
        if boundary._attempt_repository is not attempt_repository:
            raise BridgeInputError(
                "boundary and facade attempt repository must be identical"
            )
        self.runtime = runtime
        self.run_repository = run_repository
        self.audio_repository = audio_repository
        self.attempt_repository = attempt_repository
        self.boundary = boundary
        self.scheduler = scheduler
        self.now_utc = now_utc

    def submit_initial(
        self,
        request: InitialAnalysisRequest,
        terminal_sink: CoordinatorAnalysisTerminalReportSink,
    ) -> BridgeSubmissionResult:
        if not callable(terminal_sink):
            raise BridgeInputError("terminal_sink must be callable")
        if type(request) is not InitialAnalysisRequest:
            raise BridgeInputError("request must be an exact InitialAnalysisRequest")
        try:
            request.__post_init__()
        except Exception:
            raise BridgeInputError(
                "initial analysis request fields are invalid"
            ) from None
        claim = self.boundary._claim_submission()
        if claim is None:
            return self._pre_attempt_rejection(
                request.run_id,
                "ANALYSIS_ADMISSION_CLOSED",
            )
        try:
            return self._submit(
                run_id=request.run_id,
                attempt_kind=AttemptKind.INITIAL,
                requested_audio=request.audio_reference,
                analysis_window_start_sec=None,
                analysis_window_end_sec=None,
                terminal_sink=terminal_sink,
            )
        finally:
            self.boundary._retire_submission(claim)

    def submit_reanalysis(
        self,
        request: ReanalysisRequest,
        terminal_sink: CoordinatorAnalysisTerminalReportSink,
    ) -> BridgeSubmissionResult:
        if not callable(terminal_sink):
            raise BridgeInputError("terminal_sink must be callable")
        if type(request) is not ReanalysisRequest:
            raise BridgeInputError("request must be an exact ReanalysisRequest")
        try:
            request.__post_init__()
        except Exception:
            raise BridgeInputError(
                "reanalysis request fields are invalid"
            ) from None
        claim = self.boundary._claim_submission()
        if claim is None:
            return self._pre_attempt_rejection(
                request.run_id,
                "ANALYSIS_ADMISSION_CLOSED",
            )
        try:
            return self._submit(
                run_id=request.run_id,
                attempt_kind=AttemptKind.REANALYSIS,
                requested_audio=None,
                analysis_window_start_sec=request.analysis_window_start_sec,
                analysis_window_end_sec=request.analysis_window_end_sec,
                terminal_sink=terminal_sink,
            )
        finally:
            self.boundary._retire_submission(claim)

    def cancel(self, request: BridgeCancelRequest) -> BridgeCancelResult:
        if type(request) is not BridgeCancelRequest:
            raise BridgeInputError("request must be an exact BridgeCancelRequest")
        try:
            request.__post_init__()
        except Exception:
            raise BridgeInputError("cancel request fields are invalid") from None
        with self.boundary._lock:
            context = self.boundary._contexts.get(
                request.analysis_attempt_id
            )
            pending_decision = self.boundary._committed_decisions.get(
                request.analysis_attempt_id
            )
            if (
                (
                    context is None
                    or context.job.run_id != request.run_id
                    or context.terminal_claimed
                )
                and (
                    pending_decision is None
                    or pending_decision.identity.run_id != request.run_id
                )
            ):
                return self._cancel_rejected(
                    request,
                    "ANALYSIS_CANCEL_CONTEXT_INVALID",
                )
            if (
                request.origin is AnalysisCancelOrigin.RESULT_EVICTION
                and (
                    (
                        context is not None
                        and context.attempt_kind is AttemptKind.INITIAL
                    )
                    or (
                        pending_decision is not None
                        and pending_decision.attempt_kind is AttemptKind.INITIAL
                    )
                )
            ):
                return self._cancel_rejected(
                    request,
                    "RESULT_EVICTION_REQUIRES_REANALYSIS",
                )
        try:
            receipt = self.scheduler.cancel(
                request.run_id,
                request.analysis_attempt_id,
                request.origin,
                request.reason,
            )
        except Exception:
            return self._cancel_rejected(
                request,
                "SCHEDULER_CANCEL_FAILED",
            )
        if type(receipt) is not CancelReceipt:
            return self._cancel_rejected(
                request,
                "SCHEDULER_CANCEL_RECEIPT_INVALID",
            )
        try:
            receipt.__post_init__()
        except Exception:
            return self._cancel_rejected(
                request,
                "SCHEDULER_CANCEL_RECEIPT_INVALID",
            )
        if (
            receipt.run_id != request.run_id
            or receipt.analysis_attempt_id != request.analysis_attempt_id
        ):
            return self._cancel_rejected(
                request,
                "SCHEDULER_CANCEL_RECEIPT_IDENTITY_MISMATCH",
            )
        status = {
            CancelStatus.CANCELED_PENDING:
                BridgeCancelStatus.CANCEL_REQUESTED,
            CancelStatus.CANCEL_REQUESTED_RUNNING:
                BridgeCancelStatus.CANCEL_REQUESTED,
            CancelStatus.ALREADY_TERMINAL:
                BridgeCancelStatus.ALREADY_TERMINAL,
            CancelStatus.NOT_FOUND:
                BridgeCancelStatus.NOT_FOUND,
            CancelStatus.RUN_ID_MISMATCH:
                BridgeCancelStatus.RUN_ID_MISMATCH,
        }.get(receipt.status)
        if status is None:
            return self._cancel_rejected(
                request,
                "SCHEDULER_CANCEL_RECEIPT_INVALID",
            )
        if status is BridgeCancelStatus.CANCEL_REQUESTED:
            with self.boundary._lock:
                active = self.boundary._contexts.get(
                    request.analysis_attempt_id
                )
                if (
                    active is not None
                    and active is context
                    and active.cancel_origin is None
                ):
                    active.cancel_origin = request.origin
        if (
            pending_decision is not None
            and status is not BridgeCancelStatus.RUN_ID_MISMATCH
        ):
            with self.boundary._lock:
                active_decision = self.boundary._committed_decisions.get(
                    request.analysis_attempt_id
                )
                if active_decision is pending_decision:
                    self.boundary._committed_decisions.pop(
                        request.analysis_attempt_id,
                        None,
                    )
        return BridgeCancelResult(
            status=status,
            run_id=request.run_id,
            analysis_attempt_id=request.analysis_attempt_id,
            origin=request.origin,
            code=None,
            reason=None,
            receipt=receipt,
        )

    @staticmethod
    def _cancel_rejected(
        request: BridgeCancelRequest,
        code: str,
    ) -> BridgeCancelResult:
        return BridgeCancelResult(
            status=BridgeCancelStatus.REJECTED,
            run_id=request.run_id,
            analysis_attempt_id=request.analysis_attempt_id,
            origin=request.origin,
            code=code,
            reason=None,
            receipt=None,
        )

    def stop_and_join_scheduler(
        self,
        timeout_seconds: float | None = None,
    ) -> BridgeSchedulerShutdownResult:
        scheduler_timeout = _resolve_scheduler_shutdown_timeout(
            timeout_seconds
        )
        deadline = time.monotonic() + scheduler_timeout
        admission_settled = (
            self.boundary._close_submission_admission_and_wait(deadline)
        )
        if not admission_settled:
            return _bridge_submit_inflight_shutdown_result()
        scheduler_timeout = deadline - time.monotonic()
        if scheduler_timeout <= 0.0:
            return _bridge_submit_inflight_shutdown_result()
        report = self.scheduler.shutdown(scheduler_timeout)
        if type(report) is not ShutdownReport:
            raise TypeError("scheduler shutdown must return an exact ShutdownReport")
        report.__post_init__()
        mapping = {
            ShutdownStatus.CLOSED: (
                BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED,
                None,
            ),
            ShutdownStatus.IN_PROGRESS: (
                BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS,
                "SCHEDULER_IN_PROGRESS",
            ),
            ShutdownStatus.SHUTDOWN_INCOMPLETE: (
                BridgeSchedulerShutdownStatus.SCHEDULER_INCOMPLETE,
                "SCHEDULER_INCOMPLETE",
            ),
        }
        status, code = mapping[report.status]
        result = BridgeSchedulerShutdownResult(
            status=status,
            scheduler_report=report,
            code=code,
        )
        with self.boundary._lock:
            self.boundary._scheduler_shutdown_result = result
            if status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED:
                self.boundary._committed_decisions.clear()
        return result

    def close_persistence_after_upstream_stopped(
        self,
    ) -> BridgeDatabaseCloseResult:
        with self.boundary._lock:
            cached_close = self.boundary._database_close_result
            scheduler_result = self.boundary._scheduler_shutdown_result
            quarantined = self.boundary._persistence_quarantined
        if cached_close is not None:
            return cached_close
        if (
            scheduler_result is None
            or scheduler_result.status
            is not BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
        ):
            return BridgeDatabaseCloseResult(
                status=BridgeDatabaseCloseStatus.BLOCKED,
                database_closed=False,
                restart_required=False,
                code="SCHEDULER_NOT_CLOSED",
            )
        if quarantined:
            result = BridgeDatabaseCloseResult(
                status=BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED,
                database_closed=True,
                restart_required=True,
                code="DATABASE_QUARANTINED",
            )
        elif self.runtime.closed:
            result = BridgeDatabaseCloseResult(
                status=BridgeDatabaseCloseStatus.DATABASE_ALREADY_CLOSED,
                database_closed=True,
                restart_required=True,
                code="DATABASE_CLOSED_UNEXPECTED",
            )
        else:
            self.runtime.close_after_upstream_stopped()
            result = BridgeDatabaseCloseResult(
                status=BridgeDatabaseCloseStatus.DATABASE_CLOSED,
                database_closed=True,
                restart_required=False,
                code=None,
            )
        with self.boundary._lock:
            if self.boundary._database_close_result is None:
                self.boundary._database_close_result = result
            return self.boundary._database_close_result

    def _submit(
        self,
        *,
        run_id: str,
        attempt_kind: AttemptKind,
        requested_audio: PersistedAudioReference | None,
        analysis_window_start_sec: float | None,
        analysis_window_end_sec: float | None,
        terminal_sink: CoordinatorAnalysisTerminalReportSink,
    ) -> BridgeSubmissionResult:
        unavailable = self._pre_attempt_runtime_failure(run_id)
        if unavailable is not None:
            return unavailable

        try:
            run_record = self.run_repository.get(run_id)
        except DatabaseUnavailableError as exc:
            return self._pre_attempt_rejection(
                run_id,
                exc.code.value,
                restart_required=True,
                reason=exc.args[0] if exc.args else None,
            )
        if run_record is None:
            code = (
                "ANALYSIS_RUN_NOT_FOUND"
                if attempt_kind is AttemptKind.INITIAL
                else "REANALYSIS_RUN_NOT_FOUND"
            )
            return self._pre_attempt_rejection(run_id, code)
        if attempt_kind is AttemptKind.INITIAL:
            if run_record.state != RunState.ACTIVE.value:
                return self._pre_attempt_rejection(
                    run_id,
                    "ANALYSIS_RUN_NOT_ACTIVE",
                )
        elif run_record.state == RunState.ACTIVE.value:
            return self._pre_attempt_rejection(
                run_id,
                "REANALYSIS_RUN_NOT_TERMINAL",
            )

        try:
            audio_record = self.audio_repository.get_for_run(run_id)
        except DatabaseUnavailableError as exc:
            return self._pre_attempt_rejection(
                run_id,
                exc.code.value,
                restart_required=True,
                reason=exc.args[0] if exc.args else None,
            )
        if audio_record is None:
            return self._pre_attempt_rejection(
                run_id,
                "ANALYSIS_AUDIO_NOT_FOUND",
            )
        if run_record.ingress_kind not in ("CAPTURED", "IMPORTED"):
            return self._pre_attempt_rejection(run_id, "ANALYSIS_KIND_INVALID")
        if (
            audio_record.run_id != run_id
            or (
                requested_audio is not None
                and not _audio_reference_matches_record(
                    requested_audio,
                    audio_record,
                    run_record.ingress_kind,
                )
            )
        ):
            return self._pre_attempt_rejection(
                run_id,
                "ANALYSIS_AUDIO_IDENTITY_MISMATCH",
            )
        if attempt_kind is AttemptKind.REANALYSIS:
            try:
                final_path = Path(audio_record.final_path)
                if not final_path.is_file():
                    raise OSError("audio final is not a regular file")
                with final_path.open("rb") as final_audio:
                    final_audio.read(1)
            except (OSError, ValueError):
                return self._pre_attempt_rejection(
                    run_id,
                    "REANALYSIS_AUDIO_UNREADABLE",
                )

        try:
            facts = prepare_analysis_facts(
                run_record,
                audio_record,
                attempt_kind,
                analysis_window_start_sec=analysis_window_start_sec,
                analysis_window_end_sec=analysis_window_end_sec,
            )
        except AnalysisEnvelopeError:
            return self._pre_attempt_rejection(
                run_id,
                "ANALYSIS_ENVELOPE_INVALID",
            )
        except (TypeError, ValueError):
            return self._pre_attempt_rejection(
                run_id,
                "ANALYSIS_ENVELOPE_INVALID",
            )

        try:
            queued_at = self.now_utc()
            validate_utc_timestamp(queued_at)
        except Exception:
            return self._pre_attempt_rejection(
                run_id,
                "ANALYSIS_QUEUE_TIMESTAMP_FAILED",
            )
        attempt_id = new_uuid4()
        try:
            prepared = build_prevalidated_submission(
                facts,
                attempt_id=attempt_id,
                queued_at=queued_at,
            )
        except (TypeError, ValueError):
            return BridgeSubmissionResult(
                status=BridgeSubmissionStatus.REJECTED,
                authority=BridgeAuthorityStatus.NOT_ACQUIRED,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                scheduler_accepted=False,
                terminal_delivered_before_return=False,
                restart_required=False,
                code="ANALYSIS_JOB_INVALID",
                reason=None,
                submit_receipt=None,
            )

        try:
            queue_result = self.attempt_repository.create_queued(
                prepared.attempt_insert
            )
        except DatabaseUnavailableError as exc:
            return BridgeSubmissionResult(
                status=BridgeSubmissionStatus.REJECTED,
                authority=BridgeAuthorityStatus.NOT_ACQUIRED,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                scheduler_accepted=False,
                terminal_delivered_before_return=False,
                restart_required=True,
                code=exc.code.value,
                reason=_bounded_reason(exc.args[0] if exc.args else None),
                submit_receipt=None,
            )
        except Exception:
            return BridgeSubmissionResult(
                status=BridgeSubmissionStatus.STATE_UNKNOWN,
                authority=BridgeAuthorityStatus.UNKNOWN,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                scheduler_accepted=False,
                terminal_delivered_before_return=False,
                restart_required=True,
                code="ATTEMPT_QUEUE_CALL_FAILED",
                reason=None,
                submit_receipt=None,
            )

        if type(queue_result) is not TransactionResult:
            return self._invalid_queue_result(run_id, attempt_id)
        try:
            checked = _validate_transaction_result(queue_result)
        except (TypeError, ValueError):
            return self._invalid_queue_result(run_id, attempt_id)
        if checked.state is TransactionState.ROLLED_BACK:
            return BridgeSubmissionResult(
                status=BridgeSubmissionStatus.REJECTED,
                authority=BridgeAuthorityStatus.NOT_ACQUIRED,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                scheduler_accepted=False,
                terminal_delivered_before_return=False,
                restart_required=False,
                code="ATTEMPT_QUEUE_DB_FAILED",
                reason=_bounded_reason(checked.reason),
                submit_receipt=None,
            )
        if checked.state is TransactionState.UNKNOWN:
            self.boundary._latch_repository_quarantine()
            return BridgeSubmissionResult(
                status=BridgeSubmissionStatus.STATE_UNKNOWN,
                authority=BridgeAuthorityStatus.UNKNOWN,
                run_id=run_id,
                analysis_attempt_id=attempt_id,
                scheduler_accepted=False,
                terminal_delivered_before_return=False,
                restart_required=True,
                code="ANALYSIS_DB_STATE_UNKNOWN",
                reason=_bounded_reason(checked.reason),
                submit_receipt=None,
            )
        if checked.value is not True:
            return self._invalid_queue_result(run_id, attempt_id)

        context = _AttemptContext(
            job=prepared.job,
            attempt_kind=attempt_kind,
            terminal_sink=terminal_sink,
        )
        self.boundary._register_context(context)
        submit_failure_code: str | None = None
        matching_receipt: SubmitReceipt | None = None
        try:
            receipt = self.scheduler.submit(prepared.job)
        except Exception:
            receipt = None
            submit_failure_code = "ANALYSIS_SCHEDULER_SUBMIT_FAILED"

        if submit_failure_code is None:
            if type(receipt) is not SubmitReceipt:
                submit_failure_code = "SCHEDULER_RECEIPT_INVALID"
            else:
                try:
                    receipt.__post_init__()
                except Exception:
                    submit_failure_code = "SCHEDULER_RECEIPT_INVALID"
            if submit_failure_code is None:
                if (
                    receipt.run_id != prepared.job.run_id
                    or receipt.analysis_attempt_id
                    != prepared.job.analysis_attempt_id
                ):
                    submit_failure_code = (
                        "SCHEDULER_RECEIPT_IDENTITY_MISMATCH"
                    )
                else:
                    matching_receipt = receipt
                if (
                    submit_failure_code is None
                    and receipt.status is SubmitStatus.ACCEPTED
                ):
                    early = self.boundary._reconcile_accepted(prepared.job)
                    if early is None:
                        early = self.boundary._linearize_accepted_return(
                            prepared.job
                        )
                    if early is None:
                        accepted_result = BridgeSubmissionResult(
                            status=BridgeSubmissionStatus.ACCEPTED,
                            authority=BridgeAuthorityStatus.ACQUIRED,
                            run_id=run_id,
                            analysis_attempt_id=attempt_id,
                            scheduler_accepted=True,
                            terminal_delivered_before_return=False,
                            restart_required=False,
                            code=None,
                            reason=None,
                            submit_receipt=receipt,
                        )
                        early = self.boundary._complete_accepted_return(
                            prepared.job
                        )
                        if early is None:
                            return accepted_result
                    early_report = (
                        self.boundary._process_claimed_outcome(*early)
                    )
                    self.boundary._deliver_terminal_once(
                        context,
                        early_report,
                    )
                    if early_report.status is not BridgeTerminalStatus.COMMITTED:
                        return BridgeSubmissionResult(
                            status=BridgeSubmissionStatus.STATE_UNKNOWN,
                            authority=BridgeAuthorityStatus.ACQUIRED,
                            run_id=run_id,
                            analysis_attempt_id=attempt_id,
                            scheduler_accepted=True,
                            terminal_delivered_before_return=True,
                            restart_required=True,
                            code=early_report.code,
                            reason=early_report.summary,
                            submit_receipt=receipt,
                        )
                    return BridgeSubmissionResult(
                        status=(
                            BridgeSubmissionStatus.ACCEPTED_EARLY_TERMINAL
                        ),
                        authority=BridgeAuthorityStatus.ACQUIRED,
                        run_id=run_id,
                        analysis_attempt_id=attempt_id,
                        scheduler_accepted=True,
                        terminal_delivered_before_return=True,
                        restart_required=False,
                        code=None,
                        reason=None,
                        submit_receipt=receipt,
                    )
                elif submit_failure_code is None:
                    submit_failure_code = _SCHEDULER_REJECTION_CODES[
                        receipt.status
                    ]

        durable_started = self.boundary._claim_direct_submit_failure(
            prepared.job
        )
        failure_outcome = AttemptOutcome(
            state=AttemptState.FAILED,
            judgment=None,
            result_payload_json=None,
            code=submit_failure_code,
            summary=None,
        )
        terminal_report = self.boundary._write_terminal(
            context,
            failure_outcome,
            queued=not durable_started,
        )
        self.boundary._deliver_terminal_once(context, terminal_report)
        return self._submission_from_terminal_report(
            run_id=run_id,
            attempt_id=attempt_id,
            submission_code=submit_failure_code,
            report=terminal_report,
            receipt=matching_receipt,
        )

    def _pre_attempt_runtime_failure(
        self,
        run_id: str,
    ) -> BridgeSubmissionResult | None:
        with self.boundary._lock:
            quarantined = self.boundary._persistence_quarantined
        if quarantined:
            return self._pre_attempt_rejection(
                run_id,
                "ANALYSIS_DB_CLOSED",
                restart_required=True,
            )
        if self.runtime.ready:
            return None
        closed = self.runtime.closed
        return self._pre_attempt_rejection(
            run_id,
            "ANALYSIS_DB_CLOSED" if closed else "ANALYSIS_ADMISSION_CLOSED",
            restart_required=closed,
        )

    @staticmethod
    def _pre_attempt_rejection(
        run_id: str,
        code: str,
        *,
        restart_required: bool = False,
        reason: str | None = None,
    ) -> BridgeSubmissionResult:
        return BridgeSubmissionResult(
            status=BridgeSubmissionStatus.REJECTED,
            authority=BridgeAuthorityStatus.NOT_ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=None,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=restart_required,
            code=code,
            reason=_bounded_reason(reason),
            submit_receipt=None,
        )

    @staticmethod
    def _invalid_queue_result(
        run_id: str,
        attempt_id: str,
    ) -> BridgeSubmissionResult:
        return BridgeSubmissionResult(
            status=BridgeSubmissionStatus.STATE_UNKNOWN,
            authority=BridgeAuthorityStatus.UNKNOWN,
            run_id=run_id,
            analysis_attempt_id=attempt_id,
            scheduler_accepted=False,
            terminal_delivered_before_return=False,
            restart_required=True,
            code="ATTEMPT_QUEUE_RESULT_INVALID",
            reason=None,
            submit_receipt=None,
        )

    @staticmethod
    def _submission_from_terminal_report(
        *,
        run_id: str,
        attempt_id: str,
        submission_code: str,
        report: BridgeTerminalReport,
        receipt: SubmitReceipt | None,
    ) -> BridgeSubmissionResult:
        if report.status is BridgeTerminalStatus.COMMITTED:
            status = BridgeSubmissionStatus.REJECTED
            code = submission_code
            restart_required = False
        else:
            status = BridgeSubmissionStatus.STATE_UNKNOWN
            code = report.code
            restart_required = True
        return BridgeSubmissionResult(
            status=status,
            authority=BridgeAuthorityStatus.ACQUIRED,
            run_id=run_id,
            analysis_attempt_id=attempt_id,
            scheduler_accepted=False,
            terminal_delivered_before_return=True,
            restart_required=restart_required,
            code=code,
            reason=report.summary,
            submit_receipt=receipt,
        )


def _bounded_reason(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_ERROR_SUMMARY_UTF8_BYTES:
        return value
    clipped = encoded[:MAX_ERROR_SUMMARY_UTF8_BYTES]
    return clipped.decode("utf-8", errors="ignore")


def _resolve_scheduler_shutdown_timeout(
    timeout_seconds: float | None,
) -> float:
    if timeout_seconds is None:
        return _DEFAULT_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS
    if (
        type(timeout_seconds) is not float
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0.0
    ):
        raise ValueError("timeout_seconds must be a finite positive float")
    return timeout_seconds


def _bridge_submit_inflight_shutdown_result(
) -> BridgeSchedulerShutdownResult:
    sampled = time.monotonic()
    return BridgeSchedulerShutdownResult(
        status=BridgeSchedulerShutdownStatus.SCHEDULER_IN_PROGRESS,
        scheduler_report=ShutdownReport(
            status=ShutdownStatus.IN_PROGRESS,
            started_monotonic=sampled,
            finished_monotonic=sampled,
            canceled_pending=0,
            cancel_requested_running=0,
            remaining_attempt_ids=(),
            remaining_worker_ids=(),
            remaining_run_lanes=(),
            remaining_toolchain_lanes=(),
            control_thread_alive=False,
        ),
        code="BRIDGE_SUBMIT_IN_FLIGHT",
    )


def _audio_reference_matches_record(
    reference: PersistedAudioReference,
    record: AudioRecord,
    ingress_kind: str,
) -> bool:
    if type(reference) is not PersistedAudioReference:
        return False
    expected_storage = (
        AudioStorageKind.CAPTURED_FINAL_WAV
        if ingress_kind == "CAPTURED"
        else AudioStorageKind.IMPORTED_FINAL_AUDIO
        if ingress_kind == "IMPORTED"
        else None
    )
    return (
        reference.reference_id == record.audio_id
        and reference.audio_data_id == record.audio_id
        and reference.storage_kind is expected_storage
        and reference.absolute_path == record.final_path
        and reference.sample_rate_hz == record.sample_rate_hz
        and reference.channel_count == record.channel_count
        and reference.frame_count == record.frame_count
        and reference.dtype == ("<f8" if ingress_kind == "IMPORTED" else "<i2")
        and (reference.source_container_format is None or reference.source_container_format == record.container_format)
        and reference.content_revision
        == f"{record.audio_id}:{record.byte_size}"
    )


def decode_analysis_result_payload(
    payload: FrozenResultPayload,
) -> DecodedAnalysisResult:
    """Decode one exact Scheduler success payload to canonical Persistence data."""

    if type(payload) is not FrozenResultObject:
        raise AnalysisResultDecodeError(
            "analysis result payload root must be an exact FrozenResultObject"
        )
    try:
        transport = dict(payload.items)
        if transport.get("__transport__") == "canonical-json-zlib-base64/v1":
            judgment_value = transport.get("judgment")
            chunks = transport.get("chunks")
            if (
                judgment_value not in {"PASS", "FAIL"}
                or type(chunks) is not tuple
                or any(type(chunk) is not str for chunk in chunks)
            ):
                raise AnalysisResultDecodeError(
                    "analysis result transport is invalid"
                )
            compressed = base64.b64decode(
                "".join(chunks).encode("ascii"),
                validate=True,
            )
            canonical = zlib.decompress(compressed).decode("utf-8")
            validated = _decode_canonical_analysis_result_json(canonical)
            if validated.judgment.value != judgment_value:
                raise AnalysisResultDecodeError(
                    "analysis result transport judgment does not match payload"
                )
            return DecodedAnalysisResult(
                judgment=Judgment(judgment_value),
                result_payload_json=canonical,
                result_payload_zlib=compressed,
            )
        # This is the single required validation at the persistence boundary.
        validate_frozen_result_payload(payload)
        root = _decode_frozen_object(payload)
        if set(root) != {"judgment", "result"}:
            raise AnalysisResultDecodeError(
                "analysis result payload root keys must match the closed contract"
            )
        judgment_value = root["judgment"]
        if type(judgment_value) is not str or judgment_value not in ("PASS", "FAIL"):
            raise AnalysisResultDecodeError(
                "analysis result judgment must be exactly PASS or FAIL"
            )
        try:
            canonical = json.dumps(
                root,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise AnalysisResultDecodeError(
                "analysis result canonical serialization failed"
            ) from None
        return DecodedAnalysisResult(
            judgment=Judgment(judgment_value),
            result_payload_json=canonical,
        )
    except AnalysisResultDecodeError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AnalysisResultDecodeError(
            "analysis result payload is invalid"
        ) from None


def _decode_canonical_analysis_result_json(
    result_payload_json: str,
) -> DecodedAnalysisResult:
    try:
        decoded_json = json.loads(
            result_payload_json,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise ValueError(
            "successful terminal report requires a valid closed result payload"
        ) from None
    if (
        type(decoded_json) is not dict
        or set(decoded_json) != {"judgment", "result"}
    ):
        raise ValueError(
            "successful terminal report requires a valid closed result payload"
        )
    judgment_value = decoded_json["judgment"]
    if type(judgment_value) is not str or judgment_value not in {"PASS", "FAIL"}:
        raise ValueError(
            "successful terminal report requires a valid closed result payload"
        )
    canonical_prefix = f'{{"judgment":"{judgment_value}","result":'
    if (
        not result_payload_json.startswith(canonical_prefix)
        or not result_payload_json.endswith("}")
    ):
        raise ValueError(
            "successful terminal report requires canonical result JSON"
        )
    return DecodedAnalysisResult(
        judgment=Judgment(judgment_value),
        result_payload_json=result_payload_json,
    )


def prepare_analysis_facts(
    run_record: RunRecord,
    audio_record: AudioRecord,
    attempt_kind: AttemptKind,
    *,
    analysis_window_start_sec: float | None = None,
    analysis_window_end_sec: float | None = None,
) -> PreparedAnalysisFacts:
    """Purely map exact durable run/audio facts to immutable Scheduler facts."""

    if type(run_record) is not RunRecord:
        raise TypeError("run_record must be an exact RunRecord")
    if type(audio_record) is not AudioRecord:
        raise TypeError("audio_record must be an exact AudioRecord")
    _require_exact_enum(attempt_kind, AttemptKind, "attempt_kind")
    _require_uuid4(run_record.run_id, "run_record.run_id")
    _require_uuid4(audio_record.audio_id, "audio_record.audio_id")
    _require_uuid4(audio_record.run_id, "audio_record.run_id")
    if audio_record.run_id != run_record.run_id:
        raise ValueError("audio record does not belong to run")
    if audio_record.container_format not in {"WAV_PCM", "WAV_IEEE_FLOAT"}:
        raise ValueError("audio record must be supported WAV")
    if run_record.ingress_kind == "CAPTURED" and audio_record.container_format != "WAV_PCM":
        raise ValueError("captured audio record must be WAV_PCM")
    if attempt_kind is AttemptKind.INITIAL:
        if (
            analysis_window_start_sec is not None
            or analysis_window_end_sec is not None
        ):
            raise ValueError("selected window is invalid for an initial attempt")
    else:
        _validate_selected_analysis_window(
            analysis_window_start_sec,
            analysis_window_end_sec,
        )

    scheduler_kind, storage_kind = _map_durable_kind(
        run_record,
        attempt_kind,
    )
    envelope = decode_analysis_envelope(run_record.config_snapshot_json)
    if is_import_mapping(envelope.channel_mapping):
        if run_record.ingress_kind != "IMPORTED":
            raise ValueError("explicit import map requires imported audio")
        validate_import_mapping(envelope.channel_mapping,
            selected_channels=envelope.config.get("selected_channels", ()), channel_count=audio_record.channel_count)
    calibration = _decode_json_object(
        run_record.calibration_snapshot_json,
        "calibration_snapshot",
    )
    calibration_json = canonicalize_json_object(
        calibration,
        "calibration_snapshot",
    )
    toolchain = envelope.toolchain
    scheduler_toolchain = None
    if toolchain is not None:
        scheduler_toolchain = AiToolchainSnapshot(
            toolchain_id=toolchain.toolchain_id,
            runtime_lane_key=canonical_toolchain_lane_key(
                toolchain.toolchain_id
            ),
            execution_revision=toolchain.execution_revision,
            execution_mode=toolchain.execution_mode,
            entrypoint=toolchain.entrypoint,
            package_dir=toolchain.package_dir,
            params_json=canonicalize_json_object(
                toolchain.params,
                "toolchain.params",
            ),
            timeout_seconds=toolchain.timeout_seconds,
        )
    if attempt_kind is AttemptKind.INITIAL:
        snapshot_window_start = envelope.window_start_seconds
        snapshot_window_end = envelope.window_end_seconds
    else:
        snapshot_window_start = analysis_window_start_sec
        snapshot_window_end = analysis_window_end_sec
    snapshot = AnalysisInputSnapshot(
        config_snapshot_json=canonicalize_json_object(
            envelope.config,
            "analysis config",
        ),
        calibration_snapshot_json=calibration_json,
        workorder_snapshot_json=canonicalize_json_object(
            envelope.workorder,
            "analysis workorder",
        ),
        channel_mapping_json=canonicalize_json_object(
            envelope.channel_mapping,
            "analysis channel mapping",
        ),
        analysis_window_start_seconds=snapshot_window_start,
        analysis_window_end_seconds=snapshot_window_end,
        algorithm_revision=envelope.algorithm_revision,
        current_trace_json=run_record.current_trace_json,
        ai_toolchain=scheduler_toolchain,
    )
    audio_reference = PersistedAudioReference(
        reference_id=audio_record.audio_id,
        storage_kind=storage_kind,
        absolute_path=audio_record.final_path,
        sample_rate_hz=audio_record.sample_rate_hz,
        channel_count=audio_record.channel_count,
        frame_count=audio_record.frame_count,
        dtype="<f8" if storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO else "<i2",
        source_container_format=audio_record.container_format if storage_kind is AudioStorageKind.IMPORTED_FINAL_AUDIO else None,
        content_revision=f"{audio_record.audio_id}:{audio_record.byte_size}",
        audio_data_id=audio_record.audio_id,
    )
    return PreparedAnalysisFacts(
        run_id=run_record.run_id,
        audio_id=audio_record.audio_id,
        attempt_kind=attempt_kind,
        scheduler_kind=scheduler_kind,
        audio_reference=audio_reference,
        snapshot=snapshot,
        execution_timeout_seconds=envelope.execution_timeout_seconds,
        queue_timeout_seconds=envelope.queue_timeout_seconds,
    )


def build_prevalidated_submission(
    facts: PreparedAnalysisFacts,
    *,
    attempt_id: str,
    queued_at: str,
) -> PreparedAnalysisSubmission:
    """Construct the exact AttemptInsert and AnalysisJob before any DB write."""

    if type(facts) is not PreparedAnalysisFacts:
        raise TypeError("facts must be exact PreparedAnalysisFacts")
    attempt_insert = AttemptInsert(
        attempt_id=attempt_id,
        run_id=facts.run_id,
        audio_id=facts.audio_id,
        attempt_kind=facts.attempt_kind,
        queued_at=queued_at,
    )
    job = AnalysisJob(
        run_id=facts.run_id,
        analysis_attempt_id=attempt_id,
        kind=facts.scheduler_kind,
        audio=facts.audio_reference,
        snapshot=facts.snapshot,
        execution_timeout_seconds=facts.execution_timeout_seconds,
        queue_timeout_seconds=facts.queue_timeout_seconds,
    )
    return PreparedAnalysisSubmission(
        attempt_insert=attempt_insert,
        job=job,
    )


def _map_durable_kind(
    run_record: RunRecord,
    attempt_kind: AttemptKind,
) -> tuple[AnalysisKind, AudioStorageKind]:
    ingress = run_record.ingress_kind
    if ingress == "CAPTURED":
        storage_kind = AudioStorageKind.CAPTURED_FINAL_WAV
    elif ingress == "IMPORTED":
        storage_kind = AudioStorageKind.IMPORTED_FINAL_AUDIO
    else:
        raise ValueError("run ingress kind is unsupported")

    if attempt_kind is AttemptKind.INITIAL:
        if run_record.state != RunState.ACTIVE.value:
            raise ValueError("initial analysis requires an ACTIVE run")
        scheduler_kind = (
            AnalysisKind.AUTOMATIC
            if ingress == "CAPTURED"
            else AnalysisKind.IMPORTED
        )
    else:
        terminal_states = {state.value for state in RunState if state is not RunState.ACTIVE}
        if run_record.state not in terminal_states:
            raise ValueError("reanalysis requires a terminal run")
        scheduler_kind = AnalysisKind.REANALYSIS
    return scheduler_kind, storage_kind


def _decode_json_object(value: object, name: str) -> dict[str, object]:
    if type(value) is not str:
        raise AnalysisEnvelopeError(f"{name} must be valid JSON object text")
    try:
        decoded = json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AnalysisEnvelopeError(
            f"{name} must be valid JSON object text"
        ) from None
    if type(decoded) is not dict:
        raise AnalysisEnvelopeError(f"{name} must be a JSON object")
    return decoded


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_frozen_value(value: object) -> object:
    value_type = type(value)
    if value is None or value_type in (bool, int, float, str):
        return value
    if value_type is tuple:
        return [_decode_frozen_value(item) for item in value]
    if value_type is FrozenResultObject:
        return _decode_frozen_object(value)
    raise AnalysisResultDecodeError(
        f"unsupported frozen result payload type: {value_type.__name__}"
    )


def _freeze_json_value(value: object) -> FrozenResultPayload:
    value_type = type(value)
    if value is None or value_type in (bool, int, float, str):
        return value
    if value_type is list:
        return tuple(_freeze_json_value(item) for item in value)
    if value_type is dict:
        return FrozenResultObject(
            items=tuple(
                (key, _freeze_json_value(item))
                for key, item in value.items()
            )
        )
    raise TypeError(f"unsupported decoded JSON type: {value_type.__name__}")


def _decode_frozen_object(value: FrozenResultObject) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, item in value.items:
        if key in decoded:
            raise AnalysisResultDecodeError(
                "frozen result object keys must be unique"
            )
        decoded[key] = _decode_frozen_value(item)
    return decoded


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
