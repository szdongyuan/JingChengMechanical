from __future__ import annotations

from datetime import datetime

from app.service.analysis.judgment import JudgmentEngine
from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_cache import HistoricalMetricCacheComputer
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
        return list(self._audio_data_service._filtered_query(filters))

    def config_hash(self, config_content: object) -> str:
        return self._cache.config_hash(config_content)

    def current_calibration_hash(self) -> str:
        return self._cache.current_calibration_hash()

    def source_signature_for_record(self, record: AudioDataTable) -> SourceSignature:
        return self._cache.source_signature_for_record(record)

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

            signature = self._cache.source_signature_for_record(record)
            cache_rows = self._cache.valid_cache_rows_for_record(record, signature)
            if not cache_rows:
                cache_rows = self._cache.refresh_record_cache(record, signature)
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
            stats=[],
            failures=failures,
            outliers=[],
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
            channel_label=row.channel_label,
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
