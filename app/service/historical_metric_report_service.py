from __future__ import annotations

from app.service.analysis.judgment import JudgmentEngine
from app.service.audio_data_service import AudioDataService
from app.service.historical_metric_report_cache import HistoricalMetricCacheComputer
from app.service.historical_metric_report_types import (
    HISTORICAL_REPORT_ALGORITHM_VERSION,
)


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
