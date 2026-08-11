"""Read-only projection of durable run facts into export values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.service.fault_types import normalize_fault_types
from app.service.run_persistence.analysis_envelope import (
    AnalysisEnvelopeError,
    decode_analysis_envelope,
)
from app.service.run_persistence.contracts import (
    ContractValidationError,
    IngressKind,
    Judgment,
    JudgmentSource,
    RunState,
    TriggerSource,
    _validate_channel_mapping,
    validate_utc_timestamp,
    validate_uuid4,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.run_repository import (
    _decode_current_trace_json,
)
from app.service.run_record_export.contracts import RunExportRecord


class RunExportReadError(RuntimeError):
    """Selected durable facts are not a complete valid export input."""


@dataclass(frozen=True)
class DurableRunListRow:
    run_id: str
    accepted_at: str
    ingress_kind: str
    trigger_source: str
    config_id: int
    hardware_id: int | None
    state: str
    judgment: str | None
    judgment_source: str
    fault_type: str | None
    manual_judgment_revision: int | None
    terminal_code: str | None
    terminal_at: str | None


@dataclass(frozen=True)
class _JoinedFacts:
    run_id: str
    accepted_at: str
    ingress_kind: str
    trigger_source: str
    config_id: int
    hardware_id: int | None
    config_snapshot_json: str
    current_trace_json: str | None
    state: str
    judgment: str | None
    judgment_source: str
    fault_type: str | None
    manual_judgment_revision: int | None
    judged_at: str | None
    terminal_code: str | None
    terminal_summary: str | None
    terminal_at: str | None
    updated_at: str
    audio_id: str | None
    audio_run_id: str | None
    final_path: str | None
    container_format: str | None
    sample_rate_hz: int | None
    channel_count: int | None
    frame_count: int | None
    duration_ms: int | None
    byte_size: int | None
    closed_verified_at: str | None
    audio_created_at: str | None


_LIST_COLUMNS = (
    "run_id, accepted_at, ingress_kind, trigger_source, config_id, "
    "hardware_id, state, judgment, judgment_source, fault_type, "
    "manual_judgment_revision, terminal_code, terminal_at"
)

_READ_COLUMNS = (
    "r.run_id, r.accepted_at, r.ingress_kind, r.trigger_source, "
    "r.config_id, r.hardware_id, r.config_snapshot_json, "
    "r.current_trace_json, r.state, r.judgment, r.judgment_source, "
    "r.fault_type, r.manual_judgment_revision, r.judged_at, "
    "r.terminal_code, r.terminal_summary, r.terminal_at, r.updated_at, "
    "a.audio_id, a.run_id, a.final_path, a.container_format, "
    "a.sample_rate_hz, a.channel_count, a.frame_count, a.duration_ms, "
    "a.byte_size, a.closed_verified_at, a.created_at"
)


class RunExportReader:
    def __init__(self, runtime: RunPersistenceDatabase) -> None:
        if not isinstance(runtime, RunPersistenceDatabase):
            raise TypeError("runtime must be a RunPersistenceDatabase")
        self._runtime = runtime

    def list_durable(self) -> tuple[DurableRunListRow, ...]:
        """List every durable run without applying export eligibility."""

        with self._runtime.operation() as database:
            rows = database.execute_sql(
                f"""
                SELECT {_LIST_COLUMNS}
                FROM run
                ORDER BY accepted_at DESC, run_id DESC
                """
            ).fetchall()
        return tuple(DurableRunListRow(*row) for row in rows)

    def read_many(
        self,
        run_ids: tuple[str, ...],
    ) -> tuple[RunExportRecord, ...]:
        """Read one ordered, all-or-nothing selection without source-file I/O."""

        selected = self._validate_selection(run_ids)
        placeholders = ", ".join("?" for _ in selected)
        with self._runtime.operation() as database:
            with database.atomic():
                rows = database.execute_sql(
                    f"""
                    SELECT {_READ_COLUMNS}
                    FROM run AS r
                    LEFT JOIN audio AS a ON a.run_id = r.run_id
                    WHERE r.run_id IN ({placeholders})
                    """,
                    selected,
                ).fetchall()

        grouped: dict[str, list[_JoinedFacts]] = {
            run_id: [] for run_id in selected
        }
        for row in rows:
            facts = _JoinedFacts(*row)
            if facts.run_id in grouped:
                grouped[facts.run_id].append(facts)

        records: list[RunExportRecord] = []
        for run_id in selected:
            matches = grouped[run_id]
            if len(matches) != 1:
                raise RunExportReadError(
                    f"run {run_id} is missing or has duplicate durable facts"
                )
            records.append(_to_export_record(matches[0]))
        return tuple(records)

    @staticmethod
    def _validate_selection(run_ids: tuple[str, ...]) -> tuple[str, ...]:
        if type(run_ids) is not tuple or not run_ids:
            raise RunExportReadError(
                "run selection must be a nonempty exact tuple"
            )
        try:
            for run_id in run_ids:
                validate_uuid4(run_id)
        except ContractValidationError:
            raise RunExportReadError(
                "run selection contains an invalid run ID"
            ) from None
        if len(set(run_ids)) != len(run_ids):
            raise RunExportReadError("run selection contains a duplicate run ID")
        return run_ids


def _to_export_record(facts: _JoinedFacts) -> RunExportRecord:
    try:
        _validate_run_facts(facts)
        channel_order, fault_types, sn, model = _decode_config_facts(
            facts.config_snapshot_json
        )
        _validate_judgment_facts(facts, fault_types)
        _validate_audio_facts(facts, channel_order)
        current_samples = (
            None
            if facts.current_trace_json is None
            else _decode_current_trace_json(
                facts.current_trace_json
            ).samples
        )
        assert facts.terminal_at is not None
        assert facts.final_path is not None
        assert facts.sample_rate_hz is not None
        assert facts.channel_count is not None
        assert facts.frame_count is not None
        return RunExportRecord(
            run_id=facts.run_id,
            accepted_at=facts.accepted_at,
            ingress_kind=facts.ingress_kind,
            trigger_source=facts.trigger_source,
            config_id=facts.config_id,
            hardware_id=facts.hardware_id,
            sn=sn,
            model=model,
            state=facts.state,
            judgment=facts.judgment,
            judgment_source=facts.judgment_source,
            fault_type=facts.fault_type,
            manual_judgment_revision=facts.manual_judgment_revision,
            terminal_code=facts.terminal_code,
            terminal_at=facts.terminal_at,
            source_wav_path=facts.final_path,
            sample_rate_hz=facts.sample_rate_hz,
            channel_count=facts.channel_count,
            frame_count=facts.frame_count,
            channel_order=channel_order,
            current_samples=current_samples,
        )
    except (
        AnalysisEnvelopeError,
        ContractValidationError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise RunExportReadError(
            f"run {facts.run_id} has malformed durable export facts: {exc}"
        ) from None


def _validate_run_facts(facts: _JoinedFacts) -> None:
    validate_uuid4(facts.run_id)
    validate_utc_timestamp(facts.accepted_at)
    validate_utc_timestamp(facts.updated_at)
    if facts.ingress_kind not in {item.value for item in IngressKind}:
        raise ContractValidationError("ingress kind is invalid")
    if facts.trigger_source not in {item.value for item in TriggerSource}:
        raise ContractValidationError("trigger source is invalid")
    if type(facts.config_id) is not int:
        raise ContractValidationError("config ID must be an integer")
    if facts.ingress_kind == IngressKind.CAPTURED.value:
        if (
            type(facts.hardware_id) is not int
            or facts.trigger_source == TriggerSource.IMPORT.value
        ):
            raise ContractValidationError(
                "captured hardware or trigger facts are invalid"
            )
    elif (
        facts.hardware_id is not None
        or facts.trigger_source != TriggerSource.IMPORT.value
    ):
        raise ContractValidationError(
            "imported hardware or trigger facts are invalid"
        )

    if facts.state not in {item.value for item in RunState}:
        raise ContractValidationError("run state is invalid")
    if facts.state == RunState.ACTIVE.value:
        raise ContractValidationError("active run is not exportable")
    if type(facts.terminal_code) is not str or not facts.terminal_code:
        raise ContractValidationError("terminal code is invalid")
    if (
        facts.terminal_summary is not None
        and type(facts.terminal_summary) is not str
    ):
        raise ContractValidationError("terminal summary is invalid")
    if facts.terminal_at is None:
        raise ContractValidationError("terminal timestamp is missing")
    validate_utc_timestamp(facts.terminal_at)


def _decode_config_facts(
    config_snapshot_json: str,
) -> tuple[tuple[int, ...], tuple[str, ...], str, str]:
    _validate_channel_mapping(config_snapshot_json)
    envelope = decode_analysis_envelope(config_snapshot_json)
    channel_order = envelope.channel_mapping["channel_order"]
    if type(channel_order) is not tuple:
        raise ContractValidationError("channel order is invalid")

    fault_types_value = envelope.config["fault_types"]
    if type(fault_types_value) is not tuple:
        raise ContractValidationError("frozen fault types are invalid")
    normalized_fault_types = tuple(normalize_fault_types(fault_types_value))
    if normalized_fault_types != fault_types_value:
        raise ContractValidationError("frozen fault types are not normalized")
    return (
        channel_order,
        normalized_fault_types,
        _workorder_text(envelope.workorder, "sn", "sn_code"),
        _workorder_text(envelope.workorder, "model", "product_model"),
    )


def _workorder_text(
    workorder: object,
    primary: str,
    fallback: str,
) -> str:
    if not isinstance(workorder, Mapping):
        return ""
    for key in (primary, fallback):
        value = workorder.get(key)
        if type(value) is str and value.strip():
            return value.strip()
    return ""


def _validate_judgment_facts(
    facts: _JoinedFacts,
    fault_types: tuple[str, ...],
) -> None:
    if facts.judgment_source not in {
        item.value for item in JudgmentSource
    }:
        raise ContractValidationError("judgment source is invalid")
    if facts.judgment is not None and facts.judgment not in {
        item.value for item in Judgment
    }:
        raise ContractValidationError("judgment is invalid")

    if facts.judgment_source == JudgmentSource.NONE.value:
        if (
            facts.judgment is not None
            or facts.judged_at is not None
            or facts.fault_type is not None
            or facts.manual_judgment_revision is not None
            or facts.state == RunState.SUCCEEDED.value
        ):
            raise ContractValidationError("empty judgment facts are inconsistent")
        return

    if facts.judgment is None or facts.judged_at is None:
        raise ContractValidationError("judgment facts are incomplete")
    validate_utc_timestamp(facts.judged_at)

    if facts.judgment_source == JudgmentSource.AUTOMATIC.value:
        if (
            facts.state != RunState.SUCCEEDED.value
            or facts.fault_type is not None
            or facts.manual_judgment_revision is not None
        ):
            raise ContractValidationError(
                "automatic judgment facts are inconsistent"
            )
        return

    revision = facts.manual_judgment_revision
    if type(revision) is not int or revision <= 0:
        raise ContractValidationError("manual judgment revision is invalid")
    if facts.judgment == Judgment.PASS.value:
        if facts.fault_type is not None:
            raise ContractValidationError(
                "manual PASS cannot carry a fault type"
            )
        return
    if fault_types:
        if (
            type(facts.fault_type) is not str
            or facts.fault_type not in fault_types
        ):
            raise ContractValidationError(
                "manual FAIL fault type is not a frozen accepted value"
            )
    elif facts.fault_type is not None:
        raise ContractValidationError(
            "manual FAIL cannot carry an unfrozen fault type"
        )


def _validate_audio_facts(
    facts: _JoinedFacts,
    channel_order: tuple[int, ...],
) -> None:
    if facts.audio_id is None:
        raise ContractValidationError(
            "terminal run has no committed audio value"
        )
    validate_uuid4(facts.audio_id)
    if facts.audio_run_id != facts.run_id:
        raise ContractValidationError("audio run identity is invalid")
    if (
        type(facts.final_path) is not str
        or not facts.final_path
        or not Path(facts.final_path).is_absolute()
    ):
        raise ContractValidationError("audio source path is invalid")
    if facts.container_format != "WAV_PCM":
        raise ContractValidationError("audio container is invalid")
    for name in (
        "sample_rate_hz",
        "channel_count",
        "frame_count",
        "duration_ms",
        "byte_size",
    ):
        value = getattr(facts, name)
        if type(value) is not int or value <= 0:
            raise ContractValidationError(f"audio {name} is invalid")
    if facts.channel_count != len(channel_order):
        raise ContractValidationError(
            "audio channel count does not match durable channel order"
        )
    if facts.closed_verified_at is None or facts.audio_created_at is None:
        raise ContractValidationError("audio timestamps are missing")
    validate_utc_timestamp(facts.closed_verified_at)
    validate_utc_timestamp(facts.audio_created_at)
