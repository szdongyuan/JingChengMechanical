"""Narrow immutable values shared by local run-persistence repositories."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import math
import ntpath
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import re
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from app.service.run_persistence.analysis_envelope import (
    decode_analysis_envelope,
    freeze_analysis_envelope,
)


class ContractValidationError(ValueError):
    """A persistence value failed validation before database access."""


def canonicalize_absolute_path(value: str) -> str:
    """Resolve once and return the canonical Windows absolute-path spelling."""

    if type(value) is not str or not value:
        raise ContractValidationError("path must be an exact non-empty string")
    resolved = str(Path(value).resolve(strict=False))
    normalized = ntpath.normpath(resolved)
    if not ntpath.isabs(normalized) or ntpath.normpath(normalized) != normalized:
        raise ContractValidationError("path must resolve to absolute")
    return normalized


class IngressKind(str, Enum):
    CAPTURED = "CAPTURED"
    IMPORTED = "IMPORTED"


class TriggerSource(str, Enum):
    PLC = "PLC"
    MANUAL = "MANUAL"
    SCANNER = "SCANNER"
    IMPORT = "IMPORT"


class RunState(str, Enum):
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    NO_RESULT = "NO_RESULT"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"


class Judgment(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class JudgmentSource(str, Enum):
    NONE = "NONE"
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"


class AttemptKind(str, Enum):
    INITIAL = "INITIAL"
    REANALYSIS = "REANALYSIS"


class AttemptState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    NO_RESULT = "NO_RESULT"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"


class TransactionState(str, Enum):
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


def new_uuid4() -> str:
    """Return a process-local secure random UUIDv4 in canonical text form."""

    return str(uuid4())


def validate_uuid4(value: str) -> str:
    """Validate lowercase canonical UUIDv4 text and return it unchanged."""

    if not isinstance(value, str):
        raise ContractValidationError("UUIDv4 must be lowercase canonical text")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ContractValidationError(
            "UUIDv4 must be lowercase canonical text"
        ) from None
    if parsed.version != 4 or str(parsed) != value:
        raise ContractValidationError("UUIDv4 must be lowercase canonical text")
    return value


def validate_utc_timestamp(value: str) -> str:
    """Validate the durable UTC timestamp format required by the schema."""

    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ContractValidationError(
            "UTC timestamp must use YYYY-MM-DDTHH:MM:SS.ffffffZ"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise ContractValidationError(
            "UTC timestamp must use YYYY-MM-DDTHH:MM:SS.ffffffZ"
        ) from None
    return value


def validate_snapshot_object(
    value: object, name: str = "snapshot"
) -> Mapping[str, object]:
    """Deep-copy and validate one JSON-object snapshot."""

    copied, _ = _serialize_snapshot_object(value, name)
    return copied


def _serialize_snapshot_object(
    value: object, name: str
) -> tuple[Mapping[str, object], str]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{name} must be a valid JSON object")
    copied = deepcopy(dict(value))
    try:
        serialized = json.dumps(copied, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ContractValidationError(f"{name} must be a valid JSON object") from None
    return copied, serialized


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _validate_snapshot_json(value: str, name: str) -> None:
    if type(value) is not str:
        raise ContractValidationError(f"{name} must be a valid JSON object")
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, ValueError):
        raise ContractValidationError(f"{name} must be a valid JSON object") from None
    if not isinstance(decoded, dict):
        raise ContractValidationError(f"{name} must be a valid JSON object")


@dataclass(frozen=True)
class FrozenRunInputs:
    config_snapshot_json: str
    hardware_snapshot_json: str
    calibration_snapshot_json: str

    def __post_init__(self) -> None:
        decode_analysis_envelope(self.config_snapshot_json)
        _validate_snapshot_json(self.hardware_snapshot_json, "hardware_snapshot")
        _validate_snapshot_json(
            self.calibration_snapshot_json, "calibration_snapshot"
        )


@dataclass(frozen=True)
class CurrentTrace:
    samples: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if type(self.samples) is not tuple or not self.samples:
            raise ContractValidationError(
                "current trace samples must be a non-empty tuple"
            )
        previous_time: float | int | None = None
        for sample in self.samples:
            if type(sample) is not tuple or len(sample) != 2:
                raise ContractValidationError(
                    "current trace samples must be exact two-item tuples"
                )
            time_sec, current_a = sample
            if (
                type(time_sec) not in (int, float)
                or type(current_a) not in (int, float)
                or not math.isfinite(time_sec)
                or not math.isfinite(current_a)
            ):
                raise ContractValidationError(
                    "current trace values must be finite non-boolean numbers"
                )
            if time_sec < 0 or (
                previous_time is not None and time_sec <= previous_time
            ):
                raise ContractValidationError(
                    "current trace time must be nonnegative and strictly increasing"
                )
            previous_time = time_sec


def _validate_channel_mapping(snapshot_json: str) -> None:
    envelope = decode_analysis_envelope(snapshot_json)
    mapping = envelope.channel_mapping
    if set(mapping) != {"schema", "channel_order"}:
        raise ContractValidationError(
            "channel_mapping must have exact audio-channel-map/v1 shape"
        )
    if mapping["schema"] != "audio-channel-map/v1":
        raise ContractValidationError(
            "channel_mapping schema must be audio-channel-map/v1"
        )
    channel_order = mapping["channel_order"]
    if type(channel_order) is not tuple or not channel_order:
        raise ContractValidationError(
            "channel_order must be a nonempty array"
        )
    seen: set[int] = set()
    for identity in channel_order:
        if type(identity) is not int or identity < 0 or identity in seen:
            raise ContractValidationError(
                "channel_order must contain unique nonnegative integers"
            )
        seen.add(identity)


def freeze_run_inputs(
    config_snapshot: Mapping[str, object],
    hardware_snapshot: Mapping[str, object] | None = None,
    calibration_snapshot: Mapping[str, object] | None = None,
) -> FrozenRunInputs:
    """Deep-copy, validate, and serialize the three run snapshots once."""

    none_snapshot = {"kind": "NONE"}
    config_json = freeze_analysis_envelope(config_snapshot)
    _, hardware_json = _serialize_snapshot_object(
        none_snapshot if hardware_snapshot is None else hardware_snapshot,
        "hardware_snapshot",
    )
    _, calibration_json = _serialize_snapshot_object(
        none_snapshot if calibration_snapshot is None else calibration_snapshot,
        "calibration_snapshot",
    )
    return FrozenRunInputs(
        config_snapshot_json=config_json,
        hardware_snapshot_json=hardware_json,
        calibration_snapshot_json=calibration_json,
    )


@dataclass(frozen=True)
class RunInsert:
    run_id: str
    ingress_kind: IngressKind
    trigger_source: TriggerSource
    config_id: int
    hardware_id: int | None
    snapshots: FrozenRunInputs
    accepted_at: str
    imported_source_identity: str | None = None
    imported_source_path: str | None = None
    imported_expected_frame_count: int | None = None
    state: RunState = RunState.ACTIVE

    def __post_init__(self) -> None:
        validate_uuid4(self.run_id)
        validate_utc_timestamp(self.accepted_at)
        if type(self.config_id) is not int:
            raise ContractValidationError("config_id must be an integer")
        if self.hardware_id is not None and type(self.hardware_id) is not int:
            raise ContractValidationError("hardware_id must be an integer or None")
        if not isinstance(self.snapshots, FrozenRunInputs):
            raise ContractValidationError("snapshots must be FrozenRunInputs")
        _validate_channel_mapping(self.snapshots.config_snapshot_json)
        if not isinstance(self.ingress_kind, IngressKind):
            raise ContractValidationError(
                "ingress_kind must be an IngressKind enum value"
            )
        if not isinstance(self.trigger_source, TriggerSource):
            raise ContractValidationError(
                "trigger_source must be a TriggerSource enum value"
            )
        if self.state is not RunState.ACTIVE:
            raise ContractValidationError("new run state must be ACTIVE")
        if self.ingress_kind is IngressKind.CAPTURED and self.hardware_id is None:
            raise ContractValidationError("captured run requires hardware_id")
        if (
            self.ingress_kind is IngressKind.IMPORTED
            and self.trigger_source is not TriggerSource.IMPORT
        ):
            raise ContractValidationError("imported run requires IMPORT trigger")
        if (
            self.ingress_kind is IngressKind.CAPTURED
            and self.trigger_source is TriggerSource.IMPORT
        ):
            raise ContractValidationError("captured run cannot use IMPORT trigger")
        if self.ingress_kind is IngressKind.CAPTURED:
            if any(
                value is not None
                for value in (
                    self.imported_source_identity,
                    self.imported_source_path,
                    self.imported_expected_frame_count,
                )
            ):
                raise ContractValidationError(
                    "captured run forbids imported source facts"
                )
            return
        if (
            type(self.imported_source_identity) is not str
            or not self.imported_source_identity.strip()
        ):
            raise ContractValidationError(
                "imported run requires nonempty source identity"
            )
        if (
            type(self.imported_source_path) is not str
            or not self.imported_source_path.strip()
            or not (
                PurePath(self.imported_source_path).is_absolute()
                or PurePosixPath(self.imported_source_path).is_absolute()
                or PureWindowsPath(self.imported_source_path).is_absolute()
            )
        ):
            raise ContractValidationError(
                "imported run requires an absolute source path"
            )
        object.__setattr__(
            self,
            "imported_source_path",
            canonicalize_absolute_path(self.imported_source_path),
        )
        if (
            type(self.imported_expected_frame_count) is not int
            or self.imported_expected_frame_count <= 0
        ):
            raise ContractValidationError(
                "imported run requires a positive expected frame count"
            )


_VERIFIED_AUDIO_FACT_ISSUED = object()


@dataclass(frozen=True, init=False)
class VerifiedAudioFact:
    audio_id: str
    save_job_id: str
    run_id: str
    source_kind: str
    source_identity: str
    content_revision: int
    final_path: str
    container_format: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    duration_ms: int
    byte_size: int
    closed_verified_at: str
    created_at: str
    _issued_marker: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise ContractValidationError(
            "VerifiedAudioFact may only be issued after audio publication"
        )

    def _validate(self) -> None:
        validate_uuid4(self.audio_id)
        validate_uuid4(self.save_job_id)
        validate_uuid4(self.run_id)
        validate_utc_timestamp(self.closed_verified_at)
        validate_utc_timestamp(self.created_at)
        if self.container_format != "WAV_PCM":
            raise ContractValidationError("container_format must be WAV_PCM")
        if not isinstance(self.final_path, str) or not self.final_path:
            raise ContractValidationError("final_path must be a non-empty string")
        if self.source_kind not in {"CAPTURED_AUDIO", "IMPORTED_SOURCE"}:
            raise ContractValidationError(
                "source_kind must be CAPTURED_AUDIO or IMPORTED_SOURCE"
            )
        if (
            type(self.source_identity) is not str
            or not self.source_identity.strip()
        ):
            raise ContractValidationError(
                "source_identity must be nonempty text"
            )
        if type(self.content_revision) is not int or self.content_revision <= 0:
            raise ContractValidationError(
                "content_revision must be a positive integer"
            )
        for name in (
            "sample_rate_hz",
            "channel_count",
            "frame_count",
            "duration_ms",
            "byte_size",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContractValidationError(
                    f"{name} must be a positive integer"
                )


def _issue_verified_audio_fact(
    *,
    audio_id: str,
    save_job_id: str,
    run_id: str,
    source_kind: str,
    source_identity: str,
    content_revision: int,
    final_path: str,
    container_format: str,
    sample_rate_hz: int,
    channel_count: int,
    frame_count: int,
    duration_ms: int,
    byte_size: int,
    closed_verified_at: str,
    created_at: str,
) -> VerifiedAudioFact:
    """Internal constructor used only by the successful publication boundary."""

    fact = object.__new__(VerifiedAudioFact)
    for name, value in (
        ("audio_id", audio_id),
        ("save_job_id", save_job_id),
        ("run_id", run_id),
        ("source_kind", source_kind),
        ("source_identity", source_identity),
        ("content_revision", content_revision),
        ("final_path", final_path),
        ("container_format", container_format),
        ("sample_rate_hz", sample_rate_hz),
        ("channel_count", channel_count),
        ("frame_count", frame_count),
        ("duration_ms", duration_ms),
        ("byte_size", byte_size),
        ("closed_verified_at", closed_verified_at),
        ("created_at", created_at),
    ):
        object.__setattr__(fact, name, value)
    fact._validate()
    object.__setattr__(fact, "_issued_marker", _VERIFIED_AUDIO_FACT_ISSUED)
    return fact


def _is_issued_verified_audio_fact(value: object) -> bool:
    return (
        type(value) is VerifiedAudioFact
        and getattr(value, "_issued_marker", None) is _VERIFIED_AUDIO_FACT_ISSUED
    )


@dataclass(frozen=True)
class AttemptInsert:
    attempt_id: str
    run_id: str
    audio_id: str
    attempt_kind: AttemptKind
    queued_at: str
    state: AttemptState = AttemptState.QUEUED

    def __post_init__(self) -> None:
        validate_uuid4(self.attempt_id)
        validate_uuid4(self.run_id)
        validate_uuid4(self.audio_id)
        validate_utc_timestamp(self.queued_at)
        if not isinstance(self.attempt_kind, AttemptKind):
            raise ContractValidationError(
                "attempt_kind must be an AttemptKind enum value"
            )
        if self.state is not AttemptState.QUEUED:
            raise ContractValidationError("new attempt state must be QUEUED")


_ResultValue = TypeVar("_ResultValue")


@dataclass(frozen=True)
class TransactionResult(Generic[_ResultValue]):
    state: TransactionState
    value: _ResultValue | None = None
    reason: str | None = None
    restart_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.state, TransactionState):
            raise ContractValidationError(
                "state must be a TransactionState enum value"
            )
        if self.reason is not None and not isinstance(self.reason, str):
            raise ContractValidationError("reason must be a string or None")
        if type(self.restart_required) is not bool:
            raise ContractValidationError("restart_required must be a bool")
