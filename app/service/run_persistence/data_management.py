"""Immutable run-v2 data-management query and deletion authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import ceil
from pathlib import Path

from app.service.fault_types import normalize_fault_type_name
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.contracts import (
    ContractValidationError,
    TransactionState,
    validate_utc_timestamp,
    validate_uuid4,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.transactions import run_short_transaction
from model.malfunction_type_table import (
    BAD_MALFUNCTION_TYPE_NAME,
    BUILTIN_MALFUNCTION_TYPES,
    GOOD_MALFUNCTION_TYPE_NAME,
)


class ManagementJudgment(str, Enum):
    OK = "OK"
    NG = "NG"
    UNJUDGED = "UNJUDGED"


_PAGE_SIZES = frozenset({20, 50, 100, 200})


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_exact_string(value: object, name: str) -> str:
    if type(value) is not str:
        raise ContractValidationError(f"{name} must be an exact string")
    return value


def _require_optional_exact_string(value: object, name: str) -> str | None:
    if value is not None and type(value) is not str:
        raise ContractValidationError(f"{name} must be an exact string or None")
    return value


def _require_optional_exact_int(value: object, name: str) -> int | None:
    if value is not None and type(value) is not int:
        raise ContractValidationError(f"{name} must be an exact integer or None")
    return value


@dataclass(frozen=True, slots=True)
class RunManagementQuery:
    model: str
    sn_contains: str
    accepted_start_utc: str | None
    accepted_end_exclusive_utc: str | None
    judgments: frozenset[ManagementJudgment]
    page: int
    page_size: int
    fault_type_contains: str = ""

    def __post_init__(self) -> None:
        _require_exact_string(self.model, "model")
        _require_exact_string(self.sn_contains, "sn_contains")
        _require_exact_string(
            self.fault_type_contains,
            "fault_type_contains",
        )
        for value, name in (
            (self.accepted_start_utc, "accepted_start_utc"),
            (
                self.accepted_end_exclusive_utc,
                "accepted_end_exclusive_utc",
            ),
        ):
            _require_optional_exact_string(value, name)
            if value is not None:
                validate_utc_timestamp(value)
        if (
            self.accepted_start_utc is not None
            and self.accepted_end_exclusive_utc is not None
            and self.accepted_start_utc >= self.accepted_end_exclusive_utc
        ):
            raise ContractValidationError(
                "accepted start must be before accepted end"
            )
        if type(self.judgments) is not frozenset or any(
            type(value) is not ManagementJudgment for value in self.judgments
        ):
            raise ContractValidationError(
                "judgments must be a frozenset of ManagementJudgment"
            )
        if type(self.page) is not int or self.page < 1:
            raise ContractValidationError("page must be an integer at least one")
        if type(self.page_size) is not int or self.page_size not in _PAGE_SIZES:
            raise ContractValidationError(
                "page_size must be one of 20, 50, 100, or 200"
            )


@dataclass(frozen=True, slots=True)
class RunManagementRow:
    run_id: str
    accepted_at: str
    sn: str
    model: str
    ingress_kind: str
    trigger_source: str
    config_id: int | None
    hardware_id: str | None
    state: str
    judgment: str | None
    judgment_source: str | None
    fault_type: str | None
    manual_judgment_revision: int | None
    terminal_code: str | None
    terminal_at: str | None

    def __post_init__(self) -> None:
        validate_uuid4(_require_exact_string(self.run_id, "run_id"))
        validate_utc_timestamp(
            _require_exact_string(self.accepted_at, "accepted_at")
        )
        for value, name in (
            (self.sn, "sn"),
            (self.model, "model"),
            (self.ingress_kind, "ingress_kind"),
            (self.trigger_source, "trigger_source"),
            (self.state, "state"),
        ):
            _require_exact_string(value, name)
        _require_optional_exact_int(self.config_id, "config_id")
        _require_optional_exact_string(self.hardware_id, "hardware_id")
        for value, name in (
            (self.judgment, "judgment"),
            (self.judgment_source, "judgment_source"),
            (self.fault_type, "fault_type"),
            (self.terminal_code, "terminal_code"),
            (self.terminal_at, "terminal_at"),
        ):
            _require_optional_exact_string(value, name)
        _require_optional_exact_int(
            self.manual_judgment_revision,
            "manual_judgment_revision",
        )
        if self.terminal_at is not None:
            validate_utc_timestamp(self.terminal_at)


@dataclass(frozen=True, slots=True)
class RunManagementPage:
    rows: tuple[RunManagementRow, ...]
    total_count: int
    page: int
    page_size: int
    total_pages: int

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple or any(
            type(row) is not RunManagementRow for row in self.rows
        ):
            raise ContractValidationError(
                "rows must be a tuple of RunManagementRow"
            )
        for value, name in (
            (self.total_count, "total_count"),
            (self.page, "page"),
            (self.total_pages, "total_pages"),
        ):
            if type(value) is not int or value < (0 if name == "total_count" else 1):
                raise ContractValidationError(f"{name} is invalid")
        if type(self.page_size) is not int or self.page_size not in _PAGE_SIZES:
            raise ContractValidationError("page_size is invalid")
        expected_pages = max(1, ceil(self.total_count / self.page_size))
        if self.total_pages != expected_pages or self.page > self.total_pages:
            raise ContractValidationError("page metadata is inconsistent")
        if len(self.rows) > self.page_size or len(self.rows) > self.total_count:
            raise ContractValidationError("page rows are inconsistent")


class DeleteRunStatus(str, Enum):
    DELETED = "DELETED"
    PARTIAL_FILE_REMAINS = "PARTIAL_FILE_REMAINS"
    REJECTED_ACTIVE = "REJECTED_ACTIVE"
    REJECTED_ATTEMPT = "REJECTED_ATTEMPT"
    NOT_FOUND = "NOT_FOUND"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DeleteRunResult:
    run_id: str
    status: DeleteRunStatus
    reason: str
    residual_path: str | None
    database_committed: bool

    def __post_init__(self) -> None:
        validate_uuid4(_require_exact_string(self.run_id, "run_id"))
        if type(self.status) is not DeleteRunStatus:
            raise ContractValidationError("status must be a DeleteRunStatus")
        if not _require_exact_string(self.reason, "reason"):
            raise ContractValidationError("reason must not be empty")
        _require_optional_exact_string(self.residual_path, "residual_path")
        if type(self.database_committed) is not bool:
            raise ContractValidationError(
                "database_committed must be an exact boolean"
            )


@dataclass(frozen=True, slots=True)
class DeleteBatchReport:
    results: tuple[DeleteRunResult, ...]
    unattempted_run_ids: tuple[str, ...]
    restart_required: bool

    def __post_init__(self) -> None:
        if type(self.results) is not tuple or any(
            type(result) is not DeleteRunResult for result in self.results
        ):
            raise ContractValidationError(
                "results must be a tuple of DeleteRunResult"
            )
        if type(self.unattempted_run_ids) is not tuple:
            raise ContractValidationError(
                "unattempted_run_ids must be an exact tuple"
            )
        for run_id in self.unattempted_run_ids:
            validate_uuid4(_require_exact_string(run_id, "run_id"))
        if type(self.restart_required) is not bool:
            raise ContractValidationError(
                "restart_required must be an exact boolean"
            )


def _json_text_projection(primary: str, fallback: str) -> str:
    root = "r.config_snapshot_json"
    workorder = "$.workorder"
    primary_path = f"{workorder}.{primary}"
    fallback_path = f"{workorder}.{fallback}"
    return f"""
        CASE
          WHEN json_valid({root}) = 0 THEN ''
          WHEN json_type({root}) <> 'object' THEN ''
          WHEN json_type({root}, '{workorder}') <> 'object' THEN ''
          WHEN json_type({root}, '{primary_path}') = 'text'
               AND trim(json_extract({root}, '{primary_path}')) <> ''
            THEN trim(json_extract({root}, '{primary_path}'))
          WHEN json_type({root}, '{fallback_path}') = 'text'
               AND trim(json_extract({root}, '{fallback_path}')) <> ''
            THEN trim(json_extract({root}, '{fallback_path}'))
          ELSE ''
        END
    """


_SN_EXPRESSION = _json_text_projection("sn", "sn_code")
_MODEL_EXPRESSION = _json_text_projection("model", "product_model")
_ROW_COLUMNS = f"""
    r.run_id,
    r.accepted_at,
    {_SN_EXPRESSION} AS sn,
    {_MODEL_EXPRESSION} AS model,
    r.ingress_kind,
    r.trigger_source,
    r.config_id,
    r.hardware_id,
    r.state,
    r.judgment,
    r.judgment_source,
    r.fault_type,
    r.manual_judgment_revision,
    r.terminal_code,
    r.terminal_at
"""


def _escape_like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matches_builtin_fault_type_filter(
    query: str,
    builtin_name: str,
) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return False
    for _type_id, number, name in BUILTIN_MALFUNCTION_TYPES:
        if name != builtin_name:
            continue
        labels = (
            name,
            number,
            f"{name}（{number}）",
            f"{name}({number})",
        )
        return any(needle in label.casefold() for label in labels)
    return needle in builtin_name.casefold()


def _query_predicate(
    request: RunManagementQuery,
) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    parameters: list[object] = []
    model = request.model.strip()
    if model:
        clauses.append(f"({_MODEL_EXPRESSION}) = ? COLLATE NOCASE")
        parameters.append(model)
    sn_contains = request.sn_contains.strip()
    if sn_contains:
        clauses.append(
            f"({_SN_EXPRESSION}) LIKE ? ESCAPE '\\' COLLATE NOCASE"
        )
        parameters.append(f"%{_escape_like_literal(sn_contains)}%")
    fault_type_contains = request.fault_type_contains.strip()
    if fault_type_contains:
        fault_type_clauses = [
            "coalesce(r.fault_type, '') LIKE ? ESCAPE '\\' COLLATE NOCASE"
        ]
        if _matches_builtin_fault_type_filter(
            fault_type_contains,
            GOOD_MALFUNCTION_TYPE_NAME,
        ):
            fault_type_clauses.append("r.judgment = 'PASS'")
        if _matches_builtin_fault_type_filter(
            fault_type_contains,
            BAD_MALFUNCTION_TYPE_NAME,
        ):
            fault_type_clauses.append(
                "(r.judgment = 'FAIL' AND "
                "(r.fault_type IS NULL OR trim(r.fault_type) = ''))"
            )
        clauses.append(
            f"({' OR '.join(fault_type_clauses)})"
        )
        parameters.append(
            f"%{_escape_like_literal(fault_type_contains)}%"
        )
    if request.accepted_start_utc is not None:
        clauses.append("r.accepted_at >= ?")
        parameters.append(request.accepted_start_utc)
    if request.accepted_end_exclusive_utc is not None:
        clauses.append("r.accepted_at < ?")
        parameters.append(request.accepted_end_exclusive_utc)
    judgment_clauses: list[str] = []
    if ManagementJudgment.OK in request.judgments:
        judgment_clauses.append("r.judgment = 'PASS'")
    if ManagementJudgment.NG in request.judgments:
        judgment_clauses.append("r.judgment = 'FAIL'")
    if ManagementJudgment.UNJUDGED in request.judgments:
        judgment_clauses.append("r.judgment IS NULL")
    clauses.append(
        f"({' OR '.join(judgment_clauses)})" if judgment_clauses else "0"
    )
    return " AND ".join(clauses), tuple(parameters)


_REJECTION_SEPARATOR = "|"


class _DeletionRejected(RuntimeError):
    def __init__(self, status: DeleteRunStatus, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"{status.value}{_REJECTION_SEPARATOR}{reason}")


def _decode_rejection(reason: str | None) -> tuple[DeleteRunStatus, str]:
    if reason is not None and _REJECTION_SEPARATOR in reason:
        code, detail = reason.split(_REJECTION_SEPARATOR, 1)
        try:
            status = DeleteRunStatus(code)
        except ValueError:
            pass
        else:
            if status in {
                DeleteRunStatus.REJECTED_ACTIVE,
                DeleteRunStatus.REJECTED_ATTEMPT,
                DeleteRunStatus.NOT_FOUND,
            }:
                return status, detail
    return DeleteRunStatus.ROLLED_BACK, reason or "database transaction rolled back"


@dataclass(frozen=True, slots=True)
class _DeletedRunFiles:
    audio_path: str | None
    result_payloads: tuple[str, ...]


def _delete_work(database, run_id: str) -> _DeletedRunFiles:
    row = database.execute_sql(
        "SELECT state FROM run WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise _DeletionRejected(
            DeleteRunStatus.NOT_FOUND,
            "run not found",
        )
    if row[0] == "ACTIVE":
        raise _DeletionRejected(
            DeleteRunStatus.REJECTED_ACTIVE,
            "active run cannot be deleted",
        )
    active_attempt = database.execute_sql(
        """
        SELECT 1
        FROM analysis_attempt
        WHERE run_id = ? AND state IN ('QUEUED', 'RUNNING')
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if active_attempt is not None:
        raise _DeletionRejected(
            DeleteRunStatus.REJECTED_ATTEMPT,
            "queued or running analysis attempt prevents deletion",
        )
    audio = database.execute_sql(
        "SELECT final_path FROM audio WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    final_path = None if audio is None else audio[0]
    result_payloads = tuple(
        row[0]
        for row in database.execute_sql(
            """
            SELECT result_payload_json
            FROM analysis_attempt
            WHERE run_id = ? AND result_payload_json IS NOT NULL
            """,
            (run_id,),
        ).fetchall()
        if type(row[0]) is str
    )
    database.execute_sql(
        "DELETE FROM analysis_attempt WHERE run_id = ?",
        (run_id,),
    )
    database.execute_sql(
        "DELETE FROM audio WHERE run_id = ?",
        (run_id,),
    )
    deleted = database.execute_sql(
        "DELETE FROM run WHERE run_id = ?",
        (run_id,),
    )
    if deleted.rowcount != 1:
        raise RuntimeError(
            f"run delete affected {deleted.rowcount} rows instead of exactly one"
        )
    return _DeletedRunFiles(final_path, result_payloads)


class RunDataManagementService:
    def __init__(
        self,
        runtime: RunPersistenceDatabase,
        *,
        result_artifact_store: AnalysisResultArtifactStore | None = None,
    ) -> None:
        if not isinstance(runtime, RunPersistenceDatabase):
            raise TypeError("runtime must be a RunPersistenceDatabase")
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
        self._runtime = runtime
        self._result_artifact_store = result_artifact_store

    def query_page(self, request: RunManagementQuery) -> RunManagementPage:
        if type(request) is not RunManagementQuery:
            raise TypeError("request must be an exact RunManagementQuery")
        request.__post_init__()
        predicate, parameters = _query_predicate(request)
        with self._runtime.operation() as database:
            database.begin()
            try:
                total_count = database.execute_sql(
                    f"SELECT COUNT(*) FROM run AS r WHERE {predicate}",
                    parameters,
                ).fetchone()[0]
                total_pages = max(1, ceil(total_count / request.page_size))
                page = min(request.page, total_pages)
                offset = (page - 1) * request.page_size
                raw_rows = database.execute_sql(
                    f"""
                    SELECT {_ROW_COLUMNS}
                    FROM run AS r
                    WHERE {predicate}
                    ORDER BY r.accepted_at DESC, r.run_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*parameters, request.page_size, offset),
                ).fetchall()
                database.commit()
            except Exception:
                database.rollback()
                raise
        normalized_rows = []
        for row in raw_rows:
            values = list(row)
            values[7] = None if values[7] is None else str(values[7])
            normalized_rows.append(RunManagementRow(*values))
        rows = tuple(normalized_rows)
        return RunManagementPage(
            rows=rows,
            total_count=total_count,
            page=page,
            page_size=request.page_size,
            total_pages=total_pages,
        )

    def update_fault_type(self, run_id: str, fault_type: str) -> bool:
        validate_uuid4(_require_exact_string(run_id, "run_id"))
        try:
            normalized_fault_type = normalize_fault_type_name(fault_type)
        except ValueError as exc:
            raise ContractValidationError(f"fault_type is invalid: {exc}") from None
        is_good = normalized_fault_type == GOOD_MALFUNCTION_TYPE_NAME
        judgment = "PASS" if is_good else "FAIL"
        stored_fault_type = None if is_good else normalized_fault_type
        now = _utc_now()

        def update(database):
            row = database.execute_sql(
                """
                SELECT judgment
                FROM run
                WHERE run_id = ? AND state <> 'ACTIVE'
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise _DeletionRejected(
                    DeleteRunStatus.NOT_FOUND,
                    "run not found or active",
                )
            cursor = database.execute_sql(
                """
                UPDATE run
                SET judgment = ?,
                    judgment_source = 'MANUAL',
                    fault_type = ?,
                    manual_judgment_revision =
                        CASE
                            WHEN manual_judgment_revision IS NULL THEN 1
                            ELSE manual_judgment_revision + 1
                        END,
                    judged_at = ?,
                    updated_at = ?
                WHERE run_id = ? AND state <> 'ACTIVE'
                """,
                (judgment, stored_fault_type, now, now, run_id),
            )
            if cursor.rowcount != 1:
                raise _DeletionRejected(
                    DeleteRunStatus.NOT_FOUND,
                    "run not found or not editable",
                )
            return True

        with self._runtime.operation() as database:
            transaction = run_short_transaction(database, update)
            if transaction.state is TransactionState.UNKNOWN:
                self._runtime.quarantine_unknown()

        if transaction.state is TransactionState.UNKNOWN:
            raise RuntimeError(
                transaction.reason or "database transaction state unknown"
            )
        if transaction.state is TransactionState.ROLLED_BACK:
            _status, reason = _decode_rejection(transaction.reason)
            raise RuntimeError(reason)
        return True

    def delete_one(self, run_id: str) -> DeleteRunResult:
        validate_uuid4(_require_exact_string(run_id, "run_id"))
        with self._runtime.operation() as database:
            transaction = run_short_transaction(
                database,
                lambda connection: _delete_work(connection, run_id),
            )
            if transaction.state is TransactionState.UNKNOWN:
                self._runtime.quarantine_unknown()

        if transaction.state is TransactionState.UNKNOWN:
            return DeleteRunResult(
                run_id=run_id,
                status=DeleteRunStatus.UNKNOWN,
                reason=transaction.reason or "database transaction state unknown",
                residual_path=None,
                database_committed=False,
            )
        if transaction.state is TransactionState.ROLLED_BACK:
            status, reason = _decode_rejection(transaction.reason)
            return DeleteRunResult(
                run_id=run_id,
                status=status,
                reason=reason,
                residual_path=None,
                database_committed=False,
            )

        deleted_files = transaction.value
        if deleted_files is None:
            deleted_files = _DeletedRunFiles(None, ())
        if type(deleted_files) is not _DeletedRunFiles:
            return DeleteRunResult(
                run_id=run_id,
                status=DeleteRunStatus.PARTIAL_FILE_REMAINS,
                reason="committed deletion file manifest is invalid",
                residual_path=str(deleted_files),
                database_committed=True,
            )
        final_path = deleted_files.audio_path
        if type(final_path) is not str or not final_path:
            if final_path is None:
                audio_error = None
            else:
                audio_error = DeleteRunResult(
                    run_id=run_id,
                    status=DeleteRunStatus.PARTIAL_FILE_REMAINS,
                    reason="committed audio final_path is invalid",
                    residual_path=str(final_path),
                    database_committed=True,
                )
        else:
            try:
                Path(final_path).unlink(missing_ok=True)
            except OSError as exc:
                audio_error = DeleteRunResult(
                    run_id=run_id,
                    status=DeleteRunStatus.PARTIAL_FILE_REMAINS,
                    reason=str(exc),
                    residual_path=final_path,
                    database_committed=True,
                )
            else:
                audio_error = None

        artifact_error = self._delete_result_artifacts(
            run_id,
            deleted_files.result_payloads,
        )
        if audio_error is not None:
            return audio_error
        if artifact_error is not None:
            return artifact_error
        return DeleteRunResult(
            run_id=run_id,
            status=DeleteRunStatus.DELETED,
            reason="deleted",
            residual_path=None,
            database_committed=True,
        )

    def _delete_result_artifacts(
        self,
        run_id: str,
        result_payloads: tuple[str, ...],
    ) -> DeleteRunResult | None:
        store = self._result_artifact_store
        if store is None:
            return None
        for payload in result_payloads:
            if not store.is_reference(payload):
                continue
            path: Path | None = None
            try:
                path = store.path_for_reference(payload)
                store.delete_reference(payload)
            except (OSError, RuntimeError, ValueError) as exc:
                residual = str(path) if path is not None else str(store.root)
                return DeleteRunResult(
                    run_id=run_id,
                    status=DeleteRunStatus.PARTIAL_FILE_REMAINS,
                    reason=f"analysis result artifact cleanup failed: {exc}",
                    residual_path=residual,
                    database_committed=True,
                )
        return None

    def delete_many(self, run_ids: tuple[str, ...]) -> DeleteBatchReport:
        if type(run_ids) is not tuple:
            raise TypeError("run_ids must be an exact tuple")
        for run_id in run_ids:
            validate_uuid4(_require_exact_string(run_id, "run_id"))
        results: list[DeleteRunResult] = []
        unattempted: tuple[str, ...] = ()
        for index, run_id in enumerate(run_ids):
            result = self.delete_one(run_id)
            results.append(result)
            if result.status is DeleteRunStatus.UNKNOWN:
                unattempted = run_ids[index + 1 :]
                break
        return DeleteBatchReport(
            results=tuple(results),
            unattempted_run_ids=unattempted,
            restart_required=bool(
                results and results[-1].status is DeleteRunStatus.UNKNOWN
            ),
        )
