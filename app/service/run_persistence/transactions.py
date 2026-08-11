"""Mechanical classification for one short persistence transaction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from app.service.run_persistence.contracts import (
    TransactionResult,
    TransactionState,
)


_Value = TypeVar("_Value")


class SQLiteDriverTransactionState(Protocol):
    @property
    def in_transaction(self) -> bool: ...


class TransactionConnection(Protocol):
    def begin(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def connection(self) -> SQLiteDriverTransactionState: ...

    def close(self) -> None: ...


def _driver_transaction_active(connection: TransactionConnection) -> bool:
    state = connection.connection().in_transaction
    if type(state) is not bool:
        raise TypeError("SQLite driver transaction state is not boolean")
    return state


def _unknown(connection: TransactionConnection, reason: str) -> TransactionResult:
    try:
        connection.close()
    except Exception:
        pass
    return TransactionResult(
        state=TransactionState.UNKNOWN,
        reason=reason,
        restart_required=True,
    )


def run_short_transaction(
    connection: TransactionConnection,
    work: Callable[[TransactionConnection], _Value],
) -> TransactionResult[_Value]:
    """Run work once and mechanically classify its transaction outcome."""

    try:
        connection.begin()
        value = work(connection)
        connection.commit()
    except Exception as transaction_error:
        try:
            connection.rollback()
        except Exception as rollback_error:
            return _unknown(
                connection,
                f"{transaction_error}; rollback failed: {rollback_error}",
            )
        try:
            closed = not _driver_transaction_active(connection)
        except Exception as state_error:
            return _unknown(
                connection,
                f"{transaction_error}; transaction state unreadable: {state_error}",
            )
        if not closed:
            return _unknown(
                connection,
                f"{transaction_error}; transaction remained active after rollback",
            )
        return TransactionResult(
            state=TransactionState.ROLLED_BACK,
            reason=str(transaction_error),
        )

    try:
        closed = not _driver_transaction_active(connection)
    except Exception as state_error:
        return _unknown(
            connection,
            f"commit returned but transaction state unreadable: {state_error}",
        )
    if not closed:
        return _unknown(
            connection,
            "commit returned but transaction remained active",
        )
    return TransactionResult(
        state=TransactionState.COMMITTED,
        value=value,
    )
