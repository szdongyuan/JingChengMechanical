"""Exact captured/imported sequencing around the ACTIVE commit boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading
from typing import Protocol

from app.service.run_persistence.contracts import (
    IngressKind,
    RunInsert,
    TransactionResult,
    TransactionState,
    validate_uuid4,
)
from app.service.run_persistence.database import DatabaseUnavailableError


class AcceptanceInputError(ValueError):
    """Input could not be validated and frozen before formal acceptance."""


class ActiveRunWriter(Protocol):
    def insert_active(self, frozen_run: RunInsert) -> TransactionResult[bool]: ...


@dataclass(frozen=True)
class AcceptanceResult:
    accepted: bool
    run_id: str
    permission_granted: bool
    reason: str | None = None
    restart_required: bool = False


class RunAcceptanceService:
    def __init__(
        self,
        repository: ActiveRunWriter,
        *,
        freeze_captured: Callable[[str, object], RunInsert],
        freeze_imported: Callable[[str, object], RunInsert],
        reserve: Callable[[str], bool],
        cancel: Callable[[str], None],
        close_admission: Callable[[str], None],
    ) -> None:
        self._repository = repository
        self._freeze_captured = freeze_captured
        self._freeze_imported = freeze_imported
        self._reserve = reserve
        self._cancel = cancel
        self._close_admission = close_admission
        self._admission_lock = threading.RLock()
        self._admission_closed = False
        self._closed_reason: str | None = None

    def _closed_result(self, run_id: str) -> AcceptanceResult:
        reason = self._closed_reason or "database transaction outcome is unknown"
        return AcceptanceResult(
            accepted=False,
            run_id=run_id,
            permission_granted=False,
            reason=f"run acceptance is closed; restart required: {reason}",
            restart_required=True,
        )

    def _freeze(
        self,
        run_id: str,
        request: object,
        freezer: Callable[[str, object], RunInsert],
        ingress: IngressKind,
    ) -> RunInsert | AcceptanceResult:
        try:
            frozen_run = freezer(run_id, request)
        except AcceptanceInputError as exc:
            return AcceptanceResult(
                accepted=False,
                run_id=run_id,
                permission_granted=False,
                reason=str(exc),
            )
        if (
            frozen_run.run_id != run_id
            or frozen_run.ingress_kind is not ingress
        ):
            return AcceptanceResult(
                accepted=False,
                run_id=run_id,
                permission_granted=False,
                reason="frozen run identity or ingress does not match request",
            )
        return frozen_run

    def _from_transaction(
        self, run_id: str, result: TransactionResult[bool]
    ) -> AcceptanceResult:
        if result.state is TransactionState.COMMITTED and result.value is True:
            return AcceptanceResult(
                accepted=True,
                run_id=run_id,
                permission_granted=True,
            )
        if result.state is TransactionState.UNKNOWN:
            reason = result.reason or "database transaction outcome is unknown"
            self._admission_closed = True
            self._closed_reason = reason
            self._close_admission(reason)
            return AcceptanceResult(
                accepted=False,
                run_id=run_id,
                permission_granted=False,
                reason=reason,
                restart_required=True,
            )
        return AcceptanceResult(
            accepted=False,
            run_id=run_id,
            permission_granted=False,
            reason=result.reason or "ACTIVE run insert did not commit",
        )

    def accept_captured(
        self, run_id: str, request: object
    ) -> AcceptanceResult:
        validate_uuid4(run_id)
        with self._admission_lock:
            if self._admission_closed:
                return self._closed_result(run_id)
            frozen = self._freeze(
                run_id,
                request,
                self._freeze_captured,
                IngressKind.CAPTURED,
            )
            if isinstance(frozen, AcceptanceResult):
                return frozen
            if not self._reserve(run_id):
                return AcceptanceResult(
                    accepted=False,
                    run_id=run_id,
                    permission_granted=False,
                    reason="capture reservation rejected",
                )

            try:
                result = self._repository.insert_active(frozen)
            except DatabaseUnavailableError as exc:
                self._cancel(run_id)
                return AcceptanceResult(
                    accepted=False,
                    run_id=run_id,
                    permission_granted=False,
                    reason=f"{exc.code.value}: {exc}",
                )
            acceptance = self._from_transaction(run_id, result)
            if (
                not acceptance.accepted
                and result.state is not TransactionState.UNKNOWN
            ):
                self._cancel(run_id)
            return acceptance

    def accept_imported(
        self, run_id: str, request: object
    ) -> AcceptanceResult:
        validate_uuid4(run_id)
        with self._admission_lock:
            if self._admission_closed:
                return self._closed_result(run_id)
            frozen = self._freeze(
                run_id,
                request,
                self._freeze_imported,
                IngressKind.IMPORTED,
            )
            if isinstance(frozen, AcceptanceResult):
                return frozen
            try:
                result = self._repository.insert_active(frozen)
            except DatabaseUnavailableError as exc:
                return AcceptanceResult(
                    accepted=False,
                    run_id=run_id,
                    permission_granted=False,
                    reason=f"{exc.code.value}: {exc}",
                )
            return self._from_transaction(run_id, result)
