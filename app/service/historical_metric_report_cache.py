from __future__ import annotations

import hashlib
import json

from app.service.historical_metric_report_types import SourceSignature


class HistoricalMetricCacheComputer:
    def __init__(self, *, hss_service, judgment_engine=None, algorithm_version: str):
        self._hss_service = hss_service
        self._judgment_engine = judgment_engine
        self._algorithm_version = str(algorithm_version)

    def current_mic_calibration(self) -> dict:
        try:
            devices = self._hss_service.load_io_devices()
        except Exception:
            return {}
        mic_cal = devices.get("mic_cal") if isinstance(devices, dict) else None
        return mic_cal if isinstance(mic_cal, dict) else {}

    def current_calibration_hash(self) -> str:
        return _stable_hash(self.current_mic_calibration())

    def config_hash(self, config_content: object) -> str:
        text = "" if config_content is None else str(config_content)
        if not text.strip():
            return "__missing_config__"
        try:
            parsed = json.loads(text)
        except Exception:
            return "__invalid_config__:" + _sha256_text(text)
        return _stable_hash(parsed)


def _stable_hash(value: object) -> str:
    return _sha256_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
