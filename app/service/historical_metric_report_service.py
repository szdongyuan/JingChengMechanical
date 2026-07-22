from __future__ import annotations

from datetime import datetime
import json
import re

from app.service.analysis.judgment import JudgmentEngine
from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_cache import HistoricalMetricCacheComputer
from app.service.historical_metric_report_statistics import (
    compute_outliers,
    compute_statistics,
)
from app.service.historical_metric_report_types import (
    CancelCheck,
    HISTORICAL_REPORT_ALGORITHM_VERSION,
    HistoricalMetricReportResult,
    HistoricalMetricValue,
    ProgressCallback,
    ReportCanceled,
    SourceSignature,
)
from model.audio_data_table import AudioDataTable
from model.historical_metric_cache_table import HistoricalMetricCacheTable


class HistoricalMetricReportService:
    def __init__(
        self,
        *,
        audio_data_service: AudioDataService,
        hss_service,
        judgment_engine=None,
        ai_service=None,
        golden_sample_service=None,
        algorithm_version: str = HISTORICAL_REPORT_ALGORITHM_VERSION,
    ):
        if judgment_engine is None:
            judgment_engine = JudgmentEngine(
                ai_service,
                golden_sample_service=golden_sample_service,
            )
        self._audio_data_service = audio_data_service
        self._cache = HistoricalMetricCacheComputer(
            hss_service=hss_service,
            judgment_engine=judgment_engine,
            algorithm_version=algorithm_version,
        )

    def list_source_records(self, filters: dict[str, object] | None):
        records = list(self._audio_data_service._filtered_query(filters))
        return _filter_records_by_barcode_regex(records, filters)

    def config_hash(self, config_content: object) -> str:
        return self._cache.config_hash(config_content)

    def current_calibration_hash(self) -> str:
        return self._cache.current_calibration_hash()

    def source_signature_for_record(
        self,
        record: AudioDataTable,
        *,
        mic_cal: dict | None = None,
    ) -> SourceSignature:
        return self._cache.source_signature_for_record(record, mic_cal=mic_cal)

    def generate_report(
        self,
        filters: dict[str, object] | None,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> HistoricalMetricReportResult:
        records = self.list_source_records(filters)
        total = len(records)
        if cancel_check is not None and cancel_check():
            raise ReportCanceled()

        metric_rows: list[HistoricalMetricValue] = []
        rows_by_record: dict[str, list[HistoricalMetricValue]] = {}
        for done, record in enumerate(records, start=1):
            if cancel_check is not None and cancel_check():
                raise ReportCanceled()

            mic_cal = self._cache.mic_calibration_for_record(record)
            signature = self._cache.source_signature_for_record(
                record,
                mic_cal=mic_cal,
            )
            cache_rows = self._cache.valid_cache_rows_for_record(record, signature)
            if not cache_rows:
                cache_rows = self._cache.refresh_record_cache(
                    record,
                    signature,
                    mic_cal=mic_cal,
                )
            converted = [
                self._cache_row_to_metric_value(record, row) for row in cache_rows
            ]
            metric_rows.extend(converted)
            rows_by_record[record.audio_data_id] = converted

            if progress_callback is not None:
                progress_callback(done, total, record.audio_data_id)
            if cancel_check is not None and cancel_check():
                raise ReportCanceled()

        valid_record_ids = {
            record_id
            for record_id, rows in rows_by_record.items()
            if any(row.status == "ok" for row in rows)
        }
        skipped_record_ids = {
            record_id
            for record_id, rows in rows_by_record.items()
            if any(row.status == "skipped" for row in rows)
        }
        failed_record_ids = {
            record_id
            for record_id, rows in rows_by_record.items()
            if any(row.status == "failed" for row in rows)
        }
        failures = [
            row for row in metric_rows if row.status in {"skipped", "failed"}
        ]

        return HistoricalMetricReportResult(
            filters=dict(filters or {}),
            generated_at=_generated_at(),
            total_records=total,
            valid_record_count=len(valid_record_ids),
            skipped_record_count=len(skipped_record_ids),
            failed_record_count=len(failed_record_ids),
            label_counts=_label_counts(records),
            metric_rows=metric_rows,
            stats=compute_statistics(metric_rows, group_by_metric_only=True),
            failures=failures,
            outliers=compute_outliers(metric_rows, group_by_metric_only=True),
        )

    def _cache_row_to_metric_value(
        self,
        record: AudioDataTable,
        row: HistoricalMetricCacheTable,
    ) -> HistoricalMetricValue:
        return HistoricalMetricValue(
            audio_data_id=record.audio_data_id,
            record_date=record.record_date,
            product_model=record.product_model,
            barcode=record.barcode,
            label=record.labels,
            label_source=record.label_source,
            analyse_config=record.analyse_config,
            channel_index=row.channel_index,
            channel_label=_display_channel_label(record, row),
            metric_key=row.metric_key,
            metric_label=row.metric_label,
            metric_value=(
                None if row.metric_value is None else float(row.metric_value)
            ),
            metric_unit=row.metric_unit,
            status=row.status,
            error_message=row.error_message,
        )


def _label_counts(records: list[AudioDataTable]) -> dict[str, int]:
    counts = {"OK": 0, "NG": 0, "not_labeled": 0}
    for record in records:
        label = str(record.labels or "").strip()
        if label == "OK":
            counts["OK"] += 1
        elif label == "NG":
            counts["NG"] += 1
        else:
            counts["not_labeled"] += 1
    return counts


def _generated_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _filter_records_by_barcode_regex(
    records: list[AudioDataTable],
    filters: dict[str, object] | None,
) -> list[AudioDataTable]:
    if not filters or not filters.get("barcode_regex"):
        return records
    pattern = _compile_barcode_regex(filters.get("barcode"))
    if pattern is None:
        return records
    return [
        record
        for record in records
        if pattern.search(str(record.barcode or ""))
    ]


def _compile_barcode_regex(value: object) -> re.Pattern[str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return re.compile(text)
    except re.error as exc:
        raise ValueError(f"SN 正则表达式无效: {exc}") from exc


def _display_channel_label(
    record: AudioDataTable,
    row: HistoricalMetricCacheTable,
) -> str:
    channel_index = _as_non_bool_int(row.channel_index)
    if channel_index is None or channel_index < 0:
        return row.channel_label

    config = _parse_config_dict(record.config_content)
    if not config:
        return row.channel_label

    selected_channels = _selected_channels(config)
    vibration_channel = _valid_vibration_channel(config, selected_channels)
    if vibration_channel is None or len(selected_channels) != 2:
        return row.channel_label
    if channel_index not in selected_channels:
        return row.channel_label

    role = "振动" if channel_index == vibration_channel else "麦克风"
    return f"{role}({_canonical_channel_label(channel_index)})"


def _parse_config_dict(config_content: object) -> dict | None:
    text = "" if config_content is None else str(config_content)
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _selected_channels(config: dict) -> tuple[int, ...]:
    selected: list[int] = []
    seen: set[int] = set()
    for value in config.get("selected_channels", []):
        channel_index = _as_non_bool_int(value)
        if channel_index is None or channel_index < 0 or channel_index in seen:
            continue
        selected.append(channel_index)
        seen.add(channel_index)
    return tuple(selected)


def _valid_vibration_channel(
    config: dict,
    selected_channels: tuple[int, ...],
) -> int | None:
    value = _as_non_bool_int(config.get("vibration_view_channel_index"))
    if value is None:
        return None
    return value if value in selected_channels else None


def _as_non_bool_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _canonical_channel_label(channel_index: int) -> str:
    return f"CH{channel_index + 1}"
