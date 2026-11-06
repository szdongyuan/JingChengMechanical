"""Narrow repository for accepted run facts and their current projection."""

from __future__ import annotations

from dataclasses import dataclass
import json

from app.service.fault_types import normalize_fault_types
from app.service.run_persistence.analysis_envelope import (
    decode_analysis_envelope,
)
from app.service.run_persistence.contracts import (
    ContractValidationError,
    CurrentTrace,
    Judgment,
    RunInsert,
    RunState,
    TransactionResult,
    TransactionState,
    validate_utc_timestamp,
    validate_uuid4,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.transactions import run_short_transaction


@dataclass(frozen=True)
class RunTerminal:
    state: RunState
    code: str
    summary: str | None
    terminal_at: str
    judgment: Judgment | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, RunState) or self.state is RunState.ACTIVE:
            raise ContractValidationError("terminal state must be a terminal RunState")
        if not isinstance(self.code, str) or not self.code:
            raise ContractValidationError("terminal code must be a non-empty string")
        if self.summary is not None and not isinstance(self.summary, str):
            raise ContractValidationError("terminal summary must be a string or None")
        validate_utc_timestamp(self.terminal_at)
        if self.state is RunState.SUCCEEDED:
            if not isinstance(self.judgment, Judgment):
                raise ContractValidationError(
                    "successful terminal requires a Judgment"
                )
        elif self.judgment is not None:
            raise ContractValidationError(
                "only successful terminal may carry a judgment"
            )


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    ingress_kind: str
    trigger_source: str
    config_id: int
    hardware_id: int | None
    imported_source_identity: str | None
    imported_source_path: str | None
    imported_expected_frame_count: int | None
    causal_revision: int
    config_snapshot_json: str
    hardware_snapshot_json: str
    calibration_snapshot_json: str
    state: str
    judgment: str | None
    judgment_source: str
    judged_at: str | None
    terminal_code: str | None
    terminal_summary: str | None
    accepted_at: str
    terminal_at: str | None
    updated_at: str
    current_trace_json: str | None = None
    fault_type: str | None = None
    manual_judgment_revision: int | None = None

    @property
    def current_trace(self) -> CurrentTrace | None:
        if self.current_trace_json is None:
            return None
        return _decode_current_trace_json(self.current_trace_json)


_RUN_COLUMNS = (
    "run_id, ingress_kind, trigger_source, config_id, hardware_id, "
    "imported_source_identity, imported_source_path, "
    "imported_expected_frame_count, causal_revision, "
    "config_snapshot_json, hardware_snapshot_json, calibration_snapshot_json, "
    "state, judgment, judgment_source, judged_at, terminal_code, "
    "terminal_summary, accepted_at, terminal_at, updated_at, "
    "current_trace_json, fault_type, manual_judgment_revision"
)


class _ConditionalWriteRejected(RuntimeError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _encode_current_trace(trace: CurrentTrace) -> str:
    if type(trace) is not CurrentTrace:
        raise TypeError("trace must be an exact CurrentTrace")
    trace.__post_init__()
    return json.dumps(
        {
            "schema": "run-current-trace/v1",
            "unit": "A",
            "samples": [list(sample) for sample in trace.samples],
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_current_trace_json(value: str) -> CurrentTrace:
    if type(value) is not str:
        raise ContractValidationError("current trace JSON must be a string")
    try:
        decoded = json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
        if type(decoded) is not dict or set(decoded) != {
            "schema",
            "unit",
            "samples",
        }:
            raise ValueError("current trace must have exact closed shape")
        if (
            decoded["schema"] != "run-current-trace/v1"
            or decoded["unit"] != "A"
            or type(decoded["samples"]) is not list
        ):
            raise ValueError("current trace schema, unit, or samples is invalid")
        samples = decoded["samples"]
        if not samples or any(
            type(sample) is not list or len(sample) != 2
            for sample in samples
        ):
            raise ValueError("current trace samples are invalid")
        trace = CurrentTrace(
            samples=tuple((sample[0], sample[1]) for sample in samples)
        )
        if _encode_current_trace(trace) != value:
            raise ValueError("current trace JSON must be canonical")
        return trace
    except (ContractValidationError, TypeError, ValueError, json.JSONDecodeError):
        raise ContractValidationError("current trace JSON is invalid") from None


class RunRepository:
    def __init__(self, runtime: RunPersistenceDatabase) -> None:
        self._runtime = runtime

    def _write(self, work) -> TransactionResult:
        with self._runtime.operation() as database:
            result = run_short_transaction(database, work)
            if result.state is TransactionState.UNKNOWN:
                self._runtime.quarantine_unknown()
        return result

    def insert_active(self, frozen_run: RunInsert) -> TransactionResult[bool]:
        def insert(database):
            cursor = database.execute_sql(
                """
                INSERT INTO run (
                    run_id, ingress_kind, trigger_source, config_id, hardware_id,
                    imported_source_identity, imported_source_path,
                    imported_expected_frame_count, causal_revision,
                    config_snapshot_json, hardware_snapshot_json,
                    calibration_snapshot_json, current_trace_json, state,
                    judgment, judgment_source, fault_type,
                    manual_judgment_revision, judged_at, terminal_code,
                    terminal_summary, accepted_at, terminal_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, 'ACTIVE', NULL,
                          'NONE', NULL, NULL, NULL, NULL, NULL, ?, NULL, ?)
                """,
                (
                    frozen_run.run_id,
                    frozen_run.ingress_kind.value,
                    frozen_run.trigger_source.value,
                    frozen_run.config_id,
                    frozen_run.hardware_id,
                    frozen_run.imported_source_identity,
                    frozen_run.imported_source_path,
                    frozen_run.imported_expected_frame_count,
                    frozen_run.snapshots.config_snapshot_json,
                    frozen_run.snapshots.hardware_snapshot_json,
                    frozen_run.snapshots.calibration_snapshot_json,
                    frozen_run.accepted_at,
                    frozen_run.accepted_at,
                ),
            )
            return cursor.rowcount == 1

        return self._write(insert)

    def advance_causal_revision(
        self,
        run_id: str,
        *,
        expected_revision: object,
        proposed_revision: int,
    ) -> TransactionResult[bool]:
        from app.service.multi_run_coordinator_mvp.contracts import ABSENT

        validate_uuid4(run_id)
        if expected_revision is ABSENT:
            durable_expected = 0
        elif type(expected_revision) is int and expected_revision >= 0:
            durable_expected = expected_revision
        else:
            raise ContractValidationError(
                "expected_revision must be ABSENT or a nonnegative integer"
            )
        if (
            type(proposed_revision) is not int
            or proposed_revision != durable_expected + 1
        ):
            raise ContractValidationError(
                "proposed_revision must equal durable expected revision plus one"
            )

        def advance(database):
            cursor = database.execute_sql(
                """
                UPDATE run
                SET causal_revision = ?
                WHERE run_id = ? AND causal_revision = ?
                """,
                (proposed_revision, run_id, durable_expected),
            )
            if cursor.rowcount != 1:
                raise _ConditionalWriteRejected(
                    "CAUSAL_REVISION_COMPARE_AND_SWAP_REJECTED"
                )
            return True

        return self._write(advance)

    def finish_active(
        self, run_id: str, terminal: RunTerminal
    ) -> TransactionResult[bool]:
        validate_uuid4(run_id)
        judgment = (
            terminal.judgment.value if terminal.judgment is not None else None
        )
        judgment_source = "AUTOMATIC" if judgment is not None else "NONE"
        judged_at = terminal.terminal_at if judgment is not None else None

        def finish(database):
            cursor = database.execute_sql(
                """
                UPDATE run
                SET state = ?, judgment = ?, judgment_source = ?, judged_at = ?,
                    fault_type = NULL, manual_judgment_revision = NULL,
                    terminal_code = ?, terminal_summary = ?, terminal_at = ?,
                    updated_at = ?
                WHERE run_id = ? AND state = 'ACTIVE'
                """,
                (
                    terminal.state.value,
                    judgment,
                    judgment_source,
                    judged_at,
                    terminal.code,
                    terminal.summary,
                    terminal.terminal_at,
                    terminal.terminal_at,
                    run_id,
                ),
            )
            return cursor.rowcount == 1

        return self._write(finish)

    def attach_current_trace(
        self,
        run_id: str,
        trace: CurrentTrace,
    ) -> TransactionResult[bool]:
        validate_uuid4(run_id)
        encoded = _encode_current_trace(trace)

        def attach(database):
            cursor = database.execute_sql(
                """
                UPDATE run
                SET current_trace_json = ?
                WHERE run_id = ?
                  AND state = 'ACTIVE'
                  AND current_trace_json IS NULL
                """,
                (encoded, run_id),
            )
            if cursor.rowcount != 1:
                raise _ConditionalWriteRejected(
                    "CURRENT_TRACE_ROW_NOT_ELIGIBLE"
                )
            return True

        return self._write(attach)

    def set_manual_judgment(
        self,
        run_id: str,
        judgment: Judgment,
        now: str,
        *,
        expected_revision: int = 0,
        fault_type: str | None = None,
        accepted_fault_types: tuple[str, ...] = (),
    ) -> TransactionResult[bool]:
        validate_uuid4(run_id)
        if not isinstance(judgment, Judgment):
            raise ContractValidationError("judgment must be a Judgment")
        validate_utc_timestamp(now)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ContractValidationError(
                "expected_revision must be a nonnegative integer"
            )
        if type(accepted_fault_types) is not tuple:
            raise ContractValidationError(
                "accepted_fault_types must be an exact tuple"
            )
        try:
            normalized_fault_types = tuple(
                normalize_fault_types(list(accepted_fault_types))
            )
        except ValueError:
            raise ContractValidationError(
                "accepted_fault_types is invalid"
            ) from None
        if normalized_fault_types != accepted_fault_types:
            raise ContractValidationError(
                "accepted_fault_types must already be normalized"
            )
        valid_fault = (
            judgment is Judgment.PASS
            and fault_type is None
        ) or (
            judgment is Judgment.FAIL
            and (
                (
                    bool(accepted_fault_types)
                    and type(fault_type) is str
                    and fault_type in accepted_fault_types
                )
                or (not accepted_fault_types and fault_type is None)
            )
        )
        if not valid_fault:
            return TransactionResult(
                state=TransactionState.ROLLED_BACK,
                reason="MANUAL_FAULT_TYPE_MISMATCH",
            )
        committed_revision = expected_revision + 1

        def update(database):
            row = database.execute_sql(
                """
                SELECT config_snapshot_json
                FROM run
                WHERE run_id = ? AND state <> 'ACTIVE'
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise _ConditionalWriteRejected(
                    "MANUAL_JUDGMENT_ROW_NOT_ELIGIBLE"
                )
            try:
                config = decode_analysis_envelope(row[0]).config
                frozen_value = config["fault_types"]
                frozen_fault_types = tuple(
                    normalize_fault_types(frozen_value)
                )
            except (KeyError, TypeError, ValueError):
                raise _ConditionalWriteRejected(
                    "DURABLE_FAULT_TYPES_INVALID"
                ) from None
            if (
                type(frozen_value) is not tuple
                or tuple(frozen_value) != frozen_fault_types
                or frozen_fault_types != accepted_fault_types
            ):
                raise _ConditionalWriteRejected(
                    "DURABLE_FAULT_TYPES_MISMATCH"
                )
            cursor = database.execute_sql(
                """
                UPDATE run
                SET judgment = ?, judgment_source = 'MANUAL',
                    judged_at = ?, fault_type = ?,
                    manual_judgment_revision = ?, updated_at = ?
                WHERE run_id = ? AND state <> 'ACTIVE'
                  AND (
                    manual_judgment_revision IS NULL
                    OR manual_judgment_revision = ?
                  )
                """,
                (
                    judgment.value,
                    now,
                    fault_type,
                    committed_revision,
                    now,
                    run_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise _ConditionalWriteRejected(
                    "MANUAL_JUDGMENT_ROW_NOT_ELIGIBLE"
                )
            return True

        return self._write(update)

    def get(self, run_id: str) -> RunRecord | None:
        validate_uuid4(run_id)
        with self._runtime.operation() as database:
            row = database.execute_sql(
                f"SELECT {_RUN_COLUMNS} FROM run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else RunRecord(*row)
