"""Read saved analysis independently from optional replay audio, without writes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from app.service.fault_types import normalize_fault_types
from app.service.run_persistence.analysis_envelope import (
    canonicalize_json_object,
    decode_analysis_envelope,
)
from app.service.run_persistence.analysis_result_artifact import (
    AnalysisResultArtifactError,
    AnalysisResultArtifactStore,
)
from app.service.run_persistence.attempt_repository import AttemptRecord, AttemptRepository
from app.service.run_persistence.audio_repository import AudioRepository
from app.service.run_persistence.contracts import (
    ContractValidationError,
    _validate_channel_mapping,
    validate_uuid4,
)
from app.service.run_persistence.database import RunPersistenceDatabase
from app.service.run_persistence.run_repository import RunRecord, RunRepository


class HistoricalAnalysisReadError(RuntimeError):
    """The primary run or its frozen configuration cannot be read."""


@dataclass(frozen=True, slots=True)
class HistoricalReplayAudio:
    run_id: str
    source_wav_path: Path
    sample_rate_hz: int
    channel_count: int
    frame_count: int
    channel_order: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HistoricalAnalysisSnapshot:
    """Detached saved values; run judgment is the current persisted status.

    ``review`` is the original result dict, including summary/chart payloads.
    Its JSON channel keys remain strings for the display layer to normalize.
    Section messages are None when available. Plain config/workorder/review
    containers belong to this read only and never reference live configuration.
    """

    run: RunRecord
    config: dict[str, object]
    workorder: dict[str, object]
    review: dict[str, object] | None
    successful_attempt_id: str | None
    original_analysis_judgment: str | None
    audio: HistoricalReplayAudio | None
    accepted_fault_types: tuple[str, ...]
    result_message: str | None
    charts_message: str | None
    audio_message: str | None


class HistoricalAnalysisReader:
    def __init__(
        self,
        runtime: RunPersistenceDatabase,
        *,
        result_artifact_store: AnalysisResultArtifactStore | None = None,
    ) -> None:
        if not isinstance(runtime, RunPersistenceDatabase):
            raise TypeError("runtime must be a RunPersistenceDatabase")
        if result_artifact_store is not None and not isinstance(
            result_artifact_store, AnalysisResultArtifactStore
        ):
            raise TypeError(
                "result_artifact_store must be an AnalysisResultArtifactStore"
            )
        self._runtime = runtime
        self._result_artifact_store = result_artifact_store

    def read(self, run_id: str) -> HistoricalAnalysisSnapshot:
        try:
            validate_uuid4(run_id)
        except ContractValidationError as exc:
            raise HistoricalAnalysisReadError("记录编号无效") from exc
        run = RunRepository(self._runtime).get(run_id)
        if run is None:
            raise HistoricalAnalysisReadError("记录不存在或已删除")
        try:
            envelope = decode_analysis_envelope(run.config_snapshot_json)
            config = json.loads(canonicalize_json_object(envelope.config, "config"))
            workorder = json.loads(
                canonicalize_json_object(envelope.workorder, "workorder")
            )
            fault_types = tuple(normalize_fault_types(config.get("fault_types", [])))
        except (ValueError, TypeError) as exc:
            raise HistoricalAnalysisReadError("记录冻结配置不可用") from exc

        attempt, review, result_message = self._read_result(run_id)
        audio, audio_message = self._read_audio(run)
        return HistoricalAnalysisSnapshot(
            run=run,
            config=config,
            workorder=workorder,
            review=review,
            successful_attempt_id=None if attempt is None else attempt.attempt_id,
            original_analysis_judgment=None if attempt is None else attempt.judgment,
            audio=audio,
            accepted_fault_types=fault_types,
            result_message=result_message,
            charts_message=(
                "尚无已保存图表，可刷新读取后来保存的结果"
                if review is not None and not review.get("chart_payload") else None
            ),
            audio_message=audio_message,
        )

    def _read_result(
        self, run_id: str
    ) -> tuple[AttemptRecord | None, dict[str, object] | None, str | None]:
        try:
            attempt = AttemptRepository(
                self._runtime, result_artifact_store=self._result_artifact_store
            ).latest_successful_for_run(run_id)
        except AnalysisResultArtifactError:
            # Read identity without hydrating the broken attachment. Never fall
            # back to an older success and present it as the selected result.
            attempt = AttemptRepository(self._runtime).latest_successful_for_run(run_id)
            return attempt, None, "已保存分析结果不可用（文件缺失或损坏）"
        if attempt is None:
            return None, None, "未保存成功分析结果"
        try:
            root = json.loads(attempt.result_payload_json)
            if (
                type(root) is not dict
                or set(root) != {"judgment", "result"}
                or root["judgment"] not in ("PASS", "FAIL")
                or root["judgment"] != attempt.judgment
                or type(root["result"]) is not dict
            ):
                raise ValueError("invalid saved result")
            review = root["result"]
            if "chart_payload" in review and type(review["chart_payload"]) is not dict:
                raise ValueError("invalid saved charts")
        except (ValueError, TypeError, RecursionError):
            return attempt, None, "已保存分析结果不可用（内容无效）"
        return attempt, review, None

    def _read_audio(
        self, run: RunRecord
    ) -> tuple[HistoricalReplayAudio | None, str | None]:
        record = AudioRepository(self._runtime).get_for_run(run.run_id)
        if record is None:
            return None, "未保存录音"
        try:
            _validate_channel_mapping(run.config_snapshot_json)
            envelope = decode_analysis_envelope(run.config_snapshot_json)
            channel_order = envelope.channel_mapping["channel_order"]
            path = Path(record.final_path)
            if (
                record.container_format != "WAV_PCM"
                or record.sample_rate_hz <= 0
                or record.frame_count <= 0
                or record.channel_count != len(channel_order)
                or not path.is_file()
                or path.stat().st_size != record.byte_size
            ):
                raise ValueError("invalid saved audio")
            return HistoricalReplayAudio(
                run_id=run.run_id,
                source_wav_path=path,
                sample_rate_hz=record.sample_rate_hz,
                channel_count=record.channel_count,
                frame_count=record.frame_count,
                channel_order=channel_order,
            ), None
        except (ValueError, TypeError, OSError):
            return None, "录音不可用（文件缺失或保存信息无效）"
