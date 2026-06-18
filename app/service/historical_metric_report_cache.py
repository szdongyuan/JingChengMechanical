from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from base.utils import normalize_path
from model.audio_data_table import AudioDataTable
from model.historical_metric_cache_table import HistoricalMetricCacheTable
from app.service.analysis.imported_audio import load_wav, selected_config_channels
from app.service.analysis.review_payloads import build_chart_payload, canonical_channel_label
from app.service.historical_metric_report_types import (
    METRIC_DEFINITIONS,
    RECORD_STATUS_METRIC_KEY,
    SourceSignature,
)


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

    def current_calibration_hash(self, mic_cal: dict | None = None) -> str:
        if mic_cal is None:
            mic_cal = self.current_mic_calibration()
        return _stable_hash(mic_cal)

    def config_hash(self, config_content: object) -> str:
        text = "" if config_content is None else str(config_content)
        if not text.strip():
            return "__missing_config__"
        try:
            parsed = json.loads(text)
        except Exception:
            return "__invalid_config__:" + _sha256_text(text)
        return _stable_hash(parsed)

    def source_signature_for_record(
        self,
        record: AudioDataTable,
        *,
        mic_cal: dict | None = None,
    ) -> SourceSignature:
        normalized_path = _normalize_file_path(record.file_path)
        file_size, file_mtime_ns = _file_identity(normalized_path)
        return SourceSignature(
            file_path=normalized_path,
            file_size=file_size,
            file_mtime_ns=file_mtime_ns,
            config_hash=self.config_hash(record.config_content),
            calibration_hash=self.current_calibration_hash(mic_cal),
            algorithm_version=self._algorithm_version,
        )

    def valid_cache_rows_for_record(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
    ) -> list[HistoricalMetricCacheTable]:
        rows = list(
            HistoricalMetricCacheTable.select()
            .where(HistoricalMetricCacheTable.audio_data_id == record.audio_data_id)
            .order_by(
                HistoricalMetricCacheTable.channel_index,
                HistoricalMetricCacheTable.metric_key,
            )
        )
        if not rows:
            return []

        effective_signature = self._effective_signature_for_cached_rows(
            rows,
            signature,
        )
        for row in rows:
            if not self._row_matches_signature(row, effective_signature):
                return []
        return rows

    def refresh_record_cache(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        *,
        mic_cal: dict | None = None,
    ) -> list[HistoricalMetricCacheTable]:
        if mic_cal is None:
            mic_cal = self.current_mic_calibration()
        signature = replace(
            signature,
            calibration_hash=self.current_calibration_hash(mic_cal),
        )
        rows = self._compute_record_cache_rows(record, signature, mic_cal)
        database = HistoricalMetricCacheTable._meta.database
        with database.atomic():
            HistoricalMetricCacheTable.delete().where(
                HistoricalMetricCacheTable.audio_data_id == record.audio_data_id
            ).execute()
            for payload in rows:
                HistoricalMetricCacheTable.create(**payload)
        return list(
            HistoricalMetricCacheTable.select()
            .where(HistoricalMetricCacheTable.audio_data_id == record.audio_data_id)
            .order_by(
                HistoricalMetricCacheTable.channel_index,
                HistoricalMetricCacheTable.metric_key,
            )
        )

    def _effective_signature_for_cached_rows(
        self,
        rows: list[HistoricalMetricCacheTable],
        signature: SourceSignature,
    ) -> SourceSignature:
        has_failed_file_identity = any(
            row.file_size == -1 or row.file_mtime_ns == -1 for row in rows
        )
        if not has_failed_file_identity or not signature.file_path:
            return signature
        try:
            load_wav(signature.file_path)
        except Exception:
            return replace(signature, file_size=-1, file_mtime_ns=-1)
        return signature

    def _row_matches_signature(
        self,
        row: HistoricalMetricCacheTable,
        signature: SourceSignature,
    ) -> bool:
        return (
            row.file_path == signature.file_path
            and int(row.file_size) == signature.file_size
            and int(row.file_mtime_ns) == signature.file_mtime_ns
            and row.config_hash == signature.config_hash
            and row.calibration_hash == signature.calibration_hash
            and row.algorithm_version == signature.algorithm_version
        )

    def _compute_record_cache_rows(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        mic_cal: dict,
    ) -> list[dict[str, object]]:
        config_result = _parse_config(record.config_content)
        if isinstance(config_result, str):
            return [
                self._record_failure(
                    record,
                    signature,
                    config_result,
                )
            ]
        config = config_result

        selected_channels = selected_config_channels(config)
        if not selected_channels:
            return [
                self._record_failure(
                    record,
                    signature,
                    "配置未包含有效通道",
                )
            ]

        if not signature.file_path:
            return [
                self._record_failure(
                    record,
                    signature,
                    "WAV 文件路径缺失",
                )
            ]

        try:
            wav = load_wav(signature.file_path)
        except Exception as exc:
            return [
                self._record_failure(
                    record,
                    _failed_file_signature(signature),
                    f"WAV 文件读取失败: {exc}",
                )
            ]

        if wav.channel_count != len(selected_channels):
            return [
                self._record_failure(
                    record,
                    signature,
                    "WAV 通道数量必须等于已选通道数量",
                )
            ]

        try:
            start_sec, end_sec = _strict_analysis_window(config, wav.duration_sec)
        except ValueError as exc:
            return [self._record_failure(record, signature, str(exc))]

        channel_column_map = {
            selected_channel: column_index
            for column_index, selected_channel in enumerate(selected_channels)
        }
        loudness_trace_cache: dict[int, dict] = {}
        try:
            judgment_result = self._judgment_engine.rejudge(
                config,
                wav.buffer,
                wav.sample_rate,
                mic_cal,
                wav.path,
                start_sec,
                end_sec,
                run_ai=False,
                allow_golden_runtime_resample=True,
                channel_column_map=channel_column_map,
                loudness_trace_cache=loudness_trace_cache,
            )
        except Exception as exc:
            return [
                self._record_failure(
                    record,
                    signature,
                    f"指标判定失败: {exc}",
                )
            ]

        rows = self._extract_judgment_metric_rows(
            record,
            signature,
            config,
            selected_channels,
            judgment_result,
        )

        try:
            chart_payload = build_chart_payload(
                buffer=wav.buffer,
                config=config,
                sample_rate=wav.sample_rate,
                mic_cal=mic_cal,
                analysis_window=(start_sec, end_sec),
                judgment_payload=asdict(judgment_result),
                allow_golden_runtime_resample=True,
                channel_column_map=channel_column_map,
                loudness_trace_cache=loudness_trace_cache,
            )
        except Exception as exc:
            rows.append(
                self._record_failure(
                    record,
                    signature,
                    f"图表指标计算失败: {exc}",
                )
            )
            return rows

        rows.extend(
            self._extract_chart_metric_rows(
                record,
                signature,
                config,
                selected_channels,
                chart_payload,
            )
        )
        if not rows:
            return [
                self._record_failure(
                    record,
                    signature,
                    "没有可用的有限指标值",
                    status="skipped",
                )
            ]
        return rows

    def _extract_judgment_metric_rows(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        config: dict,
        selected_channels: tuple[int, ...],
        judgment_result,
    ) -> list[dict[str, object]]:
        vibration_channel = _valid_vibration_channel(config, selected_channels)
        acoustic_channels = {
            channel
            for channel in selected_channels
            if channel != vibration_channel
        }
        by_channel = {
            result.channel_index: result
            for result in getattr(judgment_result, "channel_results", [])
        }
        rows: list[dict[str, object]] = []
        for channel_index in acoustic_channels:
            result = by_channel.get(channel_index)
            if result is None:
                continue
            spl_peak = _first_finite(
                getattr(result, "spl_max_db", None),
                getattr(result, "spl_db", None),
            )
            rows.extend(
                self._metric_row_if_finite(
                    record,
                    signature,
                    channel_index,
                    metric_key,
                    value,
                )
                for metric_key, value in (
                    ("spl_peak_db", spl_peak),
                    ("spl_average_db", getattr(result, "spl_average_db", None)),
                    ("loudness_peak_sone", getattr(result, "loudness_max_sone", None)),
                    (
                        "loudness_average_sone",
                        getattr(result, "loudness_average_sone", None),
                    ),
                )
                if _is_finite_number(value)
            )
        return rows

    def _extract_chart_metric_rows(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        config: dict,
        selected_channels: tuple[int, ...],
        chart_payload: dict[int, dict],
    ) -> list[dict[str, object]]:
        vibration_channel = _valid_vibration_channel(config, selected_channels)
        acoustic_channels = {
            channel
            for channel in selected_channels
            if channel != vibration_channel
        }
        rows: list[dict[str, object]] = []
        for channel_index in acoustic_channels:
            channel_payload = chart_payload.get(channel_index, {})
            if not isinstance(channel_payload, dict):
                continue
            stft_score = _nested_value(
                channel_payload,
                "stft_score",
                "summary_score",
            )
            if _is_finite_number(stft_score):
                rows.append(
                    self._metric_row(
                        record,
                        signature,
                        channel_index,
                        "stft_score",
                        float(stft_score),
                    )
                )

        if vibration_channel is None:
            return rows
        vibration_payload = chart_payload.get(vibration_channel, {})
        if not isinstance(vibration_payload, dict):
            return rows
        vibration_payload = vibration_payload.get("vibration", {})
        if not isinstance(vibration_payload, dict):
            return rows
        for metric_key, payload_key in (
            ("vibration_rms", "overall_rms"),
            ("vibration_peak", "overall_peak"),
            ("vibration_peak_factor", "overall_crest_factor"),
            ("vibration_main_frequency_hz", "dominant_frequency_hz"),
        ):
            value = vibration_payload.get(payload_key)
            if _is_finite_number(value):
                rows.append(
                    self._metric_row(
                        record,
                        signature,
                        vibration_channel,
                        metric_key,
                        float(value),
                    )
                )
        return rows

    def _metric_row_if_finite(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        channel_index: int,
        metric_key: str,
        value: object,
    ) -> dict[str, object]:
        return self._metric_row(
            record,
            signature,
            channel_index,
            metric_key,
            float(value),
        )

    def _metric_row(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        channel_index: int,
        metric_key: str,
        value: float,
    ) -> dict[str, object]:
        metric_label, metric_unit = METRIC_DEFINITIONS[metric_key]
        payload = self._base_payload(record, signature)
        payload.update(
            {
                "channel_index": channel_index,
                "channel_label": canonical_channel_label(channel_index),
                "metric_key": metric_key,
                "metric_label": metric_label,
                "metric_value": value,
                "metric_unit": metric_unit,
                "status": "ok",
                "error_message": None,
            }
        )
        return payload

    def _record_failure(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
        error_message: str,
        *,
        status: str = "failed",
    ) -> dict[str, object]:
        payload = self._base_payload(record, signature)
        payload.update(
            {
                "channel_index": -1,
                "channel_label": "记录",
                "metric_key": RECORD_STATUS_METRIC_KEY,
                "metric_label": "记录状态",
                "metric_value": None,
                "metric_unit": "",
                "status": status,
                "error_message": error_message,
            }
        )
        return payload

    def _base_payload(
        self,
        record: AudioDataTable,
        signature: SourceSignature,
    ) -> dict[str, object]:
        return {
            "audio_data_id": record.audio_data_id,
            "file_path": signature.file_path,
            "file_size": signature.file_size,
            "file_mtime_ns": signature.file_mtime_ns,
            "record_date": record.record_date,
            "product_model": record.product_model,
            "barcode": record.barcode,
            "label": record.labels,
            "label_source": record.label_source,
            "analyse_config": record.analyse_config,
            "config_hash": signature.config_hash,
            "calibration_hash": signature.calibration_hash,
            "algorithm_version": signature.algorithm_version,
            "computed_at": _computed_at(),
        }


def _stable_hash(value: object) -> str:
    return _sha256_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_file_path(file_path: object) -> str:
    normalized = normalize_path(file_path)
    return "" if normalized is None else str(normalized)


def _file_identity(file_path: str) -> tuple[int, int]:
    if not file_path:
        return -1, -1
    try:
        stat = Path(file_path).stat()
    except OSError:
        return -1, -1
    return int(stat.st_size), int(stat.st_mtime_ns)


def _failed_file_signature(signature: SourceSignature) -> SourceSignature:
    return replace(signature, file_size=-1, file_mtime_ns=-1)


def _parse_config(config_content: object) -> dict | str:
    text = "" if config_content is None else str(config_content)
    if not text.strip():
        return "配置快照缺失"
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return f"配置快照无效: {exc}"
    if not isinstance(parsed, dict):
        return "配置快照无效"
    return parsed


def _strict_analysis_window(
    config: dict,
    wav_duration_sec: float,
) -> tuple[float, float]:
    try:
        offset_ms = float(config.get("analysis_window_offset_ms", 0) or 0)
        duration_sec = float(config.get("analysis_duration", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("分析窗口超出 WAV 时长或无效") from None
    start_sec = offset_ms / 1000.0
    end_sec = start_sec + duration_sec
    if (
        start_sec < 0.0
        or duration_sec <= 0.0
        or not math.isfinite(start_sec)
        or not math.isfinite(duration_sec)
        or end_sec > float(wav_duration_sec) + 1e-9
    ):
        raise ValueError("分析窗口超出 WAV 时长或无效")
    return start_sec, end_sec


def _valid_vibration_channel(
    config: dict,
    selected_channels: tuple[int, ...],
) -> int | None:
    value = config.get("vibration_view_channel_index")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in selected_channels else None


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _first_finite(*values: object) -> float | None:
    for value in values:
        if _is_finite_number(value):
            return float(value)
    return None


def _nested_value(payload: dict, *keys: str) -> object:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _computed_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
