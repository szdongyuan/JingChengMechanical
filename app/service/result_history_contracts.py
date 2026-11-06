from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import TypeAlias

from app.service.result_history import ResultHistoryEvent


class ResultHistoryContractError(ValueError):
    """Raised when a dashboard integration value is malformed."""


def _require_exact_self(value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ResultHistoryContractError(
            f"value must be exact {expected.__name__}"
        )


def _require_text(
    value: object,
    field_name: str,
    *,
    optional: bool = False,
    bounded: bool = False,
) -> None:
    if optional and value is None:
        return
    if type(value) is not str or not value.strip():
        raise ResultHistoryContractError(
            f"{field_name} must be a non-empty exact string"
        )
    if bounded and len(value) > 240:
        raise ResultHistoryContractError(
            f"{field_name} must contain at most 240 characters"
        )


def _require_revision(
    value: object,
    field_name: str,
    *,
    optional: bool = False,
) -> None:
    if optional and value is None:
        return
    if type(value) is not int or value < 0:
        raise ResultHistoryContractError(
            f"{field_name} must be a non-negative exact integer"
        )


_EVENT_NAMES = frozenset(
    {
        "ActiveRunChanged",
        "RunTerminated",
        "TerminalResultReplaced",
        "ReanalysisStarted",
        "ReanalysisCompleted",
        "ReanalysisCancelled",
        "ActiveRunRemoved",
    }
)
_PAYLOAD_KINDS = frozenset(
    {
        "none",
        "ReviewDisplayPayload",
        "ErrorDisplayPayload",
        "existing backup retained",
        "restore prior payload",
    }
)


@dataclass(frozen=True)
class HistoryMappingContractRow:
    domain_fact: str
    event_name: str
    card_phase: str
    card_conclusion: str | None
    payload_kind: str

    def __post_init__(self) -> None:
        _require_exact_self(self, HistoryMappingContractRow)
        _require_text(self.domain_fact, "domain_fact")
        _require_text(self.event_name, "event_name")
        _require_text(self.card_phase, "card_phase")
        _require_text(
            self.card_conclusion,
            "card_conclusion",
            optional=True,
        )
        _require_text(self.payload_kind, "payload_kind")
        if self.event_name not in _EVENT_NAMES:
            raise ResultHistoryContractError("event_name is unknown")
        if self.payload_kind not in _PAYLOAD_KINDS:
            raise ResultHistoryContractError("payload_kind is unknown")


HISTORY_MAPPING_CONTRACT = (
    HistoryMappingContractRow("run accepted / recording", "ActiveRunChanged", "recording", None, "none"),
    HistoryMappingContractRow("capture complete / persisting", "ActiveRunChanged", "waiting_save", None, "none"),
    HistoryMappingContractRow("persisted / analysis queued", "ActiveRunChanged", "waiting_analysis", None, "none"),
    HistoryMappingContractRow("analysis started", "ActiveRunChanged", "analyzing", None, "none"),
    HistoryMappingContractRow("terminal OK", "RunTerminated", "terminal", "ok", "ReviewDisplayPayload"),
    HistoryMappingContractRow("terminal NG", "RunTerminated", "terminal", "ng", "ReviewDisplayPayload"),
    HistoryMappingContractRow("terminal no-result", "RunTerminated", "terminal", "no_result", "ReviewDisplayPayload"),
    HistoryMappingContractRow("terminal analysis/system error", "RunTerminated", "terminal", "error", "ErrorDisplayPayload"),
    HistoryMappingContractRow("admission capacity rejected", "RunTerminated", "terminal", "capacity_rejected", "ErrorDisplayPayload"),
    HistoryMappingContractRow("terminal manual stop", "RunTerminated", "terminal", "manual_stopped", "ErrorDisplayPayload"),
    HistoryMappingContractRow("accepted reanalysis start", "ReanalysisStarted", "reanalyzing", "retain prior conclusion", "existing backup retained"),
    HistoryMappingContractRow("reanalysis OK", "ReanalysisCompleted", "terminal", "ok", "ReviewDisplayPayload"),
    HistoryMappingContractRow("reanalysis NG", "ReanalysisCompleted", "terminal", "ng", "ReviewDisplayPayload"),
    HistoryMappingContractRow("reanalysis no-result", "ReanalysisCompleted", "terminal", "no_result", "ReviewDisplayPayload"),
    HistoryMappingContractRow("reanalysis error", "ReanalysisCompleted", "terminal", "error", "ErrorDisplayPayload"),
    HistoryMappingContractRow("accepted reanalysis cancel", "ReanalysisCancelled", "restore prior terminal card", None, "restore prior payload"),
    HistoryMappingContractRow("accepted manual judgment OK", "TerminalResultReplaced", "terminal", "ok", "ReviewDisplayPayload"),
    HistoryMappingContractRow("accepted manual judgment NG", "TerminalResultReplaced", "terminal", "ng", "ReviewDisplayPayload"),
    HistoryMappingContractRow("active run withdrawn before terminal", "ActiveRunRemoved", "removed", None, "none"),
)


class HistoryMappingRejectionCode(str, Enum):
    UNKNOWN_PHASE = "unknown_phase"
    UNKNOWN_OUTCOME = "unknown_outcome"
    INVALID_PHASE_OUTCOME = "invalid_phase_outcome"
    MISSING_REQUIRED_PAYLOAD = "missing_required_payload"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True)
class HistoryMappingRejection:
    code: HistoryMappingRejectionCode
    run_id: str | None
    phase: str | None
    outcome: str | None
    detail: str

    def __post_init__(self) -> None:
        _require_exact_self(self, HistoryMappingRejection)
        if type(self.code) is not HistoryMappingRejectionCode:
            raise ResultHistoryContractError(
                "code must be an exact HistoryMappingRejectionCode"
            )
        _require_text(self.run_id, "run_id", optional=True)
        _require_text(self.phase, "phase", optional=True)
        _require_text(self.outcome, "outcome", optional=True)
        _require_text(self.detail, "detail", bounded=True)


HistoryMappingResult: TypeAlias = ResultHistoryEvent | HistoryMappingRejection


class HistoryProjectionDiagnosticCode(str, Enum):
    WRONG_QT_THREAD = "wrong_qt_thread"
    PROJECTION_FAILED = "projection_failed"
    STALE_TRANSITION = "stale_transition"
    FOREIGN_TRANSITION = "foreign_transition"
    CONCURRENT_STORE_CHANGE = "concurrent_store_change"
    MISSING_SELECTED_PAYLOAD = "missing_selected_payload"


@dataclass(frozen=True)
class HistoryProjectionDiagnostic:
    code: HistoryProjectionDiagnosticCode
    run_id: str | None
    transition_revision: int | None
    current_revision: int | None
    detail: str

    def __post_init__(self) -> None:
        _require_exact_self(self, HistoryProjectionDiagnostic)
        if type(self.code) is not HistoryProjectionDiagnosticCode:
            raise ResultHistoryContractError(
                "code must be an exact HistoryProjectionDiagnosticCode"
            )
        _require_text(self.run_id, "run_id", optional=True)
        _require_revision(
            self.transition_revision,
            "transition_revision",
            optional=True,
        )
        _require_revision(
            self.current_revision,
            "current_revision",
            optional=True,
        )
        _require_text(self.detail, "detail", bounded=True)


@dataclass(frozen=True)
class ManualJudgmentTarget:
    run_id: str
    revision: int
    fault_types: tuple[str, ...]
    manual_judgment_allowed: bool

    def __post_init__(self) -> None:
        _require_exact_self(self, ManualJudgmentTarget)
        _require_text(self.run_id, "run_id")
        _require_revision(self.revision, "revision")
        if (
            type(self.fault_types) is not tuple
            or not all(
                type(item) is str and bool(item.strip())
                for item in self.fault_types
            )
            or len(set(self.fault_types)) != len(self.fault_types)
        ):
            raise ResultHistoryContractError(
                "fault_types must be unique non-empty exact strings "
                "in an exact tuple"
            )
        if type(self.manual_judgment_allowed) is not bool:
            raise ResultHistoryContractError(
                "manual_judgment_allowed must be an exact bool"
            )


@dataclass(frozen=True)
class ManualJudgmentRequest:
    run_id: str
    label: str
    expected_revision: int
    fault_type: str | None = None

    def __post_init__(self) -> None:
        _require_exact_self(self, ManualJudgmentRequest)
        _require_text(self.run_id, "run_id")
        _require_text(self.label, "label")
        _require_revision(self.expected_revision, "expected_revision")
        if self.label not in {"OK", "NG"}:
            raise ResultHistoryContractError("label must be exactly OK or NG")
        _require_text(self.fault_type, "fault_type", optional=True)
        if self.label == "OK" and self.fault_type is not None:
            raise ResultHistoryContractError(
                "OK manual judgment cannot include fault_type"
            )


@dataclass(frozen=True)
class ReanalysisRequest:
    run_id: str
    expected_revision: int
    start_sec: float
    end_sec: float

    def __post_init__(self) -> None:
        _require_exact_self(self, ReanalysisRequest)
        _require_text(self.run_id, "run_id")
        _require_revision(self.expected_revision, "expected_revision")
        if (
            type(self.start_sec) is not float
            or type(self.end_sec) is not float
            or not isfinite(self.start_sec)
            or not isfinite(self.end_sec)
            or self.start_sec < 0.0
            or self.start_sec >= self.end_sec
        ):
            raise ResultHistoryContractError(
                "reanalysis window must be finite and satisfy "
                "0 <= start_sec < end_sec"
            )


@dataclass(frozen=True)
class ReanalysisCancelRequest:
    run_id: str
    operation_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        _require_exact_self(self, ReanalysisCancelRequest)
        _require_text(self.run_id, "run_id")
        _require_text(self.operation_id, "operation_id")
        _require_revision(self.expected_revision, "expected_revision")


ResultOperationRequest: TypeAlias = (
    ManualJudgmentRequest | ReanalysisRequest | ReanalysisCancelRequest
)


class ResultOperationRejectionCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    NO_SELECTION = "no_selection"
    TARGET_NOT_TERMINAL = "target_not_terminal"
    OPERATION_NOT_ALLOWED = "operation_not_allowed"
    NOT_REANALYZING = "not_reanalyzing"
    MISSING_OPERATION_ID = "missing_operation_id"


@dataclass(frozen=True)
class ResultOperationAccepted:
    request: ResultOperationRequest

    def __post_init__(self) -> None:
        _require_exact_self(self, ResultOperationAccepted)
        if type(self.request) not in (
            ManualJudgmentRequest,
            ReanalysisRequest,
            ReanalysisCancelRequest,
        ):
            raise ResultHistoryContractError(
                "request must be an exact result-operation request"
            )


@dataclass(frozen=True)
class ResultOperationRejected:
    code: ResultOperationRejectionCode
    reason: str

    def __post_init__(self) -> None:
        _require_exact_self(self, ResultOperationRejected)
        if type(self.code) is not ResultOperationRejectionCode:
            raise ResultHistoryContractError(
                "code must be an exact ResultOperationRejectionCode"
            )
        _require_text(self.reason, "reason", bounded=True)


ResultOperationResult: TypeAlias = ResultOperationAccepted | ResultOperationRejected
