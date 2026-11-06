"""Startup-only interruption settlement for unfinished durable state."""

from __future__ import annotations

from dataclasses import dataclass

from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
    validate_utc_timestamp,
)
from app.service.run_persistence.database import (
    DatabaseLifecycleCode,
    RunPersistenceDatabase,
)
from app.service.run_persistence.transactions import run_short_transaction


PROCESS_RESTARTED_CODE = "PROCESS_RESTARTED"
PROCESS_RESTARTED_SUMMARY = (
    "Previous process ended before terminal outcome"
)


@dataclass(frozen=True)
class StartupSettlementResult:
    ready: bool
    transaction: TransactionResult[object] | None = None
    code: DatabaseLifecycleCode | None = None
    reason: str | None = None
    restart_required: bool = False


def settle_startup(
    runtime: RunPersistenceDatabase,
    startup_at: str,
) -> StartupSettlementResult:
    """Validate, settle attempts then runs, and latch READY only after commit."""

    validate_utc_timestamp(startup_at)
    opened = runtime.open_and_validate_startup()
    if not opened.ok:
        return StartupSettlementResult(
            ready=False,
            code=opened.code,
            reason=opened.reason,
        )

    def settle(connection):
        connection.execute_sql(
            """
            UPDATE analysis_attempt
            SET state = 'INTERRUPTED',
                error_code = ?,
                error_summary = ?,
                terminal_at = ?,
                updated_at = ?
            WHERE state IN ('QUEUED', 'RUNNING')
            """,
            (
                PROCESS_RESTARTED_CODE,
                PROCESS_RESTARTED_SUMMARY,
                startup_at,
                startup_at,
            ),
        )
        connection.execute_sql(
            """
            UPDATE run
            SET state = 'INTERRUPTED',
                terminal_code = ?,
                terminal_summary = ?,
                terminal_at = ?,
                updated_at = ?
            WHERE state = 'ACTIVE'
            """,
            (
                PROCESS_RESTARTED_CODE,
                PROCESS_RESTARTED_SUMMARY,
                startup_at,
                startup_at,
            ),
        )

    try:
        with runtime._startup_settlement_operation() as database:
            transaction = run_short_transaction(database, settle)
            runtime._finish_startup(transaction)
    except Exception as exc:
        runtime.close_startup_failed()
        return StartupSettlementResult(
            ready=False,
            code=DatabaseLifecycleCode.DATABASE_OPEN_FAILED,
            reason=f"startup settlement connection failed: {exc}",
        )

    if transaction.state is TransactionState.COMMITTED:
        return StartupSettlementResult(
            ready=True,
            transaction=transaction,
        )
    return StartupSettlementResult(
        ready=False,
        transaction=transaction,
        reason=transaction.reason,
        restart_required=transaction.restart_required,
    )
