from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


HISTORICAL_REPORT_ALGORITHM_VERSION = "2026-06-18.1"
RECORD_STATUS_METRIC_KEY = "__record__"
METRIC_DEFINITIONS = {
    "spl_peak_db": ("声压级峰值", "dB"),
    "spl_average_db": ("平均声压级", "dB"),
    "loudness_peak_sone": ("响度峰值", "sone"),
    "loudness_average_sone": ("平均响度", "sone"),
    "stft_score": ("频谱最低得分", ""),
    "vibration_rms": ("振动 RMS", ""),
    "vibration_peak": ("振动峰值", ""),
    "vibration_peak_factor": ("振动峰值因子", ""),
    "vibration_main_frequency_hz": ("振动主频", "Hz"),
}


class ReportCanceled(Exception):
    pass


@dataclass(frozen=True)
class SourceSignature:
    file_path: str
    file_size: int
    file_mtime_ns: int
    config_hash: str
    calibration_hash: str
    algorithm_version: str


@dataclass(frozen=True)
class HistoricalMetricValue:
    audio_data_id: str
    record_date: str
    product_model: str
    barcode: str | None
    label: str | None
    label_source: str | None
    analyse_config: str | None
    channel_index: int
    channel_label: str
    metric_key: str
    metric_label: str
    metric_value: float | None
    metric_unit: str
    status: str
    error_message: str | None = None


@dataclass(frozen=True)
class MetricGroupStats:
    group_key: str
    product_model: str
    metric_key: str
    metric_label: str
    metric_unit: str
    channel_label: str
    n: int
    mean: float | None
    std: float | None
    min: float | None
    max: float | None
    p5: float | None
    p50: float | None
    p95: float | None
    histogram_bins: list[float] = field(default_factory=list)
    histogram_counts: list[int] = field(default_factory=list)
    normal_x: list[float] = field(default_factory=list)
    normal_y: list[float] = field(default_factory=list)
    normal_available: bool = False


@dataclass(frozen=True)
class HistoricalMetricReportResult:
    filters: dict[str, object]
    generated_at: str
    total_records: int
    valid_record_count: int
    skipped_record_count: int
    failed_record_count: int
    label_counts: dict[str, int]
    metric_rows: list[HistoricalMetricValue]
    stats: list[MetricGroupStats]
    failures: list[HistoricalMetricValue]
    outliers: list[HistoricalMetricValue]


ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]
