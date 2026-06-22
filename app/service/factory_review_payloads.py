from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FactoryReviewWindow:
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class ImportAudioWarning:
    code: str
    message: str
    severity: str = "warning"
    channel_index: int | None = None
    wav_channel_index: int | None = None
    metric: str | None = None


@dataclass(frozen=True)
class FactoryReviewPayload:
    displayed_config_id: int | None = None
    config_payload: dict | None = None
    run_id: str | None = None
    trigger_source: str | None = None
    captured_duration_sec: float | None = None
    default_window: FactoryReviewWindow | None = None
    fallback_window: FactoryReviewWindow | None = None
    manual_reanalysis_allowed: bool = False
    no_result: bool = False
    error_message: str | None = None
    judgment_payload: dict | None = None
    summary_payload: dict | None = None
    chart_payload: dict | None = None
    import_warnings: tuple[ImportAudioWarning, ...] = ()
    display_channel_labels: dict[int, str] = field(default_factory=dict)
