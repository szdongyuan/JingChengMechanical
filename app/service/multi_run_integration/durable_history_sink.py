"""Persist projected history revisions before one-way Dashboard forwarding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.service.multi_run_coordinator_mvp.contracts import (
    ABSENT,
    HistoryFactKind,
    ProjectedHistoryEvent,
    TerminalConclusion,
    TerminalErrorCategory,
)
from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
)


class DurableHistoryDiagnosticCode(str, Enum):
    CAS_FAILED = "cas_failed"


@dataclass(frozen=True)
class DurableHistoryDiagnostic:
    code: DurableHistoryDiagnosticCode
    run_id: str
    detail: str

    def __post_init__(self) -> None:
        if type(self.code) is not DurableHistoryDiagnosticCode:
            raise ValueError("code must be an exact durable history diagnostic code")
        if type(self.run_id) is not str or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if (
            type(self.detail) is not str
            or not self.detail.strip()
            or len(self.detail) > 240
        ):
            raise ValueError("detail must contain 1..240 characters")


class DurableHistoryPersistenceError(RuntimeError):
    """The projected history revision was not durably committed."""


class _RevisionRepository(Protocol):
    def advance_causal_revision(
        self,
        run_id: str,
        *,
        expected_revision: object,
        proposed_revision: int,
    ) -> TransactionResult[bool]: ...


class _HistorySink(Protocol):
    def publish(self, event: ProjectedHistoryEvent) -> None: ...


_PREACCEPT_COMBINATIONS = {
    TerminalErrorCategory.PREFLIGHT_REJECTED: TerminalConclusion.ERROR,
    TerminalErrorCategory.INVALID_FROZEN_INPUT: TerminalConclusion.ERROR,
    TerminalErrorCategory.CAPTURE_CAPACITY: TerminalConclusion.CAPACITY_REJECTED,
    TerminalErrorCategory.RESIDENT_UNAVAILABLE: TerminalConclusion.ERROR,
    TerminalErrorCategory.ACTIVE_COMMIT_FAILED: TerminalConclusion.ERROR,
}


class DurableHistorySink:
    """Exact-CAS history boundary that forwards an event at most once."""

    def __init__(
        self,
        repository: _RevisionRepository,
        downstream: _HistorySink,
        *,
        diagnostic_callback: Callable[[DurableHistoryDiagnostic], object]
        | None = None,
    ) -> None:
        if not callable(getattr(repository, "advance_causal_revision", None)):
            raise TypeError("repository must expose advance_causal_revision")
        if not callable(getattr(downstream, "publish", None)):
            raise TypeError("downstream must expose publish")
        if diagnostic_callback is not None and not callable(diagnostic_callback):
            raise TypeError("diagnostic_callback must be callable or None")
        self._repository = repository
        self._downstream = downstream
        self._diagnostic_callback = diagnostic_callback

    def publish(self, event: ProjectedHistoryEvent) -> None:
        if type(event) is not ProjectedHistoryEvent:
            raise TypeError("history event must be an exact ProjectedHistoryEvent")
        event.__post_init__()

        if _is_exact_preaccept(event):
            self._downstream.publish(event)
            return

        try:
            result = self._repository.advance_causal_revision(
                event.run_id,
                expected_revision=event.expected_revision,
                proposed_revision=event.proposed_revision,
            )
        except Exception as exc:
            self._fail(event.run_id, exc)

        if (
            type(result) is not TransactionResult
            or result.state is not TransactionState.COMMITTED
            or result.value is not True
        ):
            reason = (
                result.reason
                if type(result) is TransactionResult and result.reason
                else (
                    result.state.value
                    if type(result) is TransactionResult
                    else "invalid causal revision transaction result"
                )
            )
            self._fail(event.run_id, reason)

        self._downstream.publish(event)

    def _fail(self, run_id: str, error: object) -> None:
        detail = str(error).strip() or type(error).__name__
        bounded = detail[:240]
        diagnostic = DurableHistoryDiagnostic(
            code=DurableHistoryDiagnosticCode.CAS_FAILED,
            run_id=run_id,
            detail=bounded,
        )
        if self._diagnostic_callback is not None:
            try:
                self._diagnostic_callback(diagnostic)
            except Exception:
                pass
        raise DurableHistoryPersistenceError(bounded) from None


def _is_exact_preaccept(event: ProjectedHistoryEvent) -> bool:
    projection = event.projection
    return (
        event.kind is HistoryFactKind.RUN_TERMINATED
        and event.expected_revision is ABSENT
        and event.proposed_revision == 1
        and event.operation_id is None
        and projection.result_payload is None
        and projection.fault_type is None
        and projection.manual_judgment_revision is None
        and _PREACCEPT_COMBINATIONS.get(projection.error_category)
        is projection.conclusion
    )


DurableHistoryEventSink = DurableHistorySink


__all__ = [
    "DurableHistoryDiagnostic",
    "DurableHistoryDiagnosticCode",
    "DurableHistoryEventSink",
    "DurableHistoryPersistenceError",
    "DurableHistorySink",
]
