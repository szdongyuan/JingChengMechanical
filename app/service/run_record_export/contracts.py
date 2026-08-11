"""Immutable, already-frozen inputs for run-record archive creation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunExportRecord:
    run_id: str
    accepted_at: str
    ingress_kind: str
    trigger_source: str
    config_id: int
    hardware_id: int | None
    sn: str
    model: str
    state: str
    judgment: str | None
    judgment_source: str
    fault_type: str | None
    manual_judgment_revision: int | None
    terminal_code: str | None
    terminal_at: str
    source_wav_path: str
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    channel_order: tuple[int, ...]
    current_samples: tuple[tuple[float, float], ...] | None


class RunExportReaderPort(Protocol):
    def read_many(
        self,
        run_ids: tuple[str, ...],
    ) -> tuple[RunExportRecord, ...]: ...
