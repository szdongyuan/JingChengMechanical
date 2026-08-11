"""Repository for analysis-attempt state transitions and run projection."""

from __future__ import annotations

from dataclasses import dataclass
import json

from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactStore,
    PublishedAnalysisResult,
)
from app.service.run_persistence.contracts import (
    AttemptInsert,
    AttemptKind,
    AttemptState,
    ContractValidationError,
    Judgment,
    TransactionResult,
    TransactionState,
    validate_utc_timestamp,
    validate_uuid4,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.transactions import run_short_transaction


def _reject_non_standard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


@dataclass(frozen=True)
class AttemptOutcome:
    state: AttemptState
    judgment: Judgment | None
    result_payload_json: str | None
    code: str | None
    summary: str | None
    result_payload_zlib: bytes | None = None

    def __post_init__(self) -> None:
        terminal = {
            AttemptState.SUCCEEDED,
            AttemptState.NO_RESULT,
            AttemptState.FAILED,
            AttemptState.CANCELED,
            AttemptState.INTERRUPTED,
        }
        if not isinstance(self.state, AttemptState) or self.state not in terminal:
            raise ContractValidationError("outcome state must be terminal")
        if self.summary is not None and not isinstance(self.summary, str):
            raise ContractValidationError("outcome summary must be a string or None")
        if self.state is AttemptState.SUCCEEDED:
            if not isinstance(self.judgment, Judgment):
                raise ContractValidationError(
                    "successful outcome requires a Judgment"
                )
            if not isinstance(self.result_payload_json, str):
                raise ContractValidationError(
                    "successful outcome requires result JSON"
                )
            try:
                json.loads(
                    self.result_payload_json,
                    parse_constant=_reject_non_standard_json_constant,
                )
            except (TypeError, ValueError):
                raise ContractValidationError("result payload must be valid JSON") from None
            if self.code is not None:
                raise ContractValidationError(
                    "successful outcome cannot have an error code"
                )
            if (
                self.result_payload_zlib is not None
                and (
                    type(self.result_payload_zlib) is not bytes
                    or not self.result_payload_zlib
                )
            ):
                raise ContractValidationError(
                    "precompressed result payload must be nonempty exact bytes"
                )
        else:
            if (
                self.judgment is not None
                or self.result_payload_json is not None
                or self.result_payload_zlib is not None
            ):
                raise ContractValidationError(
                    "non-successful outcome cannot have judgment or result"
                )
            if not isinstance(self.code, str) or not self.code:
                raise ContractValidationError(
                    "non-successful outcome requires an error code"
                )


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    run_id: str
    audio_id: str
    attempt_kind: str
    state: str
    judgment: str | None
    result_payload_json: str | None
    error_code: str | None
    error_summary: str | None
    queued_at: str
    started_at: str | None
    terminal_at: str | None
    updated_at: str


_ATTEMPT_COLUMNS = (
    "attempt_id, run_id, audio_id, attempt_kind, state, judgment, "
    "result_payload_json, error_code, error_summary, queued_at, started_at, "
    "terminal_at, updated_at"
)


def _project_initial_run(
    database,
    run_id: str,
    state: str,
    code: str,
    summary: str | None,
    now: str,
    judgment: str | None = None,
) -> None:
    source = "AUTOMATIC" if judgment is not None else "NONE"
    judged_at = now if judgment is not None else None
    database.execute_sql(
        """
        UPDATE run
        SET state = ?, judgment = ?, judgment_source = ?, judged_at = ?,
            fault_type = NULL, manual_judgment_revision = NULL,
            terminal_code = ?, terminal_summary = ?, terminal_at = ?,
            updated_at = ?
        WHERE run_id = ? AND state = 'ACTIVE'
        """,
        (
            state,
            judgment,
            source,
            judged_at,
            code,
            summary,
            now,
            now,
            run_id,
        ),
    )


class AttemptRepository:
    def __init__(
        self,
        runtime: RunPersistenceDatabase,
        *,
        result_artifact_store: AnalysisResultArtifactStore | None = None,
    ) -> None:
        self._runtime = runtime
        if (
            result_artifact_store is not None
            and not isinstance(
                result_artifact_store,
                AnalysisResultArtifactStore,
            )
        ):
            raise TypeError(
                "result_artifact_store must be an AnalysisResultArtifactStore"
            )
        self._result_artifact_store = result_artifact_store

    def _write(self, work) -> TransactionResult:
        with self._runtime.operation() as database:
            result = run_short_transaction(database, work)
            if result.state is TransactionState.UNKNOWN:
                self._runtime.quarantine_unknown()
        return result

    def create_queued(
        self,
        attempt: AttemptInsert,
    ) -> TransactionResult[bool]:
        if type(attempt) is not AttemptInsert:
            raise TypeError("attempt must be an exact AttemptInsert")
        attempt.__post_init__()

        def insert(database):
            cursor = database.execute_sql(
                """
                INSERT INTO analysis_attempt (
                    attempt_id, run_id, audio_id, attempt_kind, state,
                    judgment, result_payload_json, error_code, error_summary,
                    queued_at, started_at, terminal_at, updated_at
                ) VALUES (?, ?, ?, ?, 'QUEUED', NULL, NULL, NULL, NULL,
                          ?, NULL, NULL, ?)
                """,
                (
                    attempt.attempt_id,
                    attempt.run_id,
                    attempt.audio_id,
                    attempt.attempt_kind.value,
                    attempt.queued_at,
                    attempt.queued_at,
                ),
            )
            return cursor.rowcount == 1

        result = self._write(insert)
        if result.state is TransactionState.ROLLED_BACK:
            return TransactionResult(
                state=TransactionState.ROLLED_BACK,
                reason=f"ATTEMPT_QUEUE_DB_FAILED: {result.reason}",
            )
        return result

    def mark_running(
        self, attempt_id: str, now: str
    ) -> TransactionResult[bool]:
        validate_uuid4(attempt_id)
        validate_utc_timestamp(now)

        def update(database):
            cursor = database.execute_sql(
                """
                UPDATE analysis_attempt
                SET state = 'RUNNING', started_at = ?, updated_at = ?
                WHERE attempt_id = ? AND state = 'QUEUED'
                """,
                (now, now, attempt_id),
            )
            return cursor.rowcount == 1

        return self._write(update)

    def finish_queued_and_project(
        self,
        attempt_id: str,
        outcome: AttemptOutcome,
        now: str,
    ) -> TransactionResult[bool]:
        if (
            type(outcome) is AttemptOutcome
            and outcome.state in (AttemptState.SUCCEEDED, AttemptState.NO_RESULT)
        ):
            raise ContractValidationError(
                "queued terminal outcome must be FAILED, CANCELED, or INTERRUPTED"
            )
        return self._finish_and_project(
            AttemptState.QUEUED,
            attempt_id,
            outcome,
            now,
        )

    def finish_running_and_project(
        self,
        attempt_id: str,
        outcome: AttemptOutcome,
        now: str,
    ) -> TransactionResult[bool]:
        return self._finish_and_project(
            AttemptState.RUNNING,
            attempt_id,
            outcome,
            now,
        )

    def _finish_and_project(
        self,
        expected_state: AttemptState,
        attempt_id: str,
        outcome: AttemptOutcome,
        now: str,
    ) -> TransactionResult[bool]:
        validate_uuid4(attempt_id)
        if type(outcome) is not AttemptOutcome:
            raise TypeError("outcome must be an exact AttemptOutcome")
        validate_utc_timestamp(now)

        published: PublishedAnalysisResult | None = None
        stored_outcome = outcome
        if (
            outcome.state is AttemptState.SUCCEEDED
            and self._result_artifact_store is not None
        ):
            assert outcome.judgment is not None
            assert outcome.result_payload_json is not None
            published = self._result_artifact_store.publish(
                attempt_id,
                outcome.judgment.value,
                outcome.result_payload_json,
                precompressed_zlib=outcome.result_payload_zlib,
                prevalidated_json=True,
            )
            stored_outcome = AttemptOutcome(
                state=outcome.state,
                judgment=outcome.judgment,
                result_payload_json=published.reference_json,
                code=outcome.code,
                summary=outcome.summary,
            )

        def finish(database):
            row = database.execute_sql(
                """
                SELECT run_id, attempt_kind
                FROM analysis_attempt
                WHERE attempt_id = ? AND state = ?
                """,
                (attempt_id, expected_state.value),
            ).fetchone()
            if row is None:
                return False
            cursor = database.execute_sql(
                """
                UPDATE analysis_attempt
                SET state = ?, judgment = ?, result_payload_json = ?,
                    error_code = ?, error_summary = ?, terminal_at = ?,
                    updated_at = ?
                WHERE attempt_id = ? AND state = ?
                """,
                (
                    stored_outcome.state.value,
                    (
                        stored_outcome.judgment.value
                        if stored_outcome.judgment is not None
                        else None
                    ),
                    stored_outcome.result_payload_json,
                    stored_outcome.code,
                    stored_outcome.summary,
                    now,
                    now,
                    attempt_id,
                    expected_state.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            run_id, kind = row
            if kind == AttemptKind.INITIAL.value:
                code = (
                    "ANALYSIS_SUCCEEDED"
                    if stored_outcome.state is AttemptState.SUCCEEDED
                    else stored_outcome.code
                )
                assert code is not None
                _project_initial_run(
                    database,
                    run_id,
                    outcome.state.value,
                    code,
                    stored_outcome.summary,
                    now,
                    (
                        stored_outcome.judgment.value
                        if stored_outcome.judgment is not None
                        else None
                    ),
                )
            elif (
                expected_state is AttemptState.RUNNING
                and stored_outcome.state is AttemptState.SUCCEEDED
            ):
                run_cursor = database.execute_sql(
                    """
                    UPDATE run
                    SET judgment = ?, judgment_source = 'AUTOMATIC',
                        judged_at = ?, fault_type = NULL,
                        manual_judgment_revision = NULL, updated_at = ?
                    WHERE run_id = ? AND state <> 'ACTIVE'
                    """,
                    (stored_outcome.judgment.value, now, now, run_id),
                )
                if run_cursor.rowcount != 1:
                    raise RuntimeError(
                        "reanalysis run projection did not update exactly one "
                        "terminal run"
                    )
            return True

        try:
            result = self._write(finish)
        except Exception:
            self._settle_artifact(published, retain=False)
            raise
        retain = result.state is TransactionState.UNKNOWN or (
            result.state is TransactionState.COMMITTED
            and result.value is True
        )
        self._settle_artifact(published, retain=retain)
        return result

    def list_for_run(self, run_id: str) -> tuple[AttemptRecord, ...]:
        validate_uuid4(run_id)
        with self._runtime.operation() as database:
            rows = database.execute_sql(
                f"""
                SELECT {_ATTEMPT_COLUMNS}
                FROM analysis_attempt
                WHERE run_id = ?
                ORDER BY queued_at ASC, attempt_id ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def latest_for_run(self, run_id: str) -> AttemptRecord | None:
        validate_uuid4(run_id)
        with self._runtime.operation() as database:
            row = database.execute_sql(
                f"""
                SELECT {_ATTEMPT_COLUMNS}
                FROM analysis_attempt
                WHERE run_id = ?
                ORDER BY queued_at DESC, attempt_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def latest_successful_for_run(self, run_id: str) -> AttemptRecord | None:
        validate_uuid4(run_id)
        with self._runtime.operation() as database:
            row = database.execute_sql(
                f"""
                SELECT {_ATTEMPT_COLUMNS}
                FROM analysis_attempt
                WHERE run_id = ? AND state = 'SUCCEEDED'
                ORDER BY terminal_at DESC, attempt_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def _record(self, row) -> AttemptRecord:
        record = AttemptRecord(*row)
        payload = record.result_payload_json
        if (
            self._result_artifact_store is None
            or not self._result_artifact_store.is_reference(payload)
        ):
            return record
        assert payload is not None
        hydrated = self._result_artifact_store.load_reference(
            payload,
            expected_judgment=record.judgment,
        )
        return AttemptRecord(
            attempt_id=record.attempt_id,
            run_id=record.run_id,
            audio_id=record.audio_id,
            attempt_kind=record.attempt_kind,
            state=record.state,
            judgment=record.judgment,
            result_payload_json=hydrated,
            error_code=record.error_code,
            error_summary=record.error_summary,
            queued_at=record.queued_at,
            started_at=record.started_at,
            terminal_at=record.terminal_at,
            updated_at=record.updated_at,
        )

    def _settle_artifact(
        self,
        published: PublishedAnalysisResult | None,
        *,
        retain: bool,
    ) -> None:
        if published is None or self._result_artifact_store is None:
            return
        try:
            self._result_artifact_store.settle_publication(
                published,
                retain=retain,
            )
        except (OSError, RuntimeError, ValueError):
            pass
