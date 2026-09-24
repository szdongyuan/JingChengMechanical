"""UI-independent contracts for the single unfinished import lane."""
from dataclasses import dataclass
from enum import Enum


class ImportPhase(str, Enum):
    ACCEPTED = 'accepted'
    WAITING = 'waiting'
    PREPARING = 'preparing'
    SAVING = 'saving'
    ANALYZING = 'analyzing'
    COMPLETING_CHARTS = 'completing_charts'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELED = 'canceled'


@dataclass(frozen=True)
class ImportRequest:
    source_path: str
    config_id: int
    assignments: tuple[dict, ...]
    start_seconds: float | None = None
    duration_seconds: float | None = None
    use_mic_calibration: bool = True


@dataclass(frozen=True)
class ImportSubmission:
    accepted: bool
    run_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ImportStatus:
    run_id: str
    generation: int
    phase: ImportPhase
    error: str | None = None
    warnings: tuple[str, ...] = ()
    result_ready: bool = False
    charts_ready: bool = False

    @property
    def finished(self):
        return self.phase in (ImportPhase.COMPLETED, ImportPhase.FAILED, ImportPhase.CANCELED)
