"""Immutable data shared by torque storage, PLC processing, and the UI."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class TorqueConfig:
    plc_id: int
    enabled: bool = False
    match_type: str = "model"
    read_db_number: int | None = None
    read_byte_offset: int | None = None
    write_db_number: int | None = None
    write_byte_offset: int | None = None
    revision: int = 0


@dataclass(frozen=True)
class RuleSnapshot:
    revision: int
    values: Mapping[tuple[str, str], float]

    def __post_init__(self) -> None:
        # A proxy alone still exposes later changes to the caller's dictionary.
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class TorqueCycle:
    plc_id: int
    trigger_ns: int
    sn: str
    model: str
    config: TorqueConfig
    rules: RuleSnapshot
    inherited_db: int
    cycle_id: int | None = None


@dataclass(frozen=True)
class TorqueStatus:
    plc_id: int
    trigger_ns: int
    state: str
    value: float | None
    source: str | None
    match_type: str
    match_key: str
    rules_revision: int
    detail: str = ""
    read_value: float | None = None
    target_db: int | None = None
    target_byte_offset: int | None = None
    cycle_id: int | None = None


@dataclass(frozen=True)
class TorqueConfigApplied:
    """Configuration receipt identified by service lifetime, not a test start."""

    plc_id: int
    service_generation: str
    config_revision: int
    success: bool
    detail: str = ""


@dataclass(frozen=True)
class TorqueImportRow:
    row_number: int
    match_type: str
    match_key: str
    torque_value: float


@dataclass(frozen=True)
class TorqueImportError:
    row_number: int
    message: str


@dataclass(frozen=True)
class TorqueImportResult:
    rows: tuple[TorqueImportRow, ...] = ()
    errors: tuple[TorqueImportError, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'rows', tuple(self.rows))
        object.__setattr__(self, 'errors', tuple(self.errors))


@dataclass(frozen=True)
class TorqueImportChange:
    row: TorqueImportRow
    old_value: float


@dataclass(frozen=True)
class TorqueImportPreview:
    revision: int
    rows: tuple[TorqueImportRow, ...]
    additions: tuple[TorqueImportRow, ...]
    overwrites: tuple[TorqueImportChange, ...]
    errors: tuple[TorqueImportError, ...] = ()

    def __post_init__(self) -> None:
        for name in ('rows', 'additions', 'overwrites', 'errors'):
            object.__setattr__(self, name, tuple(getattr(self, name)))
